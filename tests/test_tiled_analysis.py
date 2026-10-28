"""`nrw tiled push` and the fits: each starred fit, and each sample's final one,
written to the experiment's `analysis/` on Tiled as one container whose
metadata is the fit's record.

On the fake client of `tests/test_tiled_sync.py`: nothing here needs the real
one installed, or sends anything.
"""

# The fixtures are imported and requested by name, which ruff reads as
# redefinitions.
# ruff: noqa: F811

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from nr_workbench.project.layout import ProjectLayout
from nr_workbench.provenance import curation
from nr_workbench.provenance.index import FitIndex

from .test_tiled_sync import (  # noqa: F401 - fixtures
    FakeNode,
    FakeSpec,
    FakeTiled,
    catalogued,
    nrw,
    on_tiled,
    tiled,
)

A = "20260930-100000Z-aaaaaaaa"
B = "20260930-110000Z-bbbbbbbb"

SCRIPT = b"# generated from film.yaml\nproblem = None\n"
SPEC = "schema: nrw-model/1\nname: film\n"


def fitted(
    project: Path,
    fit_id: str = A,
    *,
    sample: str | None = "S1",
    chisq: float = 1.2,
) -> Path:
    """A fit as `nrw fit run` leaves one, with each file a record carries."""
    directory = (
        project / "samples" / sample / "results" / fit_id
        if sample
        else project / "results" / fit_id
    )
    (directory / "env").mkdir(parents=True)
    (directory / "fit").mkdir()
    manifest = {
        "params": {"method": "dream", "samples": 1000, "burn": 50, "seed": 7},
        "info": {"chisq": chisq, "n_free": 1, "n_points": 30},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "inputs.json").write_text(json.dumps({"inputs": []}))
    (directory / "env" / "versions.json").write_text(json.dumps({"python": "3.14"}))
    (directory / "model.py").write_bytes(SCRIPT)
    (directory / "spec.yaml").write_text(SPEC)
    (directory / "NOTES.md").write_text("# Notes\n")
    (directory / "fit" / "m.par").write_text("thickness 80.0\n")
    (directory / "fit" / "m.json").write_text(
        json.dumps(
            {"references": [{"name": "thickness", "fixed": False, "bounds": [50, 100]}]}
        )
    )
    (directory / "fit" / "m-err.json").write_text(
        json.dumps({"thickness": {"std": 1.5, "median": 80.1, "p68": [79, 81]}})
    )
    FitIndex(project / ".nrw" / "index.jsonl").append(
        {
            "fit_id": fit_id,
            "sample": sample,
            "model": "film",
            "method": "dream",
            "status": "ok",
            "started_at": fit_id[:15],
            "chisq": chisq,
            "n_free": 1,
        }
    )
    return directory


def layout(project: Path) -> ProjectLayout:
    return ProjectLayout(root=project)


def starred(project: Path, fit_id: str = A) -> None:
    curation.star(layout(project), fit_id)


def pushed(project: Path, *args: str):
    return nrw("push", "--root", str(project), "--yes", *args)


@pytest.fixture
def experiment(tiled: FakeTiled) -> FakeNode:
    """Logged in, and the experiment on Tiled, tagged."""
    tiled.context.kept = True
    return on_tiled(tiled, tags=("isaac", "ipts-1"))


def test_a_starred_fit_is_written_whole_where_everyone_on_it_can_read_it(
    project: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    directory = fitted(project)
    starred(project)

    shown = pushed(project)

    assert shown.exit_code == 0, shown.output
    assert f"+ S1/{A}  (starred)" in shown.output
    assert "Pushed 1 fit(s) to analysis/." in shown.output
    tags = ("ipts-1", "isaac")
    assert tiled.root.log == [
        ("create", "analysis", *tags),
        ("create", "S1", *tags),
        ("create", A, *tags),
    ]
    assert experiment["analysis"].specs == [FakeSpec("analysis-experiment", "1")]
    assert experiment["analysis/S1"].metadata == {"sample_id": "S1"}
    fit = experiment[f"analysis/S1/{A}"]
    assert fit.specs == [FakeSpec("analysis-fit", "1")]
    record = fit.metadata
    assert record["files"]["model.py"] == {
        "text": SCRIPT.decode(),
        "sha256": hashlib.sha256(SCRIPT).hexdigest(),
        "bytes": len(SCRIPT),
    }
    assert record["files"]["spec.yaml"]["text"] == SPEC
    assert record["manifest"] == json.loads((directory / "manifest.json").read_text())
    assert record["curation"]["starred"] is True
    assert (record["model"], record["chisq"], record["n_points"]) == ("film", 1.2, 30)
    assert record["parameters"] == [
        {
            "name": "thickness",
            "value": 80.0,
            "bounds": [50.0, 100.0],
            "std": 1.5,
            "median": 80.1,
            "p68": [79, 81],
        }
    ]


def test_a_second_push_writes_nothing_and_a_dry_run_writes_nothing(
    project: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    fitted(project)
    starred(project)
    dry = pushed(project, "--dry-run")
    assert tiled.root.log == [] and "Nothing written (--dry-run)." in dry.output

    pushed(project)
    before = list(tiled.root.log)
    again = pushed(project)

    assert again.exit_code == 0, again.output
    assert "0 new, 0 changed, 1 the same" in again.output
    assert "Tiled already holds these fits." in again.output
    assert tiled.root.log == before


def test_only_narrows_the_push_to_one_tree(
    project: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    catalogued(project)
    fitted(project)
    starred(project)

    fits = pushed(project, "--only", "fits")
    made = {entry[1] for entry in tiled.root.log}
    context = pushed(project, "--only", "context")

    assert fits.exit_code == 0 and context.exit_code == 0, fits.output
    assert "analysis" in made and "context" not in made
    assert "analysis/" not in context.output and "Pushed 3 record(s)." in context.output


def test_a_fit_unstarred_here_has_its_curation_written_and_nothing_else(
    project: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    fitted(project)
    starred(project)
    pushed(project)
    curation.star(layout(project), A, starred=False)

    shown = pushed(project)

    assert shown.exit_code == 0, shown.output
    assert f"~ S1/{A}  (unstarred; curation)" in shown.output
    record = experiment[f"analysis/S1/{A}"].metadata
    assert record["curation"]["starred"] is False
    assert "model.py" in record["files"], "the rest of the record stays"


def test_a_final_fit_is_written_unstarred(
    project: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    fitted(project)
    curation.promote(layout(project), A, reason="the answer")

    shown = pushed(project)

    assert f"+ S1/{A}  (final)" in shown.output
    assert experiment[f"analysis/S1/{A}"].metadata["curation"]["labels"] == ["final"]


def test_a_fit_neither_starred_nor_final_is_never_written(
    project: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    fitted(project)
    fitted(project, B)
    starred(project, B)
    curation.discard(layout(project), B, reason="diverged")

    shown = pushed(project)

    assert shown.exit_code == 0, shown.output
    assert "no fit is starred or final, so none is pushed" in shown.output
    assert tiled.root.log == []


def test_a_fit_whose_files_were_deleted_has_its_curation_written(
    project: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    fitted(project)
    starred(project)
    pushed(project)
    curation.discard(layout(project), A, reason="superseded")
    curation.delete_files(layout(project), A)

    shown = pushed(project)

    assert shown.exit_code == 0, shown.output
    assert f"~ S1/{A}  (deleted; curation)" in shown.output
    record = experiment[f"analysis/S1/{A}"].metadata
    assert record["curation"]["deleted"] and "model.py" in record["files"]


def test_a_fit_with_no_sample_or_no_files_here_is_left_and_said(
    project: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    fitted(project, A, sample=None)
    starred(project, A)
    directory = fitted(project, B)
    starred(project, B)
    (directory / "manifest.json").unlink()

    shown = pushed(project)

    assert shown.exit_code == 0, shown.output
    assert f"left: {A}, it has no sample" in shown.output
    assert f"left: {B}, its files are not here" in shown.output


def test_a_value_json_has_no_word_for_arrives_as_none(
    project: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    fitted(project, chisq=float("nan"))
    starred(project)

    pushed(project)

    assert experiment[f"analysis/S1/{A}"].metadata["chisq"] is None


def test_nothing_is_made_where_there_are_no_tags_to_give(
    project: Path, tiled: FakeTiled
) -> None:
    tiled.context.kept = True
    on_tiled(tiled, tags=())
    fitted(project)
    starred(project)

    shown = pushed(project)

    assert shown.exit_code == 1 and "has no access tags" in shown.output
    assert tiled.root.log == []


def test_a_push_stopped_partway_says_how_many_fits_it_wrote(
    project: Path, tiled: FakeTiled, experiment: FakeNode, monkeypatch
) -> None:
    import httpx

    fitted(project)
    fitted(project, B)
    starred(project)
    starred(project, B)
    made = FakeNode.create_container

    def server_goes_away(self, name, **kwargs):
        if name == B:
            raise httpx.ConnectError("the server went away")
        return made(self, name, **kwargs)

    monkeypatch.setattr(FakeNode, "create_container", server_goes_away)

    shown = pushed(project)

    assert shown.exit_code == 1
    assert f"S1/{B} could not be written" in shown.output
    assert "1 record(s) were written before it" in shown.output


def test_a_starred_fit_is_something_to_push_in_an_empty_catalog(
    project: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    nothing = pushed(project)
    fitted(project)
    starred(project)

    something = pushed(project)

    assert "nothing to push" in nothing.output
    assert something.exit_code == 0, something.output
    assert "Pushed 1 fit(s)" in something.output
