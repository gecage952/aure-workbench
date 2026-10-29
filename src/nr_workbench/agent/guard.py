"""Refuse the commands an unattended agent must not run.

Two things an agent may not do without a person: publish a result as the answer
(``nrw promote``), and send it outside the project (``nrw isaac export
--upload``). A third is subtler and matters more --- any flag whose whole job
is to override a check that said no: ``--force`` everywhere it appears, and
``nrw init --nested``, which would otherwise let a project be scaffolded
inside another one with nothing to reconcile the two. Every one of these
exists because some check said no, and each either records that it was forced
or backs up what it displaced. An agent reaching for one has hit exactly the
situation a human is supposed to see.

This runs as a ``PreToolUse`` hook in Claude Code, and from a plugin in
OpenCode, so the refusal happens before the command executes and does not
depend on the model agreeing.

The refusal always names what to do instead. An agent that is only told "no"
retries; one that is told "write it in ESCALATIONS.md and stop" has somewhere
to put the decision.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from dataclasses import dataclass

#: Exit code a PreToolUse hook uses to block a call and show the model why.
BLOCK = 2

#: The environment variable that tells nrw it is being driven unattended.
AGENT_ENV = "NRW_AGENT"


#: Why each action is refused, and what to do instead.
_REASONS = {
    "promote": (
        "Marking a fit as the answer is a person's decision, and the reason "
        "recorded with it is the part that matters in a year. Write what you "
        "would have said in ESCALATIONS.md, with the fit id and the evidence, "
        "and stop."
    ),
    "upload": (
        "Publishing to the ISAAC Portal shares the data and the fitted model "
        "outside this project, and a pushed record is not straightforwardly "
        "retractable. Export without --upload if you want the records on "
        "disk, and say in ESCALATIONS.md that they are ready to send."
    ),
    "force": (
        "Every --force in nr-workbench exists because a check said no: "
        "drifted inputs, a hand-edited script, a directory somebody else "
        "wrote. Reaching for one is exactly the situation a "
        "person is meant to see. Record what was refused and why you think it "
        "should be overridden in ESCALATIONS.md."
    ),
    "aure-run": (
        "An AuRE run makes billed language-model calls for as long as it takes "
        "-- and under a harness it would be a model handing the judgement you "
        "wanted to a second, weaker one. Write the setup with `nrw aure new`, "
        "say in ESCALATIONS.md that it is ready to run, and stop. "
        "`nrw aure run --dry-run` is allowed and validates it."
    ),
    "experiment": (
        "Which sample a run belongs to, under what condition, whether it is "
        "used at all, and where the experiment's data is read from are a "
        "person's claims about the experiment -- like a promotion, they decide "
        "what every later fit is built from. Write in ESCALATIONS.md what you "
        "would assign, apply or change, and why, and stop. `nrw experiment "
        "status`, and `apply`, `adopt` or `settings` without --write, are "
        "allowed: they show what would happen and change nothing."
    ),
    "reset": (
        "`nrw sample reset` deletes every fit and model of a sample: the record "
        "of what was tried, and why, which the next person reading the project "
        "needs. Write in ESCALATIONS.md what you would reset, and why, and stop. "
        "`nrw sample reset --dry-run` is allowed: it shows what would go."
    ),
    "curate": (
        "Which fits are good, set aside or deleted is a person's judgement, like "
        "a promotion: a star, a discarded fit and a deleted one all change what "
        "the next person reading the project believes. Write in ESCALATIONS.md "
        "which fits you would star, discard or delete, and why, and stop."
    ),
    "nested": (
        "`nrw init --nested` was refused because an ancestor directory is "
        "already a project -- this would create a second nrw.toml, a second "
        ".nrw/, a second samples/ tree, nested inside the first and "
        "reconciled with nothing. If a sample is what you need, `nrw sample "
        "new <ID>` adds one to the existing project. If a nested project is "
        "genuinely correct here, say so in ESCALATIONS.md and stop."
    ),
    "tiled": (
        "Tiled is reached with the person's own login: a push publishes this "
        "project's claims to everyone on the experiment, a login or a logout "
        "acts as the person, and a read could reach any experiment they may "
        "read. Say in ESCALATIONS.md what you would push or fetch, and stop."
    ),
}

#: The commands of Tiled's own command line: each one either acts with the
#: person's login, or serves data. `tiled` before anything else -- `grep
#: tiled` -- is no call of it.
_TILED_CLI = (
    "login",
    "logout",
    "whoami",
    "tree",
    "register",
    "api_key",
    "admin",
    "profile",
    "serve",
    "catalog",
)


@dataclass(frozen=True)
class Verdict:
    """Whether a command may run, and why not.

    Attributes:
        allowed: Whether to let it through.
        rule: Which rule refused, for the record.
        reason: What to tell the caller, including what to do instead.
    """

    allowed: bool
    rule: str = ""
    reason: str = ""


def judge(command: str) -> Verdict:
    """Decide whether an unattended agent may run this command.

    Args:
        command: The shell command about to be executed.

    Returns:
        The verdict. Anything that is not a recognised ``nrw`` risk is
        allowed --- this is a targeted refusal, not a sandbox, and pretending
        otherwise would give false confidence.
    """
    for piece in _segments(command):
        for words in _words(piece):
            verdict = _judge_one(words)
            if not verdict.allowed:
                return verdict
    return Verdict(allowed=True)


def _words(piece: str) -> list[list[str]]:
    """The words of *piece*, a list for each command in it.

    ``shlex`` is not bash: it cannot split ``$'...'`` quoting, nor the text
    after a quote opened in a here-document's body, and bash runs both. Such a
    piece is judged line by line, and a line ``shlex`` still cannot split is
    split on whitespace, so a refused command in it is found all the same.
    """
    try:
        return [shlex.split(piece)]
    except ValueError:
        pass
    words: list[list[str]] = []
    for line in piece.split("\n"):
        try:
            words.append(shlex.split(line))
        except ValueError:
            words.append(line.split())
    return words


#: Characters that end one command and begin another. Split on these *in the
#: raw string*, before tokenising: `shlex` keeps `ls;` as a single token and
#: treats a newline as ordinary whitespace, so a token-level split silently
#: merges `git add -A\nnrw promote abc` into one command and judges only its
#: head. Parentheses and backticks are here because `$(nrw promote x)` runs
#: too.
SEPARATORS = "\n;&|()`"


def _segments(command: str) -> list[str]:
    """Split a command line into the commands it will actually run.

    Quote-aware, because `echo 'a;b'` is one command and splitting inside the
    quotes would invent a second. A comment is dropped to the end of its line:
    bash will not run it, so judging it produces refusals nobody can act on.

    Args:
        command: The raw shell text.

    Returns:
        The pieces, in order, with empties removed.
    """
    pieces: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    comment = False

    for index, char in enumerate(command):
        if comment:
            if char != "\n":
                continue
            comment = False  # the newline ends it, and the piece below
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and quote != "'":
            current.append(char)
            escaped = True
            continue
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
            current.append(char)
            continue
        if char == "#" and (index == 0 or command[index - 1].isspace()):
            comment = True
            continue
        if char in SEPARATORS:
            pieces.append("".join(current))
            current = []
            continue
        current.append(char)

    pieces.append("".join(current))
    return [p for p in pieces if p.strip()]


#: What `nrw fit` does to a fit rather than with it: each is a person's call.
_CURATION = ("star", "unstar", "discard", "restore", "delete")


def _judge_one(tokens: list[str]) -> Verdict:
    """Judge a single command."""
    words = [t for t in tokens if not t.startswith("-")]
    if not words:
        return Verdict(allowed=True)

    # `bash -c '...'` and `eval '...'` hide a whole command line inside one
    # token. Judging the wrapper alone sees nothing, so judge the inside too.
    if wrapped := _wrapped_command(tokens):
        inner = judge(wrapped)
        if not inner.allowed:
            return inner

    # Tiled's own command line, `tiled` or `python -m tiled`, then one of its
    # commands: the person's login, used by another program.
    for place, word in enumerate(words[:-1]):
        if word.rsplit("/", 1)[-1] == "tiled" and words[place + 1] in _TILED_CLI:
            return Verdict(allowed=False, rule="tiled", reason=_REASONS["tiled"])

    # Reached through `nrw`, `nr-workbench`, or `python -m nr_workbench.cli`.
    # Every token, not the first few: an env-var prefix (`NRW_AGENT= nrw
    # promote x`), a `cd` first, or a `then`/`do` keyword all push the real
    # command past any fixed window, and each of those is something an agent
    # writes without meaning to evade anything.
    if not any(_is_nrw(word) for word in words):
        return Verdict(allowed=True)

    flags = {t.split("=", 1)[0] for t in tokens if t.startswith("-")}
    subcommands = [w for w in words if _is_subcommand(w)]

    if "promote" in subcommands:
        return Verdict(allowed=False, rule="promote", reason=_REASONS["promote"])

    if "tiled" in subcommands:
        return Verdict(allowed=False, rule="tiled", reason=_REASONS["tiled"])

    if "experiment" in subcommands and (
        {"assign", "release"} & set(subcommands)
        or ({"apply", "adopt", "settings"} & set(subcommands) and "--write" in flags)
    ):
        return Verdict(allowed=False, rule="experiment", reason=_REASONS["experiment"])

    if "sample" in subcommands and "reset" in subcommands and "--dry-run" not in flags:
        return Verdict(allowed=False, rule="reset", reason=_REASONS["reset"])

    # `fit` then the action, next to each other: `nrw fit run x.py --note
    # discard` is a fit run, not a discard.
    for place, word in enumerate(words[:-1]):
        if word == "fit" and words[place + 1] in _CURATION:
            return Verdict(allowed=False, rule="curate", reason=_REASONS["curate"])

    # `isaac push` sends the records, and the key, off the machine: to publish
    # or only to validate, it is not done unattended.
    if "isaac" in subcommands and ("--upload" in flags or "push" in subcommands):
        return Verdict(allowed=False, rule="upload", reason=_REASONS["upload"])

    if "--force" in flags:
        return Verdict(allowed=False, rule="force", reason=_REASONS["force"])

    if "init" in subcommands and "--nested" in flags:
        return Verdict(allowed=False, rule="nested", reason=_REASONS["nested"])

    # `--dry-run` validates the setup and calls no endpoint, so it stays
    # allowed: an agent that can check its own work and report is the point.
    if "aure" in subcommands and "run" in subcommands and "--dry-run" not in flags:
        return Verdict(allowed=False, rule="aure-run", reason=_REASONS["aure-run"])

    return Verdict(allowed=True)


def _wrapped_command(tokens: list[str]) -> str:
    """A command line carried inside this one, or an empty string.

    Only one level, and only the shapes that turn up by accident. Chasing
    every indirection is a losing game --- this is a targeted refusal, not a
    sandbox --- but `bash -c` is common enough to be worth the ten lines.
    """
    head = tokens[0].rsplit("/", 1)[-1] if tokens else ""
    if head in {"bash", "sh", "zsh", "dash"} and "-c" in tokens:
        index = tokens.index("-c")
        return tokens[index + 1] if index + 1 < len(tokens) else ""
    if head == "eval":
        return " ".join(tokens[1:])
    return ""


def _is_nrw(word: str) -> bool:
    """Whether a token invokes this package.

    ``python -m nr_workbench.cli`` is a working entry point --- ``cli.py`` has
    a ``__main__`` guard --- and it is the natural fallback when ``nrw`` is not
    on PATH, so it has to count.
    """
    stem = word.rsplit("/", 1)[-1]
    return stem in {"nrw", "nr-workbench", "nr_workbench"} or stem.startswith(
        ("nr_workbench.", "nr-workbench.")
    )


def _is_subcommand(word: str) -> bool:
    """Whether a token looks like a subcommand rather than a path or value."""
    return bool(re.fullmatch(r"[a-z][a-z-]*", word))


def run_guard(command: str | None = None) -> None:
    """Entry point for the hook and for `nrw agent guard`.

    Reads a Claude Code ``PreToolUse`` payload from stdin when no command is
    given, so it can be wired straight into ``.claude/settings.json``.

    Args:
        command: The command to judge. Read from stdin when omitted.

    Raises:
        SystemExit: ``BLOCK`` when the command is refused, ``0`` otherwise.
    """
    if command is None:
        command = _command_from_stdin()

    verdict = judge(command or "")
    if verdict.allowed:
        raise SystemExit(0)

    print(
        f"Refused by nr-workbench ({verdict.rule}): {verdict.reason}",
        file=sys.stderr,
    )
    raise SystemExit(BLOCK)


def _command_from_stdin() -> str:
    """The command out of a PreToolUse payload, or an empty string.

    A payload we cannot read must not block everything --- an unreadable hook
    input is our bug, and failing closed on it would make the project
    unusable rather than safe.
    """
    if sys.stdin.isatty():
        return ""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        return ""
    if not isinstance(payload, dict):
        return ""
    tool_input = payload.get("tool_input")
    if isinstance(tool_input, dict):
        return str(tool_input.get("command") or "")
    return ""


def agent_is_driving() -> bool:
    """Whether an unattended harness is running this command.

    Used to decide who does the judging. nr-workbench can call a configured
    LLM endpoint to say whether a fit looks physically sensible --- but when a
    coding harness is driving, that harness *is* a language model, and a
    better one than the endpoint is likely to be. Asking a second, weaker
    model and handing its verdict back is not a fallback; it substitutes for
    the judgement we actually want and then reads as evidence.

    So: under an agent, nothing here calls out to a model. The material that
    would have been sent is printed for the harness to read instead.
    """
    import os

    return bool(os.environ.get(AGENT_ENV))
