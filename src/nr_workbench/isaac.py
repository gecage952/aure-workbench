"""Stage a fit as a data-assembler ingest directory, for ISAAC export.

nr-workbench does not map anything to the ISAAC schema. That mapping lives in
``nr-isaac-format``, fed by ``data-assembler``, and reimplementing it here would
be a second copy to keep in step with a schema neither project owns.

What the assembler needs is a *file contract*, not a workflow: a directory
holding ``run_info.json``, the serialised bumps problem, its ``-err.json``
companion, and a ``final_state.json`` carrying chi-squared. Everything in that
contract is already in a fit record, so this module rewrites it into the shape
the assembler reads and lets the canonical pipeline do the rest.

Two things about that contract earn the module.

**Angle segments are one measurement, not three.** A REF_L steady state is
measured at three incident angles and reduced to three files with three run
numbers. They are one physical measurement of one sample, and the assembler
treats every file in a state's ``data_files`` as exactly that -- one ISAAC
record carrying N series. Listing them as separate states would claim three
measurements that never happened. (Getting this wrong is a bug AuRE has already
fixed once.)

**A co-refinement is several states of one sample.** Writing ``states[]``
rather than a flat file list gets each condition its own record, with the whole
co-refinement recorded as one fit spanning them, and the records cross-linked
by ``links[].same_sample_as`` because they share a sample id. That is what
makes three ISAAC records readable as three conditions of one experiment
rather than three unrelated measurements.

The grouping is never inferred from filenames. It comes from the frozen spec,
whose ``Measurement.key`` (``run218386#0``) is the same string the fit recorded
in its manifest -- so what is exported is what was fitted.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Filename the assembler expects for the serialised bumps problem.
PROBLEM_FILENAME = "problem.json"

#: REF_L's own name for a stitched full-Q curve. The assembler reads the run
#: number out of it, so using the convention rather than inventing one is what
#: keeps the record pointing at the right measurement.
COMBINED_TEMPLATE = "REFL_{run}_combined_data_auto.txt"

#: Marks a directory as one this module created, and may therefore replace.
#: The assembler names its outputs by uuid, so a second run writes a whole new
#: set beside the first instead of overwriting it -- and the converter then
#: sees twice as many states and emits twice as many records. Re-exporting has
#: to start from an empty directory, but "empty it" must never be applied to a
#: directory somebody else owns.
SENTINEL = ".nrw-isaac-staging"


@dataclass
class StagedState:
    """One physical condition, with the angle segments that measured it.

    Attributes:
        name: The state's name in the spec, e.g. ``run218386``.
        files: Absolute paths to its reduced data, one per angle segment.
        condition: Free text describing the condition, from the spec.
    """

    name: str
    files: list[str]
    condition: str | None = None
    segments: int = 1
    run: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the ``states[]`` entry the assembler reads."""
        entry: dict[str, Any] = {"name": self.name, "data_files": self.files}
        if self.condition:
            entry["extra_description"] = self.condition
        return entry


@dataclass
class Staged:
    """What was written, and what could not be.

    Attributes:
        directory: The staging directory.
        states: The states written to ``run_info.json``.
        chisq: Chi-squared carried into ``final_state.json``.
        problems: Anything the export will be poorer for.
    """

    directory: Path
    states: list[StagedState]
    chisq: float | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def n_files(self) -> int:
        """Total reduced files across every state."""
        return sum(len(s.files) for s in self.states)


def fitted_scales(fit_dir: Path) -> dict[str, float]:
    """Per-measurement intensity scales, keyed by ``Measurement.key``.

    A REF_L angle segment carries its own normalisation, and the fit is what
    determines it: run 218386's 3.5 deg segment fits an intensity of 0.789,
    which is the 21 % the raw file is low by. Concatenating without applying
    these publishes a curve with a visible step in it.

    Args:
        fit_dir: The result directory.

    Returns:
        ``{"run218386#2": 0.789, ...}``, empty when nothing was fitted.
    """
    scales: dict[str, float] = {}
    pattern = re.compile(r"^(\S+#\d+)\s+probe\s+intensity$")
    for path in sorted((fit_dir / "fit").glob("*.par")):
        for line in path.read_text(encoding="utf-8").splitlines():
            name, _, number = line.strip().rpartition(" ")
            match = pattern.match(name.strip())
            if not match:
                continue
            try:
                value = float(number)
            except ValueError:
                continue
            if value > 0:
                scales[match.group(1)] = value
        break
    return scales


def concatenate(
    paths: list[Path],
    scales: list[float],
    destination: Path,
    *,
    run: str,
) -> Path:
    """Merge a state's angle segments into one full-Q curve.

    Each segment is divided by its fitted intensity before merging --- the
    direction is not a guess: on run 218386 it takes the segment-2/3 overlap
    from +30 % to +1 %, and multiplying instead takes it to +68 %.

    Args:
        paths: The segment files, any order.
        scales: The fitted intensity for each, aligned with ``paths``.
        destination: Directory to write into.
        run: Run number for the filename.

    Returns:
        The written file.

    Raises:
        ValueError: If no segment could be read.
    """
    import numpy as np

    rows = []
    applied = []
    for path, scale in zip(paths, scales, strict=True):
        data = np.loadtxt(path, ndmin=2)
        if data.size == 0:
            continue
        block = np.zeros((len(data), 4))
        block[:, 0] = data[:, 0]
        # R and dR carry the scale; Q and dQ are geometry and do not.
        block[:, 1] = data[:, 1] / scale
        block[:, 2] = (data[:, 2] if data.shape[1] > 2 else 0.0) / scale
        block[:, 3] = data[:, 3] if data.shape[1] > 3 else 0.0
        rows.append(block)
        applied.append((Path(path).name, scale))

    if not rows:
        raise ValueError(f"No usable data in {[str(p) for p in paths]}")

    merged = np.vstack(rows)
    merged = merged[np.argsort(merged[:, 0])]

    target = Path(destination) / COMBINED_TEMPLATE.format(run=run)
    header = [
        # The primary segment's own header first. It carries the IPTS, run
        # number, title and reduction version, and downstream readers take the
        # run number from there rather than from the filename -- drop it and
        # the record says "Unknown".
        *_carried_header(Path(paths[0])),
        "",
        "Concatenated from the angle segments below by nr-workbench.",
        "R and dR are divided by each segment's fitted intensity, which is",
        "the per-segment normalisation the co-refinement determined.",
        "",
        *(f"  {name}  intensity {scale:.6g}" for name, scale in applied),
        "",
        "Q (1/A)  R  dR  dQ (FWHM, 1/A)",
    ]
    np.savetxt(
        target,
        merged,
        fmt="%.8e",
        header="\n".join(header),
    )
    return target


def reset(destination: Path, *, force: bool = False) -> None:
    """Empty a staging directory, or refuse if it is not ours to empty.

    Args:
        destination: The directory to clear and recreate.
        force: Replace it even without the sentinel.

    Raises:
        FileExistsError: If it exists, is not empty, carries no sentinel, and
            ``force`` was not given.
    """
    destination = Path(destination)
    if destination.exists():
        unowned = any(destination.iterdir()) and not (destination / SENTINEL).exists()
        if unowned and not force:
            raise FileExistsError(
                f"{destination} already has files in it and carries no marker "
                "saying nrw wrote them.\n"
                "Re-exporting into a directory that already holds assembler "
                "output doubles every record, so this stops rather than "
                "guessing.\n"
                "  --out <elsewhere>   write somewhere clean\n"
                "  --force             replace this directory\n"
                "(A directory written by nrw before this check existed will "
                "need --force once.)"
            )
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    (destination / SENTINEL).write_text(
        "Written by `nrw isaac export`; replaced on each run.\n", encoding="utf-8"
    )


def stage(
    fit_dir: Path,
    root: Path,
    destination: Path,
    *,
    sample_description: str | None = None,
    sample_markdown: str = "",
    use_llm: bool = True,
    force: bool = False,
) -> Staged:
    """Write a fit into a directory ``data-assembler ingest-workflow`` can read.

    Args:
        fit_dir: The immutable result directory.
        root: Project root, for resolving the recorded relative paths.
        destination: Directory to create and populate.
        sample_description: Prose for the sample record.
        sample_markdown: The sample's prose, which is where the experimental
            conditions actually live.
        use_llm: Let a language model read that prose when one is configured.
        force: Replace the destination even if nrw did not write it.

    Returns:
        What was staged.

    Raises:
        FileNotFoundError: If the fit has no serialised problem, without which
            there is no fitted model to export.
        FileExistsError: If the destination holds files this module did not
            write.
    """
    fit_dir, root = Path(fit_dir), Path(root)
    reset(destination, force=force)
    problems: list[str] = []

    problem = _problem_json(fit_dir)
    if problem is None:
        raise FileNotFoundError(
            f"No serialised bumps problem in {fit_dir / 'fit'}. ISAAC records "
            "carry the fitted model, so there is nothing to export without it."
        )
    shutil.copy2(problem, destination / PROBLEM_FILENAME)

    # The uncertainty companion. The assembler finds it beside the problem and
    # uses it for per-parameter sigma; without it the record carries values
    # with no error bars, which is worth saying rather than shipping quietly.
    err = next(iter(sorted((fit_dir / "fit").glob("*-err.json"))), None)
    if err is not None:
        shutil.copy2(err, destination / f"{Path(PROBLEM_FILENAME).stem}-err.json")
    else:
        problems.append(
            "No -err.json: this was an optimiser run, so the exported record "
            "will carry fitted values with no uncertainties."
        )

    data = destination / "data"
    data.mkdir(exist_ok=True)
    states = _states(fit_dir, root, problems, data)
    if not states:
        raise FileNotFoundError(
            f"No reduced data files could be resolved for {fit_dir.name}."
        )

    # The conditions. A fit knows nothing about applied potential; the
    # scientist wrote it in sample.md before any of this ran, and it has to be
    # carried over or the record says only "ex_situ".
    from nr_workbench.conditions import describe

    described = describe(
        states=[(s.name, s.run or "", s.condition) for s in states],
        sample_markdown=sample_markdown,
        use_llm=use_llm,
    )
    # A state with nothing is named, unless no state has anything: then once.
    unstated = []
    for state in states:
        found = described.get(state.name)
        if found and found.text:
            state.condition = found.text
        elif not state.condition:
            unstated.append(state.name)
    if len(unstated) == len(states):
        problems.append(
            "No experimental conditions found in sample.md or the spec, so the "
            "records will not say what each measurement was measured under."
        )
    else:
        problems.extend(
            f"No condition found for {name}, so its record will not say what it "
            "was measured under."
            for name in unstated
        )

    manifest = _read_json(fit_dir / "manifest.json")
    info = manifest.get("info") or {}
    chisq = info.get("chisq")

    run_info: dict[str, Any] = {
        "run_id": fit_dir.name,
        # Ignored by the assembler, and the honest answer to "who wrote this".
        "generator": "nr-workbench",
        "states": [s.as_dict() for s in states],
        # The default, stated explicitly: co-refined states are conditions of
        # one physical sample, so they share a sample id and the records come
        # out cross-linked. A co-refinement of genuinely different samples
        # would set this true -- nr-workbench has no way to express that yet,
        # so it is never guessed.
        "distinct_sample": False,
    }
    if sample_description:
        run_info["sample_description"] = sample_description

    _write_json(destination / "run_info.json", run_info)
    _write_json(
        destination / "final_state.json",
        {
            "final_chi2": chisq,
            "state": {
                "states": [
                    {"name": s.name, "extra_description": s.condition or ""}
                    for s in states
                ]
            },
        },
    )
    return Staged(directory=destination, states=states, chisq=chisq, problems=problems)


def _states(
    fit_dir: Path, root: Path, problems: list[str], workspace: Path
) -> list[StagedState]:
    """Group the fit's data into states, preferring the frozen spec."""
    spec_path = fit_dir / "spec.yaml"
    if spec_path.is_file():
        grouped = _states_from_spec(
            spec_path, root, problems, fitted_scales(fit_dir), workspace
        )
        if grouped:
            return grouped
    return _states_from_inputs(fit_dir, root, problems)


def _states_from_spec(
    spec_path: Path,
    root: Path,
    problems: list[str],
    intensities: dict[str, float],
    workspace: Path,
) -> list[StagedState]:
    """Group by the spec's states, which is how the fit itself was built.

    ``discover_measurements`` returns the same grouping the generator used, so
    a state's files here are exactly the Experiments that state contributed.
    """
    from nr_workbench.spec.models import SpecError, load_spec
    from nr_workbench.spec.resolve import discover_measurements

    try:
        spec = load_spec(spec_path)
        found = discover_measurements(spec, root)
    except (SpecError, OSError, ValueError) as exc:
        problems.append(
            f"Could not read the frozen spec ({exc}); grouping every file as "
            "one state instead."
        )
        return []

    conditions = {s.name: getattr(s, "condition", None) for s in spec.states}
    for series in getattr(spec, "series", None) or []:
        conditions.setdefault(series.name, getattr(series, "condition", None))

    runs = {s.name: str(getattr(s, "run", "") or "") for s in spec.states}

    states = []
    for group, measurements in found.items():
        paths, scales = [], []
        for measurement in measurements:
            path = (root / measurement.file).resolve()
            if path.is_file():
                paths.append(path)
                scales.append(intensities.get(measurement.key, 1.0))
            else:
                problems.append(f"Recorded data file is missing: {measurement.file}")
        if not paths:
            continue

        run = runs.get(group) or _run_from(paths[0]) or group
        if len(paths) > 1 and not any(k.startswith(f"{group}#") for k in intensities):
            problems.append(
                f"{group} has {len(paths)} angle segments but the fit varied no "
                "per-segment intensity, so they are merged unscaled. Any "
                "normalisation difference between them will show as a step."
            )
        try:
            combined = concatenate(paths, scales, workspace, run=run)
        except (ValueError, OSError) as exc:
            problems.append(f"Could not concatenate {group}: {exc}")
            continue

        states.append(
            StagedState(
                name=group,
                files=[str(combined)],
                condition=conditions.get(group),
                segments=len(paths),
                run=run,
            )
        )
    return states


def _carried_header(path: Path) -> list[str]:
    """The comment lines of a reduced file, without its column header.

    Kept verbatim: the reader takes the run number, IPTS and reduction
    version from here, and re-deriving them would be a second source of
    truth for facts the instrument already recorded.
    """
    lines = []
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw.startswith("#"):
                break
            text = raw.lstrip("#").strip()
            # Our own column header replaces theirs.
            if text.startswith("Q ") or text.startswith("Q["):
                continue
            lines.append(text)
    except OSError:
        return []
    return lines


def _run_from(path: Path) -> str | None:
    """The run number in a REF_L filename, or None."""
    match = re.search(r"REFL?_?L?_(\d{4,})", path.name)
    return match.group(1) if match else None


def _states_from_inputs(
    fit_dir: Path, root: Path, problems: list[str]
) -> list[StagedState]:
    """Fall back to one state holding every recorded data file.

    Used for a hand-written or forked script, which has no spec to group by.
    One state is the honest answer there: the segments still assemble into a
    single measurement, but nothing in the record says which condition each
    file belongs to, and inventing that from filenames would be a guess
    presented as provenance.
    """
    from nr_workbench.provenance.hashing import MODEL_ROLES
    from nr_workbench.provenance.record import FitDirectory

    files = []
    for entry in FitDirectory(fit_dir).read_inputs():
        if entry.get("role") in MODEL_ROLES:
            continue
        path = (root / str(entry.get("path", ""))).resolve()
        if path.is_file():
            files.append(str(path))
    if not files:
        return []
    problems.append(
        "No frozen spec, so every data file is exported as one state. A "
        "co-refinement of several conditions will come out as a single "
        "record rather than one per condition."
    )
    return [StagedState(name=fit_dir.name, files=files)]


def _problem_json(fit_dir: Path) -> Path | None:
    """Locate the serialised bumps problem among the fit outputs."""
    for path in sorted((fit_dir / "fit").glob("*.json")):
        if path.name.endswith("-err.json") or "-expt" in path.name:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and "references" in payload:
            return path
    return None


def _read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object, or return an empty mapping."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write a JSON object."""
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
