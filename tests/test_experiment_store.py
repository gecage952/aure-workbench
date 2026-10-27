"""The parquet catalog store: nothing a person decided may be lost quietly.

Each test here is one way the catalog could lose the experimenter's decisions
without anyone noticing: a damaged file read as empty, an interrupted save
read as complete, a binary merge conflict read as resolved, two writers
overwriting each other, or an older nrw dropping columns it does not know.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from nr_workbench.experiment import store as store_module
from nr_workbench.experiment.model import (
    ExperimentChange,
    ExperimentContext,
    RecordConflict,
    RunChange,
    RunKey,
    SampleChange,
)
from nr_workbench.experiment.store import (
    EXPERIMENT_FILE,
    MANIFEST_FILE,
    RUNS_FILE,
    SAMPLES_FILE,
    CatalogCorruptError,
    CatalogError,
    CatalogUnmergedError,
    CatalogVersionError,
    ParquetCatalogStore,
)

NOW = "2026-09-25T12:00:00Z"


@pytest.fixture
def store(tmp_path: Path) -> ParquetCatalogStore:
    return ParquetCatalogStore(tmp_path / "experiment", tmp_path / "cache")


def assign(store: ParquetCatalogStore, run: int, base_rev: int = 0, **changes):
    return store.update(runs=[RunChange(RunKey(run), base_rev, changes)], now=NOW)


def populated(store: ParquetCatalogStore) -> ParquetCatalogStore:
    assign(store, 218386, sample_id="Sample6", condition="OCV", title="CuPt-218386-1.")
    store.update(
        samples=[SampleChange("Sample6", 0, {"title": "Cu/Pt", "mounting": "once"})],
        now=NOW,
    )
    return store


# --------------------------------------------------------------------------
# Round trip
# --------------------------------------------------------------------------


def test_load_before_anything_is_written_is_an_empty_catalog(store) -> None:
    catalog = store.load()

    assert catalog.runs == {} and catalog.samples == {}
    assert not store.exists()


def test_round_trip_preserves_every_field(store) -> None:
    populated(store)

    catalog = ParquetCatalogStore(store.directory, store.cache_dir).load()

    entry = catalog.runs[RunKey(218386)]
    assert (entry.sample_id, entry.condition, entry.title, entry.rev) == (
        "Sample6",
        "OCV",
        "CuPt-218386-1.",
        1,
    )
    assert entry.include is True
    context = catalog.samples["Sample6"]
    assert (context.title, context.mounting, context.rev) == ("Cu/Pt", "once", 1)


def test_the_manifest_records_what_was_written(store) -> None:
    populated(store)

    manifest = json.loads((store.directory / MANIFEST_FILE).read_text())

    assert manifest["schema"] == "nrw-experiment/1"
    assert manifest["tables"]["runs"]["rows"] == 1
    assert manifest["tables"]["samples"]["rows"] == 1


def test_tables_carry_their_schema_and_name(store) -> None:
    import pyarrow.parquet as pq

    populated(store)

    metadata = pq.read_schema(store.directory / RUNS_FILE).metadata
    assert metadata[b"nrw.schema"] == b"nrw-experiment/1"
    assert metadata[b"nrw.table"] == b"runs"


def test_no_absolute_path_is_stored(store) -> None:
    """`nrw check` greps committed files for home paths, but skips binaries."""
    populated(store)

    for name in (RUNS_FILE, SAMPLES_FILE, MANIFEST_FILE):
        data = (store.directory / name).read_bytes()
        assert str(store.directory).encode() not in data
        assert b"/Users/" not in data and b"/home/" not in data


# --------------------------------------------------------------------------
# A damaged catalog is never an empty one
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(lambda data: b"", id="zero_bytes"),
        pytest.param(lambda data: data[: len(data) // 2], id="truncated"),
        pytest.param(lambda data: b"not parquet at all", id="garbage"),
    ],
)
def test_load_a_damaged_table_raises_rather_than_loading_empty(store, damage) -> None:
    populated(store)
    path = store.directory / RUNS_FILE
    path.write_bytes(damage(path.read_bytes()))
    # Keep the manifest consistent with the damage, so the only thing standing
    # between the damage and an empty catalog is the table read itself.
    manifest = json.loads((store.directory / MANIFEST_FILE).read_text())
    manifest["tables"]["runs"]["sha256"] = store_module._sha(path.read_bytes())
    (store.directory / MANIFEST_FILE).write_text(json.dumps(manifest))

    with pytest.raises(CatalogCorruptError):
        store.load()


def test_corrupt_error_is_not_a_value_error() -> None:
    """pyarrow's ArrowInvalid IS a ValueError; this must not be catchable as one."""
    assert not issubclass(CatalogCorruptError, ValueError)
    assert issubclass(CatalogCorruptError, CatalogError)


def test_update_on_a_damaged_catalog_refuses_and_leaves_it_alone(store) -> None:
    populated(store)
    (store.directory / RUNS_FILE).write_bytes(b"")
    before = {p.name: p.read_bytes() for p in store.directory.iterdir()}

    with pytest.raises(CatalogError):
        assign(store, 218393, sample_id="Sample6")

    assert {p.name: p.read_bytes() for p in store.directory.iterdir()} == before


def test_a_table_swapped_for_the_other_is_refused(store) -> None:
    populated(store)
    shutil.copy(store.directory / SAMPLES_FILE, store.directory / RUNS_FILE)
    (store.directory / MANIFEST_FILE).unlink()

    with pytest.raises(CatalogCorruptError, match="not the nrw runs table"):
        store.load()


def test_a_row_with_the_wrong_type_is_refused(store) -> None:
    """A table edited by hand (say, with pandas) can carry include="false"."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    populated(store)
    path = store.directory / RUNS_FILE
    table = pq.read_table(path)
    index = table.schema.get_field_index("include")
    table = table.set_column(index, "include", pa.array(["false"]))
    pq.write_table(table, path)
    (store.directory / MANIFEST_FILE).unlink()

    with pytest.raises(CatalogCorruptError, match="include"):
        store.load()


def test_missing_manifest_loads_the_tables_and_says_so(store) -> None:
    populated(store)
    (store.directory / MANIFEST_FILE).unlink()

    catalog, problems = store.load_report()

    assert RunKey(218386) in catalog.runs
    assert any("manifest" in p.message for p in problems)


# --------------------------------------------------------------------------
# Interrupted saves
# --------------------------------------------------------------------------


def _crash_on_write_number(monkeypatch, n: int) -> None:
    """Make the n-th file replacement of the next save raise, as a crash would."""
    real = store_module._replace
    calls = {"count": 0}

    def replace(target, data, scratch):
        calls["count"] += 1
        if calls["count"] == n:
            raise OSError("simulated crash")
        return real(target, data, scratch)

    monkeypatch.setattr(store_module, "_replace", replace)


def test_a_crash_between_the_two_tables_recovers_the_last_complete_catalog(
    store, monkeypatch
) -> None:
    populated(store)
    _crash_on_write_number(monkeypatch, 2)  # runs replaced, samples not

    with pytest.raises(OSError, match="simulated crash"):
        store.update(
            runs=[RunChange(RunKey(218386), 1, {"condition": "CA"})],
            samples=[SampleChange("Sample6", 1, {"title": "renamed"})],
            now=NOW,
        )
    monkeypatch.undo()

    catalog, problems = store.load_report()

    assert catalog.runs[RunKey(218386)].condition == "OCV"
    assert catalog.samples["Sample6"].title == "Cu/Pt"
    assert any("interrupted" in p.message for p in problems)


def test_the_next_save_after_a_crash_writes_a_consistent_catalog(
    store, monkeypatch
) -> None:
    populated(store)
    _crash_on_write_number(monkeypatch, 2)
    with pytest.raises(OSError):
        assign(store, 218386, base_rev=1, condition="CA")
    monkeypatch.undo()

    assign(store, 218386, base_rev=1, condition="CA")

    catalog, problems = ParquetCatalogStore(
        store.directory, store.cache_dir
    ).load_report()
    assert catalog.runs[RunKey(218386)].condition == "CA"
    assert problems == ()


def test_a_mismatch_the_kept_copy_does_not_explain_is_refused(store) -> None:
    """Restoring an older generation silently would undo edits nobody was told of."""
    populated(store)
    assign(store, 218393, sample_id="Sample6")
    # Damage the current runs table; the kept copy is from the previous save,
    # which the current manifest does not describe.
    (store.directory / RUNS_FILE).write_bytes(b"PAR1 damaged")

    with pytest.raises(CatalogCorruptError, match="manifest"):
        store.load()


# --------------------------------------------------------------------------
# Versions and unknown columns
# --------------------------------------------------------------------------


def test_a_catalog_from_a_newer_nrw_is_refused(store) -> None:
    populated(store)
    manifest = json.loads((store.directory / MANIFEST_FILE).read_text())
    manifest["schema_version"] = 99
    (store.directory / MANIFEST_FILE).write_text(json.dumps(manifest))

    with pytest.raises(CatalogVersionError, match="newer"):
        store.load()
    with pytest.raises(CatalogVersionError):
        assign(store, 218393, sample_id="Sample6")


def test_columns_this_version_does_not_know_survive_a_save(store) -> None:
    """A column added by a newer nrw must not vanish when an older one saves."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    populated(store)
    path = store.directory / RUNS_FILE
    table = pq.read_table(path)
    table = table.append_column("control_mode", pa.array(["galvanostatic"]))
    table = table.replace_schema_metadata(pq.read_schema(path).metadata)
    pq.write_table(table, path)
    (store.directory / MANIFEST_FILE).unlink()

    assign(store, 218386, base_rev=1, condition="-0.5 mA/cm2")

    reread = pq.read_table(path).to_pylist()
    assert reread[0]["control_mode"] == "galvanostatic"
    assert reread[0]["condition"] == "-0.5 mA/cm2"


# --------------------------------------------------------------------------
# The experiment's own context: a third table, as an addition
# --------------------------------------------------------------------------


def describe(store: ParquetCatalogStore, base_rev: int = 0, **changes):
    return store.update(experiment=ExperimentChange(base_rev, changes), now=NOW)


def manifest_of(store: ParquetCatalogStore) -> dict:
    return json.loads((store.directory / MANIFEST_FILE).read_text())


def test_the_experiments_context_survives_a_save_and_a_reload(store) -> None:
    populated(store)
    describe(store, summary="How Li plates on Cu.", hypotheses="It thickens.")

    catalog = ParquetCatalogStore(store.directory, store.cache_dir).load()

    assert (catalog.experiment.summary, catalog.experiment.hypotheses) == (
        "How Li plates on Cu.",
        "It thickens.",
    )
    assert (catalog.experiment.rev, catalog.experiment.updated_at) == (1, NOW)
    assert catalog.samples["Sample6"].title == "Cu/Pt", "and the rest as it was"
    assert manifest_of(store)["tables"]["experiment"]["file"] == EXPERIMENT_FILE


def test_a_project_that_says_nothing_of_its_experiment_keeps_its_files(store) -> None:
    populated(store)

    assert not (store.directory / EXPERIMENT_FILE).exists()
    assert set(manifest_of(store)["tables"]) == {"runs", "samples"}


def test_an_older_nrw_reads_past_the_experiment(store) -> None:
    """What an older nrw checks before it reads -- the schema version, and
    the runs and samples tables against their entries -- still passes."""
    populated(store)
    describe(store, summary="How Li plates on Cu.")

    manifest = manifest_of(store)

    assert manifest["schema_version"] == 1
    for name, file in (("runs", RUNS_FILE), ("samples", SAMPLES_FILE)):
        written = hashlib.sha256((store.directory / file).read_bytes()).hexdigest()
        assert manifest["tables"][name]["sha256"] == written


def test_a_save_by_an_older_nrw_leaves_the_experiment_and_it_is_still_read(
    store,
) -> None:
    """An older nrw writes its manifest without the experiment, and leaves the
    file: read anyway, or the record would vanish and the next save erase it."""
    populated(store)
    describe(store, summary="How Li plates on Cu.")
    manifest = manifest_of(store)
    del manifest["tables"]["experiment"]
    (store.directory / MANIFEST_FILE).write_text(json.dumps(manifest))

    catalog, problems = store.load_report()
    assign(store, 218393, sample_id="Sample6")

    assert catalog.experiment.summary == "How Li plates on Cu." and problems == ()
    assert store.load().experiment.summary == "How Li plates on Cu."
    assert "experiment" in manifest_of(store)["tables"], "and listed again"


def test_a_crash_before_the_manifest_recovers_the_experiment_too(
    store, monkeypatch
) -> None:
    populated(store)
    describe(store, summary="first")
    _crash_on_write_number(monkeypatch, 4)  # every table replaced, not the manifest

    with pytest.raises(OSError, match="simulated crash"):
        describe(store, 1, summary="second")
    monkeypatch.undo()

    catalog, problems = store.load_report()

    assert catalog.experiment.summary == "first"
    assert any("interrupted" in p.message for p in problems)


def _kept_as_an_older_nrw_keeps_it(store: ParquetCatalogStore) -> None:
    """An older nrw's copy for recovery: the runs, the samples, the manifest."""
    previous = store.cache_dir / "previous"
    for name in (RUNS_FILE, SAMPLES_FILE, MANIFEST_FILE):
        shutil.copy2(store.directory / name, previous / name)


def test_a_crash_while_an_older_nrw_saves_recovers_the_experiment_it_left(
    store,
) -> None:
    """An older nrw keeps no copy of the experiment for recovery, so the copy
    nrw kept can be older than the table; recovered, the next save would write
    it back over the newer."""
    populated(store)
    describe(store, summary="first")
    describe(store, 1, summary="second")  # the kept copy now says "first"
    _kept_as_an_older_nrw_keeps_it(store)
    manifest = manifest_of(store)
    del manifest["tables"]["experiment"]
    (store.directory / MANIFEST_FILE).write_text(json.dumps(manifest))  # its save
    _kept_as_an_older_nrw_keeps_it(store)
    (store.directory / RUNS_FILE).write_bytes(b"PAR1 torn")  # its next, cut short

    catalog, problems = store.load_report()

    assert catalog.experiment.summary == "second"
    assert any("interrupted" in p.message for p in problems)


def test_a_crash_while_an_older_nrw_saves_a_listed_experiment_recovers_it(
    store,
) -> None:
    """The kept manifest lists the experiment, but an older nrw kept no copy
    of its table: the one in experiment/, which it never writes, is the one."""
    populated(store)
    describe(store, summary="first")  # listed, and no copy of it kept yet
    _kept_as_an_older_nrw_keeps_it(store)
    (store.directory / RUNS_FILE).write_bytes(b"PAR1 torn")  # its save, cut short

    catalog, problems = store.load_report()

    assert catalog.experiment.summary == "first"
    assert catalog.samples["Sample6"].title == "Cu/Pt"
    assert any("interrupted" in p.message for p in problems)


def test_a_listed_experiment_table_that_is_gone_is_refused(store) -> None:
    populated(store)
    describe(store, summary="first")
    (store.directory / EXPERIMENT_FILE).unlink()

    with pytest.raises(CatalogCorruptError, match="does not match its own manifest"):
        store.load()


def test_a_crash_during_the_experiments_first_save_keeps_what_it_was_saving(
    store, monkeypatch
) -> None:
    """The one case the table in experiment/ is newer than the last complete
    set: it holds the edit being saved. Kept, nothing is lost; the runs are
    the last complete ones, and the person is told the save was cut short."""
    populated(store)
    _crash_on_write_number(monkeypatch, 4)  # every table replaced, not the manifest

    with pytest.raises(OSError, match="simulated crash"):
        store.update(
            runs=[RunChange(RunKey(218386), 1, {"condition": "CA"})],
            experiment=ExperimentChange(0, {"summary": "being saved"}),
            now=NOW,
        )
    monkeypatch.undo()

    catalog, problems = store.load_report()

    assert catalog.runs[RunKey(218386)].condition == "OCV"
    assert catalog.experiment.summary == "being saved"
    assert any("interrupted" in p.message for p in problems)


def test_an_experiment_table_of_more_than_one_row_is_refused(store) -> None:
    populated(store)
    rows = [
        store_module._experiment_row(ExperimentContext(summary=text, rev=1))
        for text in ("one", "two")
    ]
    (store.directory / EXPERIMENT_FILE).write_bytes(
        store_module._table_bytes("experiment", store_module._EXPERIMENT_COLUMNS, rows)
    )

    with pytest.raises(CatalogCorruptError, match="holds 2 rows"):
        store.load()


def test_an_edit_on_a_stale_experiment_is_refused_and_writes_nothing(store) -> None:
    describe(store, summary="first")
    before = (store.directory / EXPERIMENT_FILE).read_bytes()

    with pytest.raises(RecordConflict):
        describe(store, 0, summary="second")

    assert (store.directory / EXPERIMENT_FILE).read_bytes() == before


# --------------------------------------------------------------------------
# Concurrent edits
# --------------------------------------------------------------------------


def test_an_edit_on_a_stale_record_is_refused_and_writes_nothing(store) -> None:
    populated(store)
    before = (store.directory / RUNS_FILE).read_bytes()

    with pytest.raises(RecordConflict):
        assign(store, 218386, base_rev=0, condition="CA")

    assert (store.directory / RUNS_FILE).read_bytes() == before


def test_an_edit_to_another_record_is_merged_not_refused(store) -> None:
    populated(store)

    assign(store, 218393, sample_id="Sample6")
    store.update(samples=[SampleChange("Sample6", 1, {"title": "B"})], now=NOW)

    catalog = store.load()
    assert set(catalog.runs) == {RunKey(218386), RunKey(218393)}


def test_an_unchanged_save_writes_nothing(store) -> None:
    def stamps() -> dict[str, tuple[int, int]]:
        # The inode as well: a replace within one tick of a coarse clock keeps
        # the mtime and still makes a new file -- and a new git diff.
        return {
            p.name: (p.stat().st_mtime_ns, p.stat().st_ino)
            for p in store.directory.iterdir()
        }

    populated(store)
    before = stamps()

    assign(store, 218386, base_rev=1, condition="OCV")

    assert stamps() == before


def test_concurrent_saves_from_many_threads_lose_nothing(store) -> None:
    """The web server is threaded; every writer's edit must survive."""
    runs = list(range(218400, 218416))
    errors: list[Exception] = []

    def edit(run: int) -> None:
        try:
            assign(store, run, sample_id="Sample6")
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=edit, args=(run,)) for run in runs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert {key.run for key in store.load().runs} == set(runs)


# --------------------------------------------------------------------------
# Git
# --------------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "HOME": str(root),
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
        },
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_an_unmerged_catalog_is_refused_for_reading_and_writing(tmp_path: Path) -> None:
    """A binary conflict leaves "ours" in place with no markers at all."""
    root = tmp_path / "project"
    root.mkdir()
    store = ParquetCatalogStore(root / "experiment", tmp_path / "cache", git_root=root)
    _git(root, "init", "-q", "-b", "main")
    assign(store, 218386, sample_id="Sample6")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-qb", "other")
    assign(store, 218386, base_rev=1, condition="CA")
    _git(root, "commit", "-qam", "other")
    _git(root, "checkout", "-q", "main")
    assign(store, 218386, base_rev=1, condition="OCV")
    _git(root, "commit", "-qam", "main")
    subprocess.run(
        ["git", "merge", "other"], cwd=root, capture_output=True, check=False
    )

    with pytest.raises(CatalogUnmergedError, match="unmerged"):
        store.load()
    with pytest.raises(CatalogUnmergedError):
        assign(store, 218393, sample_id="Sample6")


# --------------------------------------------------------------------------
# Import cost
# --------------------------------------------------------------------------


def test_importing_the_store_does_not_import_pyarrow() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, nr_workbench.experiment.store; "
            "assert 'pyarrow' not in sys.modules; print('clean')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
