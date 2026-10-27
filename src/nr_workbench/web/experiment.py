"""Everything the Experiment page knows, with no Flask anywhere.

The same split as :mod:`nr_workbench.web.project`: this class is testable with
nothing but a directory, and :mod:`nr_workbench.web.experiment_api` mirrors it
one-to-one, so an agent can ``curl`` exactly what the page renders.

Two rules shape it:

**No request waits on the data mount.** Listing the source is the background
poller's job (:class:`~nr_workbench.experiment.live.LiveInventory`); requests
read its last snapshot. The two things that must read the source -- a quick
look at a run's curves, and the fresh look apply takes before writing -- run
under a deadline with a cap on how many may wait at once
(:class:`~nr_workbench.bounded.Bounded`), so a dead mount costs those requests
a timeout rather than a thread each, forever.

**Writes are the catalog's and apply's.** This class decides nothing: the
catalog validates edits, apply decides what may be copied. It refuses every
write when the server was started read-only, which the request gate in
:mod:`nr_workbench.web.security` also enforces -- two mechanisms, as with the
agent guard, so that forgetting one is not a hole.
"""

from __future__ import annotations

import dataclasses
import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nr_workbench.bounded import Bounded, Busy, TimedOut
from nr_workbench.experiment.model import (
    MEASUREMENT_TYPES,
    Catalog,
    RunChange,
    RunKey,
    SampleChange,
    validate_sample_id,
)
from nr_workbench.experiment.sources import SOURCE_TIMEOUT
from nr_workbench.experiment.status import STATES
from nr_workbench.experiment.views import (
    run_row,
    sample_card,
    unmanaged_card,
)
from nr_workbench.problems import Problem
from nr_workbench.project.layout import ProjectLayout

#: Reads of the data source that may be waiting at once, per configuration:
#: quick looks and plan reviews, which anyone who can see the pages may ask for.
READ_SLOTS = 4

#: Apply's own reads, apart from those: an apply is started only with the
#: link, one at a time, so nobody who can only view the pages can use them up.
APPLY_SLOTS = 2

#: Most files one run's quick look reads. A run has three or four; a folder
#: anyone on the team can write to could hold a thousand under one run number.
MAX_CURVE_FILES = 12


class _BoundedSource:
    """The data source, with every read of a file's bytes given a deadline.

    Apply and its review read file bytes -- to copy them, and to tell a
    re-reduced source from a touched one. Called on a request thread against
    a hard-mounted NFS path that has gone away, each read would block that
    thread for good, and apply would hold its lock while it did. Wrapping the
    source once, where the page gets it, puts every caller under the rule
    instead of trusting each call site to remember it. A read that times out
    raises, and apply reports it against that file like any other failure.

    Listing is left alone: only the background poller lists, and a poller
    stuck on a dead mount is reported as stuck rather than waited for.
    """

    def __init__(self, source: Any, bounded: Callable[..., Any]) -> None:
        self._source = source
        self._bounded = bounded
        self.kind = getattr(source, "kind", "")

    def describe(self) -> dict[str, Any]:
        return self._source.describe()

    def inventory(self) -> Any:
        return self._source.inventory()

    def read_bytes(self, file: Any, *, max_bytes: int) -> bytes:
        return self._bounded(self._source.read_bytes, file, max_bytes=max_bytes)


class RequestError(ValueError):
    """The request cannot be carried out as sent; the message says what to fix.

    Its own class so the API answers 400 for *this*, and not for any
    ``ValueError`` at all: a ``UnicodeDecodeError`` from a file, or a bug, is
    a ``ValueError`` too, and calling that "your request was wrong" would
    hide it from the log.
    """


class RunNotListedError(LookupError):
    """The data source does not list the run asked about."""


class WritesDisabledError(PermissionError):
    """The server was started without write access. The message says why."""


def require_writable(writable: bool, why_read_only: str) -> None:
    """Refuse a write on a server started read-only.

    Args:
        writable: Whether the server accepts writes.
        why_read_only: What to say when it does not.

    Raises:
        WritesDisabledError: With *why_read_only*, or a plain reason.
    """
    if not writable:
        raise WritesDisabledError(why_read_only or "This server was started read-only.")


#: What a request raises when the data source did not answer in time.
SourceTimeoutError = TimedOut


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


@dataclass(frozen=True)
class _Wiring:
    """Everything that reads the data source, for one version of ``nrw.toml``.

    Attributes:
        stamp: ``nrw.toml``'s inode, size, and modification and change times:
            every nrw write replaces the file, and a hand edit moves at least
            one of them even within one tick of a coarse clock.
        revision: Its sha256, to tell an edit from a touch.
        workspace: Source, feed and catalog store built from it.
        source: The source, every byte read bounded, for anyone's reads.
        live: The background poller over that source.
        reads: The deadline and the slots those reads run under -- this
            wiring's own, so reads stuck on the old folder's dead mount do not
            also take the new folder's.
        applying: Apply's deadline and slots, apart from everyone else's.
    """

    stamp: tuple[int, int, int, int] | None
    revision: str | None
    workspace: Any
    source: _BoundedSource
    live: Any
    reads: Bounded
    applying: Bounded

    def source_for_apply(self) -> _BoundedSource:
        """The source, every byte read under apply's own slots."""
        return _BoundedSource(self.workspace.source, self.applying.run)

    def close(self) -> None:
        """Retire it: its poller stops for good."""
        self.live.close()


class ExperimentData:
    """The experiment of one project, shaped for the page.

    Args:
        root: Project root.
        writable: Whether writes are allowed at all. ``nrw serve`` sets this
            from where it is bound and who is running it.
        why_read_only: The reason, when not writable, for the page to show.
        clock: Wall clock, for tests.
        autostart: Start background polling on first use.
    """

    def __init__(
        self,
        root: Path,
        *,
        writable: bool = True,
        why_read_only: str = "",
        clock: Callable[[], float] = time.time,
        autostart: bool = True,
    ) -> None:
        self.root = Path(root).resolve()
        self.writable = writable
        self.why_read_only = why_read_only
        self._clock = clock
        self._autostart = autostart
        self._wiring: _Wiring | None = None
        self._config_problem: Problem | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Wiring, lazily: `nrw serve` must not touch the data mount at start-up
    # ------------------------------------------------------------------

    def _wired(self) -> _Wiring:
        """The wiring for ``nrw.toml`` as it is now, rebuilt when it changes.

        A stat on every request, and a digest only when the stat moved: a
        save from the Settings page, a hand edit, or ``nrw experiment
        settings`` then takes effect without a restart. An edit that cannot be
        parsed keeps the last good wiring and says so, rather than dropping the
        page to "no data location" while someone is halfway through typing.
        """
        from nr_workbench.project.config import ProjectConfigError, load_config

        config = self.root / "nrw.toml"
        try:
            info = config.stat()
            stamp: tuple[int, int, int, int] | None = (
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
        except OSError:
            stamp = None
        with self._lock:
            current = self._wiring
            if current is not None and current.stamp == stamp:
                return current
            revision = _digest(config)
            if current is not None and revision == current.revision:
                # Touched, or put back as it was: the wiring is what the file
                # says, so any complaint about an unreadable edit is over.
                self._config_problem = None
                self._wiring = dataclasses.replace(current, stamp=stamp)
                return self._wiring
            if current is not None:
                try:
                    load_config(self.root)
                except ProjectConfigError as exc:
                    self._config_problem = Problem(
                        "config",
                        f"{exc} Still using the settings nrw.toml had before.",
                    )
                    self._wiring = dataclasses.replace(current, stamp=stamp)
                    return self._wiring
            self._config_problem = None
            self._wiring = self._build(stamp, revision)
        if current is not None:
            current.close()
        return self._wiring

    def _build(
        self, stamp: tuple[int, int, int, int] | None, revision: str | None
    ) -> _Wiring:
        """Everything that reads the data source, for one configuration.

        Built in one step and handed to a request whole, so no request pairs a
        poller made from one ``nrw.toml`` with a source made from another.
        Building touches nothing on the data mount: the poller starts on the
        first request that asks for it.
        """
        from nr_workbench.experiment.workspace import Workspace

        workspace = Workspace(self.root)
        reads = Bounded(
            slots=READ_SLOTS, timeout=SOURCE_TIMEOUT, name="nrw-source-read"
        )
        return _Wiring(
            stamp=stamp,
            revision=revision,
            workspace=workspace,
            source=_BoundedSource(workspace.source, reads.run),
            live=workspace.live(clock=self._clock, autostart=self._autostart),
            reads=reads,
            applying=Bounded(
                slots=APPLY_SLOTS, timeout=SOURCE_TIMEOUT, name="nrw-apply-read"
            ),
        )

    @property
    def workspace(self) -> Any:
        """The project's experiment workspace, for the current ``nrw.toml``."""
        return self._wired().workspace

    @property
    def source(self) -> Any:
        """The data source, every byte read bounded by :data:`SOURCE_TIMEOUT`."""
        return self._wired().source

    @property
    def live(self) -> Any:
        """The background poller for the current ``nrw.toml``."""
        return self._wired().live

    def reload(self) -> None:
        """Rebuild from ``nrw.toml`` on the next request.

        After a save, which knows the file changed however its timestamps
        read; and for tests and shutdown.
        """
        with self._lock:
            wiring, self._wiring = self._wiring, None
        if wiring is not None:
            wiring.close()

    def catalogued_runs(self) -> int | None:
        """How many runs the catalog holds; ``None`` when it cannot be read."""
        catalog, _, readable = self._catalog(self._wired())
        return len(catalog.runs) if readable else None

    def watching(self) -> str | None:
        """The folder the running server reads now."""
        return self._wired().source.describe().get("path")

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        """Everything the page shows on load.

        Returns:
            The source, feed and catalog, every run (from the source, the feed
            or the catalog), every sample, and the cursor for :meth:`changes`.
        """
        w = self._wired()
        workspace = w.workspace
        changes = w.live.changes(None)
        catalog, catalog_problems, readable = self._catalog(w)
        rows = {view.key: self._row(view.key, view, catalog) for view in changes.runs}
        for key in catalog.runs:
            if key not in rows:
                rows[key] = self._row(key, None, catalog)
        return {
            "schema": "nrw-experiment-page/1",
            "ipts": workspace.config.ipts,
            "needs_setup": workspace.config.needs_setup,
            "source": workspace.source.describe(),
            "feed": workspace.feed.describe(),
            "catalog": {
                **workspace.store.describe(),
                "version": self._catalog_version(w),
                "readable": readable,
            },
            # A catalog that cannot be read must not be edited: saving over it
            # would replace every decision in it with the page's empty view.
            "writable": self.writable and readable,
            "read_only_reason": self.why_read_only,
            "states": list(STATES),
            "measurement_types": list(MEASUREMENT_TYPES),
            "cursor": changes.cursor,
            "scan": _scan(changes),
            "runs": [rows[k] for k in sorted(rows)],
            "samples": self._samples(catalog),
            "problems": [
                p.as_dict()
                for p in (
                    *self._config_problems(),
                    *workspace.problems,
                    *catalog_problems,
                    *changes.problems,
                )
            ],
        }

    def changes(self, since: str | None) -> dict[str, Any]:
        """What changed since the page's cursor.

        Returns:
            Changed runs (all of them after a server restart), removed runs,
            the new cursor, and the catalog's version -- when that differs from
            what the page holds, someone else edited the catalog and the page
            should reload it.
        """
        w = self._wired()
        changes = w.live.changes(since)
        catalog, catalog_problems, _ = self._catalog(w)
        return {
            "cursor": changes.cursor,
            "resync": changes.resync,
            "runs": [self._row(v.key, v, catalog) for v in changes.runs],
            "removed": [k.slug() for k in changes.removed],
            "catalog_version": self._catalog_version(w),
            "scan": _scan(changes),
            "problems": [
                p.as_dict()
                for p in (
                    *self._config_problems(),
                    *catalog_problems,
                    *changes.problems,
                )
            ],
        }

    def curves(self, run: int) -> dict[str, Any]:
        """A run's reflectivity, straight from the data source, for a quick look.

        Raises:
            RunNotListedError: If the source does not list the run.
            SourceTimeoutError: If the source does not answer in time.
        """
        w = self._wired()
        from nr_workbench.experiment.apply import MAX_FILE_BYTES
        from nr_workbench.web.readers import read_reduced_bytes

        key = RunKey.parse(run)
        view = w.live.snapshot().runs.get(key)
        if view is None or view.source is None:
            raise RunNotListedError(f"The data source does not list run {key.run}.")
        curves = []
        problems = []
        source = w.source
        # The curves a fit would use -- the segments, or the combined curve
        # when there are none -- each with its own angle. `thetas` lines up
        # with `files` by position, so they are paired before any is skipped.
        thetas = list(view.source.thetas)
        thetas += [None] * (len(view.source.files) - len(thetas))
        fitting = set(view.source.fitting_files)
        pairs = [
            (source_file, theta)
            for source_file, theta in zip(view.source.files, thetas, strict=False)
            if source_file in fitting
        ]
        if len(pairs) > MAX_CURVE_FILES:
            problems.append(
                Problem(
                    f"curve:{key.run}",
                    f"run {key.run} lists {len(pairs)} files; showing the first "
                    f"{MAX_CURVE_FILES}.",
                ).as_dict()
            )
        for source_file, theta in pairs[:MAX_CURVE_FILES]:
            label = (
                f"{key.run}#{source_file.segment}"
                if source_file.segment is not None
                else f"{key.run} combined"
            )
            try:
                data = source.read_bytes(source_file, max_bytes=MAX_FILE_BYTES)
                curve = read_reduced_bytes(data, label=label, name=source_file.name)
            except (SourceTimeoutError, Busy):
                raise  # the source, not this segment: said once, for the request
            except Exception as exc:  # noqa: BLE001 - one unreadable segment is a finding
                problems.append(Problem(f"curve:{label}", str(exc)).as_dict())
                continue
            payload = curve.as_dict()
            payload["run"] = key.run
            if theta is not None:
                payload["theta"] = theta
            curves.append(payload)
        return {"run": key.run, "curves": curves, "problems": problems}

    def sample_preview(self, sample_id: str) -> dict[str, Any]:
        """The sample.md the catalog would write, and where the file stands.

        Raises:
            CatalogValidationError: If the sample id is not usable.
        """
        w = self._wired()
        from nr_workbench.experiment.render import (
            SampleRenderError,
            plan_sample,
            sample_md_relpath,
        )
        from nr_workbench.project.scaffold import classify, load_lock, render_diff
        from nr_workbench.web.prose import render

        validate_sample_id(sample_id)
        catalog, problems, _ = self._catalog(w)
        relpath = sample_md_relpath(sample_id)
        path = self.root / relpath
        try:
            planned = next(
                p
                for p in plan_sample(
                    self.root,
                    w.workspace.render_context(),
                    sample_id,
                    catalog=catalog,
                )
                if p.relpath == relpath
            )
        except SampleRenderError as exc:
            return {
                "sample": sample_id,
                "problems": [Problem("sample", str(exc)).as_dict()],
            }
        lock = load_lock(ProjectLayout(root=self.root).scaffold_lock)
        outcome = classify(planned, path, lock.get(relpath))
        text = planned.content.decode("utf-8")
        return {
            "sample": sample_id,
            "managed": catalog.manages(sample_id),
            "exists": path.is_file(),
            "state": str(outcome),
            "markdown": text,
            "html": render(text),
            "diff": render_diff(planned, path) if path.is_file() else "",
            "problems": [p.as_dict() for p in problems],
        }

    def apply_plan(
        self, samples: list[str] | None = None, confirmed: list[Any] | None = None
    ) -> dict[str, Any]:
        """What applying would do, from the latest snapshot. Writes nothing."""
        w = self._wired()
        from nr_workbench.experiment.apply import plan_apply

        catalog = self._catalog_or_raise(w)
        runs, statuses = self._observed(w.live.snapshot())
        plan = plan_apply(
            self.root,
            catalog,
            runs,
            statuses,
            w.source,
            w.workspace.render_context(),
            samples=_sample_list(samples),
            confirmed=_run_keys(confirmed),
        )
        return plan.as_dict()

    def adopt_plan(self, sample_id: str) -> dict[str, Any]:
        """What adopting (or pulling) a sample's sample.md would do."""
        w = self._wired()
        from nr_workbench.experiment.adopt import plan_adopt

        validate_sample_id(sample_id)
        plan = plan_adopt(
            self.root,
            self._catalog_or_raise(w),
            sample_id,
            w.workspace.render_context(),
        )
        return plan.as_dict()

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def update_runs(self, changes: Any) -> dict[str, Any]:
        """Record edits to run records.

        Args:
            changes: A list of ``{"run", "base_rev", "fields"}``. The run's
                title and start time are filled in from the source here, not
                taken from the request.

        Raises:
            RequestError: A malformed request (400).
            CatalogValidationError: A rule broken (400).
            RecordConflict: A record changed since the page loaded it (409).
        """
        w = self._wired()
        self._require_writable()
        if not isinstance(changes, list) or not changes:
            raise RequestError("expected a non-empty list of run changes")
        snapshot = w.live.snapshot()
        run_changes = []
        for item in changes:
            if not isinstance(item, dict):
                raise RequestError("each change must be an object")
            key = RunKey.parse(_required(item, "run"))
            fields = item.get("fields")
            if not isinstance(fields, dict) or not fields:
                raise RequestError(
                    f"run {key.run}: 'fields' must be a non-empty object"
                )
            if {"title", "start_time"} & set(fields):
                raise RequestError(
                    "title and start_time come from the data source, not the page"
                )
            view = snapshot.runs.get(key)
            if view is not None and view.source is not None:
                fields = {
                    "title": view.source.title,
                    "start_time": view.source.start_time,
                    **fields,
                }
            # base_rev is checked where the catalog changes, not here as well.
            run_changes.append(RunChange(key, item.get("base_rev", 0), fields))
        catalog = w.workspace.store.update(runs=run_changes)
        return {
            "runs": [
                self._row(c.key, snapshot.runs.get(c.key), catalog) for c in run_changes
            ],
            "samples": self._samples(catalog),
            "catalog_version": self._catalog_version(w),
        }

    def update_sample(self, sample_id: str, payload: Any) -> dict[str, Any]:
        """Record edits to a sample's context, or remove it.

        Raises:
            RequestError: A malformed request (400).
            CatalogValidationError: A rule broken (400).
            RecordConflict: The record changed since the page loaded it (409).
        """
        w = self._wired()
        self._require_writable()
        validate_sample_id(sample_id)
        if not isinstance(payload, dict):
            raise RequestError("expected an object")
        delete = payload.get("delete", False)
        if not isinstance(delete, bool):
            raise RequestError("delete must be true or false")
        if delete and (self.root / "samples" / sample_id).exists():
            raise RequestError(
                f"samples/{sample_id}/ exists, so the catalog keeps managing it. "
                f"Use `nrw experiment release {sample_id}` to make sample.md "
                "yours instead."
            )
        fields = payload.get("fields", {})
        if not isinstance(fields, dict):
            raise RequestError("'fields' must be an object")
        catalog = w.workspace.store.update(
            samples=[
                SampleChange(
                    sample_id, payload.get("base_rev", 0), fields, delete=delete
                )
            ]
        )
        return {
            "samples": self._samples(catalog),
            "catalog_version": self._catalog_version(w),
        }

    def apply(
        self,
        plan_id: Any,
        samples: list[str] | None = None,
        confirmed: list[Any] | None = None,
    ) -> dict[str, Any]:
        """Carry out a reviewed plan, after a fresh look at the source.

        Raises:
            RequestError: A malformed request.
            ApplyError: The plan changed, another apply is running, or
                something must be fixed first (409).
            SourceTimeoutError: The source did not answer in time.
        """
        w = self._wired()
        from nr_workbench.experiment.apply import apply

        self._require_writable()
        if not isinstance(plan_id, str) or not plan_id:
            raise RequestError("plan_id is required: review the plan before applying")
        catalog = self._catalog_or_raise(w)
        # A fresh poll, not the last snapshot: completeness is judged now.
        # Under the deadline, because the poll itself lists the source. The
        # wait for a poll already under way gets half of it, so a poller stuck
        # on a dead mount is what the answer names.
        snapshot = w.applying.run(w.live.scan_fresh, w.applying.timeout / 2)
        runs, statuses = self._observed(snapshot)
        report = apply(
            self.root,
            catalog,
            runs,
            statuses,
            w.source_for_apply(),
            w.workspace.render_context(),
            expected_plan_id=plan_id,
            samples=_sample_list(samples),
            confirmed=_run_keys(confirmed),
        )
        return report.as_dict()

    def adopt(self, sample_id: str, rewrite: Any, plan_id: Any) -> dict[str, Any]:
        """Adopt (or pull) a sample's sample.md into the catalog, as reviewed."""
        w = self._wired()
        from nr_workbench.experiment.adopt import adopt, plan_adopt

        self._require_writable()
        validate_sample_id(sample_id)
        if not isinstance(rewrite, bool):
            raise RequestError("rewrite must be true or false")
        if not isinstance(plan_id, str) or not plan_id:
            raise RequestError("plan_id is required: review the adoption first")
        context = w.workspace.render_context()
        plan = plan_adopt(self.root, self._catalog_or_raise(w), sample_id, context)
        report = adopt(
            self.root,
            w.workspace.store,
            plan,
            context,
            rewrite=rewrite,
            expected_plan_id=plan_id,
        )
        return {
            "catalog_changes": report.catalog_changes,
            "rewritten": report.rewritten,
            "backup": report.backup,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _config_problems(self) -> list[Problem]:
        """The newest ``nrw.toml`` could not be used, if that is so."""
        return [self._config_problem] if self._config_problem else []

    def _require_writable(self) -> None:
        require_writable(self.writable, self.why_read_only)

    def _catalog(self, w: _Wiring) -> tuple[Catalog, list[Problem], bool]:
        """The catalog, its problems, and whether it could be read at all.

        An unreadable catalog comes back empty *for display only*, with the
        third element False; every write path loads it again and refuses.
        """
        from nr_workbench.experiment.store import CatalogError

        store = w.workspace.store
        try:
            catalog, problems = store.load_report()
            return (catalog, list(problems), True)
        except CatalogError as exc:
            return (Catalog(), [Problem("catalog", str(exc))], False)

    def _catalog_or_raise(self, w: _Wiring) -> Catalog:
        return w.workspace.store.load()

    def _catalog_version(self, w: _Wiring) -> str:
        from nr_workbench.experiment.store import CATALOG_FILES

        digest = hashlib.sha256()
        for name in CATALOG_FILES:
            path = w.workspace.store.directory / name
            try:
                stat = path.stat()
            except OSError:
                digest.update(f"{name}:-".encode())
                continue
            digest.update(f"{name}:{stat.st_size}:{stat.st_mtime_ns}".encode())
        return digest.hexdigest()[:16]

    def _observed(self, snapshot: Any) -> tuple[dict, dict]:
        runs = {k: v.source for k, v in snapshot.runs.items() if v.source is not None}
        return (runs, {k: v.status for k, v in snapshot.runs.items()})

    def _row(self, key: RunKey, view: Any, catalog: Catalog) -> dict[str, Any]:
        return run_row(key, view, catalog.runs.get(key))

    def _samples(self, catalog: Catalog) -> list[dict[str, Any]]:
        managed = catalog.sample_ids()
        cards = [sample_card(self.root, catalog, sample_id) for sample_id in managed]
        cards.extend(
            unmanaged_card(sample_id)
            for sample_id in ProjectLayout(root=self.root).list_samples()
            if sample_id not in managed
        )
        return cards


def _scan(changes: Any) -> dict[str, Any]:
    return {
        "scanned_at": changes.scanned_at,
        "age": None if changes.scan_age is None else round(changes.scan_age, 1),
        "scanning": changes.scanning,
        "stuck": changes.stuck,
    }


def _required(item: dict[str, Any], name: str) -> Any:
    if name not in item:
        raise RequestError(f"'{name}' is required")
    return item[name]


def _sample_list(samples: Any) -> list[str] | None:
    if samples in (None, []):
        return None
    if not isinstance(samples, list) or not all(isinstance(s, str) for s in samples):
        raise RequestError("samples must be a list of sample ids")
    return [validate_sample_id(s) for s in samples]


def _run_keys(runs: Any) -> set[RunKey]:
    if runs in (None, []):
        return set()
    if not isinstance(runs, list):
        raise RequestError("confirmed must be a list of run numbers")
    return {RunKey.parse(run) for run in runs}
