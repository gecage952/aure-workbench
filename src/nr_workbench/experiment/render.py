"""Planning a sample's files -- from the catalog when the catalog manages it.

:func:`plan_sample` is the **only** way this package plans a sample's files, and
``nrw sample new`` and ``nrw init --sample`` go through it too. That is the
point of it. Three callers used to render the blank template, and any one of
them run after the catalog had written ``sample.md`` would have reset it:
the lock records the catalog's content as "what nrw installed", so an
untouched file reads as due for a template upgrade. The scaffold's ``owner``
rule now refuses that structurally; this makes the three callers not ask.

Nothing here writes. It plans, and the scaffold's three-way rule decides what
may be written -- so a ``sample.md`` edited by hand is never overwritten, only
joined by a ``sample.md.nrw-new``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from pathlib import Path

from nr_workbench.experiment.model import Catalog, RunEntry
from nr_workbench.project.layout import ProjectLayout
from nr_workbench.project.render import MeasurementRow, RenderContext, SampleProse
from nr_workbench.project.samples import plan_sample_files
from nr_workbench.project.scaffold import PlannedFile, load_lock
from nr_workbench.sample_md import RUN_NOTE_INDENT, RUN_NOTE_PREFIX

#: The lock's ``owner`` for a sample.md rendered from the catalog.
CATALOG_OWNER = "experiment"

#: How each mounting answer is written into *Measurement conditions*. The words
#: follow the template's own guidance, which ``nrw model new --from-notes`` and
#: the assistants read; ``unknown`` writes nothing, because "mounted once" is a
#: claim, and it decides whether alignment is fitted once or once per state.
MOUNTING_SENTENCES = {
    "once": (
        "Mounted once and not moved between measurements, so one alignment "
        "for all of them."
    ),
    "remounted": (
        "Remounted or realigned between measurements, so each measurement "
        "has its own alignment."
    ),
}


class SampleRenderError(Exception):
    """A sample's files cannot be planned safely. The message says why."""


def sample_md_relpath(sample_id: str) -> str:
    """Where a sample's ``sample.md`` lives, relative to the project root."""
    return f"samples/{sample_id}/sample.md"


def prose_for(catalog: Catalog, sample_id: str) -> SampleProse:
    """What the catalog writes into one sample's ``sample.md``.

    Only included runs are tabulated, one row each, in run order, a type never
    set shown as steady. Each included run's notes follow the sample's own
    *Measurement conditions*, one entry per run (see :func:`run_notes`). Run
    titles are never written: a title beside the condition would blind ``nrw
    data reconcile``'s check that the two agree.
    """
    context = catalog.context_for(sample_id)
    included = [entry for entry in catalog.runs_for(sample_id) if entry.include]
    rows = tuple(
        MeasurementRow(entry.key.run, entry.measurement_type, entry.condition)
        for entry in included
    )
    parts = [
        MOUNTING_SENTENCES.get(context.mounting, ""),
        context.measurement_conditions,
        run_notes(included),
    ]
    conditions = "\n\n".join(part for part in parts if part)
    return SampleProse(
        managed=True,
        description=context.description,
        details=context.details,
        measurement_conditions=conditions,
        fits_to_perform=context.fits_to_perform,
        measurements=rows,
    )


def run_notes(entries: Iterable[RunEntry]) -> str:
    """The notes on each measurement, one entry per run, in the given order.

    ``- Run 218386: realigned after mounting``. A note of several lines
    continues indented, so it stays with its run for a reader, and for
    :func:`~nr_workbench.experiment.adopt.parse_sample_md` reading it back.
    """
    lines: list[str] = []
    for entry in entries:
        if not entry.note:
            continue
        first, *rest = entry.note.split("\n")
        lines.append(RUN_NOTE_PREFIX.format(run=entry.key.run) + first)
        lines.extend(RUN_NOTE_INDENT + line if line else "" for line in rest)
    return "\n".join(lines)


def lock_owner(root: Path, sample_id: str) -> str | None:
    """Who the scaffold lock says wrote this sample's ``sample.md``, if anyone."""
    entry = load_lock(ProjectLayout(root=Path(root)).scaffold_lock).get(
        sample_md_relpath(sample_id), {}
    )
    owner = entry.get("owner") if isinstance(entry, dict) else None
    return owner if isinstance(owner, str) else None


def load_catalog(root: Path) -> Catalog:
    """The project's catalog, for planning. Empty when there is none.

    Raises:
        SampleRenderError: A catalog exists and cannot be read. Planning the
            blank template instead would be guessing that the catalog does not
            manage this sample -- and guessing wrong resets its sample.md.
    """
    from nr_workbench.experiment.config import experiment_config
    from nr_workbench.experiment.store import CatalogError, open_store
    from nr_workbench.project.config import ProjectConfigError, load_config

    try:
        kind = experiment_config(load_config(Path(root))).catalog_kind
    except ProjectConfigError:
        kind = "parquet"
    try:
        store = open_store(Path(root), kind)
        if not store.exists():
            return Catalog()
        return store.load()
    except CatalogError as exc:
        raise SampleRenderError(
            "The experiment catalog cannot be read, so nrw cannot tell whether "
            f"it manages this sample, and will not guess: {exc}"
        ) from exc


def plan_sample(
    root: Path,
    context: RenderContext,
    sample_id: str,
    *,
    title: str | None = None,
    catalog: Catalog | None = None,
) -> list[PlannedFile]:
    """Plan every file of one sample directory.

    Args:
        root: Project root, to find the catalog and the lock.
        context: The project's render context.
        sample_id: The sample.
        title: A title requested by the caller (``nrw sample new --title``).
        catalog: The catalog, if the caller has loaded it already.

    Returns:
        Planned files. ``sample.md`` comes from the catalog and is marked as
        the catalog's when the catalog manages the sample; otherwise the
        scaffold is exactly what ``nrw sample new`` has always written.

    Raises:
        SampleRenderError: The catalog cannot be read; or the lock says the
            catalog wrote this sample.md but the catalog no longer has the
            sample; or ``title`` contradicts the catalog's.
    """
    if catalog is None:
        catalog = load_catalog(root)

    if not catalog.manages(sample_id):
        if lock_owner(root, sample_id) == CATALOG_OWNER:
            raise SampleRenderError(
                f"samples/{sample_id}/sample.md was written from the experiment "
                f"catalog, but the catalog no longer has {sample_id}. Rendering "
                "the blank template would replace it. Add the sample back on "
                "the experiment page, or keep the file as your own with "
                f"`nrw experiment release {sample_id}`."
            )
        return plan_sample_files(context, sample_id, title=title)

    catalog_title = catalog.context_for(sample_id).title or sample_id
    if title and title != catalog_title:
        raise SampleRenderError(
            f"The experiment catalog titles {sample_id} {catalog_title!r}. The "
            "catalog owns this sample's sample.md, so change the title on the "
            "experiment page rather than with --title."
        )
    rendered = dataclasses.replace(context, prose=prose_for(catalog, sample_id))
    target = sample_md_relpath(sample_id)
    return [
        dataclasses.replace(planned, owner=CATALOG_OWNER)
        if planned.relpath == target
        else planned
        for planned in plan_sample_files(rendered, sample_id, title=catalog_title)
    ]


def sample_md_pending(root: Path, sample_id: str) -> bool:
    """Whether the catalog would write this sample's ``sample.md`` differently.

    Edits saved on the Experiment page reach the file only when applied, and
    what reads the file -- AuRE, the assistant -- reads it as it is. False for a
    sample the catalog does not manage, and when the catalog cannot be read.

    Args:
        root: Project root.
        sample_id: The sample.

    Returns:
        Whether the file is missing, or differs from the catalog's rendering.
    """
    try:
        catalog = load_catalog(root)
        if not catalog.manages(sample_id):
            return False
        relpath = sample_md_relpath(sample_id)
        planned = next(
            p
            for p in plan_sample(
                root, project_context(root), sample_id, catalog=catalog
            )
            if p.relpath == relpath
        )
    except SampleRenderError:
        return False
    path = Path(root) / relpath
    return not path.is_file() or path.read_bytes() != planned.content


def project_context(root: Path) -> RenderContext:
    """The render context ``nrw sample new`` would use for this project.

    Raises:
        SampleRenderError: If ``nrw.toml`` cannot be read.
    """
    from nr_workbench.project.config import ProjectConfigError, load_config

    try:
        config = load_config(Path(root))
    except ProjectConfigError as exc:
        raise SampleRenderError(str(exc)) from exc
    return RenderContext(
        project_name=config.name,
        facility=config.facility,
        instrument=config.instrument,
        beamtime=config.beamtime,
        ipts=config.ipts,
        harnesses=config.harnesses,
    )
