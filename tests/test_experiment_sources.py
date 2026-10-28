"""The local data source and the folder feed, against real files.

The folder is shared, on NFS, and polled continuously, so the tests are about
what it must never do: follow a link, accept a name the reduction would not
have written, list twice per poll, re-read an unchanged header, or skip a file
it did not recognise without saying so.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nr_workbench.experiment.config import FeedConfig, SourceConfig
from nr_workbench.experiment.feeds import FeedUnavailableError, open_feed
from nr_workbench.experiment.feeds.directory import DirectoryFeed
from nr_workbench.experiment.model import RunKey
from nr_workbench.experiment.sources import (
    SourceChangedError,
    SourceUnavailableError,
    open_source,
)
from nr_workbench.experiment.sources import local as local_module
from nr_workbench.experiment.sources.local import LocalDirectorySource

from .experiment_fixtures import copy_reference, write_autoreduced


def source_for(folder: Path, ipts: str | None = "IPTS-34347") -> LocalDirectorySource:
    return LocalDirectorySource(folder, str(folder), ipts=ipts)


# --------------------------------------------------------------------------
# Listing the reference corpus
# --------------------------------------------------------------------------


def test_the_reference_corpus_lists_runs_not_subruns(tmp_path: Path) -> None:
    """REFL_218386_2_218387 names two numbers; 218387 is a subrun, not a run.

    At a beamline that numbers consecutively 218387 is also a real, separate
    run -- which is how a looser pattern once marked neighbours as fitted.
    """
    copy_reference(tmp_path)

    inventory = source_for(tmp_path).inventory()

    assert [key.run for key in inventory.runs] == [218386, 218393]
    assert inventory.reachable and inventory.problems == ()


def test_a_reference_run_carries_what_its_headers_say(tmp_path: Path) -> None:
    copy_reference(tmp_path)

    run = source_for(tmp_path).inventory().runs[RunKey(218386)]

    assert run.segments == (1, 2, 3)
    assert run.title == "CuPt_d8-THF_FullQ-218386-1."
    assert run.start_time.startswith("2025-04-20T13:42:43")
    assert run.experiment == "IPTS-34347"
    assert [round(t, 2) for t in run.thetas] == [0.45, 1.2, 3.5]
    # The `# Meta:` dialect does not say how many segments were planned.
    assert run.n_segments is None
    assert run.problems == ()


def test_a_file_from_another_experiment_is_a_problem(tmp_path: Path) -> None:
    copy_reference(tmp_path)

    run = source_for(tmp_path, ipts="IPTS-00001").inventory().runs[RunKey(218386)]

    assert any("IPTS-34347" in p and "another experiment" in p for p in run.problems)


# --------------------------------------------------------------------------
# The new_reduction dialect
# --------------------------------------------------------------------------


def test_a_new_reduction_run_reports_its_planned_segments(tmp_path: Path) -> None:
    write_autoreduced(tmp_path, 234277, [1], planned=3)

    run = source_for(tmp_path, ipts="IPTS-00001").inventory().runs[RunKey(234277)]

    assert run.segments == (1,)
    assert run.n_segments == 3
    assert run.title == "S1_air-234277-1."


def test_a_new_reduction_run_has_no_start_time_and_none_is_invented(
    tmp_path: Path,
) -> None:
    write_autoreduced(tmp_path, 234277, [1, 2, 3])

    run = source_for(tmp_path, ipts="IPTS-00001").inventory().runs[RunKey(234277)]

    assert run.start_time == ""


def test_the_same_segment_in_two_dialects_is_quarantined(tmp_path: Path) -> None:
    """A reprocess by the other pipeline leaves both; which to use is a choice."""
    copy_reference(tmp_path)
    write_autoreduced(tmp_path, 218386, [2], ipts="IPTS-34347")

    run = source_for(tmp_path).inventory().runs[RunKey(218386)]

    assert any("segment 2 is here twice" in p for p in run.problems)


def test_a_segment_beyond_the_plan_is_a_problem(tmp_path: Path) -> None:
    write_autoreduced(tmp_path, 234277, [1, 2, 3, 4], planned=3)

    run = source_for(tmp_path, ipts="IPTS-00001").inventory().runs[RunKey(234277)]

    assert any("plans only 3" in p for p in run.problems)


def test_segments_that_skip_one_are_a_problem(tmp_path: Path) -> None:
    write_autoreduced(tmp_path, 234277, [1, 3])

    run = source_for(tmp_path, ipts="IPTS-00001").inventory().runs[RunKey(234277)]

    assert any("not contiguous" in p for p in run.problems)


def test_headers_disagreeing_about_the_plan_are_quarantined(tmp_path: Path) -> None:
    """Which header to believe is a person's call, so neither sets the plan."""
    write_autoreduced(tmp_path, 234277, [1], planned=3)
    write_autoreduced(tmp_path, 234277, [2], planned=4)

    run = source_for(tmp_path, ipts="IPTS-00001").inventory().runs[RunKey(234277)]

    assert run.n_segments is None
    assert any("disagree" in p and "(3, 4)" in p for p in run.problems)


def test_an_unreadable_header_quarantines_the_run_and_names_the_file(
    tmp_path: Path,
) -> None:
    copy_reference(tmp_path)
    broken = tmp_path / "REFL_218393_2_218394_partial.txt"
    broken.write_text(
        "".join(
            "# Meta:{not json\n" if line.startswith("# Meta:") else line
            for line in broken.read_text().splitlines(keepends=True)
        )
    )

    inventory = source_for(tmp_path).inventory()

    problems = inventory.runs[RunKey(218393)].problems
    assert any(p.startswith(f"{broken.name}: ") and "JSON" in p for p in problems)
    assert inventory.runs[RunKey(218386)].problems == ()


# --------------------------------------------------------------------------
# What is reported rather than used
# --------------------------------------------------------------------------


def test_an_unrecognized_data_file_is_reported_not_skipped(tmp_path: Path) -> None:
    """A folder of files in an unknown dialect once looked exactly like an empty one."""
    (tmp_path / "REFL_234277_seg1_v3.dat").write_text("0.01 1 0.1 0.001\n")

    inventory = source_for(tmp_path).inventory()

    assert inventory.unrecognized == ("REFL_234277_seg1_v3.dat",)
    assert any("match no known" in p.message for p in inventory.problems)


def test_a_name_almost_like_a_reduced_file_is_reported_not_used(tmp_path: Path) -> None:
    (tmp_path / "REFL_0234277_1_234277_autoreduction.dat").write_text("x\n")

    inventory = source_for(tmp_path).inventory()

    assert inventory.runs == {}
    assert any("almost, but not" in p.message for p in inventory.problems)


def test_plots_and_sidecars_are_counted_not_reported(tmp_path: Path) -> None:
    write_autoreduced(tmp_path, 234277, [1])
    (tmp_path / "REFL_234277_plot.png").write_bytes(b"\x89PNG")
    (tmp_path / "REF_L_234277_auto_template.xml").write_text("<x/>")

    inventory = source_for(tmp_path, ipts="IPTS-00001").inventory()

    assert inventory.other_files == 2
    assert inventory.problems == ()


# --------------------------------------------------------------------------
# An unreachable location
# --------------------------------------------------------------------------


def test_a_missing_location_says_to_check_the_mount(tmp_path: Path) -> None:
    inventory = source_for(tmp_path / "not-mounted").inventory()

    assert not inventory.reachable
    assert "data mount" in inventory.problems[0].message


def test_no_configured_location_is_reported_not_raised() -> None:
    inventory = LocalDirectorySource(None, "", ipts=None).inventory()

    assert not inventory.reachable and inventory.problems


# --------------------------------------------------------------------------
# NFS economy
# --------------------------------------------------------------------------


def test_an_unchanged_folder_costs_one_listing_and_nothing_else(
    tmp_path: Path, monkeypatch
) -> None:
    """Counted at the operating system, not by a tally the source keeps itself.

    Every call here is a round trip to the file server, on a folder polled
    every few seconds by everyone watching the beamtime.
    """
    copy_reference(tmp_path)
    source = source_for(tmp_path)
    source.inventory()
    calls = {"scandir": 0, "stat": 0, "open": 0}

    def counted(name: str):
        real = getattr(os, name)

        def call(*args, **kwargs):
            calls[name] += 1
            return real(*args, **kwargs)

        return call

    for name in calls:
        monkeypatch.setattr(local_module.os, name, counted(name))

    source.inventory()

    assert calls == {"scandir": 1, "stat": 0, "open": 0}


def test_an_unchanged_header_is_not_read_again(tmp_path: Path, monkeypatch) -> None:
    from nr_workbench.instrument import header as header_module

    copy_reference(tmp_path)
    source = source_for(tmp_path)
    source.inventory()
    reads: list[Path] = []
    real = header_module.read_header_bytes
    monkeypatch.setattr(
        header_module,
        "read_header_bytes",
        lambda data, p: reads.append(p) or real(data, p),
    )

    source.inventory()
    assert reads == []

    changed = tmp_path / "REFL_218386_2_218387_partial.txt"
    os.utime(changed, (1_700_000_000, 1_700_000_000))
    source.inventory()
    assert [p.name for p in reads] == [changed.name]


def test_the_header_cache_is_bounded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(local_module, "HEADER_CACHE_SIZE", 2)
    copy_reference(tmp_path)
    source = source_for(tmp_path)

    source.inventory()

    assert len(source._headers) == 2


# --------------------------------------------------------------------------
# Reading bytes
# --------------------------------------------------------------------------


def test_read_bytes_returns_the_exact_file(tmp_path: Path) -> None:
    copy_reference(tmp_path)
    source = source_for(tmp_path)
    listed = source.inventory().runs[RunKey(218386)].files[0]

    data = source.read_bytes(listed)

    assert data == (tmp_path / listed.name).read_bytes()


def test_read_bytes_refuses_a_file_rewritten_since_it_was_listed(
    tmp_path: Path,
) -> None:
    copy_reference(tmp_path)
    source = source_for(tmp_path)
    listed = source.inventory().runs[RunKey(218386)].files[0]
    (tmp_path / listed.name).write_text("rewritten by the reduction\n")

    with pytest.raises(SourceChangedError):
        source.read_bytes(listed)


# --------------------------------------------------------------------------
# Registries
# --------------------------------------------------------------------------


def test_open_source_builds_the_local_folder(tmp_path: Path) -> None:
    source = open_source(SourceConfig(path=tmp_path, location=str(tmp_path)), ipts=None)

    assert source.kind == "local"


@pytest.mark.parametrize("kind,message", [("tiled", "not known"), ("s3", "not known")])
def test_open_source_refuses_rather_than_falling_back(kind: str, message: str) -> None:
    with pytest.raises(SourceUnavailableError, match=message):
        open_source(SourceConfig(kind=kind), ipts=None)


def test_open_feed_builds_the_folder_feed() -> None:
    assert isinstance(open_feed(FeedConfig()), DirectoryFeed)


@pytest.mark.parametrize("kind,message", [("monitor", "ORNL"), ("tiled", "not known")])
def test_open_feed_refuses_a_planned_or_unknown_feed(kind: str, message: str) -> None:
    with pytest.raises(FeedUnavailableError, match=message):
        open_feed(FeedConfig(kind=kind))


def test_the_folder_feed_announces_what_the_source_listed(tmp_path: Path) -> None:
    copy_reference(tmp_path)

    update = DirectoryFeed().poll(source_for(tmp_path).inventory())

    assert [a.run for a in update.announcements] == [218386, 218393]
    assert update.announcements[0].title == "CuPt_d8-THF_FullQ-218386-1."


# --------------------------------------------------------------------------
# A measurement: its segments, and its other artifacts
# --------------------------------------------------------------------------


def test_a_combined_curve_is_an_artifact_of_its_measurement_not_a_fitting_file(
    tmp_path: Path,
) -> None:
    from nr_workbench.instrument.reduced import COMBINED, SEGMENT, combined_filename

    from .experiment_fixtures import reduced_rows

    write_autoreduced(tmp_path, 234277, [1, 2, 3])
    (tmp_path / combined_filename(234277)).write_text(reduced_rows())

    run = source_for(tmp_path, ipts="IPTS-00001").inventory().runs[RunKey(234277)]

    assert [f.role for f in run.files] == [SEGMENT, SEGMENT, SEGMENT, COMBINED]
    assert [f.segment for f in run.fitting_files] == [1, 2, 3]
    assert run.thetas[-1] is None and len(run.thetas) == len(run.files)


def test_the_in_memory_source_lists_a_combined_curve_as_the_folder_does(
    tmp_path: Path,
) -> None:
    from .experiment_fixtures import InMemorySource

    memory = InMemorySource()
    memory.add_segments(234277, [1, 2, 3])
    memory.add_combined(234277)
    write_autoreduced(tmp_path, 234277, [1, 2, 3])
    from nr_workbench.instrument.reduced import combined_filename

    from .experiment_fixtures import reduced_rows

    (tmp_path / combined_filename(234277)).write_text(reduced_rows())

    def shape(inventory):
        run = inventory.runs[RunKey(234277)]
        return [(f.name, f.role) for f in run.files], [
            f.name for f in run.fitting_files
        ]

    local = source_for(tmp_path, ipts="IPTS-00001").inventory()
    assert shape(memory.inventory()) == shape(local)
