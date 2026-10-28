"""Editing nrw.toml in place.

People write in nrw.toml, so what matters is what the editor must never do:
change a byte outside the lines it owns, write a file that no longer parses,
or edit a shape it does not understand instead of saying so.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import tomllib
from pathlib import Path

import pytest

from nr_workbench.project.tomlfile import (
    Set,
    TomlConflictError,
    TomlEditError,
    Unset,
    edit,
    placeholder_line,
    read_config,
    replace_block,
    toml_string,
    toml_value,
    verify,
    write_config,
)

DEFAULT = "/SNS/REF_L/{ipts}/shared/autoreduce/new_reduction"

BASE = """\
contract_version = 1

[beamtime]
label = ""
ipts = ""

# Which coding assistants this project is set up for.
[harness]
kinds = ["claude"]

[experiment.source]
# kind = "local"
# location = "/SNS/REF_L/{ipts}/shared/autoreduce/new_reduction"
# settle_seconds = 300
"""


def changed_lines(old: str, new: str) -> list[str]:
    return [
        line
        for line in difflib.ndiff(old.splitlines(), new.splitlines())
        if line.startswith(("- ", "+ "))
    ]


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "plain",
        'a "quoted" word',
        "C:\\x",
        "tab\there",
        "two\nlines",
        "carriage\rreturn",
        "nul\x00byte",
        "delete\x7f",
        "line\u2028separator",
        '"""',
        "{ipts}",
        "Pt/Cu 30 nm — ✓",
    ],
)
def test_toml_string_reads_back_as_exactly_what_was_written(value: str) -> None:
    assert tomllib.loads(f"x = {toml_string(value)}\n")["x"] == value


def test_toml_value_writes_a_whole_number_of_seconds_as_an_integer() -> None:
    assert toml_value(300.0) == "300"
    assert toml_value(12.5) == "12.5"
    assert toml_value(True) == "true"


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_toml_value_refuses_what_toml_cannot_hold(value: float) -> None:
    with pytest.raises(ValueError):
        toml_value(value)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def test_read_config_gives_the_bytes_and_their_revision(tmp_path: Path) -> None:
    path = tmp_path / "nrw.toml"
    path.write_text(BASE, encoding="utf-8")

    config = read_config(path)

    assert config.text == BASE
    assert config.revision == hashlib.sha256(BASE.encode()).hexdigest()


@pytest.mark.parametrize(
    "content,message",
    [
        (b"\xef\xbb\xbf" + BASE.encode(), "byte order mark"),
        (b'[beamtime]\nlabel = "\xff"\n', "not UTF-8"),
        (
            (BASE + "<<<<<<< HEAD\nx = 1\n=======\nx = 2\n>>>>>>> b\n").encode(),
            "conflict",
        ),
        (b"[beamtime\n", "not valid TOML"),
    ],
    ids=["bom", "not-utf8", "conflict-markers", "invalid"],
)
def test_read_config_refuses_a_file_it_must_not_edit(
    tmp_path: Path, content: bytes, message: str
) -> None:
    path = tmp_path / "nrw.toml"
    path.write_bytes(content)

    with pytest.raises(TomlEditError, match=message):
        read_config(path)


# --------------------------------------------------------------------------
# Editing nrw's own lines
# --------------------------------------------------------------------------


def test_edit_sets_a_value_where_it_stands() -> None:
    new = edit(BASE, {"beamtime": {"ipts": Set("IPTS-34347")}})

    assert changed_lines(BASE, new) == ['- ipts = ""', '+ ipts = "IPTS-34347"']


def test_edit_keeps_indentation_and_a_comment_after_the_value() -> None:
    text = BASE.replace('ipts = ""', '  ipts = ""   # the proposal')

    new = edit(text, {"beamtime": {"ipts": Set("IPTS-1")}})

    assert '  ipts = "IPTS-1"   # the proposal\n' in new


def test_edit_turns_nrws_placeholder_into_the_setting_and_back() -> None:
    location = {"experiment.source": {"location": Set("/data/x", default=DEFAULT)}}
    back = {"experiment.source": {"location": Unset(DEFAULT)}}

    set_once = edit(BASE, location)
    restored = edit(set_once, back)

    assert changed_lines(BASE, set_once) == [
        f"- {placeholder_line('location', DEFAULT)}",
        '+ location = "/data/x"',
    ]
    assert restored == BASE


def test_edit_leaves_a_persons_own_comment_about_the_key_alone() -> None:
    """Only nrw's exact placeholder is nrw's; `# location = "/old"` is a note."""
    text = BASE.replace(placeholder_line("location", DEFAULT), '# location = "/old"')

    new = edit(text, {"experiment.source": {"location": Set("/new", default=DEFAULT)}})

    assert '# location = "/old"\n' in new
    assert tomllib.loads(new)["experiment"]["source"]["location"] == "/new"


def test_edit_inserts_a_missing_key_after_the_tables_last_key_not_its_neighbours_comments() -> (
    None
):
    new = edit(BASE, {"beamtime": {"notes": Set("x")}})

    lines = new.splitlines()
    assert lines[lines.index('ipts = ""') + 1] == 'notes = "x"'


def test_edit_appends_a_table_the_file_does_not_have() -> None:
    new = edit(BASE, {"experiment.feed": {"poll_seconds": Set(15)}})

    assert new.startswith(BASE)
    assert new.endswith("\n[experiment.feed]\npoll_seconds = 15\n")


def test_edit_keeps_windows_line_endings() -> None:
    crlf = BASE.replace("\n", "\r\n")

    new = edit(
        crlf,
        {
            "beamtime": {"ipts": Set("IPTS-2"), "notes": Set("n")},
            "experiment.feed": {"poll_seconds": Set(15)},
        },
    )

    assert "\n" not in new.replace("\r\n", "")
    assert 'ipts = "IPTS-2"\r\nnotes = "n"\r\n' in new


def test_edit_copes_with_no_final_newline() -> None:
    text = BASE.rstrip("\n")

    new = edit(text, {"experiment.feed": {"poll_seconds": Set(15)}})

    assert tomllib.loads(new)["experiment"]["feed"] == {"poll_seconds": 15}


def test_edit_is_not_fooled_by_a_header_inside_a_multi_line_string() -> None:
    text = BASE.replace(
        'label = ""', 'label = ""\nnotes = """\n[experiment.source]\nlocation = 1\n"""'
    )

    new = edit(text, {"experiment.source": {"location": Set("/x", default=DEFAULT)}})

    document = tomllib.loads(new)
    assert document["experiment"]["source"]["location"] == "/x"
    assert "[experiment.source]\nlocation = 1\n" in document["beamtime"]["notes"]


# --------------------------------------------------------------------------
# What it refuses, and what it says instead
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '[experiment]\nsource.location = "/x"\n',
        '[experiment]\nsource = { location = "/x" }\n',
        '[experiment.source]\nlocation = """/x"""\n',
        "[experiment.source]\n'location' = \"/x\"\n",
    ],
    ids=["dotted-key", "inline-table", "multi-line-value", "quoted-key"],
)
def test_edit_refuses_a_shape_it_does_not_edit_and_gives_the_lines(text: str) -> None:
    with pytest.raises(TomlEditError) as refused:
        edit(text, {"experiment.source": {"location": Set("/y", default=DEFAULT)}})

    assert "by hand" in str(refused.value)
    assert 'location = "/y"' in refused.value.lines


def test_verify_catches_an_edit_that_changes_anything_else() -> None:
    with pytest.raises(TomlEditError, match="more of nrw.toml"):
        verify(BASE, BASE.replace('label = ""', 'label = "x"'), {})


def test_edit_never_returns_text_its_verification_rejects(monkeypatch) -> None:
    """The line editor could have a bug; the proof after it is what makes that safe."""
    from nr_workbench.project import tomlfile

    def careless(lines, region, table, keys, before, newline):
        lines[0] = "contract_version = 2\n"  # a line nobody asked to change

    monkeypatch.setattr(tomlfile, "_edit_region", careless)

    with pytest.raises(TomlEditError, match="more of nrw.toml"):
        edit(BASE, {"beamtime": {"ipts": Set("IPTS-1")}})


# --------------------------------------------------------------------------
# Against the file `nrw init` really writes
# --------------------------------------------------------------------------


def test_edit_changes_no_byte_outside_the_managed_lines(project: Path) -> None:
    text = (project / "nrw.toml").read_text(encoding="utf-8")
    text = text.replace("[harness]", "# a note someone added\n[harness]")

    new = edit(
        text,
        {
            "beamtime": {"ipts": Set("IPTS-34347"), "label": Set('my "april" run')},
        },
    )

    old_label = next(line for line in text.splitlines() if line.startswith("label = "))
    old_ipts = next(line for line in text.splitlines() if line.startswith("ipts = "))
    assert changed_lines(text, new) == [
        f"- {old_label}",
        f"- {old_ipts}",
        '+ label = "my \\"april\\" run"',
        '+ ipts = "IPTS-34347"',
    ]


# --------------------------------------------------------------------------
# Blocks nrw rendered itself
# --------------------------------------------------------------------------


def test_replace_block_rewrites_nrws_own_block_where_it_stands() -> None:
    old = "# [experiment.feed]\n# poll_seconds = 30"
    text = f"a = 1\n\n{old}\n\nb = 2\n"

    assert replace_block(text, old, "[experiment.feed]\npoll_seconds = 5") == (
        "a = 1\n\n[experiment.feed]\npoll_seconds = 5\n\nb = 2\n"
    )


@pytest.mark.parametrize(
    "text",
    ["a = 1\n", "x\n# [f]\nx\n# [f]\n", "  # [f]\n"],
    ids=["absent", "twice", "not-on-a-line-start"],
)
def test_replace_block_declines_unless_the_block_is_there_exactly_once(
    text: str,
) -> None:
    assert replace_block(text, "# [f]", "[f]") is None


def test_replace_block_keeps_windows_line_endings() -> None:
    text = "a = 1\r\n# [f]\r\n# x = 1\r\n"

    assert (
        replace_block(text, "# [f]\n# x = 1", "[f]\nx = 2")
        == "a = 1\r\n[f]\r\nx = 2\r\n"
    )


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def test_write_config_replaces_the_file_and_keeps_the_previous_one(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nrw.toml"
    path.write_text(BASE, encoding="utf-8")
    cache, backups = tmp_path / ".nrw" / "cache", tmp_path / ".nrw" / "backups"
    base = read_config(path)

    backup = write_config(path, base, BASE + "\n", cache_dir=cache, backups_dir=backups)

    assert path.read_text(encoding="utf-8") == BASE + "\n"
    assert backup.read_text(encoding="utf-8") == BASE
    # Nothing beside the tracked file: no lock file, no temporary file.
    assert sorted(p.name for p in tmp_path.iterdir()) == [".nrw", "nrw.toml"]


def test_write_config_refuses_a_file_changed_since_it_was_read(tmp_path: Path) -> None:
    path = tmp_path / "nrw.toml"
    path.write_text(BASE, encoding="utf-8")
    base = read_config(path)
    path.write_text(BASE + "# edited by hand\n", encoding="utf-8")

    with pytest.raises(TomlConflictError, match="changed since it was read"):
        write_config(
            path,
            base,
            "x = 1\n",
            cache_dir=tmp_path / "cache",
            backups_dir=tmp_path / "backups",
        )

    assert path.read_text(encoding="utf-8").endswith("# edited by hand\n")


def test_two_saves_in_one_second_keep_both_backups(tmp_path: Path, monkeypatch) -> None:
    from datetime import UTC, datetime

    from nr_workbench.project import tomlfile

    class OneSecond(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 25, 12, 0, 0, tzinfo=tz or UTC)

    monkeypatch.setattr(tomlfile, "datetime", OneSecond)
    path = tmp_path / "nrw.toml"
    path.write_text(BASE, encoding="utf-8")
    kwargs = {"cache_dir": tmp_path / "cache", "backups_dir": tmp_path / "backups"}

    first = write_config(path, read_config(path), BASE + "# 1\n", **kwargs)
    second = write_config(path, read_config(path), BASE + "# 2\n", **kwargs)

    stamp = "20260925T120000Z-"
    assert first.parent.name.startswith(stamp)
    assert second.parent.name.startswith(stamp)
    assert first != second
    assert first.read_text(encoding="utf-8") == BASE
    assert second.read_text(encoding="utf-8") == BASE + "# 1\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_write_config_keeps_the_files_permissions(tmp_path: Path) -> None:
    path = tmp_path / "nrw.toml"
    path.write_text(BASE, encoding="utf-8")
    path.chmod(0o664)

    write_config(
        path,
        read_config(path),
        BASE + "\n",
        cache_dir=tmp_path / "cache",
        backups_dir=tmp_path / "backups",
    )

    assert path.stat().st_mode & 0o777 == 0o664


def test_text_that_cannot_be_written_fails_before_any_backup(tmp_path: Path) -> None:
    path = tmp_path / "nrw.toml"
    path.write_text(BASE, encoding="utf-8")

    with pytest.raises(UnicodeEncodeError):
        write_config(
            path,
            read_config(path),
            'label = "\ud800"\n',
            cache_dir=tmp_path / ".nrw" / "cache",
            backups_dir=tmp_path / ".nrw" / "backups",
        )

    assert not (tmp_path / ".nrw" / "backups").exists()


@pytest.mark.parametrize(
    "old,new",
    [
        ("[a]\nx = 1\n", "[a]\nx = true\n"),
        ("[a]\nx = 0\n", "[a]\nx = false\n"),
        ("[a]\nx = 600\n", "[a]\nx = 600.0\n"),
    ],
    ids=["one-to-true", "zero-to-false", "int-to-float"],
)
def test_verify_sees_a_change_of_type(old: str, new: str) -> None:
    """In Python `True == 1` and `600 == 600.0`; in TOML they are different values."""
    with pytest.raises(TomlEditError):
        verify(old, new, {})


def test_verify_takes_a_whole_float_as_the_integer_it_is_written_as() -> None:
    verify("[a]\nx = 1\n", "[a]\nx = 300\n", {"a": {"x": Set(300.0)}})


def test_edit_switches_a_commented_table_on_instead_of_adding_a_second() -> None:
    text = BASE.replace("[experiment.source]", "# [experiment.source]")

    new = edit(text, {"experiment.source": {"location": Set("/x", default=DEFAULT)}})

    assert new.count("[experiment.source]") == 1
    assert "\n[experiment.source]\n# kind" in new
    assert tomllib.loads(new)["experiment"]["source"] == {"location": "/x"}
