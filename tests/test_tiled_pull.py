"""`nrw tiled pull --context --data --fits`: a colleague who starts fresh brings
back the catalog, the reduced files and the fits' models, and runs each fit
again here.

One project pushes, and a colleague's, made fresh, pulls; both on the fake
client of `tests/test_tiled_sync.py`.
"""

# The fixtures are imported and requested by name, which ruff reads as
# redefinitions.
# ruff: noqa: F811

from __future__ import annotations

import os
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from nr_workbench.cli import main
from nr_workbench.experiment.model import RunChange, RunKey
from nr_workbench.experiment.store import ParquetCatalogStore
from nr_workbench.provenance.whence import Freshness

from .test_tiled_analysis import (  # noqa: F401 - fixtures
    DATA,
    SCRIPT,
    SPEC,
    STEADY,
    A,
    B,
    experiment,
    fitted,
    pushed,
    starred,
)
from .test_tiled_sync import (  # noqa: F401 - fixtures
    NOW,
    SEGMENT,
    FakeNode,
    FakeTiled,
    catalogued,
    described,
    nrw,
    tiled,
)


@pytest.fixture
def colleague(tmp_path_factory, context) -> Path:
    """A project of the same experiment, made fresh: a colleague's."""
    from nr_workbench.commands.init_cmd import plan_project_files
    from nr_workbench.project.scaffold import apply_scaffold

    root = tmp_path_factory.mktemp("colleague")
    apply_scaffold(root, plan_project_files(context))
    return root


def pulled(project: Path, *args: str):
    return nrw("pull", "--root", str(project), "--yes", *args)


def catalog_of(project: Path):
    return ParquetCatalogStore.for_project(project).load()


# --------------------------------------------------------------------------
# --context
# --------------------------------------------------------------------------


def test_a_colleague_pulls_the_catalog_and_it_then_matches_tileds(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    catalogued(project)
    described(project, summary="Does Li plate on Cu?")
    pushed(project)

    shown = pulled(colleague, "--context")

    assert shown.exit_code == 0, shown.output
    assert "4 new, 0 changed, 0 the same" in shown.output
    catalog = catalog_of(colleague)
    assert catalog.runs[RunKey(234277)].condition == "OCV"
    assert catalog.runs[RunKey(234280)].sample_id == "S1"
    assert catalog.samples["S1"].title == "Cu/Pt"
    assert catalog.experiment.summary == "Does Li plate on Cu?"
    # Its own revisions, and the same records: a push from here has nothing
    # to write.
    again = nrw("push", "--root", str(colleague), "--yes")
    assert "Tiled already holds this catalog." in again.output


def test_a_record_that_differs_is_replaced_by_tileds_once_the_person_says_so(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    catalogued(project)
    pushed(project)
    ParquetCatalogStore.for_project(colleague).update(
        runs=[RunChange(RunKey(234277), 0, {"sample_id": "S1", "condition": "dry"})],
        now=NOW,
    )

    declined = CliRunner().invoke(
        main, ["tiled", "pull", "--context", "--root", str(colleague)], input="n\n"
    )
    dry = pulled(colleague, "--context", "--dry-run")
    kept = catalog_of(colleague).runs[RunKey(234277)].condition
    shown = pulled(colleague, "--context")

    assert declined.exit_code == 1
    assert "Write Tiled's copy over this project's catalog?" in declined.output
    assert "Nothing written (--dry-run)." in dry.output
    assert kept == "dry"
    assert shown.exit_code == 0, shown.output
    assert "~ runs/234277-steady  (condition)" in shown.output
    assert catalog_of(colleague).runs[RunKey(234277)].condition == "OCV"


def test_a_record_only_here_is_left_and_counted(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    catalogued(project)
    pushed(project)
    ParquetCatalogStore.for_project(colleague).update(
        runs=[RunChange(RunKey(234999), 0, {"sample_id": "S9"})], now=NOW
    )

    shown = pulled(colleague, "--context")

    assert shown.exit_code == 0, shown.output
    assert "1 here alone (left as they are)" in shown.output
    assert catalog_of(colleague).runs[RunKey(234999)].sample_id == "S9"


def test_a_save_made_while_the_person_is_asked_writes_nothing(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode, monkeypatch
) -> None:
    catalogued(project)
    pushed(project)
    store = ParquetCatalogStore.for_project(colleague)
    store.update(
        runs=[RunChange(RunKey(234277), 0, {"sample_id": "S1", "condition": "dry"})],
        now=NOW,
    )

    def meanwhile(*args, **kwargs) -> bool:
        # The page saves the run while the person reads the question.
        store.update(runs=[RunChange(RunKey(234277), 1, {"note": "new"})], now=NOW)
        return True

    monkeypatch.setattr(click, "confirm", meanwhile)

    shown = nrw("pull", "--context", "--root", str(colleague))

    assert shown.exit_code == 1
    assert "pull again" in shown.output
    assert catalog_of(colleague).runs[RunKey(234277)].condition == "dry"


def test_records_the_rules_refuse_together_are_said_and_nothing_is_written(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    """Each alone is a sample the catalog takes; S1 and s1 together are one
    folder on macOS."""
    catalogued(project)
    pushed(project)
    experiment["context/samples"].create_container(
        "s1", metadata={"sample_id": "s1", "title": "another"}
    )

    shown = pulled(colleague, "--context")

    assert shown.exit_code == 1
    assert "! these records together:" in shown.output
    assert "only in case" in shown.output
    assert catalog_of(colleague).samples == {}


def test_a_record_the_catalogs_rules_refuse_is_named_and_nothing_is_written(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    catalogued(project)
    pushed(project)
    experiment["context/runs/234277-steady"].metadata["condition"] = "OCV | 25 C"

    shown = pulled(colleague, "--context")

    assert shown.exit_code == 1
    assert "! runs/234277-steady: condition contains '|'" in shown.output
    assert "Nothing was written" in shown.output
    assert catalog_of(colleague).runs == {}


# --------------------------------------------------------------------------
# --fits
# --------------------------------------------------------------------------


def check_inputs(directory: Path, root: Path):
    from nr_workbench.provenance.whence import check_inputs

    return check_inputs(directory, root)


def index_of(root: Path):
    from nr_workbench.provenance.index import FitIndex

    return FitIndex(root / ".nrw" / "index.jsonl")


def curation_of(root: Path, fit_id: str):
    from nr_workbench.provenance.curation import replay

    return replay(index_of(root).entries()).of(fit_id)


def test_a_fits_pull_rebuilds_each_fit_as_it_was_recorded(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    import json

    ours = fitted(project)
    starred(project)
    pushed(project)

    shown = pulled(colleague, "--fits")

    assert shown.exit_code == 0, shown.output
    assert (
        f"+ S1/{A}  starred, dream, chisq 1.2: samples/S1/results/{A}/, "
        f"{STEADY}, samples/S1/models/film.yaml, samples/S1/models/film.py"
    ) in shown.output
    assert f"nrw fit run --from {A}" in shown.output
    theirs = colleague / "samples" / "S1" / "results" / A
    for name in (
        "model.py",
        "spec.yaml",
        "NOTES.md",
        "fit/film.json",
        "fit/film.par",
        "fit/film-err.json",
        "fit/film-1-refl.dat",
        "fit/film-1-slabs.dat",
    ):
        assert (theirs / name).read_bytes() == (ours / name).read_bytes(), name
    for name in ("manifest.json", "inputs.json", "env/versions.json"):
        assert json.loads((theirs / name).read_text()) == json.loads(
            (ours / name).read_text()
        ), name
    profile = (theirs / "fit" / "film-1-profile.dat").read_text().splitlines()
    assert profile[0].startswith("#") and len(profile) <= 401
    assert (colleague / STEADY).read_bytes() == DATA
    assert (theirs / "data" / STEADY).read_bytes() == DATA
    assert check_inputs(theirs, colleague)[1] is Freshness.FRESH
    assert not (theirs / "fit" / "film.mc.gz").exists()
    assert index_of(colleague).find(A)["chisq"] == 1.2
    assert curation_of(colleague, A).starred
    models = colleague / "samples" / "S1" / "models"
    assert (models / "film.py").read_bytes() == SCRIPT
    assert (models / "film.yaml").read_text() == SPEC


def test_a_final_fit_is_pulled_with_why_it_is_the_answer(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    from nr_workbench.project.layout import ProjectLayout
    from nr_workbench.provenance import curation

    fitted(project)
    curation.promote(ProjectLayout(root=project), A, reason="the dry reference")
    pushed(project)

    shown = pulled(colleague, "--fits")

    assert shown.exit_code == 0, shown.output
    (ours,) = [e for e in index_of(project).entries() if e["event"] == "promote"]
    held = curation_of(colleague, A).promotions["final"]
    said = ("reason", "who", "at", "forced")
    assert {k: held[k] for k in said} == {k: ours[k] for k in said}


def test_a_label_another_fit_holds_here_stays_that_fits(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    from nr_workbench.project.layout import ProjectLayout
    from nr_workbench.provenance import curation

    fitted(project)
    curation.promote(ProjectLayout(root=project), A, reason="theirs")
    pushed(project)
    fitted(colleague, B)
    curation.promote(ProjectLayout(root=colleague), B, reason="mine")

    shown = pulled(colleague, "--fits")

    assert shown.exit_code == 0, shown.output
    assert f"final: {B} holds it here, and keeps it" in shown.output
    assert curation_of(colleague, B).labels == ("final",)
    assert curation_of(colleague, A).labels == ()
    assert index_of(colleague).find(A) is not None


def test_a_fit_the_index_holds_already_is_left_whole(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    fitted(project)
    starred(project)
    pushed(project)
    pulled(colleague, "--fits")
    notes = colleague / "samples" / "S1" / "results" / A / "NOTES.md"
    notes.write_text("# The colleague's own notes\n")

    again = pulled(colleague, "--fits")

    assert again.exit_code == 0, again.output
    assert f"= S1/{A}  starred, dream, chisq 1.2: here already" in again.output
    assert notes.read_text() == "# The colleague's own notes\n"
    assert [e["fit_id"] for e in index_of(colleague).fits()] == [A]


def test_a_file_there_already_is_left_and_the_rest_are_written(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    """A pull stopped partway leaves the files it wrote, and no index line:
    the next one writes the rest."""
    ours = fitted(project)
    starred(project)
    pushed(project)
    notes = colleague / "samples" / "S1" / "results" / A / "NOTES.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("# Written before\n")

    shown = pulled(colleague, "--fits")

    assert shown.exit_code == 0, shown.output
    assert notes.read_text() == "# Written before\n"
    assert (notes.parent / "model.py").read_bytes() == (ours / "model.py").read_bytes()
    assert index_of(colleague).find(A) is not None


def test_a_reduced_file_here_already_is_left_and_the_fit_keeps_its_own_copy(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    """The facility's reduction, applied here first, under the same name: the
    fit reads as stale here, and a push still carries the file it read."""
    fitted(project)
    starred(project)
    pushed(project)
    reduced_again = b"# Reduction 2.11\n0.01 0.9 0.1 0.001\n"
    (colleague / STEADY).parent.mkdir(parents=True)
    (colleague / STEADY).write_bytes(reduced_again)

    shown = pulled(colleague, "--fits")
    push = nrw("push", "--root", str(colleague), "--yes", "--only", "fits")

    assert shown.exit_code == 0, shown.output
    assert f"{STEADY} holds another file here, left" in shown.output
    assert (colleague / STEADY).read_bytes() == reduced_again
    theirs = colleague / "samples" / "S1" / "results" / A
    assert (theirs / "data" / STEADY).read_bytes() == DATA
    assert check_inputs(theirs, colleague)[1] is Freshness.STALE
    assert "0 new, 0 changed, 1 the same" in push.output, push.output
    carried = experiment[f"analysis/S1/{A}"].metadata["data"][STEADY]
    assert carried["text"].encode() == DATA


def test_a_fit_with_no_problem_file_says_to_run_its_script(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    (fitted(project) / "fit" / "film.json").unlink()
    starred(project)
    pushed(project)

    shown = pulled(colleague, "--fits")

    assert shown.exit_code == 0, shown.output
    assert "      nrw fit run samples/S1/models/film.py\n" in shown.output
    assert "--from" not in shown.output


def test_a_model_of_the_same_name_here_is_kept_and_the_fits_written_beside_it(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    fitted(project)
    starred(project)
    pushed(project)
    models = colleague / "samples" / "S1" / "models"
    models.mkdir(parents=True)
    (models / "film.py").write_text("# the colleague's own film\n")

    shown = pulled(colleague, "--fits")

    assert shown.exit_code == 0, shown.output
    assert (models / "film.py").read_text() == "# the colleague's own film\n"
    assert (models / "film-2.py").read_bytes() == SCRIPT
    assert "samples/S1/models/film-2.py" in shown.output


def test_a_script_with_no_spec_is_written_alone(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    directory = fitted(project)
    (directory / "spec.yaml").unlink()
    starred(project)
    pushed(project)

    shown = pulled(colleague, "--fits")

    models = colleague / "samples" / "S1" / "models"
    assert shown.exit_code == 0, shown.output
    assert (models / "film.py").read_bytes() == SCRIPT
    assert not (models / "film.yaml").exists()


def test_a_fits_dry_run_writes_nothing(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    fitted(project)
    starred(project)
    pushed(project)

    shown = pulled(colleague, "--fits", "--dry-run")

    assert f"+ S1/{A}  starred" in shown.output
    assert "Nothing written (--dry-run)." in shown.output
    assert not (colleague / "samples" / "S1" / "results").exists()
    assert not (colleague / "samples" / "S1" / "models" / "film.py").exists()
    assert index_of(colleague).fits() == []


def test_two_fits_of_one_model_are_named_apart_in_a_dry_run_as_in_a_pull(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    """The final fit takes the model's own name; the other is written beside it."""
    from nr_workbench.project.layout import ProjectLayout
    from nr_workbench.provenance import curation

    refined = b"# film, refined\nproblem = None\n"
    fitted(project)
    starred(project)
    (fitted(project, B) / "model.py").write_bytes(refined)
    curation.promote(ProjectLayout(root=project), B, reason="the answer")
    pushed(project)

    dry = pulled(colleague, "--fits", "--dry-run")
    real = pulled(colleague, "--fits")

    for shown in (dry, real):
        assert shown.exit_code == 0, shown.output
        lines = shown.output.splitlines()
        (final,) = [line for line in lines if line.startswith(f"  + S1/{B}  final")]
        (other,) = [line for line in lines if line.startswith(f"  + S1/{A}  starred")]
        assert final.endswith("samples/S1/models/film.yaml, samples/S1/models/film.py")
        assert other.endswith(
            "samples/S1/models/film-2.yaml, samples/S1/models/film-2.py"
        )
    models = colleague / "samples" / "S1" / "models"
    assert (models / "film.py").read_bytes() == refined
    assert (models / "film-2.py").read_bytes() == SCRIPT


def test_a_fit_set_aside_is_listed_and_not_pulled(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    from nr_workbench.project.layout import ProjectLayout
    from nr_workbench.provenance import curation

    fitted(project)
    starred(project)
    pushed(project)
    curation.discard(ProjectLayout(root=project), A, reason="diverged")
    pushed(project)

    shown = pulled(colleague, "--fits")

    assert shown.exit_code == 0, shown.output
    assert f"- S1/{A}  discarded, dream, chisq 1.2: not pulled" in shown.output
    assert not (colleague / "samples" / "S1" / "results" / A).exists()
    assert not (colleague / "samples" / "S1" / "models" / "film.py").exists()


@pytest.mark.parametrize(
    "field", ["absolute data", "data", "problem", "fit file", "curve", "model"]
)
def test_a_record_naming_a_path_outside_its_place_is_refused_whole(
    project: Path,
    colleague: Path,
    tiled: FakeTiled,
    experiment: FakeNode,
    tmp_path: Path,
    field: str,
) -> None:
    fitted(project)
    starred(project)
    pushed(project)
    record = experiment[f"analysis/S1/{A}"].metadata
    outside = tmp_path / "outside.dat"
    value = {
        "absolute data": str(outside),
        "data": os.path.relpath(outside, colleague),
        "problem": "../film.json",
        "fit file": "../film.par",
        "curve": "../../film-1-refl.dat",
        "model": "../../film",
    }[field]
    if field.endswith("data"):
        record["data"] = {value: record["data"][STEADY]}
    elif field == "problem":
        record["problem"] = {**record["problem"], "name": value}
    elif field == "fit file":
        record["fit"] = {value: record["fit"]["film.par"]}
    elif field == "curve":
        record["curves"][0]["refl"]["name"] = value
    else:
        record["model"] = value

    shown = pulled(colleague, "--fits")

    assert shown.exit_code == 0, shown.output
    assert (
        f"- S1/{A}  starred, dream, chisq 1.2: its record names {value!r}, which "
        "would be written outside its place"
    ) in shown.output
    assert not outside.exists()
    assert not (colleague / "samples" / "S1" / "results").exists()
    assert index_of(colleague).fits() == []


@pytest.mark.parametrize("where", ["sample", "fit"])
def test_a_sample_or_fit_named_as_a_path_is_refused(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode, where: str
) -> None:
    from .test_tiled_sync import FakeSpec

    fitted(project)
    starred(project)
    pushed(project)
    record = dict(experiment[f"analysis/S1/{A}"].metadata)
    analysis = experiment["analysis"]
    if where == "sample":
        sample = analysis.create_container(
            "..", metadata={"sample_id": ".."}, specs=[FakeSpec("analysis-sample", "1")]
        )
        sample.create_container(
            B, metadata=record, specs=[FakeSpec("analysis-fit", "2")]
        )
        named = ".."
    else:
        named = "../../x"
        experiment["analysis/S1"].create_container(
            named, metadata=record, specs=[FakeSpec("analysis-fit", "2")]
        )

    shown = pulled(colleague, "--fits")

    assert shown.exit_code == 0, shown.output
    assert f"its record names {named!r}" in shown.output
    assert [e["fit_id"] for e in index_of(colleague).fits()] == [A]


def test_a_pulled_fit_line_is_a_plain_fit_line_for_that_fit(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    """A record's own index line cannot make an event, or speak for another
    fit: here, the colleague's own."""
    fitted(project)
    starred(project)
    pushed(project)
    fitted(colleague, B)
    record = experiment[f"analysis/S1/{A}"].metadata
    record["index"] = {**record["index"], "event": "delete", "fit_id": B}

    shown = pulled(colleague, "--fits")

    assert shown.exit_code == 0, shown.output
    lines = [e for e in index_of(colleague).entries() if e["fit_id"] in (A, B)]
    assert [(e["event"], e["fit_id"]) for e in lines] == [
        ("fit", B),
        ("fit", A),
        ("star", A),
    ]
    assert not curation_of(colleague, B).deleted


def test_a_push_after_a_pull_finds_the_fit_as_tiled_holds_it(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    """The pulled fit is starred here too, so the colleague's push writes it:
    its record is Tiled's, the reduced files it read still in it."""
    fitted(project)
    starred(project)
    pushed(project)
    pulled(colleague, "--fits")
    before = list(tiled.root.log)

    shown = nrw("push", "--root", str(colleague), "--yes", "--only", "fits")

    assert shown.exit_code == 0, shown.output
    assert "0 new, 0 changed, 1 the same" in shown.output
    assert tiled.root.log == before
    data = experiment[f"analysis/S1/{A}"].metadata["data"][STEADY]
    assert data["text"].encode() == DATA


def test_another_programs_node_in_analysis_is_left_alone(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    fitted(project)
    starred(project)
    pushed(project)
    experiment["analysis"].create_container("their-notes", metadata={"by": "them"})

    status = nrw("status", "--root", str(colleague))
    shown = pulled(colleague, "--fits")

    assert status.exit_code == 0, status.output
    assert shown.exit_code == 0, shown.output
    assert "/analysis: 1 fit(s)" in shown.output


# --------------------------------------------------------------------------
# All three
# --------------------------------------------------------------------------


def test_context_data_and_fits_are_pulled_in_that_order(
    project: Path,
    colleague: Path,
    tiled: FakeTiled,
    experiment: FakeNode,
    tmp_path_factory,
) -> None:
    catalogued(project)
    fitted(project)
    starred(project)
    pushed(project)
    reduced = FakeNode(content=b"q r")
    reduced.key = SEGMENT
    experiment.children["autoreduction"] = FakeNode({SEGMENT: reduced})
    folder = tmp_path_factory.mktemp("data")

    shown = pulled(colleague, "--fits", "--data", "--context", "--into", str(folder))

    assert shown.exit_code == 0, shown.output
    said = shown.output
    assert said.index("/context, into") < said.index("autoreduction ->")
    assert said.index("autoreduction ->") < said.index("/analysis: 1 fit(s)")
    assert (folder / SEGMENT).read_bytes() == b"q r"


# --------------------------------------------------------------------------
# `nrw tiled status`
# --------------------------------------------------------------------------


def test_status_says_what_a_push_writes_in_analysis(
    project: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    import json

    from nr_workbench.project.layout import ProjectLayout
    from nr_workbench.provenance import curation

    fitted(project)
    starred(project)
    before = nrw("status", "--root", str(project))
    pushed(project)
    curation.star(ProjectLayout(root=project), A, starred=False)

    after = nrw("status", "--root", str(project))
    found = json.loads(nrw("status", "--root", str(project), "--json").output)

    assert "analysis/   not made yet" in before.output
    assert f"  ~ S1/{A}" in after.output
    assert found["analysis"]["records"]["replace"] == [f"S1/{A}"]


def test_status_says_whether_each_fit_on_tiled_is_here(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    import json

    fitted(project)
    starred(project)
    pushed(project)

    before = nrw("status", "--root", str(colleague))
    pulled(colleague, "--fits")
    found = json.loads(nrw("status", "--root", str(colleague), "--json").output)

    assert "1 on Tiled alone" in before.output
    assert (
        f"= S1/{A}  starred, dream, chisq 1.2: not here; `nrw tiled pull --fits` "
        "brings it"
    ) in before.output
    (fit,) = found["analysis"]["fits"]
    assert (fit["fit_id"], fit["here"]) == (A, True)
    assert found["analysis"]["records"]["same"] == [f"S1/{A}"]
