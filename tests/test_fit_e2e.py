"""End-to-end tests: run a real fit, then interrogate its provenance.

These exercise the seam that matters -- a hand-written refl1d script goes in,
an immutable, queryable record comes out -- against real refl1d and bumps
rather than mocks. The fits are tiny (two free parameters, a handful of steps)
so the whole file runs in seconds.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nr_workbench.cli import main

pytestmark = pytest.mark.integration

refl1d = pytest.importorskip(
    "refl1d", reason="refl1d is required for end-to-end fit tests"
)

#: A script in the shape the real experiments-2025 models use: the data path is
#: assembled from ``dirname(__file__)`` and a relative hop, which is precisely
#: the pattern that defeats guessing inputs from string literals.
SCRIPT = """\
import os

import numpy as np
from refl1d.names import SLD, Experiment, FitProblem, QProbe

DATA = os.path.join(os.path.dirname(__file__), "..", "data", "steady", "{filename}")

q, r, dr, dq = np.loadtxt(DATA).T
probe = QProbe(q, dq / 2.355, data=(r, dr))

D2O = SLD("D2O", rho=6.19)
Film = SLD("Film", rho=4.0)
Si = SLD("Si", rho=2.07)

sample = D2O(0, 5) | Film(120, 5) | Si
sample["Film"].thickness.range(50, 200)
sample["Film"].material.rho.range(2.0, 6.0)

problem = FitProblem(Experiment(sample=sample, probe=probe))
"""


def synthetic_reflectivity(n: int = 60) -> str:
    """Generate a small four-column ``Q R dR dQ`` dataset.

    Not physically meaningful -- a smooth decay with noise is enough to give
    the optimizer something to descend. Keeping the fixture synthetic means the
    tests need no data from another repository.
    """
    import numpy as np

    rng = np.random.default_rng(20260805)
    q = np.linspace(0.01, 0.2, n)
    r = 1e-3 * (0.01 / q) ** 4 * (1.0 + 0.25 * np.cos(q * 240.0))
    r = np.clip(r, 1e-9, None)
    dr = 0.05 * r
    r = r * (1.0 + 0.01 * rng.standard_normal(n))
    dq = 0.02 * q
    return "\n".join(
        f"{qi:.6e} {ri:.6e} {dri:.6e} {dqi:.6e}"
        for qi, ri, dri, dqi in zip(q, r, dr, dq, strict=True)
    )


@pytest.fixture
def fitted_project(tmp_path: Path) -> Path:
    """A project with data and a script, ready to fit."""
    runner = CliRunner()
    root = tmp_path / "proj"
    result = runner.invoke(main, ["init", str(root), "--sample", "S1"])
    assert result.exit_code == 0, result.output

    data_dir = root / "samples" / "S1" / "data" / "steady"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "REFL_100001_combined_data_auto.txt").write_text(
        synthetic_reflectivity(), encoding="utf-8"
    )

    models = root / "samples" / "S1" / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "film.py").write_text(
        SCRIPT.format(filename="REFL_100001_combined_data_auto.txt"), encoding="utf-8"
    )
    return root


def run_cli(root: Path, monkeypatch: pytest.MonkeyPatch, *args: str):
    """Invoke the CLI with the project as the working directory."""
    monkeypatch.chdir(root)
    return CliRunner().invoke(main, list(args))


def do_fit(root: Path, monkeypatch: pytest.MonkeyPatch, *extra: str):
    """Run the standard tiny fit."""
    return run_cli(
        root,
        monkeypatch,
        "fit",
        "run",
        "samples/S1/models/film.py",
        "--method",
        "amoeba",
        "--steps",
        "12",
        "--seed",
        "1",
        # One CPU: a pool on every core starves the browser tests on the
        # other xdist workers, as every other module's fits do.
        "--parallel",
        "1",
        *extra,
    )


def fit_dirs(root: Path) -> list[Path]:
    """Return the fit directories, ignoring the scaffold's .gitkeep."""
    results = root / "samples" / "S1" / "results"
    if not results.is_dir():
        return []
    return sorted(p for p in results.iterdir() if p.is_dir())


def only_fit_dir(root: Path) -> Path:
    """Return the single fit directory in the project."""
    directories = fit_dirs(root)
    assert len(directories) == 1, f"expected one fit, found {directories}"
    return directories[0]


# --------------------------------------------------------------------------
# Recording a fit
# --------------------------------------------------------------------------


@pytest.mark.fit
def test_fit_run_produces_a_complete_record(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = do_fit(fitted_project, monkeypatch)
    assert result.exit_code == 0, result.output

    fit_dir = only_fit_dir(fitted_project)
    for expected in ("manifest.json", "model.py", "inputs.json", "NOTES.md"):
        assert (fit_dir / expected).is_file(), f"missing {expected}"
    assert (fit_dir / "env" / "versions.json").is_file()
    assert (fit_dir / "env" / "requirements.txt").is_file()
    assert list((fit_dir / "fit").glob("*")), "bumps wrote no output"


@pytest.mark.fit
def test_fit_run_records_the_data_file_the_script_actually_opened(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The script builds its path from dirname(__file__), so a literal-scan
    would miss the data file entirely. Observing the run must not."""
    assert do_fit(fitted_project, monkeypatch).exit_code == 0

    inputs = json.loads((only_fit_dir(fitted_project) / "inputs.json").read_text())
    roles = {entry["role"] for entry in inputs["inputs"]}
    paths = {entry["path"] for entry in inputs["inputs"]}

    assert "script" in roles
    assert "data:steady:REFL_100001_combined_data_auto" in roles
    assert "samples/S1/data/steady/REFL_100001_combined_data_auto.txt" in paths


@pytest.mark.fit
def test_fit_run_records_no_absolute_paths(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absolute paths are what made a shared script unusable on another machine."""
    assert do_fit(fitted_project, monkeypatch).exit_code == 0

    inputs = json.loads((only_fit_dir(fitted_project) / "inputs.json").read_text())

    assert all(not entry["path"].startswith("/") for entry in inputs["inputs"])


@pytest.mark.fit
def test_fit_run_freezes_the_script_as_executed(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing the script afterwards must not change what the record says ran."""
    assert do_fit(fitted_project, monkeypatch).exit_code == 0
    frozen = (only_fit_dir(fitted_project) / "model.py").read_text(encoding="utf-8")

    script = fitted_project / "samples" / "S1" / "models" / "film.py"
    script.write_text("# rewritten\n", encoding="utf-8")

    assert (only_fit_dir(fitted_project) / "model.py").read_text(
        encoding="utf-8"
    ) == frozen
    assert "FitProblem" in frozen


@pytest.mark.fit
def test_fit_run_appends_one_index_entry(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert do_fit(fitted_project, monkeypatch).exit_code == 0

    lines = (fitted_project / ".nrw" / "index.jsonl").read_text().strip().splitlines()

    assert len(lines) == 1
    assert json.loads(lines[0])["event"] == "fit"
    assert json.loads(lines[0])["status"] == "ok"


@pytest.mark.fit
def test_identical_rerun_is_refused_by_default(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusing a no-op re-run is what prevents twelve identical result dirs."""
    assert do_fit(fitted_project, monkeypatch).exit_code == 0

    second = do_fit(fitted_project, monkeypatch)

    assert second.exit_code != 0
    assert "identical run already exists" in second.output
    assert len(fit_dirs(fitted_project)) == 1


@pytest.mark.fit
def test_identical_rerun_is_allowed_with_force_and_marked_a_replicate(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert do_fit(fitted_project, monkeypatch).exit_code == 0
    first = only_fit_dir(fitted_project).name

    assert do_fit(fitted_project, monkeypatch, "--force").exit_code == 0

    directories = fit_dirs(fitted_project)
    assert len(directories) == 2
    replicate = next(d for d in directories if d.name != first)
    manifest = json.loads((replicate / "manifest.json").read_text())
    assert manifest["params"]["replicate_of"] == first


@pytest.mark.fit
def test_changing_a_setting_makes_a_distinct_run(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert do_fit(fitted_project, monkeypatch).exit_code == 0

    other = run_cli(
        fitted_project,
        monkeypatch,
        "fit",
        "run",
        "samples/S1/models/film.py",
        "--method",
        "amoeba",
        "--steps",
        "13",
        "--seed",
        "1",
        # One CPU: a pool on every core starves the browser tests on the
        # other xdist workers, as every other module's fits do.
        "--parallel",
        "1",
    )

    assert other.exit_code == 0, other.output
    assert len(fit_dirs(fitted_project)) == 2


def test_dry_run_writes_nothing(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = do_fit(fitted_project, monkeypatch, "--dry-run")

    assert result.exit_code == 0
    assert "Would create" in result.output
    assert fit_dirs(fitted_project) == []
    assert not (fitted_project / ".nrw" / "index.jsonl").exists()


def test_a_failing_script_is_still_recorded(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Knowing a model was tried and failed is worth as much as knowing one worked."""
    broken = fitted_project / "samples" / "S1" / "models" / "broken.py"
    broken.write_text("x = 1\n", encoding="utf-8")

    result = run_cli(
        fitted_project, monkeypatch, "fit", "run", "samples/S1/models/broken.py"
    )

    assert result.exit_code != 0
    assert "module-level `problem`" in result.output


# --------------------------------------------------------------------------
# What the run leaves in NOTES.md
#
# An unattended session reached morning with every result directory holding the
# untouched template. The reason is knowable at launch and nowhere else after;
# these fix the two ends of that -- capture what was said, and ask for the rest
# at the one moment somebody is looking.
# --------------------------------------------------------------------------


@pytest.mark.fit
def test_a_launch_reason_is_filed_under_why_this_run(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nr_workbench.notes import is_blank

    result = do_fit(fitted_project, monkeypatch, "--note", "testing a thicker oxide")
    assert result.exit_code == 0, result.output

    text = (only_fit_dir(fitted_project) / "NOTES.md").read_text(encoding="utf-8")
    why = text.index("## Why this run")
    showed = text.index("## What it showed")

    assert why < text.index("testing a thicker oxide") < showed
    assert not is_blank(text), "a reason that was stated must not read as unwritten"


@pytest.mark.fit
def test_a_documented_fit_stops_being_counted_as_undocumented(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`nrw ls` and `report --check` are the surfaces that nag, and they were
    nagging about runs whose reason had in fact been given."""
    from nr_workbench.commands.report import undocumented_fits
    from nr_workbench.project.layout import ProjectLayout

    assert (
        do_fit(fitted_project, monkeypatch, "--note", "the oxide again").exit_code == 0
    )

    layout = ProjectLayout.discover(fitted_project)
    assert undocumented_fits(layout, "S1") == []


@pytest.mark.fit
def test_a_run_with_no_reason_is_still_reported_as_undocumented(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signal has to survive. Nothing here may invent the human half."""
    from nr_workbench.commands.report import undocumented_fits
    from nr_workbench.project.layout import ProjectLayout

    assert do_fit(fitted_project, monkeypatch).exit_code == 0

    layout = ProjectLayout.discover(fitted_project)
    assert len(undocumented_fits(layout, "S1")) == 1


@pytest.mark.fit
def test_the_summary_names_the_note_command_and_the_sections_still_missing(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The output right after a fit is the last moment anyone is looking, and
    it used to name only `whence` and `promote`."""
    result = do_fit(fitted_project, monkeypatch, "--note", "testing a thicker oxide")

    assert "nrw note" in result.output
    assert "--showed" in result.output and "--caveat" in result.output
    assert "--why" not in result.output, "--note already answered that one"


@pytest.mark.fit
def test_the_summary_asks_for_all_three_when_no_reason_was_given(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = do_fit(fitted_project, monkeypatch)

    assert "nrw note" in result.output
    assert "--why" in result.output


@pytest.mark.fit
def test_an_unattended_session_is_not_pointed_at_a_command_it_cannot_run(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`nrw promote` is refused by the agent hook. Offering it as the next step
    spends a turn of a finite budget on a command that cannot succeed."""
    monkeypatch.setenv("NRW_AGENT", "1")

    result = do_fit(fitted_project, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "nrw promote" not in result.output
    assert "nrw note" in result.output


# --------------------------------------------------------------------------
# The query surface
# --------------------------------------------------------------------------


@pytest.mark.fit
def test_whence_traces_an_artifact_back_to_its_fit(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert do_fit(fitted_project, monkeypatch).exit_code == 0
    fit_dir = only_fit_dir(fitted_project)
    artifact = next(p for p in (fit_dir / "fit").iterdir() if p.is_file())

    result = run_cli(fitted_project, monkeypatch, "whence", str(artifact), "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["fit_id"] == fit_dir.name
    assert payload["freshness"] == "fresh"
    assert payload["provenance"]["model"] == "film"


@pytest.mark.fit
def test_whence_on_a_data_file_lists_its_consumers(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 'this file changed, what do I redo?' direction."""
    assert do_fit(fitted_project, monkeypatch).exit_code == 0

    result = run_cli(
        fitted_project,
        monkeypatch,
        "whence",
        "samples/S1/data/steady/REFL_100001_combined_data_auto.txt",
        "--json",
    )

    payload = json.loads(result.output)
    assert payload["resolution"] == "recorded-input"
    assert len(payload["consumed_by"]) == 1


@pytest.mark.fit
def test_whence_reports_stale_after_an_input_changes(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loudest thing the tool says: this result no longer matches its data."""
    assert do_fit(fitted_project, monkeypatch).exit_code == 0
    fit_id = only_fit_dir(fitted_project).name

    data = (
        fitted_project
        / "samples"
        / "S1"
        / "data"
        / "steady"
        / "REFL_100001_combined_data_auto.txt"
    )
    data.write_text(data.read_text() + "\n# re-reduced\n", encoding="utf-8")

    result = run_cli(fitted_project, monkeypatch, "whence", fit_id, "--json")

    payload = json.loads(result.output)
    assert payload["freshness"] == "stale"
    assert any(i["state"] == "stale" for i in payload["inputs"])


@pytest.mark.fit
def test_whence_reports_broken_when_an_input_is_deleted(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert do_fit(fitted_project, monkeypatch).exit_code == 0
    fit_id = only_fit_dir(fitted_project).name
    (
        fitted_project
        / "samples"
        / "S1"
        / "data"
        / "steady"
        / "REFL_100001_combined_data_auto.txt"
    ).unlink()

    result = run_cli(fitted_project, monkeypatch, "whence", fit_id, "--json")

    assert json.loads(result.output)["freshness"] == "broken"


@pytest.mark.fit
def test_whence_finds_a_figure_copied_out_of_the_project(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The stamp is what survives a figure being emailed to a collaborator."""
    from nr_workbench.provenance.stamp import stamp_file

    assert do_fit(fitted_project, monkeypatch).exit_code == 0
    fit_id = only_fit_dir(fitted_project).name

    figure = only_fit_dir(fitted_project) / "figures" / "sld.svg"
    figure.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    stamp_file(figure, fit_id)

    elsewhere = tmp_path / "for-the-paper" / "figure3.svg"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_bytes(figure.read_bytes())

    result = run_cli(fitted_project, monkeypatch, "whence", str(elsewhere), "--json")

    assert json.loads(result.output)["fit_id"] == fit_id


def test_whence_exits_nonzero_for_an_untraceable_file(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stray = fitted_project / "stray.txt"
    stray.write_text("nothing to do with anything\n", encoding="utf-8")

    result = run_cli(fitted_project, monkeypatch, "whence", str(stray))

    assert result.exit_code == 1


@pytest.mark.fit
def test_ls_shows_the_fit_and_its_freshness(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert do_fit(fitted_project, monkeypatch).exit_code == 0

    result = run_cli(fitted_project, monkeypatch, "ls")

    assert result.exit_code == 0
    assert "film" in result.output
    assert "fresh" in result.output


@pytest.mark.fit
def test_promote_requires_a_reason_and_records_who(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert do_fit(fitted_project, monkeypatch).exit_code == 0
    fit_id = only_fit_dir(fitted_project).name

    result = run_cli(
        fitted_project,
        monkeypatch,
        "promote",
        fit_id,
        "--as",
        "final",
        "--reason",
        "converged; SLD plausible",
    )

    assert result.exit_code == 0, result.output
    entries = [
        json.loads(line)
        for line in (fitted_project / ".nrw" / "index.jsonl")
        .read_text()
        .strip()
        .splitlines()
    ]
    promotion = next(e for e in entries if e["event"] == "promote")
    assert promotion["reason"] == "converged; SLD plausible"
    assert promotion["who"]


@pytest.mark.fit
def test_promote_refuses_a_stale_fit(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blessing a result whose data has moved on is the mistake worth blocking."""
    assert do_fit(fitted_project, monkeypatch).exit_code == 0
    fit_id = only_fit_dir(fitted_project).name
    data = (
        fitted_project
        / "samples"
        / "S1"
        / "data"
        / "steady"
        / "REFL_100001_combined_data_auto.txt"
    )
    data.write_text(data.read_text() + "\n# changed\n", encoding="utf-8")

    result = run_cli(
        fitted_project, monkeypatch, "promote", fit_id, "--as", "final", "--reason", "x"
    )

    assert result.exit_code != 0
    assert "STALE" in result.output


@pytest.mark.fit
def test_promote_supersedes_and_keeps_the_earlier_decision(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert do_fit(fitted_project, monkeypatch).exit_code == 0
    first = only_fit_dir(fitted_project).name
    run_cli(fitted_project, monkeypatch, "promote", first, "--reason", "first attempt")

    assert do_fit(fitted_project, monkeypatch, "--force").exit_code == 0
    second = next(d.name for d in fit_dirs(fitted_project) if d.name != first)
    result = run_cli(
        fitted_project, monkeypatch, "promote", second, "--reason", "better"
    )

    assert result.exit_code == 0, result.output
    entries = [
        json.loads(line)
        for line in (fitted_project / ".nrw" / "index.jsonl")
        .read_text()
        .strip()
        .splitlines()
    ]
    assert any(e["event"] == "supersede" and e["fit_id"] == first for e in entries)
    assert len([e for e in entries if e["event"] == "promote"]) == 2


@pytest.mark.fit
def test_check_passes_on_a_clean_project(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert do_fit(fitted_project, monkeypatch).exit_code == 0

    result = run_cli(fitted_project, monkeypatch, "check")

    assert result.exit_code == 0, result.output
    assert "no problems" in result.output


@pytest.mark.fit
def test_check_fails_when_an_input_has_changed(
    fitted_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This is the CI signal that a committed result went stale."""
    assert do_fit(fitted_project, monkeypatch).exit_code == 0
    data = (
        fitted_project
        / "samples"
        / "S1"
        / "data"
        / "steady"
        / "REFL_100001_combined_data_auto.txt"
    )
    data.write_text(data.read_text() + "\n# changed\n", encoding="utf-8")

    result = run_cli(fitted_project, monkeypatch, "check", "--json")

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert any(p["kind"] == "stale-input" for p in payload["problems"])
