"""The Settings page's API: read openly, save and check only with the link.

Saving writes nrw.toml, and checking a folder lists whatever path it is
given, so both sit behind the same gate as every other write. Reading does
not, and must never start the poller: the page exists to change a folder that
may not answer.
"""

# The web fixtures are imported from test_experiment_web and requested by
# name, which ruff reads as redefinitions.
# ruff: noqa: F811

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import pytest

from nr_workbench.project.config import load_config

from .experiment_fixtures import write_autoreduced
from .test_experiment_web import (  # noqa: F401 - fixtures
    ORIGIN,
    TOKEN,
    app,
    expt,
    make_app,
    write_headers,
    writer,
)


def put(client, app, **body):
    return client.put("/api/experiment/settings", json=body, headers=write_headers(app))


def revision(root: Path) -> str:
    return hashlib.sha256((root / "nrw.toml").read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def test_the_settings_say_what_is_set_what_is_default_and_what_is_coming(
    app, expt: Path
) -> None:
    payload = app.test_client().get("/api/experiment/settings").get_json()

    assert payload["revision"] == revision(expt)
    assert payload["values"]["ipts"] == "IPTS-00001"
    assert payload["values"]["source.location"] is not None
    assert payload["values"]["feed.poll_seconds"] is None
    assert payload["defaults"]["feed.poll_seconds"] == 30
    kinds = {o["kind"]: o["available"] for o in payload["options"]["feed"]}
    assert kinds == {"directory": True, "monitor": False}
    assert payload["effective"]["needs_setup"] is False


def test_the_catalog_is_kept_in_the_project_and_nothing_else_is_offered(
    app,
) -> None:
    """A store everyone writes was withdrawn: none is promised as coming."""
    payload = app.test_client().get("/api/experiment/settings").get_json()

    catalogs = {o["kind"]: o["available"] for o in payload["options"]["catalog"]}
    assert catalogs == {"parquet": True}
    assert payload["effective"]["catalog_kind"] == "parquet"


def test_an_unknown_catalog_is_refused_never_replaced_by_files(
    expt: Path,
) -> None:
    """Edits meant for everyone must not land in the project, where nobody looks."""
    with (expt / "nrw.toml").open("a", encoding="utf-8") as handle:
        handle.write('\n[experiment.catalog]\nkind = "api"\n')
    app = make_app(expt)
    client = app.test_client()
    client.get(f"/auth/{TOKEN}")

    overview = client.get("/api/experiment").get_json()
    saved = client.put(
        "/api/experiment/runs",
        json={
            "changes": [{"run": 234277, "base_rev": 0, "fields": {"sample_id": "S1"}}]
        },
        headers=write_headers(app),
    )

    said = [p["message"] for p in overview["problems"] if p["scope"] == "catalog"]
    assert any("kind = 'api' is not known" in m for m in said)
    assert overview["writable"] is False
    assert saved.status_code == 503
    assert not (expt / "experiment" / "runs.parquet").exists()
    app.config["NRW_EXPERIMENT"].reload()


def test_reading_the_settings_does_not_start_the_poller(expt: Path) -> None:
    from .experiment_fixtures import scan_threads

    app = make_polling_app(expt)
    try:
        before = scan_threads()

        page = app.test_client().get("/settings")
        api = app.test_client().get("/api/experiment/settings")

        assert (page.status_code, api.status_code) == (200, 200)
        assert scan_threads() - before == set()
    finally:
        app.config["NRW_EXPERIMENT"].reload()


def make_polling_app(root: Path):
    """An app whose poller starts on the first request that asks, as in `nrw serve`."""
    from nr_workbench.web.app import create_app

    return create_app(root, token=TOKEN, autostart=True)


def test_an_ipts_in_the_projects_path_is_offered(tmp_path: Path, monkeypatch) -> None:
    from click.testing import CliRunner

    from nr_workbench.cli import main

    root = tmp_path / "SNS" / "REF_L" / "IPTS-34347" / "shared" / "analysis"
    root.mkdir(parents=True)
    assert CliRunner().invoke(main, ["init", str(root)]).exit_code == 0
    app = make_app(root)
    try:
        payload = app.test_client().get("/api/experiment/settings").get_json()
    finally:
        app.config["NRW_EXPERIMENT"].reload()

    assert payload["suggested_ipts"] == "IPTS-34347"
    assert payload["effective"]["needs_setup"] is True


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------


def test_saving_without_the_link_is_refused(app, expt: Path) -> None:
    before = (expt / "nrw.toml").read_bytes()

    response = put(
        app.test_client(), app, revision=revision(expt), changes={"ipts": "IPTS-5"}
    )

    assert response.status_code == 403
    assert (expt / "nrw.toml").read_bytes() == before


def test_a_link_holder_saves_and_the_server_follows(
    app, writer, expt: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "elsewhere"
    write_autoreduced(folder, 234400, [1, 2, 3], planned=3, mtime=time.time() - 3600)

    response = put(
        writer, app, revision=revision(expt), changes={"source.location": str(folder)}
    )

    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["result"]["written"][0] == "nrw.toml"
    assert body["settings"]["values"]["source.location"] == str(folder)
    app.config["NRW_EXPERIMENT"].live.scan_once()
    overview = writer.get("/api/experiment").get_json()
    assert overview["source"]["path"] == str(folder)
    assert [row["run"] for row in overview["runs"]] == [234400]


def test_a_save_against_an_old_revision_is_a_409_and_changes_nothing(
    app, writer, expt: Path
) -> None:
    stale = revision(expt)
    toml = expt / "nrw.toml"
    toml.write_text(toml.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    before = toml.read_bytes()

    response = put(writer, app, revision=stale, changes={"ipts": "IPTS-5"})

    assert response.status_code == 409
    assert "changed since" in response.get_json()["error"]
    assert toml.read_bytes() == before


@pytest.mark.parametrize(
    ("body", "said"),
    [
        ({"changes": {"ipts": "IPTS-5"}}, "revision is required"),
        ({"revision": "REV", "changes": {}}, "non-empty object"),
        ({"revision": "REV", "changes": ["ipts", "IPTS-5"]}, "non-empty object"),
        (
            {"revision": "REV", "changes": {"ipts": "IPTS-5"}, "confirmed": "all"},
            "list of names",
        ),
        ({"revision": "REV", "changes": {"colour": "blue"}}, "colour"),
    ],
    ids=[
        "no-revision",
        "no-changes",
        "changes-a-list",
        "confirmed-a-string",
        "unknown",
    ],
)
def test_a_malformed_save_is_a_400_and_writes_nothing(
    app, writer, expt: Path, body: dict, said: str
) -> None:
    before = (expt / "nrw.toml").read_bytes()
    if body.get("revision") == "REV":
        body = {**body, "revision": revision(expt)}

    response = put(writer, app, **body)

    assert response.status_code == 400
    assert said in response.get_json()["error"]
    assert (expt / "nrw.toml").read_bytes() == before


def test_a_check_whose_body_is_not_an_object_is_a_400(app, writer) -> None:
    response = writer.post(
        "/api/experiment/settings/check", json=["/data"], headers=write_headers(app)
    )

    assert response.status_code == 400


def test_two_pages_saving_from_one_revision_write_the_first_and_refuse_the_second(
    app, writer, expt: Path
) -> None:
    """The refusal says it is a conflict, which is what makes the page reload."""
    shown = revision(expt)

    first = put(writer, app, revision=shown, changes={"feed.poll_seconds": 20})
    second = put(writer, app, revision=shown, changes={"feed.poll_seconds": 40})

    assert (first.status_code, second.status_code) == (200, 409)
    assert second.get_json()["kind"] == "TomlConflictError"
    assert load_config(expt).raw["experiment"]["feed"]["poll_seconds"] == 20


def test_two_saves_at_the_same_moment_write_one_and_refuse_the_other(
    app, expt: Path
) -> None:
    """Read, edit and write happen under one lock, so neither is lost silently."""
    from nr_workbench.project.tomlfile import TomlConflictError

    settings = app.config["NRW_SETTINGS"]
    shown = revision(expt)
    start = threading.Barrier(2)
    outcome: dict[int, str] = {}

    def save_poll(seconds: int) -> None:
        start.wait()
        try:
            settings.save_settings(shown, {"feed.poll_seconds": seconds})
            outcome[seconds] = "saved"
        except TomlConflictError:
            outcome[seconds] = "refused"

    threads = [threading.Thread(target=save_poll, args=(s,)) for s in (20, 40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(outcome.values()) == ["refused", "saved"]
    saved = next(seconds for seconds, said in outcome.items() if said == "saved")
    assert load_config(expt).raw["experiment"]["feed"]["poll_seconds"] == saved


def test_an_apply_reviewed_before_the_data_folder_changed_is_refused(
    app, writer, expt: Path, tmp_path: Path
) -> None:
    """The very same files, in another folder: the reviewed plan is not this one.

    Hard links, made before the review, so every file's version -- inode,
    size and times -- is the same in both folders, and only the source itself
    tells the two plans apart.
    """
    import os

    from .test_experiment_web import assign

    facility = Path(load_config(expt).raw["experiment"]["source"]["location"])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for path in facility.glob("REFL_*"):
        os.link(path, elsewhere / path.name)
    app.config["NRW_EXPERIMENT"].live.scan_once()  # linking touched each inode
    assert assign(writer, app).status_code == 200
    plan = writer.get("/api/experiment/apply").get_json()
    assert plan["writes"]
    moved = put(
        writer,
        app,
        revision=revision(expt),
        changes={"source.location": str(elsewhere)},
    )
    assert moved.status_code == 200, moved.get_json()
    # The new folder's poller has looked once, as it would have by the time
    # anyone clicked: a run is settled only once it is seen unchanged twice.
    app.config["NRW_EXPERIMENT"].live.scan_once()

    response = writer.post(
        "/api/experiment/apply",
        json={"plan_id": plan["plan_id"]},
        headers=write_headers(app),
    )

    assert response.status_code == 409
    assert response.get_json()["kind"] == "PlanChanged"
    assert not (expt / "samples" / "Sample6").exists()


def test_a_value_that_is_not_allowed_is_a_400_with_the_reason(
    app, writer, expt: Path
) -> None:
    response = put(
        writer, app, revision=revision(expt), changes={"feed.poll_seconds": 1}
    )

    assert response.status_code == 400
    assert "from 5 to 600" in response.get_json()["error"]


def test_a_file_nrw_cannot_edit_answers_with_the_lines_to_add(
    app, writer, expt: Path
) -> None:
    toml = expt / "nrw.toml"
    text = toml.read_text(encoding="utf-8")
    # The active table only: "# [experiment.source]" contains the header too.
    active = "\n[experiment.source]\n"
    assert text.count(active) == 1
    toml.write_text(
        text.replace(active, active + "settle_seconds = '''300'''\n"),
        encoding="utf-8",
    )

    response = put(
        writer, app, revision=revision(expt), changes={"source.settle_seconds": 60}
    )

    assert response.status_code == 409
    assert "settle_seconds = 60" in response.get_json()["lines"]


def test_changing_the_ipts_of_a_catalogued_experiment_asks_first(
    app, writer, expt: Path
) -> None:
    from .test_experiment_web import assign

    assert assign(writer, app).status_code == 200

    asked = put(writer, app, revision=revision(expt), changes={"ipts": "IPTS-2"})
    assert asked.status_code == 409
    assert asked.get_json()["needs"] == "ipts-change"
    assert load_config(expt).ipts == "IPTS-00001"

    done = put(
        writer,
        app,
        revision=revision(expt),
        changes={"ipts": "IPTS-2"},
        confirmed=["ipts-change"],
    )
    assert done.status_code == 200, done.get_json()
    assert load_config(expt).ipts == "IPTS-2"


# --------------------------------------------------------------------------
# Checking a folder
# --------------------------------------------------------------------------


def check(client, app, **body):
    return client.post(
        "/api/experiment/settings/check", json=body, headers=write_headers(app)
    )


def test_checking_a_folder_says_what_it_holds(app, writer, tmp_path: Path) -> None:
    folder = tmp_path / "facility" / "{ipts}"
    real = tmp_path / "facility" / "IPTS-00001"
    for run in (234277, 234280, 234283):
        write_autoreduced(real, run, [1], planned=1, mtime=time.time() - 3600)
    write_autoreduced(real, 234290, [1], planned=1, ipts="IPTS-99999")

    response = check(writer, app, location=str(folder))

    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["reachable"] and body["runs"] == 4
    assert (body["first"], body["last"]) == (234277, 234290)
    assert body["path"] == str(real)
    assert "IPTS-99999" in body["experiments"]
    other = next(run for run in body["newest"] if run["run"] == 234290)
    assert "IPTS-99999" in other["problems"][0]


def test_a_check_judges_the_headers_against_the_ipts_it_is_given(
    app, writer, tmp_path: Path
) -> None:
    """The page shows the server's judgement, so it must be the IPTS on the page."""
    folder = tmp_path / "facility" / "IPTS-99999"
    write_autoreduced(
        folder, 234290, [1], planned=1, ipts="IPTS-99999", mtime=time.time() - 3600
    )

    typed = check(writer, app, location=str(folder), ipts="IPTS-99999").get_json()
    saved = check(writer, app, location=str(folder)).get_json()

    assert typed["newest"][0]["problems"] == []
    assert "IPTS-00001" in saved["newest"][0]["problems"][0]


def test_checking_a_folder_reads_only_the_newest_headers(
    app, writer, tmp_path: Path, monkeypatch
) -> None:
    from nr_workbench.instrument import header as header_module

    folder = tmp_path / "many"
    for run in range(234000, 234020):
        write_autoreduced(folder, run, [1], planned=1, mtime=time.time() - 3600)
    reads: list[str] = []
    real = header_module.read_header_bytes
    monkeypatch.setattr(
        header_module,
        "read_header_bytes",
        lambda d, p: reads.append(p.name) or real(d, p),
    )

    body = check(writer, app, location=str(folder)).get_json()

    assert body["runs"] == 20
    assert len(reads) == 5


def test_checking_a_missing_folder_says_so(app, writer, tmp_path: Path) -> None:
    body = check(writer, app, location=str(tmp_path / "not-mounted")).get_json()

    assert body["reachable"] is False
    assert "does not exist" in body["problems"][0]["message"]


def test_checking_is_refused_without_the_link(app, tmp_path: Path) -> None:
    response = check(app.test_client(), app, location=str(tmp_path))

    assert response.status_code == 403


def test_a_check_reads_the_kind_of_source_the_save_would_set(
    app, writer, tmp_path: Path
) -> None:
    """Not the kind nrw.toml has now: a check must read what would be watched."""
    response = check(writer, app, location=str(tmp_path), kind="tiled")

    assert response.status_code == 400
    assert "'tiled' is not known" in response.get_json()["error"]


def test_checking_a_folder_inside_the_project_is_a_400(app, writer, expt: Path) -> None:
    response = check(writer, app, location=str(expt / "samples"))

    assert response.status_code == 400


def test_a_folder_that_never_answers_is_a_504_then_busy_then_fine_once_it_does(
    app, writer, tmp_path: Path, monkeypatch
) -> None:
    """A hard-mounted path that has gone away blocks; the request must not."""
    from nr_workbench.experiment.sources import local

    gate = threading.Event()
    real = local.LocalDirectorySource.probe
    monkeypatch.setattr(app.config["NRW_SETTINGS"].checks, "timeout", 0.2)
    monkeypatch.setattr(
        local.LocalDirectorySource,
        "probe",
        lambda self, **k: gate.wait() and real(self, **k),
    )
    try:
        first = check(writer, app, location=str(tmp_path))
        second = check(writer, app, location=str(tmp_path))
        third = check(writer, app, location=str(tmp_path))
    finally:
        gate.set()
        # The stuck checks finish once the "mount" answers, and give their
        # places back, so no later test inherits them.
        for thread in threading.enumerate():
            if thread.name == "nrw-folder-check":
                thread.join(timeout=5)
    fourth = check(writer, app, location=str(tmp_path))

    assert (first.status_code, second.status_code) == (504, 504)
    assert third.status_code == 409
    assert "already waiting" in third.get_json()["error"]
    assert fourth.status_code == 200 and fourth.get_json()["reachable"]


def test_checking_a_location_that_needs_the_ipts_without_one_is_a_400(
    app, writer, expt: Path
) -> None:
    from nr_workbench.project.settings import save

    save(expt, {"ipts": "", "source.location": None})

    response = check(writer, app, location="/SNS/REF_L/{ipts}/shared/x")

    assert response.status_code == 400
    assert "set the IPTS" in response.get_json()["error"]


@pytest.mark.parametrize(
    "call",
    [
        lambda data: data.check_folder("/data/x"),
        lambda data: data.save_settings("r", {"ipts": "IPTS-1"}),
    ],
    ids=["check", "save"],
)
def test_a_read_only_server_refuses_settings_writes_itself(expt: Path, call) -> None:
    """The second of two mechanisms: the data layer refuses, whatever the route did."""
    from nr_workbench.web.experiment import WritesDisabledError
    from nr_workbench.web.settings import SettingsData

    data = SettingsData(
        expt,
        writable=False,
        why_read_only="view only",
        count_runs=lambda: 0,
        watching=lambda: None,
        on_saved=lambda: None,
    )

    with pytest.raises(WritesDisabledError, match="view only"):
        call(data)


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------


def test_the_settings_page_renders_and_its_payload_parses(app) -> None:
    from .test_experiment_web import _embedded

    response = app.test_client().get("/settings")

    assert response.status_code == 200
    payload = _embedded(response.get_data(as_text=True), "NRW_SETTINGS")
    assert payload["schema"] == "nrw-settings/1"


def test_the_settings_page_loads_no_plotly_and_signs_its_scripts(app) -> None:
    import re

    response = app.test_client().get("/settings")
    html = response.get_data(as_text=True)
    policy = response.headers["Content-Security-Policy"]

    nonce = re.search(r"'nonce-([^']+)'", policy).group(1)
    assert f'nonce="{nonce}"' in html
    assert "cdn.plot.ly" not in policy and "unsafe-eval" not in policy
    # Neither the library nor the check that would announce it missing.
    assert "cdn.plot.ly" not in html and "typeof Plotly" not in html
    assert "frame-ancestors 'none'" in policy


def test_the_write_token_is_only_in_a_settings_page_for_a_link_holder(
    app, writer
) -> None:
    from .test_experiment_web import _embedded

    anonymous = app.test_client().get("/settings").get_data(as_text=True)
    holder = writer.get("/settings").get_data(as_text=True)

    assert _embedded(anonymous, "NRW_WRITE_TOKEN") == ""
    assert _embedded(holder, "NRW_WRITE_TOKEN") == app.config["NRW_PAGE_TOKEN"]


def test_a_hostile_label_cannot_break_out_of_the_settings_page(app, expt: Path) -> None:
    from nr_workbench.project.settings import save

    from .test_experiment_web import _embedded

    hostile = "x</script><script>alert(1)</script><!--<script>"
    save(expt, {"label": hostile})

    html = app.test_client().get("/settings").get_data(as_text=True)

    assert "<script>alert(1)" not in html
    assert _embedded(html, "NRW_SETTINGS")["values"]["label"] == hostile


def test_the_link_opens_settings_for_an_experiment_not_set_up(expt: Path) -> None:
    from nr_workbench.project.settings import save

    save(expt, {"ipts": "", "source.location": None})
    app = make_app(expt)
    try:
        response = app.test_client().get(f"/auth/{TOKEN}")
    finally:
        app.config["NRW_EXPERIMENT"].reload()

    assert response.status_code == 303
    assert response.headers["Location"].endswith("/settings")


def test_the_link_opens_the_experiment_once_it_is_set_up(app) -> None:
    response = app.test_client().get(f"/auth/{TOKEN}")

    assert response.headers["Location"].endswith("/experiment")


def test_the_experiment_page_says_when_it_needs_setting_up(app, expt: Path) -> None:
    from nr_workbench.project.settings import save

    assert app.test_client().get("/api/experiment").get_json()["needs_setup"] is False

    save(expt, {"ipts": "", "source.location": None})

    assert app.test_client().get("/api/experiment").get_json()["needs_setup"] is True


def test_serve_names_the_settings_page_and_says_when_setup_is_needed(
    expt: Path, monkeypatch
) -> None:
    from nr_workbench.project.settings import save

    from .test_experiment_web import _serve

    ready = _serve(expt, monkeypatch=monkeypatch)
    save(expt, {"ipts": "", "source.location": None})
    unready = _serve(expt, monkeypatch=monkeypatch)

    assert "/settings" in ready.output and "not set up yet" not in ready.output
    assert "not set up yet" in unready.output and "It opens Settings" in unready.output


def test_serve_says_which_folder_it_watches_and_why_it_is_the_default(
    expt: Path, monkeypatch
) -> None:
    """A folder typed into a comment once went unmentioned at start-up."""
    from nr_workbench.project.settings import save

    from .test_experiment_web import _serve

    configured = _serve(expt, monkeypatch=monkeypatch)
    save(expt, {"source.location": None})
    toml = expt / "nrw.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            '# location = "/SNS/REF_L/{ipts}/shared/autoreduce/new_reduction"',
            '# location = "/data/typed/here"',
        ),
        encoding="utf-8",
    )
    defaulted = _serve(expt, monkeypatch=monkeypatch)

    assert "Data folder  " in configured.output
    assert "(nrw's default)" not in configured.output
    assert "Data folder  /SNS/REF_L/IPTS-00001/" in defaulted.output
    assert "(nrw's default)" in defaulted.output
    assert "gives `location` in a comment" in defaulted.output


def test_init_suggests_serve_to_set_the_experiment_up(tmp_path: Path) -> None:
    from click.testing import CliRunner

    from nr_workbench.cli import main

    result = CliRunner().invoke(main, ["init", str(tmp_path / "new")])

    assert result.exit_code == 0, result.output
    assert "nrw serve" in result.output


def test_a_malformed_nrw_toml_does_not_cost_the_one_time_link(expt: Path) -> None:
    """Valid TOML of the wrong shape once made /auth fail after spending the link."""
    from nr_workbench.web import security

    app = make_app(expt)
    try:
        (expt / "nrw.toml").write_text('beamtime = "oops"\n', encoding="utf-8")
        response = app.test_client().get(f"/auth/{TOKEN}")
    finally:
        app.config["NRW_EXPERIMENT"].reload()

    assert response.status_code == 303
    assert response.headers["Location"].endswith("/settings")
    assert security.COOKIE in response.headers["Set-Cookie"]


def test_the_landing_page_is_chosen_before_the_link_is_spent(
    expt: Path, monkeypatch
) -> None:
    from nr_workbench.web.settings import SettingsData

    def broken(self):
        raise RuntimeError("anything at all")

    monkeypatch.setattr(SettingsData, "needs_setup", broken)
    app = make_app(expt)
    try:
        response = app.test_client().get(f"/auth/{TOKEN}")
    finally:
        app.config["NRW_EXPERIMENT"].reload()

    assert response.status_code == 303
    assert response.headers["Location"].endswith("/settings")


@pytest.mark.parametrize("name", ["x.html", "x.svg"])
def test_a_served_project_file_runs_nothing_as_this_server(
    app, expt: Path, name: str
) -> None:
    """A script in it would read a page's write token and pass the write gate."""
    folder = expt / "samples" / "Sample1" / "assessments" / "lbl"
    folder.mkdir(parents=True)
    (folder / name).write_text("<script>fetch('/settings')</script>", encoding="utf-8")

    response = app.test_client().get(f"/figures/Sample1/lbl/{name}")

    assert response.status_code == 200
    assert "sandbox" in response.headers["Content-Security-Policy"]


def test_checking_a_folder_reads_a_few_files_of_a_crowded_run(
    app, writer, tmp_path: Path, monkeypatch
) -> None:
    from nr_workbench.experiment.sources.local import PROBE_FILES_PER_RUN
    from nr_workbench.instrument import header as header_module

    folder = tmp_path / "crowded"
    write_autoreduced(folder, 234000, range(1, 31), planned=3, mtime=time.time() - 3600)
    reads: list[str] = []
    real = header_module.read_header_bytes
    monkeypatch.setattr(
        header_module,
        "read_header_bytes",
        lambda d, p: reads.append(p.name) or real(d, p),
    )

    body = check(writer, app, location=str(folder)).get_json()

    assert len(reads) == PROBE_FILES_PER_RUN
    assert any("30 files" in p["message"] for p in body["problems"])
