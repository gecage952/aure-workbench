"""The fit and Fits pages in a real browser: curating fits.

The API tests prove what the server allows; these prove the pages offer it and
act on the answers: a star, a finalization with its reason, a discard that the
page then offers to undo or to follow with deleting the files -- only once the
person confirmed -- and the Fits list's stars and its toggle for the discarded.
Each also fails on any script error or security violation the page raises,
which is how a script blocked by the pages' new policy would show.
"""

# The browser fixtures are imported and requested by name, which ruff reads
# as redefinitions.
# ruff: noqa: F811

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner

from nr_workbench.cli import main
from nr_workbench.project.layout import ProjectLayout
from nr_workbench.provenance import curation

from .test_curation import A, B, events, recorded
from .test_settings_page_browser import (  # noqa: F401 - fixtures
    Page,
    Site,
    browser,
    empty_home,
    page,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "analysis"
    assert (
        CliRunner().invoke(main, ["init", str(root), "--sample", "S1"]).exit_code == 0
    )
    recorded(root, A)
    recorded(root, B)
    return root


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
        # A test that failed mid-job leaves nothing running to slow the next.
        app.config["NRW_MODELS"].jobs.stop()
        app.config["NRW_EXPERIMENT"].reload()


@pytest.fixture
def site(project: Path) -> Iterator[Site]:
    from nr_workbench.web.app import create_app

    with serving(create_app(project, autostart=False)) as served:
        yield served


def open_page(page: Page, site: Site, path: str) -> None:
    page.goto(f"{site.url}{path}")


def text(page: Page, element_id: str) -> str:
    return page.js(
        f"(document.getElementById('{element_id}') || {{}}).textContent || ''"
    )


def test_a_fit_is_starred_and_finalized_and_the_answer_cannot_be_set_aside(
    page: Page, site: Site, project: Path
) -> None:
    open_page(page, site, f"/f/{A}")
    page.wait_for("document.getElementById('curate-star') !== null", what="the bar")

    page.click("curate-star")
    page.wait_for(
        "document.getElementById('fit-badges').textContent.includes('starred')",
        what="the star",
    )
    page.click("curate-finalize")
    assert "Why is this the answer for S1?" in text(page, "curate-question")
    page.type("curate-reason", "converged; the oxide is where the notes put it")
    page.click("curate-go")

    page.wait_for(
        "document.getElementById('fit-badges').textContent.includes('final')",
        what="the fit finalized",
    )
    assert text(page, "curate-finalize") == "Final"
    assert page.js("document.getElementById('curate-discard').disabled")
    (promotion,) = events(project, "promote")
    assert promotion["reason"] == "converged; the oxide is where the notes put it"


def test_a_fit_is_discarded_restored_and_its_files_go_only_when_confirmed(
    page: Page, site: Site, project: Path
) -> None:
    open_page(page, site, f"/f/{B}")
    page.wait_for("document.getElementById('curate-discard') !== null", what="the bar")

    page.click("curate-discard")
    page.type("curate-reason", "the Ti layer diverged")
    page.click("curate-go")
    page.wait_for(
        "document.getElementById('curate-restore') !== null", what="the discard"
    )
    assert "the Ti layer diverged" in text(page, "curate-discarded")

    page.click("curate-restore")
    page.wait_for(
        "document.getElementById('curate-discard') !== null", what="the restore"
    )
    assert page.js(
        "document.getElementById('curate-discarded').classList.contains('d-none')"
    )
    page.click("curate-discard")
    page.type("curate-reason", "the Ti layer diverged, again")
    page.click("curate-go")
    page.wait_for(
        "document.getElementById('curate-delete') !== null", what="discarded again"
    )

    page.js("window.confirm = () => false")
    before = text(page, "curate-status")
    page.click("curate-delete")
    # Declined, nothing was sent: a request says "…" before it is answered.
    assert text(page, "curate-status") == before
    assert (project / "samples" / "S1" / "results" / B).is_dir()

    page.js("window.confirm = () => true")
    page.click("curate-delete")
    page.wait_for(
        "document.getElementById('curate-status').textContent.startsWith('Deleted')",
        what="the files deleted",
    )
    assert not (project / "samples" / "S1" / "results" / B).exists()
    assert "The record that it ran stays" in text(page, "curate-status")


def test_the_fits_list_stars_and_keeps_the_discarded_behind_a_toggle(
    page: Page, site: Site, project: Path
) -> None:
    curation.discard(ProjectLayout(root=project), B, reason="diverged")
    open_page(page, site, "/fits")
    hidden = (
        "document.querySelector('tbody[data-discarded]').classList.contains('d-none')"
    )
    assert page.js(hidden)

    page.js("document.getElementById('fits-show-discarded').click()")
    assert not page.js(hidden)

    page.js(f"document.querySelector('button.fit-star[data-fit-id=\"{A}\"]').click()")
    page.wait_for(
        f"document.querySelector('button.fit-star[data-fit-id=\"{A}\"]')"
        ".getAttribute('aria-pressed') === 'true'",
        what="the star",
    )
    assert [e["fit_id"] for e in events(project, "star")] == [A]


def test_a_view_only_page_offers_nothing(page: Page, project: Path) -> None:
    from nr_workbench.web.app import create_app

    read_only = create_app(project, autostart=False, writable=False)
    with serving(read_only) as site:
        page.goto(f"{site.url}/f/{A}")
        page.wait_for("document.readyState === 'complete'", what="the page")

        assert page.js("document.getElementById('curate-buttons').children.length") == 0
        assert page.js("window.NRW_WRITER") is False


def test_the_isaac_panel_is_for_the_final_fit_and_asks_before_it_pushes(
    page: Page, site: Site, project: Path, tmp_path: Path, monkeypatch
) -> None:
    import sys

    from nr_workbench import env as env_module
    from nr_workbench.web import isaac as isaac_module
    from nr_workbench.web import jobs as jobs_module

    # The tools are an optional install: the panel is what is tested here.
    monkeypatch.setattr(isaac_module, "_installed", lambda name: True)

    settings = tmp_path / "nrw-settings"
    settings.write_text(
        "ISAAC_URL=https://isaac.example.org/api\nISAAC_KEY=k-1234567\n"
    )
    monkeypatch.setattr(env_module, "USER_ENV_PATH", settings)
    # Each step says what it was asked, and succeeds: what is tested is the
    # page -- the panel, the confirmation, the job followed -- not ISAAC.
    monkeypatch.setattr(
        jobs_module,
        "nrw_command",
        lambda *args: [sys.executable, "-c", f"print({' '.join(args)!r})"],
    )
    records = project / "samples" / "S1" / "results" / A / "isaac" / "records"
    records.mkdir(parents=True)
    (records / "isaac_record_state0.json").write_text("{}")
    open_page(page, site, f"/f/{A}")
    page.wait_for("document.getElementById('curate-finalize') !== null", what="the bar")
    # Its own answer first: hidden because it said so, not because it was slow.
    page.wait_for(
        "performance.getEntriesByType('resource').some(e => e.name.endsWith('/isaac'))",
        what="the panel's answer",
    )
    assert page.js("document.getElementById('isaac').classList.contains('d-none')")

    page.click("curate-finalize")
    page.type("curate-reason", "the answer")
    page.click("curate-go")
    page.wait_for(
        "!document.getElementById('isaac').classList.contains('d-none')",
        what="the panel, once the fit is final",
    )
    assert "isaac.example.org" in text(page, "isaac-setup")
    assert "k-1234567" not in text(page, "isaac")  # never the key

    page.click("isaac-export")
    page.wait_for(
        "document.getElementById('isaac-status').textContent.endsWith('done.')",
        what="the export's job",
    )
    assert f"isaac export -- {A}" in text(page, "isaac-log")

    page.js("window.confirm = () => false")
    page.wait_for(
        "!document.getElementById('isaac-push').disabled", what="push offered"
    )
    before = text(page, "isaac-status")
    page.click("isaac-push")
    # Declined, nothing was sent: a step says "…" before it is answered.
    assert text(page, "isaac-status") == before

    page.js("window.confirm = () => true")
    page.click("isaac-push")
    page.wait_for(
        "document.getElementById('isaac-log').textContent.includes('isaac push')",
        what="the push's job",
    )
    assert f"isaac push --yes -- {A}" in text(page, "isaac-log")


def test_a_fit_pushed_while_final_still_says_so_once_it_is_not(
    page: Page, site: Site, project: Path, monkeypatch
) -> None:
    from nr_workbench.provenance.index import FitIndex
    from nr_workbench.web import isaac as isaac_module

    monkeypatch.setattr(isaac_module, "_installed", lambda name: True)
    layout = ProjectLayout(root=project)
    curation.promote(layout, A, reason="the answer")
    index = FitIndex(project / ".nrw" / "index.jsonl")
    curation.record_publish(
        index,
        index.find(A),
        portal="https://isaac.example.org/api",
        files=[],
        records=[],
        complete=True,
    )
    curation.promote(layout, B, reason="a better answer")

    open_page(page, site, f"/f/{A}")
    page.wait_for(
        "document.getElementById('isaac-published').textContent.length > 0",
        what="the push, still listed",
    )

    assert "Pushed" in text(page, "isaac-published")
    assert "No longer the final fit" in text(page, "isaac-setup")
    assert page.js("document.getElementById('isaac-buttons').children.length") == 0
