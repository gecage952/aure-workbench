"""`nrw fit run --from`: a recorded fit run again from its own problem file.

The file is the one bumps exports with every fit, `fit/<model>.json`: the
model, its parameters at the fit's values, and its data. The default run
checks what the command asks of the fit without fitting; the test marked
`fit` runs one.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nr_workbench.cli import main
from nr_workbench.provenance.index import FitIndex

from .test_fit_e2e import synthetic_reflectivity

pytestmark = pytest.mark.integration

pytest.importorskip("refl1d", reason="refl1d writes the problem file run again")

#: The fit run again.
SOURCE = "20261001-120000Z-0123abcd"

#: The settings it ran with, as its index line records them.
DREAM = {"method": "dream", "samples": 1000, "burn": 50, "seed": 7, "parallel": 4}

#: The reduced file it read.
MEASURED = "samples/S1/data/steady/REFL_100001_combined_data_auto.txt"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    shown = CliRunner().invoke(main, ["init", str(root), "--sample", "S1"])
    assert shown.exit_code == 0, shown.output
    return root


def recorded(root: Path, settings: dict) -> Path:
    """A fit of a film that read :data:`MEASURED`, recorded with *settings*,
    its problem file as bumps exports one. Its problem file."""
    import numpy as np
    from bumps.serialize import serialize
    from refl1d.names import SLD, Experiment, FitProblem, QProbe

    measured = root / MEASURED
    measured.parent.mkdir(parents=True, exist_ok=True)
    measured.write_text(synthetic_reflectivity())
    q, r, dr, dq = np.loadtxt(measured).T
    d2o, film, si = SLD("D2O", rho=6.19), SLD("Film", rho=4.0), SLD("Si", rho=2.07)
    stack = d2o(0, 5) | film(120, 5) | si
    stack["Film"].thickness.range(50, 200)
    problem = FitProblem(
        Experiment(sample=stack, probe=QProbe(q, dq, R=r, dR=dr)), name="film"
    )
    directory = root / "samples" / "S1" / "results" / SOURCE
    (directory / "fit").mkdir(parents=True)
    (directory / "fit" / "film.json").write_text(json.dumps(serialize(problem)))
    (directory / "manifest.json").write_text("{}")
    data = {
        "role": "data:steady:REFL_100001_combined_data_auto",
        "path": MEASURED,
        "sha256": hashlib.sha256(measured.read_bytes()).hexdigest(),
        "bytes": measured.stat().st_size,
    }
    script = {"role": "script", "path": "samples/S1/models/film.py", "sha256": "0"}
    (directory / "inputs.json").write_text(json.dumps({"inputs": [script, data]}))
    FitIndex(root / ".nrw" / "index.jsonl").append(
        {
            "fit_id": SOURCE,
            "sample": "S1",
            "model": "film",
            "status": "ok",
            "chisq": problem.chisq(),
            "settings": settings,
        }
    )
    return directory / "fit" / "film.json"


def fit_run(root: Path, monkeypatch: pytest.MonkeyPatch, *args: str):
    monkeypatch.chdir(root)
    return CliRunner().invoke(main, ["fit", "run", *args])


def test_a_fit_runs_again_with_its_own_fitter_settings_and_seed(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    problem = recorded(project, DREAM)

    shown = fit_run(project, monkeypatch, "--from", SOURCE[:8], "--dry-run")

    assert shown.exit_code == 0, shown.output
    assert (
        f"settings  method dream (fit {SOURCE}), samples 1000 (fit {SOURCE}), "
        f"burn 50 (fit {SOURCE}), seed 7 (fit {SOURCE})"
    ) in shown.output
    assert "samples/S1/results/" in shown.output
    assert f"problem                  samples/S1/results/{SOURCE}/fit/film.json" in (
        shown.output
    )
    assert hashlib.sha256(problem.read_bytes()).hexdigest()[:12] in shown.output
    assert f"data:steady:REFL_100001_combined_data_auto {MEASURED}" in shown.output
    assert "  inputs    2 file(s)" in shown.output


def test_what_is_given_wins_and_another_fitter_takes_none_of_its_settings(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded(project, DREAM)

    fewer = fit_run(
        project, monkeypatch, "--from", SOURCE, "--samples", "50", "--dry-run"
    )
    other = fit_run(
        project, monkeypatch, "--from", SOURCE, "--method", "de", "--dry-run"
    )

    assert fewer.exit_code == 0 and other.exit_code == 0, fewer.output + other.output
    assert "samples 50" in fewer.output and f"burn 50 (fit {SOURCE})" in fewer.output
    assert f"samples 1000 (fit {SOURCE})" not in fewer.output
    assert "method    de" in other.output and f"(fit {SOURCE})" not in other.output


def test_a_script_and_from_together_or_neither_is_refused(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded(project, DREAM)
    script = project / "samples" / "S1" / "models" / "film.py"
    script.write_text("problem = None\n")

    both = fit_run(project, monkeypatch, str(script), "--from", SOURCE)
    neither = fit_run(project, monkeypatch)

    for shown in (both, neither):
        assert shown.exit_code != 0
        assert "Give a script to run, or a fit to run again with --from" in shown.output


def test_a_fit_that_left_no_problem_file_is_refused(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded(project, DREAM).unlink()

    shown = fit_run(project, monkeypatch, "--from", SOURCE)

    assert shown.exit_code != 0
    assert f"{SOURCE} left no problem file" in shown.output


@pytest.mark.fit
def test_a_fit_run_again_records_its_problem_file_and_the_fit_it_came_from(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    problem = recorded(project, {"method": "amoeba", "steps": 12, "seed": 1})

    shown = fit_run(project, monkeypatch, "--from", SOURCE, "--parallel", "1")

    assert shown.exit_code == 0, shown.output
    (entry,) = [
        e
        for e in FitIndex(project / ".nrw" / "index.jsonl").fits()
        if e["fit_id"] != SOURCE
    ]
    directory = project / "samples" / "S1" / "results" / entry["fit_id"]
    ran, measured = json.loads((directory / "inputs.json").read_text())["inputs"]
    assert (ran["role"], ran["path"]) == (
        "problem",
        f"samples/S1/results/{SOURCE}/fit/film.json",
    )
    assert ran["sha256"] == hashlib.sha256(problem.read_bytes()).hexdigest()
    assert measured["path"] == MEASURED
    diff = CliRunner().invoke(main, ["diff", SOURCE, entry["fit_id"], "--json"])
    assert json.loads(diff.output)["changed"]["data"] is False, diff.output
    assert entry["script_origin"] == "problem" and entry["model"] == "film"
    assert entry["settings"]["regenerated_from"] == SOURCE
    assert (entry["settings"]["method"], entry["settings"]["steps"]) == ("amoeba", 12)
    assert (directory / "fit" / "film.json").is_file()
    assert not (directory / "model.py").exists()


def test_a_recorded_problem_file_loads_at_its_chisq(project: Path) -> None:
    from nr_workbench.fitting.runner import load_problem_file

    problem = recorded(project, DREAM)

    loaded = load_problem_file(problem)

    recorded_chisq = FitIndex(project / ".nrw" / "index.jsonl").find(SOURCE)["chisq"]
    assert loaded.problem.chisq() == pytest.approx(recorded_chisq, rel=1e-12)


class _Touch:
    """A pickle that touches *path* as it is loaded."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __reduce__(self):
        return (Path.touch, (self.path,))


def _planted(problem: Path, marker: Path, how: str) -> None:
    """Add to *problem* a node that makes *marker* as bumps loads it: a class
    bumps calls with the file's arguments, or a pickle it loads."""
    import pickle

    from bumps.serialize import serialize_bytes

    document = json.loads(problem.read_text())
    node = (
        {"__class__": "os.mkdir", "path": str(marker)}
        if how == "class"
        else {
            "__class__": "Callable",
            "pickler": "dill",
            "pickle": serialize_bytes(pickle.dumps(_Touch(marker))),
        }
    )
    document["references"]["planted"] = node
    problem.write_text(json.dumps(document))


@pytest.mark.parametrize(
    ("how", "said"),
    [("class", "the class 'os.mkdir'"), ("pickle", "a pickled function")],
)
def test_a_problem_file_that_would_run_code_is_refused_before_it_loads(
    project: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, how: str, said: str
) -> None:
    marker = tmp_path / "ran"
    _planted(recorded(project, DREAM), marker, how)

    shown = fit_run(project, monkeypatch, "--from", SOURCE)

    assert shown.exit_code != 0
    assert f"holds {said}, so nrw does not load it" in shown.output
    assert not marker.exists()
