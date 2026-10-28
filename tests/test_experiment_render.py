"""sample.md rendered from the catalog, and read back by everything that reads it.

About twenty modules read sample.md, each its own way: the agent takes its task
from one section, ISAAC export takes conditions from the table, `nrw data
reconcile` compares the table with the file headers, `nrw sample scan` counts
the runs it mentions. A rendered file is only correct if every one of them
reads what the catalog says -- so the central test here asks all of them.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nr_workbench.experiment.model import (
    Catalog,
    RunChange,
    RunKey,
    SampleChange,
    apply_changes,
)
from nr_workbench.experiment.render import (
    CATALOG_OWNER,
    SampleRenderError,
    plan_sample,
    prose_for,
)
from nr_workbench.project.render import RenderContext
from nr_workbench.project.samples import plan_sample_files
from nr_workbench.project.scaffold import (
    Outcome,
    PlannedFile,
    apply_scaffold,
    classify,
    sha256_bytes,
)

from .experiment_fixtures import REFERENCE_STEADY

GOLDEN = Path(__file__).parent / "data" / "experiment"
NOW = "2026-09-25T12:00:00Z"

FITS = "Co-refine 218386 and 218393. The oxide should thicken between them."
DESCRIPTION = "About 30 nm Cu on Pt, in 1 M LiBF4 in d8-THF."
DETAILS = "Cu 20 nm, Pt 5 nm, Ti adhesion layer, Si substrate."


def reference_catalog(**overrides) -> Catalog:
    """218386 at OCV, 218393 recorded (wrongly) as OCV -- its title says CA."""
    runs = [
        RunChange(
            RunKey(218386),
            0,
            {"sample_id": "Sample6", "measurement": "full Q", "condition": "OCV"},
        ),
        RunChange(
            RunKey(218393),
            0,
            {
                "sample_id": "Sample6",
                "measurement": "full Q",
                "condition": overrides.pop("condition_218393", "OCV"),
            },
        ),
    ]
    context = {
        "title": "Cu/Pt in d8-THF (expt 11)",
        "description": DESCRIPTION,
        "details": DETAILS,
        "mounting": "once",
        "measurement_conditions": "Background looked flat at high Q.",
        "fits_to_perform": FITS,
    }
    context.update(overrides)
    return apply_changes(
        Catalog(), runs=runs, samples=[SampleChange("Sample6", 0, context)], now=NOW
    )


def render_into(project: Path, context: RenderContext, catalog: Catalog) -> str:
    """Plan and apply Sample6 from *catalog*, with the reference data copied in."""
    apply_scaffold(project, plan_sample(project, context, "Sample6", catalog=catalog))
    steady = project / "samples" / "Sample6" / "data" / "steady"
    for path in REFERENCE_STEADY.glob("*.txt"):
        shutil.copy(path, steady / path.name)
    return (project / "samples" / "Sample6" / "sample.md").read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# A sample the catalog does not manage is exactly as it always was
# --------------------------------------------------------------------------


def test_the_blank_render_is_byte_identical_to_the_scaffold(
    context: RenderContext,
) -> None:
    planned = {p.relpath: p.content for p in plan_sample_files(context, "Sample1")}

    assert (
        planned["samples/Sample1/sample.md"]
        == (GOLDEN / "blank_sample.md").read_bytes()
    )
    assert (
        planned["samples/Sample1/sample.yaml"]
        == (GOLDEN / "blank_sample.yaml").read_bytes()
    )


def test_plan_sample_without_a_catalog_is_the_scaffold(
    project: Path, context: RenderContext
) -> None:
    ours = plan_sample(project, context, "Sample9")
    theirs = plan_sample_files(context, "Sample9")

    assert [(p.relpath, p.content, p.owner) for p in ours] == [
        (p.relpath, p.content, None) for p in theirs
    ]


# --------------------------------------------------------------------------
# Every reader agrees with the catalog
# --------------------------------------------------------------------------


def test_applied_reference_sample_md_every_reader_agrees(
    project: Path, context: RenderContext
) -> None:
    from nr_workbench.agent.session import declared_task
    from nr_workbench.aure_setup import describe_sample, read_hypothesis
    from nr_workbench.conditions import from_table
    from nr_workbench.instrument.header import read_header
    from nr_workbench.project.scan import scan_sample
    from nr_workbench.reconcile import documented_runs, reconcile
    from nr_workbench.web.project import ProjectData
    from nr_workbench.web.prose import render

    text = render_into(project, context, reference_catalog())

    # The unattended agent's task, and AuRE's description and hypothesis.
    assert declared_task(text) == FITS
    assert read_hypothesis(text) == FITS
    assert describe_sample(text) == f"{DESCRIPTION}\n\n{DETAILS}"
    # ISAAC export's conditions, and reconcile's table.
    assert from_table(text, "218386") == "OCV"
    assert from_table(text, "218393") == "OCV"
    assert set(documented_runs(text)) == {218386, 218393}
    # The scan: every run on disk documented, nothing documented missing.
    scan = scan_sample(project, "Sample6")
    assert scan.documented_but_absent == [] and scan.present_but_undocumented == []
    # The page's title and rendering.
    assert ProjectData(project)._sample_title("Sample6") == "Cu/Pt in d8-THF (expt 11)"
    html = render(text)
    assert "<table>" in html and "<!--" not in html
    assert "Mounted once and not moved" in html

    # And reconcile still catches the real mislabelling: 218393's title says
    # CA, the table says OCV. Copying titles into the table would have hidden it.
    steady = project / "samples" / "Sample6" / "data" / "steady"
    headers = [read_header(p) for p in sorted(steady.glob("*.txt"))]
    findings = reconcile("Sample6", headers, text).findings
    flagged = {f.run for f in findings if f.kind == "state-contradicts-notes"}
    assert 218393 in flagged and 218386 not in flagged


def test_watch_sees_the_rendered_sample_as_it_sees_a_hand_written_one(
    project: Path, context: RenderContext
) -> None:
    from nr_workbench.agent import watch

    render_into(project, context, reference_catalog())

    verdicts = watch.assess(
        project, "Sample6", watch.WatchState(), settle_seconds=0, now=4e9
    )

    assert {v.run for v in verdicts if v.ready} == {218386, 218393}


def test_render_empty_condition_from_table_not_type(
    project: Path, context: RenderContext
) -> None:
    """An empty Condition cell once made ISAAC record the Type as the condition."""
    from nr_workbench.conditions import from_table

    text = render_into(project, context, reference_catalog(condition_218393=""))

    assert from_table(text, "218393") is None
    assert from_table(text, "218386") == "OCV"


@pytest.mark.parametrize(
    "description",
    [
        pytest.param("Compare with run 218387 from June.", id="six_digits"),
        pytest.param("Plot {{ 7*7 }} and </script> verbatim.", id="markup"),
        pytest.param("A dash -- and a > sign.", id="punctuation"),
    ],
)
def test_accepted_text_reads_back_verbatim_everywhere(
    project: Path, context: RenderContext, description: str
) -> None:
    from nr_workbench.agent.session import declared_task
    from nr_workbench.aure_setup import describe_sample
    from nr_workbench.conditions import from_table
    from nr_workbench.reconcile import documented_runs

    text = render_into(project, context, reference_catalog(description=description))

    assert describe_sample(text).startswith(description)
    assert declared_task(text) == FITS
    assert from_table(text, "218386") == "OCV"
    assert set(documented_runs(text)) == {218386, 218393}


def test_excluded_runs_are_not_tabulated() -> None:
    catalog = apply_changes(
        reference_catalog(),
        runs=[RunChange(RunKey(218393), 1, {"include": False})],
        now=NOW,
    )

    assert [row.run for row in prose_for(catalog, "Sample6").measurements] == [218386]


@pytest.mark.parametrize(
    "mounting,expected,absent",
    [
        ("once", "Mounted once", "Remounted"),
        ("remounted", "Remounted or realigned", "Mounted once"),
        ("unknown", None, "ounted"),
    ],
)
def test_mounting_is_written_only_as_a_claim_someone_made(
    mounting, expected, absent
) -> None:
    """`unknown` must not read as "mounted once": that decides per-model alignment."""
    conditions = prose_for(
        reference_catalog(mounting=mounting), "Sample6"
    ).measurement_conditions

    if expected:
        assert conditions.startswith(expected)
    else:
        assert conditions == "Background looked flat at high Q."
    assert absent not in conditions


# --------------------------------------------------------------------------
# The reset trap: nothing may plan the blank template over a rendered file
# --------------------------------------------------------------------------


def test_sample_new_after_apply_sample_md_unchanged(
    project: Path, context: RenderContext, monkeypatch
) -> None:
    from nr_workbench.commands.sample import run_sample_new
    from nr_workbench.experiment.store import ParquetCatalogStore

    store = ParquetCatalogStore.for_project(project)
    catalog = reference_catalog()
    store.update(
        runs=[
            RunChange(e.key, 0, {"sample_id": e.sample_id, "condition": e.condition})
            for e in catalog.runs.values()
        ],
        samples=[
            SampleChange("Sample6", 0, {"title": "Cu/Pt", "fits_to_perform": FITS})
        ],
        now=NOW,
    )
    apply_scaffold(project, plan_sample(project, context, "Sample6"))
    rendered = (project / "samples" / "Sample6" / "sample.md").read_bytes()
    monkeypatch.chdir(project)

    run_sample_new(sample_id="Sample6")

    sample_dir = project / "samples" / "Sample6"
    assert (sample_dir / "sample.md").read_bytes() == rendered
    # Nor a blank-template "proposal" beside it, which is what planning the
    # scaffold instead of the catalog's render leaves behind.
    assert not (sample_dir / "sample.md.nrw-new").exists()


def test_init_sample_after_apply_sample_md_unchanged(
    project: Path, context: RenderContext
) -> None:
    from nr_workbench.commands.init_cmd import _plan_sample
    from nr_workbench.experiment.store import ParquetCatalogStore

    store = ParquetCatalogStore.for_project(project)
    store.update(
        runs=[RunChange(RunKey(218386), 0, {"sample_id": "Sample6"})],
        samples=[SampleChange("Sample6", 0, {"fits_to_perform": FITS})],
        now=NOW,
    )
    apply_scaffold(project, plan_sample(project, context, "Sample6"))

    report = apply_scaffold(project, _plan_sample(project, context, "Sample6"))

    outcome = {f.relpath: f.outcome for f in report.files}
    assert outcome["samples/Sample6/sample.md"] is Outcome.UNCHANGED


def test_classify_owner_mismatch_is_drifted(tmp_path: Path) -> None:
    """The structural guard: a blank plan never upgrades a catalog-owned file."""
    target = tmp_path / "sample.md"
    target.write_bytes(b"rendered from the catalog\n")
    lock_entry = {
        "template_id": "sample/sample.md.j2",
        "template_version": 1,
        "sha256_at_install": sha256_bytes(target.read_bytes()),
        "owner": CATALOG_OWNER,
    }
    blank = PlannedFile("sample.md", b"blank template\n", "sample/sample.md.j2")

    assert classify(blank, target, lock_entry) is Outcome.DRIFTED


def test_classify_the_catalog_may_upgrade_the_untouched_scaffold(
    tmp_path: Path,
) -> None:
    """The first apply to a sample made by `nrw sample new` must go through."""
    target = tmp_path / "sample.md"
    target.write_bytes(b"blank template\n")
    lock_entry = {
        "template_id": "sample/sample.md.j2",
        "template_version": 1,
        "sha256_at_install": sha256_bytes(target.read_bytes()),
    }
    rendered = PlannedFile(
        "sample.md", b"rendered\n", "sample/sample.md.j2", owner=CATALOG_OWNER
    )

    assert classify(rendered, target, lock_entry) is Outcome.UPGRADE


def test_a_hand_edited_rendered_file_is_never_overwritten(
    project: Path, context: RenderContext
) -> None:
    catalog = reference_catalog()
    apply_scaffold(project, plan_sample(project, context, "Sample6", catalog=catalog))
    path = project / "samples" / "Sample6" / "sample.md"
    edited = path.read_text() + "\nA line the scientist added by hand.\n"
    path.write_text(edited)

    changed = reference_catalog(description="A different description.")
    report = apply_scaffold(
        project, plan_sample(project, context, "Sample6", catalog=changed)
    )

    assert path.read_text() == edited
    assert (path.parent / "sample.md.nrw-new").is_file()
    assert Outcome.DRIFTED in {f.outcome for f in report.files}


def test_an_unreadable_catalog_refuses_rather_than_planning_blank(
    project: Path, context: RenderContext, monkeypatch
) -> None:
    from nr_workbench.commands.sample import run_sample_new
    from nr_workbench.experiment.store import ParquetCatalogStore

    store = ParquetCatalogStore.for_project(project)
    store.update(runs=[RunChange(RunKey(218386), 0, {"sample_id": "Sample6"})], now=NOW)
    (store.directory / "runs.parquet").write_bytes(b"")

    with pytest.raises(SampleRenderError, match="cannot be read"):
        plan_sample(project, context, "Sample6")

    import click

    monkeypatch.chdir(project)
    with pytest.raises(click.ClickException):
        run_sample_new(sample_id="Sample6")
    assert not (project / "samples" / "Sample6").exists()


def test_a_catalog_owned_file_whose_sample_left_the_catalog_is_refused(
    project: Path, context: RenderContext
) -> None:
    apply_scaffold(
        project, plan_sample(project, context, "Sample6", catalog=reference_catalog())
    )

    with pytest.raises(SampleRenderError, match="no longer has"):
        plan_sample(project, context, "Sample6", catalog=Catalog())


def test_a_title_contradicting_the_catalog_is_refused(
    project: Path, context: RenderContext
) -> None:
    with pytest.raises(SampleRenderError, match="titles Sample6"):
        plan_sample(
            project, context, "Sample6", title="Other", catalog=reference_catalog()
        )


def test_a_quote_in_a_title_still_makes_a_readable_register(
    project: Path, context: RenderContext
) -> None:
    import yaml

    catalog = reference_catalog(title='Cu "thin" film & oxide')
    apply_scaffold(project, plan_sample(project, context, "Sample6", catalog=catalog))

    register = yaml.safe_load(
        (project / "samples" / "Sample6" / "sample.yaml").read_text()
    )
    assert register["title"] == 'Cu "thin" film & oxide'


def test_sample_new_writes_the_sample_md_the_experiment_page_previews(
    project: Path, monkeypatch
) -> None:
    """One context for both: the command used to build its own, dropping fields."""
    from click.testing import CliRunner

    from nr_workbench.cli import main
    from nr_workbench.experiment.store import ParquetCatalogStore
    from nr_workbench.web.experiment import ExperimentData

    ParquetCatalogStore.for_project(project).update(
        runs=[
            RunChange(RunKey(218386), 0, {"sample_id": "Sample6", "condition": "OCV"})
        ],
        samples=[SampleChange("Sample6", 0, {"fits_to_perform": FITS})],
        now=NOW,
    )
    previewed = ExperimentData(project, autostart=False).sample_preview("Sample6")
    monkeypatch.chdir(project)

    result = CliRunner().invoke(main, ["sample", "new", "Sample6"])

    assert result.exit_code == 0, result.output
    assert "written from the experiment catalog" in result.output
    assert "nrw experiment apply --write" in result.output
    written = (project / "samples" / "Sample6" / "sample.md").read_text("utf-8")
    assert written == previewed["markdown"]


def test_a_row_in_a_note_is_no_row_of_the_measurement_table() -> None:
    from nr_workbench.conditions import from_table

    # A note line shaped like a table row, under Measurement conditions -- after
    # the table has ended.
    text = (
        "## Measurements\n\n| Run | Type | Condition |\n|---|---|---|\n"
        "| 218386 | steady | 25 C |\n\n## Measurement conditions\n\n"
        "- Run 218386: compare with\n  | 218999 | steady | 80 C |\n"
    )

    assert from_table(text, "218386") == "25 C"
    assert from_table(text, "218999") is None


def test_sample_md_pending_says_when_the_page_has_edits_not_yet_applied(
    project: Path,
) -> None:
    from nr_workbench.experiment.render import project_context, sample_md_pending
    from nr_workbench.experiment.store import ParquetCatalogStore

    store = ParquetCatalogStore.for_project(project)
    store.update(runs=[RunChange(RunKey(218386), 0, {"sample_id": "Sample6"})], now=NOW)
    apply_scaffold(project, plan_sample(project, project_context(project), "Sample6"))
    applied = sample_md_pending(project, "Sample6")

    entry = store.load().runs[RunKey(218386)]
    store.update(
        runs=[RunChange(entry.key, entry.rev, {"note": "realigned after mounting"})],
        now=NOW,
    )

    assert applied is False
    assert sample_md_pending(project, "Sample6") is True
    assert sample_md_pending(project, "NotInTheCatalog") is False
