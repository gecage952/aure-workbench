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

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from nr_workbench.cli import main
from nr_workbench.experiment.model import RunChange, RunKey
from nr_workbench.experiment.store import ParquetCatalogStore

from .test_tiled_analysis import (  # noqa: F401 - fixtures
    SCRIPT,
    SPEC,
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


COMMAND = (
    "nrw fit run samples/S1/models/film.py --method dream --samples 1000 "
    f"--burn 50 --seed 7 --note 'regenerated from {A}, pulled from Tiled'"
)


def test_a_fits_pull_writes_each_fits_model_and_says_how_to_run_it_again(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    fitted(project)
    starred(project)
    pushed(project)

    shown = pulled(colleague, "--fits")

    assert shown.exit_code == 0, shown.output
    models = colleague / "samples" / "S1" / "models"
    assert (models / "film.py").read_bytes() == SCRIPT
    assert (models / "film.yaml").read_text() == SPEC
    assert f"+ S1/{A}  starred, dream, chisq 1.2" in shown.output
    assert COMMAND in shown.output
    assert "S1: its data is not applied yet" in shown.output


def test_a_model_here_already_is_left_and_another_of_its_name_kept(
    project: Path, colleague: Path, tiled: FakeTiled, experiment: FakeNode
) -> None:
    fitted(project)
    starred(project)
    pushed(project)
    pulled(colleague, "--fits")

    again = pulled(colleague, "--fits")
    models = colleague / "samples" / "S1" / "models"
    (models / "film.py").write_text("# the colleague's own film\n")
    beside = pulled(colleague, "--fits")

    assert "samples/S1/models/film.py is here already" in again.output
    assert (models / "film.py").read_text() == "# the colleague's own film\n"
    assert (models / "film-2.py").read_bytes() == SCRIPT
    assert "nrw fit run samples/S1/models/film-2.py" in beside.output


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

    assert "Nothing written (--dry-run)." in shown.output
    assert not (colleague / "samples" / "S1" / "models" / "film.py").exists()


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
        assert f"+ S1/{B}  final" in shown.output
        assert "nrw fit run samples/S1/models/film.py --method dream" in shown.output
        assert "nrw fit run samples/S1/models/film-2.py --method dream" in shown.output
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
    assert not (colleague / "samples" / "S1" / "models" / "film.py").exists()


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


def test_status_says_whether_a_fit_on_tiled_has_its_model_here(
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
        f"= S1/{A}  starred, dream, chisq 1.2: `nrw tiled pull --fits` writes its "
        "model here"
    ) in before.output
    (fit,) = found["analysis"]["fits"]
    assert (fit["fit_id"], fit["here"]) == (A, "pulled")
