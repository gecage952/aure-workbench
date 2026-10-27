"""Loading configuration from ``.env``, the way AuRE does.

Settings -- currently the language-model endpoint -- come from environment
variables. Those can be supplied four ways, in decreasing precedence:

1. the real shell environment, which is never overridden;
2. a project-local ``.env``, found by walking up from the working directory;
3. a per-user ``~/.nrw``, in the same ``KEY=value`` format;
4. a per-user ``~/.aure``, so a machine already configured for AuRE works here
   without duplicating the key.

The fourth is a convenience, and it is reported by ``nrw doctor`` rather than
being silent: configuration arriving from a file you did not think you were
using is exactly the kind of thing that makes "it works on my machine" hard to
debug.

**Loaded lazily.** ``python-dotenv`` costs about 40 ms to import, and
``nrw --help`` must stay instant, so nothing here runs until something actually
needs a setting.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: Per-user defaults, same format as ``.env``.
USER_ENV_PATH = Path.home() / ".nrw"

#: AuRE's per-user file, read last so an existing AuRE setup carries over.
AURE_ENV_PATH = Path.home() / ".aure"

#: Settings this package reads. Used by `nrw doctor` to report what is set,
#: and to redact the ones that are secret.
KNOWN_VARS = (
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_TEMPERATURE",
    "LLM_TIMEOUT",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "ALCF_ACCESS_TOKEN",
    "ALCF_CLUSTER",
    # Only read by AuRE's claude_code provider, and only when the CLI is not
    # on PATH. Reported because an endpoint that resolves to the wrong binary
    # looks exactly like one that is not configured.
    "AURE_CLAUDE_BIN",
    # The ISAAC Portal `nrw isaac push` publishes to, and the key it does so
    # with -- read by nr-isaac-format, which nrw passes its environment.
    "ISAAC_URL",
    "ISAAC_KEY",
    # The Tiled server `nrw tiled` uses, and the key it sends instead of a
    # login: the person's alone, read from the shell, ~/.nrw or ~/.aure.
    "TILED_URL",
    "TILED_API_KEY",
)

#: Variables whose value must never be printed.
#: The language model's keys: one of these set is "a key is set" for it.
LLM_KEYS = ("LLM_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "ALCF_ACCESS_TOKEN")

SECRET_VARS = frozenset({*LLM_KEYS, "ISAAC_KEY", "TILED_API_KEY"})

#: Set once loading has run, so repeated calls are free.
_loaded = False

#: The variables :func:`load_env` put in ``os.environ`` from a file, so that
#: :func:`where_set` can still tell them from the shell's afterwards.
_set_by_files: set[str] = set()

#: How :func:`where_set` names the environment a process started with.
ENVIRONMENT = "the environment"


@dataclass
class EnvSources:
    """Where configuration was read from.

    Attributes:
        files: Files that were loaded, in the order they were applied.
        missing: Files that were looked for and not found.
    """

    files: list[Path] = field(default_factory=list)
    missing: list[Path] = field(default_factory=list)


def load_env(start: Path | None = None, *, force: bool = False) -> EnvSources:
    """Populate ``os.environ`` from the project and per-user files.

    Nothing already in the real environment is overridden, so an explicit
    ``LLM_MODEL=... nrw ...`` always wins.

    Args:
        start: Directory to search upward from. Defaults to the working
            directory.
        force: Re-read even if loading has already happened this process.

    Returns:
        Which files were used.
    """
    global _loaded
    sources = EnvSources()
    if _loaded and not force:
        return sources

    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv is a declared dep
        _loaded = True
        return sources

    before = set(os.environ)
    for path, _ in _candidates(start):
        if path.is_file():
            # override=False everywhere: the first file to set a variable wins,
            # and the shell wins over all of them.
            load_dotenv(path, override=False)
            sources.files.append(path)
        else:
            sources.missing.append(path)
    _set_by_files.update(set(os.environ) - before)

    _loaded = True
    return sources


def loaded_from_files() -> frozenset[str]:
    """The variables :func:`load_env` put in ``os.environ`` from a file."""
    return frozenset(_set_by_files)


@dataclass(frozen=True)
class Setting:
    """One variable, as an ``nrw`` command started now would read it.

    Attributes:
        value: Its value; ``""`` when a file sets it empty.
        source: :data:`ENVIRONMENT`, or the file it comes from.
    """

    value: str
    source: str | Path


def where_set(
    start: Path | None = None, *, skip_project: bool = False
) -> dict[str, Setting]:
    """Each known variable an ``nrw`` command started in *start* would read.

    :func:`load_env` replayed, never run: the web server asks this, and a value
    loaded into its own environment would pass to every command it starts as
    though the shell had set it -- winning over a project ``.env`` written
    afterwards. The replay is python-dotenv's own: the same files in the same
    order, each value interpolated against the environment as it would stand by
    then -- a ``${VAR}`` in ``~/.aure`` sees what ``.env`` set -- and a file
    that sets a variable already set changes nothing.

    Args:
        start: The directory the command would run in; the working directory
            by default.
        skip_project: Leave out the project's ``.env``: what applies when it
            sets nothing.

    Returns:
        Variable name to its value and source, for the variables set anywhere.
    """
    # What a child starts with: see nr_workbench.web.jobs.child_environment.
    environ = {k: v for k, v in os.environ.items() if k not in _set_by_files}
    sources: dict[str, str | Path] = dict.fromkeys(environ, ENVIRONMENT)
    for path, is_project in _candidates(start):
        # Checked before each file, as load_dotenv does: one may set it.
        if (skip_project and is_project) or _dotenv_disabled(environ):
            continue
        # A regular file only: the server never waits on a FIFO put there.
        if not path.is_file():
            continue
        try:
            values = _resolved(path, environ)
        except (OSError, UnicodeError):
            continue  # a job fails on it; the Settings page says why
        for name, value in values.items():
            if name not in environ and value is not None:
                environ[name] = value
                sources[name] = path
    return {
        name: Setting(environ[name], sources[name])
        for name in KNOWN_VARS
        if name in environ
    }


def _resolved(path: Path, environ: dict[str, str]) -> dict[str, str | None]:
    """A file's values as ``load_dotenv(override=False)`` would have them.

    python-dotenv's ``resolve_variables``, with *environ* standing in for
    ``os.environ``: each value's ``${VAR}`` is looked up in the file's own
    earlier values, and then in the environment, which wins.
    """
    from dotenv.parser import parse_stream
    from dotenv.variables import parse_variables

    values: dict[str, str | None] = {}
    with path.open(encoding="utf-8") as stream:
        for binding in parse_stream(stream):
            if binding.key is None:
                continue
            if binding.value is None:
                values[binding.key] = None
                continue
            env = {**values, **environ}
            values[binding.key] = "".join(
                atom.resolve(env) for atom in parse_variables(binding.value)
            )
    return values


def _dotenv_disabled(environ: dict[str, str]) -> bool:
    """python-dotenv's own switch, which makes ``load_dotenv`` load nothing."""
    value = environ.get("PYTHON_DOTENV_DISABLED", "").casefold()
    return value in {"1", "true", "t", "yes", "y"}


def shown_source(source: str | Path, root: Path | None = None) -> str:
    """A setting's source as a person reads it: ``~/.aure``, not a home path.

    Args:
        source: As :class:`Setting` has it.
        root: The project, for its own ``.env``.

    Returns:
        The environment, a path under the project, or one under ``~``.
    """
    if not isinstance(source, Path):
        return source
    for base, shown in ((root, ""), (Path.home(), "~/")):
        if base is not None:
            try:
                return shown + source.relative_to(base).as_posix()
            except ValueError:
                continue
    return str(source)


def _candidates(start: Path | None) -> list[tuple[Path, bool]]:
    """Files to try, in precedence order, each with whether it is the project's.

    The one list :func:`load_env` loads and :func:`where_set` replays.
    """
    here = (Path(start) if start else Path.cwd()).resolve()

    found: list[tuple[Path, bool]] = []
    # Walk up looking for a project-local .env. Stop at the project root if we
    # reach one -- past that, a .env belongs to something else.
    for directory in [here, *here.parents]:
        candidate = directory / ".env"
        if candidate.is_file():
            found.append((candidate, True))
            break
        if (directory / "nrw.toml").is_file():
            found.append((candidate, True))  # recorded as missing by the caller
            break

    found.append((USER_ENV_PATH, False))
    found.append((AURE_ENV_PATH, False))
    return found


def describe() -> dict[str, str]:
    """Report the settings that are set, with secrets redacted.

    Returns:
        Variable name to value, with anything secret shown only as its length
        and last four characters -- enough to tell two keys apart, not enough
        to use one.
    """
    report: dict[str, str] = {}
    for name in KNOWN_VARS:
        value = os.environ.get(name)
        if not value:
            continue
        report[name] = redact(value) if name in SECRET_VARS else value
    return report


def redact(value: str) -> str:
    """Render a secret as a shape rather than a value.

    Public because the harness-provider variables reported by ``nrw check-llm``
    are a different set from :data:`KNOWN_VARS` but need the same treatment,
    and two redaction functions is one too many.

    Args:
        value: The secret.

    Returns:
        Its length and last four characters -- enough to tell two keys apart,
        not enough to use one.
    """
    if len(value) <= 4:
        return "set (short)"
    return f"set ({len(value)} chars, ...{value[-4:]})"
