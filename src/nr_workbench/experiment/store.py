"""Where the catalog is kept: parquet tables in ``experiment/``.

``runs.parquet`` holds one row per run and ``samples.parquet`` one row per
sample -- the split data-assembler's lakehouse uses, where a measurement row
carries a nullable sample id and samples are their own table.
``experiment.parquet`` holds the experiment's own context, one row, once it
has been written. Each table is replaced atomically, and its metadata records
the schema version it was written with.

Two failure modes decide how this is written, each of them silent if handled
the obvious way:

**A file that exists but will not parse is not an empty catalog.** It raises
:class:`CatalogCorruptError`, which is deliberately *not* a ``ValueError``:
pyarrow's ``ArrowInvalid`` is one, and the house pattern ``except (OSError,
ValueError)`` would turn a damaged catalog into an empty one -- whose next save
would then replace every decision the experimenter made with nothing.

**Two writers.** A browser tab and a terminal can edit at once. Every update is
a read-modify-write under a lock, and checks each edited record's revision, so
a concurrent edit to a *different* record is merged and one to the *same*
record is refused.

pyarrow is imported inside the functions that use it: it costs about a second,
and ``nrw --help`` must stay instant.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from nr_workbench.experiment.model import (
    Catalog,
    ExperimentChange,
    ExperimentContext,
    RunChange,
    RunEntry,
    RunKey,
    SampleChange,
    SampleContext,
    apply_changes,
)
from nr_workbench.fsutil import advisory_lock, atomic_write_bytes

#: Written into every table's metadata.
SCHEMA_ID = "nrw-experiment/1"

#: Bumped when a change would make an older nrw misread the tables. An older
#: nrw refuses a newer catalog rather than dropping what it cannot read.
SCHEMA_VERSION = 1

RUNS_FILE = "runs.parquet"
SAMPLES_FILE = "samples.parquet"
EXPERIMENT_FILE = "experiment.parquet"

#: The catalog's files.
CATALOG_FILES = (RUNS_FILE, SAMPLES_FILE, EXPERIMENT_FILE)

#: The digests of the tables an older nrw writes beside them, and checks. Left
#: describing tables since replaced, it would make that nrw refuse them, so a
#: save removes it.
_OLD_MANIFEST = "catalog.json"

#: Columns this version reads. Anything else is preserved, untouched.
_RUN_COLUMNS = (
    "run",
    "kind",
    "sample_id",
    "measurement",
    "condition",
    "include",
    "note",
    "title",
    "start_time",
    "rev",
    "updated_at",
)
_SAMPLE_COLUMNS = (
    "sample_id",
    "title",
    "description",
    "details",
    "mounting",
    "measurement_conditions",
    "fits_to_perform",
    "rev",
    "updated_at",
)
_EXPERIMENT_COLUMNS = ("summary", "hypotheses", "notes", "rev", "updated_at")


class CatalogError(Exception):
    """The catalog cannot be used as it is. The message says what to do.

    Not a ``ValueError``, so that no ``except ValueError`` written for bad
    *requests* can mistake a damaged catalog for a client error or an empty
    one.
    """


class CatalogCorruptError(CatalogError):
    """A catalog file exists but cannot be read."""


class CatalogVersionError(CatalogError):
    """The catalog was written by a newer nrw."""


class CatalogStore(Protocol):
    """Where the organization is kept.

    The parquet store below is the only one today. A facility service that
    serves the same tables is the one this shape is for: ``update`` sends edits
    rather than a whole catalog, so the service can merge concurrent edits by
    record exactly as the local store does.
    """

    def describe(self) -> dict[str, Any]:
        """What the page and ``nrw experiment status`` say about the store."""
        ...

    def load(self) -> Catalog:
        """The current catalog; empty when none has been written yet."""
        ...

    def update(
        self,
        *,
        runs: Iterable[RunChange] = (),
        samples: Iterable[SampleChange] = (),
        experiment: ExperimentChange | None = None,
        now: str | None = None,
    ) -> Catalog:
        """Apply edits against the latest catalog and store the result."""
        ...


class CatalogUnavailableError(CatalogError):
    """The configured kind of catalog store is not available."""


def open_store(root: Path, kind: str = "parquet") -> ParquetCatalogStore:
    """The catalog store ``[experiment.catalog] kind`` names.

    Raises:
        CatalogUnavailableError: For an unknown kind. Deliberately not a
            fallback to the local parquet store: edits meant for somewhere
            else would be written into the project instead, where nobody else
            would see them.
    """
    if kind == "parquet":
        return ParquetCatalogStore.for_project(root)
    raise CatalogUnavailableError(
        f'[experiment.catalog] kind = {kind!r} is not known. Known: "parquet".'
    )


#: One lock per catalog directory per process, for the web server's threads.
#: The file lock alone does not serialize them: flock is per open file
#: description, and it is a no-op where fcntl is missing.
_THREAD_LOCKS: dict[Path, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(directory: Path) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(directory.resolve(), threading.Lock())


class ParquetCatalogStore:
    """The catalog as ``experiment/*.parquet`` in the project.

    Args:
        directory: The committed catalog directory, ``<root>/experiment``.
        cache_dir: Where temp files and the lock go. Must be gitignored and on
            the same filesystem as ``directory``.
    """

    def __init__(self, directory: Path, cache_dir: Path) -> None:
        self.directory = Path(directory)
        self.cache_dir = Path(cache_dir)

    @classmethod
    def for_project(cls, root: Path) -> ParquetCatalogStore:
        """The store for a project, at its standard locations."""
        from nr_workbench.project.layout import ProjectLayout

        layout = ProjectLayout(root=Path(root))
        return cls(layout.experiment_dir, layout.experiment_cache_dir)

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """Where the catalog is and whether one has been written."""
        return {
            "kind": "parquet",
            "directory": self.directory.name,
            "exists": self.exists(),
        }

    def exists(self) -> bool:
        """Whether any catalog file has been written."""
        return any((self.directory / name).exists() for name in CATALOG_FILES)

    def load(self) -> Catalog:
        """Read the catalog, under the same locks as a save: a save replaces
        its tables one after another, and a read between two of them would see
        some of each.

        Returns:
            The catalog. Empty when nothing has been written yet -- and only
            then.

        Raises:
            CatalogCorruptError: A file exists but cannot be read.
            CatalogVersionError: The catalog was written by a newer nrw.
        """
        with self._reading():
            return self._load_unlocked()

    @contextmanager
    def _reading(self) -> Iterator[None]:
        """The save lock, for a read. Skipped where it cannot be taken.

        A catalog on a read-only mount -- a colleague's project, opened to look
        -- has nowhere to put the lock file. Refusing to read it would be
        worse than the race the lock guards against.
        """
        with _thread_lock(self.directory):
            try:
                context = advisory_lock(self.cache_dir / "catalog")
                context.__enter__()
            except OSError:
                yield
                return
            try:
                yield
            finally:
                context.__exit__(None, None, None)

    def _load_unlocked(self) -> Catalog:
        if not self.exists():
            return Catalog()
        runs = _read_table(self.directory / RUNS_FILE)
        samples = _read_table(self.directory / SAMPLES_FILE)
        experiment = _read_table(self.directory / EXPERIMENT_FILE)
        return Catalog(
            runs={entry.key: entry for entry in map(_run_from_row, runs)},
            samples={
                entry.sample_id: entry for entry in map(_sample_from_row, samples)
            },
            experiment=_experiment_from_rows(experiment),
        )

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def update(
        self,
        *,
        runs: Iterable[RunChange] = (),
        samples: Iterable[SampleChange] = (),
        experiment: ExperimentChange | None = None,
        now: str | None = None,
    ) -> Catalog:
        """Apply edits to the latest catalog, under the lock, and store it.

        Args:
            runs: Edits to run records.
            samples: Edits to sample records.
            experiment: An edit to the experiment's context.
            now: Timestamp for changed records; the current UTC time if omitted.

        Returns:
            The catalog as stored.

        Raises:
            CatalogError: The catalog cannot be used as it is.
            CatalogValidationError: An edit breaks a rule.
            RecordConflict: A record changed since the editor loaded it.
        """
        stamp = now or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        run_changes, sample_changes = list(runs), list(samples)

        with _thread_lock(self.directory), advisory_lock(self.cache_dir / "catalog"):
            current = self._load_unlocked()
            updated = apply_changes(
                current,
                runs=run_changes,
                samples=sample_changes,
                experiment=experiment,
                now=stamp,
            )
            if _same(current, updated):
                return current
            self._write(updated)
            return updated

    def _write(self, catalog: Catalog) -> None:
        """Replace each table, and remove an older nrw's digests of them."""
        tables = {
            RUNS_FILE: _table_bytes(
                "runs",
                _RUN_COLUMNS,
                [_run_row(entry) for key, entry in sorted(catalog.runs.items())],
            ),
            SAMPLES_FILE: _table_bytes(
                "samples",
                _SAMPLE_COLUMNS,
                [_sample_row(catalog.samples[k]) for k in sorted(catalog.samples)],
            ),
        }
        if catalog.experiment.rev:
            # Only once written: a project that never says anything about its
            # experiment keeps exactly the files it had.
            tables[EXPERIMENT_FILE] = _table_bytes(
                "experiment",
                _EXPERIMENT_COLUMNS,
                [_experiment_row(catalog.experiment)],
            )
        self.directory.mkdir(parents=True, exist_ok=True)
        for name, data in tables.items():
            atomic_write_bytes(self.directory / name, data, scratch=self.cache_dir)
        (self.directory / _OLD_MANIFEST).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def _same(before: Catalog, after: Catalog) -> bool:
    """Whether an update changed any record's revision (and so its content)."""
    return (
        {k: e.rev for k, e in before.runs.items()}
        == {k: e.rev for k, e in after.runs.items()}
        and {k: e.rev for k, e in before.samples.items()}
        == {k: e.rev for k, e in after.samples.items()}
        and before.experiment.rev == after.experiment.rev
    )


def _run_row(entry: RunEntry) -> dict[str, Any]:
    return {**entry.extra, **run_fields(entry)}


def run_fields(entry: RunEntry) -> dict[str, Any]:
    """A run's fields nrw keeps, without those it does not know: what a push
    to Tiled writes."""
    return {
        "run": entry.key.run,
        "kind": entry.key.kind,
        "sample_id": entry.sample_id,
        "measurement": entry.measurement,
        "condition": entry.condition,
        "include": entry.include,
        "note": entry.note,
        "title": entry.title,
        "start_time": entry.start_time,
        "rev": entry.rev,
        "updated_at": entry.updated_at,
    }


def _sample_row(entry: SampleContext) -> dict[str, Any]:
    return {**entry.extra, **sample_fields(entry)}


def sample_fields(entry: SampleContext) -> dict[str, Any]:
    """A sample's fields nrw keeps, as :func:`run_fields` a run's."""
    return {
        "sample_id": entry.sample_id,
        "title": entry.title,
        "description": entry.description,
        "details": entry.details,
        "mounting": entry.mounting,
        "measurement_conditions": entry.measurement_conditions,
        "fits_to_perform": entry.fits_to_perform,
        "rev": entry.rev,
        "updated_at": entry.updated_at,
    }


def _run_from_row(row: dict[str, Any]) -> RunEntry:
    try:
        return RunEntry(
            key=RunKey(_int(row, "run"), _text(row, "kind")),
            sample_id=row.get("sample_id") or None,
            measurement=_text(row, "measurement"),
            condition=_text(row, "condition"),
            include=_bool(row, "include"),
            note=_text(row, "note"),
            title=_text(row, "title"),
            start_time=_text(row, "start_time"),
            rev=_int(row, "rev"),
            updated_at=_text(row, "updated_at"),
            extra=_extra(row, _RUN_COLUMNS),
        )
    except (TypeError, ValueError) as exc:
        raise CatalogCorruptError(
            f"{RUNS_FILE}: a row cannot be read ({exc})."
        ) from exc


def _sample_from_row(row: dict[str, Any]) -> SampleContext:
    try:
        return SampleContext(
            sample_id=_text(row, "sample_id"),
            title=_text(row, "title"),
            description=_text(row, "description"),
            details=_text(row, "details"),
            mounting=_text(row, "mounting") or "unknown",
            measurement_conditions=_text(row, "measurement_conditions"),
            fits_to_perform=_text(row, "fits_to_perform"),
            rev=_int(row, "rev"),
            updated_at=_text(row, "updated_at"),
            extra=_extra(row, _SAMPLE_COLUMNS),
        )
    except (TypeError, ValueError) as exc:
        raise CatalogCorruptError(
            f"{SAMPLES_FILE}: a row cannot be read ({exc})."
        ) from exc


def _experiment_row(entry: ExperimentContext) -> dict[str, Any]:
    return {
        **entry.extra,
        "summary": entry.summary,
        "hypotheses": entry.hypotheses,
        "notes": entry.notes,
        "rev": entry.rev,
        "updated_at": entry.updated_at,
    }


def _experiment_from_rows(rows: list[dict[str, Any]]) -> ExperimentContext:
    """The experiment's context from its table: empty when there is none."""
    if not rows:
        return ExperimentContext()
    if len(rows) > 1:
        raise CatalogCorruptError(
            f"{EXPERIMENT_FILE} holds {len(rows)} rows; an experiment has one."
        )
    (row,) = rows
    try:
        return ExperimentContext(
            summary=_text(row, "summary"),
            hypotheses=_text(row, "hypotheses"),
            notes=_text(row, "notes"),
            rev=_int(row, "rev"),
            updated_at=_text(row, "updated_at"),
            extra=_extra(row, _EXPERIMENT_COLUMNS),
        )
    except (TypeError, ValueError) as exc:
        raise CatalogCorruptError(
            f"{EXPERIMENT_FILE}: its row cannot be read ({exc})."
        ) from exc


def _int(row: dict[str, Any], name: str) -> int:
    value = row.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} is {value!r}, not a whole number")
    return value


def _bool(row: dict[str, Any], name: str) -> bool:
    value = row.get(name)
    if not isinstance(value, bool):
        raise TypeError(f"{name} is {value!r}, not true or false")
    return value


def _text(row: dict[str, Any], name: str) -> str:
    value = row.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{name} is {value!r}, not text")
    return value


def _extra(row: dict[str, Any], known: tuple[str, ...]) -> dict[str, Any]:
    # Nulls kept: a column a newer nrw wrote, null in every row, must still be
    # a column after an older nrw saves.
    return {k: v for k, v in row.items() if k not in known}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def _read_table(path: Path) -> list[dict[str, Any]]:
    """Read one table as rows, or refuse loudly."""
    if not path.exists():
        return []
    try:
        import pyarrow.parquet as pq

        table = pq.read_table(path)
    except Exception as exc:  # noqa: BLE001 - ArrowInvalid, OSError, and kin
        raise CatalogCorruptError(
            f"{path.name} exists but cannot be read ({type(exc).__name__}: {exc}). "
            "It is not being treated as empty, because the next save would then "
            "replace every decision recorded in it. Restore it from git "
            f"(`git checkout -- {path.parent.name}/{path.name}`)."
        ) from exc
    version = (table.schema.metadata or {}).get(b"nrw.schema_version", b"1")
    if version.isdigit() and int(version) > SCHEMA_VERSION:
        raise CatalogVersionError(
            f"The catalog was written by a newer nr-workbench (schema version "
            f"{int(version)}; this one reads {SCHEMA_VERSION}). Upgrade before "
            "editing it -- an older version would drop what it cannot read."
        )
    return table.to_pylist()


def _table_bytes(
    name: str, columns: tuple[str, ...], rows: list[dict[str, Any]]
) -> bytes:
    """Serialize rows to parquet, known columns first, extras preserved."""
    import io

    import pyarrow as pa
    import pyarrow.parquet as pq

    extras = sorted({k for row in rows for k in row} - set(columns))
    schema_fields = {
        "run": pa.int64(),
        "rev": pa.int64(),
        "include": pa.bool_(),
    }
    arrays = []
    names = []
    for column in (*columns, *extras):
        values = [row.get(column) for row in rows]
        kind = schema_fields.get(column, pa.string() if column in columns else None)
        arrays.append(pa.array(values, type=kind))
        names.append(column)
    table = pa.Table.from_arrays(arrays, names=names).replace_schema_metadata(
        {
            b"nrw.schema": SCHEMA_ID.encode(),
            b"nrw.table": name.encode(),
            b"nrw.schema_version": str(SCHEMA_VERSION).encode(),
        }
    )
    sink = io.BytesIO()
    pq.write_table(table, sink)
    return sink.getvalue()
