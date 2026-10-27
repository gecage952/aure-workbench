"""``nrw doctor`` -- report on the environment and the current project.

Deliberately tolerant: every check degrades to a reported status rather than an
exception, because the whole point is to run when something is wrong.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

import click

#: Packages worth reporting a version for. `aure` is listed last because it is
#: the one most likely to be a git pin whose reported version lies.
_PACKAGES = (
    "numpy",
    "scipy",
    "matplotlib",
    "pydantic",
    "click",
    "refl1d",
    "bumps",
    "aure",
)

_OK = "ok"
_MISSING = "missing"

#: How each status prints. Shared with `nrw check-llm`, which reports the same
#: four states about a live call rather than about configuration.
STATUS_MARKERS = {"ok": "✓", "missing": "·", "warn": "!", "error": "✗"}


@dataclass
class Check:
    """One diagnostic line.

    Attributes:
        name: Short label.
        status: ``ok``, ``missing``, ``warn``, or ``error``.
        detail: Human-readable detail, e.g. a version or an explanation.
    """

    name: str
    status: str
    detail: str = ""


def _package_version(name: str) -> str | None:
    """Return an installed package's version, or None if absent."""
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _aure_commit() -> str | None:
    """Recover the git commit aure was installed from, if recorded.

    Through v0.1.x aure's ``pyproject.toml`` reported ``0.1.0`` across every
    tag. v1.0.0 reports a real version, but we pin a SHA on ``main``, so the
    version string still cannot identify a build. pip records the actual source
    in ``direct_url.json`` for a VCS install, which is the only reliable
    identifier -- and it is what belongs in a provenance record.

    Returns:
        The resolved commit SHA, a URL, or None if not determinable.
    """
    try:
        dist = metadata.distribution("aure")
    except metadata.PackageNotFoundError:
        return None
    try:
        raw = dist.read_text("direct_url.json")
    except Exception:
        return None
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        return None
    vcs = info.get("vcs_info") or {}
    return vcs.get("commit_id") or info.get("url")


def collect_checks() -> list[Check]:
    """Run every diagnostic and return the results.

    Returns:
        Checks in display order: interpreter, packages, then project state.
    """
    checks: list[Check] = [
        Check("python", _OK, platform.python_version()),
        Check("platform", _OK, platform.platform()),
    ]

    from nr_workbench import __version__

    checks.append(Check("nr-workbench", _OK, __version__))

    for name in _PACKAGES:
        version = _package_version(name)
        if version is None:
            status = _MISSING
            detail = "not installed"
            if name == "aure":
                detail = "not installed (pip install -e '.' pulls it from git)"
            checks.append(Check(name, status, detail))
            continue
        detail = version
        if name == "aure":
            commit = _aure_commit()
            # We pin a SHA on aure's main, so two installs can share a
            # declared version and be different code. Show the commit; it is
            # the only thing that identifies what is actually here.
            detail = (
                f"{version} @ {commit[:12]}"
                if commit and len(commit) >= 12
                else version
            )
        checks.append(Check(name, _OK, detail))

    checks.extend(_llm_checks())
    checks.extend(_tiled_checks())
    checks.extend(_project_checks())
    checks.extend(_agent_checks())
    checks.extend(_toolpath_checks())
    checks.extend(_sharing_checks())
    return checks


def _sharing_checks() -> list[Check]:
    """Report whether this project is safe for a second person to clone.

    `nrw doctor` is what the handoff skill tells a new analyst to run, so it is
    where the answer belongs. Two questions, both invisible to the person who
    caused them because both work perfectly on the machine that did:

    * are the machine-local files -- which hold absolute paths -- committable?
    * does the local settings `PATH` lead somewhere that is not this machine's
      `nrw`, i.e. did it arrive with a clone?
    """
    from nr_workbench.project import toolpath
    from nr_workbench.project.layout import ProjectLayout, ProjectNotFoundError

    try:
        root = ProjectLayout.discover().root
    except ProjectNotFoundError:
        return []

    checks: list[Check] = []
    committable = toolpath.unignored_paths(root, toolpath.MACHINE_LOCAL)
    if committable:
        tracked = set(toolpath.MACHINE_LOCAL) & set(
            _tracked(root, toolpath.MACHINE_LOCAL)
        )
        listed = ", ".join(committable)
        remedy = (
            f"`git rm --cached {' '.join(sorted(tracked))}`"
            if tracked
            else "add them to .gitignore"
        )
        checks.append(
            Check(
                "sharing",
                "warn",
                f"{listed} hold this machine's absolute paths and could be "
                f"committed; {remedy}",
            )
        )
    else:
        checks.append(Check("sharing", _OK, "machine-local paths cannot be committed"))

    stale = _foreign_path(root)
    if stale:
        checks.append(
            Check(
                "settings PATH",
                "warn",
                f"starts with {stale}, which is not this machine's `nrw`; it "
                "was probably committed elsewhere and cloned here -- "
                "`nrw doctor --fix-path`, or delete the entry",
            )
        )
    return checks


def _tracked(root: Path, relpaths: tuple[str, ...]) -> tuple[str, ...]:
    """Which of ``relpaths`` git is tracking in ``root``."""
    from nr_workbench.project import vcs

    known = set(vcs.tracked_files(root))
    return tuple(path for path in relpaths if path in known)


def _foreign_path(root: Path) -> str:
    """The local settings `PATH`'s first entry, if it is not ours.

    Args:
        root: Project root.

    Returns:
        The offending entry, or an empty string when `PATH` is absent, already
        correct, or `nrw` cannot be resolved to compare against.
    """
    from nr_workbench.project import toolpath

    resolved = toolpath.nrw_executable()
    if resolved is None:
        return ""
    settings = root / toolpath.LOCAL_SETTINGS
    if not settings.is_file():
        return ""
    try:
        document = json.loads(settings.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    environment = document.get("env") if isinstance(document, dict) else None
    if not isinstance(environment, dict):
        return ""
    return toolpath.stale_path_entry(environment, resolved)


def _toolpath_checks() -> list[Check]:
    """Report whether an assistant's shell could find ``nrw``.

    This process found it, or it would not be running -- so the check has to
    probe a *scrubbed* login shell rather than ask about its own environment.
    See :func:`nr_workbench.project.toolpath.resolvable_in_fresh_shell`.

    The failure is worth a line of its own because of how it presents: not as
    an error, but as an interactive session quietly spending its first ten
    turns searching the filesystem for a binary, and then prefixing every
    command it runs thereafter.
    """
    from nr_workbench.project import toolpath
    from nr_workbench.project.layout import ProjectLayout, ProjectNotFoundError

    if toolpath.resolvable_in_fresh_shell():
        return [Check("nrw on PATH", _OK, "a fresh login shell resolves `nrw`")]

    try:
        root = ProjectLayout.discover().root
    except ProjectNotFoundError:
        return [
            Check(
                "nrw on PATH",
                "warn",
                "a fresh login shell cannot resolve `nrw`; an assistant session "
                "started from your editor will not find it",
            )
        ]

    have_shim = (root / toolpath.SHIM_RELPATH).is_file()
    settings = root / toolpath.LOCAL_SETTINGS
    have_env = False
    if settings.is_file():
        try:
            document = json.loads(settings.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            document = {}
        environment = document.get("env") if isinstance(document, dict) else None
        have_env = isinstance(environment, dict) and bool(
            environment.get(toolpath.NRW_BIN_ENV)
        )

    if have_shim and have_env:
        detail = (
            f"not on PATH, but {toolpath.SHIM_RELPATH} and "
            f"${toolpath.NRW_BIN_ENV} are installed; `nrw doctor --fix-path` "
            "makes bare `nrw` work too"
        )
    else:
        detail = (
            "a fresh login shell cannot resolve `nrw`; run `nrw init` to install "
            f"{toolpath.SHIM_RELPATH}, or `nrw doctor --fix-path`"
        )
    return [Check("nrw on PATH", "warn", detail)]


def _project_harnesses(root: Path) -> tuple[str, ...]:
    """The harnesses a project records, or the default set if it cannot say.

    Args:
        root: Project root.

    Returns:
        Harness names.
    """
    from nr_workbench.harness import DEFAULT_HARNESSES
    from nr_workbench.project.config import ProjectConfigError, load_config

    try:
        return load_config(root).harnesses
    except ProjectConfigError:
        return DEFAULT_HARNESSES


def _agent_checks() -> list[Check]:
    """Report whether an unattended session could run here, and be limited.

    Both halves matter and they fail independently. A project can have the
    harness installed and no limits configured, which is worse than having
    neither -- it is the state where `nrw agent run` works and nothing stops a
    promotion. A project scaffolded before `.claude/settings.json` existed is
    exactly that state, so this says so rather than staying quiet.
    """
    import os

    from nr_workbench.agent.session import (
        DEFAULT_HARNESS,
        HARNESS_ENV,
        resolve_harness,
    )
    from nr_workbench.harness import resolve
    from nr_workbench.project.layout import ProjectLayout, ProjectNotFoundError

    launcher = resolve_harness()
    override = (os.environ.get(HARNESS_ENV) or "").strip()
    if launcher:
        version = _harness_version(launcher[0])
        where = " ".join(launcher)
        detail = f"{version} at {where}" if version else where
        if override:
            detail += f"  [{HARNESS_ENV}]"
        checks = [Check("harness", _OK, detail)]
    else:
        wanted = override or DEFAULT_HARNESS
        checks = [
            Check(
                "harness",
                _MISSING,
                f"{wanted!r} not on PATH; `nrw agent run` needs a coding "
                f"harness (set {HARNESS_ENV} to use your own)",
            )
        ]

    try:
        root = ProjectLayout.discover().root
    except ProjectNotFoundError:
        return checks

    # Which limits apply depends on what the project is scaffolded for. Telling
    # a project to run `nrw init` for a file `nrw init` will not write there is
    # worse than saying nothing: it sends someone to re-run a command, see no
    # change, and conclude the check is broken.
    configured_harnesses = _project_harnesses(root)
    drivers = [h for h in resolve(configured_harnesses) if h.drives_sessions]
    if not drivers:
        checks.append(
            Check(
                "agent limits",
                "warn",
                "this project is not scaffolded for a harness that can be run "
                "unattended, so `nrw agent run` will refuse to start here. "
                "`nrw init --harness claude` sets one up.",
            )
        )
        return checks

    # Each driveable harness gets its own line: they check different files, and
    # a project set up for two can easily have one guarded and one not.
    from nr_workbench.harness.driver import GuardMissing

    for harness in drivers:
        if harness.name == "claude" or harness.verify_guard is None:
            continue
        try:
            harness.verify_guard(root)
        except GuardMissing as exc:
            checks.append(
                Check(f"{harness.name} limits", "warn", str(exc).splitlines()[0])
            )
        else:
            checks.append(
                Check(
                    f"{harness.name} limits",
                    _OK,
                    "guard plugin and deny rules installed",
                )
            )

    if not any(h.name == "claude" for h in drivers):
        return checks

    settings = root / ".claude" / "settings.json"
    if not settings.is_file():
        checks.append(
            Check(
                "agent limits",
                "warn",
                "no .claude/settings.json; run `nrw init` to add the hook that "
                "refuses promote, curation, publishing and --force",
            )
        )
        return checks

    try:
        configured = json.loads(settings.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        checks.append(Check("agent limits", "error", f"{settings.name}: {exc}"))
        return checks

    if not isinstance(configured, dict):
        checks.append(
            Check("agent limits", "error", f"{settings.name} is not a JSON object")
        )
        return checks

    permissions = configured.get("permissions")
    denied = len(
        (permissions.get("deny") or []) if isinstance(permissions, dict) else []
    )
    where = guard_hook_event(configured)

    if where == "PreToolUse":
        detail = f"PreToolUse hook + {denied} deny rule(s)"
        status = _OK
    elif where:
        # The misconfiguration most worth naming: the hook is there, so a
        # substring test would call this fine, but PostToolUse runs *after*
        # the command and refuses nothing.
        detail = f"`nrw agent guard` is on {where}, not PreToolUse -- it cannot refuse"
        status = "warn"
    else:
        detail = f"{denied} deny rule(s), but no `nrw agent guard` hook"
        status = "warn"

    checks.append(Check("agent limits", status, detail))
    return checks


def guard_hook_event(configured: dict[str, Any]) -> str:
    """Which hook event runs `nrw agent guard`, or an empty string.

    Walks the structure rather than searching the serialised text: a hook
    registered under the wrong event, or matched against the wrong tool, is
    present in the JSON and does nothing.
    """
    hooks = configured.get("hooks")
    if not isinstance(hooks, dict):
        return ""
    for event, entries in hooks.items():
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            matcher = str(entry.get("matcher", ""))
            if event == "PreToolUse" and "Bash" not in matcher:
                continue
            for hook in entry.get("hooks") or []:
                command = str(hook.get("command", "")) if isinstance(hook, dict) else ""
                if "agent guard" in command:
                    return str(event)
    return ""


def _harness_version(binary: str) -> str | None:
    """The installed harness version, or None if it will not say."""
    import subprocess

    try:
        result = subprocess.run(  # noqa: S603 - argv from shutil.which
            [binary, "--version"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip().split()[0] if result.stdout.strip() else None


def _llm_checks() -> list[Check]:
    """Report the language-model endpoint, and where its settings came from.

    Only `nrw model new --from-notes` needs one, so an absent endpoint is a
    normal state rather than a problem -- but a *misconfigured* one is worth
    seeing, and so is the fact that a setting arrived from a file the user may
    not have known was being read.
    """
    from nr_workbench.aure_adapter import llm_info
    from nr_workbench.env import describe, load_env

    sources = load_env()
    checks: list[Check] = []

    if sources.files:
        checks.append(
            Check("config", _OK, ", ".join(str(path) for path in sources.files))
        )
    else:
        checks.append(Check("config", _MISSING, "no .env, ~/.nrw or ~/.aure found"))

    info = llm_info()
    if info.get("available"):
        endpoint = f"{info.get('provider')}/{info.get('model')}"
        if info.get("base_url"):
            endpoint += f" @ {info['base_url']}"
        checks.append(Check("llm", _OK, endpoint))
    else:
        checks.append(
            Check(
                "llm",
                _MISSING,
                "no endpoint; `nrw model new --print-prompt` works without one",
            )
        )

    settings = describe()
    if settings:
        checks.append(
            Check(
                "llm settings",
                _OK,
                ", ".join(f"{k}={v}" for k, v in sorted(settings.items())),
            )
        )
    return checks


def _tiled_checks() -> list[Check]:
    """Where ``nrw tiled`` would go, and with what: read from files, never sent."""
    import importlib.util

    from nr_workbench.tiled.server import INSTALL, TiledSyncError, server

    try:
        chosen = server()
    except TiledSyncError as exc:
        return [Check("tiled", "warn", str(exc))]
    detail = f"{chosen.url} ({chosen.source})"
    if importlib.util.find_spec("tiled") is None:
        return [
            Check(
                "tiled",
                _MISSING,
                f"{detail}; the client is not installed ({INSTALL})",
            )
        ]
    if chosen.key is not None:
        said = f"{detail}; TILED_API_KEY is sent, set in {chosen.key_source}"
        return [Check("tiled", _OK, said)]
    # Whether the client keeps a login is its own to say: doctor asks no server.
    said = f"{detail}; the Tiled client's login (`nrw tiled status` says whose)"
    return [Check("tiled", _OK, said)]


def _project_checks() -> list[Check]:
    """Report on the project rooted at the current directory, if there is one."""
    from nr_workbench.project.config import ProjectConfigError, load_config
    from nr_workbench.project.layout import ProjectLayout, ProjectNotFoundError

    try:
        layout = ProjectLayout.discover()
    except ProjectNotFoundError:
        return [Check("project", _MISSING, "no nrw.toml here; run `nrw init`")]

    checks = [Check("project", _OK, str(layout.root))]

    try:
        config = load_config(layout.root)
    except ProjectConfigError as exc:
        checks.append(Check("nrw.toml", "error", str(exc)))
        return checks

    checks.append(Check("instrument", _OK, f"{config.facility} {config.instrument}"))
    if config.beamtime:
        checks.append(Check("beamtime", _OK, config.beamtime))

    samples = layout.list_samples()
    checks.append(
        Check(
            "samples",
            _OK if samples else "warn",
            ", ".join(samples) if samples else "none yet; run `nrw sample new <ID>`",
        )
    )

    if layout.skills_dir.is_dir():
        installed = sorted(
            p.parent.name for p in layout.skills_dir.glob("*/*/SKILL.md")
        )
        checks.append(
            Check(
                "skills",
                _OK if installed else "warn",
                f"{len(installed)} installed: {', '.join(installed)}"
                if installed
                else "none installed",
            )
        )
    else:
        checks.append(Check("skills", "warn", "no skills/ directory; run `nrw init`"))

    checks.append(_spec_schema_check(layout))

    return checks


def _spec_schema_check(layout: Any) -> Check:
    """Report on the project's copy of the `nrw-model/1` JSON Schema.

    Its absence is silent everywhere else: `.vscode/settings.json` points at
    it, the editor says "Unable to load schema" in a corner, and every spec in
    the project simply gets no validation. Projects scaffolded before the
    schema was part of the scaffold are in exactly that state, so the check
    has to distinguish missing from stale -- both are fixed by `nrw init`,
    but only one of them looks like a bug.

    Args:
        layout: The discovered :class:`~nr_workbench.project.layout.ProjectLayout`.

    Returns:
        One diagnostic line.
    """
    from nr_workbench.spec.schema import SCHEMA_FILENAME, schema_bytes

    path = layout.schema_dir / SCHEMA_FILENAME
    relative = path.relative_to(layout.root)

    if not path.is_file():
        return Check(
            "spec schema",
            _MISSING,
            f"no {relative}; model specs get no editor validation. Run `nrw init`.",
        )
    if path.read_bytes() != schema_bytes():
        return Check(
            "spec schema",
            "warn",
            f"{relative} does not match this nr-workbench; run `nrw init`",
        )
    return Check("spec schema", _OK, str(relative))


def run_fix_path(*, yes: bool = False) -> None:
    """Write a literal ``PATH`` into the machine-local harness settings.

    Kept behind a flag, and shown before it is written, because it is an
    override rather than an addition: harness settings do not interpolate
    ``${PATH}`` -- verified, not assumed -- so the value has to be a whole PATH
    copied from this shell. That freezes it. Anything added to your profile
    afterwards is invisible to assistant sessions in this project until this is
    run again.

    Args:
        yes: Skip the confirmation.

    Raises:
        click.ClickException: If there is no project here, or ``nrw`` cannot be
            located.
    """
    from nr_workbench.project import toolpath
    from nr_workbench.project.layout import ProjectLayout, ProjectNotFoundError

    try:
        root = ProjectLayout.discover().root
    except ProjectNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    executable = toolpath.nrw_executable()
    if executable is None:
        raise click.ClickException("Could not locate the `nrw` executable to point at.")

    value = toolpath.path_override(root)
    click.echo(f"  {toolpath.LOCAL_SETTINGS} would set PATH to:")
    for part in value.split(os.pathsep):
        click.echo(f"    {part}")
    click.echo()
    click.echo(
        "  This replaces PATH for assistant sessions in this project rather "
        "than adding to it,\n  so it is a snapshot of the PATH you have right "
        "now. Re-run this after you\n  change your profile."
    )

    if not yes and not click.confirm("  Write it?", default=False):
        click.echo("  Nothing written.")
        return

    written, note = toolpath.apply_path_override(root, value)
    if written is None:
        raise click.ClickException(note)
    click.echo(f"  wrote {written}")


def run_doctor(*, as_json: bool = False) -> None:
    """Print the diagnostics.

    Args:
        as_json: Emit machine-readable JSON instead of a table.

    Raises:
        SystemExit: With code 1 if any check errored.
    """
    checks = collect_checks()

    if as_json:
        click.echo(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        width = max(len(c.name) for c in checks)
        for check in checks:
            marker = STATUS_MARKERS.get(check.status, "?")
            click.echo(f"  {marker} {check.name:<{width}}  {check.detail}")

    if any(c.status == "error" for c in checks):
        sys.exit(1)


def environment_snapshot() -> dict[str, Any]:
    """Capture the versions that belong in a provenance record.

    Separate from :func:`collect_checks` because a fit manifest needs exact
    values, not human-readable status lines. The aure commit is included
    because its version string is identical across every build.

    Returns:
        Mapping suitable for serialising into a fit manifest.
    """
    from nr_workbench import __version__

    snapshot: dict[str, Any] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "nr_workbench": __version__,
        "executable": sys.executable,
    }
    for name in _PACKAGES:
        version = _package_version(name)
        if version is not None:
            snapshot[name] = version
    commit = _aure_commit()
    if commit:
        snapshot["aure_commit"] = commit
    return snapshot
