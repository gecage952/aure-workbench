"""Apply: project the catalog into ``samples/<id>/``, where the workflow reads it.

Nothing downstream knows the catalog exists. ``nrw model new``, ``nrw data
reconcile``, ``nrw agent watch`` and the rest read ``samples/<id>/data/`` and
``sample.md`` exactly as before; apply is the one step that writes them from
what the experimenter decided. It is explicit -- planned, shown, then done --
because it writes into a directory people and agents are working in.

The rules, each of which exists because the obvious version fails quietly:

**Only complete runs are copied.** A run that has merely stopped changing may
be a third of a measurement (see :mod:`~nr_workbench.experiment.status`). An
*unconfirmed* run is copied only when a person confirms it; a quarantined,
arriving or clock-skewed one never.

**A run is copied whole or not at all.** Its files are staged under names no
reader looks for, checked, and only then renamed into ``data/steady`` in one
burst. They get a fresh modification time, not the source's: a copy dated
hours ago would look settled to ``nrw agent watch`` the moment it landed.

**Nothing is overwritten.** ``data/sources.json`` records the digest of every
file nrw copied, so a later apply can tell "the facility re-reduced it" from
"someone edited the copy" from "someone else put a different file here" -- and
reports all three rather than choosing.

**Exclusion is real.** A copied run that is now excluded, or assigned to
another sample, is *moved* to ``data/excluded/<run>/``: reversibly, and out of
every reader's way, rather than left for each one to remember a flag. Files
nrw did not copy are reported and never moved.

**sample.md goes through the scaffold's three-way rule.** A hand-edited one is
kept and the catalog's version is written beside it as ``sample.md.nrw-new``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import secrets
import shutil
import threading
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from nr_workbench.experiment.inventory import SourceFile, SourceRun
from nr_workbench.experiment.model import Catalog, RunKey
from nr_workbench.experiment.render import (
    SampleRenderError,
    plan_sample,
    sample_md_relpath,
)
from nr_workbench.experiment.status import RunStatus
from nr_workbench.fsutil import advisory_lock, atomic_write_bytes
from nr_workbench.instrument.reduced import reduced_table
from nr_workbench.problems import Problem
from nr_workbench.project.layout import ProjectLayout
from nr_workbench.project.render import RenderContext
from nr_workbench.project.scaffold import (
    Outcome,
    PlannedFile,
    apply_scaffold,
    classify,
    load_lock,
    lock_problem,
)
from nr_workbench.provenance.hashing import sha256_file_or_none

#: The record of what nrw copied into a sample, beside the data.
SOURCES_FILE = "sources.json"
SOURCES_SCHEMA = "nrw-sources/1"

#: Where a sample's steady-state data is, under ``data/`` -- what every reader
#: of the sample looks at.
STEADY_DIR = "steady"

#: Where excluded and reassigned runs are moved, under ``data/``.
EXCLUDED_DIR = "excluded"

#: One apply at a time in this process; see :class:`ApplyBusy`.
_APPLY_LOCK = threading.Lock()


class ApplyError(Exception):
    """Apply cannot go ahead. The message says why."""


class PlanChanged(ApplyError):
    """What apply would do is no longer what was reviewed."""


class ApplyBusy(ApplyError):
    """Another apply is running."""


class ApplyRefused(ApplyError):
    """Something must be fixed by a person before anything is written."""


class Action(StrEnum):
    """What apply does, or declines to do, with one file."""

    COPY = "copy"
    RESTORE = "restore"
    UNCHANGED = "unchanged"
    RECORD = "record"
    MOVE_OUT = "move-out"
    DEFERRED = "deferred"
    SOURCE_CHANGED = "source-changed"
    LOCAL_EDITED = "local-edited"
    CONFLICT = "conflict"
    SOURCE_MISSING = "source-missing"
    NOT_MANAGED = "not-managed"


#: Actions that change the working tree.
WRITING_ACTIONS = frozenset(
    {Action.COPY, Action.RESTORE, Action.RECORD, Action.MOVE_OUT}
)

#: Actions that mean a person should look.
ATTENTION_ACTIONS = frozenset(
    {Action.SOURCE_CHANGED, Action.LOCAL_EDITED, Action.CONFLICT, Action.SOURCE_MISSING}
)

#: The scaffold's verdicts on ``sample.md`` that mean the catalog cannot reach
#: it: edited by hand since nrw wrote it, or never written by nrw at all.
UNREACHABLE_SAMPLE_MD = frozenset({Outcome.DRIFTED, Outcome.UNTRACKED})


@dataclass(frozen=True)
class FileAction:
    """One file's part in an apply.

    Attributes:
        run: The run the file belongs to.
        name: The file's name.
        action: What happens to it.
        detail: Why, in a sentence.
        source: The source's listing of it, when there is one.
        sha256: The digest of the bytes involved, when known.
    """

    run: int
    name: str
    action: Action
    detail: str = ""
    source: SourceFile | None = None
    sha256: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON form."""
        return {
            "run": self.run,
            "name": self.name,
            "action": str(self.action),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SamplePlan:
    """What apply would do for one sample.

    Attributes:
        sample_id: The sample.
        creates: Whether ``samples/<id>/`` is created.
        files: One entry per data file concerned.
        sample_md: The scaffold's verdict on ``sample.md``.
        sample_md_detail: What that verdict means, for a person.
        scaffold: The files handed to the scaffold.
        problems: Why nothing is done for this sample, if anything.
    """

    sample_id: str
    creates: bool
    files: tuple[FileAction, ...]
    sample_md: Outcome | None
    sample_md_detail: str
    scaffold: tuple[PlannedFile, ...] = ()
    problems: tuple[Problem, ...] = ()

    @property
    def writes(self) -> bool:
        """Whether applying this sample changes anything on disk."""
        return (
            self.creates
            or any(f.action in WRITING_ACTIONS for f in self.files)
            or self.sample_md in (Outcome.CREATE, Outcome.UPGRADE, Outcome.DRIFTED)
        )

    @property
    def needs_attention(self) -> bool:
        """Whether a person must look before this sample is in step.

        Includes a ``sample.md`` the catalog cannot reach. Nothing *failed*
        there, but reporting success would read as "in step" when the file
        still says what it said before.
        """
        return (
            bool(self.problems)
            or any(f.action in ATTENTION_ACTIONS for f in self.files)
            or self.sample_md in UNREACHABLE_SAMPLE_MD
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON form."""
        return {
            "sample": self.sample_id,
            "creates": self.creates,
            "files": [f.as_dict() for f in self.files],
            "sample_md": str(self.sample_md) if self.sample_md else None,
            "sample_md_detail": self.sample_md_detail,
            "writes": self.writes,
            "needs_attention": self.needs_attention,
            "problems": [p.as_dict() for p in self.problems],
        }


@dataclass(frozen=True)
class ApplyPlan:
    """What apply would do, for review.

    Attributes:
        samples: One plan per sample.
        plan_id: Digest of the plan. :func:`apply` refuses to go ahead unless
            what it would do still has this digest, so what was reviewed is
            what is written.
        problems: Why nothing can be done at all, if anything.
    """

    samples: tuple[SamplePlan, ...]
    plan_id: str
    problems: tuple[Problem, ...] = ()

    @property
    def writes(self) -> bool:
        """Whether applying changes anything on disk."""
        return any(sample.writes for sample in self.samples)

    @property
    def needs_attention(self) -> bool:
        """Whether a person must look at anything in this plan."""
        return bool(self.problems) or any(s.needs_attention for s in self.samples)

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON form."""
        return {
            "plan_id": self.plan_id,
            "writes": self.writes,
            "needs_attention": self.needs_attention,
            "samples": [s.as_dict() for s in self.samples],
            "problems": [p.as_dict() for p in self.problems],
        }


@dataclass
class ApplyReport:
    """What apply did.

    Attributes:
        done: Actions carried out.
        failed: Actions that were attempted and did not happen, with why.
        sample_md: The scaffold's outcome per sample.
    """

    done: list[FileAction] = field(default_factory=list)
    failed: list[FileAction] = field(default_factory=list)
    sample_md: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON form."""
        return {
            "done": [f.as_dict() for f in self.done],
            "failed": [f.as_dict() for f in self.failed],
            "sample_md": dict(self.sample_md),
        }


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_apply(
    root: Path,
    catalog: Catalog,
    runs: Mapping[RunKey, SourceRun],
    statuses: Mapping[RunKey, RunStatus],
    source: Any,
    context: RenderContext,
    *,
    samples: Collection[str] | None = None,
    confirmed: Collection[RunKey] = (),
) -> ApplyPlan:
    """Work out what applying the catalog would do. Writes nothing.

    Args:
        root: Project root.
        catalog: The catalog.
        runs: What the source lists, by key.
        statuses: Each listed run's arrival state.
        source: The data source, read only to compare a file whose version
            changed since it was copied.
        context: The project's render context.
        samples: Restrict to these samples; all the catalog manages if omitted.
        confirmed: Unconfirmed runs a person has confirmed may be copied.

    Returns:
        The plan.
    """
    root = Path(root)
    problems: list[Problem] = []
    lock_path = ProjectLayout(root=root).scaffold_lock
    trouble = lock_problem(lock_path)
    if trouble:
        problems.append(Problem("scaffold", trouble))

    chosen = sorted(samples) if samples is not None else catalog.sample_ids()
    lock = load_lock(lock_path) if not trouble else {}
    existing = _existing_sample_dirs(root)
    plans = []
    for sample_id in chosen:
        if not catalog.manages(sample_id):
            problems.append(
                Problem(
                    f"sample:{sample_id}",
                    f"{sample_id} is not in the catalog; there is nothing to apply.",
                )
            )
            continue
        try:
            plans.append(
                _plan_sample(
                    root,
                    catalog,
                    sample_id,
                    runs,
                    statuses,
                    source,
                    context,
                    lock,
                    existing,
                    set(confirmed),
                )
            )
        except ApplyRefused as exc:
            plans.append(
                SamplePlan(
                    sample_id,
                    creates=False,
                    files=(),
                    sample_md=None,
                    sample_md_detail="",
                    problems=(Problem(f"sample:{sample_id}", str(exc)),),
                )
            )
    return ApplyPlan(
        samples=tuple(plans),
        plan_id=_plan_id(plans, catalog, source),
        problems=tuple(problems),
    )


def _existing_sample_dirs(root: Path) -> dict[str, str]:
    """Casefolded name to the real name of every directory under samples/."""
    base = root / "samples"
    if not base.is_dir():
        return {}
    return {p.name.casefold(): p.name for p in base.iterdir() if p.is_dir()}


def _plan_sample(
    root: Path,
    catalog: Catalog,
    sample_id: str,
    runs: Mapping[RunKey, SourceRun],
    statuses: Mapping[RunKey, RunStatus],
    source: Any,
    context: RenderContext,
    lock: Mapping[str, Any],
    existing: Mapping[str, str],
    confirmed: set[RunKey],
) -> SamplePlan:
    clash = existing.get(sample_id.casefold())
    if clash is not None and clash != sample_id:
        return SamplePlan(
            sample_id,
            creates=False,
            files=(),
            sample_md=None,
            sample_md_detail="",
            problems=(
                Problem(
                    f"sample:{sample_id}",
                    f"samples/{clash}/ already exists and differs from {sample_id} "
                    "only in case; on macOS and Windows they are one directory. "
                    "Rename the sample in the catalog to match.",
                ),
            ),
        )

    sample_dir = root / "samples" / sample_id
    creates = not (sample_dir / "sample.md").exists()
    steady = sample_dir / "data" / STEADY_DIR
    record = read_sources(sample_dir)

    wanted = {
        entry.key: entry for entry in catalog.runs_for(sample_id) if entry.include
    }
    actions: list[FileAction] = []

    for key in sorted(wanted):
        actions.extend(
            _plan_run(
                key, runs.get(key), statuses.get(key), steady, record, source, confirmed
            )
        )

    # nrw's own copies of runs that no longer belong here: move them aside.
    for name, entry in sorted(record.items()):
        key = RunKey(entry.run)
        if key in wanted or entry.location == EXCLUDED_DIR:
            continue
        other = catalog.runs.get(key)
        if other is None or other.sample_id is None:
            why = "no longer assigned to this sample"
        elif other.sample_id != sample_id:
            why = f"assigned to {other.sample_id} now"
        else:
            why = "excluded"
        actions.append(
            FileAction(
                key.run,
                name,
                Action.MOVE_OUT,
                f"run {key.run} is {why}; its copy moves to data/{EXCLUDED_DIR}/{key.run}/",
                sha256=entry.sha256,
            )
        )

    # Files nrw did not copy, for runs this sample does not use: say so.
    if steady.is_dir():
        from nr_workbench.instrument.reduced import (
            parse_combined_name,
            parse_segment_name,
        )

        for path in sorted(steady.iterdir()):
            if path.name in record or not path.is_file():
                continue
            segment = parse_segment_name(path.name)
            run = segment.run if segment else parse_combined_name(path.name)
            if run is None or RunKey(run) in wanted:
                continue
            actions.append(
                FileAction(
                    run,
                    path.name,
                    Action.NOT_MANAGED,
                    "not copied by nrw, and its run is not assigned here; left alone",
                )
            )

    try:
        planned = plan_sample(root, context, sample_id, catalog=catalog)
    except SampleRenderError as exc:
        return SamplePlan(
            sample_id,
            creates=False,
            files=tuple(actions),
            sample_md=None,
            sample_md_detail="",
            problems=(Problem(f"sample:{sample_id}", str(exc)),),
        )
    relpath = sample_md_relpath(sample_id)
    scaffold = tuple(
        planned if creates else [p for p in planned if p.relpath == relpath]
    )
    md = next(p for p in planned if p.relpath == relpath)
    outcome = classify(md, root / relpath, lock.get(relpath))
    return SamplePlan(
        sample_id,
        creates=creates,
        files=tuple(actions),
        sample_md=outcome,
        sample_md_detail=_describe_md(outcome),
        scaffold=scaffold,
    )


def _plan_run(
    key: RunKey,
    listed: SourceRun | None,
    status: RunStatus | None,
    steady: Path,
    record: Mapping[str, CopyEntry],
    source: Any,
    confirmed: set[RunKey],
) -> list[FileAction]:
    """What happens to one assigned, included run's files."""
    run = key.run
    recorded = {n: e for n, e in record.items() if e.run == run}
    actions: list[FileAction] = []

    if listed is None:
        for name, entry in sorted(recorded.items()):
            actions.append(_already_copied(run, name, entry, None, steady, source))
        if not recorded:
            actions.append(
                FileAction(
                    run,
                    f"run {run}",
                    Action.SOURCE_MISSING,
                    "assigned here, but the data source does not list it -- not "
                    "reduced yet, or the source cannot be reached",
                )
            )
        return actions

    listed_by_name = {f.name: f for f in listed.files}
    fitting = {f.name for f in listed.fitting_files}
    for name, entry in sorted(recorded.items()):
        if name not in fitting:
            # No longer listed; or an artifact nrw copied before only the
            # files a fit reads were copied -- still nrw's, still checked.
            actions.append(
                _already_copied(
                    run, name, entry, listed_by_name.get(name), steady, source
                )
            )

    may_copy, why_not = _may_copy(key, status, confirmed)
    # What a fit of the sample reads, and nothing else: the segments, or the
    # combined curve when there are none. A combined curve beside segments
    # stays at the source; one nrw copied before this rule is still managed.
    for source_file in listed.fitting_files:
        name = source_file.name
        entry = recorded.get(name)
        if entry is not None:
            actions.append(
                _already_copied(run, name, entry, source_file, steady, source)
            )
            continue
        target = steady / name
        if target.is_symlink():
            actions.append(
                FileAction(
                    run,
                    name,
                    Action.CONFLICT,
                    "a symbolic link with this name is already here (from `nrw "
                    "import`?); left alone",
                )
            )
            continue
        if target.exists():
            actions.append(_hand_copied(run, name, target, source_file, source))
            continue
        if not may_copy:
            actions.append(FileAction(run, name, Action.DEFERRED, why_not, source_file))
            continue
        actions.append(FileAction(run, name, Action.COPY, "", source_file))
    return actions


def _may_copy(
    key: RunKey, status: RunStatus | None, confirmed: set[RunKey]
) -> tuple[bool, str]:
    if status is None:
        return (False, "its state is not known yet")
    if status.complete:
        return (True, "")
    if status.state == "unconfirmed" and key in confirmed:
        return (True, "")
    if status.state == "unconfirmed":
        return (False, f"{status.reason}")
    return (False, f"{status.state}: {status.reason}")


def _already_copied(
    run: int,
    name: str,
    entry: CopyEntry,
    listed: SourceFile | None,
    steady: Path,
    source: Any,
) -> FileAction:
    """A file nrw copied earlier: is it still what nrw copied, and is the source?"""
    if entry.location == EXCLUDED_DIR:
        return FileAction(
            run,
            name,
            Action.RESTORE,
            f"run {run} is included again; its copy moves back from data/{EXCLUDED_DIR}/",
            listed,
            entry.sha256,
        )
    target = steady / name
    if not target.is_file() or target.is_symlink():
        return FileAction(
            run,
            name,
            Action.CONFLICT,
            "nrw copied this file, but it is no longer here as a plain file; "
            "remove its entry from data/sources.json to copy it again",
        )
    local = sha256_file_or_none(target)
    if local != entry.sha256:
        return FileAction(
            run,
            name,
            Action.LOCAL_EDITED,
            "the copy has been edited since nrw copied it; left alone",
        )
    if listed is None or listed.version == entry.source_version:
        return FileAction(run, name, Action.UNCHANGED, "", listed, local)
    # The listing says the source file changed. Only its bytes can say whether
    # it really did: a new inode or ctime alone (another client, a restore) is
    # not a re-reduction.
    try:
        fresh = hashlib.sha256(source.read_bytes(listed)).hexdigest()
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return FileAction(
            run,
            name,
            Action.SOURCE_CHANGED,
            f"the source changed and cannot be read ({exc})",
        )
    if fresh == local:
        return FileAction(
            run, name, Action.RECORD, "same bytes, new source version", listed, local
        )
    return FileAction(
        run,
        name,
        Action.SOURCE_CHANGED,
        "the facility's file was rewritten after it was copied -- probably "
        "re-reduced. The copy is kept; to take the new version, move the copy "
        "away and apply again.",
        listed,
    )


def _hand_copied(
    run: int, name: str, target: Path, listed: SourceFile, source: Any
) -> FileAction:
    """A file with the source's name that nrw did not copy."""
    try:
        fresh = hashlib.sha256(source.read_bytes(listed)).hexdigest()
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return FileAction(
            run,
            name,
            Action.CONFLICT,
            f"already here, and the source cannot be read ({exc})",
        )
    local = sha256_file_or_none(target)
    if local == fresh:
        return FileAction(
            run,
            name,
            Action.RECORD,
            "already here with the same bytes; recorded",
            listed,
            local,
        )
    return FileAction(
        run,
        name,
        Action.CONFLICT,
        "a different file with this name is already here, not copied by nrw; left alone",
    )


def _describe_md(outcome: Outcome) -> str:
    return {
        Outcome.CREATE: "will be created from the catalog",
        Outcome.UNCHANGED: "already matches the catalog",
        Outcome.UPGRADE: "will be updated from the catalog",
        Outcome.DRIFTED: (
            "was edited by hand, so it is kept; the catalog's version will be "
            "written beside it as sample.md.nrw-new"
        ),
        Outcome.UNTRACKED: (
            "exists but was not written by nrw (imported, or copied in), so it "
            "is left alone and the catalog's context does not reach it. Adopt "
            "it to let the catalog manage it."
        ),
    }.get(outcome, str(outcome))


def _plan_id(plans: Iterable[SamplePlan], catalog: Catalog, source: Any) -> str:
    digest = hashlib.sha256()
    # Which data source, too: a plan reviewed against one folder must never be
    # carried out against another after the settings change in between.
    identity = source.describe()
    digest.update(json.dumps(identity, sort_keys=True, default=str).encode())
    for plan in plans:
        digest.update(
            json.dumps(
                {
                    "sample": plan.sample_id,
                    "creates": plan.creates,
                    "md": str(plan.sample_md),
                    # sample.md's bytes -- what the person reviewed -- but only
                    # the *names* of the other scaffold files: a new sample's
                    # sample.yaml is stamped with the current second, so
                    # hashing its bytes made a review and an apply that
                    # straddled a second boundary look like two plans.
                    "md_content": [
                        hashlib.sha256(p.content).hexdigest()
                        for p in plan.scaffold
                        if p.relpath.endswith("/sample.md")
                    ],
                    "scaffold": sorted(p.relpath for p in plan.scaffold),
                    "files": [
                        [
                            f.run,
                            f.name,
                            str(f.action),
                            f.source.version if f.source else None,
                        ]
                        for f in plan.files
                    ],
                    "problems": [p.message for p in plan.problems],
                },
                sort_keys=True,
            ).encode()
        )
    digest.update(
        json.dumps(
            sorted([k.slug(), e.rev] for k, e in catalog.runs.items())
            + sorted([k, e.rev] for k, e in catalog.samples.items())
        ).encode()
    )
    return digest.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def apply(
    root: Path,
    catalog: Catalog,
    runs: Mapping[RunKey, SourceRun],
    statuses: Mapping[RunKey, RunStatus],
    source: Any,
    context: RenderContext,
    *,
    expected_plan_id: str,
    samples: Collection[str] | None = None,
    confirmed: Collection[RunKey] = (),
) -> ApplyReport:
    """Carry out a reviewed plan.

    The plan is worked out again first, from what is on disk now, and applied
    only if its digest matches the one reviewed.

    Raises:
        ApplyBusy: Another apply is running in this process.
        ApplyRefused: The plan has a problem that stops everything.
        PlanChanged: The plan is not the one that was reviewed.
    """
    if not _APPLY_LOCK.acquire(blocking=False):
        raise ApplyBusy(
            "Another apply is already running. Wait for it, then review again."
        )
    try:
        with advisory_lock(
            ProjectLayout(root=Path(root)).experiment_cache_dir / "apply"
        ):
            plan = plan_apply(
                root,
                catalog,
                runs,
                statuses,
                source,
                context,
                samples=samples,
                confirmed=confirmed,
            )
            if plan.problems:
                raise ApplyRefused("; ".join(p.message for p in plan.problems))
            if plan.plan_id != expected_plan_id:
                raise PlanChanged(
                    "What apply would do has changed since it was reviewed -- a "
                    "file arrived, or the catalog was edited. Review it again."
                )
            report = ApplyReport()
            for sample in plan.samples:
                if sample.problems:
                    continue
                _apply_sample(Path(root), sample, source, report)
            return report
    finally:
        _APPLY_LOCK.release()


def _apply_sample(
    root: Path, plan: SamplePlan, source: Any, report: ApplyReport
) -> None:
    sample_dir = root / "samples" / plan.sample_id
    if plan.creates:
        apply_scaffold(root, list(plan.scaffold))
        report.sample_md[plan.sample_id] = str(plan.sample_md)

    steady = sample_dir / "data" / STEADY_DIR
    steady.mkdir(parents=True, exist_ok=True)
    record = read_sources(sample_dir)
    try:
        _copy_runs(steady, plan, source, record, report)
        _move(sample_dir, steady, plan, record, report)
        _record_matches(plan, record, report)
    finally:
        # Whatever happened, the record must describe what is on disk: a copy
        # nrw made but did not record would read as someone else's file.
        write_sources(sample_dir, record)

    if not plan.creates:
        md = apply_scaffold(root, list(plan.scaffold))
        report.sample_md[plan.sample_id] = str(md.files[0].outcome) if md.files else ""


def _record_matches(
    plan: SamplePlan, record: dict[str, CopyEntry], report: ApplyReport
) -> None:
    """Record files found to hold exactly the source's bytes."""
    for action in plan.files:
        if (
            action.action is Action.RECORD
            and action.source is not None
            and action.sha256 is not None
        ):
            fresh = CopyEntry.copied(action.run, action.sha256, action.source)
            earlier = record.get(action.name)
            if earlier is not None:
                # Same bytes under a new source version: nothing was copied,
                # so when it *was* copied stays as it was.
                fresh = dataclasses.replace(
                    fresh,
                    copied_at=earlier.copied_at or fresh.copied_at,
                    extra=earlier.extra,
                )
            record[action.name] = fresh
            report.done.append(action)


def _copy_runs(
    steady: Path,
    plan: SamplePlan,
    source: Any,
    record: dict[str, CopyEntry],
    report: ApplyReport,
) -> None:
    """Stage each run's files, check them, then rename them in together."""
    by_run: dict[int, list[FileAction]] = {}
    for action in plan.files:
        if action.action is Action.COPY:
            by_run.setdefault(action.run, []).append(action)

    staging = steady.parent / f".nrw-staging-{secrets.token_hex(4)}"
    try:
        for run, actions in sorted(by_run.items()):
            staged: list[tuple[FileAction, Path, str]] = []
            failure: str | None = None
            for action in actions:
                assert action.source is not None
                try:
                    data = source.read_bytes(action.source)
                    reduced_table(data)
                except Exception as exc:  # noqa: BLE001 - the run is skipped, and says why
                    failure = f"{action.name}: {exc}"
                    break
                staging.mkdir(parents=True, exist_ok=True)
                part = staging / f"{action.name}.part"
                with part.open("wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                staged.append((action, part, hashlib.sha256(data).hexdigest()))

            if failure is not None:
                for _, part, _ in staged:
                    part.unlink(missing_ok=True)
                for action in actions:
                    report.failed.append(
                        FileAction(
                            run,
                            action.name,
                            action.action,
                            f"run {run} was not copied, none of its files: {failure}",
                        )
                    )
                continue

            placed: list[tuple[FileAction, str]] = []
            try:
                for action, part, digest in staged:
                    _place(part, steady / action.name)
                    placed.append((action, digest))
            except OSError as exc:
                # Whole or not at all: take back what this run already placed.
                # Each was a new file, planned because nothing was there.
                for action, _ in placed:
                    (steady / action.name).unlink(missing_ok=True)
                for action in actions:
                    report.failed.append(
                        FileAction(
                            run,
                            action.name,
                            action.action,
                            f"run {run} was not copied, none of its files: "
                            f"placing {action.name} failed ({exc})",
                        )
                    )
                continue
            for action, digest in placed:
                record[action.name] = CopyEntry.copied(run, digest, action.source)
                report.done.append(action)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _place(source: Path, target: Path) -> None:
    """Move *source* to *target*, refusing if *target* exists by then: a copy
    made by hand between review and apply is never overwritten.

    Raises:
        FileExistsError: *target* exists.
        OSError: Anything else that stops the move.
    """
    if target.exists():
        raise FileExistsError(f"{target.name} already exists")
    os.replace(source, target)


def _move(
    sample_dir: Path,
    steady: Path,
    plan: SamplePlan,
    record: dict[str, CopyEntry],
    report: ApplyReport,
) -> None:
    """Move copies out of, or back into, the data readers look at."""
    for action in plan.files:
        excluded = sample_dir / "data" / EXCLUDED_DIR / str(action.run)
        if action.action is Action.MOVE_OUT:
            origin, target, location = (
                steady / action.name,
                excluded / action.name,
                EXCLUDED_DIR,
            )
        elif action.action is Action.RESTORE:
            origin, target, location = (
                excluded / action.name,
                steady / action.name,
                STEADY_DIR,
            )
        else:
            continue
        if target.exists() or not origin.is_file() or origin.is_symlink():
            report.failed.append(
                FileAction(
                    action.run,
                    action.name,
                    action.action,
                    f"not moved: {target.relative_to(sample_dir)} already exists"
                    if target.exists()
                    else f"not moved: {origin.relative_to(sample_dir)} is missing",
                )
            )
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            _place(origin, target)
        except OSError as exc:
            report.failed.append(
                FileAction(action.run, action.name, action.action, f"not moved: {exc}")
            )
            continue
        record[action.name] = dataclasses.replace(
            record[action.name], location=location
        )
        report.done.append(action)


# ---------------------------------------------------------------------------
# The copy record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CopyEntry:
    """What nrw copied into a sample, for one file.

    Attributes:
        run: The run the file belongs to.
        sha256: Digest of the bytes copied: how a later edit of the copy is
            noticed.
        size: Their size, when the source listed it.
        source_version: The source's identity of those bytes when they were
            copied: how a re-reduction at the facility is noticed.
        copied_at: When, UTC.
        location: :data:`STEADY_DIR` or :data:`EXCLUDED_DIR`, where under
            ``data/`` the copy is now.
        kind: The run's kind; ``steady`` until tNR series are copied.
        extra: Keys this version of nrw does not know, kept so that
            rewriting the record never drops what a newer one wrote.
    """

    run: int
    sha256: str
    size: int | None = None
    source_version: str | None = None
    copied_at: str = ""
    location: str = STEADY_DIR
    kind: str = "steady"
    extra: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def copied(cls, run: int, sha256: str, source: SourceFile | None) -> CopyEntry:
        """The entry for bytes placed in ``data/steady/`` now."""
        return cls(
            run=run,
            sha256=sha256,
            size=source.size if source else None,
            source_version=source.version if source else None,
            copied_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> CopyEntry:
        """Read one entry of ``sources.json``, already checked by the caller."""
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        return cls(
            **{k: data[k] for k in known if k in data},
            extra={k: v for k, v in data.items() if k not in known},
        )

    def as_json(self) -> dict[str, Any]:
        """The entry as ``sources.json`` holds it."""
        return {
            **self.extra,
            "run": self.run,
            "kind": self.kind,
            "sha256": self.sha256,
            "size": self.size,
            "source_version": self.source_version,
            "copied_at": self.copied_at,
            "location": self.location,
        }


def read_sources(sample_dir: Path) -> dict[str, CopyEntry]:
    """What nrw has copied into a sample, by file name. Empty if nothing yet.

    Raises:
        ApplyRefused: The record exists but cannot be read. Treating it as
            empty would make every copy look hand-made, and the next apply
            would stop managing all of them.
    """
    path = Path(sample_dir) / "data" / SOURCES_FILE
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApplyRefused(
            f"{path.relative_to(Path(sample_dir).parent.parent)} cannot be read "
            f"({exc}). Restore it from git before applying."
        ) from exc
    files = document.get("files") if isinstance(document, dict) else None
    if not isinstance(document, dict) or document.get("schema") != SOURCES_SCHEMA:
        raise ApplyRefused(f"{path.name} is not an nrw copy record.")
    if not isinstance(files, dict):
        raise ApplyRefused(f"{path.name} has no file list.")
    record: dict[str, CopyEntry] = {}
    for name, entry in files.items():
        run = entry.get("run") if isinstance(entry, dict) else None
        if (
            isinstance(run, bool)
            or not isinstance(run, int)
            or run <= 0
            or not isinstance(entry.get("sha256"), str)
        ):
            raise ApplyRefused(
                f"{path.name}: the entry for {name!r} is not one nrw wrote. "
                "Restore the file from git before applying."
            )
        try:
            record[str(name)] = CopyEntry.from_json(entry)
        except TypeError as exc:
            raise ApplyRefused(
                f"{path.name}: the entry for {name!r} is not one nrw wrote ({exc}). "
                "Restore the file from git before applying."
            ) from exc
    return record


def write_sources(sample_dir: Path, record: Mapping[str, CopyEntry]) -> None:
    """Write the copy record atomically, only if it changed."""
    path = Path(sample_dir) / "data" / SOURCES_FILE
    document = {
        "schema": SOURCES_SCHEMA,
        "files": {k: record[k].as_json() for k in sorted(record)},
    }
    text = json.dumps(document, indent=2) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    if not record and not path.exists():
        return
    atomic_write_bytes(path, text.encode("utf-8"))
