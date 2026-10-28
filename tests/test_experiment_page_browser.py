"""The Experiment page in a real browser: what someone typed survives a save made elsewhere.

The catalog can be saved while the editor is open -- from another tab, the
command line, a `git pull`. The page reloads then, and on the conflict the
next save meets; each reload used to fill the editor afresh, wiping what had
been typed, or leaving stale values for that save to write back.

Headless Chrome over DevTools pipes, as in ``test_settings_page_browser``;
skipped where there is no Chrome.
"""

# The browser fixtures are imported and requested by name, which ruff reads
# as redefinitions.
# ruff: noqa: F811

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner

from nr_workbench.cli import main
from nr_workbench.experiment.model import (
    ExperimentChange,
    RunChange,
    RunKey,
    SampleChange,
)
from nr_workbench.experiment.store import ParquetCatalogStore

from .experiment_fixtures import write_autoreduced
from .test_lifecycle import write_partials
from .test_settings_page_browser import (  # noqa: F401 - fixtures
    Page,
    Site,
    browser,
    page,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project watching a folder, with one sample in its catalog."""
    from nr_workbench.project.settings import save

    root = tmp_path / "analysis"
    assert CliRunner().invoke(main, ["init", str(root)]).exit_code == 0
    facility = tmp_path / "facility"
    write_autoreduced(facility, 234277, [1, 2, 3], mtime=time.time() - 3600)
    save(root, {"ipts": "IPTS-00001", "source.location": str(facility)})
    ParquetCatalogStore.for_project(root).update(
        runs=[RunChange(RunKey(234277), 0, {"sample_id": "S1"})],
        samples=[SampleChange("S1", 0, {"title": "Mine", "description": ""})],
    )
    return root


@pytest.fixture(autouse=True)
def empty_home(tmp_path: Path, monkeypatch) -> None:
    """An empty home for every nrw process a test here starts: a child reads
    ``~/.nrw`` and ``~/.aure`` afresh, and a real endpoint key there bills. The
    browser, started for the module, is already running by now."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


@contextmanager
def serving(app) -> Iterator[Site]:
    """*app* served on a free port, for as long as the block runs."""
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield Site(url=f"http://127.0.0.1:{server.server_port}")
    finally:
        server.shutdown()
        # A test that failed mid-fit leaves nothing running to slow the next.
        app.config["NRW_MODELS"].jobs.stop()
        app.config["NRW_EXPERIMENT"].reload()


@pytest.fixture
def served(project: Path) -> Iterator[tuple[Site, object]]:
    """The project served, and the app serving it."""
    from nr_workbench.web.app import create_app

    app = create_app(project, autostart=False)
    app.config["NRW_EXPERIMENT"].live.scan_once()
    with serving(app) as site:
        yield site, app


@pytest.fixture
def site(served) -> Site:
    return served[0]


def field(page: Page, element_id: str) -> str:
    return page.js(f"document.getElementById('{element_id}').value")


def test_what_you_typed_survives_a_save_of_the_same_sample_made_elsewhere(
    page: Page, site: Site, project: Path
) -> None:
    page.goto(f"{site.url}/experiment")
    page.wait_for(
        "Array.from(document.querySelectorAll('#expt-samples button'))"
        ".some(b => b.textContent.includes('S1'))",
        what="the sample list",
    )
    page.js(
        "Array.from(document.querySelectorAll('#expt-samples button'))"
        ".find(b => b.textContent.includes('S1')).click()"
    )
    page.wait_for("!document.getElementById('f-description').disabled", what="editor")
    page.type("f-description", "typed by me")

    # The same sample is saved elsewhere -- another field -- in the meantime.
    store = ParquetCatalogStore.for_project(project)
    rev = store.load().samples["S1"].rev
    store.update(samples=[SampleChange("S1", rev, {"title": "Their title"})])

    page.click("expt-save")  # meets the conflict, and the page reloads

    page.wait_for(
        "document.getElementById('f-title').value === 'Their title'",
        what="the reload with their title",
    )
    assert field(page, "f-description") == "typed by me"
    assert "saved somewhere else" in page.text("expt-form-status")

    page.click("expt-save")

    page.wait_for(
        "document.getElementById('expt-form-status').textContent.startsWith('Saved')",
        what="the save",
    )
    saved = ParquetCatalogStore.for_project(project).load().samples["S1"]
    assert (saved.title, saved.description) == ("Their title", "typed by me")


def test_what_you_typed_about_the_experiment_survives_a_save_made_elsewhere(
    page: Page, site: Site, project: Path
) -> None:
    page.goto(f"{site.url}/experiment")
    # Nothing is written yet, so the panel is open, to be filled in.
    page.wait_for(
        "document.getElementById('expt-context').open"
        " && !document.getElementById('c-summary').disabled",
        what="the experiment's panel, open",
    )
    assert page.text("expt-context-state") == "not written yet"
    page.type("c-summary", "typed by me")

    # The experiment is saved elsewhere -- another field -- in the meantime.
    ParquetCatalogStore.for_project(project).update(
        experiment=ExperimentChange(0, {"notes": "Their notes"})
    )

    page.click("expt-context-save")  # meets the conflict, and the page reloads

    page.wait_for(
        "document.getElementById('c-notes').value === 'Their notes'",
        what="the reload with their notes",
    )
    assert field(page, "c-summary") == "typed by me"
    assert "saved somewhere else" in page.text("expt-context-status")

    page.click("expt-context-save")

    page.wait_for(
        "document.getElementById('expt-context-status').textContent === 'Saved.'",
        what="the save",
    )
    saved = ParquetCatalogStore.for_project(project).load().experiment
    assert (saved.summary, saved.notes, saved.rev) == ("typed by me", "Their notes", 2)
    assert page.text("expt-context-state") == "typed by me"


def test_an_experiment_saved_empty_reads_as_not_written(
    page: Page, site: Site, project: Path
) -> None:
    """Written means something is in it, as the command line and the docs say,
    not that it was ever saved."""
    store = ParquetCatalogStore.for_project(project)
    store.update(experiment=ExperimentChange(0, {"summary": "A first try."}))
    store.update(experiment=ExperimentChange(1, {"summary": ""}))

    page.goto(f"{site.url}/experiment")

    page.wait_for(
        "document.getElementById('expt-context').open", what="the panel, open"
    )
    assert page.text("expt-context-state") == "not written yet"


def test_a_save_here_does_not_hide_what_was_saved_elsewhere(
    page: Page, site: Site, project: Path
) -> None:
    """A save's answer covers everything saved before it, from anywhere: were
    the page to take its version from that answer, the poll would see nothing
    new, and a sample saved in another tab would never show."""
    page.goto(f"{site.url}/experiment")
    page.wait_for(
        "!document.getElementById('c-summary').disabled", what="the experiment's panel"
    )
    store = ParquetCatalogStore.for_project(project)
    rev = store.load().samples["S1"].rev
    store.update(samples=[SampleChange("S1", rev, {"title": "Their title"})])

    page.type("c-summary", "typed by me")
    page.click("expt-context-save")
    page.wait_for(
        "document.getElementById('expt-context-status').textContent === 'Saved.'",
        what="the save",
    )

    # The next poll, at most ten seconds on, brings it in.
    page.wait_for(
        "document.getElementById('expt-samples').textContent.includes('Their title')",
        what="the sample saved elsewhere, shown",
        timeout=20,
    )


def open_s1(page: Page, site: Site) -> None:
    page.goto(f"{site.url}/experiment")
    page.wait_for(
        "Array.from(document.querySelectorAll('#expt-samples button'))"
        ".some(b => b.textContent.includes('S1'))",
        what="the sample list",
    )
    page.js(
        "Array.from(document.querySelectorAll('#expt-samples button'))"
        ".find(b => b.textContent.includes('S1')).click()"
    )
    page.wait_for(
        "document.querySelector('#f-measurements .expt-measurement') !== null"
        " && !document.querySelector('#f-measurements select').disabled",
        what="the sample's measurements",
    )


def in_row(selector: str) -> str:
    return f"document.querySelector('#f-measurements .expt-measurement {selector}')"


def test_each_runs_type_condition_notes_and_good_switch_are_saved(
    page: Page, site: Site, project: Path
) -> None:
    open_s1(page, site)
    assert page.js(in_row("select") + ".value") == "steady"  # never set: steady
    assert page.js(in_row("[data-field=include]") + ".checked") is True

    page.js(
        f"{{ const s = {in_row('select')}; s.value = '__new__';"
        " s.dispatchEvent(new Event('change')); }"
    )
    page.js(f"{in_row('.expt-type-field input')}.value = 'grazing'")
    page.js(f"{in_row('[data-field=condition]')}.value = 'OCV'")
    page.js(f"{in_row('[data-field=note]')}.value = 'Beam dropped halfway.'")
    page.js(
        f"{{ const g = {in_row('[data-field=include]')}; g.checked = false;"
        " g.dispatchEvent(new Event('change')); }"
    )
    assert "bad" in page.js(in_row(".form-check-label") + ".textContent")
    page.click("expt-save")

    page.wait_for(
        "document.getElementById('expt-form-status').textContent.startsWith('Saved')",
        what="the save",
    )
    entry = ParquetCatalogStore.for_project(project).load().runs[RunKey(234277)]
    assert (entry.measurement, entry.condition, entry.note, entry.include) == (
        "grazing",
        "OCV",
        "Beam dropped halfway.",
        False,
    )
    # The type added is a choice from now on, in the bulk bar too.
    assert page.js(
        "Array.from(document.querySelectorAll('#expt-type-slot option'))"
        ".some(o => o.value === 'grazing')"
    )


def test_a_run_changed_elsewhere_keeps_the_notes_being_typed(
    page: Page, site: Site, project: Path
) -> None:
    open_s1(page, site)
    page.js(f"{in_row('[data-field=note]')}.value = 'realigned after mounting'")
    store = ParquetCatalogStore.for_project(project)
    entry = store.load().runs[RunKey(234277)]
    store.update(runs=[RunChange(entry.key, entry.rev, {"condition": "CA"})])

    page.click("expt-save")  # meets the conflict, and the page reloads

    page.wait_for(
        in_row("[data-field=condition]") + ".value === 'CA'",
        what="the reload with the condition saved elsewhere",
    )
    assert page.js(in_row("[data-field=note]") + ".value") == "realigned after mounting"
    assert "saved somewhere else" in page.js(
        "document.getElementById('expt-form-status').textContent"
    )
    page.click("expt-save")
    page.wait_for(
        "document.getElementById('expt-form-status').textContent.startsWith('Saved')",
        what="the second save",
    )
    saved = ParquetCatalogStore.for_project(project).load().runs[RunKey(234277)]
    assert (saved.condition, saved.note) == ("CA", "realigned after mounting")


def test_the_bulk_bar_leaves_each_runs_type_unless_one_is_chosen(
    page: Page, site: Site, project: Path
) -> None:
    store = ParquetCatalogStore.for_project(project)
    entry = store.load().runs[RunKey(234277)]
    store.update(runs=[RunChange(entry.key, entry.rev, {"measurement": "tNR"})])
    page.goto(f"{site.url}/experiment")
    page.wait_for(
        "document.querySelector('#expt-runs tbody input[type=checkbox]') !== null",
        what="the runs",
    )

    def assign_selected() -> None:
        page.js(
            "{ const b = document.querySelector('#expt-runs tbody input[type=checkbox]');"
            " if (!b.checked) { b.checked = true; b.dispatchEvent(new Event('change')); } }"
        )
        page.js(
            "{ const s = document.getElementById('expt-sample'); s.value = 'S1';"
            " s.dispatchEvent(new Event('change')); }"
        )
        page.click("expt-assign")
        page.wait_for(
            "document.getElementById('expt-message').textContent.startsWith('Assigned')",
            what="the assignment",
        )
        page.js("document.getElementById('expt-message').textContent = ''")

    assign_selected()  # the type chooser left at "unchanged"
    assert store.load().runs[RunKey(234277)].measurement == "tNR"

    page.js(
        "{ const b = document.querySelector('#expt-runs tbody input[type=checkbox]');"
        " b.checked = true; b.dispatchEvent(new Event('change'));"
        " const t = document.querySelector('#expt-type-slot select'); t.value = 'steady'; }"
    )
    assign_selected()
    assert store.load().runs[RunKey(234277)].measurement == "steady"


def test_a_model_is_written_from_the_samples_data_once_it_has_some(
    page: Page, site: Site, project: Path, monkeypatch
) -> None:
    open_s1(page, site)
    page.wait_for(
        "!document.getElementById('expt-models').classList.contains('d-none')",
        what="the models panel",
    )
    assert "Apply first" in page.text("expt-models-list")
    assert page.js("document.getElementById('expt-model-create').disabled") is True

    # What Apply copies in: the sample's data, on disk.
    write_partials(project / "samples" / "S1" / "data" / "steady", 234277)
    page.js(
        "Array.from(document.querySelectorAll('#expt-samples button'))"
        ".find(b => b.textContent.includes('S1')).click()"
    )
    page.wait_for(
        "!document.getElementById('expt-model-create').disabled", what="the data seen"
    )
    page.type("expt-model-name", "oxide")
    page.click("expt-model-create")

    # A job: the language model's answer can take a minute. None is set up
    # here, so the job says so and writes the placeholder stack.
    page.wait_for(
        "document.getElementById('expt-job-state').textContent === 'ok'",
        what="the spec written",
        timeout=60,
    )
    assert "from the notes" in page.text("expt-model-status")
    assert "Wrote samples/S1/models/oxide.yaml" in page.text("expt-job-log")
    assert (project / "samples" / "S1" / "models" / "oxide.yaml").is_file()
    page.wait_for(
        "document.querySelector('#expt-models-list .expt-model-placeholder') !== null",
        what="the spec listed, as a placeholder",
    )
    assert "samples/S1/models/oxide.yaml" in page.text("expt-models-list")

    # Its fit is said to mean nothing before it is run -- but not refused.
    page.js("document.querySelector('#expt-models-list .expt-model-fit').click()")
    assert "placeholder stack" in page.text("expt-fit-placeholder")
    assert not page.js(
        "document.getElementById('expt-fit-placeholder').classList.contains('d-none')"
    )
    assert page.text("expt-fit-run") == "Fit the placeholder anyway"


@pytest.mark.fit
def test_a_fit_started_on_the_page_is_followed_to_its_record(
    page: Page, site: Site, project: Path, monkeypatch
) -> None:
    from nr_workbench.web import jobs as jobs_module

    from .test_web_models import serial

    monkeypatch.setattr(jobs_module, "nrw_command", serial)
    write_partials(project / "samples" / "S1" / "data" / "steady", 234277)
    open_s1(page, site)
    page.wait_for(
        "!document.getElementById('expt-model-create').disabled", what="the data seen"
    )
    page.type("expt-model-name", "oxide")
    page.click("expt-model-create")
    page.wait_for(
        "document.getElementById('expt-job-state').textContent === 'ok'",
        what="the spec written",
        timeout=60,
    )
    page.wait_for(
        "document.querySelector('#expt-models-list .expt-model-fit') !== null",
        what="the spec listed",
    )

    page.js("document.querySelector('#expt-models-list .expt-model-fit').click()")
    # It starts at the project's default -- nrw.toml says nothing, so DREAM.
    assert page.js("document.getElementById('expt-fit-method').value") == "dream"
    assert "(default)" in page.js(
        "document.getElementById('expt-fit-method').selectedOptions[0].textContent"
    )
    page.js(
        "{ const m = document.getElementById('expt-fit-method'); m.value = 'amoeba';"
        " m.dispatchEvent(new Event('change')); }"
    )
    page.type("expt-fit-steps", "3")
    page.click("expt-fit-run")

    page.wait_for(
        "document.getElementById('expt-job-result').textContent.includes('Recorded as')",
        what="the fit recorded",
        timeout=120,
    )
    href = page.js("document.querySelector('#expt-job-result a').getAttribute('href')")
    from nr_workbench.provenance.index import FitIndex

    (recorded,) = FitIndex(project / ".nrw" / "index.jsonl").fits(sample="S1")
    assert href == f"/f/{recorded['fit_id']}"
    assert "$ nrw fit run samples/S1/models/oxide.py" in page.text("expt-job-log")
    assert page.text("expt-job-state") == "ok"

    # The fit's own page, under the policy that keeps its write token: a real
    # fit's WebGL plot draws there (it needs 'unsafe-eval'), with no error.
    page.goto(f"{site.url}{href}")
    page.wait_for(
        "document.querySelector('#plot-fit canvas') !== null",
        what="the fit's plot",
        timeout=30,
    )


def test_a_fit_is_cancelled_from_the_page(
    page: Page, site: Site, project: Path, monkeypatch
) -> None:
    import sys

    from nr_workbench.web import jobs as jobs_module

    # A step that runs until it is stopped, standing in for a long DREAM run.
    monkeypatch.setattr(
        jobs_module,
        "nrw_command",
        lambda *args: [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    spec = project / "samples" / "S1" / "models" / "oxide.yaml"
    spec.parent.mkdir(parents=True)
    spec.write_text("{}\n")
    # Data, so nothing but the running job keeps New model from starting.
    write_partials(project / "samples" / "S1" / "data" / "steady", 234277)
    open_s1(page, site)
    page.wait_for(
        "document.querySelector('#expt-models-list .expt-model-fit') !== null",
        what="the spec listed",
    )
    page.js("document.querySelector('#expt-models-list .expt-model-fit').click()")
    page.click("expt-fit-run")
    page.wait_for(
        "document.getElementById('expt-job-state').textContent === 'step 1 of 2'",
        what="the job running",
    )

    # One job at a time: New model waits for it, and says so.
    assert page.js("document.getElementById('expt-model-create').disabled")
    assert "one runs at a time" in page.text("expt-model-wait")

    page.click("expt-job-cancel")

    page.wait_for(
        "document.getElementById('expt-job-state').textContent === 'cancelled'",
        what="the job cancelled",
    )
    assert "interrupted run" in page.text("expt-job-result")
    assert not page.js("document.getElementById('expt-model-create').disabled")
    assert page.js(
        "document.getElementById('expt-model-wait').classList.contains('d-none')"
    )
    assert page.js(
        "document.getElementById('expt-job-cancel').classList.contains('d-none')"
    )


def test_a_quick_fit_with_aure_is_started_for_the_run_chosen(
    page: Page, site: Site, project: Path, monkeypatch
) -> None:
    import sys

    from nr_workbench.web import jobs as jobs_module

    # Each step says what it was asked, and succeeds: what is tested is the
    # page -- the run chosen, the job followed -- not AuRE.
    monkeypatch.setattr(
        jobs_module,
        "nrw_command",
        lambda *args: [sys.executable, "-c", f"print({' '.join(args)!r})"],
    )
    steady = project / "samples" / "S1" / "data" / "steady"
    write_partials(steady, 234277)
    write_partials(steady, 234280)
    open_s1(page, site)
    page.wait_for(
        "!document.getElementById('expt-model-run').classList.contains('d-none')",
        what="the choice of run",
    )
    page.js("document.getElementById('expt-model-run').value = '234280'")
    page.type("expt-model-name", "auto")
    page.click("expt-model-quick")

    page.wait_for(
        "document.getElementById('expt-job-state').textContent === 'ok'",
        what="the quick fit ended",
    )
    assert page.text("expt-job-title") == "Quick fit of auto with AuRE (S1)"
    log = page.text("expt-job-log")
    assert "aure new --name=auto --run=234280 -- S1" in log
    assert log.index("aure run") < log.index("aure import") < log.index("fit run")


def long_step(monkeypatch) -> None:
    """Every step runs until it is stopped, standing in for a long DREAM run."""
    import sys

    from nr_workbench.web import jobs as jobs_module

    monkeypatch.setattr(
        jobs_module,
        "nrw_command",
        lambda *args: [sys.executable, "-c", "import time; time.sleep(60)"],
    )


def test_a_page_opened_onto_a_running_fit_follows_it_and_can_cancel_it(
    page: Page, served, project: Path, monkeypatch
) -> None:
    site, app = served
    long_step(monkeypatch)
    app.config["NRW_MODELS"].jobs.start(
        label="dream fit of oxide",
        sample="S1",
        model="oxide",
        steps=[["model", "generate", "x"], ["fit", "run", "x"]],
    )

    open_s1(page, site)

    page.wait_for(
        "document.getElementById('expt-job-state').textContent === 'step 1 of 2'",
        what="the running fit, shown",
    )
    assert page.text("expt-job-title") == "Dream fit of oxide (S1)"
    page.click("expt-job-cancel")
    page.wait_for(
        "document.getElementById('expt-job-state').textContent === 'cancelled'",
        what="the fit cancelled",
    )


def test_a_view_only_page_offers_no_writes_and_no_cancel(
    page: Page, project: Path, monkeypatch
) -> None:
    from nr_workbench.web.app import create_app

    long_step(monkeypatch)
    spec = project / "samples" / "S1" / "models" / "oxide.yaml"
    spec.parent.mkdir(parents=True)
    spec.write_text("{}\n")
    write_partials(project / "samples" / "S1" / "data" / "steady", 234277)
    app = create_app(
        project, autostart=False, writable=False, read_only_reason="view only"
    )
    app.config["NRW_EXPERIMENT"].live.scan_once()
    app.config["NRW_MODELS"].jobs.start(
        label="dream fit of oxide", sample="S1", model="oxide", steps=[["x"]]
    )

    with serving(app) as site:
        page.goto(f"{site.url}/experiment")
        page.wait_for(
            "Array.from(document.querySelectorAll('#expt-samples button'))"
            ".some(b => b.textContent.includes('S1'))",
            what="the sample list",
        )
        page.js(
            "Array.from(document.querySelectorAll('#expt-samples button'))"
            ".find(b => b.textContent.includes('S1')).click()"
        )
        page.wait_for(
            "document.querySelector('#expt-models-list .expt-model-fit') !== null",
            what="the spec listed",
        )
        page.wait_for(
            "document.getElementById('expt-job-state').textContent === 'step 1 of 1'",
            what="the running fit, shown",
        )

        assert "view-only" in page.text("expt-models-list")
        for button in ("expt-model-create", "expt-model-quick"):
            assert page.js(f"document.getElementById('{button}').disabled") is True
        # Nothing written, but nothing to write it with: left folded away.
        assert page.js("document.getElementById('expt-context').open") is False
        for box in ("c-summary", "c-hypotheses", "c-notes", "expt-context-save"):
            assert page.js(f"document.getElementById('{box}').disabled") is True
    assert page.js(
        "document.querySelector('#expt-models-list .expt-model-fit').disabled"
    )
    assert page.js(
        "document.getElementById('expt-job-cancel').classList.contains('d-none')"
    )


def test_a_model_aure_proposed_offers_a_quick_fit_again(
    page: Page, site: Site, project: Path, monkeypatch
) -> None:
    from nr_workbench.web import jobs as jobs_module

    from .test_web_models import unedited_proposal

    monkeypatch.setattr(
        jobs_module, "nrw_command", lambda *args: [sys.executable, "-c", "pass"]
    )
    write_partials(project / "samples" / "S1" / "data" / "steady", 234277)
    spec = project / "samples" / "S1" / "models" / "auto.yaml"
    spec.parent.mkdir(parents=True)
    spec.write_text(unedited_proposal(), encoding="utf-8")
    open_s1(page, site)
    page.wait_for(
        "document.querySelector('#expt-models-list .expt-model-again') !== null",
        what="the quick fit again button",
    )

    page.js("document.querySelector('#expt-models-list .expt-model-again').click()")

    page.wait_for(
        "document.getElementById('expt-job-state').textContent === 'ok'",
        what="the quick fit ended",
    )
    assert page.text("expt-job-title") == "Quick fit of auto with AuRE again (S1)"


def test_a_finished_job_is_not_shown_again_and_close_puts_one_away(
    page: Page, served, project: Path, monkeypatch
) -> None:
    from nr_workbench.web import jobs as jobs_module

    site, app = served
    monkeypatch.setattr(
        jobs_module, "nrw_command", lambda *args: [sys.executable, "-c", "pass"]
    )
    runner = app.config["NRW_MODELS"].jobs
    runner.start(label="amoeba fit of oxide", sample="S1", model="oxide", steps=[["x"]])
    deadline = time.monotonic() + 20
    while runner.current().status == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
    spec = project / "samples" / "S1" / "models" / "oxide.yaml"
    spec.parent.mkdir(parents=True)
    spec.write_text("{}\n")
    (project / "samples" / "S1" / "data" / "steady").mkdir(parents=True)

    open_s1(page, site)
    page.wait_for(
        "document.querySelector('#expt-models-list .expt-model-fit') !== null",
        what="the spec listed",
    )
    # What ended before the page opened is in Fits, not here.
    assert page.js(
        "document.getElementById('expt-job-panel').classList.contains('d-none')"
    )

    page.js("document.querySelector('#expt-models-list .expt-model-fit').click()")
    page.click("expt-fit-run")
    page.wait_for(
        "document.getElementById('expt-job-state').textContent === 'ok'",
        what="the fit ended",
    )
    assert "All fits" in page.text("expt-job-result")
    page.click("expt-job-close")

    assert page.js(
        "document.getElementById('expt-job-panel').classList.contains('d-none')"
    )


def open_the_fit_form(page: Page, site: Site, project: Path) -> None:
    spec = project / "samples" / "S1" / "models" / "oxide.yaml"
    spec.parent.mkdir(parents=True)
    spec.write_text("{}\n")
    (project / "samples" / "S1" / "data" / "steady").mkdir(parents=True)
    open_s1(page, site)
    page.wait_for(
        "document.querySelector('#expt-models-list .expt-model-fit') !== null",
        what="the spec listed",
    )
    page.js("document.querySelector('#expt-models-list .expt-model-fit').click()")


def box(page: Page, element_id: str) -> dict:
    return page.js(
        f"(() => {{ const b = document.getElementById('{element_id}');"
        " return {value: b.value, placeholder: b.placeholder,"
        " shown: !b.closest('.expt-fit-setting').classList.contains('d-none'),"
        " label: b.previousElementSibling.textContent}; })()"
    )


def choose(page: Page, method: str) -> None:
    page.js(
        f"{{ const m = document.getElementById('expt-fit-method'); m.value = '{method}';"
        " m.dispatchEvent(new Event('change')); }"
    )


def test_the_fit_forms_boxes_start_at_what_the_fit_would_use(
    page: Page, site: Site, project: Path
) -> None:
    with (project / "nrw.toml").open("a", encoding="utf-8") as handle:
        handle.write("\n[fit.dream]\nburn = 500\n")

    open_the_fit_form(page, site, project)

    # DREAM, nrw's default: samples bumps' own, burn the project's.
    assert box(page, "expt-fit-samples") == {
        "value": "10000",
        "placeholder": "",
        "shown": True,
        "label": "samples",
    }
    assert box(page, "expt-fit-burn")["value"] == "500"
    # DREAM's steps follow from its samples: no one number to show.
    assert box(page, "expt-fit-steps")["value"] == ""
    assert box(page, "expt-fit-steps")["placeholder"] == "from samples"
    said = page.text("expt-fit-defaults")
    assert "samples 10000, bumps' default" in said
    assert "burn 500 from nrw.toml" in said

    choose(page, "amoeba")

    assert box(page, "expt-fit-steps")["value"] == "1000"
    assert box(page, "expt-fit-samples")["shown"] is False


def test_the_fit_form_sends_only_what_was_changed(
    page: Page, site: Site, project: Path, monkeypatch
) -> None:
    from nr_workbench.web import jobs as jobs_module

    monkeypatch.setattr(
        jobs_module, "nrw_command", lambda *args: [sys.executable, "-c", "pass"]
    )
    open_the_fit_form(page, site, project)
    choose(page, "amoeba")
    page.click("expt-fit-run")
    page.wait_for(
        "document.getElementById('expt-job-state').textContent === 'ok'",
        what="the unchanged fit",
    )
    unchanged = page.text("expt-job-log")

    page.js("document.querySelector('#expt-models-list .expt-model-fit').click()")
    choose(page, "amoeba")
    page.type("expt-fit-steps", "7")
    page.click("expt-fit-run")
    page.wait_for(
        "document.getElementById('expt-job-log').textContent.includes('--steps=7')",
        what="the changed fit",
    )

    # Left as shown, the value is `nrw fit run`'s to take -- from the same place.
    assert "--method=amoeba --verbose\n" in unchanged
    assert "--steps" not in unchanged
