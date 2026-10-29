"""The parquet catalog store: nothing a person decided may be lost quietly.

Each test here is one way the catalog could lose the experimenter's decisions
without anyone noticing: a damaged file read as empty, two writers
overwriting each other, or an older nrw dropping columns it does not know.
"""

from __future__ import annotations

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
    RUNS_FILE,
    SAMPLES_FILE,
    CatalogCorruptError,
    CatalogError,
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


def test_tables_carry_their_schema_and_name(store) -> None:
    import pyarrow.parquet as pq

    populated(store)

    metadata = pq.read_schema(store.directory / RUNS_FILE).metadata
    assert metadata[b"nrw.schema"] == b"nrw-experiment/1"
    assert metadata[b"nrw.table"] == b"runs"


def test_no_absolute_path_is_stored(store) -> None:
    """`nrw check` greps committed files for home paths, but skips binaries."""
    populated(store)

    for name in (RUNS_FILE, SAMPLES_FILE):
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

    with pytest.raises(CatalogCorruptError, match="include"):
        store.load()


# --------------------------------------------------------------------------
# Versions and unknown columns
# --------------------------------------------------------------------------


def test_a_catalog_from_a_newer_nrw_is_refused(store) -> None:
    import pyarrow.parquet as pq

    populated(store)
    path = store.directory / RUNS_FILE
    table = pq.read_table(path)
    metadata = {**table.schema.metadata, b"nrw.schema_version": b"99"}
    pq.write_table(table.replace_schema_metadata(metadata), path)

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

    assign(store, 218386, base_rev=1, condition="-0.5 mA/cm2")

    reread = pq.read_table(path).to_pylist()
    assert reread[0]["control_mode"] == "galvanostatic"
    assert reread[0]["condition"] == "-0.5 mA/cm2"


# --------------------------------------------------------------------------
# The experiment's own context
# --------------------------------------------------------------------------


def describe(store: ParquetCatalogStore, base_rev: int = 0, **changes):
    return store.update(experiment=ExperimentChange(base_rev, changes), now=NOW)


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


def test_a_project_that_says_nothing_of_its_experiment_keeps_its_files(store) -> None:
    populated(store)

    assert sorted(p.name for p in store.directory.iterdir()) == [
        RUNS_FILE,
        SAMPLES_FILE,
    ]


def test_a_save_removes_the_digests_an_older_nrw_wrote(store) -> None:
    """Left describing tables since replaced, they would make that nrw refuse
    them."""
    populated(store)
    (store.directory / "catalog.json").write_text("{}")

    assign(store, 218386, base_rev=1, condition="CA")

    assert not (store.directory / "catalog.json").exists()


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
