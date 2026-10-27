"""Turn a spec into a parameter table: which object every slot points at.

This is where the aliasing pitfall is eliminated. ``corefine-model/SKILL.md``
documents the trap: once angle segments 1..N alias segment 0's Parameter
object, later *reassigning* segment 0's parameter does not propagate, and the
cross-dataset constraint silently fails. It has produced wrong results.

The fix is structural rather than procedural. Resolution answers, for every
``(group, measurement, path)`` slot, *which key it reads from* -- and the
generated code then assigns from that table in three passes:

1. create every free parameter, once, from the table;
2. build every constrained expression from those free parameters;
3. assign every slot from the table.

No assignment ever reads from another Experiment, so there is no "segment 0" to
go stale. Segment 0 is not the source of truth; the table is.

This module is pure: no refl1d, no bumps, no filesystem beyond the discovery
helpers. That makes the hard part testable on its own.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nr_workbench.arrival import segment_gap
from nr_workbench.instrument.header import RecordedAngle, recorded_angle, theta_for_run
from nr_workbench.instrument.reduced import (
    find_segments,
    parse_segment_name,
    segment_globs,
)
from nr_workbench.problems import one_line
from nr_workbench.spec.constraints import FORMS, ConstraintError, FormContext
from nr_workbench.spec.models import (
    Constraint,
    ModelSpec,
    ParameterPath,
    ParameterSpec,
    Series,
    SpecError,
    State,
)

#: How far an angle a spec states may stray from the one its file records, in
#: degrees: the precision of a value typed to two decimals. The file's value is
#: the one fitted, so a stated angle is only a check -- and one further off than
#: rounding explains is a guess, or a copy from another run.
STATED_ANGLE_TOLERANCE = 0.005

#: Filename pattern for a time-binned tNR slice: r<run>_t<seconds>.txt.
_SLICE_RE = re.compile(r"^r(?P<run>\d+)_t(?P<t>\d+)\.txt$")


@dataclass(frozen=True)
class Measurement:
    """One data file that becomes one refl1d Experiment.

    Attributes:
        group: The state or series it belongs to.
        index: Zero-based position within that group.
        file: Path to the data, relative to the project root.
        theta: Incident angle in degrees.
        time: Elapsed seconds, for a series member; ``None`` for steady state.
        label: Interval label, when one is known.
    """

    group: str
    index: int
    file: str
    theta: float
    time: float | None = None
    label: str | None = None

    @property
    def key(self) -> str:
        """A stable identifier, e.g. ``ocv1#0`` or ``tnr#7``."""
        return f"{self.group}#{self.index}"


@dataclass
class FreeParameter:
    """A parameter created exactly once and read from everywhere.

    Attributes:
        key: Table key, e.g. ``Cu.thickness@ocv1``.
        display: Name shown by bumps in fit output.
        value: Starting value.
        bounds: ``(min, max)``, or ``None`` when fixed.
        fixed: Pinned rather than fitted.
        comment: Why it exists, for the generated script.
    """

    key: str
    display: str
    value: float
    bounds: tuple[float, float] | None = None
    fixed: bool = False
    comment: str = ""


@dataclass
class Expression:
    """A constrained value built from free parameters.

    Attributes:
        key: Table key, e.g. ``CuOx.thickness@tnr#3``.
        source: Python source referring only to ``P[...]``.
        comment: The form and endpoints, for the generated script.
    """

    key: str
    source: str
    comment: str = ""


@dataclass
class Slot:
    """Where a value goes, and what it reads from.

    Attributes:
        measurement: The measurement being configured.
        path: The layer or probe attribute being set.
        ref: Key into the free-parameter or expression table.
        kind: ``P`` for a free parameter, ``E`` for an expression.
    """

    measurement: Measurement
    path: ParameterPath
    ref: str
    kind: str


@dataclass
class ParameterTable:
    """The complete resolution of a spec.

    Attributes:
        spec: The spec this came from.
        measurements: Measurements per group, in order.
        free: Free parameters, in creation order.
        expressions: Constrained expressions, in creation order.
        slots: Every assignment to make.
        warnings: Non-fatal problems worth surfacing.
    """

    spec: ModelSpec
    measurements: dict[str, list[Measurement]] = field(default_factory=dict)
    free: list[FreeParameter] = field(default_factory=list)
    expressions: list[Expression] = field(default_factory=list)
    slots: list[Slot] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def n_free(self) -> int:
        """How many parameters the fit will vary."""
        return sum(1 for p in self.free if not p.fixed)

    @property
    def n_experiments(self) -> int:
        """How many refl1d Experiments the problem will hold."""
        return sum(len(v) for v in self.measurements.values())

    @property
    def all_files(self) -> list[str]:
        """Every data file the model reads, in order."""
        return [m.file for group in self.measurements.values() for m in group]

    def free_by_key(self, key: str) -> FreeParameter | None:
        """Look up a free parameter.

        Args:
            key: The table key.

        Returns:
            The parameter, or ``None``.
        """
        return next((p for p in self.free if p.key == key), None)

    def slots_for(self, measurement: Measurement) -> list[Slot]:
        """Every assignment for one measurement.

        Args:
            measurement: The measurement.

        Returns:
            Its slots, in table order.
        """
        return [s for s in self.slots if s.measurement.key == measurement.key]


# --------------------------------------------------------------------------
# Discovery: spec + filesystem -> measurements
# --------------------------------------------------------------------------


def discover_measurements(spec: ModelSpec, root: Path) -> dict[str, list[Measurement]]:
    """Resolve every state and series to concrete data files.

    Args:
        spec: The model spec.
        root: Project root, which all paths are relative to.

    Returns:
        Measurements per group name, in declaration order.

    Raises:
        SpecError: If a referenced file or directory is missing.
    """
    found: dict[str, list[Measurement]] = {}
    for state in spec.states:
        found[state.name] = _discover_state(state, root)
    for series in spec.series:
        found[series.name] = _discover_series(series, root)
    return found


def _discover_state(state: State, root: Path) -> list[Measurement]:
    """Resolve one steady-state measurement's files, each at its own angle."""
    owner = f"state {state.name!r}"
    if state.segments != "auto":
        return [
            Measurement(
                state.name,
                i,
                seg.file,
                file_angle(
                    owner,
                    f"listed segment {i + 1}",
                    root / seg.file,
                    seg.theta,
                    how="as that segment's `theta`",
                ),
            )
            for i, seg in enumerate(state.segments)
        ]

    directory = Path(state.data_dir) if state.data_dir else Path("data/steady")
    absolute = root / directory
    if not absolute.is_dir():
        raise SpecError(f"{owner}: no such data directory: {directory}")

    if state.kind == "combined":
        name = f"REFL_{state.run}_combined_data_auto.txt"
        if not (absolute / name).is_file():
            raise SpecError(f"{owner}: missing {directory / name}")
        if state.thetas is not None and len(state.thetas) != 1:
            raise SpecError(
                f"{owner}: a combined curve is one file, so `thetas` holds one "
                f"angle, not {len(state.thetas)}."
            )
        theta = file_angle(
            owner,
            "the combined curve",
            absolute / name,
            state.thetas[0] if state.thetas else None,
            how="under `thetas`",
        )
        return [Measurement(state.name, 0, (directory / name).as_posix(), theta)]

    # Every segment of the run in the directory, found by its name: the
    # subrun usually but not always runs consecutively from the run number, so
    # nothing here assumes it, and nothing assumes how many segments there are.
    # Both reduction dialects are matched -- a beamtime mid-migration has both
    # -- through `instrument/reduced.py`, the only place that knows the names.
    found = segments_on_disk(absolute, state.run)
    if not found:
        raise SpecError(
            f"{owner}: no file matching "
            f"{' or '.join(segment_globs(state.run))} in {directory}"
        )
    for number, paths in sorted(found.items()):
        if len(paths) > 1:
            raise SpecError(
                f"{owner}: {len(paths)} files match segment {number}: "
                f"{[one_line(p.name) for p in paths]}. List them explicitly under "
                "`segments`."
            )
    gap = segment_gap(state.run, found)
    if gap:
        raise SpecError(
            f"{owner}: {gap} If fitting only some is deliberate, list them under "
            "`segments`."
        )
    numbers = sorted(found)
    if state.thetas is not None and len(state.thetas) != len(numbers):
        raise SpecError(
            f"{owner}: `thetas` gives {len(state.thetas)} angle(s), but run "
            f"{state.run} has {len(numbers)} segment(s) in {directory}. Leave "
            "`thetas` out to read each angle from its file, or give one per "
            "segment; to fit only some, list them under `segments`."
        )
    files = [found[number][0] for number in numbers]
    records = [recorded_angle(path) for path in files]
    # Segments 1 and 2 of a run whose headers plan three are contiguous, and
    # still not the measurement: the third may not be reduced yet.
    planned = {record.planned for record in records if record.planned is not None}
    if len(planned) == 1 and (expected := planned.pop()) > len(numbers):
        raise SpecError(
            f"{owner}: run {state.run}'s headers plan {expected} segments, but "
            f"only {len(numbers)} are in {directory}. The rest may not be "
            "reduced yet; to fit only these, list them under `segments`."
        )

    measurements: list[Measurement] = []
    for index, (number, path, record) in enumerate(
        zip(numbers, files, records, strict=True)
    ):
        given = state.thetas[index] if state.thetas is not None else None
        theta = file_angle(
            owner,
            f"segment {number}",
            path,
            given,
            how="under `thetas`, one per segment -- `null` for any whose file "
            "records its own",
            recorded=record,
        )
        measurements.append(
            Measurement(state.name, index, path.relative_to(root).as_posix(), theta)
        )
    return measurements


def segments_on_disk(directory: Path, run: int | None) -> dict[int, list[Path]]:
    """Each segment number of *run* in *directory*, and the files that claim it.

    What ``segments: auto`` reads, so that `nrw model new` can tell whether a
    registered run is all of it -- and write the segments out when it is not.
    """
    found: dict[int, list[Path]] = {}
    for path in find_segments(directory, run):
        parsed = parse_segment_name(path.name)
        if parsed is not None and parsed.run == run:
            found.setdefault(parsed.segment, []).append(path)
    return found


def file_angle(
    owner: str,
    what: str,
    path: Path,
    given: float | None,
    *,
    how: str,
    recorded: RecordedAngle | None = None,
) -> float:
    """The incident angle of one file: its own record, checked against the spec.

    There is no fallback. An angle assumed from habit fits without complaint:
    it sets the wavelength axis through ``wl = 4*pi*sin(theta)/q``, and the fit
    absorbs a wrong one into roughness. So an angle comes from the file's
    header, or from the spec where the header has none -- and one the spec
    gives for a file that records its own must agree with it.

    Args:
        owner: The state or series, for the message.
        what: Which of its files, for the message.
        path: The file.
        given: The spec's angle for it, in degrees, or ``None``.
        how: Where in the spec to give an angle, for the message.
        recorded: The file's record, when the caller has read it already.

    Returns:
        The angle in degrees: the file's own where it has one.

    Raises:
        SpecError: The file is missing, neither gives an angle, or the two
            disagree by more than :data:`STATED_ANGLE_TOLERANCE`.
    """
    record = recorded if recorded is not None else recorded_angle(path)
    if record.missing:
        raise SpecError(f"{owner}: {what}: no such file: {one_line(str(path))}")
    if record.theta is None:
        if given is None:
            raise SpecError(
                f"{owner}: {what} ({one_line(path.name)}): {one_line(record.why)}. "
                f"Give it {how}, in degrees."
            )
        return float(given)
    if given is not None and abs(given - record.theta) > STATED_ANGLE_TOLERANCE:
        raise SpecError(
            f"{owner}: {what} is given as {given:g} deg, but {one_line(path.name)} "
            f"records {record.theta:.4f} deg. Leave it out to use the file's "
            "angle, or correct it -- a known misalignment belongs in a "
            "`theta_offset` parameter, not here."
        )
    return record.theta


def summed_dataset_dir(reduced_dir: str | Path) -> Path | None:
    """Where a series' run is also reduced as one summed dataset, or ``None``.

    The ``steady`` folder beside the nearest ``tnr`` folder holding the slices:
    ``samples/S/data/tnr/218389`` and a flat ``samples/S/data/tnr`` both give
    ``samples/S/data/steady``. That file has the header the slices lack, so it
    is where a series' incident angle is read from. Outside any ``tnr`` folder
    there is no telling where it would be.
    """
    path = Path(reduced_dir)
    for folder in (path, *path.parents):
        if folder.name == "tnr":
            return folder.parent / "steady"
    return None


def _series_angle(series: Series, root: Path, run: int | None) -> float:
    """A series' incident angle: from its run's summed dataset, checked against the spec.

    Args:
        series: The series.
        root: Project root.
        run: The run its slices belong to -- the sidecar's, for a series read
            from one -- which is the run whose summed dataset is looked up.
    """
    owner = f"series {series.name!r}"
    steady = summed_dataset_dir(series.reduced_dir)
    recorded, source = (
        theta_for_run(root / steady, run)
        if steady is not None and run is not None
        else (None, None)
    )
    if recorded is None:
        if series.theta is None:
            if run is None:
                where = "it names no `run` to look one up by"
            elif steady is None:
                where = (
                    f"{series.reduced_dir} is in no `tnr` folder, so there is no "
                    "summed dataset beside it"
                )
            else:
                where = f"no file of run {run} in {steady} records one"
            raise SpecError(
                f"{owner}: its slices carry no header, and {where}. Give its "
                "`theta`, in degrees."
            )
        return float(series.theta)
    if (
        series.theta is not None
        and abs(series.theta - recorded) > STATED_ANGLE_TOLERANCE
    ):
        raise SpecError(
            f"{owner}: `theta` is given as {series.theta:g} deg, but "
            f"{one_line(source or '')} -- the same run, summed -- records "
            f"{recorded:.4f} deg. Leave it out to use the file's angle, or "
            "correct it."
        )
    return float(recorded)


def _discover_series(series: Series, root: Path) -> list[Measurement]:
    """Resolve one time-resolved series' slices."""
    directory = Path(series.reduced_dir)
    absolute = root / directory
    if not absolute.is_dir():
        raise SpecError(f"series {series.name!r}: no such directory: {directory}")

    if series.time_from == "reduction_json":
        return _discover_series_from_json(series, root, absolute, directory)
    theta = _series_angle(series, root, series.run)

    select = series.select
    available: dict[int, Path] = {}
    for path in sorted(absolute.iterdir()):
        match = _SLICE_RE.match(path.name)
        if match and (series.run is None or int(match.group("run")) == series.run):
            available[int(match.group("t"))] = path

    if not available:
        raise SpecError(
            f"series {series.name!r}: no r<run>_t<seconds>.txt slices in {directory}. "
            "If the run has interval labels instead, set `time_from: reduction_json`."
        )

    if select.t_step is not None:
        wanted = list(
            range(int(select.t_start or 0), int(select.t_stop) + 1, int(select.t_step))
        )
    else:
        wanted = sorted(available)

    measurements: list[Measurement] = []
    missing: list[int] = []
    for i, t in enumerate(wanted):
        path = available.get(t)
        if path is None:
            missing.append(t)
            continue
        measurements.append(
            Measurement(
                series.name,
                len(measurements),
                path.relative_to(root).as_posix(),
                theta,
                time=float(t),
                label=path.stem,
            )
        )
        del i

    if missing:
        raise SpecError(
            f"series {series.name!r}: {len(missing)} requested slice(s) not on disk "
            f"(first missing t={missing[0]} s). Available: "
            f"{min(available)}..{max(available)} s."
        )
    if len(measurements) < 2:
        raise SpecError(
            f"series {series.name!r}: resolved {len(measurements)} slice(s); need >= 2"
        )
    return measurements


def _discover_series_from_json(
    series: Series, root: Path, absolute: Path, directory: Path
) -> list[Measurement]:
    """Resolve a series from its reduction JSON sidecar.

    The sidecar is the only source that carries interval *types*, so it is what
    makes ``select.interval_types`` possible. It is also authoritative for
    order -- filenames sort lexically.
    """
    import json
    from datetime import datetime

    candidates = sorted(absolute.glob("*_reduction.json"))
    if series.run is not None:
        # Only the series' own run: a sidecar from another would pick that
        # run's slices, and its angle, under this series' name.
        candidates = [c for c in candidates if str(series.run) in c.name]
        if not candidates:
            raise SpecError(
                f"series {series.name!r}: no *_reduction.json for run "
                f"{series.run} in {directory}"
            )
    if not candidates:
        raise SpecError(f"series {series.name!r}: no *_reduction.json in {directory}")

    payload = json.loads(candidates[0].read_text(encoding="utf-8"))
    run = payload.get("run_number", series.run)
    try:
        run = int(run) if run is not None else None
    except (TypeError, ValueError):
        raise SpecError(
            f"series {series.name!r}: {one_line(candidates[0].name)} gives its run "
            f"number as {run!r}"
        ) from None
    if series.run is not None and run != series.run:
        raise SpecError(
            f"series {series.name!r}: {one_line(candidates[0].name)} is the "
            f"reduction of run {run}, not {series.run}."
        )
    theta = _series_angle(series, root, run)
    intervals = payload.get("intervals") or []
    if not intervals:
        raise SpecError(
            f"series {series.name!r}: {candidates[0].name} lists no intervals"
        )

    t0 = datetime.fromisoformat(intervals[0]["start"])
    select = series.select

    measurements: list[Measurement] = []
    for entry in intervals:
        label = entry["label"]
        itype = entry.get("interval_type", "unknown")
        if select.interval_types and itype not in select.interval_types:
            continue
        if select.labels and not any(fnmatch.fnmatch(label, p) for p in select.labels):
            continue

        elapsed = (datetime.fromisoformat(entry["start"]) - t0).total_seconds()
        if select.t_start is not None and elapsed < select.t_start:
            continue
        if select.t_stop is not None and elapsed > select.t_stop:
            continue

        path = absolute / f"r{run}_{label}.txt"
        if not path.is_file():
            continue
        measurements.append(
            Measurement(
                series.name,
                len(measurements),
                path.relative_to(root).as_posix(),
                theta,
                time=elapsed,
                label=label,
            )
        )

    if len(measurements) < 2:
        raise SpecError(
            f"series {series.name!r}: selection resolved {len(measurements)} slice(s); need >= 2"
        )
    return measurements


# --------------------------------------------------------------------------
# Resolution: spec + measurements -> parameter table
# --------------------------------------------------------------------------


def trim_for(spec: ModelSpec, measurement: Measurement) -> dict[str, float]:
    """The data bounds in force for one measurement.

    Entries merge field by field in declaration order, so a cut that applies
    everywhere can be narrowed for one segment without restating the rest:

    .. code-block:: yaml

        trim:
          - {lambda_min: 3.5, reason: "direct beam unreliable below this"}
          - {in: [run226649#0], q_max: 0.022, reason: "scale drifts across the band"}

    Args:
        spec: The model spec.
        measurement: The measurement to resolve for.

    Returns:
        Only the bounds that apply, as ``create_probe`` keyword arguments.

    Raises:
        SpecError: If an entry's ``in`` names an unknown state or series.
    """
    effective: dict[str, float] = {}
    for entry in spec.trim:
        if entry.in_ is not None:
            for target in entry.in_:
                group = target.partition("#")[0]
                if group not in spec.group_names:
                    raise SpecError(
                        f"trim: `in` names unknown state/series {group!r}. "
                        f"Known: {', '.join(spec.group_names)}."
                    )
            if not any(_trim_matches(t, measurement) for t in entry.in_):
                continue
        effective.update(entry.as_kwargs())
    return effective


def _trim_matches(target: str, measurement: Measurement) -> bool:
    """Whether a ``state`` or ``state#index`` target names this measurement."""
    group, _, suffix = target.partition("#")
    if group != measurement.group:
        return False
    if not suffix:
        return True
    try:
        return int(suffix) == measurement.index
    except ValueError:
        return False


def build_table(
    spec: ModelSpec, measurements: dict[str, list[Measurement]]
) -> ParameterTable:
    """Resolve a spec into the table the generator emits from.

    Args:
        spec: The model spec.
        measurements: Discovered measurements per group.

    Returns:
        The parameter table.

    Raises:
        SpecError: If the spec references something that does not exist, or a
            path is both freely fitted and constrained.
    """
    table = ParameterTable(spec=spec, measurements=measurements)

    _add_free_parameters(spec, table)
    _add_constraints(spec, table)
    _check_no_double_assignment(table)
    return table


#: Specificity ranks. A declaration naming an individual measurement beats one
#: naming a whole group, in the same way a more specific CSS rule wins. That is
#: what lets a spec say "segments share an intensity, except the high-angle one
#: which has its own" -- a real pattern in the hand-written scripts, where the
#: 3.5-degree segment carries a different normalisation.
_RANK_GROUP = 1
_RANK_MEASUREMENT = 2

#: How far two incident angles may differ and still be the same setting, in
#: degrees. The same nominal angle is recorded slightly differently per run --
#: 0.37 against 0.3698, 1.2002 against 1.2001 -- because each theta is read from
#: its own file rather than tidied to the nominal value. Grouping has to see
#: through that, while staying far below the gap between two settings anyone
#: measures at -- tenths of a degree -- so anything from 0.005 to 0.1 would do,
#: and 0.02 is the middle of it.
ANGLE_TOLERANCE = 0.02


def angle_groups(measurements: dict[str, list[Measurement]]) -> dict[str, str]:
    """Map each measurement key to a label for its incident angle.

    Clustered rather than rounded: rounding splits 1.199 from 1.201 at a boundary
    that has nothing to do with the instrument, and those are the same setting.

    Args:
        measurements: Every measurement in the problem, by group.

    Returns:
        ``measurement.key`` to a label such as ``0.37deg``.
    """
    every = [m for group in measurements.values() for m in group]
    clusters: list[list[Measurement]] = []
    for measurement in sorted(every, key=lambda m: m.theta):
        if clusters and measurement.theta - clusters[-1][0].theta <= ANGLE_TOLERANCE:
            clusters[-1].append(measurement)
        else:
            clusters.append([measurement])

    # The label becomes a parameter key, which is recorded in the .par file and
    # the posterior. So it must not move when another state joins the fit: a key
    # that shifts makes two fits of one model look like fits of two. Neither the
    # mean nor the cluster minimum is safe -- a slightly lower theta arriving
    # later moves both. Rounding to the nominal setting is, because at REF_L
    # distinct settings differ by >=0.15 deg while the spread within one is
    # ~0.0005 deg, so every member of a cluster rounds to the same 2 decimals.
    labels: dict[str, str] = {}
    for cluster in clusters:
        label = f"{round(cluster[0].theta, 2):g}deg"
        for measurement in cluster:
            labels[measurement.key] = label

    distinct = {labels[m.key] for m in every}
    if len(distinct) != len(clusters):
        # Two clusters rounded together: the angles are closer than the naming
        # can express, so fall back to full precision rather than silently
        # merging two settings into one parameter.
        labels = {
            measurement.key: f"{cluster[0].theta:g}deg"
            for cluster in clusters
            for measurement in cluster
        }
    return labels


def _add_free_parameters(spec: ModelSpec, table: ParameterTable) -> None:
    """Create free parameters and the slots that read them.

    Slots are collected with a specificity rank first and resolved afterwards,
    so a measurement-level declaration can override a group-level one without
    the two racing on declaration order.
    """
    claims: dict[tuple[str, str], tuple[int, str, Measurement, ParameterPath]] = {}
    by_angle = angle_groups(table.measurements)

    for parameter in spec.parameters:
        path = parameter.parsed
        _check_owner_exists(spec, path, parameter.path)

        for group, index in _targets_for(spec, table, parameter):
            measurements = table.measurements.get(group, [])
            if index is not None:
                selected = [m for m in measurements if m.index == index]
                rank = _RANK_MEASUREMENT
            else:
                selected = measurements
                rank = _RANK_GROUP

            for measurement in selected:
                if parameter.per == "model":
                    key = f"{path.render()}@model"
                    label = "all states"
                elif parameter.per == "state" and index is None:
                    key = f"{path.render()}@{group}"
                    label = group
                elif parameter.per == "angle" and index is None:
                    # Shared across states, so the key deliberately omits the
                    # group: that is the whole point of the scope.
                    label = by_angle[measurement.key]
                    key = f"{path.render()}@{label}"
                else:
                    key = f"{path.render()}@{measurement.key}"
                    label = measurement.key

                identity = (measurement.key, path.render())
                previous = claims.get(identity)
                if previous is not None:
                    if previous[0] == rank and previous[1] != key:
                        raise SpecError(
                            f"{path.render()} is declared twice for {measurement.key} at "
                            f"the same specificity ({previous[1]!r} and {key!r}). "
                            "Make one of them measurement-specific, e.g. "
                            f"`in: [{measurement.key}]`."
                        )
                    if previous[0] > rank:
                        continue

                claims[identity] = (rank, key, measurement, path)
                _create_free(table, key, path, parameter, spec, label=label)

    for _, key, measurement, path in claims.values():
        table.slots.append(Slot(measurement, path, key, "P"))

    _prune_unreferenced(table)


def _prune_unreferenced(table: ParameterTable) -> None:
    """Drop free parameters no surviving slot reads.

    A group-level declaration that every measurement overrode would otherwise
    linger as an unused parameter, inflating the count `nrw model preview`
    reports and the fit's degrees of freedom.
    """
    referenced = {slot.ref for slot in table.slots if slot.kind == "P"}
    referenced |= {
        key for expression in table.expressions for key in _keys_in(expression.source)
    }
    table.free = [p for p in table.free if p.key in referenced]


def _keys_in(source: str) -> set[str]:
    """Extract the P[...] keys an expression refers to."""
    return set(re.findall(r"P\[[\"'](.+?)[\"']\]", source))


def _add_constraints(spec: ModelSpec, table: ParameterTable) -> None:
    """Create expressions and the slots that read them."""
    for constraint in spec.constraints:
        series = spec.series_by_name(constraint.series)
        if series is None:
            raise SpecError(
                f"constraint references unknown series {constraint.series!r}. "
                f"Known: {', '.join(spec.group_names)}."
            )
        slices = table.measurements.get(series.name, [])
        if not slices:
            raise SpecError(f"series {series.name!r} resolved no measurements")

        form = FORMS[constraint.form]
        for path in _expand_paths(spec, constraint):
            if constraint.form == "free":
                _add_free_per_slice(table, spec, constraint, path, slices)
                continue

            start_key = _resolve_endpoint(
                table, spec, path, constraint, constraint.from_, "start"
            )
            end_key = _resolve_endpoint(
                table, spec, path, constraint, constraint.to, "end"
            )

            extra_keys = _create_extras(table, spec, constraint, path, form, start_key)

            context = FormContext(
                start_key=start_key,
                end_key=end_key,
                extra_keys=extra_keys,
                times=[
                    m.time if m.time is not None else float(i)
                    for i, m in enumerate(slices)
                ],
                n=len(slices),
            )

            for i, measurement in enumerate(slices):
                key = f"{path.render()}@{measurement.key}"
                try:
                    source = form.expression(i, context)
                except ConstraintError as exc:
                    raise SpecError(
                        f"{path.render()} in {series.name!r}: {exc}"
                    ) from exc
                table.expressions.append(
                    Expression(
                        key=key,
                        source=source,
                        comment=f"{constraint.form} across {series.name}",
                    )
                )
                table.slots.append(Slot(measurement, path, key, "E"))


def _add_free_per_slice(
    table: ParameterTable,
    spec: ModelSpec,
    constraint: Constraint,
    path: ParameterPath,
    slices: list[Measurement],
) -> None:
    """Handle ``form: free`` -- one independent parameter per slice."""
    anchor = _anchor_bounds(table, spec, path, constraint)
    for measurement in slices:
        key = f"{path.render()}@{measurement.key}"
        table.free.append(
            FreeParameter(
                key=key,
                display=f"{measurement.group}[{measurement.index}] {path.owner} {path.attr}",
                value=anchor[0],
                bounds=anchor[1],
                comment=f"free per slice across {constraint.series}",
            )
        )
        table.slots.append(Slot(measurement, path, key, "P"))

    table.warnings.append(
        f"{path.render()} is `free` across {constraint.series}: "
        f"{len(slices)} extra parameters. The fit can use them to absorb noise."
    )


def _create_free(
    table: ParameterTable,
    key: str,
    path: ParameterPath,
    parameter: ParameterSpec,
    spec: ModelSpec,
    *,
    label: str,
) -> None:
    """Create one free (or fixed) parameter if it does not already exist."""
    if table.free_by_key(key) is not None:
        return

    # The stack declares the starting guess; `range` declares where it may go.
    # So an unspecified value comes from the stack, and the range midpoint is
    # only a fallback for when the stack value lies outside the bounds (which
    # bumps would reject).
    value = parameter.value
    if value is None:
        value = _stack_default(spec, path)
        if (
            parameter.range is not None
            and not parameter.range[0] <= value <= parameter.range[1]
        ):
            value = _midpoint(parameter)

    if parameter.is_fixed:
        # `fixed: 31.2` pins to that number; `fixed: true` pins to `value` (or the
        # stack default). The bool test comes FIRST because bool is a subclass of
        # int in Python: `isinstance(True, int)` is True, so testing the numeric
        # case first pins every `fixed: true` parameter to float(True) == 1.0 and
        # silently discards the value the spec asked for. That shipped once, and
        # it turned a 31.2 A Ti layer at rho -1.88 into a 1 A layer at rho +1.0 --
        # a physically impossible stack no optimiser could fit, in a spec that
        # read correctly.
        pinned = (
            value
            if isinstance(parameter.fixed, bool)
            else parameter.fixed
            if isinstance(parameter.fixed, int | float)
            else value
        )
        table.free.append(
            FreeParameter(
                key,
                parameter.name or f"{label} {path.owner} {path.attr}",
                float(pinned),
                None,
                fixed=True,
                comment="fixed",
            )
        )
        return

    bounds = parameter.range
    if bounds is None and parameter.pm is not None:
        bounds = (value - parameter.pm, value + parameter.pm)

    table.free.append(
        FreeParameter(
            key=key,
            display=parameter.name or f"{label} {path.owner} {path.attr}",
            value=float(value),
            bounds=(float(bounds[0]), float(bounds[1])) if bounds else None,
        )
    )


def _create_extras(
    table: ParameterTable,
    spec: ModelSpec,
    constraint: Constraint,
    path: ParameterPath,
    form: Any,
    start_key: str | None,
) -> dict[str, str]:
    """Create the free parameters a constraint form introduces."""
    keys: dict[str, str] = {}
    anchor_value, anchor_bounds = _anchor_bounds(table, spec, path, constraint)

    for extra in form.extra_parameters(constraint):
        key = f"{path.render()}@{constraint.series}:{extra.suffix}"
        if table.free_by_key(key) is None:
            # A knot's sensible range is the anchor parameter's own range, not
            # the placeholder the form declares.
            bounds = extra.bounds
            value = extra.default
            if "knot" in extra.suffix and anchor_bounds is not None:
                bounds, value = anchor_bounds, anchor_value
            table.free.append(
                FreeParameter(
                    key=key,
                    display=f"{constraint.series} {path.owner} {path.attr} {extra.suffix}",
                    value=float(value),
                    bounds=(float(bounds[0]), float(bounds[1])),
                    comment=extra.description,
                )
            )
        keys[extra.suffix] = key
    del start_key
    return keys


def _resolve_endpoint(
    table: ParameterTable,
    spec: ModelSpec,
    path: ParameterPath,
    constraint: Constraint,
    endpoint: str | None,
    role: str,
) -> str | None:
    """Resolve one endpoint to a free-parameter key.

    An endpoint is usually a state name, and the constraint then borrows that
    state's already-fitted parameter -- which is why the interpolating forms
    add nothing to the parameter count.

    Writing ``free`` instead creates a parameter for the endpoint itself. That
    is what you want when the series has no bracketing steady state to anchor
    to, or when where the sample started and finished *during* the run is the
    measurement rather than an assumption.

    Args:
        table: The table being built.
        spec: The spec.
        path: The parameter path.
        constraint: The constraint being resolved.
        endpoint: The ``from``/``to`` value, or None.
        role: ``start`` or ``end``, used to name the created parameter.

    Returns:
        The key to read the endpoint from, or None when unset.
    """
    from nr_workbench.spec.constraints import FREE_ENDPOINT

    if not endpoint:
        return None
    if endpoint != FREE_ENDPOINT:
        return _endpoint_key(table, path, endpoint, constraint)

    key = f"{path.render()}@{constraint.series}:{role}"
    if table.free_by_key(key) is None:
        value, bounds = _free_endpoint_bounds(table, spec, path, constraint)
        table.free.append(
            FreeParameter(
                key=key,
                display=f"{constraint.series} {path.owner} {path.attr} {role}",
                value=float(value),
                bounds=bounds,
                comment=(
                    f"{role} of the {constraint.form} trajectory across "
                    f"{constraint.series}; fitted rather than anchored to a state"
                ),
            )
        )
    return key


def _free_endpoint_bounds(
    table: ParameterTable, spec: ModelSpec, path: ParameterPath, constraint: Constraint
) -> tuple[float, tuple[float, float]]:
    """Find a range for a fitted endpoint.

    Borrowed, in order of preference, from:

    1. the constraint's own ``endpoint_range``;
    2. any existing declaration of the same path -- if the spec already says a
       Cu thickness lies in [400, 600] for the steady states, that is the same
       physical statement and there is no reason to repeat it;
    3. nothing, which is an error rather than a guess. An unbounded endpoint
       can wander somewhere unphysical and take the whole trajectory with it.

    Raises:
        SpecError: If no range can be found.
    """
    if constraint.endpoint_range is not None:
        low, high = constraint.endpoint_range
        return _stack_default(spec, path), (float(low), float(high))

    rendered = path.render()
    for parameter in table.free:
        if parameter.key.split("@", 1)[0] == rendered and parameter.bounds:
            return parameter.value, parameter.bounds

    for declared in spec.parameters:
        if declared.path == rendered and declared.range is not None:
            low, high = declared.range
            return _stack_default(spec, path), (float(low), float(high))

    raise SpecError(
        f"constraint on {rendered} across {constraint.series!r} fits a free "
        "endpoint, but there is no range to give it. Either declare the path "
        f"in `parameters` (its range is reused), or add `endpoint_range: "
        f"[min, max]` to the constraint."
    )


def _anchor_bounds(
    table: ParameterTable, spec: ModelSpec, path: ParameterPath, constraint: Constraint
) -> tuple[float, tuple[float, float] | None]:
    """Borrow a starting value and bounds from the constraint's `from` state."""
    if constraint.from_:
        anchor = table.free_by_key(f"{path.render()}@{constraint.from_}")
        if anchor is not None:
            return anchor.value, anchor.bounds
    value = _stack_default(spec, path)
    return value, None


def _endpoint_key(
    table: ParameterTable, path: ParameterPath, group: str, constraint: Constraint
) -> str:
    """Resolve an endpoint to a free-parameter key.

    Raises:
        SpecError: If the endpoint state has no free parameter for this path.
            Interpolating between two constants would silently freeze every
            slice, which looks like a working fit and is not one.
    """
    key = f"{path.render()}@{group}"
    if table.free_by_key(key) is not None:
        return key

    model_key = f"{path.render()}@model"
    if table.free_by_key(model_key) is not None:
        table.warnings.append(
            f"{path.render()} is `per: model`, so the {constraint.form} constraint "
            f"across {constraint.series} interpolates between the same value at "
            "both ends and is constant."
        )
        return model_key

    raise SpecError(
        f"constraint on {path.render()} across {constraint.series!r} anchors at "
        f"state {group!r}, but {path.render()} is not a free parameter there. "
        f"Add it to `parameters` with `per: state, in: [{group}, ...]`, or the "
        "interpolation would run between two constants."
    )


def _expand_paths(spec: ModelSpec, constraint: Constraint) -> list[ParameterPath]:
    """Expand a constraint's path globs against the stack."""
    resolved: list[ParameterPath] = []
    seen: set[str] = set()

    for pattern in constraint.paths:
        if "*" not in pattern and "?" not in pattern:
            path = ParameterPath.parse(pattern)
            _check_owner_exists(spec, path, pattern)
            if path.render() not in seen:
                seen.add(path.render())
                resolved.append(path)
            continue

        owner_pattern, _, attr = pattern.partition(".")
        if not attr:
            raise SpecError(f"constraint path {pattern!r} must be 'Layer.attr'")
        matches = [n for n in spec.layer_names if fnmatch.fnmatch(n, owner_pattern)]
        if not matches:
            raise SpecError(
                f"constraint path {pattern!r} matched no layer. "
                f"Stack: {', '.join(spec.layer_names)}."
            )
        for name in matches:
            path = ParameterPath.parse(f"{name}.{attr}")
            if path.render() not in seen:
                seen.add(path.render())
                resolved.append(path)

    return resolved


def _targets_for(
    spec: ModelSpec, table: ParameterTable, parameter: ParameterSpec
) -> list[tuple[str, int | None]]:
    """Resolve a parameter's ``in`` list to (group, measurement index) pairs.

    An entry may name a whole group (``ocv1``) or one measurement within it
    (``ocv1#2``). The second form is what expresses "this segment differs",
    and it outranks the group form.

    Args:
        spec: The model spec.
        table: The table being built, for measurement counts.
        parameter: The parameter declaration.

    Returns:
        Pairs of (group name, index or None for the whole group).

    Raises:
        SpecError: If a target names an unknown group or an out-of-range index.
    """
    if parameter.in_ is None:
        return [(group, None) for group in spec.group_names]

    targets: list[tuple[str, int | None]] = []
    for entry in parameter.in_:
        group, _, suffix = entry.partition("#")
        if group not in spec.group_names:
            raise SpecError(
                f"{parameter.path}: `in` names unknown state/series {group!r}. "
                f"Known: {', '.join(spec.group_names)}."
            )
        if not suffix:
            targets.append((group, None))
            continue

        try:
            index = int(suffix)
        except ValueError as exc:
            raise SpecError(
                f"{parameter.path}: {entry!r} -- the part after '#' must be a "
                "zero-based measurement index."
            ) from exc

        available = len(table.measurements.get(group, []))
        if not 0 <= index < available:
            raise SpecError(
                f"{parameter.path}: {entry!r} is out of range; {group!r} has "
                f"{available} measurement(s), so valid indices are 0..{available - 1}."
            )
        targets.append((group, index))

    return targets


def _check_owner_exists(spec: ModelSpec, path: ParameterPath, original: str) -> None:
    """Fail clearly when a path names a layer that is not in the stack."""
    if path.is_probe:
        return
    if spec.layer(path.owner) is None:
        suggestion = _closest(path.owner, spec.layer_names)
        hint = f" Did you mean {suggestion!r}?" if suggestion else ""
        raise SpecError(
            f"{original!r} names layer {path.owner!r}, which is not in the stack "
            f"({', '.join(spec.layer_names)}).{hint}"
        )


def _check_no_double_assignment(table: ParameterTable) -> None:
    """Reject slots written by both a free parameter and a constraint.

    Overwhelmingly the same mistake every time: structural parameters are
    declared ``per: state`` with no ``in:``, which scopes them to *every* group
    including the series, and the series is also covered by a constraint.

    **Every** collision is collected before raising. The mistake is made once,
    in one habit, and applies to every structural parameter in the spec -- on a
    real five-layer model that was eight of them. Reporting the first and
    stopping turns one edit into eight validate-fix cycles.

    The fix is almost never to delete either declaration; it is to scope the
    parameters to the steady states so the constraint owns the series. The
    message says so specifically, because the generic advice ("remove one")
    names the two wrong answers.
    """
    seen: dict[tuple[str, str], str] = {}
    collisions: dict[str, Slot] = {}
    for slot in table.slots:
        identity = (slot.measurement.key, slot.path.render())
        previous = seen.get(identity)
        if previous is not None and previous != slot.ref:
            collisions.setdefault(slot.path.render(), slot)
        seen[identity] = slot.ref

    if collisions:
        raise SpecError(_double_assignment_message(table, collisions))


def _double_assignment_message(
    table: ParameterTable, collisions: dict[str, Slot]
) -> str:
    """Describe every collision at once, and the one fix that resolves them."""
    spec = table.spec
    series_names = {series.name for series in spec.series}
    state_names = [state.name for state in spec.states]

    in_series = {
        rendered: slot
        for rendered, slot in collisions.items()
        if slot.measurement.group in series_names
    }
    plural = "s" if len(collisions) != 1 else ""
    listed = "\n".join(
        f"    {rendered}  (in series {slot.measurement.group!r})"
        if slot.measurement.group in series_names
        else f"    {rendered}  (in {slot.measurement.group!r})"
        for rendered, slot in sorted(collisions.items())
    )
    head = (
        f"{len(collisions)} path{plural} assigned twice -- once as a free "
        f"parameter and once by a constraint:\n\n{listed}\n"
    )

    if in_series and state_names:
        scoped = ", ".join(state_names)
        example = sorted(in_series)[0]
        return (
            head + "\nEach is declared `per: state` with no `in:`, which scopes it to "
            "every group -- including the series the constraint already owns.\n\n"
            f"Add `in: [{scoped}]` to {'each' if len(in_series) > 1 else 'it'}:\n\n"
            f"    - {{path: {example}, ..., per: state, in: [{scoped}]}}\n\n"
            "The constraint then supplies the series' values. Deleting the "
            "declarations instead would lose their ranges -- which the "
            "constraint borrows for a `free` endpoint -- and removing them from "
            "the constraint's `paths` would leave the series unconstrained."
        )

    return (
        head + "\nA path cannot be both freely fitted and constrained for the same "
        "measurement. Scope the `parameters` entries with `in:` so the two do "
        "not overlap, or remove the paths from the constraint's `paths`."
    )


def _stack_default(spec: ModelSpec, path: ParameterPath) -> float:
    """The starting value a path takes from the stack definition."""
    if path.is_probe:
        return {
            "intensity": 1.0,
            "background": 0.0,
            "theta_offset": 0.0,
            "sample_broadening": 0.0,
        }.get(path.attr, 0.0)
    layer = spec.layer(path.owner)
    if layer is None:
        return 0.0
    if path.attr == "thickness":
        return layer.thickness
    if path.attr == "roughness":
        return layer.roughness
    material = spec.materials.get(layer.material_key)
    if material is None:
        return 0.0
    return float(material.rho or 0.0) if path.attr == "rho" else float(material.irho)


def _midpoint(parameter: ParameterSpec) -> float:
    """Midpoint of a parameter's range."""
    low, high = parameter.range  # type: ignore[misc]
    return (low + high) / 2.0


def _closest(word: str, options: list[str]) -> str | None:
    """Nearest option by case-insensitive similarity, for a did-you-mean."""
    import difflib

    matches = difflib.get_close_matches(
        word.lower(), [o.lower() for o in options], n=1, cutoff=0.6
    )
    if not matches:
        return None
    return next((o for o in options if o.lower() == matches[0]), None)
