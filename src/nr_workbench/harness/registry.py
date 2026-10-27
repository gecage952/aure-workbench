"""The known harnesses and how to look them up."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nr_workbench.harness import driver


class HarnessError(Exception):
    """Raised when a project names a harness this package does not know."""


@dataclass(frozen=True)
class Harness:
    """One coding assistant a project can be scaffolded for.

    Most fields are optional because most harnesses do not do every job. A
    ``None`` means "this assistant has no such thing", not "not filled in yet":
    Copilot genuinely has no settings file to write ``NRW_BIN`` into, and
    inventing a path for it would produce a file nothing reads.

    Attributes:
        name: Registry key, used in ``nrw.toml`` and ``--harness``.
        title: Human-readable name, for reports a scientist reads.
        template_subdir: Subdirectory of ``templates/harness/`` holding this
            harness's scaffold files, or None if it has none of its own.
        agents_dir: Where thin dispatcher stubs go, relative to the project
            root, or None if the harness does not read subagent files.
        dispatcher_frontmatter: Extra YAML frontmatter lines for this harness's
            dispatchers. Claude Code understands a ``tools:`` guardrail;
            Copilot's schema rejects those names, so it gets none.
        local_settings: Machine-local settings file that can carry ``NRW_BIN``,
            relative to the project root, or None.
        vscode_extension: Marketplace id to recommend in
            ``.vscode/extensions.json``, or None.
        drives_sessions: Whether ``nrw agent run`` can drive this harness
            unattended.
        binary: The executable that starts it, when it drives sessions.
        build_argv: Builds the headless command line. See
            :func:`nr_workbench.harness.driver.claude_argv`.
        verify_guard: Raises :class:`~nr_workbench.harness.driver.GuardMissing`
            if this project has not installed the limit this harness needs.
        read_event: Turns one decoded output event into ``(tool, target)``, or
            None for events it does not recognise.
        read_outcome: Turns a finished run's stdout into what it answered, for
            `nrw check-llm`.
    """

    name: str
    title: str
    template_subdir: str | None = None
    agents_dir: str | None = None
    dispatcher_frontmatter: tuple[str, ...] = ()
    local_settings: str | None = None
    vscode_extension: str | None = None
    drives_sessions: bool = False
    binary: str | None = None
    build_argv: Callable[..., driver.Invocation] | None = field(
        default=None, compare=False
    )
    verify_guard: Callable[[Path], None] | None = field(default=None, compare=False)
    read_event: Callable[[dict[str, Any]], tuple[str, str] | None] | None = field(
        default=None, compare=False
    )
    read_outcome: Callable[[str], driver.Outcome] | None = field(
        default=None, compare=False
    )


#: Every harness this package knows, in the order their files are planned.
#:
#: The order is load-bearing only in that it keeps `nrw init` output stable
#: between runs --- a set would reorder the report for no reason.
HARNESSES: tuple[Harness, ...] = (
    Harness(
        name="claude",
        title="Claude Code",
        template_subdir="claude",
        agents_dir=".claude/agents",
        # Understood by Claude Code, and the reason its dispatchers are
        # read-only.
        dispatcher_frontmatter=("tools: Read, Grep, Glob, Bash",),
        local_settings=".claude/settings.local.json",
        vscode_extension="anthropic.claude-code",
        drives_sessions=True,
        binary="claude",
        build_argv=driver.claude_argv,
        verify_guard=driver.claude_guard,
        read_event=driver.claude_event,
        read_outcome=driver.claude_outcome,
    ),
    Harness(
        name="opencode",
        title="OpenCode",
        template_subdir="opencode",
        # Plural is the current convention; the singular `agent/` is kept only
        # for backwards compatibility upstream, so new projects use this.
        agents_dir=".opencode/agents",
        # OpenCode's own vocabulary: a file in the agents directory is a
        # primary agent unless it says otherwise, and a dispatcher is something
        # the main session delegates to.
        dispatcher_frontmatter=("mode: subagent",),
        vscode_extension="sst-dev.opencode",
        # Enabled only after measuring, against opencode 1.18.18, that the
        # guard plugin's `tool.execute.before` hook actually fires under
        # `--auto` and blocks the call.
        drives_sessions=True,
        binary="opencode",
        build_argv=driver.opencode_argv,
        verify_guard=driver.opencode_guard,
        read_event=driver.opencode_event,
        read_outcome=driver.opencode_outcome,
    ),
    Harness(
        name="copilot",
        title="GitHub Copilot",
        # No template subtree: Copilot's instructions live in
        # `.github/copilot-instructions.md`, which is the body the other
        # harnesses point at too, so it belongs to the project rather than to
        # Copilot. No settings file, no hook, and nothing to drive unattended.
        agents_dir=".github/agents",
    ),
)

#: What a project gets when it does not say. Exactly what every project got
#: before harnesses were selectable, so an existing project is unaffected.
DEFAULT_HARNESSES: tuple[str, ...] = ("claude", "copilot")

_BY_NAME = {harness.name: harness for harness in HARNESSES}


def known_names() -> tuple[str, ...]:
    """Every harness name this package knows, in registry order.

    Returns:
        The registry keys, suitable for a ``--harness`` help string.
    """
    return tuple(harness.name for harness in HARNESSES)


def resolve(names: Iterable[str]) -> tuple[Harness, ...]:
    """Look up harnesses by name, in registry order.

    Duplicates are dropped and the result is ordered by the registry rather
    than by the caller, so the same set always plans the same files in the
    same sequence however it was spelled.

    Args:
        names: Harness names, e.g. from ``nrw.toml`` or ``--harness``.

    Returns:
        The matching harnesses, deduplicated and in registry order.

    Raises:
        HarnessError: If any name is not a known harness, or if the resolved
            set is empty.
    """
    wanted = {str(name).strip().lower() for name in names if str(name).strip()}
    unknown = sorted(wanted - _BY_NAME.keys())
    if unknown:
        raise HarnessError(
            f"Unknown harness {', '.join(repr(name) for name in unknown)}. "
            f"Known harnesses: {', '.join(known_names())}."
        )
    if not wanted:
        raise HarnessError(
            "No harness selected. A project with no harness gets no assistant "
            "instructions, no subagents and no limits -- which is almost "
            f"certainly not what was meant. Known harnesses: "
            f"{', '.join(known_names())}."
        )
    return tuple(harness for harness in HARNESSES if harness.name in wanted)


def agent_dirs(harnesses: Iterable[Harness]) -> tuple[str, ...]:
    """The dispatcher directories for a set of harnesses.

    Args:
        harnesses: The harnesses to collect directories from.

    Returns:
        POSIX paths relative to the project root, skipping harnesses that do
        not read subagent files.
    """
    return tuple(
        harness.agents_dir for harness in harnesses if harness.agents_dir is not None
    )


def session_harnesses() -> tuple[Harness, ...]:
    """Every harness ``nrw agent run`` can drive unattended.

    Returns:
        The harnesses with :attr:`Harness.drives_sessions` set.
    """
    return tuple(harness for harness in HARNESSES if harness.drives_sessions)
