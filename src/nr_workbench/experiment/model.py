"""The catalog's vocabulary, and every rule a person's input must pass.

Pure data: no pyarrow, no filesystem, no web framework. Everything a person can
type into the experiment page is checked here, once, before it can reach a
parquet file or a ``sample.md`` -- because ``sample.md`` is read by about
twenty modules that each parse it their own way, and text that one of them
misreads is a wrong fit rather than an error:

* A heading line inside a description (``## Fits to perform``) starts a new
  section for every reader that splits on headings -- including the one that
  hands an unattended agent its task.
* ``<!--`` swallows everything up to the template's next ``-->`` for every
  reader that strips comments before looking for sections.
* A ``|`` in a table cell, or a line separator Python's ``splitlines`` honours,
  shifts or drops a row of the measurement table that ISAAC export and
  ``nrw data reconcile`` read conditions from.

So those are rejected with the reason, never silently rewritten: a person
whose text was changed behind their back cannot tell what the file now says.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Container, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

#: A steady-state measurement: a set of angle segments reduced from one run.
STEADY = "steady"

#: The kinds of measurement the catalog knows. Time-resolved (``series``) is
#: reserved: the unsliced tNR run arrives as an ordinary steady-state file and
#: is treated as one until sliced series are supported.
KINDS = (STEADY,)

#: What a measurement was, as the *Type* column of ``sample.md`` says it: the
#: choices the page offers first. Any other type a person adds is kept too --
#: the types in use are the list. A run whose type was never set is steady.
MEASUREMENT_TYPES = ("steady", "tNR")
DEFAULT_MEASUREMENT = "steady"

#: Whether the sample was moved between measurements. Not derivable from the
#: data, and it decides whether alignment is one parameter or one per state:
#: theta_offset's scope is a claim about remounting, not about change.
MOUNTINGS = ("unknown", "once", "remounted")

#: Sample ids become directory names, script tokens and URL segments. The
#: intersection of what every one of those accepts, plus a length cap.
_SAMPLE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", re.ASCII)

#: Characters that end a line for *some* reader. ``str.splitlines`` honours all
#: of these; markdown honours only ``\n`` and ``\r``. A cell containing U+2028
#: is one row to the browser and two to ``conditions.from_table``.
_LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")

#: An ATX heading, as CommonMark reads one.
_HEADING_RE = re.compile(r"^ {0,3}#{1,6}(?:[ \t]|$)", re.MULTILINE)


class CatalogValidationError(ValueError):
    """Raised when a value cannot go into the catalog. The message says why."""


class RecordConflict(Exception):
    """Raised when an edit was based on a record that has since changed.

    Deliberately not a ``ValueError``: the web layer maps those to 400 (the
    request was wrong), and a conflict is not the requester's mistake -- it is
    someone else's edit arriving first.

    Attributes:
        records: Identifiers of the records that changed underneath the edit.
    """

    def __init__(self, records: list[str]) -> None:
        self.records = records
        super().__init__(
            f"{', '.join(records)} changed since you loaded it. Reload to see "
            "the current version, then make your edit again."
        )


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class RunKey:
    """Which measurement a record is about.

    ``kind`` is part of the identity because one run number can be both a
    steady-state measurement and a time-resolved series -- which today only
    ``nrw agent watch`` sees, and which the catalog will need when series are
    supported.

    Attributes:
        run: The run number.
        kind: One of :data:`KINDS`.
    """

    run: int
    kind: str = STEADY

    def __post_init__(self) -> None:
        # `bool` subclasses `int`, and `True` is run number 1 to `int()`.
        if isinstance(self.run, bool) or not isinstance(self.run, int):
            raise CatalogValidationError(
                f"a run number must be an integer, not {self.run!r}"
            )
        if self.run <= 0:
            raise CatalogValidationError(f"{self.run} is not a run number")
        if self.kind not in KINDS:
            raise CatalogValidationError(
                f"unknown measurement kind {self.kind!r}; known: {', '.join(KINDS)}"
            )

    def slug(self) -> str:
        """The key as text, e.g. ``218386:steady``."""
        return f"{self.run}:{self.kind}"

    @classmethod
    def parse(cls, text: str | int) -> RunKey:
        """Read ``218386`` or ``218386:steady``.

        Raises:
            CatalogValidationError: If it is neither.
        """
        if isinstance(text, int) and not isinstance(text, bool):
            return cls(text)
        raw = str(text).strip()
        run_text, _, kind = raw.partition(":")
        if not run_text.isascii() or not run_text.isdigit():
            raise CatalogValidationError(f"{raw!r} is not a run number")
        return cls(int(run_text), kind or STEADY)


def validate_sample_id(sample_id: Any) -> str:
    """Check an identifier for a sample the catalog may create.

    Stricter than :func:`nr_workbench.project.samples.validate_sample_id`,
    which also accepts a leading hyphen (read as an option by every command
    line it is typed into) and names of any length. Existing samples on disk
    keep working; only the ids the catalog introduces must pass this.

    Returns:
        The id, unchanged.

    Raises:
        CatalogValidationError: With the reason.
    """
    if not isinstance(sample_id, str) or not _SAMPLE_ID_RE.fullmatch(sample_id):
        raise CatalogValidationError(
            f"{sample_id!r} is not a usable sample id: start with a letter or "
            "digit, then use letters, digits, '-' or '_', at most 64 characters."
        )
    return sample_id


# ---------------------------------------------------------------------------
# Text rules
# ---------------------------------------------------------------------------


def _invisible_trouble(value: str, *, allow: Container[str]) -> str | None:
    """Name the first line break not in *allow*, or lone surrogate, in *value*."""
    for char in value:
        if char in allow:
            continue
        if char in _LINE_BREAKS:
            return f"a line break ({unicodedata.name(char, repr(char))})"
        if unicodedata.category(char) == "Cs":
            # Half of a surrogate pair: JSON carries it, no encoding stores
            # it, and left in it would fail the whole save at the parquet.
            return f"a lone surrogate ({char!r}), which no encoding can store"
    return None


def _check_comment_markers(label: str, value: str) -> None:
    for marker in ("<!--", "-->"):
        if marker in value:
            raise CatalogValidationError(
                f"{label} contains {marker!r}. sample.md keeps its guidance in "
                "HTML comments, and every reader that strips them would take "
                "this as the start or end of one -- hiding the text between "
                "here and the next comment."
            )


def _text(label: str, value: Any) -> str:
    """*value* with Windows line endings normalized and the ends trimmed."""
    if not isinstance(value, str):
        raise CatalogValidationError(f"{label} must be text, not {value!r}")
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def clean_line(label: str, value: Any, *, allow_empty: bool = True) -> str:
    """Validate a single-line value: a title, a type, a condition.

    Args:
        label: The field's name, for the message.
        value: The proposed value.
        allow_empty: Whether ``""`` is acceptable.

    Returns:
        The value with surrounding whitespace removed.

    Raises:
        CatalogValidationError: With the reason.
    """
    if not isinstance(value, str):
        raise CatalogValidationError(f"{label} must be text, not {value!r}")
    text = value.strip()
    if not text and not allow_empty:
        raise CatalogValidationError(f"{label} must not be empty")
    trouble = _invisible_trouble(text, allow="")
    if trouble:
        raise CatalogValidationError(f"{label} contains {trouble}; it must be one line")
    if "|" in text:
        raise CatalogValidationError(
            f"{label} contains '|', which the measurement table in sample.md "
            "uses to separate columns; every reader would split the cell there."
        )
    _check_comment_markers(label, text)
    return text


def clean_prose(label: str, value: Any, *, headings: bool = True) -> str:
    """Validate a multi-line value written into ``sample.md``: a description,
    the fits to perform, a run's notes.

    Args:
        label: The field's name, for the message.
        value: The proposed value.
        headings: Whether a heading line is refused. Not for a run's notes:
            sample.md writes them after ``- Run <n>:``, every later line
            indented, so none of their lines reads as a heading to a reader
            that splits the file into sections.

    Returns:
        The text with Windows line endings normalized and the ends trimmed.
        Line endings are the one thing changed silently: a browser may send
        either, and they mean the same thing to every reader.

    Raises:
        CatalogValidationError: With the reason.
    """
    text = _text(label, value)
    trouble = _invisible_trouble(text, allow="\n")
    if trouble:
        raise CatalogValidationError(f"{label} contains {trouble}")
    _check_comment_markers(label, text)
    heading = _HEADING_RE.search(text) if headings else None
    if heading:
        line = text[heading.start() :].split("\n", 1)[0]
        raise CatalogValidationError(
            f"{label} contains a heading line ({line.strip()!r}). In sample.md a "
            "heading starts a new section, so this would read as a section of "
            "its own -- a '## Fits to perform' here would become the agent's "
            "task. Use **bold** for emphasis instead."
        )
    _check_tables(label, text)
    return text


def _check_tables(label: str, text: str) -> None:
    from nr_workbench.sample_md import RUN_HEADERS, table_cells

    for line in text.split("\n"):
        cells = table_cells(line)
        if cells is None:
            continue
        # Every header any reader takes as the run column, and "runs" too.
        if {cell.lower() for cell in cells} & {*RUN_HEADERS, "runs"}:
            raise CatalogValidationError(
                f"{label} contains a table with a Run column. nrw writes the "
                "measurement table from your assignments, and readers of "
                "sample.md take a run's condition from whichever table row "
                "they meet first or last -- a second table would make them "
                "disagree. Assign runs on the experiment page instead."
            )


def clean_title_snapshot(value: Any) -> str:
    """A run title or start time read from a file header, its ends trimmed.

    Never refused: it comes from the instrument, not from the person editing.
    """
    return value.strip() if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunEntry:
    """What the experimenter said about one run.

    Attributes:
        key: Which run.
        sample_id: The sample it belongs to, or ``None`` if unassigned.
        measurement: What kind of measurement, e.g. ``full Q``. The *Type*
            column of the table in ``sample.md``.
        condition: What it was measured under, e.g. ``OCV``. The *Condition*
            column, which ISAAC export reads.
        include: Whether the run is used. An excluded run is not copied, and
            one excluded after it was copied is moved out of the sample's data.
        note: Notes on this measurement, apart from the sample as a whole --
            realigned, bowed, a segment that looks high. Written into
            ``sample.md`` under *Measurement conditions*, one entry per run,
            where ``nrw model new --from-notes`` and the assistant read what
            becomes a nuisance parameter; not in the table, whose cells four
            readers parse.
        title: The run title from the file header, kept so the catalog reads
            sensibly without the data mount. Display only -- never rendered
            into the table, where it would sit beside the condition and blind
            ``nrw data reconcile``'s check that the two agree.
        start_time: The start time as the file recorded it, verbatim. Empty
            when the file does not say; never inferred.
        rev: Revision of this record, for detecting a concurrent edit.
        updated_at: When the record last changed, ISO-8601 UTC.
        extra: Columns written by a newer nrw, preserved on save.
    """

    key: RunKey
    sample_id: str | None = None
    measurement: str = ""
    condition: str = ""
    include: bool = True
    note: str = ""
    title: str = ""
    start_time: str = ""
    rev: int = 0
    updated_at: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def content(self) -> tuple[Any, ...]:
        """Everything a person decided, for telling an edit from a no-op."""
        return (
            self.sample_id,
            self.measurement,
            self.condition,
            self.include,
            self.note,
            self.title,
            self.start_time,
        )

    @property
    def measurement_type(self) -> str:
        """The run's type as ``sample.md`` shows it: :data:`DEFAULT_MEASUREMENT`
        when none was ever set."""
        return self.measurement or DEFAULT_MEASUREMENT


@dataclass(frozen=True)
class SampleContext:
    """What the experimenter said about one sample.

    Each prose field becomes the section of ``sample.md`` it is named after.

    Attributes:
        sample_id: The sample.
        title: The first heading of ``sample.md``.
        description: One or two sentences: what it is, in what, and what is
            expected.
        details: Composition, electrolyte, environment -- anything that
            constrains the model.
        mounting: One of :data:`MOUNTINGS`.
        measurement_conditions: Alignment, flatness, background, normalization.
        fits_to_perform: What to fit and what is expected to change. An
            unattended session will not start without it.
        rev: Revision of this record.
        updated_at: When the record last changed, ISO-8601 UTC.
        extra: Columns written by a newer nrw, preserved on save.
    """

    sample_id: str
    title: str = ""
    description: str = ""
    details: str = ""
    mounting: str = "unknown"
    measurement_conditions: str = ""
    fits_to_perform: str = ""
    rev: int = 0
    updated_at: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def content(self) -> tuple[Any, ...]:
        """Everything a person decided, for telling an edit from a no-op."""
        return (
            self.title,
            self.description,
            self.details,
            self.mounting,
            self.measurement_conditions,
            self.fits_to_perform,
        )


@dataclass(frozen=True)
class ExperimentContext:
    """What the experimenter said about the whole experiment: the context of
    every measurement in it, as a sample's context is of its own.

    No file is written from it, so there is none to edit by hand and drift
    from it: it is changed on the Experiment page, and read from the catalog.

    Attributes:
        summary: What the proposal sets out to learn.
        hypotheses: What the measurements should confirm or rule out.
        notes: Anything else every measurement needs.
        rev: Revision of this record; ``0`` until it is first written.
        updated_at: When the record last changed, ISO-8601 UTC.
        extra: Columns written by a newer nrw, preserved on save.
    """

    summary: str = ""
    hypotheses: str = ""
    notes: str = ""
    rev: int = 0
    updated_at: str = ""
    extra: Mapping[str, Any] = field(default_factory=dict)

    def content(self) -> tuple[Any, ...]:
        """Everything a person decided, for telling an edit from a no-op."""
        return (self.summary, self.hypotheses, self.notes)


#: The experiment's fields a person may change.
EXPERIMENT_FIELDS = ("summary", "hypotheses", "notes")


def clean_experiment_field(name: str, value: Any) -> str:
    """Check one of the experiment's fields.

    No ``sample.md`` holds it, so none of that file's rules apply: only a lone
    surrogate, which no file can store, is refused.

    Returns:
        The text with Windows line endings normalized and the ends trimmed.

    Raises:
        CatalogValidationError: With the reason, or for a field it has not.
    """
    if name not in EXPERIMENT_FIELDS:
        raise CatalogValidationError(f"the experiment has no field {name!r}")
    text = _text(name, value)
    trouble = _invisible_trouble(text, allow=_LINE_BREAKS)
    if trouble:
        raise CatalogValidationError(f"{name} contains {trouble}")
    return text


#: Run fields a person may change, and how each is checked.
RUN_FIELDS = ("sample_id", "measurement", "condition", "include", "note")

#: Run fields the page fills from the source, never typed by a person.
RUN_SNAPSHOT_FIELDS = ("title", "start_time")

#: Sample fields a person may change.
SAMPLE_FIELDS = (
    "title",
    "description",
    "details",
    "mounting",
    "measurement_conditions",
    "fits_to_perform",
)

#: Which sample fields are prose rendered into sample.md.
_PROSE_FIELDS = ("description", "details", "measurement_conditions", "fits_to_perform")


def _clean_run_field(name: str, value: Any) -> Any:
    if name == "sample_id":
        return None if value in (None, "") else validate_sample_id(value)
    if name in ("measurement", "condition"):
        return clean_line(name, value)
    if name == "include":
        if not isinstance(value, bool):
            # "false" is truthy, and 0/1 would pass `bool()` silently.
            raise CatalogValidationError(
                f"include must be true or false, not {value!r}"
            )
        return value
    if name == "note":
        return clean_prose("note", value, headings=False)
    if name == "title":
        return clean_title_snapshot(value)
    if name == "start_time":
        return clean_title_snapshot(value)
    raise CatalogValidationError(f"a run has no field {name!r}")


def _clean_sample_field(name: str, value: Any) -> Any:
    if name == "title":
        return clean_line("title", value)
    if name == "mounting":
        if value not in MOUNTINGS:
            raise CatalogValidationError(
                f"mounting must be one of {', '.join(MOUNTINGS)}, not {value!r}"
            )
        return value
    if name in _PROSE_FIELDS:
        return clean_prose(name.replace("_", " "), value)
    raise CatalogValidationError(f"a sample has no field {name!r}")


# ---------------------------------------------------------------------------
# The catalog, and edits to it
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunChange:
    """An edit to one run's record.

    Attributes:
        key: Which run.
        base_rev: The revision the editor saw; ``0`` for a run never recorded.
        changes: Field name to new value, a subset of :data:`RUN_FIELDS` and
            :data:`RUN_SNAPSHOT_FIELDS`.
    """

    key: RunKey
    base_rev: int
    changes: Mapping[str, Any]


@dataclass(frozen=True)
class SampleChange:
    """An edit to one sample's context, or its removal.

    Attributes:
        sample_id: Which sample.
        base_rev: The revision the editor saw; ``0`` for a new sample.
        changes: Field name to new value, a subset of :data:`SAMPLE_FIELDS`.
        delete: Remove the sample's context. Refused while runs are assigned.
    """

    sample_id: str
    base_rev: int
    changes: Mapping[str, Any] = field(default_factory=dict)
    delete: bool = False


@dataclass(frozen=True)
class ExperimentChange:
    """An edit to the experiment's context.

    Attributes:
        base_rev: The revision the editor saw; ``0`` before it is first written.
        changes: Field name to new value, a subset of :data:`EXPERIMENT_FIELDS`.
    """

    base_rev: int
    changes: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Catalog:
    """Everything the experimenter has decided, as one immutable value.

    A sample may be named by runs without having a context record, and a
    context record may exist before any run is assigned to it. Neither is an
    inconsistency, which is what lets the two tables be written one after the
    other without an interrupted save ever producing a catalog that means
    something wrong.

    Attributes:
        runs: Run records by key.
        samples: Sample context records by id.
        experiment: The experiment's own context: empty, at revision 0,
            until it is first written.
    """

    runs: Mapping[RunKey, RunEntry] = field(default_factory=dict)
    samples: Mapping[str, SampleContext] = field(default_factory=dict)
    experiment: ExperimentContext = field(default_factory=ExperimentContext)

    def sample_ids(self) -> list[str]:
        """Every sample the catalog knows, from either table, sorted."""
        named = {entry.sample_id for entry in self.runs.values() if entry.sample_id}
        return sorted(named | set(self.samples))

    def runs_for(self, sample_id: str) -> list[RunEntry]:
        """The runs assigned to a sample, in run order."""
        return sorted(
            (entry for entry in self.runs.values() if entry.sample_id == sample_id),
            key=lambda entry: entry.key,
        )

    def context_for(self, sample_id: str) -> SampleContext:
        """The sample's context, or an empty one if nothing was written yet."""
        return self.samples.get(sample_id) or SampleContext(sample_id=sample_id)

    def manages(self, sample_id: str) -> bool:
        """Whether the catalog owns this sample's ``sample.md``."""
        return sample_id in self.sample_ids()


def apply_changes(
    catalog: Catalog,
    *,
    runs: Iterable[RunChange] = (),
    samples: Iterable[SampleChange] = (),
    experiment: ExperimentChange | None = None,
    now: str,
) -> Catalog:
    """Apply a person's edits, refusing any made against a stale record.

    Every change is validated, and every base revision checked, before
    anything is applied -- so an edit is taken whole or not at all.

    Args:
        catalog: The current catalog, freshly loaded.
        runs: Edits to run records.
        samples: Edits to sample records.
        experiment: An edit to the experiment's context.
        now: The timestamp to record on changed records.

    Returns:
        The new catalog. Records whose content did not change keep their
        revision, so saving an unchanged edit writes nothing.

    Raises:
        CatalogValidationError: If a value breaks a rule.
        RecordConflict: If a record changed since the editor loaded it.
    """
    run_records = dict(catalog.runs)
    sample_records = dict(catalog.samples)
    stale: list[str] = []

    # Runs first: unassigning a sample's runs and removing the sample in one
    # request must work, and the removal checks that nothing is assigned.
    for change in runs:
        current = run_records.get(change.key)
        if _rev(change.base_rev) != (current.rev if current else 0):
            stale.append(f"run {change.key.run}")
            continue
        cleaned = {
            name: _clean_run_field(name, value)
            for name, value in change.changes.items()
        }
        base = current or RunEntry(key=change.key)
        proposed = replace(base, **cleaned)
        if current is not None and proposed.content() == current.content():
            continue
        if proposed.sample_id and proposed.sample_id != base.sample_id:
            _check_new_sample(proposed.sample_id, sample_records, run_records, None)
        run_records[change.key] = replace(proposed, rev=base.rev + 1, updated_at=now)

    for change in samples:
        validate_sample_id(change.sample_id)
        current = sample_records.get(change.sample_id)
        if _rev(change.base_rev) != (current.rev if current else 0):
            stale.append(f"sample {change.sample_id}")
            continue
        if change.delete:
            assigned = [
                e.key.run
                for e in run_records.values()
                if e.sample_id == change.sample_id
            ]
            if assigned:
                raise CatalogValidationError(
                    f"sample {change.sample_id} still has runs assigned "
                    f"({', '.join(map(str, sorted(assigned)))}); unassign them first."
                )
            sample_records.pop(change.sample_id, None)
            continue
        cleaned = {
            name: _clean_sample_field(name, value)
            for name, value in change.changes.items()
        }
        base = current or SampleContext(sample_id=change.sample_id)
        proposed = replace(base, **cleaned)
        if current is not None and proposed.content() == current.content():
            continue
        _check_new_sample(change.sample_id, sample_records, run_records, current)
        sample_records[change.sample_id] = replace(
            proposed, rev=base.rev + 1, updated_at=now
        )

    experiment_record = catalog.experiment
    if experiment is not None:
        if _rev(experiment.base_rev) != experiment_record.rev:
            stale.append("the experiment")
        else:
            cleaned = {
                name: clean_experiment_field(name, value)
                for name, value in experiment.changes.items()
            }
            proposed = replace(experiment_record, **cleaned)
            if proposed.content() != experiment_record.content():
                experiment_record = replace(
                    proposed, rev=experiment_record.rev + 1, updated_at=now
                )

    if stale:
        raise RecordConflict(stale)
    return Catalog(
        runs=run_records, samples=sample_records, experiment=experiment_record
    )


def _rev(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CatalogValidationError(
            f"a base revision must be a whole number, not {value!r}"
        )
    return value


def _check_new_sample(
    sample_id: str,
    samples: Mapping[str, SampleContext],
    runs: Mapping[RunKey, RunEntry],
    current: SampleContext | None,
) -> None:
    """Refuse an id that differs from an existing one only in case.

    ``S1`` and ``s1`` are two samples to Python and one directory on macOS and
    Windows, so the second would silently write into the first.
    """
    if current is not None:
        return
    known = set(samples) | {e.sample_id for e in runs.values() if e.sample_id}
    if sample_id in known:
        return
    clash = [other for other in known if other.casefold() == sample_id.casefold()]
    if clash:
        raise CatalogValidationError(
            f"{sample_id!r} differs from the existing sample {clash[0]!r} only in "
            "case. They would be the same directory on macOS and Windows."
        )
