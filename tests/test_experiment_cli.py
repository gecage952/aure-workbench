"""`nrw experiment`: the catalog from the command line.

The command line is how an assistant sees the experiment, and how a script
checks it, so what matters here is the contract: previews write nothing, the
exit code says when a person must look, and an unattended agent is refused
anything that changes the organization.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from nr_workbench.cli import main

from .experiment_fixtures import write_autoreduced


@pytest.fixture
def expt(project: Path, tmp_path: Path) -> Path:
    """The scaffolded project, watching a folder with two complete runs."""
    folder = tmp_path / "facility" / "new_reduction"
    for run in (234277, 234280):
        write_autoreduced(folder, run, [1, 2, 3], planned=3, mtime=time.time() - 3600)
    write_autoreduced(folder, 234283, [1], planned=3, mtime=time.time() - 3600)
    with (project / "nrw.toml").open("a", encoding="utf-8") as handle:
        handle.write(f'\n[experiment.source]\nlocation = "{folder}"\n')
    return project


def nrw(*args: str, env: dict[str, str] | None = None):
    return CliRunner().invoke(main, list(args), env=env or {}, catch_exceptions=False)


def status(root: Path) -> dict:
    result = nrw("experiment", "status", "--root", str(root), "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_status_lists_every_run_with_its_state(expt: Path) -> None:
    payload = status(expt)

    states = {row["run"]: row["state"] for row in payload["runs"]}
    assert states == {234277: "complete", 234280: "complete", 234283: "unconfirmed"}
    assert payload["reachable"] is True
    assert payload["samples"] == []


def test_status_reports_the_experiments_own_context(expt: Path) -> None:
    """A diff of the catalog's parquet shows nothing, and an assistant reads
    the experiment from here: status says what the page does."""
    from nr_workbench.experiment.model import ExperimentChange
    from nr_workbench.experiment.store import ParquetCatalogStore

    before = nrw("experiment", "status", "--root", str(expt))
    ParquetCatalogStore.for_project(expt).update(
        experiment=ExperimentChange(0, {"summary": "Li plating.", "notes": "Cu/Pt."})
    )
    after = nrw("experiment", "status", "--root", str(expt))

    listed = 'context  summary, notes; --json shows them under "experiment"'
    assert "context  none yet" in before.output
    assert listed in after.output
    payload = status(expt)
    assert payload["experiment"] == {
        "summary": "Li plating.",
        "hypotheses": "",
        "notes": "Cu/Pt.",
        "rev": 1,
    }
    assert payload["catalog"]["readable"] is True


def test_status_says_when_the_catalog_cannot_be_read(expt: Path) -> None:
    """An unreadable catalog is not an unwritten one: the page says
    `readable`, and so must the command line an assistant reads."""
    from nr_workbench.experiment.model import ExperimentChange
    from nr_workbench.experiment.store import ParquetCatalogStore

    ParquetCatalogStore.for_project(expt).update(
        experiment=ExperimentChange(0, {"summary": "Li plating."})
    )
    (expt / "experiment" / "runs.parquet").write_bytes(b"not parquet")

    shown = nrw("experiment", "status", "--root", str(expt))
    payload = status(expt)

    assert "context  cannot be read; see below" in shown.output
    assert payload["catalog"]["readable"] is False
    assert payload["experiment"]["rev"] == 0
    assert any(p["scope"] == "catalog" for p in payload["problems"])


def test_assign_records_the_runs_and_their_titles(expt: Path) -> None:
    result = nrw(
        "experiment",
        "assign",
        "234277",
        "234280",
        "--sample",
        "Sample6",
        "--type",
        "full Q",
        "--condition",
        "OCV",
        "--root",
        str(expt),
    )
    assert result.exit_code == 0, result.output

    rows = {row["run"]: row for row in status(expt)["runs"]}
    assert rows[234277]["sample"] == "Sample6"
    assert rows[234277]["condition"] == "OCV"

    from nr_workbench.experiment.model import RunKey
    from nr_workbench.experiment.store import ParquetCatalogStore

    entry = ParquetCatalogStore.for_project(expt).load().runs[RunKey(234277)]
    assert entry.title == "S1_air-234277-1."


@pytest.mark.parametrize(
    "args,message",
    [
        (["234277", "--sample", "S", "--unassign"], "contradict"),
        (["234277", "--exclude", "--include"], "contradict"),
        (["234277"], "Nothing to record"),
        (["21838x", "--sample", "S"], "not a run number"),
        (["234277", "--sample", "S", "--condition", "a|b"], "'|'"),
    ],
)
def test_assign_refuses_with_the_reason(expt: Path, args, message: str) -> None:
    result = CliRunner().invoke(
        main, ["experiment", "assign", *args, "--root", str(expt)]
    )

    assert result.exit_code != 0
    assert message in result.output


def test_apply_without_write_writes_nothing(expt: Path) -> None:
    nrw("experiment", "assign", "234277", "--sample", "Sample6", "--root", str(expt))

    result = nrw("experiment", "apply", "--root", str(expt))

    assert result.exit_code == 0, result.output
    assert "copy" in result.output and "--write" in result.output
    assert not (expt / "samples" / "Sample6").exists()


def test_apply_write_copies_then_a_second_apply_is_in_step(expt: Path) -> None:
    nrw(
        "experiment",
        "assign",
        "234277",
        "234280",
        "--sample",
        "Sample6",
        "--root",
        str(expt),
    )

    first = nrw("experiment", "apply", "--write", "--root", str(expt))
    assert first.exit_code == 0, first.output
    steady = expt / "samples" / "Sample6" / "data" / "steady"
    assert len(list(steady.glob("REFL_*"))) == 6

    second = nrw("experiment", "apply", "--root", str(expt), "--json")
    plan = json.loads(second.output)
    assert plan["writes"] is False
    assert plan["samples"][0]["sample_md"] == "unchanged"


def test_apply_confirm_copies_an_unconfirmed_run(expt: Path) -> None:
    nrw("experiment", "assign", "234283", "--sample", "Sample6", "--root", str(expt))

    preview = nrw("experiment", "apply", "--root", str(expt))
    assert "deferred" in preview.output

    nrw("experiment", "apply", "--write", "--confirm", "234283", "--root", str(expt))
    steady = expt / "samples" / "Sample6" / "data" / "steady"
    assert [p.name for p in steady.glob("REFL_*")] == [
        "REFL_234283_1_234283_autoreduction.dat"
    ]


def test_apply_exits_non_zero_when_a_person_must_look(expt: Path) -> None:
    nrw("experiment", "assign", "234277", "--sample", "Sample6", "--root", str(expt))
    nrw("experiment", "apply", "--write", "--root", str(expt))
    copy = expt / "samples/Sample6/data/steady/REFL_234277_1_234277_autoreduction.dat"
    copy.write_text("# edited\n")

    result = CliRunner().invoke(main, ["experiment", "apply", "--root", str(expt)])

    assert result.exit_code == 1
    assert "local-edited" in result.output


def test_apply_names_a_sample_md_the_catalog_cannot_reach(expt: Path) -> None:
    sample = expt / "samples" / "Sample6"
    sample.mkdir(parents=True)
    (sample / "sample.md").write_text("# Sample6\n\nWritten by hand.\n")
    nrw("experiment", "assign", "234277", "--sample", "Sample6", "--root", str(expt))

    result = CliRunner().invoke(main, ["experiment", "apply", "--root", str(expt)])

    assert result.exit_code == 1
    assert "not written by nrw" in result.output


def test_adopt_then_rewrite_puts_a_hand_written_sample_in_the_catalog(
    expt: Path,
) -> None:
    sample = expt / "samples" / "Sample6"
    sample.mkdir(parents=True)
    (sample / "sample.md").write_text(
        "# Cu/Pt\n\n## Description\n\nCu on Pt.\n\n## Measurements\n\n"
        "| Run | Type | Condition |\n|---|---|---|\n| 234277 | full Q | OCV |\n"
    )

    preview = nrw("experiment", "adopt", "Sample6", "--root", str(expt))
    assert "run 234277" in preview.output and "Nothing was written" in preview.output

    done = nrw(
        "experiment", "adopt", "Sample6", "--write", "--rewrite", "--root", str(expt)
    )
    assert done.exit_code == 0, done.output
    assert "previous kept at .nrw/backups/" in done.output
    rows = {row["run"]: row for row in status(expt)["runs"]}
    assert rows[234277]["sample"] == "Sample6"


def test_apply_write_exits_non_zero_when_a_copy_fails(expt: Path, monkeypatch) -> None:
    """A script must not read a run that failed to copy as applied."""
    from nr_workbench.experiment.sources.local import LocalDirectorySource

    nrw("experiment", "assign", "234277", "--sample", "Sample6", "--root", str(expt))

    def unreadable(self, listed):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(LocalDirectorySource, "read_bytes", unreadable)

    result = CliRunner().invoke(
        main, ["experiment", "apply", "--write", "--root", str(expt)]
    )

    assert result.exit_code == 1
    assert "run 234277 was not copied" in result.output
    assert not list((expt / "samples/Sample6/data/steady").glob("REFL_*"))


def conflict(lock: Path) -> bytes:
    """Leave the scaffold lock as a merge would, and return its bytes."""
    lock.write_text(
        "<<<<<<< HEAD\n" + lock.read_text() + "=======\n>>>>>>> theirs\n",
        encoding="utf-8",
    )
    return lock.read_bytes()


def test_adopt_rewrite_on_a_conflicted_lock_changes_nothing(expt: Path) -> None:
    """Refused before the catalog changes: adopted-but-not-rewritten is half done."""
    from nr_workbench.experiment.store import ParquetCatalogStore

    md = expt / "samples" / "Sample6" / "sample.md"
    md.parent.mkdir(parents=True)
    md.write_text(
        "# Cu/Pt\n\n## Measurements\n\n"
        "| Run | Type | Condition |\n|---|---|---|\n| 234277 | full Q | OCV |\n"
    )
    lock = expt / ".nrw" / "scaffold.lock.json"
    locked, written = conflict(lock), md.read_bytes()

    result = CliRunner().invoke(
        main,
        ["experiment", "adopt", "Sample6", "--write", "--rewrite", "--root", str(expt)],
    )

    assert result.exit_code == 1
    assert "conflict markers" in result.output
    assert lock.read_bytes() == locked
    assert md.read_bytes() == written
    assert ParquetCatalogStore.for_project(expt).load().runs == {}


def test_status_names_a_misspelled_setting_and_the_folder_it_fell_back_to(
    project: Path,
) -> None:
    """A typo in nrw.toml looks exactly like an empty beamtime unless it is said."""
    with (project / "nrw.toml").open("a", encoding="utf-8") as handle:
        handle.write('\n[experiment.source]\nlocaton = "/data/elsewhere"\n')

    payload = status(project)

    messages = [p["message"] for p in payload["problems"]]
    assert any("locaton" in m for m in messages)
    assert payload["reachable"] is False
    assert any("data mount" in m for m in messages)
    assert payload["source"]["path"].startswith("/SNS/REF_L/IPTS-00001/")


def test_sample_new_is_refused_on_a_conflicted_lock(project: Path, monkeypatch) -> None:
    lock = project / ".nrw" / "scaffold.lock.json"
    locked = conflict(lock)
    monkeypatch.chdir(project)

    result = CliRunner().invoke(main, ["sample", "new", "Sample9"])

    assert result.exit_code != 0
    assert "conflict markers" in result.output
    assert lock.read_bytes() == locked
    assert not (project / "samples" / "Sample9").exists()


def test_rewrite_without_write_is_refused(expt: Path) -> None:
    result = CliRunner().invoke(
        main, ["experiment", "adopt", "--rewrite", "--root", str(expt)]
    )

    assert result.exit_code != 0 and "needs --write" in result.output


def test_release_hands_sample_md_back(expt: Path) -> None:
    from nr_workbench.project.scaffold import load_lock

    nrw("experiment", "assign", "234277", "--sample", "Sample6", "--root", str(expt))
    nrw("experiment", "apply", "--write", "--root", str(expt))

    result = nrw("experiment", "release", "Sample6", "--root", str(expt))

    assert "yours now" in result.output
    lock = load_lock(expt / ".nrw" / "scaffold.lock.json")
    assert "samples/Sample6/sample.md" not in lock


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------


def test_settings_says_what_is_set_and_what_follows_the_default(expt: Path) -> None:
    result = nrw("experiment", "settings", "--root", str(expt))

    assert result.exit_code == 0, result.output
    assert "IPTS       IPTS-00001" in result.output
    assert "poll       30 s  (nrw's default)" in result.output
    assert "SNS web monitor (watcher)" in result.output
    assert "catalog    parquet" in result.output
    assert "  coming     SNS web monitor (watcher)\n" in result.output


def test_settings_json_is_the_settings_pages_view(expt: Path) -> None:
    payload = json.loads(
        nrw("experiment", "settings", "--root", str(expt), "--json").output
    )

    assert payload["settings"]["schema"] == "nrw-settings/1"
    assert payload["settings"]["values"]["ipts"] == "IPTS-00001"
    assert (payload["change"], payload["check"]) == (None, None)


def test_settings_previews_a_change_and_writes_nothing(expt: Path) -> None:
    before = (expt / "nrw.toml").read_bytes()

    result = nrw("experiment", "settings", "--poll", "20", "--root", str(expt))

    assert result.exit_code == 0, result.output
    assert "+poll_seconds = 20" in result.output
    assert "--write" in result.output
    assert (expt / "nrw.toml").read_bytes() == before


def test_settings_write_saves_and_names_the_backup(expt: Path) -> None:
    result = nrw(
        "experiment", "settings", "--poll", "20", "--write", "--root", str(expt)
    )

    assert result.exit_code == 0, result.output
    assert "kept at .nrw/backups/" in result.output
    assert status(expt)["feed"]["kind"] == "directory"
    from nr_workbench.project.config import load_config

    assert load_config(expt).raw["experiment"]["feed"]["poll_seconds"] == 20


@pytest.mark.parametrize(
    "args,message",
    [
        (["--location", "/x", "--default-location"], "contradict"),
        (["--write"], "Nothing to write"),
        (["--poll", "1"], "from 5 to 600"),
        (["--location", "relative/path"], "not a full path"),
    ],
)
def test_settings_refuses_with_the_reason(expt: Path, args, message: str) -> None:
    result = CliRunner().invoke(
        main, ["experiment", "settings", *args, "--root", str(expt)]
    )

    assert result.exit_code != 0
    assert message in result.output


def test_settings_asks_before_changing_a_catalogued_experiments_ipts(
    expt: Path,
) -> None:
    from nr_workbench.project.config import load_config

    nrw("experiment", "assign", "234277", "--sample", "Sample6", "--root", str(expt))
    args = [
        "experiment",
        "settings",
        "--ipts",
        "IPTS-2",
        "--write",
        "--root",
        str(expt),
    ]

    asked = CliRunner().invoke(main, args)
    assert asked.exit_code != 0 and "--confirm-ipts-change" in asked.output
    assert load_config(expt).ipts == "IPTS-00001"

    done = CliRunner().invoke(main, [*args, "--confirm-ipts-change"])
    assert done.exit_code == 0, done.output
    assert load_config(expt).ipts == "IPTS-2"


def test_settings_check_says_what_the_folder_holds(expt: Path) -> None:
    result = nrw("experiment", "settings", "--check", "--root", str(expt))

    assert result.exit_code == 0, result.output
    assert "3 run(s), 234277 to 234283" in result.output
