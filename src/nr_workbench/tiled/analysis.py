"""The experiment's ``analysis/`` on Tiled: the fits a push writes there.

``analysis/<sample>/<fit_id>/`` holds one fit: each starred fit, and each
sample's ``final`` fit. Tiled 0.2.18 takes no file from a project member, so a
fit is one container whose metadata is its record: its files as text, each
with its sha256, and its provenance as nrw writes it. A colleague who pulls it
runs it again from those files (:mod:`nr_workbench.tiled.pull`).

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
from pathlib import Path
from typing import Any

from nr_workbench.tiled.context import Record, Remote, Tree

ANALYSIS = "analysis"

#: What each node says it is: a spec's name, and its fields' version.
ANALYSIS_SPEC = ("analysis-experiment", "1")
ANALYSIS_SAMPLE_SPEC = ("analysis-sample", "1")
FIT_SPEC = ("analysis-fit", "1")

#: ``analysis/``: a container per sample, and a fit's record in it.
ANALYSIS_TREE = Tree(
    ANALYSIS,
    ANALYSIS_SPEC,
    part_spec=ANALYSIS_SAMPLE_SPEC,
    part_key="sample_id",
    ignore=("pushed_at", "nr_workbench"),
)

#: A fit's files a record carries, by their names in its directory.
FILES = ("model.py", "spec.yaml", "model.md", "NOTES.md")

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
    from nr_workbench.provenance.curation import FINAL, replay
    from nr_workbench.provenance.index import FitIndex
    from nr_workbench.provenance.lookup import fit_dir

    layout = ProjectLayout(root=Path(root))
    index = FitIndex(layout.index_file)
    state = replay(index.entries())
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
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
            fields = record_of(entry, kept, directory, pushed_at=stamp)
            records.append(Record(sample, fit_id, fields, FIT_SPEC))
        elif on_tiled:
            records.append(Record(sample, fit_id, {"curation": kept}, FIT_SPEC))
        elif wanted:
            why = "it has no sample" if not sample else "its files are not here"
            left.append(Left(fit_id, why))
    return records, left


def curation_fields(curation: Any) -> dict[str, Any]:
    """What a person has said about a fit, as a record carries it: all but its
    pushes to ISAAC."""
    fields = curation.as_dict()
    fields.pop("published", None)
    return fields


def record_of(
    entry: dict[str, Any],
    curation: dict[str, Any],
    directory: Path,
    *,
    pushed_at: str,
) -> dict[str, Any]:
    """One fit's record, as Tiled holds it.

    Args:
        entry: The fit's index entry.
        curation: What :func:`curation_fields` says of it.
        directory: Its result directory.
        pushed_at: When the push runs, ISO-8601 UTC.

    Returns:
        The record: the index's fields, its curation, its manifest, inputs and
        environment whole, its parameters, its assessment, and its files.
    """
    from nr_workbench import __version__

    manifest = _json(directory / "manifest.json") or {}
    record: dict[str, Any] = {name: entry.get(name) for name in INDEX_FIELDS}
    record["n_points"] = (manifest.get("info") or {}).get("n_points")
    record["curation"] = curation
    record["manifest"] = manifest
    record["inputs"] = _json(directory / "inputs.json")
    record["env"] = _json(directory / "env" / "versions.json")
    record["parameters"] = parameters(directory)
    record["assessment"] = _json(directory / "assessment.json")
    record["files"] = {
        name: file_of(directory / name)
        for name in FILES
        if (directory / name).is_file()
    }
    record["nr_workbench"] = __version__
    record["written_by"] = "nr-workbench"
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


def _json(path: Path) -> Any:
    """A JSON file's content, or ``None`` when the fit has none."""
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
