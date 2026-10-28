"""Reading the metadata REF_L writes into a reduced file's header.

Every reduced steady-state file carries a full record of how it was made, in a
single JSON object on a ``# Meta:`` line:

    # Meta:{"theta": 0.007853609528660925, "norm_run": 218274,
            "dq_over_q": 0.0271, "scaling_factors": {"a": 4.0, ...}, ...}

That is exact, machine-readable, and already on disk. Guessing any of it is
therefore not a heuristic problem -- it is a parsing problem that was being
skipped.

**Angles are stored in radians.** ``theta: 0.00785`` is 0.45 degrees. Reading
it as degrees is the kind of error that produces a fit rather than a failure.

**Why this is not an LLM call.** ``theta`` sets the resolution of every point
through ``dT = dq/q * tan(theta)``, so a plausible-looking wrong value corrupts
a fit silently. A parser is exact, deterministic, offline, and testable; a
model call is none of those, and would be asked to read a number that is
already sitting in a JSON field. Where a model genuinely helps is an *unfamiliar*
header, and the right shape for that is: parse the format we know, fail loudly
when we meet one we do not, and document the format so an agent can read a new
one itself -- see the ``refl-reduced-headers`` skill.

Time-resolved slices carry no header at all. Nothing invents one; the caller is
told the angle is unknown and must declare it.

**Two dialects are live.** ``_partial.txt`` writes the ``# Meta:`` block above;
``_autoreduction.dat`` writes ``# Key = value`` lines and, crucially, a fourth
column in **sigma** rather than FWHM. Which one a file is in is decided here,
and the filename patterns that decide it are also duplicated in
``project/scan.py`` and in AuRE -- see ``docs/plan-reduced-format-registry.md``.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .reduced import find_segments, parse_segment_name

#: The line carrying the JSON metadata block.
META_PREFIX = "# Meta:"

#: The fixed-width summary table REF_L also writes, used as a fallback when the
#: JSON block is absent. ``TwoTheta`` is in degrees and is twice theta.
_TABLE_RE = re.compile(
    r"^#\s+(?P<run>\d+)\s+(?P<norm>\d+)\s+(?P<twotheta>[\d.eE+-]+)\s+"
)

#: The column-title line, which states the width convention of the 4th column:
#:
#:     # Q [1/Angstrom]   R   dR   dQ [FWHM]
#:
#: This is the only place on disk that says whether ``dQ`` is a full width or a
#: standard deviation, and the two differ by 2.355 -- a factor that broadens or
#: sharpens every fringe and that a fit absorbs into roughness rather than
#: reporting. ``_partial.txt`` writes FWHM here; ``_autoreduction.dat`` writes
#: sigma on a different line. Both are in use, so the convention is read per
#: file and never assumed.
_DQ_COLUMN_RE = re.compile(r"^#.*\bdQ\b\s*\[\s*(?P<label>[^\]]*?)\s*\]", re.IGNORECASE)

#: The same statement in the ``new_reduction`` dialect: parentheses rather than
#: brackets, on a ``columns =`` line.
#:
#:     # columns = Q, R, dR, dQ (sigma)
_DQ_COLUMNS_RE = re.compile(
    r"^#\s*columns\s*=.*\bdQ\b\s*\(\s*(?P<label>[^)]*?)\s*\)", re.IGNORECASE
)

#: Column labels meaning a full width at half maximum.
_FWHM_LABELS = frozenset({"fwhm", "full width", "full width at half maximum"})

#: Column labels meaning one standard deviation.
_SIGMA_LABELS = frozenset(
    {"sigma", "σ", "1-sigma", "1 sigma", "one sigma", "std", "stdev", "std dev"}
)

#: ``# Key = value`` and ``# Key: value``, the ``new_reduction`` header's shape.
_AUTORED_KV_RE = re.compile(
    r"^#\s*(?P<key>[A-Za-z][A-Za-z0-9 _]*?)\s*[:=]\s*(?P<value>\S.*)$"
)

#: The trailing segment index in a run title, e.g. ``Sample1_air-234277-2.``
#:
#: This is how a file finds its own entry in the header's parallel arrays, and
#: the reason it is not simply ``array[seg - 1]``.
#:
#: **The reduction appends to these arrays on reprocess instead of replacing
#: them.** Reduce a run twice and ``Run Title.title`` and ``Angles.*`` carry
#: two complete passes -- ``[1, 2, 3, 1, 2, 3]`` -- while the arrays under
#: ``Config`` come from the reduction template and stay at the segment count.
#: A partial reprocess leaves a ragged mixture: run 234277 reads
#: ``[1, 2, 2, 3]``, where positional indexing gives segment 3 an angle of
#: 1.251 deg instead of 3.5 -- a factor of ~2.8 in Q, which fits cleanly to a
#: wrong thickness.
#:
#: An earlier version of this comment explained the over-length arrays as "a
#: segment measured in two pieces gets two entries". That is wrong, and the
#: way it is wrong matters: splitting one segment cannot produce
#: ``[1, 2, 3, 1, 2, 3]``, which is what four of the five runs in IPTS-37740
#: actually carry. Under the correct explanation the *last* matching slot is
#: the current one and the first is superseded -- see :func:`_segment_slots`.
_TITLE_SEGMENT_RE = re.compile(r"-(?P<seg>\d+)\.?\s*$")

#: Lines whose presence identifies the dialect.
_AUTORED_MARKERS = ("# Angles:", "# Config:")

#: How many leading lines to scan. The header is a dozen lines; reading the
#: whole of a 250-row file to find it would still be cheap, but a malformed
#: file should not be read in full either.
_MAX_HEADER_LINES = 60

#: How many leading bytes to read, whatever the lines. A real header is a few
#: kilobytes; a file with no newline at all would otherwise be read whole into
#: memory as "line 1" -- and the experiment page reads files in a folder the
#: whole team can write to.
MAX_HEADER_BYTES = 64 * 1024


class HeaderError(Exception):
    """Raised when a header is present but cannot be understood."""


@dataclass
class ReducedHeader:
    """What a reduced REF_L file records about its own making.

    Attributes:
        path: The file this came from.
        theta: Incident angle in **degrees**, converted from the radians the
            file stores. ``None`` when the file carries no header.
        run: The run that produced this segment.
        norm_run: The direct-beam run it was divided by. ``None`` in the
            ``new_reduction`` dialect, which records a filename instead -- see
            ``norm_source``.
        norm_source: The direct beam as named by the ``new_reduction`` header,
            e.g. ``"A2_Si.txt"``. A filename rather than a run number, so it
            does not say *when* that direct beam was measured -- which is the
            question a disagreeing pair of segments sends you here to answer.
        sequence_number: Which angle segment this is, 1-based.
        sequence_id: The run number of the measurement as a whole.
        n_segments: How many angle segments the measurement was *planned*
            with, where the header says. Only the ``new_reduction`` dialect
            does: its per-segment arrays (``DB``, ``scale_factor``,
            ``ThetaShift``) come from the reduction template, so they are
            sized to the plan rather than to what has been reduced so far.
            ``None`` when the header does not say, or when those arrays
            disagree about the count -- a guess here would call a run
            complete while its last segment is still being measured.
        dq_over_q: Fractional resolution as reduced.
        dq_convention: ``"fwhm"`` or ``"sigma"`` -- what the 4th column's width
            actually is, read from the column-title line. ``None`` when the file
            does not say, which is normal for a time-resolved slice and means
            the caller must declare it rather than assume.
        dq_column_label: The label exactly as written, for the error message
            when it is one we do not recognise.
        scaling_factor: The constant the reduction multiplied this segment by.
        q_range: ``(q_min, q_max)`` as recorded.
        wavelength_range: ``(wl_min, wl_max)`` in angstroms.
        run_title: The operator's title for the run.
        start_time: ISO-8601 start, usable for a geometry lookup.
        experiment: The IPTS identifier.
        theta_offset: Any offset already applied by the reduction.
        source: ``meta`` when read from the ``# Meta:`` JSON block,
            ``autoreduction`` from the ``new_reduction`` dialect, ``table``
            from the fixed-width summary, ``none`` when there was no header.
        raw: The full JSON object, for anything not surfaced above.
        warnings: What the header says that is self-contradictory. Empty on
            every healthy file, so a caller may surface these unconditionally.
            These are not read errors -- a malformed header raises -- but
            statements the file makes that disagree with each other, which are
            survivable and must not be survived quietly.
    """

    path: Path
    theta: float | None = None
    run: int | None = None
    norm_run: int | None = None
    norm_source: str | None = None
    sequence_number: int | None = None
    sequence_id: int | None = None
    n_segments: int | None = None
    dq_over_q: float | None = None
    dq_convention: str | None = None
    dq_column_label: str | None = None
    scaling_factor: float | None = None
    q_range: tuple[float | None, float | None] = (None, None)
    wavelength_range: tuple[float | None, float | None] = (None, None)
    run_title: str | None = None
    start_time: str | None = None
    experiment: str | None = None
    theta_offset: float | None = None
    source: str = "none"
    raw: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_theta(self) -> bool:
        """Whether an angle was actually recorded."""
        return self.theta is not None

    @property
    def dq_is_fwhm(self) -> bool | None:
        """Whether the 4th column is FWHM, or ``None`` if the file does not say.

        ``None`` is deliberately not ``True``. A caller that needs the answer
        must decide, and record what it decided, rather than inherit a default
        from a reduction that is expected to change.
        """
        if self.dq_convention is None:
            return None
        return self.dq_convention == "fwhm"

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON form."""
        return {
            "path": str(self.path),
            "theta_deg": self.theta,
            "run": self.run,
            "norm_run": self.norm_run,
            "norm_source": self.norm_source,
            "sequence_number": self.sequence_number,
            "sequence_id": self.sequence_id,
            "n_segments": self.n_segments,
            "dq_over_q": self.dq_over_q,
            "dq_convention": self.dq_convention,
            "dq_column_label": self.dq_column_label,
            "scaling_factor": self.scaling_factor,
            "q_range": list(self.q_range),
            "wavelength_range": list(self.wavelength_range),
            "run_title": self.run_title,
            "start_time": self.start_time,
            "experiment": self.experiment,
            "theta_offset": self.theta_offset,
            "source": self.source,
            "warnings": list(self.warnings),
        }


def read_header(path: Path, *, dq: bool = True) -> ReducedHeader:
    """Read the metadata a reduced file carries about itself.

    Only a regular file is read. Opening a pipe waits for a writer, so a pipe
    named like a reduced file -- in a folder anyone on the team can write --
    would hang whatever asked; it is refused instead.

    Args:
        path: The reduced ASCII file.
        dq: Read the ``dQ`` width convention too. ``False`` for a caller that
            wants only the angle, which an unknown ``dQ`` label must not hide.

    Returns:
        What the header said. ``source`` is ``none`` and ``theta`` is ``None``
        when the file has no header -- which is the normal case for a
        time-resolved slice, not an error.

    Raises:
        HeaderError: If a ``# Meta:`` line is present but is not valid JSON, if
            the ``dQ`` column carries a width convention we do not know, or if
            it is not a regular file.
        OSError: If the file cannot be read.
    """
    path = Path(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise HeaderError(f"{path}: not a regular file, so it was not read")
        return read_header_bytes(handle.read(MAX_HEADER_BYTES), path, dq=dq)


@dataclass(frozen=True)
class RecordedAngle:
    """What a reduced file records of its own angle, for a model spec.

    Attributes:
        theta: Incident angle in degrees, or ``None`` when it records none.
        planned: How many angle segments its measurement was planned with,
            when the header says.
        why: Why there is no angle, when there is none.
        missing: The file is not there at all.
    """

    theta: float | None
    planned: int | None = None
    why: str = ""
    missing: bool = False


def recorded_angle(path: Path) -> RecordedAngle:
    """The incident angle *path* records about itself -- the one reader of it.

    What resolving a model spec, scaffolding one, and finding a series' angle
    all use, so that they cannot disagree about whether a file records one. An
    unknown ``dQ`` label does not hide the angle: the two are separate facts,
    and the label is reported where the convention is read.

    Args:
        path: The reduced file.

    Returns:
        The angle, or ``None`` and a reason a person can act on.
    """
    try:
        header = read_header(path, dq=False)
    except FileNotFoundError:
        return RecordedAngle(None, why="the file is not there", missing=True)
    except (HeaderError, OSError) as exc:
        return RecordedAngle(None, why=f"its header cannot be read ({exc})")
    if header.theta is None:
        return RecordedAngle(
            None, header.n_segments, why="its header records no incident angle"
        )
    return RecordedAngle(float(header.theta), header.n_segments)


def read_header_bytes(data: bytes, path: Path, *, dq: bool = True) -> ReducedHeader:
    """:func:`read_header`, for a caller that has already read the file.

    The experiment's data source opens each file itself, checking it is still
    the file it listed, and hands the bytes here.

    Args:
        data: The start of the file. Only the first :data:`MAX_HEADER_BYTES`
            are read, whatever is passed.
        path: The file, for its name (which carries the segment in the
            ``new_reduction`` dialect) and for messages.
        dq: Read the ``dQ`` width convention too; see :func:`read_header`.

    Returns:
        What the header said.

    Raises:
        HeaderError: As for :func:`read_header`.
    """
    path = Path(path)
    header = ReducedHeader(path=path)

    lines: list[str] = []
    text = data[:MAX_HEADER_BYTES].decode("utf-8", errors="replace")
    for index, line in enumerate(text.split("\n")):
        if index >= _MAX_HEADER_LINES or not line.startswith("#"):
            break
        lines.append(line.rstrip("\r"))

    # Read first and unconditionally: the column titles sit *below* the JSON
    # block, so anything that returns on finding `# Meta:` would never see them.
    if dq:
        _apply_dq_convention(header, lines, path)

    for line in lines:
        if line.startswith(META_PREFIX):
            _apply_meta(header, line[len(META_PREFIX) :], path)
            return header

    if any(line.startswith(marker) for line in lines for marker in _AUTORED_MARKERS):
        _apply_autoreduction(header, lines, path)
        return header

    # No JSON block. The fixed-width table still carries TwoTheta.
    for line in lines:
        match = _TABLE_RE.match(line)
        if match:
            header.run = int(match.group("run"))
            header.norm_run = int(match.group("norm"))
            header.theta = float(match.group("twotheta")) / 2.0
            header.source = "table"
            return header

    return header


def _apply_dq_convention(header: ReducedHeader, lines: list[str], path: Path) -> None:
    """Record whether the 4th column is FWHM or sigma, from the column titles.

    Leaves both fields ``None`` when no column-title line names ``dQ`` -- a
    time-resolved slice has no header at all, and that is not an error.

    Raises:
        HeaderError: If ``dQ`` is labelled with something we do not recognise.
            Guessing here would silently scale every resolution by 2.355, so an
            unknown label has to stop the caller rather than default to FWHM.
    """
    for line in lines:
        match = _DQ_COLUMN_RE.match(line) or _DQ_COLUMNS_RE.match(line)
        if match is None:
            continue
        label = match.group("label")
        header.dq_column_label = label
        normalised = label.strip().lower()
        if normalised in _FWHM_LABELS:
            header.dq_convention = "fwhm"
        elif normalised in _SIGMA_LABELS:
            header.dq_convention = "sigma"
        else:
            raise HeaderError(
                f"{path}: the dQ column is labelled {label!r}, which is neither "
                "a FWHM nor a sigma convention this package knows. FWHM and "
                "sigma differ by 2.355 and the difference is absorbed into "
                "roughness rather than raised, so it cannot be assumed. Add the "
                "label to instrument/header.py once you have confirmed what the "
                "reduction meant."
            )
        return


def _autoreduction_fields(lines: list[str]) -> dict[str, Any]:
    """Parse the ``# Key = value`` lines of the ``new_reduction`` header.

    **The writer mixes two notations, line by line**, so both are tried:

    ==============================  =========================================
    ``# Config: {"a": null, ...}``  JSON -- ``null``, ``false`` are not Python
    ``# NR_runs = [None, None, 3]`` Python -- ``None`` is not JSON
    ``# DB = ['A1_Si.txt']``        Python -- single quotes are not JSON
    ``# Angles: {"THS": [-0.45]}``  either, being only numbers
    ``# Lambda Range = 2.65Å to …`` neither; kept as its raw string
    ==============================  =========================================

    Parsing with only one of them looks like it works: ``Angles`` succeeds
    under both, so the angle -- the field most likely to be checked -- comes
    out right while ``Config`` silently degrades to a string and every field
    under it goes ``None``.

    :func:`ast.literal_eval` executes nothing, so neither path evaluates code
    from a data file.

    Args:
        lines: The file's leading comment lines.

    Returns:
        Mapping of key to parsed value. Later lines win, which matters only for
        a malformed file that repeats a key.
    """
    import ast

    fields: dict[str, Any] = {}
    for line in lines:
        match = _AUTORED_KV_RE.match(line)
        if match is None:
            continue
        raw = match.group("value").strip()
        value: Any = raw
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError):
            try:
                value = ast.literal_eval(raw)
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                value = raw
        fields[match.group("key").strip()] = value
    return fields


def _segment_slots(titles: list[Any], segment: int) -> list[int]:
    """Every slot in the header's parallel arrays belonging to *segment*.

    The arrays are per *acquisition*, not per segment, and the reduction
    appends to them on reprocess -- so a run reduced twice has two complete
    passes and ``array[segment - 1]`` silently mis-assigns everything after
    the first repeat. The run titles end in ``-<segment>.``, so they are what
    ties a slot to a segment.

    All matches are returned, most recent last, because which one to believe
    is the caller's decision and a disagreement between them is worth
    reporting rather than resolving quietly.

    Args:
        titles: The ``Run Title.title`` array.
        segment: 1-based segment number, from the filename.

    Returns:
        Slot indices in file order; empty if no title names this segment.
    """
    slots = []
    for index, title in enumerate(titles):
        match = _TITLE_SEGMENT_RE.search(str(title))
        if match and int(match.group("seg")) == segment:
            slots.append(index)
    return slots


def _apply_autoreduction(header: ReducedHeader, lines: list[str], path: Path) -> None:
    """Populate a header from the ``new_reduction`` dialect.

    Unlike ``# Meta:``, this header describes the whole run and is identical in
    every one of its files, so the file's own segment comes from its name and
    every per-segment value is looked up rather than read.

    Nothing here raises on a missing field. The dialect is new and the writer
    may drop keys; a ``None`` that callers must handle is already the contract,
    whereas refusing to parse a file over an absent ``Lambda Range`` would be
    worse than the gap it reports.

    Args:
        header: The header to populate, modified in place.
        lines: The file's leading comment lines.
        path: The file, used for its segment number.
    """
    fields = _autoreduction_fields(lines)
    header.raw = fields
    header.source = "autoreduction"

    # The segment comes from the *filename*: this header describes the whole
    # run and is byte-identical in every one of that run's files, so it cannot
    # say which segment it is attached to. `instrument/reduced.py` owns the
    # name pattern.
    parsed = parse_segment_name(path.name)
    segment = parsed.segment if parsed else None
    if parsed:
        header.sequence_number = parsed.segment
        header.sequence_id = parsed.run
        header.run = parsed.subrun

    config = fields.get("Config")
    config = config if isinstance(config, dict) else {}
    header.experiment = _as_str(config.get("experiment_id"))

    titles = fields.get("Run Title")
    titles = titles.get("title") if isinstance(titles, dict) else None
    titles = titles if isinstance(titles, list) else []

    slots = _segment_slots(titles, segment) if segment is not None else []
    # The LAST matching slot, not the first. Because the arrays are appended
    # to on reprocess, the first slot naming this segment is the *oldest*
    # reduction pass -- stale by construction, and silently so if a reprocess
    # corrected `ThetaShift` or switched `useCalcTheta`.
    slot = slots[-1] if slots else None
    if slot is not None:
        header.run_title = _as_str(titles[slot])

    angles = fields.get("Angles")
    angles = angles if isinstance(angles, dict) else {}
    # THS is the sample angle and the one that carries the setting; ThCen
    # repeats it. Both are signed -- negative on this project's back-reflection
    # run -- and the probe wants the magnitude.
    series = angles.get("THS") or angles.get("ThCen")
    if isinstance(series, list) and slots:
        # Every pass that recorded this segment should agree about its angle.
        # They do on IPTS-37740, so this is quiet in practice -- but a
        # disagreement means a reprocess changed the answer, and taking one
        # value without saying so is how the wrong one would be used.
        recorded = [
            _as_float(series[i])
            for i in slots
            if i < len(series) and _as_float(series[i]) is not None
        ]
        if len(set(recorded)) > 1:
            header.warnings.append(
                f"segment {segment} is recorded at "
                f"{', '.join(f'{v:g}' for v in recorded)} deg by different "
                f"reduction passes; using the most recent ({recorded[-1]:g}). "
                f"The reduction appends to these arrays rather than replacing "
                f"them, so the earlier value is superseded, not an alternative"
            )
    if isinstance(series, list) and slot is not None and slot < len(series):
        theta = _as_float(series[slot])
        header.theta = abs(theta) if theta is not None else None

    header.n_segments = _planned_segments(fields, config)

    if segment is not None:
        header.norm_source = _as_str(_by_segment(fields.get("DB"), segment))
        scaling = fields.get("Scaling factors")
        if isinstance(scaling, dict):
            header.scaling_factor = _as_float(
                _by_segment(scaling.get("scale_factor"), segment)
            )
        header.theta_offset = _as_float(_by_segment(config.get("ThetaShift"), segment))

    header.wavelength_range = (
        _as_float(config.get("LambdaMinUse")),
        _as_float(config.get("LambdaMaxUse")),
    )

    # `q_range` and `dq_over_q` are deliberately left None. `Config` carries
    # `qmin`/`qmax`/`dqbin`, but those are the limits and binning the reduction
    # was *asked* for, not the range it produced or the resolution it achieved
    # -- on run 234277, qmax is 0.5 against data reaching 0.278, and dqbin is
    # 0.015 against a median dQ/Q of 0.011. Reporting a request as a
    # measurement is the error this module exists to prevent.


def _planned_segments(fields: dict[str, Any], config: dict[str, Any]) -> int | None:
    """How many segments the reduction template planned, if the header agrees.

    Uses the same arrays :func:`_by_segment` indexes -- the ones with exactly
    one entry per segment -- and not the angle or title arrays, which are per
    *acquisition* and grow when a run is reprocessed.

    Args:
        fields: The parsed ``# Key = value`` lines.
        config: The parsed ``Config`` object, or an empty mapping.

    Returns:
        The count when every per-segment array present has the same non-zero
        length, else ``None``.
    """
    scaling = fields.get("Scaling factors")
    candidates = [
        fields.get("DB"),
        scaling.get("scale_factor") if isinstance(scaling, dict) else None,
        config.get("ThetaShift"),
    ]
    lengths = {len(value) for value in candidates if isinstance(value, list)}
    if len(lengths) != 1:
        return None
    count = lengths.pop()
    return count if count > 0 else None


def _by_segment(series: Any, segment: int) -> Any:
    """Take a per-segment entry from an array indexed 1-based by segment.

    Only for arrays that genuinely have one entry per segment -- ``DB``,
    ``scale_factor``, ``ThetaShift``. The angle and title arrays are per
    acquisition and must go through :func:`_segment_slots` instead.

    Args:
        series: The candidate array.
        segment: 1-based segment number.

    Returns:
        The entry, or None if ``series`` is not a long enough list.
    """
    if not isinstance(series, list) or segment < 1 or segment > len(series):
        return None
    return series[segment - 1]


def _apply_meta(header: ReducedHeader, payload: str, path: Path) -> None:
    """Populate a header from the JSON metadata block."""
    try:
        meta = json.loads(payload)
    except (ValueError, RecursionError) as exc:
        # RecursionError: deeply nested JSON is valid enough to recurse on and
        # is not a ValueError, so it would escape every caller's handler.
        raise HeaderError(
            f"{path}: the '# Meta:' line is not valid JSON ({exc}). "
            "The file may be truncated."
        ) from exc
    if not isinstance(meta, dict):
        raise HeaderError(f"{path}: '# Meta:' is not a JSON object.")

    header.raw = meta
    header.source = "meta"

    # Radians on disk, degrees everywhere in this package.
    theta = meta.get("theta")
    if isinstance(theta, int | float) and math.isfinite(theta):
        header.theta = math.degrees(float(theta))

    header.run = _as_int(meta.get("run_number"))
    header.norm_run = _as_int(meta.get("norm_run"))
    header.sequence_number = _as_int(meta.get("sequence_number"))
    header.sequence_id = _as_int(meta.get("sequence_id"))
    header.dq_over_q = _as_float(meta.get("dq_over_q"))
    header.theta_offset = _as_float(meta.get("theta_offset"))
    header.q_range = (_as_float(meta.get("q_min")), _as_float(meta.get("q_max")))
    header.wavelength_range = (
        _as_float(meta.get("wl_min")),
        _as_float(meta.get("wl_max")),
    )
    header.run_title = _as_str(meta.get("run_title"))
    header.start_time = _as_str(meta.get("start_time"))
    header.experiment = _as_str(meta.get("experiment"))

    factors = meta.get("scaling_factors")
    if isinstance(factors, dict):
        header.scaling_factor = _as_float(factors.get("a"))


def theta_for_run(steady_dir: Path, run: int) -> tuple[float | None, str | None]:
    """Find the incident angle of a run from its summed steady-state file.

    Time-resolved slices carry no header, and the angle appears in neither the
    reduction JSON nor the tNR template -- but the same run is *also* reduced
    as a summed dataset into the steady folder, and that file has the full
    ``# Meta:`` block. So the angle of a tNR series is on disk after all, one
    directory across.

    Args:
        steady_dir: The sample's ``data/steady`` directory.
        run: The run number to look for.

    Returns:
        ``(theta_degrees, source_filename)``, or ``(None, None)`` if no file
        for that run records an angle.
    """
    steady_dir = Path(steady_dir)
    if not steady_dir.is_dir():
        return (None, None)

    # Prefer a per-angle partial: it names its own sequence number, so a
    # measurement with several angles is unambiguous. The combined file is a
    # fine fallback and carries the same theta for a single-angle run.
    candidates = find_segments(steady_dir, run) + sorted(
        steady_dir.glob(f"REFL_{run}_combined*.txt")
    )
    for candidate in candidates:
        recorded = recorded_angle(candidate)
        if recorded.theta is not None:
            return (recorded.theta, candidate.name)
    return (None, None)


def _as_int(value: Any) -> int | None:
    """Coerce to int, tolerating the strings REF_L sometimes writes."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    """Coerce to a finite float, or ``None``."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _as_str(value: Any) -> str | None:
    """Coerce to a non-empty string, or ``None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
