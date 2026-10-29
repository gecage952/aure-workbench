"""The language model the page's jobs use, set on the Settings page.

Three layers, each with its own promise:

- ``env.where_set`` says what an nrw command started now would read, and where
  each value comes from, without loading anything into the process asking --
  and says what a real child reads, ``${VAR}`` included.
- ``project.envfile`` changes only the lines it is asked to in the project's
  ``.env``, the person's file: what a dotenv reader then gets is what was asked
  for, every other byte is kept, and it never reads or writes through a link or
  over a hand edit made meanwhile.
- The Settings API: reading is open and shows no key; choosing and checking
  need the link and a writable server. What a choice changes is proved by a real
  child ``nrw`` -- what a job reads -- not by the server's own view of it.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner
from dotenv import dotenv_values

from nr_workbench import env as env_module
from nr_workbench.cli import main
from nr_workbench.project import envfile as envfile_module
from nr_workbench.project.envfile import (
    ABSENT,
    EnvFileConflict,
    EnvValueError,
    read_env_file,
    set_values,
)
from nr_workbench.web import jobs as jobs_module
from nr_workbench.web import llm_settings as llm_module
from nr_workbench.web.app import create_app

LLM = "/api/experiment/settings/llm"
POSIX = pytest.mark.skipif(os.name == "nt", reason="POSIX files and modes")

#: What ~/.aure holds for LLM_API_KEY: it must never reach the page, redacted
#: or not. Plainly not a real one.
PLANTED = "not-a-real-one-zq7x"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    result = CliRunner().invoke(main, ["init", str(root)])
    assert result.exit_code == 0, result.output
    return root


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """A home whose ~/.aure is set up for another provider, as a machine that
    has run AuRE before often is. The child processes read it through HOME,
    this one through the paths conftest points elsewhere."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".aure").write_text(
        f"LLM_PROVIDER=openai\nLLM_MODEL=gpt-4o\nLLM_API_KEY={PLANTED}\nLLM_TIMEOUT=99\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(env_module, "USER_ENV_PATH", home / ".nrw")
    monkeypatch.setattr(env_module, "AURE_ENV_PATH", home / ".aure")
    return home


@pytest.fixture
def app(project: Path, home: Path):
    return create_app(project, autostart=False)


@pytest.fixture
def writer(app):
    return app.test_client()


@pytest.fixture
def environ_restored() -> Iterator[None]:
    """For a test that loads files into this process: whatever it loads -- any
    variable, not only nrw's own -- is gone again for the next test."""
    saved = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def headers(app) -> dict[str, str]:
    return {}


def choose(client, app, provider, model: str = "", revision: str | None = None):
    if revision is None:
        revision = client.get(LLM).json["revision"]
    return client.put(
        LLM,
        json={"revision": revision, "provider": provider, "model": model},
        headers=headers(app),
    )


def as_a_job_sees_it(project: Path) -> str | None:
    """What a job's nrw reads: `nrw doctor`, run with the jobs' own launcher.

    ``None`` when it reads no language-model setting at all.
    """
    completed = jobs_module.run_nrw(project, "doctor", "--json", timeout=120)
    checks = json.loads(completed.stdout)
    return next((c["detail"] for c in checks if c["name"] == "llm settings"), None)


# --------------------------------------------------------------------------
# Where each setting comes from
# --------------------------------------------------------------------------


def test_each_setting_comes_from_the_first_place_that_sets_it(
    project: Path, home: Path
) -> None:
    (project / ".env").write_text("LLM_PROVIDER=claude_code\nLLM_MODEL=\n")

    found = env_module.where_set(project)
    outside = env_module.where_set(project, skip_project=True)

    assert found["LLM_PROVIDER"] == env_module.Setting("claude_code", project / ".env")
    # Set empty, and so not taken from ~/.aure: gpt-4o is not a Claude model.
    assert found["LLM_MODEL"] == env_module.Setting("", project / ".env")
    assert found["LLM_TIMEOUT"] == env_module.Setting("99", home / ".aure")
    assert outside["LLM_PROVIDER"] == env_module.Setting("openai", home / ".aure")
    assert env_module.shown_source(home / ".aure", project) == "~/.aure"
    assert env_module.shown_source(project / ".env", project) == ".env"


def test_the_environment_wins_and_reading_it_changes_nothing(
    project: Path, home: Path, monkeypatch
) -> None:
    (project / ".env").write_text("LLM_PROVIDER=claude_code\n")
    monkeypatch.setenv("LLM_MODEL", "opus")

    found = env_module.where_set(project)

    assert found["LLM_MODEL"] == env_module.Setting("opus", env_module.ENVIRONMENT)
    assert "LLM_PROVIDER" not in os.environ
    assert env_module._loaded is False


def test_a_variable_loaded_here_is_left_to_the_child_and_the_shells_is_kept(
    project: Path, home: Path, monkeypatch, environ_restored
) -> None:
    # Loaded into this process, a value would otherwise reach every job as
    # though the shell had set it, and win over a .env written afterwards.
    monkeypatch.setenv("LLM_TIMEOUT", "7")  # the shell's; ~/.aure says 99
    (project / ".env").write_text("LLM_PROVIDER=claude_code\n")
    env_module.load_env(project, force=True)
    (project / ".env").write_text("LLM_PROVIDER=local\n")

    child = jobs_module.child_environment()
    found = env_module.where_set(project)

    assert os.environ["LLM_PROVIDER"] == "claude_code"  # loaded, and stale
    assert found["LLM_PROVIDER"].value == "local"
    assert "LLM_PROVIDER" not in child
    assert child["LLM_TIMEOUT"] == "7"
    assert found["LLM_TIMEOUT"] == env_module.Setting("7", env_module.ENVIRONMENT)
    assert child["PATH"] == os.environ["PATH"]


def test_what_the_page_says_is_what_a_job_reads_an_interpolated_value_included(
    project: Path, home: Path
) -> None:
    # A ${VAR} in ~/.aure sees what the project's .env set: load_dotenv puts
    # each file's values in the environment before reading the next.
    (project / ".env").write_text("LLM_PROVIDER=claude_code\nBASE=sonnet\n")
    (home / ".aure").write_text("LLM_MODEL=${BASE}\n")

    found = env_module.where_set(project)

    assert found["LLM_MODEL"] == env_module.Setting("sonnet", home / ".aure")
    assert "LLM_MODEL=sonnet" in as_a_job_sees_it(project)


def test_a_bare_name_sets_nothing_and_hides_nothing(project: Path, home: Path) -> None:
    # python-dotenv reads `LLM_MODEL` with no `=` as no value: it is not set,
    # and so a later file's value is.
    (project / ".env").write_text("LLM_MODEL\n")

    assert env_module.where_set(project)["LLM_MODEL"] == env_module.Setting(
        "gpt-4o", home / ".aure"
    )


def test_python_dotenvs_own_switch_is_honoured(
    project: Path, home: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    (project / ".env").write_text("LLM_PROVIDER=claude_code\n")

    assert env_module.where_set(project) == {}
    assert as_a_job_sees_it(project) is None  # a job loads no file either


# --------------------------------------------------------------------------
# Changing the project's .env
# --------------------------------------------------------------------------


def revision(root: Path) -> str:
    return read_env_file(root).revision


@POSIX
def test_a_new_env_holds_only_the_lines_asked_for_and_is_its_owners_alone(
    tmp_path: Path,
) -> None:
    changes = set_values(
        tmp_path,
        {"LLM_PROVIDER": "claude_code", "LLM_MODEL": ""},
        base_revision=ABSENT,
    )

    env = tmp_path / ".env"
    assert env.read_text() == "LLM_PROVIDER=claude_code\nLLM_MODEL=\n"
    assert changes == ["added LLM_PROVIDER=claude_code", "added LLM_MODEL="]
    assert env.stat().st_mode & 0o777 == 0o600


def test_what_a_reader_gets_is_what_was_asked_and_every_other_byte_is_kept(
    tmp_path: Path,
) -> None:
    env = tmp_path / ".env"
    kept = [
        "# my endpoint\n",
        f"LLM_API_KEY={PLANTED}\n\n",  # a blank line before the next, replaced
        # One value over two lines: its second line is not a variable of its own.
        'NOTE="first\nLLM_MODEL=inside a quoted value"\n',
        "\n\x0cOTHER_TOOL=1\n",  # a form feed is not a line break
        "# LLM_PROVIDER=claude_code  (a comment, left alone)\n",
    ]
    env.write_text(
        kept[0]
        + "export LLM_PROVIDER=openai\n"
        + kept[1]
        + "LLM_MODEL=gpt-4o\n"
        + kept[2]
        + kept[3]
        + "\nLLM_MODEL=gpt-4o-mini\n"  # a blank line before one removed
        # Bare, it unsets the provider for a reader: removed, or Claude would
        # be chosen in the text and not by what a job reads.
        + "LLM_PROVIDER\n"
        + kept[4]
        + "\n\n"
    )

    set_values(
        tmp_path,
        {"LLM_PROVIDER": "claude_code", "LLM_MODEL": "sonnet"},
        base_revision=revision(tmp_path),
    )

    read = dotenv_values(env)
    assert (read["LLM_PROVIDER"], read["LLM_MODEL"]) == ("claude_code", "sonnet")
    assert read["NOTE"] == "first\nLLM_MODEL=inside a quoted value"
    assert env.read_text() == (
        kept[0]
        + "LLM_PROVIDER=claude_code\n"
        + kept[1]
        + "LLM_MODEL=sonnet\n"
        + kept[2]
        + kept[3]
        + "\n"
        + kept[4]
        + "\n\n"
    )


def test_a_change_undone_restores_the_file_byte_for_byte(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    before = f"# mine\nLLM_API_KEY={PLANTED}\n\n"
    env.write_text(before)
    both = {"LLM_PROVIDER": "claude_code", "LLM_MODEL": ""}
    set_values(tmp_path, both, base_revision=revision(tmp_path))

    changes = set_values(
        tmp_path, dict.fromkeys(both), base_revision=revision(tmp_path)
    )

    assert env.read_text() == before
    assert changes == ["removed LLM_PROVIDER=claude_code", "removed LLM_MODEL="]


def test_a_line_is_added_after_a_last_line_that_has_no_line_break(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text("OTHER=1")

    set_values(
        tmp_path, {"LLM_PROVIDER": "claude_code"}, base_revision=revision(tmp_path)
    )

    assert (tmp_path / ".env").read_text() == "OTHER=1\nLLM_PROVIDER=claude_code\n"


def test_a_file_written_on_windows_keeps_its_line_endings(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_bytes(b"# mine\r\nLLM_MODEL=gpt-4o\r\n")

    set_values(
        tmp_path,
        {"LLM_PROVIDER": "claude_code", "LLM_MODEL": ""},
        base_revision=revision(tmp_path),
    )

    assert env.read_bytes() == b"# mine\r\nLLM_MODEL=\r\nLLM_PROVIDER=claude_code\r\n"


def test_saving_the_same_choice_again_leaves_the_file_alone(tmp_path: Path) -> None:
    both = {"LLM_PROVIDER": "claude_code", "LLM_MODEL": "sonnet"}
    set_values(tmp_path, both, base_revision=ABSENT)
    env = tmp_path / ".env"
    before = (env.read_bytes(), env.stat().st_mtime_ns)

    changes = set_values(tmp_path, both, base_revision=revision(tmp_path))

    assert changes == []
    assert (env.read_bytes(), env.stat().st_mtime_ns) == before


def test_a_change_made_by_hand_meanwhile_is_never_written_over(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("LLM_PROVIDER=openai\n")
    seen = revision(tmp_path)
    env.write_text("LLM_PROVIDER=openai\nLLM_API_KEY=typed-just-now\n")

    with pytest.raises(EnvFileConflict, match="has changed"):
        set_values(tmp_path, {"LLM_PROVIDER": "claude_code"}, base_revision=seen)

    assert env.read_text() == "LLM_PROVIDER=openai\nLLM_API_KEY=typed-just-now\n"


def test_a_hand_edit_made_while_the_change_is_worked_out_is_refused_too(
    tmp_path: Path, monkeypatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("LLM_PROVIDER=openai\n")
    real = envfile_module.edited

    def edited_by_hand_meanwhile(text, values):
        env.write_text("LLM_PROVIDER=openai\nLLM_API_KEY=typed-just-now\n")
        return real(text, values)

    monkeypatch.setattr(envfile_module, "edited", edited_by_hand_meanwhile)

    with pytest.raises(EnvFileConflict):
        set_values(
            tmp_path, {"LLM_PROVIDER": "claude_code"}, base_revision=revision(tmp_path)
        )

    assert "typed-just-now" in env.read_text()


@pytest.mark.parametrize(
    "value", ["a b", "x#y", "$HOME", "sonnet\nLLM_API_KEY=x", '"q"', "-x", "x" * 129]
)
def test_a_value_a_dotenv_reader_would_misread_is_refused(
    tmp_path: Path, value: str
) -> None:
    with pytest.raises(EnvValueError, match="one word"):
        set_values(tmp_path, {"LLM_MODEL": value}, base_revision=ABSENT)

    assert not (tmp_path / ".env").exists()


@POSIX
def test_an_existing_env_keeps_its_mode(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("OTHER=1\n")
    env.chmod(0o640)

    set_values(
        tmp_path, {"LLM_PROVIDER": "claude_code"}, base_revision=revision(tmp_path)
    )

    assert env.stat().st_mode & 0o777 == 0o640


# --------------------------------------------------------------------------
# The Settings page's API
# --------------------------------------------------------------------------


def test_the_section_says_what_applies_and_where_and_never_the_key(
    app,
) -> None:
    shown = app.test_client().get(LLM).json

    assert (shown["choice"], shown["revision"], shown["problem"]) == (
        "outside",
        ABSENT,
        None,
    )
    assert shown["effective"] == {
        "provider": "openai",
        "provider_from": "~/.aure",
        "model": "gpt-4o",
        "model_from": "~/.aure",
        "key": True,
    }
    assert shown["outside"] == shown["effective"]
    assert shown["environment"] == []
    assert shown["claude_code"]["id"] == "claude_code"
    # Not even its shape.
    assert PLANTED[-4:] not in json.dumps(shown)


def test_an_empty_key_is_not_a_key(app, project: Path) -> None:
    # AuRE takes an empty key as none; so must the page.
    (project / ".env").write_text("LLM_API_KEY=\n")

    assert app.test_client().get(LLM).json["effective"]["key"] is False


def test_choosing_claude_is_what_the_next_job_reads(app, writer, project: Path) -> None:
    before = as_a_job_sees_it(project)

    response = choose(writer, app, "claude_code")

    assert response.status_code == 200, response.json
    assert response.json["changes"] == [
        "added LLM_PROVIDER=claude_code",
        "added LLM_MODEL=",
    ]
    shown = response.json["llm"]
    assert (shown["choice"], shown["effective"]["provider"]) == (
        "claude_code",
        "claude_code",
    )
    assert shown["effective"]["model_from"] == ".env"
    after = as_a_job_sees_it(project)
    assert "LLM_PROVIDER=openai" in before and "LLM_MODEL=gpt-4o" in before
    # And not ~/.aure's model, which `claude --model gpt-4o` would refuse.
    assert "LLM_PROVIDER=claude_code" in after and "gpt-4o" not in after
    assert "LLM_PROVIDER" not in os.environ  # the server itself loaded nothing


def test_a_model_chosen_is_written_and_what_is_set_outside_can_be_used_again(
    app, writer, project: Path
) -> None:
    chosen = choose(writer, app, "claude_code", model="sonnet").json["llm"]

    assert "LLM_MODEL=sonnet" in (project / ".env").read_text()
    assert chosen["project"] == {"LLM_PROVIDER": "claude_code", "LLM_MODEL": "sonnet"}

    back = choose(writer, app, None)

    assert back.status_code == 200, back.json
    assert back.json["llm"]["effective"]["provider"] == "openai"
    assert (project / ".env").read_text() == ""
    assert "LLM_PROVIDER=openai" in as_a_job_sees_it(project)


def test_a_provider_written_by_hand_is_shown_as_the_projects_own(
    app, project: Path
) -> None:
    (project / ".env").write_text("LLM_PROVIDER=local\nLLM_BASE_URL=http://gpu:8000\n")

    shown = app.test_client().get(LLM).json

    assert shown["choice"] == "other"
    assert shown["project"] == {"LLM_PROVIDER": "local"}
    assert shown["effective"]["provider_from"] == ".env"


def test_a_bare_name_that_would_unset_the_choice_is_taken_out(
    app, writer, project: Path
) -> None:
    (project / ".env").write_text("LLM_PROVIDER=openai\nLLM_PROVIDER\n")

    shown = choose(writer, app, "claude_code").json["llm"]

    assert dotenv_values(project / ".env")["LLM_PROVIDER"] == "claude_code"
    assert shown["choice"] == "claude_code"


def test_an_env_the_page_cannot_use_is_said_and_never_written(
    app, writer, project: Path
) -> None:
    (project / ".env").write_bytes(b"LLM_MODEL=caf\xe9\n")

    shown = writer.get(LLM).json
    refused = choose(writer, app, "claude_code", revision="whatever")

    assert (shown["problem"], shown["revision"], shown["choice"]) == (
        ".env is not UTF-8 text.",
        None,
        None,
    )
    assert refused.status_code == 409
    assert (project / ".env").read_bytes() == b"LLM_MODEL=caf\xe9\n"


def marker_command(monkeypatch, marker: Path) -> None:
    """Replace the command a check runs with one that leaves *marker*."""
    monkeypatch.setattr(
        jobs_module,
        "nrw_command",
        lambda *args: [
            sys.executable,
            "-c",
            f"open({str(marker)!r}, 'w').close(); print('{{\"probes\": []}}')",
        ],
    )


def test_a_read_only_server_shows_the_section_and_changes_nothing(
    project: Path, home: Path, tmp_path: Path, monkeypatch
) -> None:
    marker = tmp_path / "checked"
    marker_command(monkeypatch, marker)
    app = create_app(project, autostart=False, writable=False)
    client = app.test_client()

    shown = client.get(LLM)
    put = choose(client, app, "claude_code", revision=ABSENT)
    check = client.post(f"{LLM}/check", json={}, headers=headers(app))

    assert shown.status_code == 200
    assert shown.json["writable"] is False
    assert (put.status_code, check.status_code) == (403, 403)
    assert not marker.exists()
    assert not (project / ".env").exists()


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX permissions, which root ignores",
)
def test_an_env_that_cannot_be_written_says_why(app, writer, project: Path) -> None:
    project.chmod(0o555)  # a read-only project: no file can be made in it
    try:
        response = choose(writer, app, "claude_code")
    finally:
        project.chmod(0o755)

    assert response.status_code == 500
    assert response.json["kind"] == "WriteFailedError"
    assert ".env could not be written" in response.json["error"]


def test_a_stale_revision_is_refused_and_env_is_unchanged(
    app, writer, project: Path
) -> None:
    seen = writer.get(LLM).json["revision"]
    (project / ".env").write_text("LLM_API_KEY=typed-just-now\n")

    response = choose(writer, app, "claude_code", revision=seen)

    assert response.status_code == 409
    assert response.json["kind"] == "EnvFileConflict"
    assert (project / ".env").read_text() == "LLM_API_KEY=typed-just-now\n"


@pytest.mark.parametrize(
    "body",
    [
        {"provider": "openai"},
        {"provider": "claude_code", "model": "a b"},
        {"provider": "claude_code", "model": 3},
        {},  # no provider: refused, never taken as "remove it"
        {"provider": None, "revision": ""},
    ],
)
def test_only_claude_or_nothing_is_chosen_and_a_model_is_one_word(
    app, writer, project: Path, body: dict
) -> None:
    (project / ".env").write_text("LLM_PROVIDER=local\n")
    body = {"revision": writer.get(LLM).json["revision"], **body}

    response = writer.put(LLM, json=body, headers=headers(app))

    assert response.status_code == 400, response.json
    assert (project / ".env").read_text() == "LLM_PROVIDER=local\n"


def test_the_environment_nrw_serve_was_started_with_is_said_to_win(
    app, project: Path, monkeypatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "gemini")

    shown = app.test_client().get(LLM).json

    assert shown["environment"] == ["LLM_PROVIDER"]
    assert shown["effective"]["provider_from"] == "the environment"


@POSIX
def test_whether_claude_can_be_run_is_found_as_aure_finds_it(
    app, project: Path, tmp_path: Path, monkeypatch
) -> None:
    empty, tools = tmp_path / "empty", tmp_path / "tools"
    empty.mkdir()
    tools.mkdir()
    stub = tools / "claude"
    stub.write_text("#!/bin/sh\n")
    stub.chmod(0o755)

    def claude() -> dict:
        return app.test_client().get(LLM).json["claude_code"]

    monkeypatch.setenv("PATH", str(empty))
    nowhere = claude()
    monkeypatch.setenv("PATH", str(tools))
    on_path = claude()
    monkeypatch.setenv("PATH", str(empty))
    (project / ".env").write_text(f"AURE_CLAUDE_BIN={stub}\n")
    named = claude()

    assert (nowhere["cli"], on_path["cli"], named["cli"]) == (False, True, True)
    assert (on_path["binary_from"], named["binary_from"]) == (None, ".env")
    assert str(stub) not in json.dumps(named)  # where from, never the path


# --------------------------------------------------------------------------
# Checking it
# --------------------------------------------------------------------------


def reports(monkeypatch, code: str) -> None:
    """Replace the command a check runs with *code*, run in a child as it is."""
    monkeypatch.setattr(
        jobs_module, "nrw_command", lambda *args: [sys.executable, "-c", code]
    )


def check(writer, app):
    return writer.post(f"{LLM}/check", json={}, headers=headers(app))


def test_a_check_runs_in_the_project_as_a_job_would_and_makes_one_call(
    app, writer, project: Path, monkeypatch
) -> None:
    choose(writer, app, "claude_code", model="haiku")
    # Says what it would have asked: the model the job's own nrw reads.
    reports(
        monkeypatch,
        "import json, os; from nr_workbench.env import load_env; load_env();"
        "print(json.dumps({'probes': [{'status': 'ok', 'detail': 'answered',"
        " 'model': os.environ['LLM_PROVIDER'] + '/' + os.environ['LLM_MODEL']"
        " + ' in ' + os.getcwd() + ', retries ' + os.environ['LLM_MAX_RETRIES']}]}))",
    )

    response = check(writer, app)

    assert response.status_code == 200, response.json
    probe = response.json["probe"]
    assert probe["status"] == "ok"
    assert probe["model"] == (
        f"claude_code/haiku in {os.path.realpath(project)}, retries 0"
    )


def test_a_check_that_gives_no_report_says_so(app, writer, monkeypatch) -> None:
    reports(monkeypatch, "import sys; print('Traceback: it broke', file=sys.stderr)")

    probe = check(writer, app).json["probe"]

    assert probe == {
        "status": "error",
        "detail": "nrw check-llm gave no report: Traceback: it broke",
    }


@POSIX
def test_a_check_out_of_time_is_stopped_whole_and_the_next_one_runs(
    app, writer, project: Path, tmp_path: Path, monkeypatch
) -> None:
    # LLM_TIMEOUT=1 and no margin: the check is waited for one second.
    monkeypatch.setattr(llm_module, "CHECK_MARGIN", 0.0)
    (project / ".env").write_text("LLM_TIMEOUT=1\n")
    pid_file = tmp_path / "grandchild"
    # A CLI the check started, as AuRE starts `claude`: it must not outlive it.
    reports(
        monkeypatch,
        "import subprocess, sys, time;"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid)); time.sleep(60)",
    )

    asked = time.monotonic()
    timed_out = check(writer, app)

    assert time.monotonic() - asked < 10  # LLM_TIMEOUT's second, and the kill
    assert timed_out.status_code == 504
    assert "did not answer" in timed_out.json["error"]
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("the check's grandchild outlived it")
    reports(monkeypatch, 'print(\'{"probes": [{"status": "ok", "detail": "x"}]}\')')
    assert check(writer, app).status_code == 200  # the lock was let go


@pytest.mark.parametrize(
    ("aure", "env", "waited"),
    [
        ("", "", 120 + llm_module.CHECK_MARGIN),  # AuRE's own default
        ("LLM_TIMEOUT=99\n", "", 99 + llm_module.CHECK_MARGIN),  # from ~/.aure
        ("LLM_TIMEOUT=99\n", "LLM_TIMEOUT=30\n", 30 + llm_module.CHECK_MARGIN),
        ("", "LLM_TIMEOUT=100000\n", llm_module.MAX_CHECK),  # a request waits on it
        ("", "LLM_TIMEOUT=soon\n", 120 + llm_module.CHECK_MARGIN),  # AuRE refuses it
    ],
)
def test_a_check_is_waited_for_as_long_as_aure_waits_for_the_call(
    app, project: Path, home: Path, aure: str, env: str, waited: float
) -> None:
    (home / ".aure").write_text(aure)
    (project / ".env").write_text(env)

    assert app.config["NRW_LLM"]._deadline() == waited


def test_a_second_check_while_one_runs_is_refused(
    app, writer, tmp_path: Path, monkeypatch
) -> None:
    from nr_workbench.bounded import Busy

    started, release = tmp_path / "started", tmp_path / "release"
    reports(
        monkeypatch,
        "import json, os, time\n"
        f"open({str(started)!r}, 'w').close()\n"
        f"while not os.path.exists({str(release)!r}):\n"
        "    time.sleep(0.02)\n"
        "print(json.dumps({'probes': [{'status': 'ok', 'detail': 'x'}]}))\n",
    )
    answers: list = []
    waiting = threading.Thread(target=lambda: answers.append(check(writer, app)))
    waiting.start()
    try:
        deadline = time.monotonic() + 30
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)

        # The link opens once, for one browser: a second one is a second tab.
        with pytest.raises(Busy, match="running already"):
            app.config["NRW_LLM"].check_llm()
    finally:
        release.touch()
        waiting.join(30)

    assert answers[0].status_code == 200


def test_the_real_check_runs_and_says_what_is_missing_without_calling_anything(
    project: Path, tmp_path: Path, monkeypatch
) -> None:
    # Nothing chosen, and a home with nothing in it: `nrw check-llm` has no
    # endpoint to call, so this spends nothing -- and it is the real command.
    empty = tmp_path / "empty-home"
    empty.mkdir()
    monkeypatch.setenv("HOME", str(empty))
    app = create_app(project, autostart=False)

    response = check(app.test_client(), app)

    assert response.status_code == 200, response.json
    probe = response.json["probe"]
    assert probe["status"] == "error"
    assert not probe["detail"].startswith("nrw check-llm gave no report"), probe


def test_a_key_in_a_providers_error_is_taken_out_before_it_is_printed(
    monkeypatch,
) -> None:
    from nr_workbench.aure_adapter import scrubbed

    monkeypatch.setenv("LLM_API_KEY", PLANTED)

    said = scrubbed(f"401 for key {PLANTED}: unauthorised")

    assert said == "401 for key [a key]: unauthorised"
