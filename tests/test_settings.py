"""Settings in nrw.toml: what nrw writes, and that `nrw init` agrees with it.

`nrw init` re-renders nrw.toml on every run and leaves a `.nrw-new` beside a
file it did not write. So everything nrw itself puts into nrw.toml -- the IPTS,
the audience, the experiment's settings -- has to be something `init` renders
back byte for byte, or every later `init` reads the project as hand-edited.
The `[fit]` and `[agent]` tables are a person's to write, but a value a tool
sets in them through nrw's editor is nrw's text too, and `init` keeps it.
"""

from __future__ import annotations

import dataclasses
import difflib
import os
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from nr_workbench.cli import main
from nr_workbench.fitting.settings import written_settings
from nr_workbench.project.config import ProjectConfigError, load_config
from nr_workbench.project.nrwtoml import write_as_nrw
from nr_workbench.project.render import init_context, render_tree
from nr_workbench.project.settings import (
    EXPERIMENT_KEYS,
    experiment_block,
    written_experiment,
)
from nr_workbench.project.tomlfile import Set


def nrw(root: Path, monkeypatch, *args: str):
    monkeypatch.chdir(root)
    return CliRunner().invoke(main, list(args))


def changed_lines(before: str, after: str) -> list[tuple[list[str], list[str]]]:
    """Each change, in order: the lines taken out and the lines put in their place.

    Everything between two changes is equal, in order -- which a comparison of
    line sets cannot say: it misses a line moved, or one written twice.
    """
    old, new = before.splitlines(), after.splitlines()
    matcher = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    return [
        (old[i1:i2], new[j1:j2])
        for op, i1, i2, j1, j2 in matcher.get_opcodes()
        if op != "equal"
    ]


def rendered_nrw_toml(root: Path, **overrides) -> str:
    context = dataclasses.replace(init_context(root), **overrides)
    planned = {p.relpath: p for p in render_tree("project", context)}
    return planned["nrw.toml"].content.decode("utf-8")


# --------------------------------------------------------------------------
# Values that would break the file
# --------------------------------------------------------------------------


def test_a_quote_or_backslash_in_a_name_or_label_still_gives_valid_toml(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'run "7" \\ april'
    root.mkdir()

    result = CliRunner().invoke(
        main, ["init", str(root), "--beamtime", 'the "april" beamtime \\ 2026']
    )

    assert result.exit_code == 0, result.output
    config = load_config(root)
    assert config.name == 'run "7" \\ april'
    assert config.beamtime == 'the "april" beamtime \\ 2026'


def test_a_table_declared_twice_names_the_fix(project: Path) -> None:
    toml = project / "nrw.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8") + "\n[harness]\nkinds = []\n", encoding="utf-8"
    )

    with pytest.raises(ProjectConfigError, match="keep one"):
        load_config(project)


# --------------------------------------------------------------------------
# `nrw init` renders back what nrw wrote
# --------------------------------------------------------------------------


def test_the_experiment_block_is_commented_out_until_something_is_set() -> None:
    block = experiment_block({})

    assert tomllib.loads(block) == {}
    assert "# [experiment.source]" in block and "# [experiment.feed]" in block


def test_a_set_key_writes_its_table_out_and_keeps_the_rest_as_placeholders() -> None:
    block = experiment_block({"experiment.source": {"location": "/data/x"}})

    assert tomllib.loads(block) == {"experiment": {"source": {"location": "/data/x"}}}
    assert "# settle_seconds = 300" in block
    assert "# [experiment.feed]" in block


def test_every_managed_key_reads_back_as_written() -> None:
    chosen = {
        "experiment.source": {"kind": "local", "location": "/d", "settle_seconds": 60},
        "experiment.feed": {"kind": "directory", "poll_seconds": 15},
    }

    document = tomllib.loads(experiment_block(chosen))

    assert written_experiment(document) == chosen
    assert set(chosen) == set(EXPERIMENT_KEYS)


def test_init_twice_with_experiment_settings_reports_no_changes(
    project: Path, monkeypatch
) -> None:
    toml = project / "nrw.toml"
    toml.write_text(
        rendered_nrw_toml(
            project,
            experiment={
                "experiment.source": {"location": "/data/new_reduction"},
                "experiment.feed": {"poll_seconds": 15},
            },
        ),
        encoding="utf-8",
    )

    result = nrw(project, monkeypatch, "init", "--check")

    assert result.exit_code == 0, result.output
    assert load_config(project).raw["experiment"]["feed"] == {"poll_seconds": 15}


def test_a_fit_or_agent_setting_takes_the_line_of_its_example(project: Path) -> None:
    text = rendered_nrw_toml(
        project, settings={"agent.limits.dream": {"samples": 50000}}
    )

    block = text[text.index("[agent.limits.dream]") :].split("\n\n")[0]
    assert block.splitlines() == [
        "[agent.limits.dream]",
        "samples = 50000",
        "# burn = 1000",
        "# steps = 300",
        "# pop = 10",
    ]
    assert "# [fit.dream]" in text  # the other examples stay examples


def test_fit_and_agent_settings_read_back_as_written(project: Path) -> None:
    settings = {
        "fit": {"method": "de", "seed": 7},
        "fit.de": {"steps": 3000},
        "agent.limits.dream": {"samples": 50000, "pop": 8},
    }

    text = rendered_nrw_toml(project, settings=settings)

    assert written_settings(tomllib.loads(text)) == settings


def test_init_keeps_fit_and_agent_settings_set_through_nrws_editor(
    project: Path, monkeypatch
) -> None:
    """Re-rendered without them, an unattended run's limits would quietly lapse."""
    limits = {"samples": 100000, "burn": 1000, "steps": 300, "pop": 10}
    write_as_nrw(
        project,
        {
            "fit": {"method": Set("de")},
            "fit.de": {"steps": Set(3000)},
            "agent.limits.dream": {k: Set(v, default=v) for k, v in limits.items()},
        },
    )

    # A new harness list makes `init` render the file again.
    result = nrw(
        project, monkeypatch, "init", "--harness", "claude", "--harness", "opencode"
    )

    assert result.exit_code == 0, result.output
    raw = load_config(project).raw
    assert raw["fit"] == {"method": "de", "de": {"steps": 3000}}
    assert raw["agent"]["limits"]["dream"] == limits
    check = nrw(project, monkeypatch, "init", "--check")
    assert check.exit_code == 0, check.output


def test_nrw_audience_then_init_check_is_clean(project: Path, monkeypatch) -> None:
    """It used to leave nrw.toml looking hand-edited to every later `init`."""
    changed = nrw(project, monkeypatch, "audience", "--set", "statistics=expert")
    assert changed.exit_code == 0, changed.output

    result = nrw(project, monkeypatch, "init", "--check")

    assert result.exit_code == 0, result.output
    assert load_config(project).raw["audience"]["statistics"] == "expert"


def test_an_unedited_older_nrw_toml_upgrades_to_the_new_render(
    project: Path, monkeypatch
) -> None:
    """A file from before the experiment block, untouched since init wrote it."""
    from nr_workbench.project.scaffold import load_lock, sha256_bytes, write_lock

    toml = project / "nrw.toml"
    current = toml.read_text(encoding="utf-8")
    older = current[: current.index("# The experiment:")].rstrip("\n") + "\n"
    toml.write_text(older, encoding="utf-8")
    lock_path = project / ".nrw" / "scaffold.lock.json"
    lock = load_lock(lock_path)
    lock["nrw.toml"]["sha256_at_install"] = sha256_bytes(older.encode())
    write_lock(lock_path, lock)

    result = nrw(project, monkeypatch, "init")

    assert result.exit_code == 0, result.output
    assert toml.read_text(encoding="utf-8") == current
    assert not (project / "nrw.toml.nrw-new").exists()


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------


def init_check(project: Path):
    """``nrw init --check``: exit code 0 when it would change nothing."""
    return CliRunner().invoke(main, ["init", str(project), "--check"])


def test_after_each_save_of_a_mixed_sequence_init_check_reports_nothing_to_do(
    project: Path,
) -> None:
    from nr_workbench.project.settings import save

    steps = [
        {"ipts": "34347"},
        {"source.location": "/data/{ipts}/new_reduction"},
        {"feed.poll_seconds": 15, "source.settle_seconds": 120},
        {"label": "april 2026"},
        {"source.location": None, "feed.poll_seconds": None},
    ]
    for step in steps:
        result = save(project, step)

        assert result.changed, step
        checked = init_check(project)
        assert checked.exit_code == 0, (step, checked.output)
    config = load_config(project)
    assert config.ipts == "IPTS-34347"
    assert config.raw["experiment"]["source"] == {"settle_seconds": 120}


def test_a_save_changes_no_byte_outside_the_managed_lines(project: Path) -> None:
    """A person's edits anywhere else survive, byte for byte."""
    from nr_workbench.project.settings import save

    toml = project / "nrw.toml"
    edited = toml.read_text(encoding="utf-8").replace(
        "[conventions]", "# our own note\n[conventions]"
    )
    toml.write_text(edited, encoding="utf-8")

    save(project, {"ipts": "IPTS-7", "source.location": "/data/x"})

    after = toml.read_text(encoding="utf-8")
    assert changed_lines(edited, after) == [
        (['ipts = "IPTS-00001"'], ['ipts = "IPTS-7"']),
        (["# [experiment.source]"], ["[experiment.source]"]),
        (
            ['# location = "/SNS/REF_L/{ipts}/shared/autoreduce/new_reduction"'],
            ['location = "/data/x"'],
        ),
    ]


def test_a_save_on_a_windows_file_keeps_its_line_endings(project: Path) -> None:
    """Each line ends as the file's lines did, or every line reads as changed."""
    from nr_workbench.project.settings import save

    toml = project / "nrw.toml"
    windows = toml.read_bytes().replace(b"\n", b"\r\n")
    windows = windows.replace(b"[conventions]", b"# our note\r\n[conventions]")
    toml.write_bytes(windows)

    save(project, {"ipts": "IPTS-7", "source.location": "/data/x"})

    after = toml.read_bytes()
    assert b"\n" not in after.replace(b"\r\n", b"")
    assert changed_lines(windows.decode(), after.decode()) == [
        (['ipts = "IPTS-00001"'], ['ipts = "IPTS-7"']),
        (["# [experiment.source]"], ["[experiment.source]"]),
        (
            ['# location = "/SNS/REF_L/{ipts}/shared/autoreduce/new_reduction"'],
            ['location = "/data/x"'],
        ),
    ]
    assert load_config(project).raw["experiment"]["source"] == {"location": "/data/x"}


def test_a_save_on_a_file_without_a_final_newline_keeps_its_last_line(
    project: Path,
) -> None:
    from nr_workbench.project.settings import save

    toml = project / "nrw.toml"
    text = toml.read_text(encoding="utf-8").rstrip("\n") + "\n# the last word"
    toml.write_text(text, encoding="utf-8")

    save(project, {"feed.poll_seconds": 20})

    after = toml.read_text(encoding="utf-8")
    assert "\n# the last word" in after
    assert load_config(project).raw["experiment"]["feed"] == {"poll_seconds": 20}
    assert changed_lines(text, after) == [
        (["# [experiment.feed]"], ["[experiment.feed]"]),
        (["# poll_seconds = 30"], ["poll_seconds = 20"]),
    ]


def test_a_save_over_a_file_edited_since_it_was_read_is_a_conflict(
    project: Path,
) -> None:
    from nr_workbench.project.settings import read, save
    from nr_workbench.project.tomlfile import TomlConflictError

    shown = read(project)
    toml = project / "nrw.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    before = toml.read_bytes()

    with pytest.raises(TomlConflictError):
        save(project, {"ipts": "IPTS-9"}, base_revision=shown.revision)

    assert toml.read_bytes() == before


def test_a_save_refreshes_the_lock_so_a_later_template_change_upgrades(
    project: Path,
) -> None:
    """Otherwise the next nrw upgrade finds the file "edited" and leaves a .nrw-new."""
    from nr_workbench.commands.init_cmd import plan_project_files
    from nr_workbench.project.scaffold import Outcome, apply_scaffold
    from nr_workbench.project.settings import save

    save(project, {"source.location": "/data/x"})
    planned = [
        dataclasses.replace(p, content=p.content + b"# a newer template\n")
        if p.relpath == "nrw.toml"
        else p
        for p in plan_project_files(init_context(project))
    ]

    report = apply_scaffold(project, planned)

    outcome = {f.relpath: f.outcome for f in report.files}["nrw.toml"]
    assert outcome is Outcome.UPGRADE
    assert "location" in load_config(project).raw["experiment"]["source"]


def test_unsetting_every_setting_returns_the_file_to_the_render(project: Path) -> None:
    from nr_workbench.project.settings import save

    toml = project / "nrw.toml"
    original = toml.read_text(encoding="utf-8")
    save(project, {"source.location": "/data/x", "feed.poll_seconds": 20})

    save(project, {"source.location": None, "feed.poll_seconds": None})

    assert toml.read_text(encoding="utf-8") == original


def test_a_save_keeps_the_previous_file(project: Path) -> None:
    from nr_workbench.project.settings import save

    before = (project / "nrw.toml").read_text(encoding="utf-8")

    result = save(project, {"ipts": "IPTS-5"})

    assert (project / result.backup).read_text(encoding="utf-8") == before
    assert result.backup.startswith(".nrw/backups/")


def test_a_preview_writes_nothing(project: Path) -> None:
    from nr_workbench.project.settings import save

    before = (project / "nrw.toml").read_bytes()

    result = save(project, {"ipts": "IPTS-5"}, write=False)

    assert result.changed and '+ipts = "IPTS-5"' in result.diff
    assert (project / "nrw.toml").read_bytes() == before


def test_a_save_on_a_damaged_lock_saves_and_leaves_the_lock(project: Path) -> None:
    from nr_workbench.project.settings import save

    lock = project / ".nrw" / "scaffold.lock.json"
    lock.write_text("{ not json", encoding="utf-8")

    result = save(project, {"ipts": "IPTS-5"})

    assert load_config(project).ipts == "IPTS-5"
    assert lock.read_text(encoding="utf-8") == "{ not json"
    assert any("scaffold lock" in note for note in result.notes)


def test_saving_the_ipts_refreshes_an_untouched_readme(project: Path) -> None:
    from nr_workbench.project.settings import save

    result = save(project, {"ipts": "IPTS-4242"})

    assert "README.md" in result.written
    assert "IPTS-4242" in (project / "README.md").read_text(encoding="utf-8")
    checked = init_check(project)
    assert checked.exit_code == 0, checked.output


def test_a_hand_edited_readme_is_left_alone_and_said_so(project: Path) -> None:
    from nr_workbench.project.settings import save

    readme = project / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\nOur notes.\n")

    result = save(project, {"ipts": "IPTS-4242"})

    assert "README.md" not in result.written
    assert readme.read_text(encoding="utf-8").endswith("Our notes.\n")
    assert any("README.md" in note for note in result.notes)


def test_a_file_from_before_the_experiment_block_gets_nrws_block(project: Path) -> None:
    from nr_workbench.project.settings import save

    toml = project / "nrw.toml"
    text = toml.read_text(encoding="utf-8")
    older = text[: text.index("# The experiment:")] + "# our own note\n"
    toml.write_text(older, encoding="utf-8")

    save(project, {"feed.poll_seconds": 10})

    after = toml.read_text(encoding="utf-8")
    assert after.startswith(older)
    assert '[experiment.feed]\n# kind = "directory"\npoll_seconds = 10\n' in after
    assert "# [experiment.source]" in after


def test_a_shape_nrw_does_not_edit_is_refused_with_the_lines_to_add(
    project: Path,
) -> None:
    from nr_workbench.project.settings import save
    from nr_workbench.project.tomlfile import TomlEditError

    toml = project / "nrw.toml"
    text = toml.read_text(encoding="utf-8")
    text = text[: text.index("# The experiment:")]
    text += '[experiment]\nsource = { location = "/data/old" }\n'
    toml.write_text(text, encoding="utf-8")

    with pytest.raises(TomlEditError) as refused:
        save(project, {"source.location": "/data/new"})

    assert 'location = "/data/new"' in refused.value.lines
    assert toml.read_text(encoding="utf-8") == text


def test_changing_the_ipts_of_a_catalogued_experiment_needs_confirming(
    project: Path,
) -> None:
    from nr_workbench.project.settings import NeedsConfirmation, save

    with pytest.raises(NeedsConfirmation) as asked:
        save(project, {"ipts": "IPTS-2"}, catalogued_runs=3)

    assert asked.value.needs == "ipts-change"
    assert "3 run(s)" in str(asked.value)
    assert load_config(project).ipts == "IPTS-00001"
    save(project, {"ipts": "IPTS-2"}, catalogued_runs=3, confirmed={"ipts-change"})
    assert load_config(project).ipts == "IPTS-2"


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX, not root")
def test_a_read_only_project_is_refused_cleanly(project: Path) -> None:
    from nr_workbench.project.settings import save

    before = (project / "nrw.toml").read_bytes()
    folders = [project, project / ".nrw", project / ".nrw" / "cache"]
    for folder in folders:
        folder.mkdir(exist_ok=True)
        folder.chmod(0o555)
    try:
        with pytest.raises(OSError):
            save(project, {"ipts": "IPTS-5"})
    finally:
        for folder in folders:
            folder.chmod(0o755)

    assert (project / "nrw.toml").read_bytes() == before


# --------------------------------------------------------------------------
# What a setting may be
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [("34347", "IPTS-34347"), ("ipts-00001", "IPTS-00001"), ("", ""), (None, "")],
)
def test_an_ipts_is_saved_in_one_form(project: Path, value, expected: str) -> None:
    from nr_workbench.project.settings import validate

    edits, _ = validate(project, {"ipts": value})

    assert edits["beamtime"]["ipts"].value == expected


@pytest.mark.parametrize(
    "name,value,message",
    [
        ("ipts", "IPTS-3x", "not an IPTS number"),
        ("ipts", 34347, "must be text"),
        ("ipts", "123456789", "not an IPTS number"),
        ("label", "\ud800", "not text"),
        ("label", "two\nlines", "one line"),
        ("label", "x" * 101, "longer than"),
        ("label", "a\u2028b", "one line"),
        ("label", "a\u0085b", "one line"),
        ("source.location", "data/new_reduction", "not a full path"),
        ("source.location", "~/data", "full path"),
        ("source.location", "/data/\x00x", "one line"),
        ("source.location", "/" + "d" * 1024, "longer than"),
        ("source.location", "", "empty"),
        ("source.settle_seconds", 5, "from 10 to 3600"),
        ("source.settle_seconds", 9.9, "from 10 to 3600"),
        ("source.settle_seconds", 3601, "from 10 to 3600"),
        ("feed.poll_seconds", 4.9, "from 5 to 600"),
        ("feed.poll_seconds", 601, "from 5 to 600"),
        ("source.settle_seconds", True, "number of seconds"),
        ("feed.poll_seconds", float("nan"), "number of seconds"),
        ("feed.poll_seconds", "30", "number of seconds"),
        ("feed.kind", "monitor", "not available yet"),
        ("source.kind", "tiled", "not known"),
        ("source.kind", "s3", "not known"),
        ("colour", "blue", "not a setting"),
    ],
)
def test_a_value_that_is_not_allowed_is_refused_with_the_reason(
    project: Path, name: str, value, message: str
) -> None:
    from nr_workbench.project.settings import SettingsError, validate

    with pytest.raises(SettingsError, match=message):
        validate(project, {name: value})


@pytest.mark.parametrize(
    "name,value",
    [
        ("label", "x" * 100),
        ("source.location", "/" + "d" * 1023),
        ("source.settle_seconds", 10),
        ("source.settle_seconds", 3600),
        ("feed.poll_seconds", 5),
        ("feed.poll_seconds", 600),
        ("feed.poll_seconds", 7.5),
    ],
)
def test_a_value_at_the_edge_of_what_is_allowed_is_saved_as_given(
    project: Path, name: str, value
) -> None:
    from nr_workbench.project.settings import validate

    edits, _ = validate(project, {name: value})

    table, key = {
        "label": ("beamtime", "label"),
        "source.location": ("experiment.source", "location"),
        "source.settle_seconds": ("experiment.source", "settle_seconds"),
        "feed.poll_seconds": ("experiment.feed", "poll_seconds"),
    }[name]
    assert edits[table][key].value == value


@pytest.mark.parametrize("inside", ["samples", ".nrw", "experiment"])
def test_a_data_location_inside_the_project_is_refused(
    project: Path, inside: str
) -> None:
    from nr_workbench.project.settings import SettingsError, validate

    with pytest.raises(SettingsError, match=f"inside this project's {inside}/"):
        validate(project, {"source.location": str(project / inside / "x")})


def test_a_data_location_in_the_home_directory_is_allowed_with_a_warning(
    project: Path,
) -> None:
    from nr_workbench.project.settings import validate

    edits, warnings = validate(
        project, {"source.location": str(Path.home() / "beamtime" / "data")}
    )

    assert edits["experiment.source"]["location"].value.endswith("beamtime/data")
    assert any("home directory" in w for w in warnings)


def test_a_location_template_keeps_its_placeholder(project: Path) -> None:
    from nr_workbench.project.settings import validate

    edits, _ = validate(project, {"source.location": "/SNS/REF_L/{ipts}/x"})

    assert edits["experiment.source"]["location"].value == "/SNS/REF_L/{ipts}/x"


def test_choosing_the_default_kind_follows_the_default(project: Path) -> None:
    from nr_workbench.project.settings import validate
    from nr_workbench.project.tomlfile import Unset

    edits, _ = validate(project, {"source.kind": "local", "feed.kind": "directory"})

    assert isinstance(edits["experiment.source"]["kind"], Unset)
    assert isinstance(edits["experiment.feed"]["kind"], Unset)


def test_needs_setup_is_judged_without_the_data_mount(project: Path) -> None:
    from nr_workbench.experiment.config import experiment_config
    from nr_workbench.project.settings import save

    save(project, {"ipts": ""})
    assert experiment_config(load_config(project)).needs_setup

    save(project, {"source.location": "/data/without/ipts"})
    assert not experiment_config(load_config(project)).needs_setup


def test_a_save_never_writes_an_edit_its_proof_rejects(
    project: Path, monkeypatch
) -> None:
    """Whatever the block rewrite gets wrong, the proof after it stops the write."""
    from nr_workbench.project import nrwtoml, tomlfile
    from nr_workbench.project.settings import save

    toml = project / "nrw.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8") + "# hand-edited, so the edit path runs\n",
        encoding="utf-8",
    )
    before = toml.read_bytes()
    real = tomlfile.replace_block

    def careless(*args):
        return real(*args).replace('label = "june2026"', 'label = "oops"')

    monkeypatch.setattr(nrwtoml, "replace_block", careless)

    with pytest.raises(tomlfile.TomlEditError):
        save(project, {"source.location": "/data/x"})

    assert toml.read_bytes() == before


def test_a_save_on_an_unedited_older_file_leaves_the_upgrade_to_init(
    project: Path,
) -> None:
    """The save changes its own lines; `nrw init` upgrades the rest and keeps them.

    Once, the save re-rendered the whole file from the current template --
    unproved, since the rest of the template changed too.
    """
    from nr_workbench.project.scaffold import load_lock, sha256_bytes, write_lock
    from nr_workbench.project.settings import save

    toml = project / "nrw.toml"
    current = toml.read_text(encoding="utf-8")
    older = current[: current.index("# The experiment:")].rstrip("\n") + "\n"
    toml.write_text(older, encoding="utf-8")
    lock_path = project / ".nrw" / "scaffold.lock.json"
    lock = load_lock(lock_path)
    lock["nrw.toml"]["sha256_at_install"] = sha256_bytes(older.encode())
    write_lock(lock_path, lock)

    result = save(project, {"feed.poll_seconds": 10})

    assert toml.read_text(encoding="utf-8").startswith(older)
    assert any("next `nrw init`" in note for note in result.notes)
    init = CliRunner().invoke(main, ["init", str(project)])
    assert init.exit_code == 0, init.output
    assert toml.read_text(encoding="utf-8") == rendered_nrw_toml(project)
    assert load_config(project).raw["experiment"]["feed"] == {"poll_seconds": 10}
    assert not (project / "nrw.toml.nrw-new").exists()
    checked = init_check(project)
    assert checked.exit_code == 0, checked.output


@pytest.mark.parametrize(
    "text,message",
    [
        ('beamtime = "oops"\n', "[beamtime] must be a table"),
        ('contract_version = "one"\n', "whole number"),
    ],
)
def test_a_nrw_toml_of_the_wrong_shape_is_a_sentence_not_a_traceback(
    tmp_path: Path, text: str, message: str
) -> None:
    (tmp_path / "nrw.toml").write_text(text, encoding="utf-8")

    with pytest.raises(ProjectConfigError, match=message.replace("[", "\\[")):
        load_config(tmp_path)


# --------------------------------------------------------------------------
# One writer at a time, and nrw's file stays nrw's -- never a person's
# --------------------------------------------------------------------------


def test_a_save_while_nrw_init_runs_is_not_put_back(project: Path, monkeypatch) -> None:
    """Init plans, a save lands, init writes: the save must survive.

    It once did not. The save recorded nrw.toml as nrw's, and init's older plan
    then "upgraded" it -- the chosen folder was nowhere on disk, not even in
    the save's backup.
    """
    import threading

    from nr_workbench.commands import init_cmd
    from nr_workbench.project.settings import save

    real = init_cmd.apply_scaffold
    saver = threading.Thread(
        target=save, args=(project, {"source.location": "/data/chosen"})
    )

    def apply_after_a_save_tries_to_land(*args, **kwargs):
        saver.start()
        saver.join(timeout=0.5)  # blocks on init's lock, or lands here unguarded
        return real(*args, **kwargs)

    monkeypatch.setattr(init_cmd, "apply_scaffold", apply_after_a_save_tries_to_land)

    result = CliRunner().invoke(main, ["init", str(project)])
    saver.join(timeout=10)

    assert result.exit_code == 0, result.output
    assert not saver.is_alive()
    source = load_config(project).raw["experiment"]["source"]
    assert source["location"] == "/data/chosen"
    # Nor did init find a half-saved file to set a .nrw-new beside: the save
    # waited for init, whole.
    assert not (project / "nrw.toml.nrw-new").exists()


def test_a_save_on_a_hand_edited_file_leaves_it_the_persons(project: Path) -> None:
    """A later template change must leave it DRIFTED, with the person's note."""
    from nr_workbench.commands.init_cmd import plan_project_files
    from nr_workbench.project.scaffold import Outcome, apply_scaffold
    from nr_workbench.project.settings import save

    toml = project / "nrw.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            "[conventions]", "# our note\n[conventions]"
        ),
        encoding="utf-8",
    )
    save(project, {"source.location": "/data/x"})
    planned = [
        dataclasses.replace(p, content=p.content + b"# a newer template\n")
        if p.relpath == "nrw.toml"
        else p
        for p in plan_project_files(init_context(project))
    ]

    report = apply_scaffold(project, planned)

    outcome = {f.relpath: f.outcome for f in report.files}["nrw.toml"]
    assert outcome is Outcome.DRIFTED
    assert "# our note" in toml.read_text(encoding="utf-8")


def test_nrw_audience_on_an_untouched_older_file_leaves_init_nothing_beside_it(
    project: Path, monkeypatch
) -> None:
    from nr_workbench.project.scaffold import load_lock, sha256_bytes, write_lock

    toml = project / "nrw.toml"
    current = toml.read_text(encoding="utf-8")
    older = current[: current.index("# The experiment:")].rstrip("\n") + "\n"
    toml.write_text(older, encoding="utf-8")
    lock_path = project / ".nrw" / "scaffold.lock.json"
    lock = load_lock(lock_path)
    lock["nrw.toml"]["sha256_at_install"] = sha256_bytes(older.encode())
    write_lock(lock_path, lock)

    assert (
        nrw(project, monkeypatch, "audience", "--set", "statistics=expert").exit_code
        == 0
    )
    init = nrw(project, monkeypatch, "init")

    assert init.exit_code == 0, init.output
    assert not (project / "nrw.toml.nrw-new").exists()
    assert load_config(project).raw["audience"]["statistics"] == "expert"


#: The block as an earlier nrw wrote it: a `#` line between the tables, and a
#: note on the feed's kind.
OLDER_BLOCK = """# [experiment.source]
# kind = "local"
# location = "/SNS/REF_L/{ipts}/shared/autoreduce/new_reduction"
# settle_seconds = 300
#
# [experiment.feed]
# kind = "directory"   # a run is noticed when its reduced files appear
# poll_seconds = 30"""


def test_an_older_commented_block_is_switched_on_where_it_stands(project: Path) -> None:
    """Never a second table below it, which its own advice would then break."""
    from nr_workbench.project.settings import save

    toml = project / "nrw.toml"
    text = toml.read_text(encoding="utf-8")
    ours = experiment_block({})  # found as nrw renders it, wherever it stands
    assert text.count(ours) == 1
    text = text.replace(ours, OLDER_BLOCK)
    text = text.replace("[conventions]", "# our note\n[conventions]")
    toml.write_text(text, encoding="utf-8")

    save(project, {"source.location": "/data/x", "feed.poll_seconds": 10})

    after = toml.read_text(encoding="utf-8")
    assert after.count("[experiment.source]") == 1
    assert '\n[experiment.source]\n# kind = "local"\nlocation = "/data/x"\n' in after
    assert "\npoll_seconds = 10\n" in after
    assert load_config(project).raw["experiment"]["feed"] == {"poll_seconds": 10}


def test_an_unreadable_catalog_still_asks_before_the_ipts_changes(
    project: Path,
) -> None:
    """It once read as "no runs", which skipped the question."""
    from nr_workbench.project.settings import NeedsConfirmation, save

    with pytest.raises(NeedsConfirmation, match="cannot be read"):
        save(project, {"ipts": "IPTS-2"}, catalogued_runs=None)

    assert load_config(project).ipts == "IPTS-00001"


@pytest.mark.parametrize(
    "order",
    [
        ("nr_workbench.project.settings", "nr_workbench.project.render"),
        ("nr_workbench.project.render", "nr_workbench.project.settings"),
        ("nr_workbench.project.nrwtoml", "nr_workbench.experiment.config"),
    ],
)
def test_the_settings_modules_import_in_any_order(order: tuple[str, str]) -> None:
    """No import cycle waiting for someone to move an import to the top."""
    import subprocess
    import sys

    code = "; ".join(f"import {name}" for name in order)
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_the_default_kinds_are_named_not_the_first_option_listed() -> None:
    from nr_workbench.experiment.config import experiment_config
    from nr_workbench.project.config import DEFAULT_FEED_KIND, DEFAULT_SOURCE_KIND

    config = experiment_config(None)

    assert EXPERIMENT_KEYS["experiment.source"]["kind"] == DEFAULT_SOURCE_KIND
    assert EXPERIMENT_KEYS["experiment.feed"]["kind"] == DEFAULT_FEED_KIND
    assert (config.source.kind, config.feed.kind) == (
        DEFAULT_SOURCE_KIND,
        DEFAULT_FEED_KIND,
    )


def test_a_hand_typed_setting_out_of_range_is_said_not_used(project: Path) -> None:
    """The same range the Settings page holds a value to."""
    from nr_workbench.experiment.config import experiment_config

    toml = project / "nrw.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            '# [experiment.feed]\n# kind = "directory"\n# poll_seconds = 30',
            "[experiment.feed]\npoll_seconds = 1",
        ),
        encoding="utf-8",
    )

    config = experiment_config(load_config(project))

    assert config.feed.poll_seconds == 30
    assert any("from 5 to 600" in p.message for p in config.problems)


# --------------------------------------------------------------------------
# A value typed into a comment is said, not silently ignored
# --------------------------------------------------------------------------

#: The block as a person left it: the folder typed over the placeholder's
#: default, and neither `#` removed -- so nrw watched its default location.
TYPED_IN_A_COMMENT = """# [experiment.source]
# kind = "local"
# location = "/data/IPTS-37787/test"
# settle_seconds = 300

# [experiment.feed]
# kind = "directory"
# poll_seconds = 30"""


def typed_into_a_comment(project: Path) -> Path:
    toml = project / "nrw.toml"
    text = toml.read_text(encoding="utf-8")
    ours = experiment_block({})
    assert text.count(ours) == 1
    toml.write_text(text.replace(ours, TYPED_IN_A_COMMENT), encoding="utf-8")
    return toml


def test_nrws_own_placeholders_are_not_settings_typed_in_a_comment(
    project: Path,
) -> None:
    from nr_workbench.project.tomlfile import settings_in_comments

    text = (project / "nrw.toml").read_text(encoding="utf-8")

    assert settings_in_comments(text, EXPERIMENT_KEYS, tomllib.loads(text)) == []


def test_a_folder_typed_into_a_commented_line_is_named_with_the_fix(
    project: Path,
) -> None:
    """It once left `nrw serve` on the default location with nothing said."""
    from nr_workbench.experiment.config import experiment_config_for

    toml = typed_into_a_comment(project)
    line = (
        toml.read_text(encoding="utf-8")
        .splitlines()
        .index('# location = "/data/IPTS-37787/test"')
    )

    config = experiment_config_for(project)

    assert config.source.location != "/data/IPTS-37787/test"  # still not read
    [problem] = [p for p in config.problems if "in a comment" in p.message]
    assert f"line {line + 1} gives `location` in a comment" in problem.message
    assert "and from the `# [experiment.source]` line above it" in problem.message


def test_switching_the_line_on_as_the_message_says_is_the_whole_fix(
    project: Path,
) -> None:
    from nr_workbench.experiment.config import experiment_config_for

    toml = typed_into_a_comment(project)
    text = toml.read_text(encoding="utf-8")
    text = text.replace("# [experiment.source]", "[experiment.source]")
    text = text.replace('# location = "/data/', 'location = "/data/')
    toml.write_text(text, encoding="utf-8")

    config = experiment_config_for(project)

    assert config.source.location == "/data/IPTS-37787/test"
    assert not [p for p in config.problems if "in a comment" in p.message]


def test_a_comment_beside_a_setting_that_is_set_is_only_a_note(project: Path) -> None:
    """What a save through the Settings page leaves: the old line, and the key."""
    from nr_workbench.experiment.config import experiment_config_for
    from nr_workbench.project.settings import save

    typed_into_a_comment(project)
    save(project, {"source.location": "/data/IPTS-37787/test"})

    config = experiment_config_for(project)

    assert config.source.location == "/data/IPTS-37787/test"
    assert not [p for p in config.problems if "in a comment" in p.message]


def test_a_commented_value_under_a_table_that_is_written_out_is_named(
    project: Path,
) -> None:
    from nr_workbench.experiment.config import experiment_config_for
    from nr_workbench.project.settings import save

    save(project, {"source.location": "/data/x"})
    toml = project / "nrw.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            "# settle_seconds = 300", "# settle_seconds = 120"
        ),
        encoding="utf-8",
    )

    [problem] = [
        p
        for p in experiment_config_for(project).problems
        if "in a comment" in p.message
    ]
    assert "gives `settle_seconds` in a comment" in problem.message
    assert "remove the `#` from that line --" in problem.message


def test_nrw_experiment_settings_names_the_commented_folder(
    project: Path, monkeypatch
) -> None:
    typed_into_a_comment(project)

    result = nrw(project, monkeypatch, "experiment", "settings")

    assert result.exit_code == 0, result.output
    assert "gives `location` in a comment" in result.output
