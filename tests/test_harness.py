"""Tests for the harness registry.

The registry is what stops `.claude/` and `.github/` being spelled out in six
modules. Its job is small and its failure mode is quiet -- a harness that
resolves to the wrong directory writes files nothing reads -- so the contract
is pinned here rather than inferred from the scaffold tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nr_workbench.harness import (
    DEFAULT_HARNESSES,
    HARNESSES,
    HarnessError,
    agent_dirs,
    known_names,
    resolve,
    session_harnesses,
)


def test_the_default_set_is_what_projects_already_had() -> None:
    """Pinned: an existing project must be unaffected by harness selection."""
    assert DEFAULT_HARNESSES == ("claude", "copilot")


def test_every_registered_harness_has_a_unique_name() -> None:
    names = [harness.name for harness in HARNESSES]

    assert len(names) == len(set(names))


def test_every_registered_harness_does_something() -> None:
    """A harness with no files, no agents and no session is not a harness."""
    for harness in HARNESSES:
        assert (
            harness.template_subdir is not None
            or harness.agents_dir is not None
            or harness.drives_sessions
        ), f"{harness.name} contributes nothing"


def test_every_template_subdir_exists_in_the_package() -> None:
    """A registry entry naming a missing subtree fails at scaffold time.

    Cheap to assert here and expensive to discover from a `nrw init`
    traceback in a beamtime folder.
    """
    from nr_workbench.project.render import templates_root

    for harness in HARNESSES:
        if harness.template_subdir is None:
            continue
        subtree = templates_root() / "harness" / harness.template_subdir
        assert subtree.is_dir(), f"{harness.name} names a missing subtree: {subtree}"


def test_resolve_returns_registry_order_regardless_of_input_order() -> None:
    """The same set must always plan the same files in the same sequence."""
    forward = [h.name for h in resolve(["claude", "copilot"])]
    backward = [h.name for h in resolve(["copilot", "claude"])]

    assert forward == backward == ["claude", "copilot"]


def test_resolve_drops_duplicates_and_normalises_case() -> None:
    resolved = resolve(["Claude", "claude", "  CLAUDE  "])

    assert [h.name for h in resolved] == ["claude"]


def test_resolve_rejects_an_unknown_name_and_says_what_is_known() -> None:
    with pytest.raises(HarnessError) as excinfo:
        resolve(["claude", "emacs"])

    message = str(excinfo.value)
    assert "emacs" in message
    assert "claude" in message


def test_resolve_refuses_an_empty_set() -> None:
    """A project with no harness has no instructions and no limits."""
    with pytest.raises(HarnessError):
        resolve([])


def test_agent_dirs_skips_harnesses_that_read_no_subagents() -> None:
    dirs = agent_dirs(resolve(known_names()))

    assert ".claude/agents" in dirs
    assert all(isinstance(path, str) for path in dirs)
    assert len(dirs) == len([h for h in HARNESSES if h.agents_dir is not None])


def test_copilot_does_not_drive_sessions() -> None:
    """A harness may only be driven unattended once both halves exist.

    Copilot has neither a headless mode we drive nor a way to refuse a command
    before it runs, so `nrw agent run` must never select it. Claude Code and
    OpenCode have both -- OpenCode's only after its plugin was measured
    blocking a call under `--auto`.
    """
    drivable = [h.name for h in session_harnesses()]

    assert "copilot" not in drivable
    assert drivable == ["claude", "opencode"]


def test_every_drivable_harness_can_be_started_and_followed() -> None:
    for harness in session_harnesses():
        assert harness.binary, f"{harness.name} has no binary to run"
        assert harness.build_argv is not None, f"{harness.name} cannot be started"
        assert harness.read_event is not None, f"{harness.name} cannot be followed"


def test_a_project_without_a_harness_table_gets_the_default_set(
    tmp_path: Path,
) -> None:
    """The back-compat guarantee, stated against a real nrw.toml.

    Every project scaffolded before harnesses were selectable has no
    ``[harness]`` table, and must keep getting exactly what it has on disk.
    """
    from nr_workbench.project.config import load_config

    (tmp_path / "nrw.toml").write_text(
        'contract_version = 1\n\n[project]\nname = "old"\n', encoding="utf-8"
    )

    assert load_config(tmp_path).harnesses == DEFAULT_HARNESSES


def test_a_recorded_harness_table_is_read_back(tmp_path: Path) -> None:
    from nr_workbench.project.config import load_config

    (tmp_path / "nrw.toml").write_text(
        'contract_version = 1\n\n[project]\nname = "n"\n\n'
        '[harness]\nkinds = ["claude"]\n',
        encoding="utf-8",
    )

    assert load_config(tmp_path).harnesses == ("claude",)
