"""The Experiment page and its API: the server's first write surface.

Until now `nrw serve` only read, bound to loopback, and needed no
authentication. The tests here are mostly about who may write -- a browser
that opened the one-time link, from this machine, with a JSON body, from this
page's own origin -- and that everything else still only reads.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nr_workbench.web.app import _compact_json, create_app

from .experiment_fixtures import write_autoreduced

TOKEN = "one-time-secret-for-tests"
ORIGIN = "http://localhost"


@pytest.fixture
def expt(project: Path, tmp_path: Path) -> Path:
    """The scaffolded project, watching a folder with runs in three states."""
    folder = tmp_path / "facility" / "new_reduction"
    for run in (234277, 234280):
        write_autoreduced(folder, run, [1, 2, 3], planned=3, mtime=time.time() - 3600)
    write_autoreduced(folder, 234283, [1], planned=3, mtime=time.time() - 3600)
    with (project / "nrw.toml").open("a", encoding="utf-8") as handle:
        handle.write(f'\n[experiment.source]\nlocation = "{folder}"\n')
    return project


def make_app(root: Path, **kwargs):
    app = create_app(root, token=TOKEN, autostart=False, **kwargs)
    app.config["NRW_EXPERIMENT"].live.scan_once()
    return app


@pytest.fixture
def app(expt: Path):
    app = make_app(expt)
    yield app
    app.config["NRW_EXPERIMENT"].reload()


@pytest.fixture
def writer(app):
    """A client that opened the one-time link."""
    client = app.test_client()
    response = client.get(f"/auth/{TOKEN}")
    assert response.status_code == 303
    return client


def write_headers(app, **extra) -> dict[str, str]:
    headers = {"X-NRW-Token": app.config["NRW_PAGE_TOKEN"], "Origin": ORIGIN}
    headers.update(extra)
    return headers


def assign(client, app, run: int = 234277, base_rev: int = 0, **headers):
    return client.put(
        "/api/experiment/runs",
        json={
            "changes": [
                {"run": run, "base_rev": base_rev, "fields": {"sample_id": "Sample6"}}
            ]
        },
        headers=write_headers(app, **headers),
    )


# --------------------------------------------------------------------------
# The data layer needs no web framework
# --------------------------------------------------------------------------


def test_experiment_data_imports_without_flask() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, nr_workbench.web.experiment; "
            "assert 'flask' not in sys.modules; print('clean')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_api_mirrors_experiment_data(app) -> None:
    data = app.config["NRW_EXPERIMENT"]

    via_api = app.test_client().get("/api/experiment").get_json()
    direct = data.overview()

    for key in ("runs", "samples", "ipts", "cursor"):
        assert via_api[key] == direct[key], key


def test_overview_lists_runs_with_their_states(app) -> None:
    payload = app.test_client().get("/api/experiment").get_json()

    states = {row["run"]: row["state"] for row in payload["runs"]}
    assert states == {234277: "complete", 234280: "complete", 234283: "unconfirmed"}


def test_the_page_and_the_command_line_describe_a_run_identically(
    app, writer, expt: Path
) -> None:
    """A script reading `status --json` sees what a person sees on the page."""
    import json

    from click.testing import CliRunner

    from nr_workbench.cli import main

    assert assign(writer, app).status_code == 200
    page = app.test_client().get("/api/experiment").get_json()

    result = CliRunner().invoke(
        main, ["experiment", "status", "--root", str(expt), "--json"]
    )
    cli = json.loads(result.output)

    assert cli["runs"] == page["runs"]
    managed = [card for card in page["samples"] if card["managed"]]
    assert cli["samples"] == managed


def test_curves_come_from_the_source_segment_by_segment(app) -> None:
    payload = app.test_client().get("/api/experiment/runs/234277/curves").get_json()

    assert [c["label"] for c in payload["curves"]] == [
        "234277#1",
        "234277#2",
        "234277#3",
    ]
    assert all(c["run"] == 234277 for c in payload["curves"])


def test_the_quick_look_plots_the_segments_not_the_combined_curve(
    expt: Path, tmp_path: Path
) -> None:
    """Each segment keeps its own angle, even with an artifact among the files."""
    from nr_workbench.instrument.reduced import combined_filename

    from .experiment_fixtures import reduced_rows

    folder = tmp_path / "facility" / "new_reduction"
    combined = folder / combined_filename(234277)
    combined.write_text(reduced_rows(), encoding="utf-8")
    old = time.time() - 3600
    os.utime(combined, (old, old))
    app = make_app(expt)
    try:
        body = app.test_client().get("/api/experiment/runs/234277/curves").get_json()
        row = next(
            r
            for r in app.test_client().get("/api/experiment").get_json()["runs"]
            if r["run"] == 234277
        )
    finally:
        app.config["NRW_EXPERIMENT"].reload()

    assert [c["label"] for c in body["curves"]] == ["234277#1", "234277#2", "234277#3"]
    assert [round(c["theta"], 2) for c in body["curves"]] == [0.45, 1.25, 3.5]
    assert [a["role"] for a in row["artifacts"]] == ["segment"] * 3 + ["combined"]


def test_curves_for_an_unlisted_run_are_404(app) -> None:
    assert (
        app.test_client().get("/api/experiment/runs/999999/curves").status_code == 404
    )


@pytest.mark.parametrize(
    "error",
    [
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
        PermissionError(13, "Permission denied", "/private/path"),
        FileNotFoundError(2, "No such file", "/private/path"),
    ],
    ids=["a ValueError", "a PermissionError", "a FileNotFoundError"],
)
def test_an_unexpected_error_is_a_logged_500_not_the_callers_fault(
    app, monkeypatch, caplog, error: Exception
) -> None:
    """Once mapped to 400, 403 and 404 by class alone, and never logged."""

    def broken():
        raise error

    monkeypatch.setattr(app.config["NRW_EXPERIMENT"], "overview", broken)
    with caplog.at_level("ERROR"):
        response = app.test_client().get("/api/experiment")

    assert response.status_code == 500
    assert response.get_json()["kind"] == "InternalError"
    assert "/private/path" not in response.get_data(as_text=True)
    assert any(record.exc_info for record in caplog.records)


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------


def _embedded(html: str, name: str) -> object:
    match = re.search(rf"window\.{name} = (.*?);\n", html)
    assert match, name
    return json.loads(match.group(1))


def test_the_page_renders_and_its_payload_parses(app) -> None:
    response = app.test_client().get("/experiment")

    assert response.status_code == 200
    payload = _embedded(response.get_data(as_text=True), "NRW_EXPERIMENT")
    assert {row["run"] for row in payload["runs"]} == {234277, 234280, 234283}


def test_the_page_forbids_framing_and_scripts_it_did_not_sign(app) -> None:
    response = app.test_client().get("/experiment")

    policy = response.headers["Content-Security-Policy"]
    nonce = re.search(r"'nonce-([^']+)'", policy).group(1)
    assert "frame-ancestors 'none'" in policy
    assert f'nonce="{nonce}"' in response.get_data(as_text=True)
    assert response.headers["X-Frame-Options"] == "DENY"


def test_every_html_page_forbids_framing(app) -> None:
    response = app.test_client().get("/")

    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_the_write_token_is_only_in_a_page_served_to_a_link_holder(app, writer) -> None:
    anonymous = app.test_client().get("/experiment").get_data(as_text=True)
    holder = writer.get("/experiment").get_data(as_text=True)

    assert _embedded(anonymous, "NRW_WRITE_TOKEN") == ""
    assert _embedded(holder, "NRW_WRITE_TOKEN") == app.config["NRW_PAGE_TOKEN"]


def test_a_hostile_run_title_cannot_break_out_of_the_script_block(
    project: Path, tmp_path: Path
) -> None:
    folder = tmp_path / "facility" / "new_reduction"
    write_autoreduced(
        folder,
        234277,
        [1],
        planned=1,
        mtime=time.time() - 3600,
        stem="x</script><script>alert(1)</script><!--<script>",
    )
    with (project / "nrw.toml").open("a", encoding="utf-8") as handle:
        handle.write(f'\n[experiment.source]\nlocation = "{folder}"\n')
    app = make_app(project)

    html = app.test_client().get("/experiment").get_data(as_text=True)

    script = html.split("window.NRW_EXPERIMENT = ", 1)[1].split(";\n", 1)[0]
    assert "<" not in script and ">" not in script
    assert "alert(1)" in _embedded(html, "NRW_EXPERIMENT")["runs"][0]["title"]


def test_compact_json_escapes_everything_that_can_end_a_script() -> None:
    text = _compact_json({"t": "</script><!--<script>&\u2028"})

    assert not set("<>&\u2028") & set(text)
    assert json.loads(text) == {"t": "</script><!--<script>&\u2028"}


# --------------------------------------------------------------------------
# Who may write
# --------------------------------------------------------------------------


def test_a_link_holder_can_assign_runs(app, writer, expt: Path) -> None:
    response = assign(writer, app)

    assert response.status_code == 200, response.get_json()
    from nr_workbench.experiment.store import ParquetCatalogStore

    catalog = ParquetCatalogStore.for_project(expt).load()
    assert catalog.sample_ids() == ["Sample6"]


def test_a_write_without_the_link_is_refused(app) -> None:
    response = assign(app.test_client(), app)

    assert response.status_code == 403
    assert "link" in response.get_json()["error"]


def test_a_write_without_the_page_token_is_refused(app, writer) -> None:
    response = writer.put(
        "/api/experiment/runs", json={"changes": []}, headers={"Origin": ORIGIN}
    )

    assert response.status_code == 403


def test_a_text_plain_write_is_refused(app, writer) -> None:
    """A text/plain POST is a CORS 'simple request': no preflight protects it."""
    response = writer.put(
        "/api/experiment/runs",
        data='{"changes": []}',
        headers=write_headers(app, **{"Content-Type": "text/plain"}),
    )

    assert response.status_code == 415


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"Origin": "http://localhost:3000"}, id="another_localhost_port"),
        pytest.param({"Origin": "null"}, id="null_origin"),
        pytest.param({"Origin": "https://evil.example"}, id="another_site"),
        pytest.param({"Sec-Fetch-Site": "cross-site"}, id="cross_site_fetch"),
    ],
)
def test_a_write_from_another_origin_is_refused(app, writer, headers) -> None:
    response = assign(writer, app, **headers)

    assert response.status_code == 403


def test_a_write_from_another_machine_is_refused_even_claiming_localhost(app) -> None:
    client = app.test_client()
    client.set_cookie("nrw_session", TOKEN, domain="localhost")

    response = client.put(
        "/api/experiment/runs",
        json={"changes": []},
        headers=write_headers(app),
        environ_base={"REMOTE_ADDR": "10.0.0.5"},
    )

    assert response.status_code == 403


def test_an_ipv4_mapped_loopback_address_is_this_machine() -> None:
    from nr_workbench.web.security import is_loopback

    assert is_loopback("::ffff:127.0.0.1")
    assert is_loopback("::1")
    assert not is_loopback("::ffff:10.0.0.5")


def test_an_oversized_write_is_refused(app, writer) -> None:
    response = writer.put(
        "/api/experiment/runs",
        data=json.dumps({"changes": [], "pad": "x" * (2 * 1024 * 1024)}),
        headers=write_headers(app, **{"Content-Type": "application/json"}),
    )

    assert response.status_code == 413


def test_a_stale_revision_is_a_409_and_changes_nothing(app, writer) -> None:
    assert assign(writer, app).status_code == 200

    response = writer.put(
        "/api/experiment/runs",
        json={
            "changes": [{"run": 234277, "base_rev": 0, "fields": {"condition": "CA"}}]
        },
        headers=write_headers(app),
    )

    assert response.status_code == 409
    from nr_workbench.experiment.model import RunKey
    from nr_workbench.experiment.store import ParquetCatalogStore

    entry = (
        ParquetCatalogStore.for_project(app.config["NRW_ROOT"])
        .load()
        .runs[RunKey(234277)]
    )
    assert (entry.condition, entry.rev) == ("", 1)


def test_a_rule_broken_is_a_400_with_the_reason(app, writer) -> None:
    response = writer.put(
        "/api/experiment/runs",
        json={
            "changes": [{"run": 234277, "base_rev": 0, "fields": {"condition": "a|b"}}]
        },
        headers=write_headers(app),
    )

    assert response.status_code == 400
    assert "'|'" in response.get_json()["error"]


@pytest.mark.parametrize("base_rev", [True, -1, "0", 1.5])
def test_a_malformed_base_rev_is_a_400_and_changes_nothing(
    app, writer, expt: Path, base_rev
) -> None:
    """Checked once, where the catalog changes; `True` is an int in Python."""
    response = writer.put(
        "/api/experiment/runs",
        json={
            "changes": [
                {"run": 234277, "base_rev": base_rev, "fields": {"condition": "OCV"}}
            ]
        },
        headers=write_headers(app),
    )

    assert response.status_code == 400
    assert "whole number" in response.get_json()["error"]
    assert not (expt / "experiment" / "catalog.json").exists()


def test_the_page_cannot_set_the_title_it_came_from_the_source(app, writer) -> None:
    response = writer.put(
        "/api/experiment/runs",
        json={"changes": [{"run": 234277, "base_rev": 0, "fields": {"title": "mine"}}]},
        headers=write_headers(app),
    )

    assert response.status_code == 400


def test_a_corrupt_catalog_is_shown_read_only_and_refuses_writes(
    app, writer, expt: Path
) -> None:
    assert assign(writer, app).status_code == 200
    (expt / "experiment" / "runs.parquet").write_bytes(b"")

    overview = writer.get("/api/experiment").get_json()
    assert overview["writable"] is False
    assert overview["catalog"]["readable"] is False
    assert any(p["scope"] == "catalog" for p in overview["problems"])

    assert assign(writer, app, base_rev=1).status_code == 503


def test_a_read_only_server_refuses_writes_and_the_link(expt: Path) -> None:
    app = make_app(expt, writable=False, read_only_reason="testing read-only")
    client = app.test_client()

    assert client.get(f"/auth/{TOKEN}").status_code == 403
    client.set_cookie("nrw_session", TOKEN, domain="localhost")
    response = assign(client, app)
    assert response.status_code == 403
    assert "testing read-only" in response.get_json()["error"]


def test_a_server_bound_beyond_loopback_never_writes(expt: Path) -> None:
    app = make_app(expt, bound_host="0.0.0.0")
    client = app.test_client()

    assert client.get(f"/auth/{TOKEN}").status_code == 403
    # Everything else a write needs, forged: still refused.
    client.set_cookie("nrw_session", TOKEN, domain="localhost")
    assert assign(client, app).status_code == 403


# --------------------------------------------------------------------------
# Hosts, and read-only everywhere else
# --------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8765", "0.0.0.0:8765"])
def test_a_foreign_host_name_is_refused_even_for_reads(app, host: str) -> None:
    """DNS rebinding makes a hostile page same-origin; its Host still names it."""
    response = app.test_client().get("/api/experiment", headers={"Host": host})

    assert response.status_code == 400


@pytest.mark.parametrize("host", ["localhost:8765", "127.0.0.1:8765", "[::1]:8765"])
def test_loopback_host_names_are_served(app, host: str) -> None:
    assert (
        app.test_client().get("/api/experiment", headers={"Host": host}).status_code
        == 200
    )


def test_every_non_get_route_is_behind_the_write_gate(app) -> None:
    """The one blueprint that writes is the only one allowed to."""
    unsafe = {
        rule.endpoint
        for rule in app.url_map.iter_rules()
        if rule.methods - {"GET", "HEAD", "OPTIONS"}
    }
    assert unsafe
    assert all(endpoint.startswith("experiment_api.") for endpoint in unsafe), unsafe


def test_reads_change_nothing_in_the_project(app, writer, expt: Path) -> None:
    """A GET can be triggered by an <img> tag; it must never write."""
    assert assign(writer, app).status_code == 200
    before = {
        p: p.stat().st_mtime_ns
        for p in expt.rglob("*")
        if p.is_file() and ".nrw/cache" not in p.as_posix()
    }
    for path in (
        "/api/experiment",
        "/api/experiment/changes?since=x",
        "/api/experiment/runs/234277/curves",
        "/api/experiment/samples/Sample6/preview",
        "/api/experiment/samples/Sample6/models",
        "/api/experiment/jobs/current",
        "/api/experiment/apply",
        "/api/experiment/settings",
        "/experiment",
    ):
        assert writer.get(path).status_code == 200, path

    after = {p: p.stat().st_mtime_ns for p in before}
    assert after == before
    assert not (expt / "samples" / "Sample6").exists()


# --------------------------------------------------------------------------
# Apply and preview through the API
# --------------------------------------------------------------------------


def test_apply_through_the_api_copies_the_reviewed_plan(
    app, writer, expt: Path
) -> None:
    assert assign(writer, app).status_code == 200
    plan = writer.get("/api/experiment/apply").get_json()
    assert plan["writes"]

    response = writer.post(
        "/api/experiment/apply",
        json={"plan_id": plan["plan_id"]},
        headers=write_headers(app),
    )

    assert response.status_code == 200, response.get_json()
    steady = expt / "samples" / "Sample6" / "data" / "steady"
    assert len(list(steady.glob("REFL_234277_*"))) == 3


def test_apply_with_a_stale_plan_is_a_409(app, writer) -> None:
    assert assign(writer, app).status_code == 200

    response = writer.post(
        "/api/experiment/apply", json={"plan_id": "stale"}, headers=write_headers(app)
    )

    assert response.status_code == 409


def test_the_preview_shows_what_sample_md_would_become(app, writer) -> None:
    assert assign(writer, app).status_code == 200

    preview = writer.get("/api/experiment/samples/Sample6/preview").get_json()

    assert preview["state"] == "create"
    assert "| 234277 |" in preview["markdown"]
    assert "<table>" in preview["html"]


@pytest.mark.parametrize("sample_id", ["S.1", "-S1", "con"])
def test_an_unusable_sample_id_is_refused_on_every_route(
    app, writer, sample_id
) -> None:
    """Ids the router accepts, so the refusal is validate_sample_id's own.

    Every Experiment API route with a sample id in it, found in the app's own
    map: one added later is covered without being listed here. On some, the id
    reaches a child's command line. (The read-only project API answers 404 for
    a sample that is not on disk, and runs nothing.)
    """
    routes = [
        (method, rule.rule)
        for rule in app.url_map.iter_rules()
        if rule.rule.startswith("/api/experiment/") and "<sample_id>" in rule.rule
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"})
    ]
    assert len(routes) >= 8, routes  # found, not empty by accident
    for method, rule in routes:
        path = rule.replace("<sample_id>", sample_id).replace("<name>", "oxide")
        response = writer.open(path, method=method, json={}, headers=write_headers(app))
        assert response.status_code == 400, (
            method,
            path,
            response.get_data(as_text=True),
        )
        assert response.is_json


# --------------------------------------------------------------------------
# `nrw serve`
# --------------------------------------------------------------------------


def _serve(expt: Path, *args: str, env: dict[str, str] | None = None, monkeypatch=None):
    from click.testing import CliRunner
    from flask import Flask

    from nr_workbench.cli import main

    captured: dict[str, Flask] = {}

    def run(self, **kwargs) -> None:
        del kwargs
        captured["app"] = self

    monkeypatch.setattr(Flask, "run", run)
    # setenv, not delenv: delenv on an absent variable registers no undo, and
    # run_serve then sets it for every later test on this worker.
    monkeypatch.setenv("NRW_SERVE_TOKEN", "")
    result = CliRunner().invoke(
        main, ["serve", "--root", str(expt), *args], env=env or {}
    )
    result.app = captured.get("app")  # type: ignore[attr-defined]
    return result


def test_serve_loads_no_env_file_into_its_own_environment(
    expt: Path, monkeypatch
) -> None:
    """Flask's app.run loads the nearest .env and .flaskenv, walking up from where
    it is started. The project's .env, which anyone who can commit to it can
    write, then read as the person's own shell: it chose where their ISAAC key
    was sent. Only the socket server is replaced here; Flask's run is real."""
    import werkzeug.serving
    from click.testing import CliRunner

    from nr_workbench.cli import main
    from nr_workbench.commands.isaac_cmd import portal_settings

    planted = ("ISAAC_URL", "TILED_URL", "NRW_PLANTED_ABOVE")
    (expt / ".env").write_text(
        "ISAAC_URL=https://elsewhere.example/portal/api\n"
        "TILED_URL=https://elsewhere.example\n",
        encoding="utf-8",
    )
    # Flask looks for a .flaskenv above the project too, to the root.
    (expt.parent / ".flaskenv").write_text("NRW_PLANTED_ABOVE=1\n", encoding="utf-8")
    started: list[tuple] = []
    monkeypatch.setattr(
        werkzeug.serving, "run_simple", lambda *args, **kwargs: started.append(args)
    )
    monkeypatch.setenv("NRW_SERVE_TOKEN", "")
    # The shell's own say-so, which Flask would honour over load_dotenv=False.
    monkeypatch.setenv("FLASK_SKIP_DOTENV", "0")
    monkeypatch.chdir(expt)
    try:
        result = CliRunner().invoke(main, ["serve", "--root", str(expt)])

        assert result.exit_code == 0, result.output
        assert started, "the server was never started"
        assert [name for name in planted if name in os.environ] == []
        url, _, ignored = portal_settings(expt)
        assert url is None and ignored == ["ISAAC_URL"]
    finally:
        for name in planted:  # what a failure loaded must not reach a later test
            os.environ.pop(name, None)


def test_serve_prints_a_one_time_link_that_works(expt: Path, monkeypatch) -> None:
    result = _serve(expt, monkeypatch=monkeypatch)

    assert result.exit_code == 0, result.output
    path = re.search(r"http://[^/]+(/auth/\S+)", result.output).group(1)
    client = result.app.test_client()
    assert client.get(path).status_code == 303
    assert client.get(path).status_code == 403  # and only once


def test_serve_beyond_loopback_says_view_only_and_prints_no_link(
    expt: Path, monkeypatch
) -> None:
    result = _serve(expt, "--host", "0.0.0.0", monkeypatch=monkeypatch)

    assert result.exit_code == 0, result.output
    assert "/auth/" not in result.output
    assert "not edited" in result.output


def test_serve_under_an_agent_accepts_no_edits(expt: Path, monkeypatch) -> None:
    result = _serve(expt, env={"NRW_AGENT": "1"}, monkeypatch=monkeypatch)

    assert "/auth/" not in result.output
    assert "does not accept edits" in result.output


def test_serve_debug_beyond_loopback_is_refused(expt: Path, monkeypatch) -> None:
    """The Werkzeug debugger runs whatever it is sent."""
    result = _serve(expt, "--debug", "--host", "0.0.0.0", monkeypatch=monkeypatch)

    assert result.exit_code != 0
    assert "run code on this machine" in result.output


# --------------------------------------------------------------------------
# The one-time link
# --------------------------------------------------------------------------


def test_a_wrong_link_is_refused_and_grants_nothing(app) -> None:
    client = app.test_client()

    response = client.get("/auth/not-the-token")

    assert response.status_code == 403
    assert "Set-Cookie" not in response.headers
    assert assign(client, app).status_code == 403


def test_the_link_sets_an_httponly_strict_cookie_that_is_not_the_link(app) -> None:
    """The secret travels in a URL and may be logged; the cookie must not be it."""
    header = app.test_client().get(f"/auth/{TOKEN}").headers["Set-Cookie"]

    assert "HttpOnly" in header and "SameSite=Strict" in header and "Max-Age" in header
    value = header.split(";", 1)[0].split("=", 1)[1]
    assert value and value != TOKEN


def test_the_link_works_once(app, writer) -> None:
    second = app.test_client().get(f"/auth/{TOKEN}")

    assert second.status_code == 403
    assert "already been used" in second.get_data(as_text=True)
    # The browser that did open it keeps working.
    assert assign(writer, app).status_code == 200


def test_the_link_from_another_machine_is_refused_and_sets_no_cookie(app) -> None:
    response = app.test_client().get(
        f"/auth/{TOKEN}", environ_base={"REMOTE_ADDR": "10.0.0.5"}
    )

    assert response.status_code == 403
    assert "Set-Cookie" not in response.headers


def test_the_request_log_never_shows_the_link(app) -> None:
    import logging

    from nr_workbench.web import security

    assert security._REDACTOR in logging.getLogger("werkzeug").filters
    record = logging.LogRecord(
        "werkzeug",
        logging.INFO,
        "x",
        1,
        '"GET /auth/%s HTTP/1.1" 303 -',
        (TOKEN,),
        None,
    )
    security._REDACTOR.filter(record)
    assert TOKEN not in record.getMessage()
    assert "/auth/[redacted]" in record.getMessage()


# --------------------------------------------------------------------------
# The sample-context and adopt routes
# --------------------------------------------------------------------------


def put_sample(client, app, sample: str = "Sample6", **body):
    return client.put(
        f"/api/experiment/samples/{sample}", json=body, headers=write_headers(app)
    )


def test_the_fits_to_perform_are_written_and_previewed(app, writer) -> None:
    """This route is how an unattended session's task gets written."""
    assert assign(writer, app).status_code == 200

    response = put_sample(
        writer, app, base_rev=0, fields={"fits_to_perform": "Co-refine both."}
    )

    assert response.status_code == 200, response.get_json()
    preview = writer.get("/api/experiment/samples/Sample6/preview").get_json()
    assert "Co-refine both." in preview["markdown"]


def test_a_stale_sample_edit_is_a_409(app, writer) -> None:
    assert put_sample(writer, app, base_rev=0, fields={"title": "A"}).status_code == 200

    assert put_sample(writer, app, base_rev=0, fields={"title": "B"}).status_code == 409


def test_a_heading_in_the_fits_is_a_400_with_the_reason(app, writer) -> None:
    response = put_sample(
        writer, app, base_rev=0, fields={"fits_to_perform": "x\n## Details\ny"}
    )

    assert response.status_code == 400
    assert "heading" in response.get_json()["error"]


def test_a_sample_with_runs_cannot_be_removed(app, writer) -> None:
    assert assign(writer, app).status_code == 200
    assert put_sample(writer, app, base_rev=0, fields={"title": "A"}).status_code == 200

    assert put_sample(writer, app, base_rev=1, delete=True).status_code == 400


def test_a_sample_already_on_disk_cannot_be_removed(app, writer, expt: Path) -> None:
    assert put_sample(writer, app, base_rev=0, fields={"title": "A"}).status_code == 200
    (expt / "samples" / "Sample6").mkdir(parents=True)

    response = put_sample(writer, app, base_rev=1, delete=True)

    assert response.status_code == 400
    assert "release" in response.get_json()["error"]


def _hand_written(expt: Path, extra: str = "") -> Path:
    path = expt / "samples" / "Sample9" / "sample.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "# S9\n\n## Description\n\nHand written.\n" + extra, encoding="utf-8"
    )
    return path


def test_adopt_needs_a_real_boolean_and_the_reviewed_plan(
    app, writer, expt: Path
) -> None:
    _hand_written(expt)
    plan = writer.get("/api/experiment/samples/Sample9/adopt").get_json()
    url = "/api/experiment/samples/Sample9/adopt"

    bad = writer.post(
        url,
        json={"rewrite": "yes", "plan_id": plan["plan_id"]},
        headers=write_headers(app),
    )
    stale = writer.post(
        url, json={"rewrite": False, "plan_id": "stale"}, headers=write_headers(app)
    )

    assert bad.status_code == 400
    assert stale.status_code == 409


def test_adopt_refuses_a_rewrite_that_would_lose_text(app, writer, expt: Path) -> None:
    path = _hand_written(expt, "\n## Notes\n\nKeep me.\n")
    before = path.read_bytes()
    plan = writer.get("/api/experiment/samples/Sample9/adopt").get_json()

    response = writer.post(
        "/api/experiment/samples/Sample9/adopt",
        json={"rewrite": True, "plan_id": plan["plan_id"]},
        headers=write_headers(app),
    )

    assert response.status_code == 409
    assert path.read_bytes() == before


# --------------------------------------------------------------------------
# The experiment's own context
# --------------------------------------------------------------------------


def put_context(client, app, **body):
    return client.put("/api/experiment/context", json=body, headers=write_headers(app))


def test_the_experiments_context_is_saved_and_on_the_page_once_loaded(
    app, writer
) -> None:
    assert writer.get("/api/experiment").get_json()["experiment"] == {
        "summary": "",
        "hypotheses": "",
        "notes": "",
        "rev": 0,
    }

    response = put_context(
        writer, app, base_rev=0, fields={"summary": "Li plating at a low rate."}
    )

    assert response.status_code == 200, response.get_json()
    loaded = writer.get("/api/experiment").get_json()["experiment"]
    assert (loaded["summary"], loaded["rev"]) == ("Li plating at a low rate.", 1)
    assert response.get_json()["experiment"] == loaded


def test_a_stale_experiment_edit_is_a_409_and_changes_nothing(app, writer) -> None:
    first = put_context(writer, app, base_rev=0, fields={"summary": "A"})
    assert first.status_code == 200

    response = put_context(writer, app, base_rev=0, fields={"summary": "B"})

    assert response.status_code == 409
    assert "the experiment" in response.get_json()["error"]
    assert writer.get("/api/experiment").get_json()["experiment"]["summary"] == "A"


def test_a_heading_in_the_experiments_summary_is_a_400_with_the_reason(
    app, writer
) -> None:
    response = put_context(writer, app, base_rev=0, fields={"summary": "x\n## Aims\ny"})

    assert response.status_code == 400
    assert "heading" in response.get_json()["error"]
    assert writer.get("/api/experiment").get_json()["experiment"]["rev"] == 0


@pytest.mark.parametrize(
    ("body", "says"),
    [
        ({"base_rev": 0}, "'fields' must be a non-empty object"),
        ({"base_rev": 0, "fields": {}}, "'fields' must be a non-empty object"),
        ({"base_rev": 0, "fields": {"title": "x"}}, "the experiment has no field"),
        ({"base_rev": "0", "fields": {"summary": "x"}}, "a base revision must be"),
        # JSON can carry half a surrogate pair, which no encoding stores.
        ({"base_rev": 0, "fields": {"summary": chr(0xD800)}}, "a lone surrogate"),
    ],
    ids=["no-fields", "empty-fields", "unknown-field", "text-rev", "surrogate"],
)
def test_a_malformed_experiment_edit_is_a_400(app, writer, body, says) -> None:
    response = put_context(writer, app, **body)

    assert response.status_code == 400
    assert says in response.get_json()["error"]


# --------------------------------------------------------------------------
# A dead data mount costs a request a timeout, never a thread
# --------------------------------------------------------------------------


@pytest.fixture
def dead_mount(app, monkeypatch):
    """Every read and listing of the source blocks until released."""
    import threading

    from nr_workbench.experiment.sources.local import LocalDirectorySource
    from nr_workbench.web import experiment as experiment_module

    gate = threading.Event()
    monkeypatch.setattr(experiment_module, "SOURCE_TIMEOUT", 0.3)
    # Reads keep the deadline their configuration was built with, so build
    # again under the short one -- and list once, as the page would have
    # before the mount went away.
    data = app.config["NRW_EXPERIMENT"]
    data.reload()
    data.live.scan_once()
    monkeypatch.setattr(
        LocalDirectorySource, "read_bytes", lambda self, f, max_bytes: gate.wait(30)
    )
    monkeypatch.setattr(LocalDirectorySource, "inventory", lambda self: gate.wait(30))
    yield gate
    gate.set()


def test_a_quick_look_on_a_dead_mount_is_a_504(app, dead_mount) -> None:
    started = time.monotonic()

    response = app.test_client().get("/api/experiment/runs/234277/curves")

    assert response.status_code == 504 and response.is_json
    assert time.monotonic() - started < 5


def test_an_apply_on_a_dead_mount_is_a_504(app, writer, dead_mount) -> None:
    dead_mount.set()  # let the assignment's own snapshot read proceed
    assert assign(writer, app).status_code == 200
    plan = writer.get("/api/experiment/apply").get_json()
    dead_mount.clear()
    started = time.monotonic()

    response = writer.post(
        "/api/experiment/apply",
        json={"plan_id": plan["plan_id"]},
        headers=write_headers(app),
    )

    assert response.status_code == 504 and response.is_json
    assert time.monotonic() - started < 5


@pytest.mark.parametrize("how", ["replaced with its times kept", "chmod"])
def test_a_file_touched_but_not_changed_can_still_be_read(
    app, expt: Path, how: str
) -> None:
    """Its size and mtime as they were, its inode or change time not."""
    import shutil

    from nr_workbench.project.config import load_config

    folder = Path(load_config(expt).raw["experiment"]["source"]["location"])
    target = folder / "REFL_234277_1_234277_autoreduction.dat"
    before = target.stat()
    if how == "chmod":
        target.chmod(0o640)
    else:  # what `rsync -t` and `cp -p` do
        copy = target.with_name("incoming.part")
        shutil.copy2(target, copy)
        os.replace(copy, target)
    after = target.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    app.config["NRW_EXPERIMENT"].live.scan_once()

    looked = app.test_client().get("/api/experiment/runs/234277/curves").get_json()

    assert looked["problems"] == []
    assert len(looked["curves"]) == 3


def short_deadline(app, monkeypatch):
    """The app's reads, rebuilt with a deadline a test can wait out, and listed once."""
    from nr_workbench.web import experiment as experiment_module

    monkeypatch.setattr(experiment_module, "SOURCE_TIMEOUT", 0.3)
    data = app.config["NRW_EXPERIMENT"]
    data.reload()
    data.live.scan_once()
    return data


def test_an_apply_while_the_poller_is_stuck_on_a_dead_mount_is_a_504(
    app, writer, monkeypatch
) -> None:
    """The background poll holds its lock while it waits; apply says so, not a 500."""
    import threading

    from nr_workbench.experiment.sources.local import LocalDirectorySource

    assert assign(writer, app).status_code == 200
    plan = writer.get("/api/experiment/apply").get_json()
    data = short_deadline(app, monkeypatch)
    gate, entered = threading.Event(), threading.Event()
    real = LocalDirectorySource.inventory

    def stuck(self):
        entered.set()
        gate.wait(30)
        return real(self)

    monkeypatch.setattr(LocalDirectorySource, "inventory", stuck)
    poll = threading.Thread(target=data.live.scan_once, daemon=True)
    poll.start()
    assert entered.wait(5)  # the poll now holds the scan lock
    try:
        started = time.monotonic()
        response = writer.post(
            "/api/experiment/apply",
            json={"plan_id": plan["plan_id"]},
            headers=write_headers(app),
        )
        waited = time.monotonic() - started
    finally:
        gate.set()
        poll.join(timeout=5)

    assert response.status_code == 504, response.get_json()
    assert "poll of the data source has been running" in response.get_json()["error"]
    assert waited < 5


def test_quick_looks_cannot_use_up_the_reads_an_apply_needs(
    app, writer, expt: Path, monkeypatch
) -> None:
    """Anyone who can see the pages can ask for a quick look; only a link holder applies."""
    import threading

    from nr_workbench.experiment.sources.local import LocalDirectorySource
    from nr_workbench.web.experiment import READ_SLOTS

    assert assign(writer, app).status_code == 200
    plan = writer.get("/api/experiment/apply").get_json()
    short_deadline(app, monkeypatch)
    gate = threading.Event()
    real = LocalDirectorySource.read_bytes
    hung: list[str] = []

    def read_bytes(self, file, *, max_bytes):
        if len(hung) < READ_SLOTS:  # the first few hang, as on a dead mount
            hung.append(file.name)
            gate.wait(30)
        return real(self, file, max_bytes=max_bytes)

    monkeypatch.setattr(LocalDirectorySource, "read_bytes", read_bytes)
    viewer = app.test_client()
    try:
        looks = [
            viewer.get("/api/experiment/runs/234277/curves") for _ in range(READ_SLOTS)
        ]
        busy = viewer.get("/api/experiment/runs/234277/curves")
        applied = writer.post(
            "/api/experiment/apply",
            json={"plan_id": plan["plan_id"]},
            headers=write_headers(app),
        )
    finally:
        gate.set()
        for thread in threading.enumerate():
            if thread.name == "nrw-source-read":
                thread.join(timeout=5)

    assert [look.status_code for look in looks] == [504] * READ_SLOTS
    # Said once, for the request -- not once per segment in a 200.
    assert busy.status_code == 409 and busy.get_json()["kind"] == "Busy"
    assert applied.status_code == 200, applied.get_json()
    steady = expt / "samples" / "Sample6" / "data" / "steady"
    assert len(list(steady.glob("REFL_234277_*"))) == 3


# --------------------------------------------------------------------------
# The backup safeguards, on their own
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "write",
    [
        lambda data: data.update_runs(
            [{"run": 234277, "base_rev": 0, "fields": {"sample_id": "S"}}]
        ),
        lambda data: data.update_experiment({"base_rev": 0, "fields": {"notes": "x"}}),
    ],
    ids=["runs", "experiment"],
)
def test_experiment_data_refuses_writes_on_its_own(expt: Path, write) -> None:
    """Second to the request gate; must hold even if the gate were bypassed."""
    from nr_workbench.web.experiment import ExperimentData, WritesDisabledError

    data = ExperimentData(expt, writable=False, autostart=False)

    with pytest.raises(WritesDisabledError):
        write(data)


def test_an_unsafe_method_outside_the_experiment_api_is_refused(expt: Path) -> None:
    app = create_app(expt, token=TOKEN, autostart=False)
    app.add_url_rule("/probe", "probe", lambda: "wrote", methods=["POST"])

    assert app.test_client().post("/probe").status_code == 405
