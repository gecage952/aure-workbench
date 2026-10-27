"""The Settings page in a real browser: what settings.js does with each answer.

The API tests prove what the server says; these prove the page acts on it --
lands on Settings from the one-time link, shows the folder check's per-run
findings, reloads on a conflict and only then, asks before a change the server
wants confirmed and asks once, and shows the lines to add when nrw.toml cannot
be edited. Each also fails on any script error or security violation the page
raises, which is how a broken nonce or a typo in settings.js would show.

Headless Chrome, driven over the DevTools protocol against an in-process
server. The protocol runs over two pipes (``--remote-debugging-pipe``), never
a port: a DevTools port answers anyone on the machine, and on a shared
analysis node another account could drive this browser while the tests run --
open ``file://`` pages, and read whatever the developer can. Skipped where
there is no Chrome, and on Windows, where the pipes are not file descriptors.
"""

from __future__ import annotations

import json
import os
import secrets
import select
import shutil
import signal
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from nr_workbench.cli import main
from nr_workbench.project.config import load_config

from .experiment_fixtures import write_autoreduced

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.name == "nt", reason="Chrome's DevTools pipes need POSIX descriptors"
    ),
]


def _chrome() -> str | None:
    candidates = [
        os.environ.get("NRW_TEST_CHROME"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        *(
            shutil.which(name)
            for name in ("google-chrome", "google-chrome-stable", "chromium", "chrome")
        ),
    ]
    return next((c for c in candidates if c and Path(c).exists()), None)


# --------------------------------------------------------------------------
# A browser, a server, and a page
# --------------------------------------------------------------------------


def _pipe_above(low: int) -> tuple[int, int]:
    """A pipe whose two ends are numbered above ``low``.

    Chrome's ends are placed on descriptors 3 and 4. Were one of them already
    3 or 4, placing the other could overwrite it first.
    """
    import fcntl

    ends = []
    for end in os.pipe():
        if end > low:
            ends.append(end)
        else:
            ends.append(fcntl.fcntl(end, fcntl.F_DUPFD_CLOEXEC, low + 1))
            os.close(end)
    return ends[0], ends[1]


class Browser:
    """Headless Chrome, spoken to over two pipes.

    With ``--remote-debugging-pipe`` Chrome reads the protocol on its
    descriptor 3 and answers on 4, each message ending in a NUL byte. Each tab
    is a session on that one connection (``Target.attachToTarget`` with
    ``flatten``), its messages carrying its ``sessionId``.
    """

    def __init__(self, chrome: str, profile: Path) -> None:
        commands, self._commands = _pipe_above(4)  # Chrome reads, we write
        self._replies, replies = _pipe_above(4)  # Chrome writes, we read
        flags = [
            "--headless=new",
            "--remote-debugging-pipe",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--window-size=1300,1000",
        ]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            flags.append("--no-sandbox")  # Chrome refuses root otherwise (containers)
        # posix_spawn, not subprocess: it can place the two ends exactly, and
        # safely with the server's threads running, which preexec_fn cannot.
        self.pid = os.posix_spawn(
            chrome,
            [chrome, *flags, "about:blank"],
            dict(os.environ),
            file_actions=[
                (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
                (os.POSIX_SPAWN_OPEN, 1, os.devnull, os.O_WRONLY, 0),
                (os.POSIX_SPAWN_OPEN, 2, os.devnull, os.O_WRONLY, 0),
                (os.POSIX_SPAWN_DUP2, commands, 3),
                (os.POSIX_SPAWN_DUP2, replies, 4),
            ],
        )
        os.close(commands)
        os.close(replies)
        self._buffer = b""
        self._next = 0
        self._pages: dict[str, Page] = {}

    def send(
        self, method: str, *, session: str | None = None, **params: Any
    ) -> dict[str, Any]:
        """One command, and its answer; events that arrive meanwhile go to their tab."""
        self._next += 1
        ident = self._next
        message: dict[str, Any] = {"id": ident, "method": method, "params": params}
        if session is not None:
            message["sessionId"] = session
        data = memoryview(json.dumps(message).encode() + b"\0")
        while data:
            data = data[os.write(self._commands, data) :]
        deadline = time.monotonic() + 30
        while True:
            reply = self._receive(deadline)
            if reply.get("id") == ident:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get("result", {})
            page = self._pages.get(reply.get("sessionId", ""))
            if page is not None:
                page.note(reply)

    def _receive(self, deadline: float) -> dict[str, Any]:
        while b"\0" not in self._buffer:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("Chrome did not answer in time")
            readable, _, _ = select.select([self._replies], [], [], left)
            if readable:
                chunk = os.read(self._replies, 1 << 16)
                if not chunk:
                    raise ConnectionError("Chrome closed its end of the pipe")
                self._buffer += chunk
        message, _, self._buffer = self._buffer.partition(b"\0")
        return json.loads(message)

    def open(self) -> Page:
        target = self.send("Target.createTarget", url="about:blank")["targetId"]
        session = self.send("Target.attachToTarget", targetId=target, flatten=True)[
            "sessionId"
        ]
        page = Page(self, session, target)
        self._pages[session] = page
        for domain in ("Runtime", "Page", "Log"):
            page.send(f"{domain}.enable")
        return page

    def close_page(self, page: Page) -> None:
        self._pages.pop(page.session, None)
        self.send("Target.closeTarget", targetId=page.target)

    def close(self) -> None:
        """Close the pipes, which ends Chrome; kill it if it lingers."""
        for end in (self._commands, self._replies):
            os.close(end)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if os.waitpid(self.pid, os.WNOHANG)[0]:
                return
            time.sleep(0.05)
        os.kill(self.pid, signal.SIGKILL)
        os.waitpid(self.pid, 0)


class Page:
    """One tab.

    Collects what would tell a person the page is broken: uncaught script
    errors, ``console.error``, and anything the browser itself logs as an
    error -- a script refused by the page's security policy among them.
    Failed requests are not collected: a 409 the page shows is the point.
    """

    def __init__(self, browser: Browser, session: str, target: str) -> None:
        self.browser = browser
        self.session = session
        self.target = target
        self.errors: list[str] = []

    def send(self, method: str, **params: Any) -> dict[str, Any]:
        return self.browser.send(method, session=self.session, **params)

    def note(self, message: dict[str, Any]) -> None:
        method, params = message.get("method"), message.get("params", {})
        if method == "Runtime.exceptionThrown":
            detail = params["exceptionDetails"]
            self.errors.append(
                detail.get("exception", {}).get("description") or detail.get("text")
            )
        elif method == "Runtime.consoleAPICalled" and params["type"] == "error":
            self.errors.append(" ".join(str(a.get("value")) for a in params["args"]))
        elif method == "Log.entryAdded":
            entry = params["entry"]
            if entry["level"] == "error" and entry.get("source") != "network":
                self.errors.append(f"{entry.get('source')}: {entry.get('text')}")

    def js(self, expression: str) -> Any:
        result = self.send(
            "Runtime.evaluate",
            expression=expression,
            awaitPromise=True,
            returnByValue=True,
        )
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"]
            raise RuntimeError(detail.get("exception", {}).get("description"))
        return result["result"].get("value")

    def wait_for(self, expression: str, what: str, timeout: float = 15) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.js(expression):
                    return
            except RuntimeError:
                pass  # the page is still loading
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for {what}; errors: {self.errors}")

    def goto(self, url: str) -> None:
        self.send("Page.navigate", url=url)
        self.wait_for(
            "document.readyState === 'complete' && window.NRWPage !== undefined",
            what=f"{url} to load",
        )

    def text(self, element_id: str) -> str:
        return self.js(f"document.getElementById({json.dumps(element_id)}).textContent")

    def type(self, element_id: str, value: str) -> None:
        """Set a field as typing would, so the page's input listeners run."""
        self.js(
            f"""{{
              const box = document.getElementById({json.dumps(element_id)});
              box.value = {json.dumps(value)};
              box.dispatchEvent(new Event("input"));
            }}"""
        )

    def click(self, element_id: str) -> None:
        self.js(f"document.getElementById({json.dumps(element_id)}).click()")


@pytest.fixture(autouse=True)
def empty_home(tmp_path: Path, monkeypatch) -> None:
    """A child the page starts reads ~/.nrw and ~/.aure through HOME: an empty
    one, so that a developer's real endpoint key is never called."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture(scope="module")
def browser(tmp_path_factory) -> Iterator[Browser]:
    """A headless Chrome for this module's tests."""
    chrome = _chrome()
    if chrome is None:
        pytest.skip("no Chrome or Chromium here (set NRW_TEST_CHROME to use one)")
    profile = tmp_path_factory.mktemp("chrome-profile")
    started = Browser(chrome, profile)
    try:
        try:
            started.send("Browser.getVersion")
        except (TimeoutError, ConnectionError) as exc:
            pytest.skip(f"Chrome did not start: {exc}")
        yield started
    finally:
        started.close()
        # Thousands of files: left behind, they make a later pytest's clean-up
        # of old temporary directories take seconds of every run.
        shutil.rmtree(profile, ignore_errors=True)


@pytest.fixture
def page(browser: Browser) -> Iterator[Page]:
    """A fresh tab; the test fails if the page raised an error in it."""
    tab = browser.open()
    try:
        yield tab
        assert tab.errors == []
    finally:
        browser.close_page(tab)


@dataclass(frozen=True)
class Site:
    """A served project: its address, and the one-time link `nrw serve` prints."""

    url: str
    link: str


@pytest.fixture
def fresh(tmp_path: Path) -> Path:
    """What `nrw init` makes in an empty folder: no IPTS, nothing to watch yet."""
    root = tmp_path / "analysis"
    result = CliRunner().invoke(main, ["init", str(root)])
    assert result.exit_code == 0, result.output
    return root


@pytest.fixture
def facility(tmp_path: Path) -> Path:
    """A beamtime folder: two runs of IPTS-34347 and one from another experiment."""
    folder = tmp_path / "facility" / "IPTS-34347" / "new_reduction"
    settled = time.time() - 3600
    for run in (234277, 234280):
        write_autoreduced(folder, run, [1, 2, 3], ipts="IPTS-34347", mtime=settled)
    write_autoreduced(folder, 234290, [1, 2, 3], ipts="IPTS-99999", mtime=settled)
    return folder


@pytest.fixture
def site(fresh: Path) -> Iterator[Site]:
    """The project served on a free port, as `nrw serve` would.

    Its link is as unguessable as `nrw serve`'s: the server listens on the
    machine's loopback, which other accounts on it can reach.
    """
    from werkzeug.serving import make_server

    from nr_workbench.web.app import create_app

    token = secrets.token_urlsafe(24)
    app = create_app(fresh, token=token, autostart=False)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield Site(url=url, link=f"{url}/auth/{token}")
    finally:
        server.shutdown()
        app.config["NRW_EXPERIMENT"].reload()


def open_settings(page: Page, site: Site) -> None:
    """Open the one-time link, then Settings, and wait for it to be editable."""
    page.goto(site.link)
    page.goto(f"{site.url}/settings")
    page.wait_for("!document.getElementById('s-save').disabled", what="editable")


def location_of(facility: Path) -> str:
    return str(facility).replace("IPTS-34347", "{ipts}")


# --------------------------------------------------------------------------
# The scenarios
# --------------------------------------------------------------------------


def test_the_link_lands_on_settings_and_the_default_path_follows_the_ipts(
    page: Page, site: Site
) -> None:
    page.goto(site.link)
    page.wait_for("location.pathname === '/settings'", what="landing on Settings")
    page.wait_for("!document.getElementById('s-save').disabled", what="editable")
    assert "needs the IPTS" in page.text("s-default-path")

    page.type("s-ipts", "34347")

    assert "/IPTS-34347/" in page.text("s-default-path")
    # Where samples and assignments are kept: the project's files, and
    # nothing else promised as coming.
    catalogs = page.text("s-catalogs")
    assert "Parquet files in this project" in catalogs
    assert "coming" not in catalogs


def test_a_folder_check_shows_what_the_server_found_run_by_run(
    page: Page, site: Site, facility: Path
) -> None:
    open_settings(page, site)
    page.type("s-ipts", "34347")
    page.type("s-location", location_of(facility))

    page.click("s-check")

    page.wait_for(
        "document.getElementById('s-check-result').textContent.includes('3 run(s)')",
        what="the check's answer",
    )
    found = page.text("s-check-result")
    assert str(facility) in found
    # The server's judgement, made against the IPTS typed on the page.
    assert "names IPTS-99999, but this project is IPTS-34347" in found


def test_a_refused_value_says_why_and_writes_nothing_and_a_good_one_saves(
    page: Page, site: Site, fresh: Path, facility: Path
) -> None:
    before = (fresh / "nrw.toml").read_bytes()
    open_settings(page, site)
    page.type("s-poll", "1")

    page.click("s-save")

    page.wait_for(
        "document.getElementById('set-message').textContent.includes('from 5 to 600')",
        what="the refusal",
    )
    assert (fresh / "nrw.toml").read_bytes() == before

    page.type("s-poll", "")
    page.type("s-ipts", "34347")
    page.type("s-location", location_of(facility))
    page.click("s-save")

    page.wait_for(
        "document.getElementById('set-message').textContent.startsWith('Saved.')",
        what="the save",
    )
    config = load_config(fresh)
    assert config.ipts == "IPTS-34347"
    assert config.raw["experiment"]["source"]["location"] == location_of(facility)
    # What the server now reads, as the server says it.
    assert f"now watches {facility}" in page.text("s-status")


def test_a_hand_edit_while_the_page_is_open_is_kept_and_the_page_reloads(
    page: Page, site: Site, fresh: Path
) -> None:
    open_settings(page, site)
    toml = fresh / "nrw.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            "[conventions]", "# a note made by hand\n[conventions]"
        ),
        encoding="utf-8",
    )
    page.type("s-label", "april 2026")

    page.click("s-save")

    page.wait_for(
        "document.getElementById('set-message').textContent.includes('reloaded')",
        what="the conflict",
    )
    assert "changed since" in page.text("set-message")
    assert page.js("document.getElementById('s-label').value") == ""  # reloaded

    page.type("s-label", "april 2026")
    page.click("s-save")

    page.wait_for(
        "document.getElementById('set-message').textContent.startsWith('Saved.')",
        what="the save after reloading",
    )
    after = toml.read_text(encoding="utf-8")
    assert "# a note made by hand" in after
    assert 'label = "april 2026"' in after


def test_a_change_the_server_wants_confirmed_is_asked_once_and_sent_again(
    page: Page, site: Site, fresh: Path
) -> None:
    from nr_workbench.experiment.model import RunChange, RunKey
    from nr_workbench.experiment.workspace import Workspace
    from nr_workbench.project.settings import save

    save(fresh, {"ipts": "IPTS-34347"})
    Workspace(fresh).store.update(
        runs=[RunChange(RunKey(234277), 0, {"sample_id": "Sample1"})]
    )
    open_settings(page, site)
    page.js("window.asked = []; window.confirm = m => (window.asked.push(m), false)")
    page.type("s-ipts", "34348")

    page.click("s-save")

    page.wait_for("window.asked.length === 1", what="the question")
    page.wait_for(
        "document.getElementById('s-status').textContent === 'Not saved.'",
        what="the declined save",
    )
    assert load_config(fresh).ipts == "IPTS-34347"

    page.js("window.asked = []; window.confirm = m => (window.asked.push(m), true)")
    page.click("s-save")

    page.wait_for(
        "document.getElementById('set-message').textContent.startsWith('Saved.')",
        what="the confirmed save",
    )
    assert page.js("window.asked.length") == 1
    assert load_config(fresh).ipts == "IPTS-34348"


def test_a_file_nrw_cannot_edit_shows_the_lines_to_add_and_keeps_the_form(
    page: Page, site: Site, fresh: Path
) -> None:
    toml = fresh / "nrw.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8")
        + "\n[experiment.source]\nsettle_seconds = '''300'''\n",
        encoding="utf-8",
    )
    before = toml.read_bytes()
    open_settings(page, site)
    page.type("s-settle", "60")

    page.click("s-save")

    page.wait_for(
        "!document.getElementById('s-lines').classList.contains('d-none')",
        what="the lines to add",
    )
    assert "settle_seconds = 60" in page.text("s-lines")
    # Not a conflict, so not reloaded: what the person typed is still there.
    assert page.js("document.getElementById('s-settle').value") == "60"
    assert toml.read_bytes() == before


def wait_for_the_language_model(page: Page) -> None:
    """Until the section has its answer: only its render checks a choice."""
    page.wait_for(
        "document.querySelector('input[name=llm-choice]:checked') !== null",
        what="the Language model section",
    )


LLM_CONTROLS = ["llm-claude", "llm-outside", "llm-model", "llm-save", "llm-check"]


def disabled(page: Page, ids: list[str]) -> list[bool]:
    return [page.js(f"document.getElementById('{i}').disabled") for i in ids]


def test_an_env_the_page_cannot_change_is_said_and_only_checking_is_offered(
    page: Page, site: Site, fresh: Path
) -> None:
    (fresh / ".env").write_bytes(b"LLM_MODEL=caf\xe9\n")
    open_settings(page, site)

    page.wait_for(
        "document.getElementById('llm-problem').textContent.includes('UTF-8')",
        what="the problem said",
    )

    assert disabled(page, LLM_CONTROLS) == [True, True, True, True, False]


def test_a_section_whose_answer_never_came_offers_nothing(
    page: Page, site: Site, monkeypatch
) -> None:
    from nr_workbench.web.llm_settings import LlmSettingsData

    def broken(self):
        raise RuntimeError("an unexpected failure")

    monkeypatch.setattr(LlmSettingsData, "llm", broken)
    open_settings(page, site)

    page.wait_for(
        "document.getElementById('llm-now').textContent.includes('could not be read')",
        what="the failure said",
    )

    # No choice made without the section's answer, and no billed call either.
    assert disabled(page, LLM_CONTROLS) == [True] * len(LLM_CONTROLS)


def test_claude_is_chosen_for_the_projects_jobs_and_checked(
    page: Page, site: Site, fresh: Path, monkeypatch
) -> None:
    import sys

    from nr_workbench.web import jobs as jobs_module

    # The check's call stands in, answering as `nrw check-llm --json` does:
    # what is tested is the page.
    monkeypatch.setattr(
        jobs_module,
        "nrw_command",
        lambda *args: [
            sys.executable,
            "-c",
            "import json; print(json.dumps({'probes': [{'status': 'ok', "
            "'detail': 'answered', 'model': 'claude_code/sonnet', 'seconds': 2.5}]}))",
        ],
    )
    open_settings(page, site)
    wait_for_the_language_model(page)
    assert "nothing is set up" in page.text("llm-now")
    assert page.js("document.getElementById('llm-outside').checked")

    page.type("llm-model", "sonnet")  # a model is Claude's: typing one chooses it
    assert page.js("document.getElementById('llm-claude').checked")
    page.click("llm-save")

    page.wait_for(
        "document.getElementById('llm-status').textContent.startsWith('Saved in .env')",
        what="the choice saved",
    )
    assert "LLM_MODEL=sonnet" in (fresh / ".env").read_text(encoding="utf-8")
    assert "Claude, through the Claude Code CLI, model sonnet (from .env)" in page.text(
        "llm-now"
    )

    page.click("llm-check")

    page.wait_for(
        "document.querySelector('#llm-result .llm-probe') !== null", what="the check"
    )
    assert "It answered, in 2.5 s." in page.text("llm-result")
    assert "claude_code/sonnet" in page.text("llm-result")
