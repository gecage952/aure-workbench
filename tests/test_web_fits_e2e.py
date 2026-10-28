"""Fits from the Experiment page, end to end: every step the real command.

Split from ``test_web_models.py`` because each of these runs real child
processes for seconds: in one module they would all queue on one test worker,
and the suite waits for the slowest worker.
"""

# The fixtures are imported and requested by name, which ruff reads as
# redefinitions.
# ruff: noqa: F811

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from nr_workbench.provenance.index import FitIndex
from nr_workbench.web import jobs as jobs_module

from .test_web_models import (  # noqa: F401 - fixtures
    app,
    create,
    described,
    empty_home,
    fit,
    job_ended,
    project,
    quick,
    serial,
    serial_fits,
    started,
    writer,
)

pytestmark = pytest.mark.integration


@pytest.mark.fit
@pytest.mark.integration
def test_a_page_fit_is_recorded_as_nrw_fit_run_records_it_and_again_as_a_replicate(
    app, writer, project: Path
) -> None:
    started(create(writer, app, "S1", "oxide"))
    assert job_ended(writer)["job"]["status"] == "ok"
    body = {"method": "amoeba", "steps": 3}

    started(fit(writer, app, "S1", "oxide", body))
    fitted = job_ended(writer)
    started(fit(writer, app, "S1", "oxide", body))  # nothing changed since
    again = job_ended(writer)

    job, log = fitted["job"], fitted["log"]
    assert (job["status"], job["step"]) == ("ok", 2), log
    assert log.startswith("$ nrw model generate samples/S1/models/oxide.yaml")
    assert "$ nrw fit run samples/S1/models/oxide.py --method=amoeba" in log
    index = FitIndex(project / ".nrw" / "index.jsonl")
    recorded = index.find(job["fit_id"])
    assert (recorded["model"], recorded["method"], recorded["status"]) == (
        "oxide",
        "amoeba",
        "ok",
    )
    assert recorded["settings"]["steps"] == 3
    # The identical fit again is recorded as a replicate, and the job links it.
    assert again["job"]["status"] == "ok", again["log"]
    assert "An identical run already exists" in again["log"]
    newest = index.fits(sample="S1")[0]
    assert again["job"]["fit_id"] == newest["fit_id"] != job["fit_id"]


def aure_run_stands_in(monkeypatch) -> None:
    """Every step is the real command but AuRE's run, which needs a language
    model: that one writes what a finished run leaves."""
    from .test_aure_cmd import FITTED

    finished = json.dumps(
        {
            "success": True,
            "error": None,
            "final_chi2": 1.8,
            "state": {"current_model": FITTED, "best_chi2": 1.8},
        }
    )

    def command(*args: str) -> list[str]:
        if args[:2] == ("aure", "run"):
            output = Path(args[2]).parent / "output"
            code = (
                f"import pathlib; p = pathlib.Path({str(output)!r}); "
                f"p.mkdir(parents=True); (p / 'final_state.json').write_text({finished!r})"
            )
            return [sys.executable, "-c", code]
        return serial(*args)

    monkeypatch.setattr(jobs_module, "nrw_command", command)


@pytest.mark.fit
def test_a_quick_fit_with_aure_is_recorded_as_a_fit_of_the_spec_it_proposed(
    app, writer, project: Path, monkeypatch
) -> None:
    described(project)
    aure_run_stands_in(monkeypatch)

    started(quick(writer, app, "S1", {"name": "auto", "run": 100001}))
    payload = job_ended(writer)

    job, log = payload["job"], payload["log"]
    assert (job["status"], job["step"]) == ("ok", 5), log
    assert "$ nrw aure new --name=auto --run=100001 -- S1\n" in log
    assert "$ nrw aure run samples/S1/aure/auto/setup.yaml --budget=quick\n" in log
    # No --run: import reads the run from the setup AuRE was given.
    assert (
        "$ nrw aure import samples/S1/aure/auto/output --sample=S1 --name=auto\n" in log
    )
    assert (project / "samples" / "S1" / "models" / "auto.yaml").is_file()
    (recorded,) = FitIndex(project / ".nrw" / "index.jsonl").fits(sample="S1")
    assert recorded["fit_id"] == job["fit_id"]
    # The project's fitter: this nrw.toml sets no [fit], so DREAM -- the fit
    # that counts carries uncertainties.
    assert (recorded["model"], recorded["method"]) == ("auto", "dream")


def test_a_quick_fit_without_an_endpoint_stops_at_aure_run_and_says_how_to_set_one(
    app, writer, project: Path, monkeypatch
) -> None:
    from nr_workbench import aure_adapter

    # Every step real. Conftest clears the variables AuRE reads, and the home
    # the steps see is empty -- but a machine where an endpoint is configured
    # anyway must never be billed for this test.
    if aure_adapter.llm_available():
        pytest.skip("a language-model endpoint is configured here")
    monkeypatch.delenv("NRW_AGENT", raising=False)
    described(project)

    started(quick(writer, app, "S1", {"name": "auto", "run": 100001}))
    payload = job_ended(writer)

    job, log = payload["job"], payload["log"]
    assert (job["status"], job["step"], job["fit_id"]) == ("failed", 2, None), log
    assert "no endpoint is configured" in log
    assert "nrw check-llm" in log
    assert not (project / "samples" / "S1" / "models" / "auto.yaml").exists()
    assert FitIndex(project / ".nrw" / "index.jsonl").fits(sample="S1") == []


@pytest.mark.fit
def test_a_quick_fit_runs_again_after_the_notes_change_and_aure_hears_them(
    app, writer, project: Path, monkeypatch
) -> None:
    """What the page could not do: a model AuRE proposed, quick-fitted again once
    something was written about the measurement -- which reaches AuRE's setup."""
    import yaml

    described(project)
    aure_run_stands_in(monkeypatch)
    started(quick(writer, app, "S1", {"name": "auto", "run": 100001}))
    first = job_ended(writer)["job"]
    with (project / "samples" / "S1" / "sample.md").open("a", encoding="utf-8") as f:
        f.write("\n## Measurement conditions\n\n- Run 100001: in D2O, realigned\n")

    started(quick(writer, app, "S1", {"name": "auto", "run": 100001}))
    again = job_ended(writer)

    job = again["job"]
    assert (first["status"], job["status"], job["step"]) == ("ok", "ok", 5), again[
        "log"
    ]
    assert job["label"] == "quick fit of auto with AuRE again"
    setup = yaml.safe_load(
        (project / "samples" / "S1" / "aure" / "auto-2" / "setup.yaml").read_text()
    )
    # In the description, which AuRE's prompts read (see aure_setup.compose).
    assert "Notes on run 100001: in D2O, realigned" in setup["sample_description"]
    # The first run of AuRE is kept; the spec is AuRE's second proposal.
    assert (project / "samples" / "S1" / "aure" / "auto" / "output").is_dir()
    spec = (project / "samples" / "S1" / "models" / "auto.yaml").read_text()
    assert "aure/auto-2/output" in spec
    fits = FitIndex(project / ".nrw" / "index.jsonl").fits(sample="S1")
    assert [f["fit_id"] for f in fits] == [job["fit_id"], first["fit_id"]]
