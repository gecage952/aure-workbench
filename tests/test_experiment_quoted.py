"""The experiment's context as a language model is given it."""

from __future__ import annotations

from pathlib import Path

from nr_workbench.experiment.model import ExperimentChange, ExperimentContext
from nr_workbench.experiment.quoted import quotable, quotable_record
from nr_workbench.experiment.store import ParquetCatalogStore


def test_each_written_field_is_quoted_under_its_heading() -> None:
    quoted = quotable_record(
        ExperimentContext(summary="Li plating.", notes="Cu/Pt.", rev=1)
    )

    assert quoted.fields == {"summary": "Li plating.", "notes": "Cu/Pt."}
    assert quoted.text() == "Summary:\nLi plating.\n\nNotes:\nCu/Pt."


def test_each_field_is_quoted_as_it_is_saved() -> None:
    record = ExperimentContext(
        summary="Li plating.",
        hypotheses="## SEI\nIt thickens.",
        notes="```\nQ R dR",
        rev=1,
    )

    quoted = quotable_record(record)

    assert quoted.fields == {
        "summary": "Li plating.",
        "hypotheses": "## SEI\nIt thickens.",
        "notes": "```\nQ R dR",
    }


def test_a_project_without_a_catalog_has_nothing_to_quote_and_opens_nothing(
    tmp_path: Path,
) -> None:
    quoted = quotable(tmp_path)

    assert quoted.empty
    assert quoted.text() == ""
    assert list(tmp_path.iterdir()) == []


def test_the_projects_own_record_is_quoted(project: Path) -> None:
    ParquetCatalogStore.for_project(project).update(
        experiment=ExperimentChange(0, {"summary": "Li plating."})
    )

    assert quotable(project).fields == {"summary": "Li plating."}


def test_an_unreadable_catalog_is_said_not_guessed(project: Path) -> None:
    ParquetCatalogStore.for_project(project).update(
        experiment=ExperimentChange(0, {"summary": "Li plating."})
    )
    (project / "experiment" / "runs.parquet").write_bytes(b"not parquet")

    quoted = quotable(project)

    assert quoted.unreadable
    assert not quoted.fields
    assert "could not be read" in quoted.text()
