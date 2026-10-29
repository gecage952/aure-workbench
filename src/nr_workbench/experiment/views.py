"""The experiment, its runs and samples, as the page and the command line report them.

Both show the same things, and each used to build its own dictionary for them.
They had already drifted: the command line said "the source does not list it"
where the page said "the data source does not list it", and a field added to
one surface was missing from the other. One builder for each, here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nr_workbench.experiment.model import (
    Catalog,
    ExperimentContext,
    RunEntry,
    RunKey,
)
from nr_workbench.experiment.render import sample_md_relpath

#: The state shown for a run the catalog records but the data source does not
#: list -- deleted at the facility, or a source that cannot be reached.
NOT_LISTED = "not listed"


def run_row(key: RunKey, view: Any, entry: RunEntry | None) -> dict[str, Any]:
    """One run: what the source lists, how far it has arrived, what was decided.

    Args:
        key: Which run.
        view: Its :class:`~nr_workbench.experiment.live.RunView`, or ``None``
            when only the catalog knows it.
        entry: Its catalog record, or ``None`` when it has none yet.

    Returns:
        The JSON-ready row.
    """
    source = view.source if view is not None else None
    status = view.status if view is not None else None
    return {
        "key": key.slug(),
        "run": key.run,
        "kind": key.kind,
        "state": status.state if status else NOT_LISTED,
        "reason": status.reason if status else "the data source does not list it",
        "complete": bool(status and status.complete),
        # The source's title and start time when it lists the run; the catalog
        # keeps a snapshot of both so a run that has gone is still recognizable.
        "title": (source.title if source else "") or (entry.title if entry else ""),
        "start_time": (source.start_time if source else "")
        or (entry.start_time if entry else ""),
        "segments": list(source.segments) if source else [],
        "n_segments": source.n_segments if source else None,
        # Every file of the measurement and what it is to it: the segments a
        # fit co-refines, and other artifacts such as the combined curve.
        "artifacts": [
            {"name": f.name, "role": f.role, "segment": f.segment}
            for f in (source.files if source else ())
        ],
        "thetas": [round(t, 3) if t is not None else None for t in source.thetas]
        if source
        else [],
        "announced": view is not None and view.announcement is not None,
        "sample": entry.sample_id if entry else None,
        # A run in a sample whose type was never set is steady, as sample.md
        # writes it; an unassigned one has no type to show.
        "measurement": (
            entry.measurement_type if entry.sample_id else entry.measurement
        )
        if entry
        else "",
        "condition": entry.condition if entry else "",
        "include": entry.include if entry else True,
        "note": entry.note if entry else "",
        "rev": entry.rev if entry else 0,
    }


def sample_card(root: Path, catalog: Catalog, sample_id: str) -> dict[str, Any]:
    """A sample the catalog manages: its context, and which runs it holds.

    Args:
        root: Project root, to say whether ``sample.md`` exists yet.
        catalog: The catalog.
        sample_id: A sample in it.

    Returns:
        The JSON-ready card.
    """
    context = catalog.context_for(sample_id)
    runs = catalog.runs_for(sample_id)
    return {
        "id": sample_id,
        "managed": True,
        "on_disk": (Path(root) / sample_md_relpath(sample_id)).is_file(),
        "title": context.title,
        "description": context.description,
        "details": context.details,
        "mounting": context.mounting,
        "measurement_conditions": context.measurement_conditions,
        "fits_to_perform": context.fits_to_perform,
        "rev": context.rev,
        "runs": [e.key.run for e in runs if e.include],
        "excluded": [e.key.run for e in runs if not e.include],
    }


def experiment_card(record: ExperimentContext) -> dict[str, Any]:
    """The experiment's own context: what every sample in it is part of.

    Args:
        record: The catalog's experiment record.

    Returns:
        The JSON-ready card, with the revision an edit is made against. The
        columns a newer nrw added are left out, as they are of a sample's
        card: they are that version's to show.
    """
    return {
        "summary": record.summary,
        "hypotheses": record.hypotheses,
        "notes": record.notes,
        "rev": record.rev,
    }


def unmanaged_card(sample_id: str) -> dict[str, Any]:
    """A sample on disk that the catalog does not manage, or not yet.

    Args:
        sample_id: The sample's directory name.

    Returns:
        The JSON-ready card.
    """
    return {
        "id": sample_id,
        "managed": False,
        "on_disk": True,
        "runs": [],
        "excluded": [],
    }


def settings_view(root: Path, *, catalogued_runs: int | None = 0) -> dict[str, Any]:
    """The experiment's settings as the Settings page and ``nrw experiment
    settings`` show them: what is set, what follows nrw's default, which
    choices exist yet, and what is wrong.

    Read from ``nrw.toml`` alone -- nothing here touches the data mount.

    Args:
        root: Project root.
        catalogued_runs: How many runs the catalog holds, which makes an IPTS
            change something to confirm.
    """
    import dataclasses

    from nr_workbench.experiment.config import experiment_config
    from nr_workbench.problems import Problem
    from nr_workbench.project import experiment_schema as project
    from nr_workbench.project.config import ProjectConfigError, load_config
    from nr_workbench.project.settings import read as read_settings
    from nr_workbench.project.tomlfile import TomlEditError

    problems: list[Problem] = []
    current = None
    try:
        current = read_settings(root)
    except (TomlEditError, OSError) as exc:
        problems.append(Problem("config", f"nrw.toml cannot be edited here: {exc}"))
    try:
        config = experiment_config(load_config(root))
    except ProjectConfigError:
        config = experiment_config(None)
    problems.extend(config.problems)
    if catalogued_runs is None:
        problems.append(
            Problem(
                "catalog",
                "The experiment catalog cannot be read, so a change of IPTS asks "
                "first: nrw cannot tell whether it holds runs.",
            )
        )

    chosen = current.experiment if current else {}
    values: dict[str, Any] = {
        "ipts": current.ipts if current else "",
        "label": current.label if current else "",
    }
    defaults: dict[str, Any] = {}
    for table, keys in project.EXPERIMENT_KEYS.items():
        prefix = table.split(".", 1)[1]
        for key, default in keys.items():
            values[f"{prefix}.{key}"] = chosen.get(table, {}).get(key)
            defaults[f"{prefix}.{key}"] = default
    return {
        "schema": "nrw-settings/1",
        "revision": current.revision if current else None,
        "editable": current is not None,
        "values": values,
        "defaults": defaults,
        "effective": {
            "ipts": config.ipts,
            "location": config.source.location,
            "path": str(config.source.path) if config.source.path else None,
            "needs_setup": config.needs_setup,
            "catalog_kind": config.catalog_kind,
        },
        "options": {
            "source": [dataclasses.asdict(o) for o in project.SOURCE_OPTIONS],
            "feed": [dataclasses.asdict(o) for o in project.FEED_OPTIONS],
            "catalog": [dataclasses.asdict(o) for o in project.CATALOG_OPTIONS],
        },
        "ranges": {
            "source.settle_seconds": list(project.SETTLE_RANGE),
            "feed.poll_seconds": list(project.POLL_RANGE),
        },
        "suggested_ipts": ipts_in_path(Path(root)),
        "catalogued_runs": catalogued_runs,
        "problems": [p.as_dict() for p in problems],
    }


def ipts_in_path(root: Path) -> str | None:
    """An IPTS the project's own path names, e.g. /SNS/REF_L/IPTS-34347/shared/x."""
    from nr_workbench.project.experiment_schema import normalize_ipts

    for part in Path(root).absolute().parts:
        if part.upper().startswith("IPTS-"):
            found = normalize_ipts(part)
            if found:
                return found
    return None
