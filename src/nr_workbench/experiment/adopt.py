"""Bring a hand-written sample.md into the catalog -- or pull hand edits back into it.

Two situations, one mechanism:

**Adopting** a sample that predates the catalog. Its ``sample.md`` was written
in an editor; the catalog knows nothing about it. Its sections become the
sample's context and its measurement table becomes run assignments.

**Pulling** hand edits. The catalog renders a sample's ``sample.md``; someone
edits the file by hand; the scaffold then refuses to overwrite it and writes
the catalog's version beside it. Pulling reads the edited file back into the
catalog, so the two agree again.

Either way the file is *parsed*, never guessed at, and anything the parser
cannot carry into the catalog is listed as a **leftover** rather than dropped.
A leftover blocks the rewrite: the rewrite replaces the file with the catalog's
rendering, and text with no place in the catalog would be lost in it. (The
original is backed up regardless, under ``.nrw/backups/``; note that that
directory is gitignored, so the backup exists on this machine only.)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from nr_workbench.experiment.model import (
    DEFAULT_MEASUREMENT,
    Catalog,
    CatalogValidationError,
    RunChange,
    RunKey,
    SampleChange,
    apply_changes,
    clean_line,
    clean_prose,
)
from nr_workbench.experiment.render import (
    CATALOG_OWNER,
    MOUNTING_SENTENCES,
    SampleRenderError,
    lock_owner,
    plan_sample,
    sample_md_relpath,
)
from nr_workbench.problems import Problem
from nr_workbench.project.layout import ProjectLayout
from nr_workbench.project.render import MeasurementRow, RenderContext, SampleProse
from nr_workbench.project.scaffold import (
    Outcome,
    classify,
    load_lock,
    render_diff,
    replace_owned,
)
from nr_workbench.sample_md import RUN_NOTE_INDENT, RUN_NOTE_RE

#: The sections of the scaffolded sample.md, and the catalog field for each.
_PROSE_SECTIONS = {
    "Description": "description",
    "Details": "details",
    "Measurement conditions": "measurement_conditions",
    "Fits to perform": "fits_to_perform",
}
_MEASUREMENTS = "Measurements"

_HEADING_RE = re.compile(r"^##[ \t]+(.+?)[ \t]*$")
_TITLE_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*$")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")


class AdoptRefused(Exception):
    """Adoption cannot go ahead as asked. The message says why."""


@dataclass(frozen=True)
class ParsedSample:
    """What a ``sample.md`` says, in the catalog's terms.

    Attributes:
        title: The first heading.
        fields: Catalog field name to value, for the prose sections and
            ``mounting``.
        rows: The measurement table, one row per run, in file order.
        leftovers: Text with no place in the catalog, each with the reason.
        listed: Every run number the table names, including rows whose cells
            could not be used. A run in the table is not a run removed from
            it, whatever is wrong with its row.
        blocking: Rows that name a run but cannot be carried into the catalog
            as written. They stop adoption: recording such a run without its
            condition -- or, worse, as excluded -- would be a claim the table
            does not make.
        notes: The notes on each measurement, by run, read from the entries
            under *Measurement conditions* for runs the table lists.
        unlisted_notes: Entries for runs the table does not list. The catalog
            cannot hold them, so they are leftovers too; kept whole here for
            a reader that wants them anyway -- AuRE's setup, fitting that run.
    """

    title: str
    fields: dict[str, str]
    rows: tuple[MeasurementRow, ...]
    leftovers: tuple[str, ...]
    listed: tuple[int, ...] = ()
    blocking: tuple[str, ...] = ()
    notes: dict[int, str] = field(default_factory=dict)
    unlisted_notes: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AdoptPlan:
    """What adopting (or pulling) would do, for review.

    Attributes:
        sample_id: The sample.
        parsed: What its sample.md says.
        run_changes: Catalog edits for its runs.
        sample_change: The catalog edit for its context, if any.
        undocumented: Runs whose files are in the sample but not in its
            table. Reported, not assigned: assigning them would be a claim
            nobody made.
        kept: Runs the catalog assigns to the sample that a file it did not
            write leaves out -- assigned on the page after the file was
            written, say. Kept as the catalog has them: that file never listed
            them, so leaving one out is not removing it.
        sample_md: The scaffold's view of the file as it stands, against the
            catalog as it is now: ``UNTRACKED`` for a file nrw never wrote,
            ``DRIFTED`` for one edited by hand since.
        in_step: Whether the catalog, once adopted, renders exactly this file
            -- in which case no rewrite is needed, and the next apply records
            the file as the catalog's.
        diff: The file now, against the catalog's rendering after adoption.
        problems: What stops adoption altogether.
    """

    sample_id: str
    parsed: ParsedSample
    run_changes: tuple[RunChange, ...] = ()
    sample_change: SampleChange | None = None
    undocumented: tuple[int, ...] = ()
    kept: tuple[int, ...] = ()
    sample_md: Outcome | None = None
    in_step: bool = False
    diff: str = ""
    problems: tuple[Problem, ...] = ()
    plan_id: str = ""

    @property
    def rewrite_ready(self) -> bool:
        """Whether the file can be replaced by the catalog's rendering losslessly."""
        return not self.problems and not self.parsed.leftovers

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON form."""
        return {
            "sample": self.sample_id,
            "title": self.parsed.title,
            "runs": [{"run": c.key.run, **dict(c.changes)} for c in self.run_changes],
            "context": dict(self.sample_change.changes) if self.sample_change else {},
            "leftovers": list(self.parsed.leftovers),
            "undocumented": list(self.undocumented),
            "kept": list(self.kept),
            "sample_md": str(self.sample_md) if self.sample_md else None,
            "in_step": self.in_step,
            "diff": self.diff,
            "rewrite_ready": self.rewrite_ready,
            "plan_id": self.plan_id,
            "problems": [p.as_dict() for p in self.problems],
        }


@dataclass
class AdoptReport:
    """What adoption did.

    Attributes:
        catalog_changes: How many catalog records changed.
        rewritten: Whether sample.md was replaced by the catalog's rendering.
        backup: Where the previous sample.md was backed up, relative to the
            project root.
    """

    catalog_changes: int = 0
    rewritten: bool = False
    backup: str | None = field(default=None)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _template_comments() -> dict[str, str]:
    """The scaffold's own guidance comment for each section, and the marker.

    Taken from the template itself, so that a comment the *template* wrote is
    recognised exactly and dropped, while a comment a *person* wrote is
    reported -- it is theirs, and it has nowhere to go in the catalog.
    """
    from nr_workbench.project.samples import plan_sample_files

    comments: dict[str, str] = {}
    for managed in (False, True):
        context = RenderContext(project_name="x", prose=SampleProse(managed=managed))
        text = next(
            p.content.decode("utf-8")
            for p in plan_sample_files(context, "x")
            if p.relpath.endswith("/sample.md")
        )
        title, preamble, sections = _split(text)
        del title
        if managed:
            comments["__marker__"] = _COMMENT_RE.search(preamble).group(0)  # type: ignore[union-attr]
            continue
        for heading, body in sections:
            found = _COMMENT_RE.search(body)
            if found:
                comments[heading] = found.group(0)
    return comments


def _split(text: str) -> tuple[str, str, list[tuple[str, str]]]:
    """Title, the text before the first section, and each section's body.

    Sections are split on ``## `` lines exactly as every other reader of
    sample.md splits them -- including inside a code fence, where they do too.
    """
    lines = text.split("\n")
    title = ""
    start = 0
    for index, line in enumerate(lines):
        if line.strip():
            match = _TITLE_RE.match(line)
            if match:
                title = match.group(1)
                start = index + 1
            break

    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in lines[start:]:
        match = _HEADING_RE.match(line)
        if match:
            sections.append((match.group(1), []))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)
    return (title, "\n".join(preamble), [(h, "\n".join(b)) for h, b in sections])


def _snippet(text: str) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= 80 else one_line[:79] + "…"


@dataclass(frozen=True)
class Measurements:
    """What ``sample.md`` says about its measurements, run by run.

    Attributes:
        shared: The sample's own *Measurement conditions* -- what holds for
            every run.
        conditions: Each run's condition, from the Measurements table.
        notes: Each run's notes, from its entry under *Measurement
            conditions* -- a run no table lists included, as hand-written notes
            often have no table.
    """

    shared: str
    conditions: dict[int, str]
    notes: dict[int, str]


def measurements_of(text: str) -> Measurements:
    """Read what a ``sample.md`` says about each measurement.

    What reads it -- AuRE's setup, the request for a model from the notes --
    wants each run's condition and notes set out, so that a contrast or a
    condition written for one run is used for that run.

    Args:
        text: The file's contents.

    Returns:
        The shared conditions, and each run's condition and notes.
    """
    parsed = parse_sample_md(text)
    return Measurements(
        shared=parsed.fields.get("measurement_conditions", ""),
        conditions={row.run: row.condition for row in parsed.rows if row.condition},
        notes={**parsed.unlisted_notes, **parsed.notes},
    )


def parse_sample_md(text: str) -> ParsedSample:
    """Read a ``sample.md`` into the catalog's fields, without losing anything.

    Args:
        text: The file's content.

    Returns:
        The parse. Text that does not fit a catalog field is in
        ``leftovers``, with the reason, rather than silently dropped.
    """
    comments = _template_comments()
    title, preamble, sections = _split(text)
    fields: dict[str, str] = {"mounting": "unknown"}
    rows: list[MeasurementRow] = []
    leftovers: list[str] = []

    rest = preamble.replace(comments.get("__marker__", "\x00"), "", 1)
    for comment in _COMMENT_RE.findall(rest):
        leftovers.append(f"a comment before the first section: {_snippet(comment)}")
    if _COMMENT_RE.sub("", rest).strip():
        leftovers.append(
            f"text before the first section: {_snippet(_COMMENT_RE.sub('', rest))}"
        )

    listed: list[int] = []
    blocking: list[str] = []
    notes: dict[int, str] = {}
    seen: set[str] = set()
    for heading, body in sections:
        if heading in seen:
            leftovers.append(f"a second '## {heading}' section")
            continue
        seen.add(heading)
        guidance = comments.get(heading)
        if guidance and guidance in body:
            body = body.replace(guidance, "", 1)
        for comment in _COMMENT_RE.findall(body):
            leftovers.append(f"a comment in '## {heading}': {_snippet(comment)}")
        body = _COMMENT_RE.sub("", body)

        if heading == _MEASUREMENTS:
            rows.extend(_parse_table(body, leftovers, listed, blocking))
        elif heading in _PROSE_SECTIONS:
            if _PROSE_SECTIONS[heading] == "measurement_conditions":
                body = _split_run_notes(body, notes)
            _parse_prose(_PROSE_SECTIONS[heading], body.strip(), fields, leftovers)
        elif body.strip():
            leftovers.append(
                f"a '## {heading}' section, which the catalog has no place for: "
                f"{_snippet(body)}"
            )
        else:
            leftovers.append(f"an empty '## {heading}' section")

    try:
        title = clean_line("title", title)
    except CatalogValidationError as exc:
        leftovers.append(f"the title: {exc}")
        title = ""
    unlisted: dict[int, str] = {}
    for run in sorted(set(notes) - set(listed)):
        # The catalog keeps notes on the runs of this sample; a note for a run
        # the table does not list has nowhere to go, and is said, not dropped.
        unlisted[run] = notes.pop(run)
        leftovers.append(
            f"a note on run {run}, which the table does not list: "
            f"{_snippet(unlisted[run])}"
        )
    kept: dict[int, str] = {}
    for run, note in notes.items():
        try:
            kept[run] = clean_prose("note", note, headings=False)
        except CatalogValidationError as exc:
            leftovers.append(f"the note on run {run}: {exc}")
    return ParsedSample(
        title=title,
        fields=fields,
        rows=tuple(rows),
        leftovers=tuple(leftovers),
        listed=tuple(listed),
        blocking=tuple(blocking),
        notes=kept,
        unlisted_notes=unlisted,
    )


def _split_run_notes(body: str, notes: dict[int, str]) -> str:
    """Take each measurement's notes out of *Measurement conditions*.

    An entry opens ``- Run <n>: `` and continues on the lines indented under
    it, blank lines between paragraphs included -- as :func:`~nr_workbench.
    experiment.render.run_notes` writes them. The notes go into *notes*, by
    run; everything else is the sample's own text, returned.
    """
    kept: list[str] = []
    entries: list[tuple[int, list[str]]] = []
    current: list[str] | None = None
    indent = len(RUN_NOTE_INDENT)
    for line in body.split("\n"):
        match = RUN_NOTE_RE.match(line)
        if match:
            current = [match.group(2)]
            entries.append((int(match.group(1)), current))
            continue
        if current is not None and (
            line.startswith(RUN_NOTE_INDENT) or not line.strip()
        ):
            current.append(line[indent:] if line.startswith(RUN_NOTE_INDENT) else "")
            continue
        current = None
        kept.append(line)
    for run, lines in entries:
        text = "\n".join(lines).strip("\n")
        # A run given two entries keeps both, in order: nothing is dropped.
        notes[run] = f"{notes[run]}\n{text}" if notes.get(run) else text
    return "\n".join(kept)


def _parse_prose(
    name: str, value: str, fields: dict[str, str], leftovers: list[str]
) -> None:
    if name == "measurement_conditions":
        for mounting, sentence in MOUNTING_SENTENCES.items():
            if value.startswith(sentence):
                fields["mounting"] = mounting
                value = value[len(sentence) :].strip()
                break
    try:
        fields[name] = clean_prose(name.replace("_", " "), value)
    except CatalogValidationError as exc:
        leftovers.append(str(exc))


def _parse_table(
    body: str, leftovers: list[str], listed: list[int], blocking: list[str]
) -> list[MeasurementRow]:
    from nr_workbench.sample_md import (
        CONDITION_HEADERS,
        RUN_HEADERS,
        TYPE_HEADERS,
        column,
        table_cells,
    )

    header: list[str] | None = None
    rows: list[MeasurementRow] = []
    runs: set[int] = set()
    for line in body.split("\n"):
        cells = table_cells(line)
        if cells is None:
            if line.strip():
                leftovers.append(f"text in '## Measurements': {_snippet(line)}")
            continue
        if header is None:
            header = [cell.lower() for cell in cells]
            if column(header, RUN_HEADERS) is None:
                leftovers.append(f"a table with no Run column: {_snippet(line)}")
                return rows
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        run_at = column(header, RUN_HEADERS)
        type_at = column(header, TYPE_HEADERS)
        condition_at = column(header, CONDITION_HEADERS)
        run_text = cells[run_at] if run_at is not None and run_at < len(cells) else ""
        if not run_text:
            if any(cells):
                leftovers.append(f"a table row with no run number: {_snippet(line)}")
            continue
        if not run_text.isascii() or not run_text.isdigit():
            blocking.append(
                f"a table row names run {run_text!r}, which is not a run number"
            )
            continue
        run = int(run_text)
        listed.append(run)
        if run in runs:
            blocking.append(f"run {run} is listed twice in the table")
            continue
        kept = {i for i in (run_at, type_at, condition_at) if i is not None}
        extra = [
            f"{name}={cells[i]}"
            for i, name in enumerate(header)
            if i not in kept and i < len(cells) and cells[i]
        ]
        if extra:
            leftovers.append(
                f"run {run}: columns the catalog does not keep ({', '.join(extra)})"
            )
        try:
            row = MeasurementRow(
                run=run,
                type=clean_line("type", _at(cells, type_at)),
                condition=clean_line("condition", _at(cells, condition_at)),
            )
        except CatalogValidationError as exc:
            blocking.append(f"run {run}: {exc}")
            continue
        runs.add(run)
        rows.append(row)
    return rows


def _at(cells: list[str], index: int | None) -> str:
    if index is None or index >= len(cells):
        return ""
    return cells[index]


# ---------------------------------------------------------------------------
# Planning and adopting
# ---------------------------------------------------------------------------


def plan_adopt(
    root: Path, catalog: Catalog, sample_id: str, context: RenderContext
) -> AdoptPlan:
    """Work out what adopting a sample's ``sample.md`` would do. Writes nothing.

    Args:
        root: Project root.
        catalog: The current catalog.
        sample_id: The sample.
        context: The project's render context.

    Returns:
        The plan. For a sample the catalog already manages, it is a *pull*:
        the edits that make the catalog match the file.
    """
    root = Path(root)
    path = root / sample_md_relpath(sample_id)
    if not path.is_file():
        return AdoptPlan(
            sample_id,
            ParsedSample("", {}, (), ()),
            problems=(Problem(f"sample:{sample_id}", f"{path.name} does not exist."),),
        )
    raw = path.read_bytes()
    try:
        # Strictly: text replaced by U+FFFD here would be written into the
        # catalog as though the scientist had typed it.
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return AdoptPlan(
            sample_id,
            ParsedSample("", {}, (), ()),
            problems=(
                Problem(
                    f"sample:{sample_id}",
                    f"{path.name} is not UTF-8 text ({exc.reason} at byte "
                    f"{exc.start}); save it as UTF-8 and try again.",
                ),
            ),
        )
    parsed = parse_sample_md(text)
    problems: list[Problem] = [
        Problem(f"sample:{sample_id}", reason) for reason in parsed.blocking
    ]

    run_changes: list[RunChange] = []
    listed = set(parsed.listed)
    for row in parsed.rows:
        key = RunKey(row.run)
        current = catalog.runs.get(key)
        if current is not None and current.sample_id not in (None, sample_id):
            problems.append(
                Problem(
                    f"run:{row.run}",
                    f"run {row.run} is in this sample's table, but the catalog "
                    f"assigns it to {current.sample_id}. Decide which is right "
                    "on the experiment page first.",
                )
            )
            continue
        # Notes the file gives a run replace the catalog's, and an entry left
        # empty clears them. A run the file gives no entry keeps its notes: a
        # sample.md written by hand, or before nrw wrote notes, never had any.
        note = parsed.notes.get(row.run, current.note if current else "")
        # A type never set is steady, so "steady" in the table is no change.
        wanted = (sample_id, row.type or DEFAULT_MEASUREMENT, row.condition, True, note)
        if (
            current is not None
            and (
                current.sample_id,
                current.measurement_type,
                current.condition,
                current.include,
                current.note,
            )
            == wanted
        ):
            continue
        fields: dict[str, Any] = {
            "sample_id": sample_id,
            "measurement": row.type,
            "condition": row.condition,
            "include": True,
        }
        if note != (current.note if current else ""):
            fields["note"] = note
        run_changes.append(RunChange(key, current.rev if current else 0, fields))
    # A row removed from the table by hand means the run is not used here --
    # in a table the catalog wrote. A sample.md written by hand never listed
    # the runs assigned to its sample on the page afterwards, and adopting it
    # used to exclude every one of them.
    missing = [
        entry
        for entry in catalog.runs_for(sample_id)
        if entry.include and entry.key.run not in listed
    ]
    kept: tuple[int, ...] = ()
    if lock_owner(root, sample_id) == CATALOG_OWNER:
        run_changes.extend(RunChange(e.key, e.rev, {"include": False}) for e in missing)
    else:
        kept = tuple(entry.key.run for entry in missing)

    current = catalog.samples.get(sample_id)
    base = current or catalog.context_for(sample_id)
    desired = dict(parsed.fields)
    if parsed.title and parsed.title != sample_id:
        desired["title"] = parsed.title
    changes = {k: v for k, v in desired.items() if getattr(base, k) != v}
    sample_change = (
        SampleChange(sample_id, current.rev if current else 0, changes)
        if changes
        else None
    )

    relpath = sample_md_relpath(sample_id)
    lock = load_lock(ProjectLayout(root=root).scaffold_lock)
    outcome: Outcome | None = None
    try:
        now_md = next(
            p
            for p in plan_sample(root, context, sample_id, catalog=catalog)
            if p.relpath == relpath
        )
        outcome = classify(now_md, path, lock.get(relpath))
    except SampleRenderError:
        outcome = None  # released or unreadable; the problems below say which

    diff = ""
    in_step = False
    try:
        proposed = apply_changes(
            catalog,
            runs=run_changes,
            samples=[sample_change] if sample_change else [],
            now="adopt-preview",
        )
        after = next(
            p
            for p in plan_sample(root, context, sample_id, catalog=proposed)
            if p.relpath == relpath
        )
        diff = render_diff(after, path)
        in_step = after.content == path.read_bytes()
    except (CatalogValidationError, SampleRenderError) as exc:
        problems.append(Problem(f"sample:{sample_id}", str(exc)))

    undocumented = ()
    try:
        from nr_workbench.project.scan import scan_sample

        on_disk = scan_sample(root, sample_id).runs_on_disk
        undocumented = tuple(sorted(on_disk - listed))
    except FileNotFoundError:
        pass

    return AdoptPlan(
        sample_id,
        parsed,
        run_changes=tuple(run_changes),
        sample_change=sample_change,
        undocumented=undocumented,
        kept=kept,
        sample_md=outcome,
        in_step=in_step,
        diff=diff,
        problems=tuple(problems),
        plan_id=_plan_id(raw, run_changes, sample_change),
    )


def _plan_id(
    raw: bytes, run_changes: list[RunChange], sample_change: SampleChange | None
) -> str:
    """What was reviewed: the file's exact bytes, and the edits read from it."""
    import hashlib
    import json

    digest = hashlib.sha256(raw)
    digest.update(
        json.dumps(
            [
                [
                    [c.key.slug(), c.base_rev, sorted(c.changes.items())]
                    for c in run_changes
                ],
                [sample_change.base_rev, sorted(sample_change.changes.items())]
                if sample_change
                else None,
            ],
            default=str,
        ).encode()
    )
    return digest.hexdigest()[:16]


def adopt(
    root: Path,
    store: Any,
    plan: AdoptPlan,
    context: RenderContext,
    *,
    rewrite: bool,
    expected_plan_id: str | None = None,
) -> AdoptReport:
    """Carry out a reviewed adoption.

    Args:
        root: Project root.
        store: The catalog store.
        plan: What :func:`plan_adopt` proposed.
        context: The project's render context.
        rewrite: Also replace sample.md with the catalog's rendering (after a
            backup), so later applies keep it in step. Without it the catalog
            is updated and the file is left exactly as it is.
        expected_plan_id: The plan id the person reviewed. The page passes
            it: pulling hand edits happens while someone is editing the file,
            and the file adopted must be the file that was shown.

    Raises:
        AdoptRefused: The plan has problems, a rewrite would lose text or
            cannot be written safely, or the plan is not the one reviewed.
            Raised before anything is changed.
        RecordConflict: The catalog changed since the plan was made.
    """
    if expected_plan_id is not None and expected_plan_id != plan.plan_id:
        raise AdoptRefused("sample.md changed since it was reviewed; review it again.")
    if plan.problems:
        raise AdoptRefused("; ".join(p.message for p in plan.problems))
    if rewrite and plan.parsed.leftovers:
        raise AdoptRefused(
            "sample.md has text the catalog cannot hold, which rewriting would "
            "lose: " + "; ".join(plan.parsed.leftovers)
        )
    if rewrite:
        # Checked before the catalog changes: a rewrite refused afterwards
        # would leave the catalog adopted and the file not.
        from nr_workbench.project.scaffold import lock_problem

        trouble = lock_problem(ProjectLayout(root=Path(root)).scaffold_lock)
        if trouble:
            raise AdoptRefused(trouble)

    catalog = store.update(
        runs=plan.run_changes,
        samples=[plan.sample_change] if plan.sample_change else [],
        now=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    report = AdoptReport(
        catalog_changes=len(plan.run_changes) + (1 if plan.sample_change else 0)
    )
    if rewrite:
        relpath = sample_md_relpath(plan.sample_id)
        md = next(
            p
            for p in plan_sample(root, context, plan.sample_id, catalog=catalog)
            if p.relpath == relpath
        )
        backup = replace_owned(Path(root), md)
        report.rewritten = True
        if backup is not None:
            report.backup = backup.relative_to(Path(root).resolve()).as_posix()
    return report
