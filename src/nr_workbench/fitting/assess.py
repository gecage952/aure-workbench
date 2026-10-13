"""Automatic checks on a finished fit, and the prose to go with them.

Two halves, deliberately separated by what they need.

The **computable** half runs everywhere, with no language model and no AuRE
import. It reads the bumps problem JSON, the ``.par`` file and the DREAM
``-err.json`` and reports what those say about whether the answer is trustable:
parameters pressed against their bounds, posteriors that span most of their
prior, a maximum-likelihood point sitting outside its own credible interval,
per-model chi-squared spread, and BIC against the sibling fits of the same
sample. None of that is a matter of opinion, and all of it is the kind of thing
that gets missed at 2am.

The **judgement** half, :mod:`nr_workbench.fitting.judge`, asks a model whether
the numbers are physically sensible given ``sample.md`` and the skills its notes
call for, and hands it what this half found. It runs only when an endpoint is
configured.

The split matters because AuRE's own no-LLM fallback (``_simple_evaluation``)
is three chi-squared bands and nothing else --- it reads no parameters, no
bounds, no posterior. Wrapping it would have made the offline path look
supported while telling a user less than they could work out themselves. So the
offline path here is nr-workbench's own, and it is the stronger of the two on
everything except physics.

A posterior-aware boundary check is worth calling out: AuRE tests the point
estimate against the bound, and a parameter whose *p95 edge* touches its floor
is pinned in a way that test cannot see. Both are reported, separately.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: How close to a bound counts as sitting on it, as a fraction of the range.
#: AuRE uses 0.01 for point estimates; the same figure is used here so the two
#: agree on the same fit.
BOUND_TOLERANCE = 0.01

#: A posterior wider than this fraction of its prior is barely constrained --
#: the fit is reporting the range it was given back to you.
UNCONSTRAINED_FRACTION = 0.75

#: Above this, two parameters are trading against each other rather than each
#: being measured. `thin-layer-degeneracy` calls the (rho, t) ridge the thing
#: to look for, and the real analysis turned on exactly these numbers: an
#: r = -0.93 pair that made a thickness interchangeable with a solvent
#: interface, and an r = +0.97 pair broken only by fixing one side.
CORRELATION_THRESHOLD = 0.8

#: How many correlated pairs to name before summarising the rest. Eight free
#: parameters produced ten pairs on the real fit; listing all of them buries
#: the strongest.
MAX_PAIRS_REPORTED = 5

#: Draw a chain down to about this many rows before correlating. A posterior
#: correlation does not need half a million draws, and the largest chain in
#: the real corpus is 298 MB.
CORRELATION_SAMPLE_ROWS = 40_000

#: A layer counts as reaching its nominal SLD if the profile comes within this
#: much of it, in 1e-6 A^-2.
ATTAINMENT_SLACK = 0.05

#: How many equal-count Q bands to split a segment into when asking where its
#: chi-squared lives. Four is enough to separate "the edges" from "the middle"
#: without any band being too small to mean anything.
Q_BANDS = 4

#: A band this many times the segment's own mean chi-squared is carrying the
#: segment. Below it, the excess is spread and a band breakdown says nothing.
BAND_CONCENTRATION = 2.0

#: Significance at which a coherent residual against the model's own fringes is
#: worth reporting. Coherent structure adds in phase, so a pattern this strong
#: can sit inside a chi-squared that reads as "good" -- which is the entire
#: reason for this check.
FRINGE_SIGNIFICANCE = 4.0

#: Points per window when separating a model curve into its fringe modulation
#: and its smooth envelope. About two fringe periods of a 400 A layer at the
#: sampling REF_L reductions use.
FRINGE_WINDOW = 41

#: Segments shorter than this are not decomposed: there is no room to separate
#: a fringe from an envelope.
MIN_POINTS_FOR_FRINGES = 60

#: Combined uncertainty on the two fringe amplitudes above which their agreement
#: says nothing. An angle-dependent cause big enough to explain the observed
#: damping shifts the amplitude ratio by roughly 0.2 between a 1.2 and a 3.5
#: degree segment, so error bars at that scale cannot exclude one, and the check
#: must say so rather than return a verdict it has not earned.
VISIBILITY_RESOLVING_POWER = 0.15


@dataclass
class Finding:
    """One thing worth knowing about a fit.

    Attributes:
        kind: A short slug, e.g. ``bound``, ``unconstrained``.
        severity: ``info``, ``warn``, or ``blocker``.
        parameter: The parameter involved, if any.
        message: What was found, in a sentence.
        value: The fitted value, for findings about one parameter.
        limit: The bound it is against, where that is the point.
        edge: ``lower`` or ``upper``.
    """

    kind: str
    severity: str
    message: str
    parameter: str | None = None
    value: float | None = None
    limit: float | None = None
    edge: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON form."""
        return {
            "kind": self.kind,
            "severity": self.severity,
            "parameter": self.parameter,
            "message": self.message,
            "value": self.value,
            "limit": self.limit,
            "edge": self.edge,
        }


@dataclass
class Assessment:
    """Everything the checks found.

    Attributes:
        fit_id: The fit assessed.
        chisq: Reduced chi-squared.
        n_free: Free parameters.
        n_points: Data points, summed across models.
        bic: Bayesian information criterion, when computable.
        findings: What the computable checks found.
        judgement: The language model's verdict, when one ran.
        problems: What could not be checked, and why.
    """

    fit_id: str
    chisq: float | None = None
    n_free: int | None = None
    n_points: int | None = None
    bic: float | None = None
    findings: list[Finding] = field(default_factory=list)
    judgement: dict[str, Any] | None = None
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON form."""
        return {
            "schema": "nrw-fit-assessment/1",
            "fit_id": self.fit_id,
            "chisq": self.chisq,
            "n_free": self.n_free,
            "n_points": self.n_points,
            "bic": self.bic,
            "findings": [f.as_dict() for f in self.findings],
            "judgement": self.judgement,
            "problems": self.problems,
        }

    @property
    def worst(self) -> str:
        """The highest severity present, or ``ok``."""
        for level in ("blocker", "warn"):
            if any(f.severity == level for f in self.findings):
                return level
        return "ok"


#: The lists a language model's verdict carries, with each one's heading in
#: NOTES.md. With ``quality_assessment`` these are the only keys a verdict
#: keeps: `nrw assess` prints them and :func:`as_markdown` files them.
VERDICT_LISTS: tuple[tuple[str, str], ...] = (
    ("issues", "Issues"),
    ("physical_concerns", "Physical concerns"),
    ("suggestions", "Suggestions"),
)

#: The words a verdict's quality may be.
QUALITIES = ("excellent", "good", "marginal", "poor")


def bic(chisq: float, n_points: int, n_free: int) -> float:
    """Bayesian information criterion from a *reduced* chi-squared.

    Args:
        chisq: Reduced chi-squared.
        n_points: Number of data points.
        n_free: Number of free parameters.

    Returns:
        ``n ln(chi2) + k ln(n)``, AuRE's form, so the two are comparable.
    """
    return n_points * math.log(chisq) + n_free * math.log(n_points)


def read_bounds(fit_dir: Path) -> dict[str, tuple[float, float]]:
    """Read each free parameter's range from the serialised bumps problem.

    The problem JSON is the right source: it has already resolved everything
    the spec expresses indirectly --- ``pm: 0.3`` around a value, a constraint
    endpoint that becomes two parameters, a per-measurement override --- into
    the names and ranges the fitter actually used.

    Args:
        fit_dir: The result directory.

    Returns:
        Parameter name to ``(low, high)``, for free parameters only.
    """
    bounds: dict[str, tuple[float, float]] = {}
    for path in sorted((fit_dir / "fit").glob("*.json")):
        if path.name.endswith("-err.json") or "-expt" in path.name:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        references = payload.get("references")
        if not isinstance(references, dict | list):
            continue
        entries = references.values() if isinstance(references, dict) else references
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("fixed"):
                continue
            name, limits = entry.get("name"), entry.get("bounds")
            if not isinstance(name, str) or not isinstance(limits, list):
                continue
            if len(limits) == 2 and all(isinstance(v, int | float) for v in limits):
                bounds[name] = (float(limits[0]), float(limits[1]))
        if bounds:
            break
    return bounds


def read_par(fit_dir: Path) -> dict[str, float]:
    """Read the best-fit parameters.

    Args:
        fit_dir: The result directory.

    Returns:
        Parameter name to value. Names contain spaces, so the split is on the
        last field.
    """
    values: dict[str, float] = {}
    for path in sorted((fit_dir / "fit").glob("*.par")):
        for line in path.read_text(encoding="utf-8").splitlines():
            name, _, number = line.strip().rpartition(" ")
            if not name:
                continue
            try:
                values[name.strip()] = float(number)
            except ValueError:
                continue
        break
    return values


def read_uncertainty(fit_dir: Path) -> dict[str, dict[str, Any]]:
    """Read the DREAM per-parameter statistics, if the fit produced them.

    Args:
        fit_dir: The result directory.

    Returns:
        Parameter name to its ``-err.json`` entry. Empty for an optimiser run,
        which is an absence of information rather than certainty.
    """
    for path in sorted((fit_dir / "fit").glob("*-err.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if isinstance(payload, dict):
            return {k: v for k, v in payload.items() if isinstance(v, dict)}
    return {}


def per_model_chisq(fit_dir: Path) -> dict[str, float]:
    """Per-model chi-squared, parsed from the bumps run log.

    Args:
        fit_dir: The result directory.

    Returns:
        Model label to chi-squared. Empty when there is no log.
    """
    found: dict[str, float] = {}
    for path in sorted((fit_dir / "fit").glob("*.out")):
        label = None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            header = re.match(r"^--\s+Model\s+\d+\s+(\S+)", line)
            if header:
                label = header.group(1)
                continue
            match = re.search(r"\[chisq=([0-9.eE+-]+)", line)
            if match and label:
                with contextlib.suppress(ValueError):
                    found[label] = float(match.group(1))
                label = None
        break
    return found


def read_reflectivity(fit_dir: Path) -> dict[str, dict[str, Any]]:
    """Read each model's measured and theoretical curve from its ``-refl.dat``.

    These files are what makes residual structure visible at all: chi-squared is
    a sum, and a sum cannot say that its excess is concentrated at one segment's
    edge, or that it oscillates in step with the model's own fringes.

    Args:
        fit_dir: The result directory.

    Returns:
        Model name to ``{"Q", "dQ", "R", "dR", "theory"}`` arrays, keeping only
        points with a positive uncertainty and a positive measured and modelled
        reflectivity --- the rest cannot enter a log-space comparison. Empty when
        the fit exported no curves.
    """
    import numpy as np

    by_index = _model_names(fit_dir)
    curves: dict[str, dict[str, Any]] = {}
    for path in sorted((fit_dir / "fit").glob("*-refl.dat")):
        match = re.search(r"-(\d+)-refl\.dat$", path.name)
        if not match:
            continue
        name = by_index.get(int(match.group(1))) or path.stem
        try:
            data = np.loadtxt(path, ndmin=2)
        except (OSError, ValueError):
            continue
        if data.shape[1] < 5 or len(data) == 0:
            continue
        q, dq, r, dr, theory = (data[:, i] for i in range(5))
        usable = (
            (dr > 0) & (r > 0) & (theory > 0) & np.isfinite(r) & np.isfinite(theory)
        )
        if usable.sum() < 5:
            continue
        curves[name] = {
            "Q": q[usable],
            "dQ": dq[usable],
            "R": r[usable],
            "dR": dr[usable],
            "theory": theory[usable],
        }
    return curves


def _fringe_modulation(theory: Any) -> Any:
    """Split ``log10(theory)`` into its fringe part, dropping the envelope.

    A running mean over about two fringe periods is the envelope; what is left
    oscillates with the layer thicknesses. Reflected padding keeps the ends
    usable rather than tapering the very points a segment edge lives at.
    """
    import numpy as np

    curve = np.log10(theory)
    width = min(FRINGE_WINDOW, len(curve) // 2 * 2 + 1)
    half = width // 2
    padded = np.r_[curve[1 : half + 1][::-1], curve, curve[-half - 1 : -1][::-1]]
    envelope = np.convolve(padded, np.ones(width) / width, mode="valid")
    return curve - envelope[: len(curve)]


def _band_findings(name: str, curve: dict[str, Any]) -> list[Finding]:
    """Where in Q a segment's chi-squared actually lives.

    An excess concentrated in the *first or last* band is a segment-edge
    artifact --- a stitching scale, a band-edge normalisation --- and no amount
    of structural refinement will fix it. One spread across the middle is the
    model.
    """
    import numpy as np

    q, r, dr, theory = curve["Q"], curve["R"], curve["dR"], curve["theory"]
    residual = (r - theory) / dr
    edges = np.quantile(q, np.linspace(0, 1, Q_BANDS + 1))
    bands: list[tuple[float, float, float]] = []
    for low, high in zip(edges[:-1], edges[1:], strict=False):
        inside = (q >= low) & (q <= high)
        if inside.sum() >= 5:
            bands.append((low, high, float(np.mean(residual[inside] ** 2))))
    if len(bands) < Q_BANDS:
        return []

    overall = float(np.mean(residual**2))
    worst = max(range(len(bands)), key=lambda i: bands[i][2])
    low, high, value = bands[worst]
    if overall <= 0 or value < BAND_CONCENTRATION * overall:
        return []

    at_edge = worst in (0, len(bands) - 1)
    where = "lowest-Q" if worst == 0 else "highest-Q" if at_edge else "interior"
    hint = (
        " That is the segment's own edge, which is where a stitching scale or a "
        "band-edge normalisation error shows up -- check the overlap with the "
        "neighbouring segment before changing the model."
        if at_edge
        else " That is in the segment's interior, so it is more likely the model "
        "than the normalisation."
    )
    return [
        Finding(
            kind="chisq-concentrated",
            severity="warn",
            parameter=name,
            value=value,
            message=(
                f"{name}'s chi-squared is concentrated in its {where} band: "
                f"{value:.3g} over Q {low:.4g}-{high:.4g} against {overall:.3g} for "
                f"the segment.{hint}"
            ),
        )
    ]


def _coherent_findings(name: str, curve: dict[str, Any]) -> list[Finding]:
    """Residual that oscillates in step with the model's own fringes.

    Regresses the residual on the model's fringe modulation and on its
    derivative. The two say different things and have different fixes:

    * **in phase** --- the fringe *depth* is wrong. Resolution, interfacial
      width, or a lateral thickness distribution.
    * **quadrature** --- the fringe *positions* are wrong. A thickness or an
      incident angle.

    Coherent residual adds in phase, so a ten-sigma pattern fits comfortably
    inside a chi-squared that reads as "good". A sum cannot report it.
    """
    import numpy as np

    if len(curve["Q"]) < MIN_POINTS_FOR_FRINGES:
        return []

    residual = (curve["R"] - curve["theory"]) / curve["dR"]
    in_phase = _fringe_modulation(curve["theory"])
    if not np.any(np.abs(in_phase) > 0):
        return []
    quadrature = np.gradient(in_phase, curve["Q"])
    spread = np.std(quadrature)
    if spread > 0:
        quadrature = quadrature / spread * np.std(in_phase)

    design = np.vstack([in_phase, quadrature]).T
    try:
        coefficients, *_ = np.linalg.lstsq(design, residual, rcond=None)
        covariance = np.linalg.inv(design.T @ design) * np.var(
            residual - design @ coefficients
        )
    except np.linalg.LinAlgError:
        return []

    errors = np.sqrt(np.abs(np.diag(covariance)))
    if not np.all(errors > 0):
        return []
    significance = np.abs(coefficients) / errors
    if significance.max() < FRINGE_SIGNIFICANCE:
        return []

    depth, position = significance
    if depth >= position:
        sense = "too deep" if coefficients[0] < 0 else "too shallow"
        diagnosis = (
            f"the model's fringes are {sense} ({depth:.0f} sigma in phase against "
            f"{position:.0f} in quadrature), so the fringe DEPTH is wrong: "
            "resolution, an interfacial width, or a lateral thickness "
            "distribution. Changing a thickness will not fix it"
        )
    else:
        diagnosis = (
            f"the residual is in quadrature with the fringes ({position:.0f} sigma "
            f"against {depth:.0f} in phase), so the fringe POSITIONS are wrong: "
            "a thickness or an incident angle, not a roughness"
        )
    return [
        Finding(
            kind="coherent-residual",
            severity="warn",
            parameter=name,
            value=float(significance.max()),
            message=(
                f"{name}'s residual is not noise -- {diagnosis}. Coherent residual "
                "adds in phase, so this can sit inside a chi-squared that reads as "
                "acceptable."
            ),
        )
    ]


def _visibility_findings(curves: dict[str, dict[str, Any]]) -> list[Finding]:
    """Compare two overlapping segments at the *same* Q.

    This is the test that says what to do about damped fringes, and it is the
    one nothing else runs. Where two segments at different incident angles cover
    the same Q, measure each one's fringe amplitude against the model's:

    * **they differ** --- the cause depends on incident angle, so it is
      instrumental: ``sample_broadening``, or a resolution term the angular-only
      convention discards.
    * **they agree** --- the cause is a function of Q alone: an interfacial
      width, a lateral thickness distribution, or a relative-resolution floor.
      No angular broadening can express it, and forcing one to try corrupts the
      low-angle segment.

    Without this, a fit is damped by whichever parameter happened to be free.
    """
    import numpy as np

    def amplitude_ratio(curve, low, high):
        """Data fringe amplitude over model fringe amplitude, in one Q window."""
        inside = (curve["Q"] > low) & (curve["Q"] < high)
        if inside.sum() < 12:
            return None
        q = curve["Q"][inside]
        sigma = curve["dR"][inside] / (curve["R"][inside] * math.log(10))
        modulation = _fringe_modulation(curve["theory"][inside])
        if np.std(modulation) <= 0 or not np.all(sigma > 0):
            return None
        centred = q - q.mean()
        design = (
            np.vstack([np.ones_like(q), centred, centred**2, modulation]).T
            / sigma[:, None]
        )
        try:
            coefficients, *_ = np.linalg.lstsq(
                design, np.log10(curve["R"][inside]) / sigma, rcond=None
            )
            covariance = np.linalg.inv(design.T @ design)
        except np.linalg.LinAlgError:
            return None
        error = math.sqrt(abs(covariance[3, 3]))
        return (float(coefficients[3]), error) if error > 0 else None

    findings: list[Finding] = []
    by_state: dict[str, list[str]] = {}
    for name in curves:
        by_state.setdefault(name.split("#", 1)[0], []).append(name)

    for state, names in sorted(by_state.items()):
        ordered = sorted(names, key=lambda n: float(np.min(curves[n]["Q"])))
        for lower, upper in zip(ordered[:-1], ordered[1:], strict=False):
            low = float(np.min(curves[upper]["Q"]))
            high = float(np.max(curves[lower]["Q"]))
            if not high > low:
                continue
            first = amplitude_ratio(curves[lower], low, high)
            second = amplitude_ratio(curves[upper], low, high)
            if first is None or second is None:
                continue
            (k_low, e_low), (k_high, e_high) = first, second
            spread = math.hypot(e_low, e_high)
            gap = abs(k_low - k_high) / spread
            damped = min(k_low, k_high) < 0.8 and max(k_low, k_high) < 1.2
            if gap < 3.0 and not damped:
                continue
            if gap >= 3.0:
                verdict = (
                    "they disagree, so the cause depends on incident angle and is "
                    "instrumental -- probe.sample_broadening, per: measurement"
                )
            elif spread > VISIBILITY_RESOLVING_POWER:
                # "They agree, so it is Q-only" is a directive not to reach for
                # sample_broadening, and an angle-dependent cause large enough to
                # explain this damping would move the ratio by about as much as
                # these error bars. Say what was measured, not what it rules out.
                verdict = (
                    f"they are consistent ({gap:.1f} sigma apart), but neither is "
                    "well enough determined for that to exclude an angle-dependent "
                    "cause: this overlap cannot separate resolution from an "
                    "interfacial width. What it does establish is that the fringes "
                    "are damped in both. Get a better-resolved overlap before "
                    "choosing between them"
                )
            else:
                verdict = (
                    f"they agree to {gap:.1f} sigma, which points at a cause that is "
                    "a function of Q alone -- an interfacial width, a lateral "
                    "thickness distribution, or a relative-resolution floor. Angular "
                    "broadening cannot express any of those, and forcing it to try "
                    "will corrupt the low-angle segment"
                )
            findings.append(
                Finding(
                    kind="fringe-damping",
                    severity="warn",
                    parameter=state,
                    message=(
                        f"{lower} and {upper} overlap over Q {low:.4g}-{high:.4g}; "
                        f"their fringe amplitudes against the model are "
                        f"{k_low:.2f}+-{e_low:.2f} and {k_high:.2f}+-{e_high:.2f}. "
                        f"At equal Q {verdict}."
                    ),
                )
            )
    return findings


def _residual_findings(fit_dir: Path) -> list[Finding]:
    """Everything the exported curves say that chi-squared cannot."""
    curves = read_reflectivity(fit_dir)
    if not curves:
        return []
    findings: list[Finding] = []
    for name in sorted(curves):
        findings.extend(_band_findings(name, curves[name]))
        findings.extend(_coherent_findings(name, curves[name]))
    findings.extend(_visibility_findings(curves))
    return findings


def check(fit_dir: Path, manifest: dict[str, Any]) -> Assessment:
    """Run every check that needs no language model.

    Args:
        fit_dir: The result directory.
        manifest: The fit's manifest.

    Returns:
        The assessment, with ``judgement`` unset.
    """
    info = manifest.get("info") or {}
    provenance = manifest.get("provenance") or {}
    result = Assessment(
        fit_id=str(provenance.get("fit_id") or fit_dir.name),
        chisq=info.get("chisq"),
        n_free=info.get("n_free"),
    )

    # `info.n_points` is bumps' `dof`, not N. The per-model counts are the
    # honest total, and BIC is wrong by k log n if you use the wrong one.
    models = info.get("models") or []
    counted = sum(m.get("n_points", 0) for m in models if isinstance(m, dict))
    result.n_points = counted or info.get("n_points")

    if (
        isinstance(result.chisq, int | float)
        and result.chisq > 0
        and isinstance(result.n_points, int)
        and result.n_points > 0
        and isinstance(result.n_free, int)
    ):
        result.bic = bic(float(result.chisq), result.n_points, result.n_free)

    values = read_par(fit_dir)
    bounds = read_bounds(fit_dir)
    stats = read_uncertainty(fit_dir)

    if not values:
        result.problems.append("No .par file, so the fitted values could not be read.")
    if not bounds:
        result.problems.append(
            "No serialised problem, so parameter ranges are unknown and "
            "bound checks were skipped."
        )

    railed = _bound_findings(values, bounds)
    result.findings.extend(railed)
    # The posterior checks earn their place by catching what the point estimate
    # misses. Repeating them for a parameter already reported as railed turns
    # one problem into three lines and buries the other parameters.
    result.findings.extend(
        _posterior_findings(bounds, stats, already={f.parameter for f in railed})
    )

    if not stats:
        result.findings.append(
            Finding(
                kind="no-uncertainty",
                severity="info",
                message=(
                    "No uncertainty estimate: this was an optimiser run. That is "
                    "not the same as small uncertainties -- run dream before "
                    "quoting any interval."
                ),
            )
        )

    result.findings.extend(_correlation_findings(fit_dir))
    result.findings.extend(_attainment_findings(fit_dir))
    result.findings.extend(_inflation_findings(result.chisq, result.n_points, stats))
    result.findings.extend(_residual_findings(fit_dir))

    spread = per_model_chisq(fit_dir)
    if len(spread) > 1:
        worst = max(spread.items(), key=lambda kv: kv[1])
        best = min(spread.items(), key=lambda kv: kv[1])
        if worst[1] > 2 * best[1]:
            result.findings.append(
                Finding(
                    kind="uneven-fit",
                    severity="warn",
                    message=(
                        f"Chi-squared varies {best[1]:.3g} to {worst[1]:.3g} across "
                        f"{len(spread)} models; the overall figure hides that "
                        f"{worst[0]} fits far worse than {best[0]}."
                    ),
                )
            )
    return result


#: Below this, the posterior's own width is the honest one and inflating it would
#: overstate the uncertainty instead. Chosen rather than 1.0 so ordinary
#: point-count scatter does not trigger a finding on every good fit.
INFLATION_THRESHOLD = 1.2

#: How many parameters to name in the inflation finding. The point is to make the
#: correction concrete on the numbers most likely to be quoted, not to reprint the
#: whole table -- that is what the .par and err.json files are for.
INFLATION_EXAMPLES = 4


def _inflation_findings(
    chisq: float | None,
    n_points: int | None,
    stats: dict[str, dict[str, Any]],
) -> list[Finding]:
    """Report that the DREAM intervals are too narrow, and by how much.

    DREAM's posterior width is conditional on the reported ``dR`` being right. A
    reduced chi-squared of 3 says they are understated by about ``sqrt(3)``, or
    the model is missing something, or both -- and in every one of those cases the
    68% interval it prints is too narrow by that factor. Quoting it unscaled is
    how two states come to look three sigma apart when they are not.

    This is deliberately a finding rather than a silent rescaling of ``err.json``:
    the raw posterior is what the sampler produced and the record should keep it.
    The correction belongs in the note, applied by someone who has said why.

    Args:
        chisq: Reduced chi-squared for the fit.
        n_points: Data points, for the significance of the excess.
        stats: The uncertainty summary, keyed by parameter name.

    Returns:
        One finding when inflation is warranted, otherwise nothing.
    """
    if not stats or not isinstance(chisq, int | float) or chisq <= INFLATION_THRESHOLD:
        return []

    scale = math.sqrt(float(chisq))

    # Widest-relative-interval first: those are the ones a reader is most likely
    # to be leaning on, and the ones the correction changes most in absolute terms.
    widths: list[tuple[str, float, float]] = []
    for name, entry in stats.items():
        interval = entry.get("p68") or entry.get("p68_range")
        if not (isinstance(interval, list | tuple) and len(interval) == 2):
            continue
        try:
            low, high = float(interval[0]), float(interval[1])
        except (TypeError, ValueError):
            continue
        half = abs(high - low) / 2.0
        if half > 0:
            widths.append((name, half, half * scale))

    ranked = sorted(widths, key=lambda w: -w[1])
    examples = ", ".join(
        f"{name} +-{half:.3g} -> +-{scaled:.3g}"
        for name, half, scaled in ranked[:INFLATION_EXAMPLES]
    )
    # Never let the truncation read as "these are the affected parameters": the
    # factor applies to every interval in the fit, not the four that are named.
    if examples and len(ranked) > INFLATION_EXAMPLES:
        examples += (
            f" (and {len(ranked) - INFLATION_EXAMPLES} more, all by {scale:.2f})"
        )

    # sqrt(2/N) is the standard error of chi-squared-reduced at N points, so this
    # says how far from 1 the fit actually is rather than just that it is above.
    distance = ""
    if isinstance(n_points, int) and n_points > 1:
        sigma = math.sqrt(2.0 / n_points)
        distance = f" -- {abs(float(chisq) - 1.0) / sigma:.0f} sigma from 1 at {n_points} points"

    return [
        Finding(
            kind="intervals-need-inflation",
            severity="warn",
            value=float(chisq),
            message=(
                f"chi-squared is {float(chisq):.3g}{distance}, so every 68% interval "
                f"below is too narrow by about sqrt({float(chisq):.3g}) = {scale:.2f}. "
                "DREAM's widths assume the reported dR are correct; this fit says "
                "they are understated, the model is incomplete, or both. Scale the "
                "intervals by that factor before quoting one, and before calling any "
                "difference between states significant"
                + (f". For example: {examples}" if examples else ".")
            ),
        )
    ]


def read_chain(fit_dir: Path) -> tuple[Any, list[str]]:
    """Read the posterior draws and the parameter names of their columns.

    Column 0 of a bumps ``-point.mc.gz`` is the log-likelihood; the parameter
    columns follow in the order ``-err.json`` records as ``index``. That
    ordering is the file's own and must not be re-derived --- reading it wrong
    silently correlates the wrong quantities.

    Args:
        fit_dir: The result directory.

    Returns:
        ``(draws, names)``, or ``(None, [])`` when there is no chain.
    """
    import numpy as np

    stats = read_uncertainty(fit_dir)
    if not stats:
        return None, []
    names = sorted(
        (n for n in stats if isinstance(stats[n].get("index"), int)),
        key=lambda n: stats[n]["index"],
    )
    if not names:
        return None, []

    chain = next(iter(sorted((fit_dir / "fit").glob("*-point.mc.gz"))), None)
    if chain is None:
        return None, []
    try:
        draws = np.loadtxt(chain)
    except (OSError, ValueError):
        return None, []
    if draws.ndim != 2 or draws.shape[1] < len(names) + 1:
        return None, []

    columns = draws[:, 1 : 1 + len(names)]
    if len(columns) > CORRELATION_SAMPLE_ROWS:
        # Stride rather than truncate: the head of a chain is not the
        # posterior, and a correlation from it would be the burn-in's.
        stride = len(columns) // CORRELATION_SAMPLE_ROWS + 1
        columns = columns[::stride]
    return columns, names


def _correlation_findings(fit_dir: Path) -> list[Finding]:
    """Parameter pairs the data cannot separate.

    A strong correlation is not a defect --- it is the honest shape of the
    posterior --- but it changes what may be quoted. Two parameters at r = 0.94
    have one measured combination between them, and reporting each with its own
    interval claims two measurements where there was one.
    """
    import numpy as np

    draws, names = read_chain(fit_dir)
    if draws is None or len(names) < 2 or len(draws) < 3:
        return []

    with np.errstate(invalid="ignore", divide="ignore"):
        matrix = np.corrcoef(draws, rowvar=False)
    if not np.ndim(matrix):
        return []

    pairs = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            r = float(matrix[i][j])
            if np.isfinite(r) and abs(r) >= CORRELATION_THRESHOLD:
                pairs.append((abs(r), r, names[i], names[j]))
    if not pairs:
        return []

    pairs.sort(reverse=True)
    shown = pairs[:MAX_PAIRS_REPORTED]
    listing = "; ".join(f"{a} <-> {b} (r={r:+.2f})" for _, r, a, b in shown)
    remainder = (
        f" and {len(pairs) - len(shown)} more pair(s) above {CORRELATION_THRESHOLD}"
        if len(pairs) > len(shown)
        else ""
    )
    return [
        Finding(
            kind="correlated",
            # Information, not a defect. Every DREAM fit in the real corpus --
            # all 19 with a chain -- has pairs above this threshold, because
            # correlation is the honest shape of a reflectometry posterior. A
            # warning that fires on everything discriminates nothing; what it
            # changes is what may be *quoted*, which is a judgement no check
            # can make.
            severity="info",
            parameter=shown[0][2],
            message=(
                f"{len(pairs)} parameter pair(s) are correlated above "
                f"{CORRELATION_THRESHOLD}: {listing}{remainder}. Each pair has "
                "one measured combination between them, so quoting both with "
                "their own intervals claims more measurements than were made."
            ),
        )
    ]


def _attainment_findings(fit_dir: Path) -> list[Finding]:
    """Layers the fit dissolved into their own interfaces.

    A slab whose two roughnesses sum to more than its thickness is not a slab:
    it is two overlapping error functions, its nominal SLD occurs nowhere in
    the structure, and the value reported for it describes a shape that is not
    there. Chi-squared cannot see it --- the analyst wrote the check as a
    manual TODO in his own spec header --- but the exported layer table can.

    Tested on the *fitted* table rather than the profile curve, because a
    profile passes through every intermediate value on its way between layers,
    so asking "is this SLD reached anywhere" is nearly always yes. The
    criterion the real analysis derived is arithmetic:
    ``sigma_top + sigma_bot`` against ``t``.

    Per state, because that is how it presented: OCV1's oxide was swallowed
    (21.3 A of layer between interfaces summing to 33.0) while OCV2's, at
    48.3 A, was fine.
    """
    from nr_workbench.web.trajectory import read_slabs

    layers = _stack_layers(fit_dir)
    by_index = _model_names(fit_dir)

    findings: list[Finding] = []
    seen: set[str] = set()
    for path in sorted((fit_dir / "fit").glob("*-slabs.dat")):
        model = by_index.get(_slab_index(path), "")
        state = model.split("#", 1)[0] if model else ""
        try:
            slabs = read_slabs(path)
        except (OSError, ValueError):
            continue
        if len(slabs) < 3:
            continue

        for row, slab in enumerate(slabs):
            # Row 0 is the ambient and the last is the substrate; neither has
            # a thickness to be swallowed.
            if row in {0, len(slabs) - 1}:
                continue
            name = layers[row - 1] if row - 1 < len(layers) else f"layer {row}"
            key = f"{state} {name}".strip()
            if key in seen:
                continue

            # `read_slabs` renames bumps' `interface` column to `roughness`;
            # reading the original name silently yields zero and the check
            # never fires.
            thickness = float(slab.get("thickness", 0.0))
            top = float(slabs[row - 1].get("roughness", 0.0))
            bottom = float(slab.get("roughness", 0.0))
            if thickness <= 0 or top + bottom <= thickness:
                continue

            seen.add(key)
            findings.append(
                Finding(
                    kind="layer-swallowed",
                    severity="warn",
                    parameter=key,
                    value=thickness,
                    message=(
                        f"{name} fitted to {thickness:.4g} A with interfaces of "
                        f"{top:.4g} and {bottom:.4g} A, which sum to "
                        f"{top + bottom:.4g} -- {(top + bottom) / thickness:.2f}x "
                        "its own thickness. It is two overlapping error "
                        "functions rather than a slab, so its SLD occurs "
                        "nowhere in the structure and the value reported for "
                        "it describes a shape that is not there."
                    ),
                )
            )
    return findings


def _stack_layers(fit_dir: Path) -> list[str]:
    """The named layers between the ambient and the substrate."""
    stack = _frozen_spec(fit_dir).get("stack") or []
    return [
        str(layer.get("name"))
        for index, layer in enumerate(stack)
        if isinstance(layer, dict) and index not in {0, len(stack) - 1}
    ]


def _frozen_spec(fit_dir: Path) -> dict[str, Any]:
    """The spec frozen into the fit directory, or an empty mapping."""
    path = fit_dir / "spec.yaml"
    if not path.is_file():
        return {}
    try:
        import yaml

        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - a spec we cannot read is not a finding
        return {}
    return payload if isinstance(payload, dict) else {}


def _model_names(fit_dir: Path) -> dict[int, str]:
    """Export index to model name, from the fit's manifest."""
    try:
        manifest = json.loads((fit_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    models = (manifest.get("info") or {}).get("models") or []
    return {
        int(m["index"]): str(m.get("name", ""))
        for m in models
        if isinstance(m, dict) and isinstance(m.get("index"), int)
    }


def _slab_index(path: Path) -> int:
    """The export position in ``<stem>-<i>-slabs.dat``, or -1."""
    match = re.search(r"-(\d+)-slabs\.dat$", path.name)
    return int(match.group(1)) if match else -1


def _bound_findings(
    values: dict[str, float],
    bounds: dict[str, tuple[float, float]],
) -> list[Finding]:
    """Parameters whose best-fit value sits on a bound."""
    findings = []
    for name, value in values.items():
        limits = bounds.get(name)
        if not limits:
            continue
        low, high = limits
        span = high - low
        if span <= 0:
            continue
        if abs(value - low) <= BOUND_TOLERANCE * span:
            edge, at = "lower", low
        elif abs(value - high) <= BOUND_TOLERANCE * span:
            edge, at = "upper", high
        else:
            continue
        findings.append(
            Finding(
                kind="bound",
                severity="warn",
                parameter=name,
                value=value,
                limit=at,
                edge=edge,
                message=(
                    f"{name} = {value:.6g} sits on its {edge} bound ({at:g}). "
                    "The data want to go past it, so this value is the bound "
                    "you chose, not a measurement."
                ),
            )
        )
    return findings


def _posterior_findings(
    bounds: dict[str, tuple[float, float]],
    stats: dict[str, dict[str, Any]],
    already: set[str | None] | None = None,
) -> list[Finding]:
    """What the DREAM posterior says that the point estimate does not.

    Args:
        bounds: Parameter ranges.
        stats: The ``-err.json`` entries.
        already: Parameters already reported as sitting on a bound, whose
            posterior findings would only restate that.

    Returns:
        Findings for parameters not already accounted for.
    """
    reported = already or set()
    findings = []
    for name, entry in stats.items():
        if name in reported:
            continue
        limits = bounds.get(name)
        p95 = entry.get("p95")
        p68 = entry.get("p68")
        best = entry.get("best")

        if limits and isinstance(p95, list) and len(p95) == 2:
            low, high = limits
            span = high - low
            if span > 0:
                width = float(p95[1]) - float(p95[0])
                if width >= UNCONSTRAINED_FRACTION * span:
                    findings.append(
                        Finding(
                            kind="unconstrained",
                            severity="warn",
                            parameter=name,
                            message=(
                                f"{name}'s 95% interval spans {width / span:.0%} of "
                                "its allowed range: the data barely constrain it, "
                                "and the fit is handing your prior back."
                            ),
                        )
                    )
                elif (
                    float(p95[0]) - low <= BOUND_TOLERANCE * span
                    or high - float(p95[1]) <= BOUND_TOLERANCE * span
                ):
                    findings.append(
                        Finding(
                            kind="posterior-bound",
                            severity="warn",
                            parameter=name,
                            message=(
                                f"{name}'s 95% interval reaches its bound even "
                                "though the best-fit value does not. The "
                                "posterior is pressed against the range."
                            ),
                        )
                    )

        if (
            isinstance(best, int | float)
            and isinstance(p68, list)
            and len(p68) == 2
            and not (float(p68[0]) <= float(best) <= float(p68[1]))
        ):
            findings.append(
                Finding(
                    kind="skewed",
                    severity="info",
                    parameter=name,
                    message=(
                        f"{name}'s best-fit value {float(best):.6g} lies outside its "
                        f"own 68% interval [{float(p68[0]):.6g}, {float(p68[1]):.6g}] "
                        "-- a skewed or multimodal posterior. Quote the median "
                        "and the interval, not the point."
                    ),
                )
            )
    return findings


def as_markdown(assessment: Assessment) -> str:
    """Render an assessment as the prose that goes into a note.

    Args:
        assessment: What the checks found.

    Returns:
        A markdown section.
    """
    from nr_workbench.notes import GENERATED_CLOSE, GENERATED_OPEN, inert

    lines = [GENERATED_OPEN, "## Assessment", ""]
    facts = []
    if assessment.chisq is not None:
        facts.append(f"chi-squared {assessment.chisq:.4g}")
    if assessment.n_free is not None:
        facts.append(f"{assessment.n_free} free")
    if assessment.n_points is not None:
        facts.append(f"{assessment.n_points} points")
    if assessment.bic is not None:
        facts.append(f"BIC {assessment.bic:.1f}")
    if facts:
        lines += [", ".join(facts), ""]

    if assessment.findings:
        for finding in assessment.findings:
            mark = {"blocker": "**", "warn": "**", "info": ""}[finding.severity]
            lines.append(f"- {mark}{finding.kind}{mark}: {finding.message}")
        lines.append("")
    else:
        lines += [
            "No parameter sits on a bound, and no posterior is unconstrained.",
            "",
        ]

    judgement = assessment.judgement
    if judgement:
        lines += ["### What a language model made of it", ""]
        # Inert again here, whatever the verdict's reader did: a line that
        # closed the generated fence would count as the scientist's own note.
        quality = judgement.get("quality_assessment")
        if quality:
            lines += [f"Quality: **{inert(quality)}**", ""]
        for key, heading in VERDICT_LISTS:
            items = judgement.get(key) or []
            if items:
                lines.append(f"{heading}:")
                lines += [f"- {inert(item)}" for item in items]
                lines.append("")
        lines += [
            "<!-- Written by a language model from the numbers, sample.md and the",
            "     skills its notes call for. It has not seen the data. Treat it as",
            "     a reviewer's first pass, not a result. -->",
            "",
        ]

    if assessment.problems:
        lines.append("Not checked:")
        # A provider's error text can reach a problem, so it is made inert too.
        lines += [f"- {inert(p, 2000)}" for p in assessment.problems]
        lines.append("")
    lines.append(GENERATED_CLOSE)
    lines.append("")
    return "\n".join(lines)
