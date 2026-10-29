"""How each harness is started unattended, and how its output is read.

One harness per backend: the command line that starts a headless session, and
how to turn its event stream into a progress line.

The two backends differ in a way that matters and is not cosmetic:

* **Claude Code** takes a turn cap (``--max-turns``) and, run with
  ``--permission-mode bypassPermissions``, keeps only its hooks --- the deny
  list in ``settings.json`` stops applying, and the hook is what refuses.
* **OpenCode** has *no turn cap at all*, so a bounded session there means a
  wall-clock timeout and nothing else. But ``--auto`` auto-approves only what
  is not explicitly denied, so its deny list survives beside the plugin.

Both facts were measured against opencode 1.18.18, not read off a docs page.
OpenCode 2 (measured on 2.0.22) changed how a guard plugin is written and
where a session runs; see :func:`opencode_argv`.
"""

from __future__ import annotations

import functools
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: What a harness's pipes carry, both ways. Python opens a text pipe in the
#: locale's encoding -- cp1252 on Windows, ASCII under a bare C locale -- while
#: Claude Code and OpenCode read and write UTF-8 whatever the locale.
HARNESS_ENCODING = "utf-8"


@dataclass(frozen=True)
class Invocation:
    """How to start one headless session.

    Attributes:
        argv: The command line, minus the launcher, which the caller prepends.
        prompt_on_stdin: Feed the prompt to the process's stdin rather than
            naming a file in ``argv``.
        turn_cap: Whether ``turns`` is actually enforced by this harness.
    """

    argv: list[str]
    prompt_on_stdin: bool = False
    turn_cap: bool = True


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------


def claude_argv(
    prompt_file: Path,
    *,
    turns: int,
    model: str | None,
    launcher: list[str] | None = None,
) -> Invocation:
    """Build the Claude Code headless command line.

    The prompt goes on stdin, not as ``-p @<prompt-file>``. Named that way, a
    large prompt never reaches the model: Claude Code sends the bare reference
    in place of the file's text, and the session starts without its
    instructions. Measured with claude 2.1.232: a 20 KB file arrived, a 120 KB
    one did not, and the same 120 KB on stdin arrived whole.

    Args:
        prompt_file: Unused; the prompt is streamed to stdin. The file is still
            written, as the record of what the session was told.
        turns: Cap on agent turns.
        model: Model name, or None for the harness default.
        launcher: Unused, for signature parity with the other backends.

    Returns:
        The invocation.
    """
    del prompt_file, launcher
    argv = [
        "-p",
        "--max-turns",
        str(turns),
        "--output-format",
        "stream-json",
        "--verbose",
        # Headless has nobody to approve a prompt, so without this every Bash
        # call comes back "This command requires approval" and the session
        # accomplishes nothing -- measured, not assumed: a 45-turn run spent
        # all of it being refused and then tried to write itself a
        # settings.local.json to get out.
        #
        # This turns off Claude Code's permission layer, including the `deny`
        # list in .claude/settings.json. It does NOT turn off the PreToolUse
        # hook, which still fires (verified against `nrw promote` under this
        # exact flag).
        "--permission-mode",
        "bypassPermissions",
    ]
    if model:
        argv += ["--model", model]
    return Invocation(argv=argv, prompt_on_stdin=True)


def claude_event(payload: dict[str, Any]) -> tuple[str, str] | None:
    """Read one Claude Code ``stream-json`` event.

    Args:
        payload: One decoded event.

    Returns:
        ``(name, target)`` for a tool call, or None for anything else.
    """
    if payload.get("type") != "assistant":
        return None
    message = payload.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = str(block.get("name") or "tool")
            payload_in = block.get("input")
            target = _claude_target(payload_in) if isinstance(payload_in, dict) else ""
            return name, target
    return None


def _claude_target(tool_input: dict[str, Any]) -> str:
    """The informative field of a Claude Code tool call."""
    for key in ("command", "file_path", "path", "pattern", "prompt"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


# --------------------------------------------------------------------------
# OpenCode
# --------------------------------------------------------------------------


def opencode_argv(
    prompt_file: Path,
    *,
    turns: int,
    model: str | None,
    launcher: list[str] | None = None,
) -> Invocation:
    """Build the OpenCode headless command line.

    The prompt goes on stdin rather than in ``argv``: ``opencode run`` takes
    its message positionally, and a composed session prompt is far too long to
    be safe as a command-line argument. Verified working on 1.18.18.

    ``turns`` is accepted and ignored --- OpenCode has no turn cap. The caller
    is told so via :attr:`Invocation.turn_cap` rather than being left to
    believe the number did something.

    OpenCode 2 runs ``opencode run`` against a shared background service unless
    told ``--standalone``, and the session then belongs to the service: killed
    at its timeout, the ``opencode run`` this package started went away and the
    session's command ran on (measured on 2.0.22). The service also keeps the
    plugins it loaded, whatever ``nrw init`` has written since. ``--standalone``
    gives each session its own server, which exits with it. OpenCode 1 has no
    service and no such flag, so it is left out only for a launcher that says
    it is version 1; one whose version cannot be read gets it, because the
    failure that buys is loud and the other is not.

    Args:
        prompt_file: Unused; the prompt is streamed to stdin.
        turns: Ignored, for signature parity with the other backends.
        model: Model in ``provider/model`` form, or None for the default.
        launcher: The command that starts OpenCode, to ask its version.

    Returns:
        The invocation.
    """
    del prompt_file, turns
    major = _opencode_major(tuple(launcher)) if launcher else None
    argv = [
        "run",
        *([] if major is not None and major < 2 else ["--standalone"]),
        "--format",
        "json",
        # Auto-approves only what is not explicitly denied, so unlike Claude
        # Code's bypassPermissions the deny list in opencode.json still
        # applies. Measured: with the plugin disabled via --pure, `nrw promote`
        # was still refused by the permission rule alone.
        "--auto",
    ]
    if model:
        argv += ["--model", model]
    return Invocation(argv=argv, prompt_on_stdin=True, turn_cap=False)


@functools.lru_cache(maxsize=8)
def _opencode_major(launcher: tuple[str, ...]) -> int | None:
    """The major version an OpenCode launcher reports, or None if it does not."""
    try:
        done = subprocess.run(  # noqa: S603 - the resolved launcher, no shell
            [*launcher, "--version"],
            capture_output=True,
            text=True,
            encoding=HARNESS_ENCODING,
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.search(r"(\d+)\.\d+", f"{done.stdout} {done.stderr}")
    return int(found.group(1)) if found else None


@dataclass(frozen=True)
class Outcome:
    """What a finished probe run answered.

    Attributes:
        reply: The final assistant text, empty if there was none.
        model: Which model answered, as the harness reported it.
        cost_usd: What the call cost, or None if the harness does not say.
        failed: Whether the harness itself reported an error.
        answered: Whether a completed exchange was found at all. False means
            the harness died before reaching the model, which is what a bad
            provider configuration usually looks like.
    """

    reply: str = ""
    model: str = ""
    cost_usd: float | None = None
    failed: bool = False
    answered: bool = False


def claude_outcome(stdout: str) -> Outcome:
    """Read Claude Code's final ``result`` event.

    Args:
        stdout: The harness's ``stream-json`` events.

    Returns:
        What it answered.
    """
    event = None
    for line in stdout.splitlines():
        try:
            decoded = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(decoded, dict) and decoded.get("type") == "result":
            event = decoded
    if event is None:
        return Outcome()

    cost = event.get("total_cost_usd")
    return Outcome(
        reply=str(event.get("result") or "").strip(),
        model=", ".join(sorted(event.get("modelUsage") or {})),
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        failed=bool(event.get("is_error")),
        answered=True,
    )


def opencode_outcome(stdout: str) -> Outcome:
    """Read OpenCode's ``--format json`` stream.

    There is no single result event. The reply is the last ``text`` part, and
    cost accumulates across ``step-finish`` parts --- which report ``0`` on a
    free model, so a zero cost is real rather than missing.

    Args:
        stdout: The harness's JSON events.

    Returns:
        What it answered.
    """
    reply = ""
    cost = 0.0
    answered = False
    for line in stdout.splitlines():
        try:
            decoded = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(decoded, dict):
            continue
        part = decoded.get("part")
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                reply = text.strip()
                answered = True
        elif part.get("type") == "step-finish":
            answered = True
            step_cost = part.get("cost")
            if isinstance(step_cost, int | float):
                cost += float(step_cost)

    return Outcome(reply=reply, cost_usd=cost if answered else None, answered=answered)


def opencode_event(payload: dict[str, Any]) -> tuple[str, str] | None:
    """Read one OpenCode ``--format json`` event.

    The shape, captured from 1.18.18::

        {"type": "tool_use", "part": {"type": "tool", "tool": "bash",
         "state": {"status": "...", "input": {"command": "..."}}}}

    Args:
        payload: One decoded event.

    Returns:
        ``(name, target)`` for a tool call, or None for anything else.
    """
    if payload.get("type") != "tool_use":
        return None
    part = payload.get("part")
    if not isinstance(part, dict):
        return None
    name = str(part.get("tool") or "tool")
    state = part.get("state")
    tool_input = state.get("input") if isinstance(state, dict) else None
    target = ""
    if isinstance(tool_input, dict):
        for key in ("command", "filePath", "file_path", "path", "pattern"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                target = value
                break
    return name, target
