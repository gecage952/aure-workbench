"""`nrw tiled`: the person's server, a client that asks no one to log in, a
push in which the last writer wins and nothing is made untagged, and the
guard that keeps every Tiled command a person's.

The official client is replaced by a fake at the one place `tiled/server.py`
loads it (`library`), so nothing here needs it installed, or sends anything.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple

import pytest
from click.testing import CliRunner

from nr_workbench import env as env_module
from nr_workbench.agent.guard import judge
from nr_workbench.cli import main
from nr_workbench.tiled import server as server_module
from nr_workbench.tiled.server import TiledSyncError, connect, server

KEY = "K" * 32


def own_settings(text: str) -> None:
    """The person's ``~/.nrw``, where the suite's isolation points it."""
    env_module.USER_ENV_PATH.write_text(text, encoding="utf-8")
    env_module.USER_ENV_PATH.chmod(0o600)


def project_env(project: Path, text: str) -> None:
    """The project's own ``.env``, which anyone who commits writes."""
    (project / ".env").write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------
# A fake of the official client
# --------------------------------------------------------------------------


class FakeNode:
    """A node: its children by key, its tags, its metadata and specs. What is
    made in it, or written over it, goes in one log the whole tree shares."""

    def __init__(
        self,
        children: dict[str, FakeNode] | None = None,
        *,
        tags: tuple[str, ...] = ("isaac",),
        metadata: dict[str, Any] | None = None,
        specs: tuple[Any, ...] = (),
        log: list[tuple[str, ...]] | None = None,
    ) -> None:
        self.children = dict(children or {})
        self.tags = list(tags)
        self.metadata = dict(metadata or {})
        self.specs = list(specs)
        self.log = log if log is not None else []
        self.key = ""
        self.context: Any = None
        for child in self.children.values():
            child.share(self.log)

    def share(self, log: list[tuple[str, ...]]) -> None:
        self.log = log
        for child in self.children.values():
            child.share(log)

    def __iter__(self):
        return iter(self.children)

    def items(self) -> list[tuple[str, FakeNode]]:
        return list(self.children.items())

    def create_container(
        self, key: str, *, metadata=None, specs=None, access_tags=None
    ) -> FakeNode:
        assert key not in self.children, f"{key} made twice"
        made = FakeNode(
            tags=tuple(access_tags or ()),
            metadata=metadata,
            specs=tuple(specs or ()),
            log=self.log,
        )
        made.key = key
        self.children[key] = made
        self.log.append(("create", key, *sorted(access_tags or ())))
        return made

    def replace_metadata(self, metadata=None, specs=None, access_tags=None) -> None:
        assert access_tags is None, "a replace leaves the tags"
        self.metadata = dict(metadata or {})
        if specs is not None:
            self.specs = list(specs)
        self.log.append(("replace", self.key))

    @property
    def access_blob(self) -> dict[str, Any]:
        return {"tags": list(self.tags)}

    def __getitem__(self, key: str) -> FakeNode:
        node = self
        for part in key.strip("/").split("/"):
            node = node.children[part]
        return node


class FakeSpec(NamedTuple):
    """The client's ``Spec``: a name, and a version."""

    name: str
    version: str | None = None


class FakeContext:
    """What the client's ``Context`` is asked by nrw."""

    def __init__(self, *, providers: tuple[str, ...] = ("globus",), kept=False):
        self.server_info = SimpleNamespace(
            authentication=SimpleNamespace(providers=list(providers))
        )
        self.kept = kept
        self.logins: list[bool] = []
        self.logged_out = False
        self.principal: dict[str, Any] = {
            "type": "user",
            "identities": [{"id": "someone@ornl.gov", "provider": "globus"}],
        }

    def use_cached_tokens(self) -> bool:
        return self.kept

    def authenticate(self, *, remember_me: bool = True) -> None:
        self.logins.append(remember_me)
        self.kept = True

    def logout(self) -> None:
        self.logged_out = True
        self.kept = False

    def whoami(self) -> dict[str, Any]:
        return self.principal


class FakeTiled:
    """The client's ``Context`` and ``from_context``, over one fake server."""

    def __init__(self, root: FakeNode | None = None, **context: Any) -> None:
        self.root = root or FakeNode()
        self.context = FakeContext(**context)
        self.asked: list[tuple[str, str | None]] = []

    def library(self) -> server_module.Library:
        fake = self

        class Context:
            @staticmethod
            def from_any_uri(url: str, *, api_key: str | None = None):
                fake.asked.append((url, api_key))
                return fake.context, []

        def from_context(context: FakeContext, node_path_parts=()):
            fake.root.context = context
            return fake.root

        return server_module.Library(Context, from_context, FakeSpec)


@pytest.fixture
def tiled(monkeypatch, tmp_path: Path) -> FakeTiled:
    """The fake, loaded where the real client would be; its token cache in
    the test's own folder, never the person's."""
    fake = FakeTiled(FakeNode({"projects": FakeNode({"isaac": FakeNode({})})}))
    monkeypatch.setattr(server_module, "library", fake.library)
    monkeypatch.setenv("TILED_CACHE_DIR", str(tmp_path / "tiled-cache"))
    return fake


def on_tiled(fake: FakeTiled, ipts: str = "IPTS-00001", **node: Any) -> FakeNode:
    experiment = FakeNode(**node)
    experiment.share(fake.root.log)
    fake.root["projects/isaac"].children[ipts] = experiment
    return experiment


# --------------------------------------------------------------------------
# The person's server, and the key
# --------------------------------------------------------------------------


def test_the_server_is_ornls_unless_the_person_names_another(project: Path) -> None:
    assert server(project).url == "https://tiled.ornl.gov"
    assert server(project).source == "nrw's default"

    own_settings("TILED_URL=https://tiled-test.ornl.gov/\n")

    assert server(project).url == "https://tiled-test.ornl.gov"


def test_a_projects_env_chooses_neither_the_server_nor_the_key(project: Path) -> None:
    """It is written by anyone who commits: it would choose where the person's
    login is sent, and what is sent."""
    project_env(project, f"TILED_URL=https://evil.example\nTILED_API_KEY={KEY}\n")

    chosen = server(project)

    assert chosen.url == "https://tiled.ornl.gov"
    assert chosen.key is None


def test_the_persons_key_is_sent_and_said_where_it_is_set(project: Path) -> None:
    own_settings(f"TILED_API_KEY={KEY}\n")

    chosen = server(project)

    assert chosen.key == KEY and chosen.key_source.endswith(".nrw")


@pytest.mark.parametrize(
    ("url", "said"),
    [
        ("http://tiled.ornl.gov", "not an https:// address"),
        ("https://someone:pw@tiled.ornl.gov", "a user name"),
        ("https://tiled.ornl.gov/?api_key=SECRET-1", "a query"),
        ("https://tiled.ornl.gov/api/v1", "has a path"),
    ],
)
def test_an_address_nrw_sends_no_login_to_is_refused_without_repeating_it(
    project: Path, url: str, said: str
) -> None:
    own_settings(f"TILED_URL={url}\n")

    with pytest.raises(TiledSyncError) as refused:
        server(project)

    assert said in str(refused.value)
    assert url not in str(refused.value) and "SECRET" not in str(refused.value)


def test_a_server_on_this_machine_may_be_plain_http(project: Path) -> None:
    own_settings("TILED_URL=http://127.0.0.1:8000\n")

    assert server(project).url == "http://127.0.0.1:8000"


def test_what_nrws_env_loading_set_of_tileds_is_taken_out_and_the_shells_kept(
    monkeypatch,
) -> None:
    """The client reads TILED_CACHE_DIR, TILED_API_KEY and TILED_LOG_AUTH_TOKEN
    itself: from a file, they would move the person's tokens, or log them."""
    monkeypatch.setenv("TILED_LOG_AUTH_TOKEN", "1")
    monkeypatch.setenv("TILED_RETRY_ATTEMPTS", "2")
    monkeypatch.setattr(env_module, "_set_by_files", {"TILED_LOG_AUTH_TOKEN"})

    server_module._scrub_environment()

    import os

    assert "TILED_LOG_AUTH_TOKEN" not in os.environ
    assert os.environ["TILED_RETRY_ATTEMPTS"] == "2"


# --------------------------------------------------------------------------
# Connecting, and logging in
# --------------------------------------------------------------------------


def test_a_client_not_installed_says_how_to_install_it(project: Path, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "tiled.client.constructors", None)

    with pytest.raises(TiledSyncError, match=r"nr-workbench\[tiled\]"):
        connect(project)


def test_a_command_never_starts_a_login(project: Path, tiled: FakeTiled) -> None:
    """Only `nrw tiled login` prints Globus's address and waits."""
    with pytest.raises(TiledSyncError, match="run `nrw tiled login`"):
        connect(project)

    assert tiled.context.logins == []


def test_a_login_is_started_when_asked_and_kept_by_the_client(
    project: Path, tiled: FakeTiled
) -> None:
    client = connect(project, login=True)

    assert tiled.context.logins == [True]
    assert client is tiled.root


def test_with_the_persons_key_no_login_is_looked_for(
    project: Path, tiled: FakeTiled
) -> None:
    own_settings(f"TILED_API_KEY={KEY}\n")

    connect(project)

    assert tiled.asked == [("https://tiled.ornl.gov", KEY)]
    assert tiled.context.logins == []


def test_a_server_that_does_not_answer_is_said_as_its_own(
    project: Path, monkeypatch
) -> None:
    import httpx

    class Context:
        @staticmethod
        def from_any_uri(url: str, *, api_key=None):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(
        server_module, "library", lambda: server_module.Library(Context, None, None)
    )

    with pytest.raises(TiledSyncError, match="tiled.ornl.gov did not answer"):
        connect(project)


def test_whose_login_it_is_shows_nothing_a_terminal_would_act_on(
    tiled: FakeTiled,
) -> None:
    tiled.context.principal["identities"] = [
        {"id": f"x{chr(27)}[2Jy", "provider": "globus"}
    ]

    shown = server_module.whoami(SimpleNamespace(context=tiled.context))

    assert chr(27) not in shown and "globus" in shown


def test_logging_out_ends_the_kept_login(project: Path, tiled: FakeTiled) -> None:
    tiled.context.kept = True

    said = server_module.logout(project)

    assert tiled.context.logged_out
    assert "Logged out of https://tiled.ornl.gov" in said


def test_with_a_key_there_is_no_login_to_end(project: Path, tiled: FakeTiled) -> None:
    own_settings(f"TILED_API_KEY={KEY}\n")

    said = server_module.logout(project)

    assert "no login to end" in said and tiled.asked == []


def test_a_kept_login_is_found_by_its_files_alone(tmp_path: Path, monkeypatch) -> None:
    import urllib.parse

    pytest.importorskip("platformdirs")
    cache = tmp_path / "cache"
    monkeypatch.setenv("TILED_CACHE_DIR", str(cache))
    url = "https://tiled.ornl.gov"
    tokens = cache / "tokens" / urllib.parse.quote_plus(f"{url}/api/v1/")

    assert not server_module.kept_login(url)

    tokens.mkdir(parents=True)
    (tokens / "refresh_token").write_text("t", encoding="utf-8")

    assert server_module.kept_login(url)


# --------------------------------------------------------------------------
# `nrw tiled login`, `logout` and `status`
# --------------------------------------------------------------------------


def nrw(*args: str):
    return CliRunner().invoke(main, ["tiled", *args])


def test_status_says_whose_the_login_is_and_what_the_experiment_holds(
    project: Path, tiled: FakeTiled
) -> None:
    tiled.context.kept = True
    on_tiled(tiled, tags=("isaac", "ipts-1"))

    shown = nrw("status", "--root", str(project))
    found = json.loads(nrw("status", "--root", str(project), "--json").output)

    assert shown.exit_code == 0, shown.output
    assert "someone@ornl.gov (globus)" in shown.output
    assert "tagged isaac, ipts-1" in shown.output
    assert "not made yet" in shown.output
    assert found["experiment"] == "projects/isaac/IPTS-00001"
    assert (found["experiment_found"], found["context_found"]) == (True, False)


def test_status_of_an_experiment_not_on_tiled_says_so(
    project: Path, tiled: FakeTiled
) -> None:
    tiled.context.kept = True

    shown = nrw("status", "--root", str(project))

    assert shown.exit_code == 0, shown.output
    assert "not there, or not shared with this login" in shown.output


def test_status_without_a_login_says_how_to_log_in(
    project: Path, tiled: FakeTiled
) -> None:
    shown = nrw("status", "--root", str(project))

    assert shown.exit_code == 1
    assert "nrw tiled login" in shown.output and tiled.context.logins == []


def test_login_with_a_key_set_says_to_remove_it(project: Path, tiled: FakeTiled):
    own_settings(f"TILED_API_KEY={KEY}\n")

    shown = nrw("login", "--root", str(project))

    assert shown.exit_code == 1 and "remove it to log in" in shown.output
    assert KEY not in shown.output


def test_login_logs_in_and_says_as_whom(project: Path, tiled: FakeTiled) -> None:
    shown = nrw("login", "--root", str(project))

    assert shown.exit_code == 0, shown.output
    assert "as someone@ornl.gov (globus)" in shown.output


@pytest.mark.parametrize("command", ["login", "logout", "status"])
def test_every_tiled_command_refuses_itself_to_an_agent(
    project: Path, tiled: FakeTiled, monkeypatch, command: str
) -> None:
    monkeypatch.setenv("NRW_AGENT", "1")

    shown = nrw(command, "--root", str(project))

    assert shown.exit_code == 1 and "person's own login" in shown.output
    assert tiled.asked == []


# --------------------------------------------------------------------------
# The guard
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "nrw tiled status",
        "nrw tiled login",
        "cd x && nrw tiled push",
        "tiled login https://tiled.ornl.gov",
        "tiled api_key create",
        "/opt/conda/bin/tiled tree /projects",
        "python -m tiled whoami",
        "bash -c 'tiled logout'",
    ],
)
def test_the_hook_refuses_every_tiled_command(command: str) -> None:
    verdict = judge(command)

    assert not verdict.allowed and verdict.rule == "tiled"


@pytest.mark.parametrize("command", ["grep tiled notes.md", "nrw fit ls --tiled-x"])
def test_the_hook_lets_the_word_alone_through(command: str) -> None:
    assert judge(command).allowed


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("installed", "settings", "kept", "said"),
    [
        (False, "", False, "client is not installed"),
        (True, f"TILED_API_KEY={KEY}\n", False, "TILED_API_KEY is sent"),
        (True, "", True, "logged in"),
        (True, "", False, "not logged in (nrw tiled login)"),
    ],
    ids=["not-installed", "a-key", "a-kept-login", "nothing"],
)
def test_doctor_says_where_tiled_commands_would_go_and_with_what(
    monkeypatch, installed: bool, settings: str, kept: bool, said: str
) -> None:
    import importlib.util

    from nr_workbench.commands import doctor

    own_settings(settings)
    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a: (
            (object() if installed else None) if name == "tiled" else real(name, *a)
        ),
    )
    monkeypatch.setattr(server_module, "kept_login", lambda url: kept)

    (check,) = doctor._tiled_checks()

    assert said in check.detail and KEY not in check.detail
    assert check.detail.startswith("https://tiled.ornl.gov (nrw's default)")


# --------------------------------------------------------------------------
# `nrw tiled push`
# --------------------------------------------------------------------------

NOW = "2026-10-27T12:00:00Z"


def catalogued(project: Path, **sample: Any) -> None:
    """A sample and two of its runs, in the project's own catalog."""
    from nr_workbench.experiment.model import RunChange, RunKey, SampleChange
    from nr_workbench.experiment.store import ParquetCatalogStore

    ParquetCatalogStore.for_project(project).update(
        runs=[
            RunChange(RunKey(234277), 0, {"sample_id": "S1", "condition": "OCV"}),
            RunChange(RunKey(234280), 0, {"sample_id": "S1", "condition": "CA"}),
        ],
        samples=[SampleChange("S1", 0, {"title": "Cu/Pt", **sample})],
        now=NOW,
    )


def pushed(project: Path, *args: str):
    return nrw("push", "--root", str(project), "--yes", *args)


@pytest.fixture
def ready(project: Path, tiled: FakeTiled) -> FakeNode:
    """Logged in, the experiment on Tiled, two runs and a sample to push."""
    tiled.context.kept = True
    catalogued(project)
    return on_tiled(tiled, tags=("isaac", "ipts-1"))


def test_a_first_push_makes_the_catalog_with_the_experiments_tags(
    project: Path, tiled: FakeTiled, ready: FakeNode
) -> None:
    shown = pushed(project)

    assert shown.exit_code == 0, shown.output
    assert "Pushed 3 record(s)." in shown.output
    tags = ("ipts-1", "isaac")
    assert tiled.root.log == [
        ("create", "context", *tags),
        ("create", "runs", *tags),
        ("create", "samples", *tags),
        # The sample before its runs: no reader meets a run whose sample is
        # not there yet.
        ("create", "S1", *tags),
        ("create", "234277-steady", *tags),
        ("create", "234280-steady", *tags),
    ]
    context = ready["context"]
    assert context.metadata == {"ipts": "IPTS-00001", "written_by": "nr-workbench"}
    assert context.specs == [FakeSpec("context-experiment", "1")]
    run = ready["context/runs/234277-steady"]
    assert run.specs == [FakeSpec("context-run", "1")]
    assert (run.metadata["run"], run.metadata["sample_id"]) == (234277, "S1")
    assert ready["context/samples/S1"].metadata["title"] == "Cu/Pt"


def test_a_push_of_what_tiled_holds_already_writes_nothing(
    project: Path, tiled: FakeTiled, ready: FakeNode
) -> None:
    pushed(project)
    before = list(tiled.root.log)

    shown = pushed(project)

    assert shown.exit_code == 0, shown.output
    assert "Tiled already holds this catalog." in shown.output
    assert tiled.root.log == before


def test_the_last_writer_wins_and_another_programs_fields_stay(
    project: Path, tiled: FakeTiled, ready: FakeNode
) -> None:
    pushed(project)
    sample = ready["context/samples/S1"]
    sample.metadata["title"] = "Changed on Tiled"
    sample.metadata["beamline_note"] = "kept by another program"
    from nr_workbench.experiment.model import SampleChange
    from nr_workbench.experiment.store import ParquetCatalogStore

    ParquetCatalogStore.for_project(project).update(
        samples=[SampleChange("S1", 1, {"title": "Cu/Pt, annealed"})], now=NOW
    )

    shown = pushed(project)

    assert shown.exit_code == 0, shown.output
    assert "0 new, 1 changed, 2 the same" in shown.output
    assert sample.metadata["title"] == "Cu/Pt, annealed"
    assert sample.metadata["beamline_note"] == "kept by another program"
    assert sample.tags == ["isaac", "ipts-1"]


def test_a_record_tiled_alone_holds_is_left_and_said(
    project: Path, tiled: FakeTiled, ready: FakeNode
) -> None:
    pushed(project)
    ready["context/samples"].create_container("S9", metadata={"title": "theirs"})

    shown = pushed(project)
    status = json.loads(nrw("status", "--root", str(project), "--json").output)

    assert "Tiled already holds this catalog." in shown.output
    assert "S9" in ready["context/samples"].children
    assert status["only_on_tiled"] == ["samples/S9"]
    assert status["records"]["same"] == [
        "runs/234277-steady",
        "runs/234280-steady",
        "samples/S1",
    ]


def test_nothing_is_made_where_there_are_no_tags_to_give(
    project: Path, tiled: FakeTiled
) -> None:
    """A node made without them is its maker's alone: an empty catalog to
    everyone else on the experiment, with no error anywhere."""
    tiled.context.kept = True
    catalogued(project)
    on_tiled(tiled, tags=())

    shown = pushed(project)

    assert shown.exit_code == 1
    assert "has no access tags" in shown.output
    assert tiled.root.log == []


def test_a_dry_run_says_what_would_be_written_and_writes_nothing(
    project: Path, tiled: FakeTiled, ready: FakeNode
) -> None:
    shown = pushed(project, "--dry-run")

    assert shown.exit_code == 0, shown.output
    assert "3 new, 0 changed, 0 the same" in shown.output
    assert "+ samples/S1" in shown.output and "+ runs/234277-steady" in shown.output
    assert "Nothing written (--dry-run)." in shown.output
    assert tiled.root.log == []


def test_a_push_not_confirmed_writes_nothing(
    project: Path, tiled: FakeTiled, ready: FakeNode
) -> None:
    shown = CliRunner().invoke(
        main, ["tiled", "push", "--root", str(project)], input="n\n"
    )

    assert shown.exit_code == 1
    assert "for everyone on the experiment" in shown.output
    assert tiled.root.log == []


def test_a_push_stopped_partway_says_what_it_wrote_and_the_next_finishes(
    project: Path, tiled: FakeTiled, ready: FakeNode, monkeypatch
) -> None:
    """Each record is written whole, or not at all: a push run again writes
    only what is still different."""
    import httpx

    made = FakeNode.create_container

    def server_goes_away(self, key, **kwargs):
        if key == "234280-steady":
            raise httpx.ConnectError("the server went away")
        return made(self, key, **kwargs)

    monkeypatch.setattr(FakeNode, "create_container", server_goes_away)
    stopped = pushed(project)
    monkeypatch.setattr(FakeNode, "create_container", made)
    finished = pushed(project)

    assert stopped.exit_code == 1
    assert "runs/234280-steady could not be written" in stopped.output
    assert "2 record(s) were written before it" in stopped.output
    assert finished.exit_code == 0, finished.output
    assert "1 new, 0 changed, 2 the same" in finished.output
    assert "234280-steady" in ready["context/runs"].children


def test_a_push_to_an_experiment_not_on_tiled_writes_nothing(
    project: Path, tiled: FakeTiled
) -> None:
    tiled.context.kept = True
    catalogued(project)

    shown = pushed(project)

    assert shown.exit_code == 1
    assert "projects/isaac/IPTS-00001 is not on https://tiled.ornl.gov" in shown.output


def test_an_empty_catalog_has_nothing_to_push(project: Path, tiled: FakeTiled):
    tiled.context.kept = True

    shown = pushed(project)

    assert shown.exit_code == 0 and "nothing to push" in shown.output
    assert tiled.asked == []


def test_a_field_a_newer_nrw_wrote_is_left_for_it_to_push(project: Path) -> None:
    """Its type is not known to be JSON's, and it is that nrw's to send."""
    from nr_workbench.experiment.model import Catalog, SampleContext
    from nr_workbench.tiled.context import records_of

    catalog = Catalog(
        samples={"S1": SampleContext("S1", title="Cu", extra={"future": object()})}
    )

    (record,) = records_of(catalog)

    assert "future" not in record.fields and record.fields["title"] == "Cu"
