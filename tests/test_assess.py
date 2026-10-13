"""`nrw assess`: the checks that run without a language model.

AuRE's own offline evaluator is three chi-squared bands and reads no
parameters, no bounds and no posterior. These tests pin the things it cannot
see, because those are the reason not to wrap it.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from click.testing import CliRunner

from nr_workbench.cli import main
from nr_workbench.fitting.assess import (
    Assessment,
    Finding,
    as_markdown,
    bic,
    check,
    per_model_chisq,
    read_bounds,
    read_par,
    read_reflectivity,
)

from .test_lifecycle import write_partials


def write_fit(
    directory: Path,
    *,
    par: dict[str, float],
    bounds: dict[str, tuple[float, float] | None],
    err: dict[str, dict] | None = None,
    out: str | None = None,
) -> None:
    """Write the bumps outputs an assessment reads."""
    fit = directory / "fit"
    fit.mkdir(parents=True, exist_ok=True)
    (fit / "m.par").write_text(
        "".join(f"{name} {value!r}\n" for name, value in par.items()), encoding="utf-8"
    )
    (fit / "m.json").write_text(
        json.dumps(
            {
                "references": [
                    {
                        "name": name,
                        "fixed": limits is None,
                        "bounds": list(limits) if limits else None,
                    }
                    for name, limits in bounds.items()
                ]
            }
        ),
        encoding="utf-8",
    )
    if err is not None:
        (fit / "m-err.json").write_text(json.dumps(err), encoding="utf-8")
    if out is not None:
        (fit / "m.out").write_text(out, encoding="utf-8")


def manifest(chisq: float = 1.2, n_free: int = 2, points: int = 100) -> dict:
    """A manifest shaped like the real one."""
    return {
        "info": {
            "chisq": chisq,
            "n_free": n_free,
            "n_points": points - n_free,
            "models": [{"name": "a", "n_points": points}],
        },
        "provenance": {"fit_id": "20260807-163359Z-0103d9c7"},
    }


# --------------------------------------------------------------------------
# Reading what the fitter wrote
# --------------------------------------------------------------------------


def test_bounds_come_from_the_serialised_problem(tmp_path: Path) -> None:
    """The spec expresses ranges indirectly -- `pm: 0.3` around a value, an
    endpoint that becomes two parameters. The problem JSON has already
    resolved all of it into the names the fitter used."""
    write_fit(
        tmp_path,
        par={"a thickness": 50.0},
        bounds={"a thickness": (40.0, 80.0), "a rho fixed": None},
    )

    assert read_bounds(tmp_path) == {"a thickness": (40.0, 80.0)}


def test_par_names_are_split_on_the_last_field(tmp_path: Path) -> None:
    """Parameter names contain spaces: `run218386 probe intensity 1.0999`."""
    write_fit(tmp_path, par={"run218386 probe intensity": 1.1}, bounds={})

    assert read_par(tmp_path) == {"run218386 probe intensity": 1.1}


def test_per_model_chisq_is_read_from_the_run_log(tmp_path: Path) -> None:
    write_fit(
        tmp_path,
        par={},
        bounds={},
        out=(
            "-- Model 0 tnr#0\n[chisq=1.441(28), nllf=183.76]\n"
            "-- Model 1 tnr#1\n[chisq=4.002(31), nllf=190.10]\n"
            "[overall chisq=1.285(24)]\n"
        ),
    )

    assert per_model_chisq(tmp_path) == {"tnr#0": 1.441, "tnr#1": 4.002}


# --------------------------------------------------------------------------
# What the checks find
# --------------------------------------------------------------------------


def test_a_railed_parameter_is_reported_as_a_bound_not_a_measurement(
    tmp_path: Path,
) -> None:
    write_fit(
        tmp_path,
        par={"a thickness": 199.999},
        bounds={"a thickness": (100.0, 200.0)},
    )

    result = check(tmp_path, manifest())

    # `no-uncertainty` rides along because this fixture has no -err.json.
    assert [f.kind for f in result.findings] == ["bound", "no-uncertainty"]
    assert result.findings[0].edge == "upper"
    assert result.findings[0].limit == 200.0
    assert "not a measurement" in result.findings[0].message


def test_a_parameter_well_inside_its_range_is_not_flagged(tmp_path: Path) -> None:
    write_fit(
        tmp_path, par={"a thickness": 150.0}, bounds={"a thickness": (100.0, 200.0)}
    )

    assert [f.kind for f in check(tmp_path, manifest()).findings] == [
        "no-uncertainty"
    ], "the range is fine; only the missing posterior is worth saying"


def test_a_posterior_touching_its_bound_is_caught_when_the_point_is_not(
    tmp_path: Path,
) -> None:
    """The check AuRE cannot make: it tests the point estimate against the
    bound and never reads `uncertainties`, so a parameter whose p95 edge sits
    on its floor looks fine."""
    write_fit(
        tmp_path,
        par={"CuOx roughness": 5.11},
        bounds={"CuOx roughness": (5.0, 11.0)},
        err={
            "CuOx roughness": {"best": 5.11, "p68": [5.18, 6.83], "p95": [5.03, 8.38]}
        },
    )

    kinds = [f.kind for f in check(tmp_path, manifest()).findings]

    assert "posterior-bound" in kinds
    assert "bound" not in kinds, "the point estimate genuinely is not on the bound"


def test_an_unconstrained_posterior_is_reported(tmp_path: Path) -> None:
    """A p95 spanning most of its prior means the fit handed your range back."""
    write_fit(
        tmp_path,
        par={"Ti rho": 0.0},
        bounds={"Ti rho": (-2.0, 1.0)},
        err={"Ti rho": {"best": 0.0, "p68": [-1.0, 1.0], "p95": [-1.9, 0.95]}},
    )

    findings = check(tmp_path, manifest()).findings

    assert [f.kind for f in findings] == ["unconstrained"]
    assert "%" in findings[0].message


def test_a_best_fit_outside_its_own_interval_is_reported(tmp_path: Path) -> None:
    """Legitimate for a skewed posterior, and worth saying so the reader
    quotes the median rather than the point."""
    write_fit(
        tmp_path,
        par={"a rho": 5.0},
        bounds={"a rho": (0.0, 100.0)},
        err={"a rho": {"best": 5.0, "p68": [5.2, 6.8], "p95": [5.1, 8.0]}},
    )

    assert [f.kind for f in check(tmp_path, manifest()).findings] == ["skewed"]


def test_a_railed_parameter_is_reported_once_not_three_times(tmp_path: Path) -> None:
    """The posterior checks earn their place by catching what the point
    estimate misses. Repeating them buries the other parameters."""
    write_fit(
        tmp_path,
        par={"a thickness": 199.999},
        bounds={"a thickness": (100.0, 200.0)},
        err={
            "a thickness": {
                "best": 199.999,
                "p68": [199.98, 199.999],
                "p95": [199.9, 200.0],
            }
        },
    )

    assert [f.kind for f in check(tmp_path, manifest()).findings] == ["bound"]


def test_a_missing_uncertainty_is_absence_of_information_not_certainty(
    tmp_path: Path,
) -> None:
    write_fit(tmp_path, par={"a": 1.0}, bounds={"a": (0.0, 10.0)})

    findings = check(tmp_path, manifest()).findings

    assert [f.kind for f in findings] == ["no-uncertainty"]
    assert "not the same as small uncertainties" in findings[0].message


def test_an_uneven_co_refinement_is_reported(tmp_path: Path) -> None:
    """One overall chi-squared hides a model fitting three times worse."""
    write_fit(
        tmp_path,
        par={},
        bounds={},
        out="-- Model 0 a\n[chisq=1.0]\n-- Model 1 b\n[chisq=4.0]\n",
    )

    kinds = [f.kind for f in check(tmp_path, manifest()).findings]

    assert "uneven-fit" in kinds


# --------------------------------------------------------------------------
# Numbers
# --------------------------------------------------------------------------


def test_bic_matches_aures_formula() -> None:
    """Comparable across the two tools only if the form is the same."""
    assert bic(2.0, 100, 5) == pytest.approx(100 * math.log(2.0) + 5 * math.log(100))


def test_n_points_sums_the_models_rather_than_using_dof(tmp_path: Path) -> None:
    """`info.n_points` is bumps' `dof`, so BIC computed from it is wrong by
    k log n -- and for a 15-slice co-refinement that is not a rounding error.
    """
    write_fit(tmp_path, par={}, bounds={})
    payload = {
        "info": {
            "chisq": 2.0,
            "n_free": 8,
            "n_points": 3907,
            "models": [{"name": f"s{i}", "n_points": 261} for i in range(15)],
        },
        "provenance": {"fit_id": "f"},
    }

    result = check(tmp_path, payload)

    assert result.n_points == 3915
    assert result.bic == pytest.approx(bic(2.0, 3915, 8))


# --------------------------------------------------------------------------
# Rendering and the command
# --------------------------------------------------------------------------


def test_the_markdown_says_plainly_when_nothing_was_flagged() -> None:
    text = as_markdown(Assessment(fit_id="f", chisq=1.2, n_free=3, n_points=100))

    assert "No parameter sits on a bound" in text


def test_a_language_model_verdict_is_labelled_as_one() -> None:
    """It has not seen the data. A reader must be able to tell its opinion
    from the arithmetic above it."""
    text = as_markdown(
        Assessment(
            fit_id="f",
            chisq=1.2,
            judgement={"quality_assessment": "good", "issues": ["thin oxide"]},
        )
    )

    assert "thin oxide" in text
    assert "It has not seen the data" in text


def test_findings_render_with_their_severity() -> None:
    text = as_markdown(
        Assessment(
            fit_id="f",
            findings=[Finding(kind="bound", severity="warn", message="on the floor")],
        )
    )

    assert "**bound**" in text
    assert "on the floor" in text


@pytest.fixture
def fitted(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    """A project with one real fit."""
    pytest.importorskip("refl1d")
    root = tmp_path / "proj"
    assert (
        CliRunner().invoke(main, ["init", str(root), "--sample", "S1"]).exit_code == 0
    )
    write_partials(root / "samples" / "S1" / "data" / "steady", 100001)
    monkeypatch.chdir(root)
    runner = CliRunner()
    runner.invoke(main, ["model", "new", "S1", "--name", "m"])
    runner.invoke(main, ["model", "generate", "samples/S1/models/m.yaml"])
    result = runner.invoke(
        main,
        [
            "fit",
            "run",
            "samples/S1/models/m.py",
            "--method",
            "amoeba",
            "--steps",
            "20",
            "--seed",
            "1",
            "--parallel",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(runner.invoke(main, ["ls", "--json"]).stdout)
    return root, rows[0]["fit_id"]


def test_assess_writes_into_the_fits_note(
    fitted: tuple[Path, str], monkeypatch
) -> None:
    """An assessment printed to a terminal is gone by the next command. In the
    note it is in the bundle, on the fit page, and in front of whoever asks."""
    root, fit_id = fitted

    result = CliRunner().invoke(main, ["assess", fit_id, "--no-llm"])

    assert result.exit_code == 0, result.output
    notes = (root / "samples" / "S1" / "results" / fit_id / "NOTES.md").read_text()
    assert "## Assessment" in notes
    assert "chi-squared" in notes


def test_assess_without_an_endpoint_says_what_was_not_checked(
    fitted: tuple[Path, str], monkeypatch
) -> None:
    """Silence would read as "nothing to report" rather than "not asked"."""
    root, fit_id = fitted
    monkeypatch.setattr(
        "nr_workbench.aure_adapter.llm_available", lambda: False, raising=False
    )

    result = CliRunner().invoke(main, ["assess", fit_id, "--json"])

    payload = json.loads(result.stdout)
    assert any("No language-model endpoint" in p for p in payload["problems"])


def _stub_model(monkeypatch, reply: str | Exception) -> list[tuple[str, str]]:
    """Answer every language-model call with *reply*, recording the request.

    The stub has `complete`'s own signature, so a call the real function would
    refuse is refused here too.
    """
    from nr_workbench import aure_adapter

    sent: list[tuple[str, str]] = []

    def complete(system: str, user: str, *, temperature: float = 0.0) -> str:
        sent.append((system, user))
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(aure_adapter, "llm_available", lambda: True)
    monkeypatch.setattr(aure_adapter, "complete", complete)
    return sent


def _record(root: Path, fit_id: str) -> tuple[str, dict]:
    """The fit's NOTES.md and assessment.json, as `nrw assess` left them."""
    fit = root / "samples" / "S1" / "results" / fit_id
    notes = (fit / "NOTES.md").read_text(encoding="utf-8")
    return notes, json.loads((fit / "assessment.json").read_text(encoding="utf-8"))


def test_assess_asks_from_the_fits_own_record_and_notes_the_verdict(
    fitted: tuple[Path, str], monkeypatch
) -> None:
    """The judge is told what this fit's record holds -- its notes, its frozen
    spec, each value's range, the skills, and the convergence flag as the
    manifest records it -- and its verdict goes into the note under a heading
    that says whose it is."""
    from nr_workbench.fitting.assess import read_bounds
    from nr_workbench.fitting.judge import CONVERGENCE
    from nr_workbench.spec.authoring import find_skills

    root, fit_id = fitted
    fit = root / "samples" / "S1" / "results" / fit_id
    # As if DREAM had warned. amoeba records None, which would hide a judge
    # that never read the flag at all.
    manifest = json.loads((fit / "manifest.json").read_text(encoding="utf-8"))
    manifest["info"]["converged"] = False
    (fit / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    notes_path = root / "samples" / "S1" / "sample.md"
    notes_path.write_text(
        notes_path.read_text(encoding="utf-8")
        + "\nA 100 A film on Si, measured in air from the film side.\n",
        encoding="utf-8",
    )
    sent = _stub_model(
        monkeypatch,
        '{"quality_assessment": "marginal", "issues": ["Film is a placeholder"]}',
    )

    result = CliRunner().invoke(main, ["assess", fit_id])

    assert result.exit_code == 0, result.output
    ((system, user),) = sent
    assert "A 100 A film on Si, measured in air" in user
    assert "ionomer" not in user, "the template's example is not this sample"
    assert (fit / "spec.yaml").read_text(encoding="utf-8").strip() in user
    assert CONVERGENCE[False] in user
    name, (low, high) = next(iter(read_bounds(fit).items()))
    line = next(row for row in user.splitlines() if row.startswith(f"- {name} = "))
    assert f"{low:.6g}" in line and f"{high:.6g}" in line
    assert "chi-squared by model" in user, "three segments are three models"
    skill = find_skills(root)["nrw-model-spec"].read_text(encoding="utf-8")
    assert skill.strip()[:300] in system

    notes, record = _record(root, fit_id)
    assert "What a language model made of it" in notes
    assert "Film is a placeholder" in notes
    assert record["judgement"]["quality_assessment"] == "marginal"


@pytest.mark.parametrize("reply", ["Looks fine.", '{"issues": []}'])
def test_a_reply_without_a_verdict_is_recorded_as_none(
    fitted: tuple[Path, str], monkeypatch, reply: str
) -> None:
    """No stand-in, and no empty section: either under the model's heading
    would read as the model's opinion."""
    root, fit_id = fitted
    _stub_model(monkeypatch, reply)

    result = CliRunner().invoke(main, ["assess", fit_id])

    assert result.exit_code == 0, result.output
    notes, record = _record(root, fit_id)
    assert "What a language model made of it" not in notes
    assert record["judgement"] is None
    assert any("not with a verdict that could be read" in p for p in record["problems"])


def test_a_failed_call_is_recorded_not_raised(
    fitted: tuple[Path, str], monkeypatch
) -> None:
    from nr_workbench.aure_adapter import AureUnavailableError

    root, fit_id = fitted
    _stub_model(monkeypatch, AureUnavailableError("The call failed: quota\nexceeded"))

    result = CliRunner().invoke(main, ["assess", fit_id])

    assert result.exit_code == 0, result.output
    notes, record = _record(root, fit_id)
    assert "What a language model made of it" not in notes
    assert record["judgement"] is None
    assert any("quota exceeded" in p for p in record["problems"])


def test_a_skill_it_cannot_read_is_named_and_the_rest_is_still_judged(
    fitted: tuple[Path, str], monkeypatch
) -> None:
    """A broken SKILL.md once raised before anything was written, losing the
    arithmetic the command had already run."""
    root, fit_id = fitted
    skill = root / "skills" / "reflectometry" / "nrw-model-spec" / "SKILL.md"
    skill.unlink()
    skill.symlink_to(root / "nowhere.md")
    _stub_model(monkeypatch, '{"quality_assessment": "good"}')

    result = CliRunner().invoke(main, ["assess", fit_id])

    assert result.exit_code == 0, result.output
    _, record = _record(root, fit_id)
    assert record["judgement"] == {"quality_assessment": "good"}
    assert any("nrw-model-spec" in p for p in record["problems"])


def test_assess_under_an_agent_asks_no_model(
    fitted: tuple[Path, str], monkeypatch
) -> None:
    """Through the command, with the record checked: under a harness the
    harness judges, and no second model's verdict is filed."""
    from nr_workbench import aure_adapter

    root, fit_id = fitted
    monkeypatch.setenv("NRW_AGENT", "1")

    def explode(*args: object, **kwargs: object) -> bool:
        raise AssertionError("consulted an endpoint while an agent was driving")

    monkeypatch.setattr(aure_adapter, "llm_available", explode)
    monkeypatch.setattr(aure_adapter, "complete", explode)

    result = CliRunner().invoke(main, ["assess", fit_id])

    assert result.exit_code == 0, result.output
    notes, record = _record(root, fit_id)
    assert "What a language model made of it" not in notes
    assert record["judgement"] is None
    assert any("yourself" in p for p in record["problems"])


def test_assess_json_carries_every_finding(
    fitted: tuple[Path, str], monkeypatch
) -> None:
    root, fit_id = fitted

    result = CliRunner().invoke(main, ["assess", fit_id, "--no-llm", "--json"])

    payload = json.loads(result.stdout)
    assert payload["schema"] == "nrw-fit-assessment/1"
    assert payload["fit_id"] == fit_id
    assert isinstance(payload["findings"], list)


# --------------------------------------------------------------------------
# Degeneracy and layer coherence
# --------------------------------------------------------------------------


def write_slabs(
    directory: Path, name: str, rows: list[tuple[float, float, float]]
) -> None:
    """Write a bumps `-slabs.dat`: thickness, interface, rho, irho."""
    fit = directory / "fit"
    fit.mkdir(parents=True, exist_ok=True)
    (fit / name).write_text(
        "# thickness interface rho irho\n"
        + "".join(f"{t} {i} {r} 0\n" for t, i, r in rows),
        encoding="utf-8",
    )


SPEC_YAML = """\
schema: nrw-model/1
name: m
sample: S1
materials: {A: {rho: 6.0}, CuOx: {rho: 4.0}, Cu: {rho: 6.3}, Si: {rho: 2.07}}
stack:
  - {name: A, thickness: 0, roughness: 20}
  - {name: CuOx, thickness: 21, roughness: 13}
  - {name: Cu, thickness: 486, roughness: 5}
  - {name: Si}
probe: {resolution: angular_only, dq_is_fwhm: true}
states: [{name: s1, run: 100001, segments: auto, thetas: [0.45]}]
parameters: [{path: CuOx.thickness, range: [10, 80], per: state}]
"""


def test_a_layer_thinner_than_its_own_interfaces_is_flagged(tmp_path: Path) -> None:
    """The real failure, with its real numbers: an oxide of 21.29 A between
    interfaces of 20 and 12.99, summing to 1.55x its own thickness. It is two
    overlapping error functions, not a slab, so its SLD occurs nowhere in the
    structure -- and chi-squared cannot see that.
    """
    write_fit(tmp_path, par={}, bounds={})
    (tmp_path / "spec.yaml").write_text(SPEC_YAML, encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"info": {"models": [{"index": 1, "name": "run218386#0"}]}}),
        encoding="utf-8",
    )
    write_slabs(
        tmp_path,
        "m-1-slabs.dat",
        [(0, 20, 5.88), (21.2925, 12.989, 4.0047), (486.5, 5.05, 6.30), (0, 0, 2.07)],
    )

    findings = check(tmp_path, manifest()).findings

    flagged = [f for f in findings if f.kind == "layer-swallowed"]
    assert [f.parameter for f in flagged] == ["run218386 CuOx"]
    assert "1.55x" in flagged[0].message
    assert "32.99" in flagged[0].message


def test_a_layer_thicker_than_its_interfaces_is_not_flagged(tmp_path: Path) -> None:
    """The other state of the same real fit: at 48 A the oxide is fine."""
    write_fit(tmp_path, par={}, bounds={})
    (tmp_path / "spec.yaml").write_text(SPEC_YAML, encoding="utf-8")
    write_slabs(
        tmp_path,
        "m-1-slabs.dat",
        [(0, 8, 5.88), (48.3, 14.1, 4.0), (486.5, 5.05, 6.30), (0, 0, 2.07)],
    )

    assert [
        f for f in check(tmp_path, manifest()).findings if f.kind == "layer-swallowed"
    ] == []


def test_correlated_parameters_are_reported_with_the_strongest_first(
    tmp_path: Path,
) -> None:
    """Two parameters at r = 0.94 have one measured combination between them,
    so quoting both with their own intervals claims two measurements where
    there was one."""
    import gzip

    import numpy as np

    rng = np.random.default_rng(0)
    base = rng.normal(size=400)
    # a and b nearly collinear; c independent
    draws = np.column_stack(
        [
            np.zeros(400),  # column 0 is the log-likelihood
            base,
            base * 2 + rng.normal(scale=0.1, size=400),
            rng.normal(size=400),
        ]
    )
    write_fit(
        tmp_path,
        par={},
        bounds={},
        err={
            "a": {"index": 0, "best": 0.0},
            "b": {"index": 1, "best": 0.0},
            "c": {"index": 2, "best": 0.0},
        },
    )
    with gzip.open(tmp_path / "fit" / "m-point.mc.gz", "wt") as handle:
        np.savetxt(handle, draws)

    findings = check(tmp_path, manifest()).findings

    flagged = [f for f in findings if f.kind == "correlated"]
    assert len(flagged) == 1
    assert "a <-> b" in flagged[0].message
    assert "c" not in flagged[0].message.split("Each pair")[0].replace("correlated", "")


def test_uncorrelated_parameters_produce_nothing(tmp_path: Path) -> None:
    import gzip

    import numpy as np

    rng = np.random.default_rng(1)
    draws = np.column_stack([np.zeros(400), rng.normal(size=(400, 2))])
    write_fit(
        tmp_path,
        par={},
        bounds={},
        err={"a": {"index": 0, "best": 0.0}, "b": {"index": 1, "best": 0.0}},
    )
    with gzip.open(tmp_path / "fit" / "m-point.mc.gz", "wt") as handle:
        np.savetxt(handle, draws)

    assert [
        f for f in check(tmp_path, manifest()).findings if f.kind == "correlated"
    ] == []


def test_no_chain_means_no_correlation_opinion(tmp_path: Path) -> None:
    """An optimiser run has no posterior, which is absence of information."""
    write_fit(tmp_path, par={"a": 1.0}, bounds={})

    assert [
        f for f in check(tmp_path, manifest()).findings if f.kind == "correlated"
    ] == []


# --------------------------------------------------------------------------
# Interval inflation
#
# DREAM's posterior width is conditional on the reported dR being right. At
# chi-squared 2.9 they are understated by about 1.7, and the interval it prints
# is too narrow by that factor -- which is how two states come to look 3 sigma
# apart when they are 1.9.
# --------------------------------------------------------------------------


def err_for(**halves: float) -> dict[str, dict]:
    """An uncertainty summary with the given half-widths at 68%."""
    return {
        name: {"median": 100.0, "p68": [100.0 - half, 100.0 + half]}
        for name, half in halves.items()
    }


def test_a_high_chisq_asks_for_the_intervals_to_be_inflated(tmp_path: Path) -> None:
    """The real case: chi-squared 2.94, Cu thickness +-0.774 -> +-1.33."""
    write_fit(tmp_path, par={}, bounds={}, err=err_for(Cu_thickness=0.774))

    findings = check(tmp_path, manifest(chisq=2.94, points=2292)).findings

    flagged = [f for f in findings if f.kind == "intervals-need-inflation"]
    assert len(flagged) == 1
    assert flagged[0].severity == "warn"
    assert "1.71" in flagged[0].message
    assert "+-0.774 -> +-1.33" in flagged[0].message
    # sqrt(2/2292) = 0.0295, so 2.94 is 66 sigma from 1 -- not a rounding error.
    assert "66 sigma from 1 at 2292 points" in flagged[0].message


def test_a_good_chisq_leaves_the_intervals_alone(tmp_path: Path) -> None:
    """Below the threshold the posterior width is the honest one."""
    write_fit(tmp_path, par={}, bounds={}, err=err_for(Cu_thickness=0.774))

    findings = check(tmp_path, manifest(chisq=1.1, points=2292)).findings

    assert not [f for f in findings if f.kind == "intervals-need-inflation"]


def test_an_optimiser_run_is_not_asked_to_inflate_nothing(tmp_path: Path) -> None:
    """No posterior, no intervals to scale -- `no-uncertainty` already covers it."""
    write_fit(tmp_path, par={}, bounds={}, err=None)

    findings = check(tmp_path, manifest(chisq=12.1, points=2292)).findings

    assert not [f for f in findings if f.kind == "intervals-need-inflation"]
    assert [f for f in findings if f.kind == "no-uncertainty"]


def test_the_widest_intervals_are_the_ones_named(tmp_path: Path) -> None:
    """Those are what a reader leans on, and what the correction moves most."""
    write_fit(
        tmp_path,
        par={},
        bounds={},
        err=err_for(tiny=0.01, small=0.1, big=5.0, medium=1.0, mid=0.5),
    )

    findings = check(tmp_path, manifest(chisq=4.0, points=2292)).findings
    message = next(f for f in findings if f.kind == "intervals-need-inflation").message

    assert message.index("big") < message.index("medium") < message.index("mid")
    assert "+-5 -> +-10" in message  # sqrt(4) = 2


def test_naming_only_the_widest_says_how_many_were_not_named(tmp_path: Path) -> None:
    """The factor applies to every interval, not the four that fit in a sentence.

    A truncated list with no count reads as "these are the affected parameters",
    which is the opposite of what the finding means.
    """
    write_fit(
        tmp_path,
        par={},
        bounds={},
        err=err_for(a=5.0, b=4.0, c=3.0, d=2.0, e=1.0, f=0.5),
    )

    findings = check(tmp_path, manifest(chisq=4.0, points=2292)).findings
    message = next(f for f in findings if f.kind == "intervals-need-inflation").message

    assert "(and 2 more, all by 2.00)" in message


def test_the_inflation_finding_reaches_the_markdown(tmp_path: Path) -> None:
    write_fit(tmp_path, par={}, bounds={}, err=err_for(Cu_thickness=0.774))

    rendered = as_markdown(check(tmp_path, manifest(chisq=2.94, points=2292)))

    assert "too narrow" in rendered


# --------------------------------------------------------------------------
# Residual structure
#
# Chi-squared is a sum, and a sum cannot say that its excess sits at one
# segment's edge, or that it oscillates in step with the model's own fringes.
# Coherent residual adds in phase, so a ten-sigma pattern fits inside a
# chi-squared that reads as "good" -- which is how the real analysis stopped at
# 2.94 with 11 sigma of unmodelled fringe contrast in it.
# --------------------------------------------------------------------------


def write_refl(
    directory: Path,
    index: int,
    q,
    r,
    dr,
    theory,
    *,
    stem: str = "m",
) -> None:
    """Write a `<stem>-<index>-refl.dat` in the five-column export format."""
    fit = directory / "fit"
    fit.mkdir(parents=True, exist_ok=True)
    rows = "\n".join(
        f"{a:.8g} {a * 0.02:.8g} {b:.8g} {c:.8g} {d:.8g}"
        for a, b, c, d in zip(q, r, dr, theory, strict=True)
    )
    (fit / f"{stem}-{index}-refl.dat").write_text(
        "# Q dQ R dR theory\n" + rows + "\n", encoding="utf-8"
    )


def fringed(q, *, thickness: float = 400.0, depth: float = 0.3, level: float = 1e-3):
    """A decaying curve with Kiessig fringes, as a stand-in for a real model."""
    import numpy as np

    return level * (q / q[0]) ** -4 * (1.0 + depth * np.cos(q * thickness))


def refl_manifest(names: dict[int, str], chisq: float = 1.0) -> dict:
    """A manifest naming each exported model, as `read_reflectivity` needs."""
    return {
        "info": {
            "chisq": chisq,
            "n_free": 2,
            "models": [
                {"index": i, "name": n, "n_points": 200} for i, n in names.items()
            ],
        },
        "provenance": {"fit_id": "20260807-163359Z-0103d9c7"},
    }


def test_reflectivity_is_keyed_by_model_name(tmp_path: Path) -> None:
    import numpy as np

    q = np.linspace(0.01, 0.2, 200)
    theory = fringed(q)
    write_refl(tmp_path, 1, q, theory, theory * 0.05, theory)
    (tmp_path / "manifest.json").write_text(
        json.dumps(refl_manifest({1: "run100001#0"})), encoding="utf-8"
    )

    curves = read_reflectivity(tmp_path)

    assert list(curves) == ["run100001#0"]
    assert len(curves["run100001#0"]["Q"]) == 200


def test_nonpositive_points_are_dropped(tmp_path: Path) -> None:
    """A negative R cannot enter a log-space comparison; refl exports contain them."""
    import numpy as np

    q = np.linspace(0.01, 0.2, 200)
    theory = fringed(q)
    r = theory.copy()
    r[10] = -1e-9
    dr = theory * 0.05
    dr[20] = 0.0
    write_refl(tmp_path, 1, q, r, dr, theory)
    (tmp_path / "manifest.json").write_text(
        json.dumps(refl_manifest({1: "a"})), encoding="utf-8"
    )

    assert len(read_reflectivity(tmp_path)["a"]["Q"]) == 198


def test_a_model_with_fringes_too_deep_is_reported_in_phase(tmp_path: Path) -> None:
    """The real signature: data fringes shallower than the model's.

    Depth is wrong, not position, so the fix is resolution or an interfacial
    width -- and the finding has to say that rather than "chi-squared is high".
    """
    import numpy as np

    q = np.linspace(0.01, 0.2, 200)
    theory = fringed(q, depth=0.3)
    data = fringed(q, depth=0.1)  # same positions, shallower fringes
    write_refl(tmp_path, 1, q, data, theory * 0.01, theory)
    (tmp_path / "manifest.json").write_text(
        json.dumps(refl_manifest({1: "run1#1"})), encoding="utf-8"
    )

    findings = check(tmp_path, refl_manifest({1: "run1#1"})).findings

    flagged = [f for f in findings if f.kind == "coherent-residual"]
    assert len(flagged) == 1
    assert "too deep" in flagged[0].message
    assert "DEPTH" in flagged[0].message


def test_a_model_with_shifted_fringes_is_reported_in_quadrature(tmp_path: Path) -> None:
    """A thickness error moves the fringes; that is a different fix entirely."""
    import numpy as np

    q = np.linspace(0.01, 0.2, 200)
    theory = fringed(q, thickness=400.0)
    data = fringed(q, thickness=404.0)  # same depth, shifted positions
    write_refl(tmp_path, 1, q, data, theory * 0.01, theory)
    (tmp_path / "manifest.json").write_text(
        json.dumps(refl_manifest({1: "run1#1"})), encoding="utf-8"
    )

    findings = check(tmp_path, refl_manifest({1: "run1#1"})).findings

    flagged = [f for f in findings if f.kind == "coherent-residual"]
    assert len(flagged) == 1
    assert "POSITIONS" in flagged[0].message


def test_a_faithful_model_produces_no_coherent_finding(tmp_path: Path) -> None:
    """Noise around the right curve must not be reported as structure."""
    import numpy as np

    rng = np.random.default_rng(7)
    q = np.linspace(0.01, 0.2, 200)
    theory = fringed(q)
    dr = theory * 0.05
    write_refl(tmp_path, 1, q, theory + rng.normal(0, 1, 200) * dr, dr, theory)
    (tmp_path / "manifest.json").write_text(
        json.dumps(refl_manifest({1: "run1#1"})), encoding="utf-8"
    )

    findings = check(tmp_path, refl_manifest({1: "run1#1"})).findings

    assert not [f for f in findings if f.kind == "coherent-residual"]
    assert not [f for f in findings if f.kind == "chisq-concentrated"]


def test_excess_at_a_segment_edge_is_named_as_an_edge(tmp_path: Path) -> None:
    """An edge excess is a stitching or band-edge problem, not the model."""
    import numpy as np

    rng = np.random.default_rng(3)
    q = np.linspace(0.01, 0.2, 200)
    theory = fringed(q)
    dr = theory * 0.05
    data = theory + rng.normal(0, 1, 200) * dr
    data[-50:] = theory[-50:] * 1.30  # the top band is 30% high
    write_refl(tmp_path, 1, q, data, dr, theory)
    (tmp_path / "manifest.json").write_text(
        json.dumps(refl_manifest({1: "run1#1"})), encoding="utf-8"
    )

    findings = check(tmp_path, refl_manifest({1: "run1#1"})).findings

    flagged = [f for f in findings if f.kind == "chisq-concentrated"]
    assert len(flagged) == 1
    assert "highest-Q" in flagged[0].message
    assert "segment's own edge" in flagged[0].message


def test_equal_q_damping_that_differs_by_angle_points_at_the_instrument(
    tmp_path: Path,
) -> None:
    """Two segments overlapping in Q, damped by different amounts.

    Angle-dependent means instrumental, and the finding must name
    sample_broadening rather than leave the analyst to guess.
    """
    import numpy as np

    lower_q = np.linspace(0.03, 0.10, 200)
    upper_q = np.linspace(0.08, 0.20, 200)
    names = {1: "run1#0", 2: "run1#1"}
    # The lower-angle segment matches; the upper one is heavily damped.
    write_refl(
        tmp_path,
        1,
        lower_q,
        fringed(lower_q, depth=0.30),
        fringed(lower_q) * 0.002,
        fringed(lower_q, depth=0.30),
    )
    write_refl(
        tmp_path,
        2,
        upper_q,
        fringed(upper_q, depth=0.02),
        fringed(upper_q) * 0.002,
        fringed(upper_q, depth=0.30),
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps(refl_manifest(names)), encoding="utf-8"
    )

    findings = check(tmp_path, refl_manifest(names)).findings

    flagged = [f for f in findings if f.kind == "fringe-damping"]
    assert len(flagged) == 1
    assert "disagree" in flagged[0].message
    assert "sample_broadening" in flagged[0].message


def test_agreement_within_wide_error_bars_is_not_called_agreement(
    tmp_path: Path,
) -> None:
    """The honest case, and the one the real fit lands in.

    "They agree, so it is Q-only" is a directive NOT to reach for
    sample_broadening. An angle-dependent cause big enough to explain the damping
    moves the amplitude ratio by about as much as these error bars, so the check
    must report what it measured rather than what it cannot exclude.
    """
    import numpy as np

    lower_q = np.linspace(0.03, 0.10, 200)
    upper_q = np.linspace(0.08, 0.20, 200)
    names = {1: "run1#0", 2: "run1#1"}
    # Both damped to nothing, with uncertainties too large to compare them.
    for index, q in ((1, lower_q), (2, upper_q)):
        write_refl(
            tmp_path,
            index,
            q,
            fringed(q, depth=0.0),
            fringed(q) * 0.30,
            fringed(q, depth=0.30),
        )
    (tmp_path / "manifest.json").write_text(
        json.dumps(refl_manifest(names)), encoding="utf-8"
    )

    findings = check(tmp_path, refl_manifest(names)).findings

    flagged = [f for f in findings if f.kind == "fringe-damping"]
    assert len(flagged) == 1
    assert "cannot separate resolution from an interfacial width" in flagged[0].message
    assert "damped in both" in flagged[0].message


def test_a_fit_with_no_exported_curves_says_nothing(tmp_path: Path) -> None:
    """An older result directory must not raise, and must not invent findings."""
    write_fit(tmp_path, par={}, bounds={})

    findings = check(tmp_path, manifest()).findings

    assert not [
        f
        for f in findings
        if f.kind in {"coherent-residual", "chisq-concentrated", "fringe-damping"}
    ]


# --------------------------------------------------------------------------
# The note is not the assessment
#
# `nrw assess --write` fills the generated block, and a note holding only that
# block looks written to anyone skimming it -- headings, prose, numbers. Every
# note in the reference sample was in that state: three empty template sections
# with a machine-written assessment under them.
# --------------------------------------------------------------------------


def _captured(action) -> str:
    """Run a click-printing callable and return what it wrote."""
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        action()
    return buffer.getvalue()


def test_assess_says_the_note_is_still_unwritten(tmp_path: Path) -> None:
    from nr_workbench.commands.assess import _nudge_if_unwritten
    from nr_workbench.notes import NOTES_TEMPLATE

    path = tmp_path / "NOTES.md"
    path.write_text(
        NOTES_TEMPLATE.format(fit_id="20260807-163359Z-0103d9c7", description="x")
        + "\n<!-- nrw:generated -->\n## Assessment\n\nchi-squared 2.9\n"
        "<!-- /nrw:generated -->\n",
        encoding="utf-8",
    )

    printed = _captured(lambda: _nudge_if_unwritten(path, "20260807-163359Z-0103d9c7"))

    assert "not what you concluded" in printed
    assert "nrw note 0103d9c7" in printed, "the exact command, not a hint"


def test_assess_stays_quiet_once_someone_has_written(tmp_path: Path) -> None:
    from nr_workbench.commands.assess import _nudge_if_unwritten
    from nr_workbench.notes import NOTES_TEMPLATE, write_section

    path = tmp_path / "NOTES.md"
    path.write_text(
        write_section(
            NOTES_TEMPLATE.format(fit_id="20260807-163359Z-0103d9c7", description="x"),
            "Why this run",
            "testing whether the oxide is resolvable",
        ),
        encoding="utf-8",
    )

    printed = _captured(lambda: _nudge_if_unwritten(path, "20260807-163359Z-0103d9c7"))

    assert printed.strip() == ""
