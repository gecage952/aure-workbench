"""The only module in this package that imports AuRE.

Two constraints shape everything here.

**Every import is function-local.** ``import aure`` costs 1.5-3 seconds and
pulls the whole LLM stack: ``aure/__init__.py`` eagerly imports ``.state`` and
``.workflow``, which chain through eight node modules into ``langchain_core``
and ``periodictable``. ``nrw --help`` must never pay that, and
``tests/test_cli.py`` fails if it does. A module-scope ``import aure`` anywhere
in this package is a bug.

**AuRE declares no stable API.** Its ``__all__`` covers only
``ReflectivityState``, ``create_initial_state`` and ``run_analysis``; every
function used below appears in no ``__all__`` anywhere, and the project has
already been through one architectural rewrite. Routing all of it through one
module means an upstream signature change is a one-file fix rather than a
scavenger hunt, and :func:`contract` lets a test assert the signatures we
depend on before a fit does it for us.

What is deliberately *not* wrapped: ``parse_ort_file`` is a stub that reads
column data and ignores the ORSO header, not an ORSO reader. Use
:func:`load_data` on the reduced ASCII we actually have.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

# Re-exported at the bottom of this module's public surface; see "Materials".

#: The AuRE callables this package depends on, as ``module: (name, ...)``.
#: :func:`contract` checks these resolve; a contract test asserts it, so an
#: upstream rename surfaces in CI rather than mid-fit.
REQUIRED: dict[str, tuple[str, ...]] = {
    "aure.tools.feature_tools": (
        "extract_all_features",
        "extract_critical_edges",
        "extract_kiessig_fringes",
        "estimate_total_thickness",
        "detect_profile_artifacts",
        "check_roughness_thickness_ratios",
    ),
    "aure.tools.data_tools": (
        "validate_reflectivity_data",
        "load_reflectivity_data",
    ),
    # Driving a run. `setup` parses and validates the per-run YAML; the
    # runner executes it. Both are listed so an upstream rename surfaces in
    # CI rather than when a scientist is waiting on a first fit.
    "aure.setup": ("load_setup", "dump_setup"),
    "aure.workflow.runner": ("run_analysis",),
    # The language-model client. `nrw assess`, `nrw model new --from-notes` and
    # `nrw isaac export` reach a model through `complete` and `llm_available`
    # and nothing else, and `llm_available` reports a renamed function as no
    # endpoint at all -- so a rename has to fail here, not there.
    "aure.llm.config": ("llm_available", "get_llm_config", "get_llm_info"),
    "aure.llm.providers": ("get_llm",),
}


#: AuRE's Claude Code CLI provider, as ``LLM_PROVIDER`` names it. AuRE owns the
#: name, so it is spelled here: the one module that speaks AuRE's API.
CLAUDE_CODE = "claude_code"

#: AuRE's own limit on one language-model call, in seconds, when
#: ``LLM_TIMEOUT`` does not say (``aure.llm.config.get_llm_timeout``).
DEFAULT_LLM_TIMEOUT = 120


class AureUnavailableError(Exception):
    """Raised when AuRE is not importable or has moved a function we need."""


def is_available() -> bool:
    """Report whether AuRE can be located without importing it.

    Uses ``find_spec``, which locates the package without executing its
    ``__init__``, so this stays cheap enough to call from ``nrw doctor``.

    Returns:
        Whether the ``aure`` package is installed.
    """
    import importlib.util

    try:
        return importlib.util.find_spec("aure") is not None
    except (ImportError, ValueError):
        return False


def claude_code_supported() -> bool:
    """Whether the installed AuRE can use the Claude Code CLI as its endpoint.

    AuRE is pinned by SHA and tracks ``main``, so an installed copy may predate
    the ``claude_code`` provider. Telling somebody to set a provider their AuRE
    does not have is worse than telling them nothing, so every message that
    offers it asks this first.

    Checked on disk rather than by importing: ``aure.llm.providers`` pulls in
    langchain, and this is called from error paths and from ``nrw doctor``.

    Returns:
        Whether the provider module is present.
    """
    import importlib.util

    try:
        spec = importlib.util.find_spec("aure")
    except (ImportError, ValueError):
        return False
    if spec is None or not spec.submodule_search_locations:
        return False
    for location in spec.submodule_search_locations:
        if (Path(location) / "llm" / "providers" / f"{CLAUDE_CODE}.py").is_file():
            return True
    return False


def claude_cli(binary: str | None = None) -> str | None:
    """The ``claude`` AuRE's provider would run, or ``None`` when there is none.

    AuRE's own rule (``claude_code._binary``), found without importing AuRE:
    ``AURE_CLAUDE_BIN`` when it is set -- a path to an executable, or a name on
    ``PATH`` -- and otherwise ``claude`` on ``PATH``.

    Args:
        binary: What ``AURE_CLAUDE_BIN`` is set to, if anything.

    Returns:
        The path of the executable.
    """
    import os
    import shutil

    if binary:
        if os.path.isfile(binary) and os.access(binary, os.X_OK):
            return binary
        return shutil.which(binary)
    return shutil.which("claude")


def endpoint_hint() -> str:
    """One sentence on how to get an endpoint, tailored to what is installed.

    Shared by every "no endpoint configured" message so the advice cannot
    drift between them.
    """
    base = (
        "Set LLM_PROVIDER and LLM_API_KEY (or LLM_BASE_URL for an "
        "OpenAI-compatible endpoint)"
    )
    if claude_code_supported():
        return (
            base + f", or set LLM_PROVIDER={CLAUDE_CODE} to use the Claude Code "
            "CLI you already have — that one needs no key."
        )
    return base + "."


def resolved_commit() -> str | None:
    """Return the git commit AuRE was installed from, if recorded.

    Through v0.1.x AuRE reported ``version = "0.1.0"`` regardless of which
    commit was installed; v1.0.0 reports a real number. Either way the version
    cannot identify the code, because we track ``main`` and it moves between
    releases. The direct-reference URL pip records at install time can.

    Returns:
        The 40-character commit, or ``None`` if it cannot be determined.
    """
    import json
    from importlib.metadata import Distribution, PackageNotFoundError

    try:
        distribution = Distribution.from_name("aure")
    except PackageNotFoundError:
        return None

    try:
        raw = distribution.read_text("direct_url.json")
    except OSError:
        return None
    if not raw:
        return None

    try:
        payload = json.loads(raw)
    except ValueError:
        return None

    commit = (payload.get("vcs_info") or {}).get("commit_id")
    return str(commit) if commit else None


def contract() -> dict[str, list[str]]:
    """Check that every AuRE function this package calls still exists.

    Returns:
        Mapping of module name to the names that are missing. Empty when the
        contract holds.

    Raises:
        AureUnavailableError: If AuRE cannot be imported at all.
    """
    import importlib

    missing: dict[str, list[str]] = {}
    for module_name, names in REQUIRED.items():
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise AureUnavailableError(
                f"Cannot import {module_name}: {exc}. "
                "Reinstall with `pip install -e .` to restore the AuRE pin."
            ) from exc
        absent = [name for name in names if not hasattr(module, name)]
        if absent:
            missing[module_name] = absent
    return missing


# --------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------


#: Estimates AuRE reports as ``(value, uncertainty, confidence)`` key triples.
#: A missing uncertainty or confidence key is recorded as ``None`` rather than
#: guessed at.
_ESTIMATE_KEYS: tuple[tuple[str, str, str | None, str | None], ...] = (
    (
        "thickness",
        "estimated_total_thickness",
        "thickness_uncertainty",
        "thickness_confidence",
    ),
    ("roughness", "estimated_roughness", None, "roughness_confidence"),
    ("n_layers", "estimated_n_layers", None, "layer_count_confidence"),
)


@dataclass
class Estimate:
    """One quantity AuRE inferred, with how much it trusts it.

    The confidence is not decoration. On a real REF_L curve the total-thickness
    estimate came back as 444 +/- 555 A -- an uncertainty larger than the value,
    which is a way of saying "no useful constraint". Reporting the number alone
    would invite someone to seed a model with it.

    Attributes:
        value: The estimate.
        uncertainty: One-sigma uncertainty, where AuRE reports one.
        confidence: AuRE's own label, e.g. ``low``, ``medium``, ``high``.
    """

    value: float | int | None = None
    uncertainty: float | None = None
    confidence: str | None = None

    @property
    def usable(self) -> bool:
        """Whether the estimate constrains anything.

        False when there is no value, or when the uncertainty is at least as
        large as the value itself.
        """
        if self.value is None:
            return False
        if self.uncertainty is None:
            return True
        return abs(float(self.uncertainty)) < abs(float(self.value))

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON form."""
        return {
            "value": self.value,
            "uncertainty": self.uncertainty,
            "confidence": self.confidence,
            "usable": self.usable,
        }


@dataclass
class Features:
    """What AuRE's feature extraction found in one curve.

    AuRE returns one flat mapping; it is unpacked here so a key rename upstream
    breaks in one place with a test on it, rather than silently yielding empty
    plots and absent estimates.

    Attributes:
        critical_edges: Candidate critical edges, each with ``q_c`` and an
            implied SLD.
        oscillation_periods: Fringe spacings found in Q.
        n_fringes: How many fringes were counted.
        thickness: Total-thickness estimate.
        roughness: Roughness estimate.
        n_layers: Layer-count estimate.
        q_range: The Q range actually analysed.
        n_points: How many points AuRE used, which may be fewer than supplied.
        raw: AuRE's full payload, for anything not surfaced above.
    """

    critical_edges: list[dict[str, Any]] = field(default_factory=list)
    oscillation_periods: list[Any] = field(default_factory=list)
    n_fringes: int = 0
    thickness: Estimate = field(default_factory=Estimate)
    roughness: Estimate = field(default_factory=Estimate)
    n_layers: Estimate = field(default_factory=Estimate)
    q_range: tuple[float | None, float | None] = (None, None)
    n_points: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON form."""
        return {
            "critical_edges": self.critical_edges,
            "oscillation_periods": self.oscillation_periods,
            "n_fringes": self.n_fringes,
            "thickness": self.thickness.as_dict(),
            "roughness": self.roughness.as_dict(),
            "n_layers": self.n_layers.as_dict(),
            "q_range": list(self.q_range),
            "n_points": self.n_points,
        }


def extract_features(
    q: np.ndarray, r: np.ndarray, dr: np.ndarray | None = None
) -> Features:
    """Extract critical edges, Kiessig fringes and a thickness estimate.

    This is AuRE's strongest LLM-free contribution -- plain numpy and scipy,
    well tested upstream. Reimplementing it here would be duplicated work with
    a second set of bugs.

    Args:
        q: Momentum transfer in inverse angstroms.
        r: Reflectivity.
        dr: Uncertainty on ``r``, where available.

    Returns:
        The extracted features.

    Raises:
        AureUnavailableError: If AuRE is not importable.
    """
    from aure.tools import feature_tools

    try:
        payload = feature_tools.extract_all_features(
            np.asarray(q, dtype=float),
            np.asarray(r, dtype=float),
            None if dr is None else np.asarray(dr, dtype=float),
        )
    except Exception as exc:  # upstream raises bare exceptions on odd input
        raise AureUnavailableError(f"AuRE feature extraction failed: {exc}") from exc

    payload = payload if isinstance(payload, dict) else {}
    estimates = {
        name: Estimate(
            value=payload.get(value_key),
            uncertainty=payload.get(error_key) if error_key else None,
            confidence=payload.get(confidence_key) if confidence_key else None,
        )
        for name, value_key, error_key, confidence_key in _ESTIMATE_KEYS
    }
    return Features(
        critical_edges=list(payload.get("critical_edges") or []),
        oscillation_periods=list(payload.get("oscillation_periods") or []),
        n_fringes=int(payload.get("n_fringes") or 0),
        thickness=estimates["thickness"],
        roughness=estimates["roughness"],
        n_layers=estimates["n_layers"],
        q_range=(payload.get("q_min"), payload.get("q_max")),
        n_points=int(payload.get("n_points") or 0),
        raw=payload,
    )


def profile_artifacts(
    z: np.ndarray, rho: np.ndarray, layer_rhos: list[float]
) -> dict[str, Any]:
    """Flag over- and undershoot artifacts in a fitted SLD profile.

    Args:
        z: Depth in angstroms.
        rho: Real SLD.
        layer_rhos: The nominal SLD of each layer in the model.

    Returns:
        AuRE's artifact report.

    Raises:
        AureUnavailableError: If AuRE is not importable.
    """
    from aure.tools import feature_tools

    result = feature_tools.detect_profile_artifacts(
        np.asarray(z, dtype=float),
        np.asarray(rho, dtype=float),
        list(layer_rhos),
    )
    return dict(result) if isinstance(result, dict) else {}


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


#: Knobs that change what model comes out of a run and have **no setup-YAML
#: key and no CLI flag** -- AuRE reads them from the environment only. Its own
#: `docs/launching.md` states the consequence: "a setup file therefore does not
#: fully record the physics policy its run used." We therefore set every one of
#: them explicitly and record what we set, rather than inheriting whatever the
#: shell happened to hold.
#:
#: This mapping is the single source of both the defaults and the record --
#: `commands/aure_cmd.py` builds the run environment from it and a test asserts
#: the two agree, because a knob listed here but not set is a reproducibility
#: hole that nothing else would report. Re-read `docs/launching.md` on every
#: pin bump: a knob added upstream that we do not know about goes unrecorded
#: and nothing fails.
ENVIRONMENT_ONLY_KNOBS: dict[str, str] = {
    # Thin-layer SLD basin search. Off by default because it is slow; it is
    # the deliberate retry when a first pass puts a thin layer in the wrong
    # basin. This single knob decides whether a thin layer is found at all.
    "MODE_ENUMERATION": "0",
    "THIN_LAYER_MODE_K": "1.0",
    "THIN_LAYER_MODE_SEEDS": "3",
    # Upstream's own default is "each layer's declared roughness_max", which
    # no number can express -- so the empty string means "leave it unset", and
    # the recorded file says so rather than omitting the key.
    "ROUGHNESS_MAX_OUTER": "",
    "FINAL_SELECTION_TOL": "0.02",
    "FINAL_TIER_CHI2_FACTOR": "3.0",
    "USE_RUN_TITLE": "0",
}

#: Setup keys that choose the language-model endpoint or carry its credential.
#: AuRE accepts all of these and applies them as environment overrides for the
#: duration of a run, so a setup file can silently re-point the run at another
#: host -- taking the caller's own ``LLM_API_KEY`` with it, because AuRE reads
#: the key from the ambient environment while taking the URL from the file.
#:
#: A setup is a *tracked, shareable* file here, which is exactly why this
#: matters: one arriving from a collaborator, or from an archived beamtime,
#: must not be able to decide where this machine's credentials are sent. The
#: endpoint comes from ``.env``, which is gitignored for the same reason.
CREDENTIAL_SETUP_KEYS: tuple[str, ...] = (
    "llm_api_key",
    "llm_base_url",
    "llm_provider",
    "llm_model",
    "llm_temperature",
    "llm_timeout",
)


class SetupInvalidError(Exception):
    """Raised when a setup document is rejected -- by AuRE, or by us.

    Distinct from :class:`AureUnavailableError` on purpose. "AuRE is not
    importable" is our problem and warrants a bug report; "this setup names a
    data file that is not there" is the scientist's, and takes ten seconds to
    fix. Reporting the second as the first sends a fixable data problem to an
    issue tracker.
    """


def credential_keys_in(document: dict[str, Any]) -> list[str]:
    """Return any endpoint- or credential-choosing keys the document sets.

    Args:
        document: A parsed setup mapping.

    Returns:
        The offending key names, in declaration order.
    """
    return [key for key in CREDENTIAL_SETUP_KEYS if document.get(key) not in (None, "")]


def validate_setup(path: Path) -> dict[str, Any]:
    """Parse a setup YAML through AuRE's own loader.

    Worth doing the moment a setup is written rather than when it is run.
    AuRE rejects unknown top-level keys, resolves every ``data_files`` entry
    against the search path, and validates a state against the instrument its
    filenames imply -- the combined/partial mixing rule and the shared-set-id
    rule both live here. A typo caught now costs a second; the same typo caught
    at run time costs whatever the intake LLM calls cost before it.

    Args:
        path: The setup YAML.

    Returns:
        The parsed setup, with data-file paths resolved to absolute.

    Raises:
        AureUnavailableError: If AuRE is not importable.
        SetupInvalidError: If the setup is rejected -- by AuRE's own loader, or
            because it tries to choose the language-model endpoint.
    """
    try:
        from aure.setup import load_setup
    except ImportError as exc:
        raise AureUnavailableError(f"Cannot import aure.setup: {exc}") from exc

    try:
        document = dict(load_setup(str(path)))
    except Exception as exc:
        raise SetupInvalidError(f"{path}: {exc}") from exc

    # Checked after parsing so synonyms and coercion have been applied, and
    # before the caller can act on the document. See CREDENTIAL_SETUP_KEYS.
    offending = credential_keys_in(document)
    if offending:
        raise SetupInvalidError(
            f"{path} sets {', '.join(offending)}. A setup file may not choose "
            "the language-model endpoint or carry a key: it is committed and "
            "shared, and AuRE would apply those to the run while still using "
            "the API key from your environment. Remove them -- the endpoint "
            "comes from .env, which is gitignored. If this file came from "
            "somebody else, rotate any key you have configured."
        )
    return document


# --------------------------------------------------------------------------
# Data validation
# --------------------------------------------------------------------------


@dataclass
class Validation:
    """Whether a curve is usable, and what is wrong with it.

    Attributes:
        valid: AuRE's overall verdict.
        issues: Human-readable problems found.
    """

    valid: bool
    issues: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON form."""
        return {"valid": self.valid, "issues": self.issues}


def validate(q: np.ndarray, r: np.ndarray, dr: np.ndarray) -> Validation:
    """Check a reflectivity curve for the usual reduction failures.

    Args:
        q: Momentum transfer.
        r: Reflectivity.
        dr: Uncertainty.

    Returns:
        The verdict and any issues.

    Raises:
        AureUnavailableError: If AuRE is not importable.
    """
    from aure.tools import data_tools

    payload = data_tools.validate_reflectivity_data(
        np.asarray(q, dtype=float),
        np.asarray(r, dtype=float),
        np.asarray(dr, dtype=float),
    )
    payload = payload if isinstance(payload, dict) else {}
    issues = payload.get("issues") or payload.get("warnings") or []
    return Validation(
        valid=bool(payload.get("valid", False)),
        issues=[str(item) for item in issues],
    )


def load_data(path: Path) -> dict[str, np.ndarray]:
    """Load a reduced reflectivity file through AuRE's loader.

    Args:
        path: The file to read.

    Returns:
        Mapping with ``Q``, ``R`` and ``dR`` arrays.

    Raises:
        AureUnavailableError: If AuRE is not importable or the file is
            unreadable.
    """
    from aure.tools import data_tools

    try:
        payload = data_tools.load_reflectivity_data(str(path))
    except Exception as exc:
        raise AureUnavailableError(f"AuRE could not read {path}: {exc}") from exc
    return dict(payload) if isinstance(payload, dict) else {}


# --------------------------------------------------------------------------
# Materials
# --------------------------------------------------------------------------
#
# AuRE retired `aure.database.materials` -- the SLDs in its fitted models come
# from its intake LLM, so the table had no consumer there -- and
# `nr_workbench.materials` followed it for the same reason plus one more: the
# frontier models this repository is used with supply a compound density on
# request, and `periodictable` (a refl1d dependency) does the physics. A
# name-to-density table in between was a third copy of something neither end
# needed, and it drifted: six SLDs quoted in these skills had diverged from it,
# two of them disagreeing with the table sitting beside them in this package.
#
# What does not come free is the contrast-match arithmetic, so it lives here
# over two constants of nature -- the same shape as AuRE keeping
# `_SILICON_SLD = 2.07` when it retired the rest.

#: H2O and D2O at 20 C, in 1e-6 per square angstrom. Constants, not a table:
#: `periodictable` has densities for elements only, and these two are the ends
#: of every water contrast series.
H2O_SLD = -0.56
D2O_SLD = 6.37


def contrast_match_ratio(
    target_sld: float, *, low: float = H2O_SLD, high: float = D2O_SLD
) -> float:
    """Deuterated volume fraction whose mixture SLD matches *target_sld*.

    A two-solvent mixture interpolates linearly in SLD, so this inverts that
    line and clamps to the physically reachable range.

    Args:
        target_sld: The SLD to match, in 1e-6 per square angstrom.
        low: SLD of the protiated end. Defaults to H2O.
        high: SLD of the deuterated end. Defaults to D2O.

    Returns:
        The deuterated volume fraction, between 0 and 1. A target outside the
        span the pair can reach clamps to the nearer end rather than raising --
        the caller asked which mixture is closest.

    Raises:
        ValueError: If the two ends have the same SLD, so no mixture varies.
    """
    if high == low:
        raise ValueError(
            "the protiated and deuterated ends have the same SLD; no mixture "
            "can be matched against them."
        )
    return max(0.0, min(1.0, (target_sld - low) / (high - low)))


def mixture_sld(
    fraction_deuterated: float, *, low: float = H2O_SLD, high: float = D2O_SLD
) -> float:
    """SLD of a two-solvent mixture. The inverse of contrast_match_ratio."""
    return low + float(fraction_deuterated) * (high - low)


# --------------------------------------------------------------------------
# Language models
# --------------------------------------------------------------------------
#
# AuRE already resolves a provider from the environment -- openai, gemini,
# alcf, or a local OpenAI-compatible endpoint -- so there is nothing to
# configure here beyond routing through it. Everything stays function-local for
# the same reason as the rest of this module: `nrw --help` must not import
# langchain.


def llm_available() -> bool:
    """Report whether a usable language-model endpoint is configured.

    Reads only environment variables; makes no network call, so this is cheap
    enough to branch on.

    Returns:
        Whether ``LLM_PROVIDER``/``LLM_API_KEY``/``LLM_BASE_URL`` (or the
        provider-specific equivalents) describe a usable endpoint.
    """
    if not is_available():
        return False
    from nr_workbench.env import load_env

    load_env()
    try:
        from aure.llm.config import llm_available as _available

        return bool(_available())
    except Exception:
        return False


def llm_info() -> dict[str, Any]:
    """Describe the configured endpoint, without its credentials.

    Returns:
        Provider, model and base URL where set, plus ``available``. Never
        includes the API key.
    """
    info: dict[str, Any] = {"available": False}
    if not is_available():
        return info
    from nr_workbench.env import load_env

    load_env()
    try:
        from aure.llm.config import get_llm_config
        from aure.llm.config import llm_available as _available

        config = get_llm_config()
    except Exception:
        return info

    # The claude_code provider has no base URL and no key; what identifies it
    # is which binary answered. AuRE reports that, so pass it through.
    try:
        from aure.llm.config import get_llm_info as _aure_info

        binary = _aure_info().get("binary")
    except Exception:
        binary = None
    if binary:
        info["binary"] = binary

    info.update(
        {
            "available": bool(_available()),
            "provider": config.get("provider"),
            "model": config.get("model"),
            "base_url": config.get("base_url"),
            "temperature": config.get("temperature"),
        }
    )
    return info


def scrubbed(text: str) -> str:
    """*text* with every key this process holds taken out of it.

    A provider's error can quote the request it refused, key included, and what
    a job prints is served to anyone who can read the page.

    Args:
        text: An error message, say.

    Returns:
        The text, each secret's value replaced by ``[a key]``.
    """
    import os

    from nr_workbench.env import SECRET_VARS

    for name in SECRET_VARS:
        value = os.environ.get(name, "")
        # A short value would be found in ordinary words; no key is that short.
        if len(value) >= 8:
            text = text.replace(value, "[a key]")
    return text


def complete(system: str, user: str, *, temperature: float = 0.0) -> str:
    """Send one prompt to the configured endpoint and return the reply text.

    Args:
        system: System instruction.
        user: The request.
        temperature: Sampling temperature; 0 by default because the task here
            is extraction, not invention.

    Returns:
        The reply as text.

    Raises:
        AureUnavailableError: If no endpoint is configured or the call fails.
    """
    if not llm_available():
        raise AureUnavailableError(
            "No language-model endpoint is configured. "
            f"{endpoint_hint()} `nrw doctor` reports what it sees."
        )

    try:
        from aure.llm.providers import get_llm

        model = get_llm(temperature=temperature)
        reply = model.invoke([("system", system), ("human", user)])
    except Exception as exc:
        raise AureUnavailableError(
            scrubbed(f"The language-model call failed: {type(exc).__name__}: {exc}")
        ) from exc

    content = getattr(reply, "content", reply)
    if isinstance(content, list):
        # Some providers return a list of content blocks.
        parts = [
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        ]
        return "".join(parts)
    return str(content)
