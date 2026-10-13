"""Shared fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from nr_workbench import env as env_module
from nr_workbench.project.render import RenderContext

#: Variables the per-user files can set. Cleared per test alongside the paths,
#: because `load_dotenv(override=False)` leaves whatever a previous test (or
#: the developer's own shell) already put in `os.environ`.
_USER_ENV_VARS = env_module.KNOWN_VARS


@pytest.fixture(autouse=True)
def isolate_user_env(tmp_path_factory, monkeypatch) -> None:
    """Keep the developer's own ``~/.nrw`` and ``~/.aure`` out of every test.

    `load_env` reads those two files, and `nrw` reaches it from almost every
    command, so without this a test's behaviour depends on the home directory
    it happens to run in. That is not hypothetical: a real `~/.aure` carrying
    `LLM_MODEL` was observed changing what `nrw check-llm` reported during a
    test run, which means CI and a laptop can legitimately disagree about a
    green suite.

    `_loaded` is reset too. It is a module-level latch, so the first test to
    call `load_env` would otherwise decide for every test after it; and so is
    `_set_by_files`, which would otherwise take a later test's shell variable
    for one a file set.
    """
    empty = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(env_module, "USER_ENV_PATH", empty / ".nrw")
    monkeypatch.setattr(env_module, "AURE_ENV_PATH", empty / ".aure")
    monkeypatch.setattr(env_module, "_loaded", False)
    monkeypatch.setattr(env_module, "_set_by_files", set())
    for name in _USER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # Set by `nrw agent run` for its session. A suite run inside one would
    # otherwise see every model call stand down.
    monkeypatch.delenv("NRW_AGENT", raising=False)


#: A fixed timestamp, so rendered templates are byte-stable across test runs.
FIXED_CREATED = "2026-01-02T03:04:05Z"


@pytest.fixture
def context() -> RenderContext:
    """A render context with a fixed timestamp."""
    return RenderContext(
        project_name="test-project",
        beamtime="june2026",
        ipts="IPTS-00001",
        created=FIXED_CREATED,
    )


@pytest.fixture
def project(tmp_path: Path, context: RenderContext) -> Path:
    """A scaffolded project with the seed skills and one sample."""
    from nr_workbench.commands.init_cmd import plan_project_files
    from nr_workbench.commands.sample import plan_sample_files
    from nr_workbench.project.scaffold import apply_scaffold

    planned = plan_project_files(context)
    planned.extend(plan_sample_files(context, "Sample1"))
    apply_scaffold(tmp_path, planned)
    return tmp_path
