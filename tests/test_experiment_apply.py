"""Apply: the one step that writes the catalog into samples/<id>/.

It writes into a directory a scientist and an agent are both working in, so
each test here pins one way it could write something it should not, or fail to
say that it did not.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from nr_workbench.experiment import apply as apply_module
from nr_workbench.experiment.apply import (
    Action,
    ApplyBusy,
    ApplyRefused,
    PlanChanged,
    apply,
    plan_apply,
    read_sources,
)
from nr_workbench.experiment.feeds.directory import DirectoryFeed
from nr_workbench.experiment.live import LiveInventory
from nr_workbench.experiment.model import (
    Catalog,
    RunChange,
    RunKey,
    SampleChange,
    apply_changes,
)
from nr_workbench.experiment.sources.local import LocalDirectorySource
from nr_workbench.project.render import RenderContext
from nr_workbench.project.scaffold import Outcome

from .experiment_fixtures import InMemorySource, write_autoreduced

NOW = "2026-09-25T12:00:00Z"
RUNS = (234277, 234280)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def catalog_for(*runs: int, sample: str = "Sample6", **run_fields) -> Catalog:
    fields = {"sample_id": sample, "measurement": "full Q", "condition": "OCV"}
    fields.update(run_fields)
    return apply_changes(
        Catalog(),
        runs=[RunChange(RunKey(run), 0, dict(fields)) for run in runs],
        samples=[SampleChange(sample, 0, {"fits_to_perform": "Fit both."})],
        now=NOW,
    )


def edit(catalog: Catalog, run: int, **changes) -> Catalog:
    entry = catalog.runs[RunKey(run)]
    return apply_changes(
        catalog, runs=[RunChange(RunKey(run), entry.rev, changes)], now=NOW
    )


def observe(source) -> tuple[dict, dict]:
    """Poll once with every file long settled; return (runs, statuses)."""
    live = LiveInventory(
        source,
        DirectoryFeed(),
        settle_seconds=300,
        poll_seconds=30,
        clock=lambda: time.time() + 10_000,
        autostart=False,
    )
    snapshot = live.scan_once()
    runs = {k: v.source for k, v in snapshot.runs.items() if v.source is not None}
    return runs, {k: v.status for k, v in snapshot.runs.items()}


#: The run measured after RUNS. Its reduced file is the evidence that they
#: have finished; nothing in these tests assigns it.
NEXT_RUN = 234290


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    """A facility folder with two complete new_reduction runs, and the next one."""
    reduced = tmp_path / "facility" / "new_reduction"
    for run in RUNS:
        write_autoreduced(reduced, run, [1, 2, 3], planned=3, mtime=time.time() - 3600)
    write_autoreduced(reduced, NEXT_RUN, [1], planned=3, mtime=time.time() - 3600)
    return reduced


@pytest.fixture
def source(folder: Path) -> LocalDirectorySource:
    return LocalDirectorySource(folder, str(folder), ipts="IPTS-00001")


class Applier:
    """Plan and apply against one project, source and catalog."""

    def __init__(self, project: Path, context: RenderContext, source) -> None:
        self.project, self.context, self.source = project, context, source

    def plan(self, catalog: Catalog, **kwargs):
        runs, statuses = observe(self.source)
        return plan_apply(
            self.project, catalog, runs, statuses, self.source, self.context, **kwargs
        )

    def apply(self, catalog: Catalog, **kwargs):
        plan = self.plan(catalog, **kwargs)
        runs, statuses = observe(self.source)
        return plan, apply(
            self.project,
            catalog,
            runs,
            statuses,
            self.source,
            self.context,
            expected_plan_id=plan.plan_id,
            **kwargs,
        )


@pytest.fixture
def applier(project: Path, context: RenderContext, source) -> Applier:
    return Applier(project, context, source)


def folder_of(applier: Applier) -> Path:
    return Path(applier.source.path)


def steady(project: Path, sample: str = "Sample6") -> Path:
    return project / "samples" / sample / "data" / "steady"


def actions(plan, sample: str = "Sample6") -> dict[str, Action]:
    chosen = next(s for s in plan.samples if s.sample_id == sample)
    return {f.name: f.action for f in chosen.files}


# --------------------------------------------------------------------------
# The ordinary case
# --------------------------------------------------------------------------


def test_apply_copies_complete_runs_and_renders_sample_md(
    applier: Applier, project: Path, folder: Path
) -> None:
    catalog = catalog_for(*RUNS)

    plan, report = applier.apply(catalog)

    names = sorted(p.name for p in folder.iterdir() if str(NEXT_RUN) not in p.name)
    assert sorted(p.name for p in steady(project).glob("REFL_*")) == names
    for name in names:
        assert (steady(project) / name).read_bytes() == (folder / name).read_bytes()
    assert plan.samples[0].sample_md is Outcome.CREATE
    text = (project / "samples" / "Sample6" / "sample.md").read_text()
    assert "| 234277 | full Q | OCV |" in text and "Fit both." in text
    assert set(read_sources(project / "samples" / "Sample6")) == set(names)
    assert not report.failed


def test_copies_are_dated_now_not_when_the_facility_wrote_them(
    applier: Applier, project: Path
) -> None:
    """A copy dated an hour ago looks settled to `nrw agent watch` on arrival."""
    applier.apply(catalog_for(*RUNS))

    for path in steady(project).glob("REFL_*"):
        assert time.time() - path.stat().st_mtime < 60


def test_apply_twice_second_run_writes_nothing(applier: Applier, project: Path) -> None:
    catalog = catalog_for(*RUNS)
    applier.apply(catalog)

    def stamp(path: Path) -> tuple[int, int]:
        # The inode as well: os.replace makes a new one even within the second
        # a coarse filesystem clock cannot tell apart.
        info = path.stat()
        return (info.st_mtime_ns, info.st_ino)

    stamps = {
        p: stamp(p)
        for p in [*project.rglob("*")]
        if p.is_file() and ".nrw/cache" not in p.as_posix()
    }

    plan, report = applier.apply(catalog)

    assert not plan.writes
    assert set(actions(plan).values()) == {Action.UNCHANGED}
    assert report.done == []
    assert {p: stamp(p) for p in stamps} == stamps


def test_downstream_scan_reads_the_applied_sample(
    applier: Applier, project: Path
) -> None:
    from nr_workbench.project.scan import scan_sample

    applier.apply(catalog_for(*RUNS))

    scan = scan_sample(project, "Sample6")
    assert sorted(scan.steady) == list(RUNS)
    assert scan.documented_but_absent == [] and scan.present_but_undocumented == []


# --------------------------------------------------------------------------
# Only complete runs
# --------------------------------------------------------------------------


def test_apply_defers_an_unconfirmed_run_until_a_person_confirms_it(
    applier: Applier, project: Path, folder: Path
) -> None:
    write_autoreduced(folder, 234290, [1], planned=3, mtime=time.time() - 3600)
    catalog = catalog_for(234290)

    plan = applier.plan(catalog)
    assert set(actions(plan).values()) == {Action.DEFERRED}
    assert "1 of 3" in plan.samples[0].files[0].detail

    _, report = applier.apply(catalog, confirmed={RunKey(234290)})
    assert [a.name for a in report.done] == ["REFL_234290_1_234290_autoreduction.dat"]


def test_confirmation_does_not_override_a_quarantine(
    applier: Applier, folder: Path
) -> None:
    write_autoreduced(folder, 234290, [1, 3], planned=3, mtime=time.time() - 3600)

    plan = applier.plan(catalog_for(234290), confirmed={RunKey(234290)})

    assert set(actions(plan).values()) == {Action.DEFERRED}
    assert "quarantined" in plan.samples[0].files[0].detail


def test_apply_rechecks_what_it_would_do_before_writing(
    applier: Applier, project: Path, folder: Path, context: RenderContext, source
) -> None:
    catalog = catalog_for(*RUNS)
    plan = applier.plan(catalog)
    # A segment is rewritten by the reduction between review and apply.
    (folder / "REFL_234277_2_234278_autoreduction.dat").write_text(
        "# rewritten\n1 2 3\n"
    )
    os.utime(folder / "REFL_234277_2_234278_autoreduction.dat", (1, 1))

    runs, statuses = observe(source)
    with pytest.raises(PlanChanged):
        apply(
            project,
            catalog,
            runs,
            statuses,
            source,
            context,
            expected_plan_id=plan.plan_id,
        )
    assert not steady(project).exists()


# --------------------------------------------------------------------------
# Never overwrite
# --------------------------------------------------------------------------


def test_apply_source_rereduced_after_copy_is_reported_not_replaced(
    applier: Applier, project: Path, folder: Path
) -> None:
    catalog = catalog_for(*RUNS)
    applier.apply(catalog)
    name = "REFL_234277_1_234277_autoreduction.dat"
    copied = (steady(project) / name).read_bytes()
    (folder / name).write_text((folder / name).read_text() + "0.3 1e-9 1e-10 0.001\n")

    plan, _ = applier.apply(catalog)

    assert actions(plan)[name] is Action.SOURCE_CHANGED
    assert (steady(project) / name).read_bytes() == copied


def test_a_touched_but_identical_source_file_is_not_a_change(
    applier: Applier, project: Path, folder: Path
) -> None:
    catalog = catalog_for(*RUNS)
    applier.apply(catalog)
    name = "REFL_234277_1_234277_autoreduction.dat"
    os.utime(folder / name, (time.time() - 100, time.time() - 100))

    plan = applier.plan(catalog)

    assert actions(plan)[name] is Action.RECORD


def test_apply_a_local_edit_to_a_copy_is_left_alone(
    applier: Applier, project: Path
) -> None:
    catalog = catalog_for(*RUNS)
    applier.apply(catalog)
    name = "REFL_234280_3_234282_autoreduction.dat"
    (steady(project) / name).write_text("# edited by hand\n")

    plan, _ = applier.apply(catalog)

    assert actions(plan)[name] is Action.LOCAL_EDITED
    assert (steady(project) / name).read_text() == "# edited by hand\n"


def test_a_different_hand_copied_file_is_a_conflict_left_alone(
    applier: Applier, project: Path
) -> None:
    steady(project).mkdir(parents=True)
    name = "REFL_234277_1_234277_autoreduction.dat"
    (steady(project) / name).write_text("# someone else's file\n")

    plan, _ = applier.apply(catalog_for(234277))

    assert actions(plan)[name] is Action.CONFLICT
    assert (steady(project) / name).read_text() == "# someone else's file\n"


def test_an_identical_hand_copied_file_is_recorded_not_copied(
    applier: Applier, project: Path, folder: Path
) -> None:
    steady(project).mkdir(parents=True)
    name = "REFL_234277_1_234277_autoreduction.dat"
    (steady(project) / name).write_bytes((folder / name).read_bytes())

    plan, _ = applier.apply(catalog_for(234277))

    assert actions(plan)[name] is Action.RECORD
    assert name in read_sources(project / "samples" / "Sample6")


def test_apply_destination_symlink_target_not_modified(
    applier: Applier, project: Path, tmp_path: Path
) -> None:
    """`nrw import` symlinks data; writing through one writes the facility's file."""
    name = "REFL_234277_1_234277_autoreduction.dat"
    # The same bytes as the source, so a digest comparison would call it a
    # match and record it: only the link check stands between them.
    outside = tmp_path / "facility-file.dat"
    outside.write_bytes((folder_of(applier) / name).read_bytes())
    steady(project).mkdir(parents=True)
    (steady(project) / name).symlink_to(outside)

    plan, _ = applier.apply(catalog_for(234277))

    chosen = next(f for f in plan.samples[0].files if f.name == name)
    assert chosen.action is Action.CONFLICT and "symbolic link" in chosen.detail
    assert name not in read_sources(project / "samples" / "Sample6")


# --------------------------------------------------------------------------
# A run is copied whole or not at all
# --------------------------------------------------------------------------


def test_apply_copy_fails_midway_no_partial_run_visible(
    project: Path, context: RenderContext
) -> None:
    source = InMemorySource(fail_reads_after=1)
    source.add_segments(234277, [1, 2, 3], planned=3, mtime=time.time() - 3600)
    source.add_segments(NEXT_RUN, [1], planned=3, mtime=time.time() - 3600)
    applier = Applier(project, context, source)

    _, report = applier.apply(catalog_for(234277))

    assert not any(steady(project).glob("REFL_*"))
    assert len(report.failed) == 3
    assert "none of its files" in report.failed[0].detail
    assert not any((project / "samples" / "Sample6" / "data").glob(".nrw-staging-*"))


def test_apply_a_source_file_that_is_not_reduced_data_is_refused(
    project: Path, context: RenderContext
) -> None:
    source = InMemorySource()
    source.add_segments(234277, [1, 2, 3], planned=3, mtime=time.time() - 3600)
    source.add_segments(NEXT_RUN, [1], planned=3, mtime=time.time() - 3600)
    source.files[
        "REFL_234277_2_234278_autoreduction.dat"
    ].data = b"<html>not data</html>"
    applier = Applier(project, context, source)

    _, report = applier.apply(catalog_for(234277))

    assert not any(steady(project).glob("REFL_*"))
    assert "not reduced data" in report.failed[0].detail


# --------------------------------------------------------------------------
# Exclusion is real
# --------------------------------------------------------------------------


def test_an_excluded_run_moves_out_of_every_readers_way_and_back(
    applier: Applier, project: Path
) -> None:
    from nr_workbench.project.scan import scan_sample

    catalog = catalog_for(*RUNS)
    applier.apply(catalog)

    excluded = edit(catalog, 234280, include=False)
    plan, _ = applier.apply(excluded)
    assert {a for n, a in actions(plan).items() if "234280" in n} == {Action.MOVE_OUT}
    assert sorted(scan_sample(project, "Sample6").steady) == [234277]
    assert (project / "samples/Sample6/data/excluded/234280").is_dir()

    restored = edit(excluded, 234280, include=True)
    plan, _ = applier.apply(restored)
    assert {a for n, a in actions(plan).items() if "234280" in n} == {Action.RESTORE}
    assert sorted(scan_sample(project, "Sample6").steady) == list(RUNS)


def test_applying_again_after_an_exclusion_is_a_no_op(
    applier: Applier, project: Path
) -> None:
    """A copy already moved aside is not moved again.

    Planning the move a second time finds its target taken and reports a
    failure -- on every apply from then on, so ``--write`` never exits 0.
    """
    catalog = catalog_for(*RUNS)
    applier.apply(catalog)
    excluded = edit(catalog, 234280, include=False)
    applier.apply(excluded)

    plan, report = applier.apply(excluded)

    assert not plan.writes
    assert report.failed == []
    assert not any(f.run == 234280 for f in plan.samples[0].files)


def test_a_key_a_newer_nrw_wrote_in_the_copy_record_survives_a_rewrite(
    applier: Applier, project: Path
) -> None:
    """The record is committed and shared; an older nrw must not strip it."""
    catalog = catalog_for(*RUNS)
    applier.apply(catalog)
    path = project / "samples" / "Sample6" / "data" / "sources.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    name = next(n for n in document["files"] if "234280" in n)
    document["files"][name]["verified_by"] = "a newer nrw"
    path.write_text(json.dumps(document), encoding="utf-8")

    applier.apply(edit(catalog, 234280, include=False))

    entry = json.loads(path.read_text(encoding="utf-8"))["files"][name]
    assert entry["location"] == "excluded"
    assert entry["verified_by"] == "a newer nrw"


def test_a_reassigned_run_leaves_the_old_sample_and_reaches_the_new(
    applier: Applier, project: Path
) -> None:
    catalog = catalog_for(*RUNS)
    applier.apply(catalog)

    moved = edit(catalog, 234280, sample_id="Sample7")
    plan, _ = applier.apply(moved)

    old = actions(plan, "Sample6")
    assert {a for n, a in old.items() if "234280" in n} == {Action.MOVE_OUT}
    assert any("assigned to Sample7" in f.detail for f in plan.samples[0].files)
    assert len(list(steady(project, "Sample7").glob("REFL_234280_*"))) == 3
    assert not list(steady(project, "Sample6").glob("REFL_234280_*"))


def test_a_hand_copied_file_of_an_unassigned_run_is_reported_not_moved(
    applier: Applier, project: Path
) -> None:
    applier.apply(catalog_for(234277))
    stray = steady(project) / "REFL_218386_1_218386_partial.txt"
    stray.write_text("# copied in by hand\n")

    plan, _ = applier.apply(catalog_for(234277))

    assert actions(plan)[stray.name] is Action.NOT_MANAGED
    assert stray.is_file()


# --------------------------------------------------------------------------
# sample.md
# --------------------------------------------------------------------------


def test_a_hand_edited_sample_md_gets_a_nrw_new_beside_it(
    applier: Applier, project: Path
) -> None:
    catalog = catalog_for(*RUNS)
    applier.apply(catalog)
    path = project / "samples" / "Sample6" / "sample.md"
    edited = path.read_text() + "\nMy own line.\n"
    path.write_text(edited)

    changed = edit(catalog, 234277, condition="CA")
    plan, _ = applier.apply(changed)

    assert plan.samples[0].sample_md is Outcome.DRIFTED
    assert "nrw-new" in plan.samples[0].sample_md_detail
    assert path.read_text() == edited
    assert "| 234277 | full Q | CA |" in (path.parent / "sample.md.nrw-new").read_text()


def test_an_untracked_sample_md_is_named_not_silently_skipped(
    applier: Applier, project: Path
) -> None:
    """An imported sample.md is left alone -- and the plan must say so."""
    sample = project / "samples" / "Sample6"
    sample.mkdir(parents=True)
    (sample / "sample.md").write_text("# Sample6\n\nWritten by hand.\n")

    plan = applier.plan(catalog_for(*RUNS))

    assert plan.samples[0].sample_md is Outcome.UNTRACKED
    assert "Adopt" in plan.samples[0].sample_md_detail


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_apply_a_changed_plan_id_refuses(
    applier: Applier, project: Path, context, source
) -> None:
    runs, statuses = observe(source)

    with pytest.raises(PlanChanged):
        apply(
            project,
            catalog_for(*RUNS),
            runs,
            statuses,
            source,
            context,
            expected_plan_id="not-the-plan",
        )


def test_apply_a_second_concurrent_apply_is_busy(
    applier: Applier, project, context, source
) -> None:
    runs, statuses = observe(source)
    plan = applier.plan(catalog_for(*RUNS))

    assert apply_module._APPLY_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(ApplyBusy):
            apply(
                project,
                catalog_for(*RUNS),
                runs,
                statuses,
                source,
                context,
                expected_plan_id=plan.plan_id,
            )
    finally:
        apply_module._APPLY_LOCK.release()


def test_apply_conflict_markers_in_the_scaffold_lock_refuse_and_leave_it(
    applier: Applier, project: Path
) -> None:
    """A partial write over a lock that failed to load drops every other entry."""
    lock = project / ".nrw" / "scaffold.lock.json"
    damaged = "<<<<<<< HEAD\n" + lock.read_text() + "=======\n>>>>>>> other\n"
    lock.write_text(damaged)

    plan = applier.plan(catalog_for(*RUNS))

    assert any("conflict markers" in p.message for p in plan.problems)
    with pytest.raises(ApplyRefused):
        applier.apply(catalog_for(*RUNS))
    assert lock.read_text() == damaged


def test_a_sample_directory_differing_only_in_case_is_refused(
    applier: Applier, project: Path
) -> None:
    (project / "samples" / "sample6").mkdir(parents=True)

    plan = applier.plan(catalog_for(*RUNS))

    assert "only in case" in plan.samples[0].problems[0].message


def test_an_unreadable_copy_record_stops_that_sample_only(
    applier: Applier, project: Path
) -> None:
    catalog = apply_changes(
        catalog_for(234277),
        runs=[RunChange(RunKey(234280), 0, {"sample_id": "Sample7"})],
        now=NOW,
    )
    record = project / "samples" / "Sample6" / "data" / "sources.json"
    record.parent.mkdir(parents=True)
    record.write_text("{not json")

    plan = applier.plan(catalog)

    by_sample = {s.sample_id: s for s in plan.samples}
    assert by_sample["Sample6"].problems
    assert not by_sample["Sample7"].problems


def test_a_plan_reviewed_in_one_second_applies_in_the_next(
    project: Path, source, monkeypatch
) -> None:
    """A new sample's sample.yaml is stamped with the current second.

    Found by driving the page in a browser: review, then click Apply a second
    later, and the plan "changed" -- because its digest hashed that timestamp.
    """
    import datetime as real

    from nr_workbench.project import render as render_module

    ticks = iter(range(100))

    class Clock(real.datetime):
        @classmethod
        def now(cls, tz=None):
            return real.datetime(2026, 9, 25, 12, 0, next(ticks), tzinfo=tz)

    monkeypatch.setattr(render_module, "datetime", Clock)
    # The context the page and the CLI use: no fixed `created`, so every
    # render of a new sample's sample.yaml asks the clock.
    from nr_workbench.experiment.render import project_context

    context = project_context(project)
    # The precondition: if the timestamp stops coming through this module's
    # `datetime`, the patch stops biting and this test stops testing anything.
    from nr_workbench.experiment.render import plan_sample

    def register() -> bytes:
        return next(
            f.content
            for f in plan_sample(project, context, "Sample6", catalog=catalog)
            if f.relpath.endswith("sample.yaml")
        )

    catalog = catalog_for(*RUNS)
    assert register() != register()

    applier = Applier(project, context, source)
    plan = applier.plan(catalog)
    runs, statuses = observe(source)

    report = apply(
        project, catalog, runs, statuses, source, context, expected_plan_id=plan.plan_id
    )

    assert len(report.done) == 6


# --------------------------------------------------------------------------
# Never overwrite, on every path
# --------------------------------------------------------------------------


def test_restoring_a_run_never_overwrites_a_file_put_back_by_hand(
    applier: Applier, project: Path
) -> None:
    catalog = catalog_for(*RUNS)
    applier.apply(catalog)
    excluded = edit(catalog, 234280, include=False)
    applier.apply(excluded)
    name = "REFL_234280_1_234280_autoreduction.dat"
    (steady(project) / name).write_text("# put back by hand\n")

    _, report = applier.apply(edit(excluded, 234280, include=True))

    assert (steady(project) / name).read_text() == "# put back by hand\n"
    assert any(f.name == name and "already exists" in f.detail for f in report.failed)


def test_a_file_appearing_between_review_and_apply_is_not_overwritten(
    applier: Applier, project: Path, context: RenderContext, source
) -> None:
    """The window a plan cannot see: a hand copy made after review."""
    catalog = catalog_for(234277)
    plan = applier.plan(catalog)
    runs, statuses = observe(source)
    name = "REFL_234277_2_234278_autoreduction.dat"
    steady(project).mkdir(parents=True, exist_ok=True)

    original = apply_module._place

    def racing_place(part: Path, target: Path) -> None:
        if target.name == name:
            target.write_text("# arrived meanwhile\n")
        original(part, target)

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(apply_module, "_place", racing_place)
        report = apply(
            project,
            catalog,
            runs,
            statuses,
            source,
            context,
            expected_plan_id=plan.plan_id,
        )

    assert (steady(project) / name).read_text() == "# arrived meanwhile\n"
    # Whole or not at all: none of the run's files stayed.
    assert not list(steady(project).glob("REFL_234277_1_*"))
    assert all(f.run == 234277 for f in report.failed) and len(report.failed) == 3


def test_a_rename_failing_midway_leaves_no_partial_run(
    applier: Applier, project: Path, monkeypatch
) -> None:
    calls = {"n": 0}
    original = apply_module._place

    def failing(part: Path, target: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        original(part, target)

    monkeypatch.setattr(apply_module, "_place", failing)

    _, report = applier.apply(catalog_for(234277))

    assert not list(steady(project).glob("REFL_234277_*"))
    assert len(report.failed) == 3 and "disk full" in report.failed[0].detail
    assert not read_sources(project / "samples" / "Sample6")


# --------------------------------------------------------------------------
# The beamline's normal failures
# --------------------------------------------------------------------------


def test_with_the_source_gone_copies_stay_and_missing_runs_are_named(
    applier: Applier, project: Path, folder: Path
) -> None:
    catalog = catalog_for(234277)
    applier.apply(catalog)
    # 234280 is assigned after the mount went away: it was never copied.
    catalog = apply_changes(
        catalog, runs=[RunChange(RunKey(234280), 0, {"sample_id": "Sample6"})], now=NOW
    )
    folder.rename(folder.with_name("unmounted"))

    plan = applier.plan(catalog)

    kinds = {f.name: f.action for f in plan.samples[0].files}
    assert {a for n, a in kinds.items() if "234277" in n} == {Action.UNCHANGED}
    assert kinds["run 234280"] is Action.SOURCE_MISSING
    assert Action.MOVE_OUT not in kinds.values()


def test_a_copy_deleted_by_hand_is_a_conflict_not_a_silent_recopy(
    applier: Applier, project: Path
) -> None:
    catalog = catalog_for(234277)
    applier.apply(catalog)
    name = "REFL_234277_2_234278_autoreduction.dat"
    (steady(project) / name).unlink()

    plan, _ = applier.apply(catalog)

    assert actions(plan)[name] is Action.CONFLICT
    assert not (steady(project) / name).exists()


def test_an_unassigned_copy_moves_out_as_no_longer_assigned(
    applier: Applier, project: Path
) -> None:
    catalog = catalog_for(*RUNS)
    applier.apply(catalog)

    plan, _ = applier.apply(edit(catalog, 234280, sample_id=None))

    for_run = [f for f in plan.samples[0].files if f.run == 234280]
    # Each file once, as a move: not also "not copied by nrw; left alone",
    # which would contradict the move on the review page.
    assert [f.action for f in for_run] == [Action.MOVE_OUT] * 3
    assert "no longer assigned" in for_run[0].detail
    assert not list(steady(project).glob("REFL_234280_*"))


def test_one_broken_sample_does_not_stop_the_others(
    applier: Applier, project: Path
) -> None:
    catalog = apply_changes(
        catalog_for(234277),
        runs=[RunChange(RunKey(234280), 0, {"sample_id": "Sample7"})],
        now=NOW,
    )
    record = project / "samples" / "Sample6" / "data" / "sources.json"
    record.parent.mkdir(parents=True)
    record.write_text("{not json")

    applier.apply(catalog)

    assert len(list(steady(project, "Sample7").glob("REFL_234280_*"))) == 3
    assert record.read_text() == "{not json"
    assert not list(steady(project).glob("REFL_*"))


# --------------------------------------------------------------------------
# A measurement and its artifacts: apply copies what a fit reads
# --------------------------------------------------------------------------


def settled_file(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    old = time.time() - 3600
    os.utime(path, (old, old))
    return path


def test_apply_copies_the_segments_and_leaves_the_combined_curve_at_the_source(
    applier: Applier, project: Path
) -> None:
    """A fit co-refines the segments; the stitched curve is not the sample's data."""
    from nr_workbench.instrument.reduced import combined_filename

    from .experiment_fixtures import reduced_rows

    combined = settled_file(
        folder_of(applier) / combined_filename(234277), reduced_rows()
    )

    plan, report = applier.apply(catalog_for(234277))

    copied = sorted(p.name for p in steady(project).glob("REFL_*"))
    assert copied == [
        "REFL_234277_1_234277_autoreduction.dat",
        "REFL_234277_2_234278_autoreduction.dat",
        "REFL_234277_3_234279_autoreduction.dat",
    ]
    assert combined.name not in actions(plan)
    assert report.failed == []


def test_a_measurement_with_only_a_combined_curve_copies_that(
    applier: Applier, project: Path
) -> None:
    """Nothing else to fit, so the combined curve is what `nrw model new` fits."""
    from nr_workbench.instrument.reduced import combined_filename

    from .experiment_fixtures import reduced_rows

    only = settled_file(folder_of(applier) / combined_filename(234286), reduced_rows())

    applier.apply(catalog_for(234286))

    assert [p.name for p in steady(project).glob("REFL_*")] == [only.name]


def test_a_combined_curve_copied_before_stays_managed(
    applier: Applier, project: Path
) -> None:
    """One an earlier nrw copied is still checked, not dropped from the record."""
    from nr_workbench.instrument.reduced import combined_filename

    from .experiment_fixtures import reduced_rows

    applier.apply(catalog_for(234277))
    name = combined_filename(234277)
    settled_file(folder_of(applier) / name, reduced_rows())
    (steady(project) / name).write_text(reduced_rows(), encoding="utf-8")
    record = project / "samples" / "Sample6" / "data" / "sources.json"
    document = json.loads(record.read_text(encoding="utf-8"))
    document["files"][name] = {
        "run": 234277,
        "sha256": "0" * 64,  # not what is there now: an edited copy
    }
    record.write_text(json.dumps(document), encoding="utf-8")

    plan = applier.plan(catalog_for(234277))

    assert actions(plan)[name] is Action.LOCAL_EDITED
