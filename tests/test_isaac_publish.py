"""Publishing a finalized fit to ISAAC: `nrw isaac push`, and the page's panel.

`nr-isaac-format` is stood in for by a script printing what the real one
prints, one line per record the portal made, so what is under test is nrw's
side: only the final fit is published; what is pushed is what the export
wrote; each push -- a half-failed one included -- is recorded with the fit; the
key is read as every setting is and never shown; and an agent publishes
nothing.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from nr_workbench import env as env_module
from nr_workbench.cli import main
from nr_workbench.commands import isaac_cmd
from nr_workbench.project.layout import ProjectLayout
from nr_workbench.provenance import curation
from nr_workbench.web import jobs as jobs_module
from nr_workbench.web.app import create_app

from .test_curation import A, B, events, recorded

#: The key ~/.nrw holds: it must reach the tool, and never the page.
PLANTED = "not-a-real-one-q2w8"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    assert (
        CliRunner().invoke(main, ["init", str(root), "--sample", "S1"]).exit_code == 0
    )
    recorded(root, A)
    recorded(root, B)
    return root


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    """~/.nrw with the portal and its key, as a person would set them; and
    whatever loading it puts in this process's environment, taken out again."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".nrw").write_text(
        f"ISAAC_URL=https://isaac.example.org/portal/api\nISAAC_KEY={PLANTED}\n"
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(env_module, "USER_ENV_PATH", home / ".nrw")
    saved = dict(os.environ)
    try:
        yield home
    finally:
        os.environ.clear()
        os.environ.update(saved)


def exported(project: Path, fit_id: str, count: int = 2) -> Path:
    records = project / "samples" / "S1" / "results" / fit_id / "isaac" / "records"
    records.mkdir(parents=True)
    for i in range(count):
        (records / f"isaac_record_state{i}.json").write_text("{}")
    return records


def portal(monkeypatch, *, failing: int | None = None) -> None:
    """Stand in for `nr-isaac-format push`: each record made, as it says so,
    with the key's last four and the URL's host as its id -- so a test sees
    what reached the tool. A failing record is said, and the rest go on, as
    the real tool does; it then exits 1."""
    code = (
        "import os, sys, pathlib, urllib.parse\n"
        "files = sorted(pathlib.Path(sys.argv[2]).glob('*.json'))\n"
        "if '--validate-only' in sys.argv:\n"
        "    [print(f'  ✓ {f.name}: valid') for f in files]; sys.exit(0)\n"
        "key = os.environ.get('ISAAC_KEY', 'none')[-4:]\n"
        "host = urllib.parse.urlsplit(sys.argv[sys.argv.index('--url') + 1]).hostname\n"
        f"failing = {failing!r}\n"
        "for n, f in enumerate(files):\n"
        "    if n == failing:\n"
        "        print(f'  ✗ {f.name}: network error', file=sys.stderr); continue\n"
        # Coloured as a tool that forces colour would: the name itself, where
        # a code would otherwise become part of the recorded file name.
        "    print(f'  ✓ \\x1b[1m{f.name}\\x1b[0m: created (record_id={key}-{n}@{host})')\n"
        "sys.exit(1 if failing is not None else 0)\n"
    )
    monkeypatch.setattr(isaac_cmd, "_find", lambda names: [sys.executable, "-c", code])


def nrw(project: Path, monkeypatch, *args: str):
    monkeypatch.chdir(project)
    return CliRunner().invoke(main, list(args))


def finalize(project: Path, fit_id: str = A) -> None:
    curation.promote(ProjectLayout(root=project), fit_id, reason="the answer")


def pushes(project: Path, fit_id: str = A) -> tuple[dict, ...]:
    """Each push of a fit, as the replay reads the index."""
    from nr_workbench.provenance.index import FitIndex

    index = FitIndex(project / ".nrw" / "index.jsonl")
    return curation.replay(index.entries()).of(fit_id).published


# --------------------------------------------------------------------------
# From the terminal
# --------------------------------------------------------------------------


def test_only_the_final_fit_is_published_but_any_can_be_validated(
    project: Path, settings: Path, monkeypatch
) -> None:
    exported(project, A)
    portal(monkeypatch)

    refused = nrw(project, monkeypatch, "isaac", "push", A, "--yes")
    validated = nrw(project, monkeypatch, "isaac", "push", A, "--validate-only")

    assert refused.exit_code != 0
    assert "only a finalized fit is published" in refused.output
    assert validated.exit_code == 0, validated.output
    assert "valid" in validated.output
    assert events(project, "publish") == []


def test_a_push_sends_what_was_exported_and_records_what_the_portal_made(
    project: Path, settings: Path, monkeypatch
) -> None:
    exported(project, A)
    finalize(project)
    portal(monkeypatch)

    result = nrw(project, monkeypatch, "isaac", "push", A, "--yes")

    assert result.exit_code == 0, result.output
    (push,) = pushes(project)
    # The key and the portal reached the tool from ~/.nrw -- the person's own
    # settings -- and the records the portal made are kept.
    assert push["records"] == [
        {"file": "isaac_record_state0.json", "record_id": "q2w8-0@isaac.example.org"},
        {"file": "isaac_record_state1.json", "record_id": "q2w8-1@isaac.example.org"},
    ]
    assert push["portal"] == "https://isaac.example.org/portal/api"
    assert push["complete"] is True
    assert push["who"] and push["at"]
    assert PLANTED not in json.dumps(push)
    assert "\x1b[" not in result.output  # the tool's colours, not echoed raw
    # Recorded with what it sent.
    assert [f["file"] for f in push["files"]] == [
        "isaac_record_state0.json",
        "isaac_record_state1.json",
    ]


def test_a_push_that_fails_half_way_still_records_what_was_made(
    project: Path, settings: Path, monkeypatch
) -> None:
    exported(project, A, count=3)
    finalize(project)
    portal(monkeypatch, failing=1)

    result = nrw(project, monkeypatch, "isaac", "push", A, "--yes")

    assert result.exit_code != 0
    assert "2 of 3 record(s) were made" in result.output
    (push,) = pushes(project)
    # The real tool goes on past a failed record: the third was made too.
    assert [r["file"] for r in push["records"]] == [
        "isaac_record_state0.json",
        "isaac_record_state2.json",
    ]
    assert push["complete"] is False


def test_no_portal_or_key_sends_nothing(project: Path, monkeypatch) -> None:
    exported(project, A)
    finalize(project)

    def never(names):
        raise AssertionError("the tool was run")

    monkeypatch.setattr(isaac_cmd, "_find", never)

    result = nrw(project, monkeypatch, "isaac", "push", A, "--yes")

    assert result.exit_code != 0
    assert "Set ISAAC_URL and ISAAC_KEY" in result.output
    assert pushes(project) == ()


def test_the_host_shown_is_where_the_url_goes() -> None:
    # The part before an @ is a user name, whatever it looks like.
    tricky = "https://isaac.slac.stanford.edu@evil.example:8443/api?token=x"

    assert isaac_cmd.portal_host(tricky) == "evil.example:8443"
    assert isaac_cmd.shown_portal(tricky) == "https://evil.example:8443/api"


def test_pushing_again_says_it_adds_records_and_asks(
    project: Path, settings: Path, monkeypatch
) -> None:
    exported(project, A)
    finalize(project)
    portal(monkeypatch)
    nrw(project, monkeypatch, "isaac", "push", A, "--yes")

    monkeypatch.chdir(project)
    declined = CliRunner().invoke(main, ["isaac", "push", A], input="n\n")

    assert "published before" in declined.output
    assert "Not uploaded" in declined.output
    assert len(pushes(project)) == 1


def test_nothing_exported_is_nothing_to_push(
    project: Path, settings: Path, monkeypatch
) -> None:
    finalize(project)

    result = nrw(project, monkeypatch, "isaac", "push", A, "--yes")

    assert result.exit_code != 0
    assert f"nrw isaac export {A}" in result.output


def stopped(monkeypatch, stop) -> None:
    """The push's tool, stopped while it runs: *stop* is called in its place."""
    run = isaac_cmd._run

    def stopping(cmd, step, **kwargs):
        if step == "nr-isaac-format push":
            stop()
        return run(cmd, step, **kwargs)

    monkeypatch.setattr(isaac_cmd, "_run", stopping)


def test_a_push_stopped_by_ctrl_c_is_recorded_as_one_that_may_have_made_records(
    project: Path, settings: Path, monkeypatch
) -> None:
    exported(project, A)
    finalize(project)
    portal(monkeypatch)

    def interrupt() -> None:
        raise KeyboardInterrupt

    stopped(monkeypatch, interrupt)

    result = nrw(project, monkeypatch, "isaac", "push", A, "--yes")

    assert result.exit_code != 0
    (push,) = pushes(project)
    assert "complete" not in push and "records" not in push
    assert [f["file"] for f in push["files"]] == [
        "isaac_record_state0.json",
        "isaac_record_state1.json",
    ]


def test_a_push_the_pages_cancel_stops_is_recorded_too(
    project: Path, settings: Path, monkeypatch
) -> None:
    """Cancel sends SIGTERM to the push's processes."""
    import signal

    exported(project, A)
    finalize(project)
    portal(monkeypatch)
    before = signal.getsignal(signal.SIGTERM)
    stopped(monkeypatch, lambda: signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None))

    result = nrw(project, monkeypatch, "isaac", "push", A, "--yes")

    assert result.exit_code == 128 + signal.SIGTERM
    (push,) = pushes(project)
    assert "complete" not in push
    assert signal.getsignal(signal.SIGTERM) is before


def test_the_hook_refuses_a_push() -> None:
    from nr_workbench.agent.guard import judge

    assert judge(f"nrw isaac push {A} --yes").rule == "upload"
    # Validating sends the records, and the key, off the machine too.
    assert judge(f"nrw isaac push {A} --validate-only").rule == "upload"


# --------------------------------------------------------------------------
# From the page
# --------------------------------------------------------------------------


@pytest.fixture
def app(project: Path, settings: Path):
    app = create_app(project, autostart=False)
    yield app
    app.config["NRW_MODELS"].jobs.stop()


@pytest.fixture
def writer(app):
    return app.test_client()


def step(client, app, fit_id: str, name: str, body: dict | None = None):
    return client.post(f"/api/experiment/fits/{fit_id}/isaac/{name}", json=body or {})


def test_the_panel_says_where_the_portal_and_key_are_set_and_never_the_key(
    app, project: Path
) -> None:
    exported(project, A)
    finalize(project)

    shown = app.test_client().get(f"/api/experiment/fits/{A}/isaac").json

    assert shown["final"] is True
    assert shown["portal"] == {"host": "isaac.example.org", "from": "~/.nrw"}
    assert shown["key"] == {"set": True, "from": "~/.nrw"}
    assert shown["records"] == ["isaac_record_state0.json", "isaac_record_state1.json"]
    assert PLANTED[-4:] not in json.dumps(shown)


def test_each_step_is_a_job_and_only_for_the_final_fit(
    app, writer, project: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        jobs_module, "nrw_command", lambda *args: [sys.executable, "-c", "pass"]
    )
    not_final = step(writer, app, A, "export")
    finalize(project)
    early = step(writer, app, A, "validate")
    export = step(writer, app, A, "export")
    jobs = app.config["NRW_MODELS"].jobs
    deadline = time.monotonic() + 30
    while jobs.current().status == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
    exported(project, A)
    unconfirmed = step(writer, app, A, "push")
    pushed = step(writer, app, A, "push", {"confirm": A})

    assert (
        not_final.status_code == 409 and "Finalize it first" in not_final.json["error"]
    )
    assert early.status_code == 409 and "export them first" in early.json["error"]
    assert export.status_code == 202, export.json
    assert export.json["job"]["steps"] == [["isaac", "export", "--", A]]
    assert unconfirmed.status_code == 400
    assert pushed.status_code == 202, pushed.json
    assert pushed.json["job"]["steps"] == [["isaac", "push", "--yes", "--", A]]


def test_publishing_from_the_page_needs_a_writable_server(
    project: Path, settings: Path
) -> None:
    exported(project, A)
    finalize(project)
    read_only = create_app(project, autostart=False, writable=False)

    refused = step(read_only.test_client(), read_only, A, "push", {"confirm": A})

    assert refused.status_code == 403
    assert read_only.config["NRW_MODELS"].jobs.current() is None


def test_nrw_doctor_says_the_key_is_set_and_never_what_it_is(
    project: Path, settings: Path, monkeypatch
) -> None:
    result = nrw(project, monkeypatch, "doctor", "--json")

    reported = next(c for c in json.loads(result.output) if c["name"] == "llm settings")
    assert "ISAAC_KEY=set" in reported["detail"]
    assert "ISAAC_URL=https://isaac.example.org/portal/api" in reported["detail"]
    assert PLANTED not in result.output


def test_the_real_tool_pushing_to_a_portal_that_refuses_one_record(
    project: Path, tmp_path: Path, monkeypatch
) -> None:
    """The contract, with the real `nr-isaac-format`: what nrw reads of it is
    what it prints, where it goes is what nrw says, and the key reaches it."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    if not isaac_cmd.tool_installed("nr-isaac-format"):
        pytest.skip("nr-isaac-format is not installed (pip install '.[isaac]')")
    seen: list[tuple[str, str | None]] = []

    class Portal(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append((self.path, self.headers.get("Authorization")))
            made = sum(1 for path, _ in seen if path.endswith("/records"))
            if self.path.endswith("/validate"):
                code, body = 200, {"valid": True}
            elif made == 2:
                code, body = 500, {"detail": "the second is refused"}
            else:
                code, body = 201, {"success": True, "record_id": f"R{made}"}
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Portal)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/portal/api"
    settings = tmp_path / "nrw-settings"
    settings.write_text(f"ISAAC_URL={url}\nISAAC_KEY={PLANTED}\n")
    monkeypatch.setattr(env_module, "USER_ENV_PATH", settings)
    exported(project, A, count=3)
    finalize(project)
    try:
        validated = nrw(project, monkeypatch, "isaac", "push", A, "--validate-only")
        result = nrw(project, monkeypatch, "isaac", "push", A, "--yes")
    finally:
        server.shutdown()

    assert validated.exit_code == 0, validated.output
    assert result.exit_code != 0
    (push,) = pushes(project)  # validating recorded nothing
    assert push["records"] == [
        {"file": "isaac_record_state0.json", "record_id": "R1"},
        {"file": "isaac_record_state2.json", "record_id": "R3"},
    ]
    assert (push["complete"], push["portal"]) == (False, url)
    assert {auth for _, auth in seen} == {f"Bearer {PLANTED}"}
    assert PLANTED not in result.output
