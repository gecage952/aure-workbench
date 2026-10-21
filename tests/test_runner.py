"""Tests for the fit runner: input observation and the bumps export path."""

from __future__ import annotations

from pathlib import Path

import pytest

from nr_workbench.fitting.runner import (
    FitError,
    collect_artifacts,
    load_problem,
    track_opened_files,
)

pytestmark = pytest.mark.integration

pytest.importorskip("refl1d", reason="refl1d is required for runner tests")

TRIVIAL = """\
import os

import numpy as np
from refl1d.names import SLD, Experiment, FitProblem, QProbe

DATA = os.path.join(os.path.dirname(__file__), "..", "data", "steady", "d.txt")
q, r, dr, dq = np.loadtxt(DATA).T
probe = QProbe(q, dq, data=(r, dr))

Air = SLD("Air", rho=0.0)
Film = SLD("Film", rho=4.0)
Si = SLD("Si", rho=2.07)
sample = Air(0, 5) | Film(100, 5) | Si
sample["Film"].thickness.range(50, 200)

problem = FitProblem(Experiment(sample=sample, probe=probe))
"""


@pytest.fixture
def script_project(tmp_path: Path) -> Path:
    """A minimal tree with a script that reads data via a relative hop."""
    (tmp_path / "data" / "steady").mkdir(parents=True)
    (tmp_path / "data" / "steady" / "d.txt").write_text(
        "\n".join(f"{0.01 * i + 0.01:.4f} 1e-3 1e-4 1e-4" for i in range(1, 25)),
        encoding="utf-8",
    )
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "m.py").write_text(TRIVIAL, encoding="utf-8")
    return tmp_path


# --------------------------------------------------------------------------
# Observing inputs
# --------------------------------------------------------------------------


def test_track_opened_files_records_a_read(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")

    with track_opened_files() as opened:
        target.read_text(encoding="utf-8")

    assert any(str(target) in path for path in opened)


def test_track_opened_files_stops_collecting_after_the_block(tmp_path: Path) -> None:
    """The hook is permanent; the collector must not keep growing."""
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")

    with track_opened_files() as opened:
        pass
    target.read_text(encoding="utf-8")

    assert not any(str(target) in path for path in opened)


def test_track_opened_files_nests(tmp_path: Path) -> None:
    """A nested block must not clobber the outer collector."""
    inner_file = tmp_path / "inner.txt"
    inner_file.write_text("x", encoding="utf-8")
    outer_file = tmp_path / "outer.txt"
    outer_file.write_text("x", encoding="utf-8")

    with track_opened_files() as outer:
        with track_opened_files() as inner:
            inner_file.read_text(encoding="utf-8")
        outer_file.read_text(encoding="utf-8")

    assert any("inner.txt" in p for p in inner)
    assert any("outer.txt" in p for p in outer)
    assert not any("inner.txt" in p for p in outer)


def test_load_problem_observes_a_path_built_at_runtime(script_project: Path) -> None:
    """The whole point: the data path never appears whole in the source."""
    loaded = load_problem(script_project / "models" / "m.py", root=script_project)

    names = [p.name for p in loaded.opened]
    assert "d.txt" in names


def test_load_problem_excludes_the_script_and_the_interpreter(
    script_project: Path,
) -> None:
    """Only project data files count as inputs; the script is recorded apart."""
    loaded = load_problem(script_project / "models" / "m.py", root=script_project)

    assert all(p.suffix != ".py" for p in loaded.opened)
    assert all(str(p).startswith(str(script_project)) for p in loaded.opened)


def test_load_problem_rejects_a_script_without_a_problem(tmp_path: Path) -> None:
    script = tmp_path / "no_problem.py"
    script.write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(FitError, match="module-level `problem`"):
        load_problem(script)


def test_load_problem_reports_a_raising_script(tmp_path: Path) -> None:
    script = tmp_path / "boom.py"
    script.write_text("raise ValueError('nope')\n", encoding="utf-8")

    with pytest.raises(FitError, match="raised while loading"):
        load_problem(script)


def test_load_problem_reports_a_missing_script(tmp_path: Path) -> None:
    with pytest.raises(FitError, match="not found"):
        load_problem(tmp_path / "absent.py")


# --------------------------------------------------------------------------
# The bumps export workaround
# --------------------------------------------------------------------------


@pytest.mark.fit
def test_our_export_path_writes_the_uncertainty_block(
    script_project: Path, tmp_path: Path
) -> None:
    """Calling export_fit ourselves must produce the MCMC outputs.

    This is the payoff of not passing ``export=`` to ``bumps.fitters.fit``:
    ``-err.json`` carries the parameter uncertainties and ``-chain.mc.gz`` the
    DREAM chain, and both are what downstream SLD confidence bands need.
    """
    from nr_workbench.fitting.runner import run_fit

    loaded = load_problem(script_project / "models" / "m.py", root=script_project)
    out = tmp_path / "out"

    outcome = run_fit(
        loaded.problem, out, method="dream", samples=600, burn=30, quiet=True
    )

    names = [p.name for p in out.iterdir()]
    assert outcome.export_ok, outcome.export_error
    assert any(n.endswith("-err.json") for n in names), names
    assert any(n.endswith("-chain.mc.gz") for n in names), names
    assert "uncertainties" in outcome.artifacts
    assert "chain" in outcome.artifacts


@pytest.mark.fit
@pytest.mark.slow
def test_bumps_export_kwarg_still_drops_the_uncertainty_block(
    script_project: Path, tmp_path: Path
) -> None:
    """Pin the bug the workaround exists for, so we notice when it is fixed.

    In bumps 1.0.x, ``fit(export=...)`` forwards a bare ``MCMCDraw`` where
    ``export_fit`` expects an ``OptimizeResult``, so the uncertainty block is
    silently skipped. If this test starts failing, bumps has been fixed and
    ``_export`` can be simplified -- which is worth knowing rather than
    carrying the workaround forever as folklore.
    """
    from bumps.fitters import fit as bumps_fit

    loaded = load_problem(script_project / "models" / "m.py", root=script_project)
    loaded.problem.name = "problem"
    out = tmp_path / "buggy"
    out.mkdir()

    bumps_fit(
        loaded.problem, method="dream", samples=600, burn=30, verbose=0, export=str(out)
    )

    names = [p.name for p in out.iterdir()]
    assert not any(n.endswith("-err.json") for n in names), (
        "bumps fit(export=...) now writes the uncertainty block -- the "
        "workaround in fitting/runner.py::_export can be revisited."
    )


def test_collect_artifacts_names_the_interesting_files(tmp_path: Path) -> None:
    out = tmp_path / "fit"
    out.mkdir()
    for name in (
        "problem.par",
        "problem-err.json",
        "problem-1-refl.dat",
        "problem-chain.mc.gz",
    ):
        (out / name).write_text("x", encoding="utf-8")

    artifacts = collect_artifacts(out)

    assert artifacts["parameters"] == "fit/problem.par"
    assert artifacts["uncertainties"] == "fit/problem-err.json"
    assert artifacts["reflectivity"] == "fit/problem-1-refl.dat"
    assert artifacts["chain"] == "fit/problem-chain.mc.gz"


def test_collect_artifacts_tolerates_a_missing_directory(tmp_path: Path) -> None:
    assert collect_artifacts(tmp_path / "nope") == {}


def test_parallel_defaults_to_all_cores() -> None:
    """bumps defaults to one CPU; a fit that could use twenty should."""
    import inspect

    from nr_workbench.fitting.runner import run_fit

    assert inspect.signature(run_fit).parameters["parallel"].default == 0


def test_parallel_is_part_of_the_recorded_settings() -> None:
    """It changes how the fit ran, so it belongs in the record."""
    from nr_workbench.fitting.runner import FIT_SETTING_KEYS

    assert "parallel" in FIT_SETTING_KEYS


def test_a_worker_pool_failure_falls_back_to_serial(monkeypatch) -> None:
    """Losing an hour of fitting to multiprocessing is worse than running slow.

    bumps starts a Manager and spawns workers that re-import `__main__`, which
    some environments cannot do. The model is fine; only the pool failed.
    """
    from nr_workbench.fitting import runner

    calls: list[int] = []

    def fake_fit(problem, **kwargs):
        calls.append(kwargs["parallel"])
        if kwargs["parallel"] != 1:
            raise EOFError("multiprocessing manager died")
        return object()

    monkeypatch.setattr("bumps.fitters.fit", fake_fit)
    monkeypatch.setattr(runner, "_export", lambda *a, **k: None)

    class Problem:
        name = "p"

        def chisq(self):
            return 1.0

        models = []

        def getp(self):
            return []

    runner.run_fit(Problem(), Path("/tmp/nowhere"), method="dream", parallel=0)

    assert calls == [0, 1], "it retried on a single CPU"


def test_a_model_failure_is_not_retried(monkeypatch) -> None:
    """Only pool failures fall back. A broken model should fail once, fast."""
    from nr_workbench.fitting import runner

    calls: list[int] = []

    def fake_fit(problem, **kwargs):
        calls.append(kwargs["parallel"])
        raise ValueError("the model is nonsense")

    monkeypatch.setattr("bumps.fitters.fit", fake_fit)

    class Problem:
        name = "p"

    with pytest.raises(runner.FitError, match="nonsense"):
        runner.run_fit(Problem(), Path("/tmp/nowhere"), method="dream", parallel=0)

    assert calls == [0], "no pointless retry"


def test_plots_are_off_by_default_and_the_data_survives() -> None:
    """bumps renders its PNGs *before* saving the chain.

    `export_fit` calls `problem.plot()` and `fit_state.show()` ahead of
    `fit_state.save()`, so a rendering failure -- which a 21-model
    co-refinement provokes -- destroys the chain and every uncertainty output
    of an hour-long DREAM run. Same shape as the `export=` trap.
    """
    import inspect

    from nr_workbench.fitting.runner import _plots_disabled, run_fit

    assert inspect.signature(run_fit).parameters["plots"].default is False

    class Problem:
        def plot(self, **kwargs):
            raise RuntimeError("too many figures")

    problem = Problem()
    with _plots_disabled(problem, enabled=False):
        assert problem.plot() is None, "plotting is a no-op inside the block"
    with pytest.raises(RuntimeError):
        problem.plot()  # and the real method is back on the way out


def test_the_parameter_statistics_are_kept_when_plots_are_off() -> None:
    """`-err.json` is written by the same call that draws the figures.

    `bumps.dream.views.plot_all` computes the variable statistics and writes
    `-err.json` before it imports matplotlib. Skipping the whole call would
    silently drop the parameter uncertainty table that the fit page and the
    trajectory band both read -- which is exactly what a first attempt did.
    """
    import bumps.dream.stats as stats

    from nr_workbench.fitting.runner import _stats_without_plots

    class State:
        def draw(self, portion=None):
            return "the-draw"

    written: dict[str, object] = {}
    original_save, original_stats = stats.save_vars, stats.var_stats
    try:
        stats.var_stats = lambda draw: {"drawn": draw}
        stats.save_vars = lambda s, path: written.update({"path": path, "stats": s})
        _stats_without_plots(State(), figfile="/tmp/base")
    finally:
        stats.save_vars, stats.var_stats = original_save, original_stats

    assert written["path"] == "/tmp/base-err.json"
    assert written["stats"] == {"drawn": "the-draw"}, "the real statistics, not a stub"


def test_convergence_is_read_from_the_warning_not_assumed() -> None:
    """DREAM reports non-convergence by warning and carrying on, so the only
    record of it is a line on stderr nothing reads. On the real Cu/THF corpus
    the non-converged fit had a *better* chi-squared than the answer, so losing
    this turns the caveat that disqualifies a result into the one fact nobody
    has.
    """
    from nr_workbench.fitting.runner import _read_convergence

    class Caught:
        def __init__(self, message: str) -> None:
            self.message = message

    assert _read_convergence([Caught("Did not converge!")], "dream") is False
    assert _read_convergence([Caught("something else")], "dream") is True


def test_an_optimiser_has_no_opinion_about_convergence() -> None:
    """`None` and `True` are different claims. amoeba does not test
    convergence, and recording that as converged would assert something the
    fitter never checked."""
    from nr_workbench.fitting.runner import _read_convergence

    assert _read_convergence([], "amoeba") is None


# --------------------------------------------------------------------------
# Quiet by default
#
# bumps' live progress on a DREAM run overflows a coding harness's output
# buffer, and the agent then pages the overflow file back in 60-200 lines at a
# time. Measured on one real session: 19 of 95 tool calls -- a fifth of a
# 60-turn budget -- were re-reading two fit logs it had caused to be written.
# --------------------------------------------------------------------------


def test_the_fitter_is_quiet_unless_asked() -> None:
    """`--verbose` opts back in; nothing else turns it on."""
    import inspect

    from nr_workbench.commands import fit as fit_cmd

    default = inspect.signature(fit_cmd.run_fit_command).parameters["verbose"].default
    assert default is False, "verbose must be opt-in"

    source = inspect.getsource(fit_cmd.run_fit_command)
    assert "quiet=not verbose or as_json" in source, (
        "quiet must follow from verbose, and JSON output must stay parseable"
    )


def test_the_log_is_written_whether_or_not_it_was_printed(tmp_path: Path) -> None:
    """Suppressing the live output must not cost the record.

    `nrw assess` reads per-model chi-squared out of `fit/<model>.out`, which is
    the `uneven-fit` finding -- the one that says an overall chi-squared is
    hiding a segment fitting far worse than the others.
    """
    from nr_workbench.fitting.assess import per_model_chisq

    fit = tmp_path / "fit"
    fit.mkdir()
    (fit / "m.out").write_text(
        "-- Model 0 a#0\n[chisq=1.5(2), nllf=10]\n"
        "-- Model 1 a#1\n[chisq=4.0(3), nllf=20]\n",
        encoding="utf-8",
    )

    assert per_model_chisq(tmp_path) == {"a#0": 1.5, "a#1": 4.0}
