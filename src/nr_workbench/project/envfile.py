"""The project's ``.env``: the lines nrw's pages write, and nothing else.

``.env`` is the person's file. It may hold keys, comments and settings for
other tools, so nrw changes only the lines that set the variables it was asked
to, and leaves every other byte as it was. It is edited through python-dotenv's
own parser -- the reader every nrw command uses -- so what nrw changes is what
a reader sees change, a quoted value spanning lines included.

The file is replaced whole (:func:`nr_workbench.fsutil.atomic_write_bytes`),
keeping its mode; a new one is readable by its owner only, because it is where
keys go.

A change is made against the revision of the file the page was shown, and
checked again just before the file is replaced, so an edit made by hand in
between is refused rather than written over.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

from nr_workbench.fsutil import atomic_write_bytes

#: The file, at the project's root: the first one :mod:`nr_workbench.env` reads.
ENV_FILE = ".env"

#: The revision of a project with no ``.env``.
ABSENT = "absent"

#: The longest value nrw writes.
MAX_VALUE = 128

#: A value nrw writes: one plain word, or nothing. No quotes, spaces, ``#``,
#: ``$``, ``=`` or backslash, which a dotenv reader takes as quoting, a comment,
#: a variable or an escape; and not a leading ``-``, which ``claude --model``
#: would read as an option.
_VALUE_RE = re.compile(rf"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{{0,{MAX_VALUE - 1}}}")

#: The mode of a new file: the owner's alone.
NEW_FILE_MODE = 0o600


class EnvFileError(Exception):
    """The project's ``.env`` cannot be read or changed safely; the message says why."""


class EnvFileConflict(EnvFileError):
    """``.env`` changed since it was read."""


class EnvValueError(ValueError):
    """A value nrw will not write: a dotenv reader would misread it."""


@dataclass(frozen=True)
class EnvFile:
    """The project's ``.env`` as it is now.

    Attributes:
        path: Where it is, or would be.
        text: What it holds; ``None`` when there is no file.
        revision: What a change must be made against.
    """

    path: Path
    text: str | None
    revision: str


def read_env_file(root: Path) -> EnvFile:
    """Read the project's ``.env``.

    Args:
        root: The project.

    Returns:
        Its text and revision.

    Raises:
        EnvFileError: It is not UTF-8 text, or cannot be read.
    """
    path = Path(root) / ENV_FILE
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return EnvFile(path=path, text=None, revision=ABSENT)
    except OSError as exc:
        raise EnvFileError(f"{ENV_FILE} cannot be read: {exc.strerror}.") from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EnvFileError(f"{ENV_FILE} is not UTF-8 text.") from exc
    return EnvFile(path=path, text=text, revision=hashlib.sha256(data).hexdigest())


def check_value(name: str, value: str) -> str:
    """A value nrw may write for *name*: one plain word, or empty.

    Raises:
        EnvValueError: It holds what a dotenv reader would misread, or is too
            long.
    """
    if value and not _VALUE_RE.fullmatch(value):
        raise EnvValueError(
            f"{name} must be one word of letters, digits and . _ - : / @ [ ], "
            f"{MAX_VALUE} characters at most, not {value!r}."
        )
    return value


def set_values(
    root: Path, values: dict[str, str | None], *, base_revision: str
) -> list[str]:
    """Set, or remove, variables in the project's ``.env``.

    A variable already set has its first line replaced where it is, and any
    later line setting it -- even a bare ``NAME``, which a reader takes as
    unsetting it -- removed, so the file says it once. One not set yet is added
    at the end. Every other byte is kept, and so a change undone restores the
    file as it was.

    Args:
        root: The project.
        values: Each variable's new value, or ``None`` to remove it.
        base_revision: The revision the change was decided against.

    Returns:
        What changed, one line each, for the person to read; empty when nothing
        needed changing, and then nothing is written.

    Raises:
        EnvValueError: A value is not one nrw writes.
        EnvFileConflict: The file changed since *base_revision*.
        EnvFileError: The file cannot be read or changed safely.
        OSError: It could not be written: a read-only project, a full disk.
    """
    for name, value in values.items():
        if value is not None:
            check_value(name, value)
    current = read_env_file(root)
    if current.revision != base_revision:
        raise EnvFileConflict(_CHANGED)
    text, changes = edited(current.text or "", values)
    if not changes:
        return []
    # Once more, as late as can be: an edit made while this ran is refused.
    if read_env_file(root).revision != current.revision:
        raise EnvFileConflict(_CHANGED)
    atomic_write_bytes(
        current.path,
        text.encode("utf-8"),
        mode=NEW_FILE_MODE if current.text is None else None,
    )
    return changes


_CHANGED = (
    f"{ENV_FILE} has changed since this page read it. Reload the page to see what "
    "it holds now."
)


def edited(text: str, values: dict[str, str | None]) -> tuple[str, list[str]]:
    """*text* with *values* set or removed, and what changed.

    Args:
        text: A ``.env``'s contents.
        values: Each variable's new value, or ``None`` to remove it.

    Returns:
        The new contents, and one line per change.
    """
    from dotenv.parser import parse_stream

    from nr_workbench.env import SECRET_VARS

    def shown(line: str) -> str:
        name, _, value = " ".join(line.split()).partition("=")
        return f"{name}=…" if name in SECRET_VARS and value else f"{name}={value}"

    newline = "\r\n" if "\r\n" in text else "\n"
    pieces: list[str] = []
    changes: list[str] = []
    done: set[str] = set()
    for binding in parse_stream(io.StringIO(text)):
        original = binding.original.string
        if binding.key not in values:
            pieces.append(original)
            continue
        name, wanted = binding.key, values[binding.key]
        # What the parser gives a binding starts with the blank lines before
        # it, which are not its to keep or drop.
        body = original.lstrip()
        lead = original[: len(original) - len(body)]
        ending = next((e for e in ("\r\n", "\n") if body.endswith(e)), "")
        line = body[: len(body) - len(ending)]
        if name in done or wanted is None:
            pieces.append(lead)
            changes.append(f"removed {shown(line)}")
        else:
            new = f"{name}={wanted}"
            if line != new:
                changes.append(f"{shown(line)} is now {new}")
            pieces.append(lead + new + ending)
        done.add(name)
    out = "".join(pieces)
    added = [
        f"{name}={value}"
        for name, value in values.items()
        if value is not None and name not in done
    ]
    if added:
        if out and not out.endswith("\n"):
            out += newline
        out += "".join(line + newline for line in added)
        changes += [f"added {line}" for line in added]
    return out, changes
