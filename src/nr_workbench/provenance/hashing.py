"""Content hashing for provenance records.

Every input a fit consumed is hashed, so a result can later be checked against
what is on disk: if a data file was re-reduced, the fit that used the old bytes
is stale and must say so.

Hashing a 200-slice tNR series on every command would be slow, so digests are
memoized in ``.nrw/cache/hashes.json`` keyed by ``(size, mtime_ns)``. That is a
cache, never a source of truth -- ``--verify`` style checks bypass it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Read in chunks so a large chain file does not land in memory whole.
_CHUNK = 1 << 20

#: The roles of a fit's inputs that define its model rather than its
#: measurements: the script it ran, or, for a fit run again with
#: ``nrw fit run --from``, the recorded fit's problem file.
MODEL_ROLES = frozenset({"script", "problem"})


@dataclass(frozen=True)
class FileDigest:
    """A hashed input file.

    Attributes:
        role: What the file was to this fit, e.g. ``"script"`` or
            ``"data:tnr:t000240"``. The role vocabulary is what makes a reverse
            lookup precise rather than a bag of paths.
        path: POSIX path relative to the project root.
        sha256: Hex digest of the file's bytes.
        bytes: File size in bytes.
    """

    role: str
    path: str
    sha256: str
    bytes: int

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serialisable form."""
        return {
            "role": self.role,
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
        }


def sha256_bytes(data: bytes) -> str:
    """Return the hex sha256 of ``data``.

    Args:
        data: Bytes to hash.

    Returns:
        Lowercase hex digest.
    """
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Return the hex sha256 of a file's contents.

    Args:
        path: File to hash.

    Returns:
        Lowercase hex digest.

    Raises:
        OSError: If the file cannot be read.
    """
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_file_or_none(path: Path) -> str | None:
    """:func:`sha256_file`, or ``None`` when the file cannot be read.

    For comparisons where an unreadable file is simply "not the same bytes":
    a copy that has been deleted or locked is then an edited copy, and a
    table that cannot be read does not match the manifest.
    """
    try:
        return sha256_file(path)
    except OSError:
        return None


class HashCache:
    """Memoizes file digests by ``(size, mtime_ns)``.

    Deliberately not keyed on content -- that would defeat the purpose. A file
    whose size and mtime are unchanged is assumed unchanged, which is the same
    assumption make(1) has relied on for decades. Anything that must be certain
    (staleness checks, verification) hashes directly instead.
    """

    def __init__(self, cache_path: Path | None = None) -> None:
        """Initialise the cache.

        Args:
            cache_path: Where to persist digests. ``None`` disables persistence,
                which is what tests and one-shot runs want.
        """
        self._path = Path(cache_path) if cache_path else None
        self._entries: dict[str, list[Any]] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        """Read the cache file, tolerating absence and corruption."""
        if self._path is None or not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            # A damaged cache is not worth reporting: it costs a re-hash and
            # is rewritten below.
            return
        if isinstance(data, dict):
            self._entries = {
                k: v for k, v in data.items() if isinstance(v, list) and len(v) == 3
            }

    def digest(self, path: Path) -> str:
        """Return the sha256 of ``path``, using the cache when it is valid.

        Args:
            path: File to hash.

        Returns:
            Lowercase hex digest.

        Raises:
            OSError: If the file cannot be read.
        """
        resolved = Path(path).resolve()
        stat = resolved.stat()
        key = str(resolved)
        cached = self._entries.get(key)
        if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
            return str(cached[2])

        value = sha256_file(resolved)
        self._entries[key] = [stat.st_size, stat.st_mtime_ns, value]
        self._dirty = True
        return value

    def save(self) -> None:
        """Persist the cache if it changed and a path was configured."""
        if self._path is None or not self._dirty:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(self._entries), encoding="utf-8")
        tmp.replace(self._path)
        self._dirty = False


def digest_files(
    entries: Iterable[tuple[str, Path]],
    *,
    root: Path,
    cache: HashCache | None = None,
) -> list[FileDigest]:
    """Hash a set of role-tagged files.

    Args:
        entries: Pairs of (role, absolute path).
        root: Project root, used to make paths relative.
        cache: Optional digest cache.

    Returns:
        Digests sorted by role then path, so the list is stable and diffable.

    Raises:
        FileNotFoundError: If any listed file is absent. A fit cannot be
            recorded against inputs that are not there.
    """
    root = Path(root).resolve()
    digests: list[FileDigest] = []

    for role, path in entries:
        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Input '{role}' not found: {path}")
        value = cache.digest(resolved) if cache else sha256_file(resolved)
        digests.append(
            FileDigest(
                role=role,
                path=_relative_to_root(resolved, root),
                sha256=value,
                bytes=resolved.stat().st_size,
            )
        )

    return sorted(digests, key=lambda d: (d.role, d.path))


def inputs_digest(digests: Iterable[FileDigest]) -> str:
    """Collapse a set of file digests into one identifier for "the inputs".

    Lets a record answer "did the data change?" with a single comparison
    instead of walking a list.

    Args:
        digests: The file digests to combine.

    Returns:
        Lowercase hex digest over the canonical JSON of the sorted entries.
    """
    canonical = canonical_json(
        [d.as_dict() for d in sorted(digests, key=lambda d: (d.role, d.path))]
    )
    return sha256_bytes(canonical.encode("utf-8"))


def canonical_json(value: Any) -> str:
    """Serialise ``value`` so that equal data always produces equal bytes.

    Sorted keys and no incidental whitespace, because these strings get hashed.

    Args:
        value: Any JSON-serialisable structure.

    Returns:
        The canonical JSON text.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _relative_to_root(path: Path, root: Path) -> str:
    """Express ``path`` relative to ``root`` as POSIX, falling back to absolute.

    Absolute paths are what put ``/Users/jenni/OneDrive/...`` into a script
    shared with colleagues, so relative is always preferred -- but a file
    genuinely outside the project must still be recordable, and recording it
    honestly beats silently mangling the path.
    """
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def hash_mapping(digests: Iterable[FileDigest]) -> Mapping[str, str]:
    """Return a path-to-digest mapping, for quick staleness comparison.

    Args:
        digests: The file digests to index.

    Returns:
        Mapping of relative path to hex digest.
    """
    return {d.path: d.sha256 for d in digests}
