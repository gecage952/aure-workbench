"""How a fit is set up when the command line does not say.

A fitter setting comes from, in order:

1. the command line -- ``nrw fit run model.py --method de --steps 3000`` -- or
   what the Experiment page's Fit form was given;
2. the project's ``nrw.toml``: ``[fit]`` for the fitter and for what applies to
   every fitter (``seed``, ``parallel``), and one table per fitter for its own
   settings (``[fit.dream]``, ``[fit.de]``, ``[fit.amoeba]``);
3. bumps' own default for that fitter, and :data:`DEFAULT_METHOD` for the
   fitter itself.

A fitter's table applies to that fitter only. DREAM's ``samples`` never reaches
an amoeba fit: bumps would ignore it, but the fit record would still carry it,
and two identical amoeba fits would read as different runs.

Whatever a fit ran with is kept in its record, wherever each value came from,
so a fit stays reproducible after ``nrw.toml`` changes.

A fit started by an unattended session (``NRW_AGENT`` set) is also held to
``[agent.limits.<method>]``: the most it may ask of each of its fitter's own
settings, compared with what it would actually run with. See
:class:`AgentLimits`.

Nothing here imports bumps: this is read by ``nrw fit run --help`` and by the
page, neither of which may pay for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from nr_workbench.fitters import FITTERS, refuse

#: The fitter used when neither the command line nor ``nrw.toml`` names one.
#: DREAM samples the posterior, so what it finds comes with uncertainties.
DEFAULT_METHOD = "dream"

#: The settings each fitter takes of its own, in its ``[fit.<method>]`` table.
METHOD_SETTINGS: dict[str, tuple[str, ...]] = {
    "amoeba": ("steps",),
    "de": ("steps", "pop"),
    "dream": ("samples", "burn", "steps", "pop"),
}

#: The settings every fitter takes, in ``[fit]`` itself.
COMMON_SETTINGS = ("seed", "parallel")

#: bumps' own default for each setting, for what ``nrw.toml`` and the docs show
#: beside it. Not applied here: an unset value is left to bumps. DREAM's
#: ``steps`` of 0 means bumps derives it from ``samples``.
BUMPS_DEFAULTS: dict[str, dict[str, int]] = {
    "amoeba": {"steps": 1000},
    "de": {"steps": 1000, "pop": 10},
    "dream": {"samples": 10000, "burn": 100, "steps": 0, "pop": 10},
}

#: How many CPUs a fit uses when nothing says: all of them.
DEFAULT_PARALLEL = 0

#: The least each setting may be.
FIT_MINIMUMS: dict[str, int] = {
    "steps": 1,
    "samples": 1,
    "burn": 0,
    "pop": 1,
    "seed": 0,
    "parallel": 0,
}

#: Every setting a fit records, besides its method.
ALL_SETTINGS = ("steps", "samples", "burn", "pop", "seed", "parallel")


class FitSettingsError(ValueError):
    """A fit setting, in ``nrw.toml`` or on the command line, is not usable."""


@dataclass(frozen=True)
class FitDefaults:
    """What ``nrw.toml`` says about fitting.

    Attributes:
        method: The fitter a fit uses unless told otherwise.
        common: Settings every fitter takes (``seed``, ``parallel``).
        per_method: Each fitter's own settings, by fitter.
        named: Whether ``nrw.toml`` named the fitter, rather than it being
            :data:`DEFAULT_METHOD`.
    """

    method: str = DEFAULT_METHOD
    common: dict[str, int] = field(default_factory=dict)
    per_method: dict[str, dict[str, int]] = field(default_factory=dict)
    named: bool = False

    def settings_for(self, method: str) -> dict[str, int]:
        """The settings ``nrw.toml`` gives a fit with *method*."""
        return {**self.common, **self.per_method.get(method, {})}


@dataclass(frozen=True)
class ResolvedFit:
    """What one fit runs with, and where each value came from.

    Attributes:
        method: The fitter.
        settings: Every setting a fit records; ``None`` where bumps' own
            default applies.
        origins: For each value set, ``"command line"``, ``"nrw.toml"``, or
            ``"nrw's default"``.
    """

    method: str
    settings: dict[str, int | None]
    origins: dict[str, str]


def read_fit_defaults(document: dict[str, Any]) -> FitDefaults:
    """Read the ``[fit]`` tables of a parsed ``nrw.toml``.

    Args:
        document: The parsed file (``ProjectConfig.raw``).

    Returns:
        What it says; everything unset when it has no ``[fit]``.

    Raises:
        FitSettingsError: A table, key or value is not one this reads. A
            setting that changes nothing is never silently accepted.
    """
    table = document.get("fit")
    if table is None:
        return FitDefaults()
    if not isinstance(table, dict):
        raise FitSettingsError(f"[fit] must be a table, not {table!r}.")
    method = table.get("method", DEFAULT_METHOD)
    if not isinstance(method, str) or method not in FITTERS:
        raise FitSettingsError(f"[fit] method: {refuse(str(method))}")
    common: dict[str, int] = {}
    per_method: dict[str, dict[str, int]] = {}
    for key, value in table.items():
        if key == "method":
            continue
        if key in METHOD_SETTINGS:
            if not isinstance(value, dict):
                raise FitSettingsError(f"[fit.{key}] must be a table, not {value!r}.")
            per_method[key] = _settings(value, key)
        elif key in COMMON_SETTINGS:
            common[key] = _checked(key, value, "[fit]")
        else:
            raise FitSettingsError(
                f"[fit] has no setting {key!r}. It takes method, seed and "
                "parallel; a fitter's own settings go in [fit.dream], [fit.de] "
                "or [fit.amoeba]."
            )
    return FitDefaults(
        method=method, common=common, per_method=per_method, named="method" in table
    )


def resolve(
    defaults: FitDefaults, *, method: str | None, given: dict[str, Any]
) -> ResolvedFit:
    """Settle one fit's settings: the command line, then nrw.toml, then bumps.

    Args:
        defaults: What ``nrw.toml`` says.
        method: The fitter the command line named, or ``None``.
        given: Settings the command line set; ``None`` values are unset.

    Returns:
        The fit's method and settings, and where each came from.

    Raises:
        FitSettingsError: A setting given is not one the fitter takes, or is
            out of range.
    """
    origins: dict[str, str] = {}
    if method is None:
        method = defaults.method
        origins["method"] = "nrw.toml" if defaults.named else "nrw's default"
    elif method not in FITTERS:
        raise FitSettingsError(refuse(method))
    else:
        origins["method"] = "command line"
    takes = (*METHOD_SETTINGS[method], *COMMON_SETTINGS)
    configured = defaults.settings_for(method)
    settings: dict[str, int | None] = {}
    for key in ALL_SETTINGS:
        value = given.get(key)
        if value is not None:
            if key not in takes:
                raise FitSettingsError(_not_taken(method, key))
            settings[key] = _checked(key, value, "the command line")
            origins[key] = "command line"
        elif key in configured:
            settings[key] = configured[key]
            origins[key] = "nrw.toml"
        else:
            settings[key] = None
    if settings["parallel"] is None:
        settings["parallel"] = DEFAULT_PARALLEL
    return ResolvedFit(method=method, settings=settings, origins=origins)


@dataclass(frozen=True)
class AgentLimits:
    """The most a fit started by an unattended session may ask of each fitter.

    Read from ``[agent.limits.<method>]`` in ``nrw.toml``, one table per
    fitter, holding that fitter's own settings as maxima. A setting with no
    limit there is not limited, and a person's own fits never are.

    Attributes:
        per_method: Each fitter's limits, by fitter.
    """

    per_method: dict[str, dict[str, int]] = field(default_factory=dict)

    def exceeded(self, fit: ResolvedFit) -> dict[str, tuple[int, int, str]]:
        """Each setting of *fit* over its limit, as ``(asked, limit, origin)``.

        A setting left unset asks for bumps' own default, so a limit below it
        refuses a fit that names no setting at all; checking only what a
        command line spells out would let exactly that fit through.

        Args:
            fit: The fit, settled by :func:`resolve`.

        Returns:
            The settings over their limit, in the order the limits list them.
        """
        over: dict[str, tuple[int, int, str]] = {}
        for key, limit in self.per_method.get(fit.method, {}).items():
            value = fit.settings.get(key)
            origin = fit.origins.get(key, "bumps' default")
            if value is None:
                value = BUMPS_DEFAULTS[fit.method].get(key)
            if value is not None and value > limit:
                over[key] = (value, limit, origin)
        return over


def read_agent_limits(document: dict[str, Any]) -> AgentLimits:
    """Read the ``[agent.limits]`` tables of a parsed ``nrw.toml``.

    Args:
        document: The parsed file (``ProjectConfig.raw``).

    Returns:
        The limits; none when it has no ``[agent.limits]``.

    Raises:
        FitSettingsError: A table, fitter, key or value is not one this reads:
            a limit that limits nothing must not read as one that holds.
    """
    agent = document.get("agent")
    if agent is None:
        return AgentLimits()
    if not isinstance(agent, dict):
        raise FitSettingsError(f"[agent] must be a table, not {agent!r}.")
    limits = agent.get("limits")
    if limits is None:
        return AgentLimits()
    if not isinstance(limits, dict):
        raise FitSettingsError(f"[agent.limits] must be a table, not {limits!r}.")
    per_method: dict[str, dict[str, int]] = {}
    for method, table in limits.items():
        if method not in METHOD_SETTINGS:
            raise FitSettingsError(
                f"[agent.limits] has no fitter {method!r}: it takes one table "
                f"per fitter, [agent.limits.{'], [agent.limits.'.join(METHOD_SETTINGS)}]."
            )
        if not isinstance(table, dict):
            raise FitSettingsError(
                f"[agent.limits.{method}] must be a table, not {table!r}."
            )
        checked: dict[str, int] = {}
        for key, value in table.items():
            if key not in METHOD_SETTINGS[method]:
                own = ", ".join(METHOD_SETTINGS[method])
                raise FitSettingsError(
                    f"[agent.limits.{method}] limits {method}'s own settings "
                    f"({own}), not {key!r}."
                )
            checked[key] = _checked(key, value, f"[agent.limits.{method}]")
        per_method[method] = checked
    return AgentLimits(per_method=per_method)


def written_settings(
    document: dict[str, Any],
) -> dict[str, dict[str, str | int | float | bool]]:
    """The ``[fit]`` and ``[agent]`` values a parsed ``nrw.toml`` sets, by table.

    What ``nrw init`` writes back when it renders the file again. The template
    holds these tables as commented examples, so a value set in them through
    nrw's editor (``write_as_nrw``) -- an unattended run's limits, as
    nr-watcher writes them -- would otherwise be gone after the next
    ``nrw init``. Tables are named as an edit names them
    (``"agent.limits.dream"``), and only plain values are carried: nothing else
    in these tables is a setting.

    Args:
        document: The parsed file.

    Returns:
        Each table's plain values; a table without any is left out.
    """
    found: dict[str, dict[str, str | int | float | bool]] = {}

    def take(name: str, table: Any) -> None:
        if not isinstance(table, dict):
            return
        plain = {
            key: value
            for key, value in table.items()
            if isinstance(value, str | int | float | bool)
        }
        if plain:
            found[name] = plain
        for key, value in table.items():
            if isinstance(value, dict):
                take(f"{name}.{key}", value)

    for name in ("fit", "agent"):
        take(name, document.get(name))
    return found


def _settings(table: dict[str, Any], method: str) -> dict[str, int]:
    checked: dict[str, int] = {}
    for key, value in table.items():
        if key not in METHOD_SETTINGS[method]:
            raise FitSettingsError(f"[fit.{method}]: {_not_taken(method, key)}")
        checked[key] = _checked(key, value, f"[fit.{method}]")
    return checked


def _checked(key: str, value: Any, where: str) -> int:
    low = FIT_MINIMUMS[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise FitSettingsError(
            f"{where} {key} must be a whole number, at least {low}, not {value!r}."
        )
    return value


def _not_taken(method: str, key: str) -> str:
    own = ", ".join(METHOD_SETTINGS[method])
    return (
        f"{method} takes no {key!r}: its own settings are {own}, and every fitter "
        f"takes {', '.join(COMMON_SETTINGS)}."
    )
