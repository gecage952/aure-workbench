"""Editing ``nrw.toml`` in place, without losing a byte a person wrote.

``nrw.toml`` is mostly comments explaining why its conventions are what they
are, and people edit it by hand. The standard library only reads TOML, and a
round-trip through any writer drops the comments. So nrw edits the file the way
a careful person would. It changes only lines whose shape it recognises -- a
single-line ``key = value`` inside a table header it recognises, or the
commented placeholder ``# key = <default>`` it wrote itself -- and then it
*proves* the edit: both versions are parsed, and the new document must be the
old one with exactly the intended keys changed. Anything else is refused,
along with the lines a person can add by hand.

The functions here only transform text; :func:`write_config` is the one that
touches the disk.
"""

from __future__ import annotations

import copy
import hashlib
import re
import secrets
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: A value nrw writes: a string, a whole or decimal number, or a flag.
Value = str | int | float | bool


class TomlEditError(Exception):
    """The file cannot be edited safely. The message says what to do instead.

    Attributes:
        lines: TOML a person can add by hand to make the same change, when the
            refusal is about the file's shape rather than its state.
    """

    def __init__(self, message: str, lines: str = "") -> None:
        super().__init__(message)
        self.lines = lines

    def explained(self) -> str:
        """The message, followed by the lines to add by hand when there are any."""
        return f"{self}\n{self.lines}" if self.lines else str(self)


class TomlConflictError(TomlEditError):
    """The file changed after it was read; nothing was written."""


@dataclass(frozen=True)
class Set:
    """Give a key this value.

    Attributes:
        value: The new value.
        default: nrw's default for the key, when it writes one as a commented
            placeholder; that placeholder line is then replaced rather than
            left beside the new line.
    """

    value: Value
    default: Value | None = None


@dataclass(frozen=True)
class Unset:
    """Remove a key, so nrw's default applies again.

    Attributes:
        default: Written back as the placeholder ``# key = <default>``.
    """

    default: Value


#: Changes by table name (``"experiment.source"``), then by key.
Changes = Mapping[str, Mapping[str, Set | Unset]]


@dataclass(frozen=True)
class ConfigText:
    """A configuration file as read, checked to be safe to edit.

    Attributes:
        raw: Its exact bytes.
        text: The same, decoded.
        revision: sha256 of ``raw``: what a page sends back to say "the file I
            showed you", so an edit made in between is not written over.
    """

    raw: bytes
    text: str
    revision: str


# ---------------------------------------------------------------------------
# Encoding values
# ---------------------------------------------------------------------------

_ESCAPES = {
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
    '"': '\\"',
    "\\": "\\\\",
}


def toml_string(value: str) -> str:
    """A TOML basic string that reads back as exactly ``value``.

    Escapes the quote, the backslash and every control character, which is
    what TOML requires; a folder name containing ``"`` or ``\\`` otherwise
    makes the whole file unreadable.
    """
    out = []
    for char in value:
        if char in _ESCAPES:
            out.append(_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def toml_value(value: Value) -> str:
    """``value`` as TOML: a basic string, an integer, a decimal, or a flag.

    A whole float is written as an integer (``300``, not ``300.0``), the way a
    person writes a number of seconds.

    Raises:
        ValueError: For a float that is not finite, or an unsupported type.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{value!r} is not a number TOML can hold")
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, str):
        return toml_string(value)
    raise ValueError(f"cannot write {type(value).__name__} values")


def key_line(key: str, value: Value) -> str:
    """The line nrw writes for a key that is set, without its line ending."""
    return f"{key} = {toml_value(value)}"


def placeholder_line(key: str, default: Value) -> str:
    """The commented line nrw writes for a key left at its default."""
    return f"# {key} = {toml_value(default)}"


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def read_config(path: Path) -> ConfigText:
    """Read a configuration file, refusing any the editor must not touch.

    Raises:
        TomlEditError: It is not UTF-8, starts with a byte order mark, holds
            git conflict markers, or is not valid TOML.
        OSError: It cannot be read.
    """
    path = Path(path)
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise TomlEditError(
            f"{path.name} starts with a byte order mark, which TOML does not "
            "allow; save it as plain UTF-8."
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TomlEditError(f"{path.name} is not UTF-8 ({exc}).") from exc
    if re.search(r"^(<{7}|>{7})", text, re.MULTILINE):
        raise TomlEditError(
            f"{path.name} contains git conflict markers. Resolve the merge and "
            "commit before nrw edits it."
        )
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise TomlEditError(f"{path.name} is not valid TOML: {exc}") from exc
    return ConfigText(raw=raw, text=text, revision=hashlib.sha256(raw).hexdigest())


# ---------------------------------------------------------------------------
# Finding nrw's own lines
# ---------------------------------------------------------------------------

_HEADER = re.compile(
    r"^[ \t]*\[(?!\[)[ \t]*(?P<name>[A-Za-z0-9_-]+(?:[ \t]*\.[ \t]*[A-Za-z0-9_-]+)*)"
    r"[ \t]*\][ \t]*(?:#.*)?$"
)
#: A table header nrw wrote commented out: ``# [experiment.source]``.
_COMMENTED_HEADER = re.compile(
    r"^[ \t]*#[ \t]*\[(?!\[)[ \t]*(?P<name>[A-Za-z0-9_-]+(?:[ \t]*\.[ \t]*[A-Za-z0-9_-]+)*)"
    r"[ \t]*\][ \t]*$"
)
_VALUE = (
    r'"(?:[^"\\\r\n]|\\.)*"'  # basic string, one line
    r"|'[^'\r\n]*'"  # literal string, one line
    r"|[+-]?[0-9][0-9_]*(?:\.[0-9_]+)?(?:[eE][+-]?[0-9]+)?"  # number
    r"|true|false"
)
_KEY = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>[A-Za-z0-9_-]+)[ \t]*=[ \t]*"
    rf"(?P<value>{_VALUE})(?P<rest>[ \t]*(?:#.*)?)$"
)
_ANY_KEY = re.compile(r"^[ \t]*(?P<key>[A-Za-z0-9_\"'.-][^=]*?)[ \t]*=")


def _starts_inside_string(lines: list[str]) -> list[bool]:
    """For each line, whether it begins inside a multi-line string.

    Such a line is text, whatever it looks like: a ``[experiment.source]``
    written inside a ``\"\"\"`` string is not a table. A mistake here could only
    ever make an edit fail its verification, never pass it.
    """
    inside: str | None = None
    starts: list[bool] = []
    for line in lines:
        starts.append(inside is not None)
        i, n = 0, len(line)
        while i < n:
            if inside is not None:
                if inside == '"""' and line[i] == "\\":
                    i += 2
                elif line.startswith(inside, i):
                    i += 3
                    inside = None
                else:
                    i += 1
                continue
            char = line[i]
            if char == "#":
                break
            if line.startswith('"""', i) or line.startswith("'''", i):
                inside = line[i : i + 3]
                i += 3
            elif char == '"':
                i += 1
                while i < n and line[i] != '"':
                    i += 2 if line[i] == "\\" else 1
                i += 1
            elif char == "'":
                end = line.find("'", i + 1)
                i = n if end < 0 else end + 1
            else:
                i += 1
    return starts


@dataclass(frozen=True)
class _Region:
    """One table's lines: its header, and everything up to the next header."""

    header: int
    end: int


def _regions(lines: list[str]) -> tuple[dict[str, _Region], dict[str, _Region]]:
    """Each table's region: those written out, and those nrw wrote commented out.

    A region runs to the next header of either kind, so a commented-out table
    never claims the lines of the one after it.
    """
    inside = _starts_inside_string(lines)
    headers: list[tuple[int, str | None, bool]] = []
    for index, line in enumerate(lines):
        if inside[index]:
            continue
        body = line.rstrip("\r\n")
        if line.lstrip().startswith("["):
            match = _HEADER.match(body)
            headers.append((index, _name(match), True))
        elif (match := _COMMENTED_HEADER.match(body)) is not None:
            headers.append((index, _name(match), False))
    regions: dict[str, _Region] = {}
    commented: dict[str, _Region] = {}
    for position, (index, name, active) in enumerate(headers):
        end = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
        if name is None:
            continue
        found = regions if active else commented
        if name in found:
            if active:
                raise TomlEditError(f"[{name}] is declared twice; TOML allows it once.")
            continue  # a second commented copy is a person's note; leave it
        found[name] = _Region(index, end)
    return regions, commented


@dataclass(frozen=True)
class CommentedSetting:
    """A setting a person typed into a comment, where nothing reads it.

    Attributes:
        line: Its line number, counting from 1.
        table: The table it is under, e.g. ``experiment.source``.
        key: The setting.
        text: The line as written.
        table_commented: Whether the table's own header is commented out too.
    """

    line: int
    table: str
    key: str
    text: str
    table_commented: bool


def settings_in_comments(
    text: str,
    managed: Mapping[str, Mapping[str, Value]],
    document: Mapping[str, Any],
) -> list[CommentedSetting]:
    """Settings typed into a comment, where nrw cannot read them.

    Every placeholder nrw writes shows the setting's default -- ``# location =
    "<default>"``. A commented line in one of nrw's tables holding any other
    value was typed by a person, and nearly always meant to set it: editing the
    value "shown beside it" in the comment, and missing that a comment is not
    read, leaves nrw on its default with nothing said. A setting the file does
    set is not reported: the comment is then only a note beside it.

    Args:
        text: The file.
        managed: Each table nrw writes, and its settings' defaults.
        document: The file, parsed: what is actually set.
    """
    lines = text.splitlines()
    try:
        regions, commented = _regions(lines)
    except TomlEditError:
        return []  # a table declared twice: load_config says so already
    found: list[CommentedSetting] = []
    for table, defaults in managed.items():
        active = _lookup(document, table)
        for region, table_commented in (
            (commented.get(table), True),
            (regions.get(table), False),
        ):
            if region is None:
                continue
            for index in range(region.header + 1, region.end):
                body = lines[index].strip()
                if not body.startswith("#"):
                    continue
                match = _KEY.match(body.lstrip("#").strip())
                if match is None or match["key"] not in defaults:
                    continue
                key = match["key"]
                if isinstance(active, Mapping) and key in active:
                    continue
                try:
                    value = tomllib.loads(f"v = {match['value']}")["v"]
                except tomllib.TOMLDecodeError:
                    continue
                if not _same(value, defaults[key]):
                    found.append(
                        CommentedSetting(index + 1, table, key, body, table_commented)
                    )
    return sorted(found, key=lambda setting: setting.line)


def _name(match: re.Match[str] | None) -> str | None:
    return re.sub(r"[ \t]*\.[ \t]*", ".", match.group("name")) if match else None


def _lookup(document: Mapping[str, Any], table: str) -> Any:
    node: Any = document
    for part in table.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


def edit(text: str, changes: Changes) -> str:
    """Apply ``changes`` to the text of a TOML file, touching only nrw's lines.

    For each key, in its table's region:

    * a ``key = value`` line gets the new value, keeping its indentation and
      any comment after it;
    * nrw's own placeholder ``# key = <default>`` becomes the set line (or
      stays, for a key being unset);
    * otherwise a set key is inserted after the last key line of the table.

    A table nrw wrote commented out (``# [experiment.source]``) is switched on
    where it stands -- appending a second one below it would leave the file
    advising a person to uncomment the first, which TOML then refuses. A table
    the file does not have at all is appended at the end.

    Raises:
        TomlEditError: The file writes a managed key or table in a shape this
            editor does not recognise -- a dotted key, an inline table, a
            multi-line string -- or the edit would change anything else.
    """
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    before = tomllib.loads(text)

    appended: list[str] = []
    # Bottom to top, so that an insertion never moves a region still to come.
    regions, commented = _regions(lines)

    def position(table: str) -> int:
        region = regions.get(table) or commented.get(table)
        return -region.header if region else 0

    for table in sorted(changes, key=position):
        keys = changes[table]
        region = regions.get(table)
        if (
            region is None
            and table in commented
            and _lookup(before, table) is None
            and any(isinstance(c, Set) for c in keys.values())
        ):
            region = commented[table]
            line = lines[region.header]
            ending = line[len(line.rstrip("\r\n")) :]
            indent = line[: len(line) - len(line.lstrip())]
            lines[region.header] = f"{indent}[{table}]{ending}"
        if region is None:
            if _lookup(before, table) is not None:
                raise TomlEditError(
                    f"nrw.toml defines [{table}] in a way nrw does not edit "
                    "(dotted keys or an inline table). Make the change by hand:",
                    _hand_lines(table, keys),
                )
            block = [
                key_line(k, c.value) for k, c in keys.items() if isinstance(c, Set)
            ]
            if block:
                appended.append("\n".join([f"[{table}]", *block]))
            continue
        _edit_region(lines, region, table, keys, before, newline)

    if appended:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += newline
        if lines and lines[-1].strip():
            lines.append(newline)
        lines.append(newline.join("\n\n".join(appended).split("\n")) + newline)

    result = "".join(lines)
    verify(text, result, changes)
    return result


def _edit_region(
    lines: list[str],
    region: _Region,
    table: str,
    keys: Mapping[str, Set | Unset],
    before: Mapping[str, Any],
    newline: str,
) -> None:
    inside = _starts_inside_string(lines)
    existing = _lookup(before, table) or {}
    for key, change in keys.items():
        active: list[int] = []
        placeholder: int | None = None
        default = change.default
        for index in range(region.header + 1, region.end):
            if inside[index]:
                continue
            body = lines[index].rstrip("\r\n")
            match = _KEY.match(body)
            if match and match.group("key") == key:
                active.append(index)
            elif (
                default is not None
                and body.strip() == placeholder_line(key, default)
                and placeholder is None
            ):
                placeholder = index
        if len(active) > 1:
            raise TomlEditError(f"[{table}] sets {key} more than once.")
        if key in existing and not active:
            raise TomlEditError(
                f"nrw.toml sets [{table}] {key} in a way nrw does not edit (a "
                "multi-line value, a dotted key, or a quoted key). Make the "
                "change by hand:",
                _hand_lines(table, {key: change}),
            )

        if active:
            index = active[0]
            ending = lines[index][len(lines[index].rstrip("\r\n")) :]
            match = _KEY.match(lines[index].rstrip("\r\n"))
            assert match is not None
            if isinstance(change, Set):
                lines[index] = (
                    f"{match.group('indent')}{key} = {toml_value(change.value)}"
                    f"{match.group('rest')}{ending}"
                )
            else:
                lines[index] = (
                    f"{match.group('indent')}{placeholder_line(key, change.default)}"
                    f"{match.group('rest')}{ending}"
                )
        elif isinstance(change, Set):
            if placeholder is not None:
                ending = lines[placeholder][len(lines[placeholder].rstrip("\r\n")) :]
                indent = lines[placeholder][
                    : len(lines[placeholder]) - len(lines[placeholder].lstrip())
                ]
                lines[placeholder] = f"{indent}{key_line(key, change.value)}{ending}"
            else:
                at = _insertion_point(lines, region, inside)
                if not lines[at - 1].endswith(("\n", "\r")):
                    lines[at - 1] += newline
                lines.insert(at, key_line(key, change.value) + newline)
                region = _Region(region.header, region.end + 1)
                inside = _starts_inside_string(lines)
        # An Unset key with no line is already unset.


def _insertion_point(lines: list[str], region: _Region, inside: list[bool]) -> int:
    """After the table's last key line or placeholder, else right after its header.

    Not at the end of the region: the comment block introducing the *next*
    table sits there, and a key inserted after it would read as that table's.
    """
    at = region.header + 1
    for index in range(region.header + 1, region.end):
        if inside[index]:
            continue
        body = lines[index].strip()
        if _ANY_KEY.match(body) or re.match(r"^#\s*[A-Za-z0-9_-]+\s*=", body):
            at = index + 1
    return at


def _hand_lines(table: str, keys: Mapping[str, Set | Unset]) -> str:
    rows = [f"[{table}]"]
    for key, change in keys.items():
        if isinstance(change, Set):
            rows.append(key_line(key, change.value))
        else:
            rows.append(f"# remove {key} to use nrw's default")
    return "\n".join(rows)


def replace_block(text: str, old: str, new: str) -> str | None:
    """Replace ``old`` with ``new`` where it stands on whole lines, exactly once.

    For a block nrw rendered itself, such as the experiment tables: while it is
    still exactly nrw's text it can be rewritten as a unit.

    Returns:
        The new text, or ``None`` when ``old`` is not there exactly once.
    """
    if not old:
        return None
    for ending in ("\n", "\r\n"):
        candidate = old.replace("\n", ending) if ending != "\n" else old
        pattern = re.compile(r"(?:^|(?<=\n))" + re.escape(candidate) + r"(?=\r?\n|$)")
        found = list(pattern.finditer(text))
        if len(found) == 1:
            replacement = new.replace("\n", ending) if ending != "\n" else new
            start, end = found[0].span()
            return text[:start] + replacement + text[end:]
        if found:
            return None
    return None


# ---------------------------------------------------------------------------
# Proving an edit
# ---------------------------------------------------------------------------


def verify(old: str, new: str, changes: Changes) -> None:
    """Refuse unless ``new`` parses as ``old`` with exactly ``changes`` made.

    Raises:
        TomlEditError: ``new`` does not parse, or differs in anything else.
    """
    try:
        after = tomllib.loads(new)
    except tomllib.TOMLDecodeError as exc:
        raise TomlEditError(f"the edit would make nrw.toml invalid ({exc}).") from exc
    want = applied(tomllib.loads(old), changes)
    if not _same(_pruned(after), _pruned(want)):
        raise TomlEditError(
            "the edit would change more of nrw.toml than the settings asked "
            "for, so nothing was written. Make the change by hand."
        )


def applied(document: Mapping[str, Any], changes: Changes) -> dict[str, Any]:
    """A parsed document with ``changes`` made, as it would read back once written."""
    want = copy.deepcopy(dict(document))
    for table, keys in changes.items():
        node = want
        for part in table.split("."):
            node = node.setdefault(part, {})
        for key, change in keys.items():
            if isinstance(change, Set):
                node[key] = _as_written(change.value)
            else:
                node.pop(key, None)
    return want


def _as_written(value: Value) -> Value:
    """A value as it reads back once written: a whole float is written whole."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _same(a: Any, b: Any) -> bool:
    """Equal, and of the same type: in Python ``True == 1`` and ``600 == 600.0``."""
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if type(a) is not type(b):
        return False
    return a == b or (a != a and b != b)  # NaN is NaN


def _pruned(document: Any) -> Any:
    """The document without empty tables: an empty table and none mean the same."""
    if isinstance(document, dict):
        kept = {k: _pruned(v) for k, v in document.items()}
        return {k: v for k, v in kept.items() if v != {}}
    return document


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def write_config(
    path: Path,
    base: ConfigText,
    new_text: str,
    *,
    cache_dir: Path,
    backups_dir: Path,
) -> Path:
    """Replace a configuration file with ``new_text``, if it is still ``base``.

    The previous file is kept under ``backups_dir``, and the temporary file
    lives in ``cache_dir``: beside a tracked file, either would show up as
    something to commit. The caller holds the scaffold's writer lock
    (:func:`nr_workbench.project.nrwtoml.write_as_nrw` does), which is what
    keeps the comparison and the replacement together.

    Returns:
        Where the previous version was kept.

    Raises:
        TomlConflictError: The file is no longer what was read.
        OSError: It could not be written.
    """
    from nr_workbench.fsutil import atomic_write_bytes

    path = Path(path)
    cache_dir, backups_dir = Path(cache_dir), Path(backups_dir)
    # Encoded before anything is written: text that cannot be, fails here,
    # not after the backup has been made.
    data = new_text.encode("utf-8")
    if path.read_bytes() != base.raw:
        raise TomlConflictError(
            f"{path.name} changed since it was read -- edited by hand, or saved "
            "from somewhere else. Nothing was written; look again."
        )
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    # The bytes that were read and compared, not the file read again.
    folder = backups_dir / f"{stamp}-{secrets.token_hex(8)}"
    folder.mkdir(parents=True)
    backup = folder / path.name
    backup.write_bytes(base.raw)
    atomic_write_bytes(path, data, scratch=cache_dir)
    return backup
