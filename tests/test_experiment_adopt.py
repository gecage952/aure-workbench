"""Adopting a hand-written sample.md into the catalog, and pulling hand edits back.

A sample.md written in an editor holds a person's words. Adoption reads them
into the catalog, and a rewrite then replaces the file with the catalog's
rendering -- so a rewrite is allowed only when nothing would be lost, and the
parser has to be lossless for everything it claims to understand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nr_workbench.experiment.adopt import (
    AdoptRefused,
    adopt,
    parse_sample_md,
    plan_adopt,
)
from nr_workbench.experiment.model import (
    Catalog,
    RunChange,
    RunKey,
    SampleChange,
    apply_changes,
)
from nr_workbench.experiment.render import CATALOG_OWNER, plan_sample, prose_for
from nr_workbench.experiment.store import ParquetCatalogStore
from nr_workbench.project.render import RenderContext
from nr_workbench.project.samples import plan_sample_files
from nr_workbench.project.scaffold import (
    Outcome,
    apply_scaffold,
    classify,
    forget,
    load_lock,
    replace_owned,
)

NOW = "2026-09-25T12:00:00Z"

HAND_WRITTEN = """\
# Cu/Pt in d8-THF (expt 11)

## Description

About 30 nm Cu on Pt, in 1 M LiBF4 in d8-THF.

## Details

- Cu 20 nm, Pt 5 nm
- Ti adhesion layer

## Measurements

| Run    | Type   | Condition   |
|--------|--------|-------------|
| 218386 | full Q | OCV         |
| 218393 | full Q | CA -0.5 V   |

## Measurement conditions

Background looked flat at high Q.

## Fits to perform

Co-refine 218386 and 218393.
"""


def rendered(catalog: Catalog, sample: str = "Sample6") -> str:
    context = RenderContext(project_name="p", prose=prose_for(catalog, sample))
    return next(
        p.content.decode()
        for p in plan_sample_files(
            context, sample, title=catalog.context_for(sample).title or None
        )
        if p.relpath.endswith("sample.md")
    )


def catalog_with(**context) -> Catalog:
    return apply_changes(
        Catalog(),
        runs=[
            RunChange(
                RunKey(218386),
                0,
                {
                    "sample_id": "Sample6",
                    "measurement": "full Q",
                    "condition": "OCV",
                    "note": "Realigned after mounting.\n\nThe 0.45 deg segment reads high.",
                },
            ),
            RunChange(RunKey(218393), 0, {"sample_id": "Sample6", "condition": ""}),
        ],
        samples=[SampleChange("Sample6", 0, context)] if context else [],
        now=NOW,
    )


def write_sample(project: Path, text: str, sample: str = "Sample6") -> Path:
    path = project / "samples" / sample / "sample.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# The parser is lossless for what it understands
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "context",
    [
        pytest.param({}, id="empty"),
        pytest.param(
            {
                "title": "Cu/Pt in d8-THF",
                "description": "First paragraph.\n\nSecond paragraph, *emphasis*.",
                "details": "- bullet one\n- bullet two\n\n| Layer | nm |\n|---|---|\n| Cu | 20 |",
                "mounting": "once",
                "measurement_conditions": "Background flat.\n\n```\nQ R dR\n```",
                "fits_to_perform": "Co-refine both.\n\n1. first\n2. second",
            },
            id="formatted",
        ),
        pytest.param({"mounting": "remounted"}, id="remounted_only"),
        pytest.param(
            {"mounting": "unknown", "measurement_conditions": "Flat."}, id="unknown"
        ),
        # Remarks on single runs in the sample's own text, as they were written
        # before runs had notes: not in the run-note form, so they stay put.
        pytest.param(
            {
                "measurement_conditions": "Flat.\n- Run 218393 was realigned.\n"
                "- Run 218399 had the beam drop."
            },
            id="remarks_per_run_in_prose",
        ),
    ],
)
def test_parse_of_a_rendering_is_the_catalog_again(context) -> None:
    catalog = catalog_with(**context)

    parsed = parse_sample_md(rendered(catalog))

    assert parsed.leftovers == ()
    record = catalog.context_for("Sample6")
    for name in (
        "description",
        "details",
        "mounting",
        "measurement_conditions",
        "fits_to_perform",
    ):
        assert parsed.fields[name] == getattr(record, name), name
    # A type never set is written as steady.
    assert [(r.run, r.type, r.condition) for r in parsed.rows] == [
        (218386, "full Q", "OCV"),
        (218393, "steady", ""),
    ]
    assert parsed.notes == {218386: catalog.runs[RunKey(218386)].note}


def test_the_blank_scaffold_parses_to_nothing_and_leaves_nothing_over() -> None:
    """The template's own guidance comments are recognised, not reported."""
    blank = (
        Path(__file__).parent / "data" / "experiment" / "blank_sample.md"
    ).read_text()

    parsed = parse_sample_md(blank)

    assert parsed.leftovers == ()
    assert parsed.rows == ()
    assert parsed.fields["description"] == "" and parsed.fields["mounting"] == "unknown"


def test_a_hand_written_sample_md_parses_as_a_person_would_read_it() -> None:
    parsed = parse_sample_md(HAND_WRITTEN)

    assert parsed.title == "Cu/Pt in d8-THF (expt 11)"
    assert parsed.fields["details"] == "- Cu 20 nm, Pt 5 nm\n- Ti adhesion layer"
    assert [(r.run, r.condition) for r in parsed.rows] == [
        (218386, "OCV"),
        (218393, "CA -0.5 V"),
    ]
    assert parsed.leftovers == ()


@pytest.mark.parametrize(
    "extra,reason",
    [
        pytest.param(
            "\n## Notes\n\nKeep for later.\n", "'## Notes' section", id="own_section"
        ),
        pytest.param(
            "\n## Details\n\nagain\n", "second '## Details'", id="duplicate_section"
        ),
    ],
)
def test_text_with_no_place_in_the_catalog_is_a_leftover(
    extra: str, reason: str
) -> None:
    parsed = parse_sample_md(HAND_WRITTEN + extra)

    assert any(reason in leftover for leftover in parsed.leftovers)


def test_a_persons_own_comment_is_a_leftover_not_template_guidance() -> None:
    text = HAND_WRITTEN.replace(
        "Background looked flat", "<!-- check this -->\nBackground looked flat"
    )

    parsed = parse_sample_md(text)

    assert any("check this" in leftover for leftover in parsed.leftovers)
    assert (
        parsed.fields["measurement_conditions"] == "Background looked flat at high Q."
    )


def test_a_table_column_the_catalog_does_not_keep_is_a_leftover() -> None:
    text = HAND_WRITTEN.replace(
        "| Run    | Type   | Condition   |", "| Run    | Type   | Condition   | Notes |"
    ).replace(
        "| 218386 | full Q | OCV         |", "| 218386 | full Q | OCV | aborted? |"
    )

    parsed = parse_sample_md(text)

    assert any("notes=aborted?" in leftover for leftover in parsed.leftovers)


def test_a_heading_inside_a_section_is_a_leftover_with_the_reason() -> None:
    text = HAND_WRITTEN.replace(
        "- Ti adhesion layer", "- Ti adhesion layer\n### Substrate\nSi"
    )

    parsed = parse_sample_md(text)

    assert any("heading" in leftover for leftover in parsed.leftovers)
    assert "details" not in parsed.fields


# --------------------------------------------------------------------------
# Adopting
# --------------------------------------------------------------------------


def test_adopt_a_hand_written_sample_assigns_its_runs_and_context(
    project: Path, context: RenderContext
) -> None:
    write_sample(project, HAND_WRITTEN)
    store = ParquetCatalogStore.for_project(project)

    plan = plan_adopt(project, store.load(), "Sample6", context)
    adopt(project, store, plan, context, rewrite=False)

    catalog = store.load()
    assert catalog.runs[RunKey(218393)].condition == "CA -0.5 V"
    assert (
        catalog.context_for("Sample6").fits_to_perform == "Co-refine 218386 and 218393."
    )
    # Without --rewrite, the file is left exactly as the person wrote it.
    assert (project / "samples/Sample6/sample.md").read_text() == HAND_WRITTEN


def test_adopt_with_rewrite_backs_up_and_later_applies_are_unchanged(
    project: Path, context: RenderContext
) -> None:
    path = write_sample(project, HAND_WRITTEN)
    store = ParquetCatalogStore.for_project(project)

    plan = plan_adopt(project, store.load(), "Sample6", context)
    assert plan.sample_md is Outcome.UNTRACKED and plan.rewrite_ready
    report = adopt(project, store, plan, context, rewrite=True)

    backup = project / report.backup
    assert backup.read_text() == HAND_WRITTEN
    entry = load_lock(project / ".nrw" / "scaffold.lock.json")[
        "samples/Sample6/sample.md"
    ]
    assert entry["owner"] == CATALOG_OWNER
    # The catalog now renders exactly what is on disk.
    md = next(
        p
        for p in plan_sample(project, context, "Sample6", catalog=store.load())
        if p.relpath.endswith("sample.md")
    )
    assert classify(md, path, entry) is Outcome.UNCHANGED
    assert "| 218393 | full Q | CA -0.5 V |" in path.read_text()


def test_adopt_refuses_a_rewrite_that_would_lose_text(
    project: Path, context: RenderContext
) -> None:
    path = write_sample(project, HAND_WRITTEN + "\n## Notes\n\nKeep for later.\n")
    store = ParquetCatalogStore.for_project(project)
    plan = plan_adopt(project, store.load(), "Sample6", context)

    with pytest.raises(AdoptRefused, match="Notes"):
        adopt(project, store, plan, context, rewrite=True)

    assert "Keep for later." in path.read_text()
    assert not store.exists()


def test_adopt_a_run_the_catalog_gives_to_another_sample_is_refused(
    project: Path, context: RenderContext
) -> None:
    write_sample(project, HAND_WRITTEN)
    store = ParquetCatalogStore.for_project(project)
    store.update(runs=[RunChange(RunKey(218386), 0, {"sample_id": "Sample7"})], now=NOW)

    plan = plan_adopt(project, store.load(), "Sample6", context)

    assert any("Sample7" in p.message for p in plan.problems)
    with pytest.raises(AdoptRefused):
        adopt(project, store, plan, context, rewrite=False)


def test_runs_on_disk_but_not_in_the_table_are_reported_not_assigned(
    project: Path, context: RenderContext
) -> None:
    write_sample(project, HAND_WRITTEN)
    steady = project / "samples" / "Sample6" / "data" / "steady"
    steady.mkdir(parents=True)
    (steady / "REFL_218400_1_218400_partial.txt").write_text("0.01 1 0.1 0.001\n")

    plan = plan_adopt(project, Catalog(), "Sample6", context)

    assert plan.undocumented == (218400,)
    assert RunKey(218400) not in {c.key for c in plan.run_changes}


# --------------------------------------------------------------------------
# Pulling hand edits
# --------------------------------------------------------------------------


def test_pull_a_hand_edit_back_into_the_catalog(
    project: Path, context: RenderContext
) -> None:
    store = ParquetCatalogStore.for_project(project)
    store.update(
        runs=[
            RunChange(RunKey(218386), 0, {"sample_id": "Sample6", "condition": "OCV"}),
            RunChange(RunKey(218393), 0, {"sample_id": "Sample6", "condition": "OCV"}),
        ],
        samples=[SampleChange("Sample6", 0, {"description": "Before."})],
        now=NOW,
    )
    apply_scaffold(project, plan_sample(project, context, "Sample6"))
    path = project / "samples" / "Sample6" / "sample.md"
    edited = (
        path.read_text()
        .replace("| 218393 | steady | OCV |", "| 218393 | steady | CA |")
        .replace("\nBefore.\n", "\nAfter, by hand.\n")
    )
    path.write_text(edited)

    plan = plan_adopt(project, store.load(), "Sample6", context)
    assert plan.sample_md is Outcome.DRIFTED
    # Pulled, the catalog renders the edited file byte for byte.
    assert plan.in_step
    adopt(project, store, plan, context, rewrite=True)

    catalog = store.load()
    assert catalog.runs[RunKey(218393)].condition == "CA"
    assert catalog.context_for("Sample6").description == "After, by hand."
    report = apply_scaffold(project, plan_sample(project, context, "Sample6"))
    assert {f.outcome for f in report.files if f.relpath.endswith("sample.md")} == {
        Outcome.UNCHANGED
    }


def test_pull_a_row_removed_by_hand_excludes_the_run(
    project: Path, context: RenderContext
) -> None:
    store = ParquetCatalogStore.for_project(project)
    store.update(
        runs=[
            RunChange(RunKey(218386), 0, {"sample_id": "Sample6"}),
            RunChange(RunKey(218393), 0, {"sample_id": "Sample6"}),
        ],
        now=NOW,
    )
    apply_scaffold(project, plan_sample(project, context, "Sample6"))
    path = project / "samples" / "Sample6" / "sample.md"
    path.write_text(path.read_text().replace("| 218393 | steady |  |\n", ""))

    plan = plan_adopt(project, store.load(), "Sample6", context)

    assert RunChange(RunKey(218393), 1, {"include": False}) in plan.run_changes


def test_adopting_a_hand_written_file_keeps_the_runs_assigned_on_the_page(
    project: Path, context: RenderContext
) -> None:
    """Its table was written before those runs were assigned, and never listed
    them: leaving them out is not removing them."""
    store = ParquetCatalogStore.for_project(project)
    store.update(
        runs=[
            RunChange(RunKey(218386), 0, {"sample_id": "Sample6"}),
            RunChange(RunKey(218393), 0, {"sample_id": "Sample6"}),
        ],
        now=NOW,
    )
    write_sample(
        project,
        "# Sample6\n\n## Measurements\n\n"
        "| Run | Type | Condition |\n|---|---|---|\n| 218386 | full Q | OCV |\n",
    )

    plan = plan_adopt(project, store.load(), "Sample6", context)

    assert plan.kept == (218393,)
    assert not [c for c in plan.run_changes if c.changes.get("include") is False]
    assert plan.as_dict()["kept"] == [218393]


# --------------------------------------------------------------------------
# The scaffold helpers
# --------------------------------------------------------------------------


def test_replace_owned_keeps_a_backup_and_records_the_owner(project: Path) -> None:
    from nr_workbench.project.scaffold import PlannedFile

    target = write_sample(project, "# mine\n")
    planned = PlannedFile(
        "samples/Sample6/sample.md",
        b"# theirs\n",
        "sample/sample.md.j2",
        owner="experiment",
    )

    backup = replace_owned(project, planned)

    assert backup.read_text() == "# mine\n"
    assert target.read_text() == "# theirs\n"
    assert (
        load_lock(project / ".nrw/scaffold.lock.json")[planned.relpath]["owner"]
        == "experiment"
    )


def test_force_still_leaves_a_users_own_file_alone(
    project: Path, context: RenderContext
) -> None:
    """replace_owned is separate precisely so `--force` does not grow."""
    target = write_sample(project, "# brought by the user\n", sample="Sample9")

    apply_scaffold(project, plan_sample_files(context, "Sample9"), force=True)

    assert target.read_text() == "# brought by the user\n"


def test_forget_makes_a_file_its_owners(project: Path, context: RenderContext) -> None:
    """`nrw experiment release`: the lock forgets, the file stays, no plan writes it."""
    catalog = catalog_with(fits_to_perform="Fit.")
    apply_scaffold(project, plan_sample(project, context, "Sample6", catalog=catalog))
    path = project / "samples" / "Sample6" / "sample.md"
    content = path.read_text()

    assert forget(project, "samples/Sample6/sample.md")

    report = apply_scaffold(
        project, plan_sample(project, context, "Sample6", catalog=Catalog())
    )
    outcome = {f.relpath: f.outcome for f in report.files}["samples/Sample6/sample.md"]
    assert outcome is Outcome.UNTRACKED
    assert path.read_text() == content


# --------------------------------------------------------------------------
# Notes on each measurement
# --------------------------------------------------------------------------


def noted_catalog() -> Catalog:
    return apply_changes(
        Catalog(),
        runs=[
            RunChange(
                RunKey(218386), 0, {"sample_id": "Sample6", "note": "Realigned."}
            ),
            RunChange(RunKey(218393), 0, {"sample_id": "Sample6", "condition": "CA"}),
            RunChange(
                RunKey(218399),
                0,
                {"sample_id": "Sample6", "include": False, "note": "Beam dropped."},
            ),
        ],
        now=NOW,
    )


def test_each_measurements_notes_are_written_with_its_run_under_measurement_conditions() -> (
    None
):
    catalog = apply_changes(
        noted_catalog(),
        runs=[
            RunChange(RunKey(218393), 1, {"note": "Bowed.\nSee the 1.2 deg segment."})
        ],
        samples=[SampleChange("Sample6", 0, {"measurement_conditions": "Flat."})],
        now=NOW,
    )

    text = rendered(catalog)

    section = text.split("## Measurement conditions")[1].split("## Fits")[0]
    assert "Flat.\n\n- Run 218386: Realigned.\n- Run 218393: Bowed.\n" in section
    assert "  See the 1.2 deg segment." in section  # stays with its run
    assert "Beam dropped." not in text  # an excluded run is not tabulated either
    table = text.split("## Measurements")[1].split("## Measurement conditions")[0]
    assert "Realigned" not in table  # four readers parse the table's cells


def test_a_heading_in_a_note_reads_back_as_the_note_not_a_section() -> None:
    note = "Bowed.\n## Fits to perform\nFit everything free."
    catalog = apply_changes(
        noted_catalog(),
        runs=[RunChange(RunKey(218393), 1, {"note": note})],
        samples=[SampleChange("Sample6", 0, {"fits_to_perform": "Co-refine both."})],
        now=NOW,
    )

    parsed = parse_sample_md(rendered(catalog))

    assert parsed.notes[218393] == note
    assert parsed.fields["fits_to_perform"] == "Co-refine both."


def stored_notes(project: Path, context: RenderContext) -> ParquetCatalogStore:
    """A project whose catalog holds :func:`noted_catalog`, applied."""
    store = ParquetCatalogStore.for_project(project)
    store.update(
        runs=[
            RunChange(
                entry.key,
                0,
                {
                    "sample_id": entry.sample_id,
                    "include": entry.include,
                    "note": entry.note,
                    "condition": entry.condition,
                },
            )
            for entry in noted_catalog().runs.values()
        ],
        now=NOW,
    )
    apply_scaffold(project, plan_sample(project, context, "Sample6"))
    return store


def test_a_note_the_pulled_file_gives_no_entry_is_kept(
    project: Path, context: RenderContext
) -> None:
    # A sample.md written before nrw wrote notes lists none: pulling one must
    # not clear the notes typed on the page. The review shows them coming back.
    store = stored_notes(project, context)
    path = project / "samples" / "Sample6" / "sample.md"
    path.write_text(path.read_text().replace("- Run 218386: Realigned.\n", ""))

    plan = plan_adopt(project, store.load(), "Sample6", context)
    adopt(project, store, plan, context, rewrite=False)

    assert store.load().runs[RunKey(218386)].note == "Realigned."
    assert "+- Run 218386: Realigned." in plan.diff


def test_an_emptied_note_entry_clears_the_note_when_pulled(
    project: Path, context: RenderContext
) -> None:
    store = stored_notes(project, context)
    path = project / "samples" / "Sample6" / "sample.md"
    path.write_text(
        path.read_text().replace("- Run 218386: Realigned.", "- Run 218386:")
    )

    plan = plan_adopt(project, store.load(), "Sample6", context)
    adopt(project, store, plan, context, rewrite=False)

    assert store.load().runs[RunKey(218386)].note == ""


def test_a_hand_written_file_keeps_the_notes_it_never_listed(
    project: Path, context: RenderContext
) -> None:
    store = ParquetCatalogStore.for_project(project)
    store.update(
        runs=[
            RunChange(RunKey(218386), 0, {"sample_id": "Sample6", "note": "Realigned."})
        ],
        now=NOW,
    )
    write_sample(
        project,
        "# Sample6\n\n## Measurements\n\n"
        "| Run | Type | Condition |\n|---|---|---|\n| 218386 | steady | OCV |\n",
    )

    plan = plan_adopt(project, store.load(), "Sample6", context)

    (change,) = plan.run_changes
    assert "note" not in change.changes  # kept as the catalog has it
    assert change.changes["condition"] == "OCV"


def test_a_note_written_by_hand_under_measurement_conditions_becomes_the_runs(
    project: Path, context: RenderContext
) -> None:
    """How people wrote per-run notes before there was a place for them."""
    write_sample(
        project,
        "# Sample6\n\n## Measurements\n\n"
        "| Run | Type | Condition |\n|---|---|---|\n| 218386 | steady | OCV |\n\n"
        "## Measurement conditions\n\nFlat throughout.\n\n"
        "- Run 218386: realigned after the first segment\n"
        "- Run 999999: a run the table does not list\n",
    )

    plan = plan_adopt(project, Catalog(), "Sample6", context)

    (change,) = plan.run_changes
    assert change.changes["note"] == "realigned after the first segment"
    assert plan.sample_change.changes["measurement_conditions"] == "Flat throughout."
    assert any(
        "run 999999, which the table does not list" in s for s in plan.parsed.leftovers
    )


def test_a_note_in_the_file_that_would_break_sample_md_is_set_aside(
    project: Path, context: RenderContext
) -> None:
    # A comment opened in a note: every reader that strips comments would
    # hide what follows. The note is said and left out; the run is adopted.
    write_sample(
        project,
        "# Sample6\n\n## Measurement conditions\n\n"
        "- Run 218386: Realigned. <!-- see the log\n\n"
        "## Measurements\n\n"
        "| Run | Type | Condition |\n|---|---|---|\n| 218386 | steady | OCV |\n",
    )
    store = ParquetCatalogStore.for_project(project)

    plan = plan_adopt(project, store.load(), "Sample6", context)

    assert any("the note on run 218386" in item for item in plan.parsed.leftovers)
    assert not plan.problems
    adopt(project, store, plan, context, rewrite=False)
    entry = store.load().runs[RunKey(218386)]
    assert (entry.condition, entry.note) == ("OCV", "")
