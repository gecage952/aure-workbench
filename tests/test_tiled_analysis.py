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


#: A reduced file the test fits read: its header, and two rows.
DATA = b"# Experiment IPTS-00001 Run 234277\n# Reduction 2.10\n0.01 1.0 0.1 0.001\n0.02 0.5 0.05 0.002\n"

#: Where in the project the test fits read :data:`DATA`.
STEADY = "samples/S1/data/steady/REFL_234277_1_234277_autoreduction.dat"

#: An SLD profile of more rows than a record keeps.
PROFILE = "#  z (A) rho irho\n" + "".join(f"{z} 2.07 0\n" for z in range(1000))


def fitted(
    project: Path,
    fit_id: str = A,
    *,
    sample: str | None = "S1",
    chisq: float = 1.2,
) -> Path:
    """A fit as `nrw fit run` leaves one, with each file a record carries: the
    data it read, recorded in its inputs, and bumps' export of the problem."""
    directory = (
        project / "samples" / sample / "results" / fit_id
        if sample
        else project / "results" / fit_id
    )
    (directory / "env").mkdir(parents=True)
    (directory / "fit").mkdir()
    own = project / "samples" / (sample or "S1")
    (own / "data" / "steady").mkdir(parents=True, exist_ok=True)
    (own / "models").mkdir(exist_ok=True)
    data = project / STEADY.replace("S1", sample or "S1", 1)
    data.write_bytes(DATA)
    script = own / "models" / "film.py"
    script.write_bytes(SCRIPT)
    manifest = {
        "params": {"method": "dream", "samples": 1000, "burn": 50, "seed": 7},
        "info": {"chisq": chisq, "n_free": 1, "n_points": 30},
    }
    inputs = {
        "inputs": [
            {
                "role": "data:steady:REFL_234277_1_234277_autoreduction",
                "path": data.relative_to(project).as_posix(),
                "sha256": hashlib.sha256(DATA).hexdigest(),
                "bytes": len(DATA),
            },
            {
                "role": "script",
                "path": script.relative_to(project).as_posix(),
                "sha256": hashlib.sha256(SCRIPT).hexdigest(),
                "bytes": len(SCRIPT),
            },
        ]
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "inputs.json").write_text(json.dumps(inputs))
    (directory / "env" / "versions.json").write_text(json.dumps({"python": "3.14"}))
    (directory / "model.py").write_bytes(SCRIPT)
    (directory / "spec.yaml").write_text(SPEC)
    (directory / "NOTES.md").write_text("# Notes\n")
    fit = directory / "fit"
    (fit / "film.par").write_text("thickness 80.0\n")
    (fit / "film.json").write_text(
        json.dumps(
            {"references": [{"name": "thickness", "fixed": False, "bounds": [50, 100]}]}
        )
    )
    (fit / "film-err.json").write_text(
        json.dumps({"thickness": {"std": 1.5, "median": 80.1, "p68": [79, 81]}})
    )
    (fit / "film-1-refl.dat").write_text(
        "# Q dQ R dR theory fresnel\n0.01 0.001 1 0.1 0.9 1\n"
    )
    (fit / "film-1-slabs.dat").write_text(
        "# thickness interface rho irho\n0 3 2.07 0\n"
    )
    (fit / "film-1-profile.dat").write_text(PROFILE)
    (fit / "film.mc.gz").write_bytes(b"chains")
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
    assert fit.specs == [FakeSpec("analysis-fit", "2")]
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


def test_a_record_is_the_fit_as_analysed_data(
    project: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    """The model refl1d loads, the curves, the data as fitted, and why it is
    the answer: no chains."""
    directory = fitted(project)
    curation.promote(layout(project), A, reason="the dry reference, settled")

    pushed(project)

    record = experiment[f"analysis/S1/{A}"].metadata
    problem = (directory / "fit" / "film.json").read_bytes()
    assert record["problem"] == {
        "name": "film.json",
        "text": problem.decode(),
        "sha256": hashlib.sha256(problem).hexdigest(),
        "bytes": len(problem),
    }
    assert set(record["fit"]) == {"film.par", "film-err.json"}
    (segment,) = record["curves"]
    assert segment["index"] == 1 and segment["refl"]["name"] == "film-1-refl.dat"
    assert segment["slabs"]["text"].startswith("# thickness")
    assert segment["profile"]["points"] <= 400 < 1000
    data = record["data"][STEADY]
    assert data["text"].encode() == DATA
    assert data["sha256"] == hashlib.sha256(DATA).hexdigest()
    assert record["index"]["fit_id"] == A and record["index"]["model"] == "film"
    assert record["inputs"]["inputs"][0]["path"] == STEADY and record["pushed_by"]
    promotion = record["curation"]["promotions"]["final"]
    assert promotion["reason"] == "the dry reference, settled" and promotion["who"]
    assert "chains" not in json.dumps(record)


def test_a_data_file_changed_since_the_fit_is_named_not_carried(
    project: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    fitted(project)
    starred(project)
    (project / STEADY).write_bytes(b"# reduced again\n")

    pushed(project)

    carried = experiment[f"analysis/S1/{A}"].metadata["data"][STEADY]
    assert "text" not in carried
    assert carried["why"] == "the file here is not the one the fit read"
    assert carried["sha256"] == hashlib.sha256(DATA).hexdigest()


def test_an_input_that_is_not_text_is_named_and_the_push_goes_on(
    project: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    directory = fitted(project)
    starred(project)
    binary = project / "samples" / "S1" / "data" / "steady" / "resolution.npy"
    binary.write_bytes(b"\x93NUMPY\xff\xfe")
    inputs = json.loads((directory / "inputs.json").read_text())
    inputs["inputs"].append(
        {
            "role": "data:steady:resolution",
            "path": binary.relative_to(project).as_posix(),
            "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "bytes": binary.stat().st_size,
        }
    )
    (directory / "inputs.json").write_text(json.dumps(inputs))

    shown = pushed(project)

    assert shown.exit_code == 0, shown.output
    data = experiment[f"analysis/S1/{A}"].metadata["data"]
    assert (
        data["samples/S1/data/steady/resolution.npy"]["why"] == "the file is not text"
    )
    assert data[STEADY]["text"].encode() == DATA


def test_a_record_replaced_says_what_it_is_now_and_keeps_others_specs(
    project: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    fitted(project)
    starred(project)
    pushed(project)
    fit = experiment[f"analysis/S1/{A}"]
    fit.specs = [FakeSpec("analysis-fit", "1"), FakeSpec("their-spec", "3")]
    curation.star(layout(project), A, starred=False)

    pushed(project)

    assert fit.specs == [FakeSpec("analysis-fit", "2"), FakeSpec("their-spec", "3")]


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


@pytest.mark.parametrize("rows", [400, 401, 800, 1000, 5003])
def test_a_profile_is_thinned_to_its_ends_and_what_fits_between_once(
    tmp_path: Path, rows: int
) -> None:
    """Thinned again, as a push of a pulled fit thins it, it is the same."""
    from nr_workbench.tiled.analysis import PROFILE_POINTS, thinned

    profile = tmp_path / "film-1-profile.dat"
    profile.write_text(
        "#  z rho irho\n" + "".join(f"{z} 2.07 0\n" for z in range(rows))
    )

    once = thinned(profile)
    profile.write_text(once["text"])
    twice = thinned(profile)

    kept = once["text"].splitlines()[1:]
    assert once["points"] == len(kept) <= PROFILE_POINTS
    assert (kept[0], kept[-1]) == ("0 2.07 0", f"{rows - 1} 2.07 0")
    assert twice["text"] == once["text"]
