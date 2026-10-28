"""The experiment's ``analysis/`` on Tiled: the fits a push writes there.

``analysis/<sample>/<fit_id>/`` holds one fit: each starred fit, and each
sample's ``final`` fit. Tiled 0.2.18 takes no file from a project member, so a
fit is one container whose metadata is its record, the fit as analysed data:

* the model: bumps' own file of the fitted problem, ``fit/<model>.json``,
  which holds the stack, every parameter at its best-fit value with its
  bounds, the instrument settings and the measured data; refl1d loads it as
  it is, and recomputes R(q) and the SLD profile from it;
* the intent: ``spec.yaml`` and ``model.py``, with ``model.md`` and
  ``NOTES.md``;
* the result: each parameter with its DREAM statistics, the fit's
  statistics, each segment's R(q) with the data, its slabs, and the SLD
  profile thinned for a plot;
* the provenance: the reduced files as the fit read them, with their
  headers; the fit's index line, ``inputs.json``, ``manifest.json`` and
  ``env/versions.json``; the curation, each promotion's reason included; and
  who pushed it, when.

The chains stay here: :mod:`nr_workbench.tiled.pull` rebuilds the fit from
its record, and ``nrw fit run --from`` runs it again for the posterior.

A fit pushed before, then unstarred, discarded or deleted here, keeps its
record on Tiled: the next push writes its curation over Tiled's copy, and
nothing else. Nothing is removed there.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from nr_workbench.tiled.context import Record, Remote, Tree

ANALYSIS = "analysis"

#: What each node says it is: a spec's name, and its fields' version.
ANALYSIS_SPEC = ("analysis-experiment", "1")
ANALYSIS_SAMPLE_SPEC = ("analysis-sample", "1")
FIT_SPEC = ("analysis-fit", "2")

#: ``analysis/``: a container per sample, and a fit's record in it.
ANALYSIS_TREE = Tree(
    ANALYSIS,
    ANALYSIS_SPEC,
    part_spec=ANALYSIS_SAMPLE_SPEC,
    part_key="sample_id",
    ignore=("pushed_at", "pushed_by", "nr_workbench"),
)

#: A fit's files a record carries as text, by their names in its directory.
FILES = ("model.py", "spec.yaml", "model.md", "NOTES.md")

#: A fit's JSON files a record carries as they are: each one's part of the
#: record, and its path in the fit's directory.
JSON_FILES = {
    "manifest": "manifest.json",
    "inputs": "inputs.json",
    "env": "env/versions.json",
    "assessment": "assessment.json",
}

#: Each segment's curves a record carries: each one's part of the segment,
#: and the end of its file's name, after ``<model>-<n>``.
CURVES = {"refl": "-refl.dat", "slabs": "-slabs.dat", "profile": "-profile.dat"}

#: The index's fields a record carries as they are: what Tiled's search and a
#: person read first.
INDEX_FIELDS = (
    "fit_id",
    "sample",
    "model",
    "method",
    "status",
    "chisq",
    "n_free",
    "converged",
    "started_at",
    "finished_at",
    "note",
    "stack",
    "run_key",
    "inputs_digest",
    "data_digest",
    "script_sha256",
)

#: A parameter's DREAM statistics a record carries, from ``-err.json``.
STATISTICS = ("std", "median", "p68", "p95")

#: Most points of the SLD profile a record carries: enough for a plot.
PROFILE_POINTS = 400


@dataclass(frozen=True)
class Left:
    """A fit a push would write, and does not.

    Attributes:
        fit_id: The fit.
        why: Why, as a person reads it.
    """

    fit_id: str
    why: str


def fit_records(root: Path, remote: Remote) -> tuple[list[Record], list[Left]]:
    """The project's fits as a push writes them over *remote*.

    A starred fit, or a sample's ``final``, neither discarded nor deleted, is
    written whole. Any other fit Tiled holds already has its curation written,
    and nothing else. By sample, oldest first.

    Args:
        root: Project root.
        remote: ``analysis/`` as it was read.

    Returns:
        The records, and the fits left out, each with why.
    """
    from nr_workbench.project.layout import ProjectLayout
    from nr_workbench.provenance.curation import FINAL, current_user, replay
    from nr_workbench.provenance.index import FitIndex
    from nr_workbench.provenance.lookup import fit_dir

    layout = ProjectLayout(root=Path(root))
    index = FitIndex(layout.index_file)
    state = replay(index.entries())
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    who = current_user()
    records: list[Record] = []
    left: list[Left] = []
    oldest_first = list(reversed(index.fits()))
    for entry in sorted(oldest_first, key=lambda e: str(e.get("sample") or "")):
        fit_id = str(entry.get("fit_id") or "")
        sample = str(entry.get("sample") or "")
        curation = state.of(fit_id)
        kept = curation_fields(curation)
        on_tiled = (sample, fit_id) in remote.records
        wanted = (curation.starred or FINAL in curation.labels) and not (
            curation.discarded or curation.deleted
        )
        directory = fit_dir(layout, entry) if wanted and sample else None
        if directory is not None:
            fields = record_of(
                entry, kept, directory, root=layout.root, pushed_at=stamp, pushed_by=who
            )
            records.append(Record(sample, fit_id, fields, FIT_SPEC))
        elif on_tiled:
            records.append(Record(sample, fit_id, {"curation": kept}, FIT_SPEC))
        elif wanted:
            why = "it has no sample" if not sample else "its files are not here"
            left.append(Left(fit_id, why))
    return records, left


def curation_fields(curation: Any) -> dict[str, Any]:
    """What a person has said about a fit, as a record carries it: all but its
    pushes to ISAAC, and each promotion's reason, who and when, and whether it
    was forced over inputs changed since the fit."""
    fields = curation.as_dict()
    fields.pop("published", None)
    fields["promotions"] = {
        label: {key: promotion.get(key) for key in ("reason", "who", "at", "forced")}
        for label, promotion in curation.promotions.items()
    }
    return fields


def record_of(
    entry: dict[str, Any],
    curation: dict[str, Any],
    directory: Path,
    *,
    root: Path,
    pushed_at: str,
    pushed_by: str,
) -> dict[str, Any]:
    """One fit's record, as Tiled holds it.

    Args:
        entry: The fit's index entry.
        curation: What :func:`curation_fields` says of it.
        directory: Its result directory.
        root: Project root, where its data files are.
        pushed_at: When the push runs, ISO-8601 UTC.
        pushed_by: Who runs it.

    Returns:
        The record described in the module's docstring.
    """
    from nr_workbench import __version__

    record: dict[str, Any] = {name: entry.get(name) for name in INDEX_FIELDS}
    record["index"] = {key: value for key, value in entry.items() if key != "event"}
    record["curation"] = curation
    record.update({part: _json(directory / name) for part, name in JSON_FILES.items()})
    record["n_points"] = ((record["manifest"] or {}).get("info") or {}).get("n_points")
    record["parameters"] = parameters(directory)
    record["files"] = {
        name: file_of(directory / name)
        for name in FILES
        if (directory / name).is_file()
    }
    record["problem"] = problem_file(directory)
    record["fit"] = fit_files(directory)
    record["curves"] = curves(directory)
    record["data"] = data_files(root, directory, record["inputs"])
    record["nr_workbench"] = __version__
    record["written_by"] = "nr-workbench"
    record["pushed_by"] = pushed_by
    record["pushed_at"] = pushed_at
    return jsonable(record)


def parameters(directory: Path) -> list[dict[str, Any]]:
    """Each fitted parameter: its value, its range, and DREAM's statistics."""
    from nr_workbench.fitting.assess import read_bounds, read_par, read_uncertainty

    bounds = read_bounds(directory)
    statistics = read_uncertainty(directory)
    rows = []
    for name, value in read_par(directory).items():
        row: dict[str, Any] = {
            "name": name,
            "value": value,
            "bounds": list(bounds[name]) if name in bounds else None,
        }
        own = statistics.get(name, {})
        row.update({key: own[key] for key in STATISTICS if key in own})
        rows.append(row)
    return rows


def problem_file(directory: Path) -> dict[str, Any] | None:
    """Bumps' file of the fitted problem, ``fit/<model>.json``, or ``None``
    when the fit left none."""
    from nr_workbench.fitting.runner import problem_path

    path = problem_path(directory)
    return {"name": path.name, **file_of(path)} if path is not None else None


def fit_files(directory: Path) -> dict[str, Any]:
    """The best-fit values and DREAM's statistics, as bumps wrote them:
    ``fit/<model>.par`` and ``fit/<model>-err.json``."""
    found = {}
    for pattern in ("*.par", "*-err.json"):
        for path in sorted((directory / "fit").glob(pattern)):
            found[path.name] = file_of(path)
    return found


def curves(directory: Path) -> list[dict[str, Any]]:
    """Each segment's curves: its R(q) with the data, its slabs, and its SLD
    profile thinned to :data:`PROFILE_POINTS`."""
    fit = directory / "fit"
    found = []
    for n, refl in _numbered(fit, CURVES["refl"]):
        stem = refl.name[: -len(CURVES["refl"])]
        segment: dict[str, Any] = {"index": n}
        for part, end in CURVES.items():
            path = fit / f"{stem}{end}"
            if path.is_file():
                segment[part] = (
                    thinned(path)
                    if part == "profile"
                    else {"name": path.name, **file_of(path)}
                )
        found.append(segment)
    return found


def thinned(profile: Path) -> dict[str, Any]:
    """An SLD profile of at most :data:`PROFILE_POINTS` points: its header,
    every ``stride``-th row from the first, and the last.

    A profile of that many points or fewer is kept whole, so a profile
    thinned once, as a pulled fit holds it, is the same thinned again.
    """
    lines = profile.read_text(encoding="utf-8").splitlines()
    header = [line for line in lines if line.startswith("#")]
    rows = [line for line in lines if line.strip() and not line.startswith("#")]
    kept = rows
    if len(rows) > PROFILE_POINTS:
        # The first and the last row, and as many between as fit.
        stride = math.ceil((len(rows) - 1) / (PROFILE_POINTS - 1))
        kept = rows[::stride]
        if (len(rows) - 1) % stride:
            kept.append(rows[-1])
    text = "\n".join(header + kept) + "\n"
    return {
        "name": profile.name,
        "text": text,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "bytes": len(text.encode("utf-8")),
        "points": len(kept),
    }


def data_files(root: Path, directory: Path, inputs: Any) -> dict[str, Any]:
    """The reduced files the fit read, as it read them: each input file but
    the script, by its path in ``inputs.json``, with its text when the file
    is still the one recorded there.

    A fit pulled from Tiled keeps its own copy of each in its directory
    (:func:`data_copy`), which is read first.
    """
    found: dict[str, Any] = {}
    entries = (inputs or {}).get("inputs") if isinstance(inputs, dict) else None
    for entry in entries or ():
        if not isinstance(entry, dict) or entry.get("role") == "script":
            continue
        relative = str(entry.get("path") or "")
        own = directory / data_copy(relative)
        path = own if own.is_file() else Path(root) / relative
        recorded = entry.get("sha256")
        item: dict[str, Any] = {
            "role": entry.get("role"),
            "path": relative,
            "sha256": recorded,
        }
        if path.is_file():
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != recorded:
                item["why"] = "the file here is not the one the fit read"
            else:
                try:
                    item.update({"text": data.decode("utf-8"), "bytes": len(data)})
                except UnicodeDecodeError:
                    item["why"] = "the file is not text"
        else:
            item["why"] = "the file is not here"
        found[relative] = item
    return found


def data_copy(recorded: str) -> str:
    """Where in its directory a fit pulled from Tiled keeps its copy of a
    reduced file it read, the file recorded at *recorded*: under ``data/``,
    at that path."""
    return (PurePosixPath("data") / recorded).as_posix()


def file_of(path: Path) -> dict[str, Any]:
    """A file as a record carries it: its text, sha256 and size, from its bytes."""
    data = path.read_bytes()
    return {
        "text": data.decode("utf-8"),
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }


def jsonable(value: Any) -> Any:
    """*value* as Tiled gives it back: JSON's types, with ``None`` for a NaN or
    an infinity, which JSON has no word for."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(item) for item in value]
    return value


def label_of(fields: dict[str, Any]) -> str:
    """How a fit stands, as a push's list says it: ``final``, ``starred`` …"""
    curation = fields.get("curation") or {}
    if curation.get("deleted"):
        return "deleted"
    if curation.get("discarded"):
        return "discarded"
    if "final" in (curation.get("labels") or []):
        return "final"
    return "starred" if curation.get("starred") else "unstarred"


def _numbered(directory: Path, suffix: str) -> list[tuple[int, Path]]:
    """``<model>-<n><suffix>`` files in *directory*, by ``n``."""
    found = []
    for path in directory.glob(f"*{suffix}"):
        _, _, tail = path.name[: -len(suffix)].rpartition("-")
        if tail.isdigit():
            found.append((int(tail), path))
    return sorted(found)


def _json(path: Path) -> Any:
    """A JSON file's content, or ``None`` when the fit has none."""
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
