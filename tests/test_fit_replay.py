"""The fit replay: how the default test run fits nothing, and stays that way.

Tests that only need a fit's record get the optimizer's answer from a
recording (``tests/fit_replay.py``). These tests pin the replay itself: a fit
no recording answers is refused, never run; a recording is replayed without
the optimizer; and a recording is found again only for the same fit.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from . import fit_replay

refl1d = pytest.importorskip("refl1d", reason="the replay answers refl1d problems")


def problem(thickness: float = 100.0, low: float = 50.0):
    """A two-parameter film on silicon, with made-up data: nothing is fitted."""
    import numpy as np
    from refl1d.names import SLD, Experiment, FitProblem, QProbe

    q = np.linspace(0.01, 0.1, 20)
    probe = QProbe(q, q * 0.02, R=np.exp(-50 * q), dR=np.exp(-50 * q) * 0.05)
    film = SLD("Film", rho=4.0)
    sample = SLD("Si", rho=2.07)(0, 3) | film(thickness, 5) | SLD("air", rho=0)
    sample["Film"].thickness.range(low, 200)
    sample["Film"].material.rho.range(1, 6)
    return FitProblem(Experiment(probe=probe, sample=sample))


@pytest.fixture
def recordings(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(fit_replay, "RECORDINGS", tmp_path / "fits")
    return tmp_path / "fits"


def test_a_fit_no_recording_answers_is_refused_with_the_command_that_records_it(
    recordings: Path,
) -> None:
    replay = fit_replay.replayer("tests/test_x.py::test_y")

    with pytest.raises(fit_replay.NoRecordedFit) as refused:
        replay(problem(), method="amoeba", steps=3)

    assert "pytest --record-fits tests/test_x.py::test_y" in str(refused.value)


def test_a_recorded_answer_is_replayed_without_the_optimizer(
    recordings: Path,
) -> None:
    fitted = problem()
    key = fit_replay.fingerprint(fitted, "amoeba", {"steps": 3})
    recordings.mkdir()
    (recordings / f"{key}.json").write_text(
        json.dumps({"x": [120.0, 3.5], "fun": 1.25, "dx": None}), encoding="utf-8"
    )

    result = fit_replay.replayer("t")(fitted, method="amoeba", steps=3)

    # Left at the answer, as bumps leaves a problem: nrw records it from there.
    assert list(fitted.getp()) == [120.0, 3.5]
    assert (list(result.x), result.fun, result.state) == ([120.0, 3.5], 1.25, None)


@pytest.mark.parametrize(
    ("other", "options"),
    [
        (lambda: problem(thickness=110.0), {"steps": 3}),
        (lambda: problem(low=40.0), {"steps": 3}),
        (lambda: problem(), {"steps": 4}),
        (lambda: problem(), {"steps": 3, "seed": 1}),
    ],
    ids=["another-start", "other-bounds", "more-steps", "a-seed"],
)
def test_a_fit_changed_in_what_it_finds_is_another_fit(other, options) -> None:
    same = fit_replay.fingerprint(problem(), "amoeba", {"steps": 3})

    assert fit_replay.fingerprint(other(), "amoeba", options) != same


def test_how_a_fit_runs_is_not_what_it_finds() -> None:
    """One CPU or all, quiet or not: the same fit, so the same recording."""
    same = fit_replay.fingerprint(problem(), "amoeba", {"steps": 3})

    assert (
        fit_replay.fingerprint(
            problem(), "amoeba", {"steps": 3, "parallel": 1, "verbose": 0}
        )
        == same
    )
    assert fit_replay.fingerprint(problem(), "dream", {"steps": 3}) != same


def test_a_samplers_answer_is_not_recorded_for_replay(recordings: Path) -> None:
    """DREAM's uncertainty comes from its draws, which a recording does not keep."""

    def sampler(problem, **options):
        return SimpleNamespace(
            x=[1.0], fun=0.0, dx=None, state=SimpleNamespace(show=print)
        )

    with pytest.raises(fit_replay.NoRecordedFit, match="mark this test `fit`"):
        fit_replay.recorder(sampler)(problem(), method="dream", samples=10)
    assert not recordings.exists()


def test_every_recording_has_one_answer_per_parameter() -> None:
    """Each was made by `pytest --record-fits`, from a fit of the suite's own."""
    names = sorted(p.name for p in fit_replay.RECORDINGS.glob("*.json"))

    assert names, "tests/data/fits/ holds no recordings"
    for path in fit_replay.RECORDINGS.glob("*.json"):
        recorded = json.loads(path.read_text(encoding="utf-8"))
        assert len(recorded["x"]) == len(recorded["labels"]), path.name
