"""Curating fits: star, finalize, discard, restore, and delete their files.

Fits here are recorded the way `nrw fit run` records them -- an index entry and
a result directory holding a manifest and its inputs -- without fitting
anything: what is under test is what a person may say about a fit, and what
that may and may not do to it. The rules are one module's; the terminal and the
page are each tested for reaching them, and for nothing else.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nr_workbench.cli import main
from nr_workbench.project.layout import ProjectLayout
from nr_workbench.provenance import curation
from nr_workbench.provenance.index import FitIndex
from nr_workbench.web.app import create_app

A = "20260930-100000Z-aaaaaaaa"
B = "20260930-110000Z-bbbbbbbb"
C = "20260930-120000Z-cccccccc"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    result = CliRunner().invoke(main, ["init", str(root), "--sample", "S1"])
    assert result.exit_code == 0, result.output
    return root


def layout_of(project: Path) -> ProjectLayout:
    return ProjectLayout(root=project)


def index_of(project: Path) -> FitIndex:
    return FitIndex(project / ".nrw" / "index.jsonl")


def recorded(
    project: Path,
    fit_id: str,
    *,
    sample: str | None = "S1",
    status: str = "ok",
    inputs: tuple[str, ...] = (),
) -> Path:
    """A fit as `nrw fit run` leaves one: its entry, its directory, its inputs.

    With no sample, where a script run from outside samples/ records it.
    """
    directory = (
        project / "samples" / sample / "results" / fit_id
        if sample
        else project / "results" / fit_id
    )
    directory.mkdir(parents=True)
    # What the pages read of a real one: its chi-squared, and how many free.
    (directory / "manifest.json").write_text(
        json.dumps({"info": {"chisq": 1.2, "n_free": 3, "n_points": 30}})
    )
    listed = []
    for relative in inputs:
        data = (project / relative).read_bytes()
        listed.append(
            {
                "role": "data",
                "path": relative,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    (directory / "inputs.json").write_text(json.dumps({"inputs": listed}))
    index_of(project).append(
        {
            "fit_id": fit_id,
            "sample": sample,
            "model": "film",
            "status": status,
            "started_at": fit_id[:15],
            "chisq": 1.2,
            "n_free": 3,
        }
    )
    return directory


def events(project: Path, kind: str) -> list[dict]:
    return [e for e in index_of(project).entries() if e.get("event") == kind]


# --------------------------------------------------------------------------
# What a fit is now: the last word on it
# --------------------------------------------------------------------------


def test_each_fit_is_what_was_last_said_about_it(project: Path) -> None:
    for fit_id in (A, B):
        recorded(project, fit_id)
    layout = layout_of(project)

    curation.star(layout, A)
    curation.star(layout, A, starred=False)
    curation.star(layout, B)
    curation.discard(layout, A, reason="diverged")
    curation.restore(layout, A)

    said = curation.curation_of(index_of(project).entries())
    assert said[A] == curation.Curation()  # unstarred, restored
    assert said[B].starred is True
    # Saying it again adds nothing to the history.
    assert curation.star(layout, B) == ""
    assert len(events(project, "star")) == 2
    assert all(e["who"] and e["at"] for e in events(project, "star"))


def test_a_label_is_held_by_the_last_fit_of_its_sample_promoted_to_it(
    project: Path,
) -> None:
    recorded(project, A)
    recorded(project, B, sample="S2")
    recorded(project, C)
    layout = layout_of(project)

    for fit_id in (A, B, C):
        curation.promote(layout, fit_id, reason="the best of the three")

    said = curation.curation_of(index_of(project).entries())
    # A was superseded by C in S1; B is S2's own, untouched by either.
    assert A not in said or said[A].labels == ()
    assert (said[B].labels, said[C].labels) == (("final",), ("final",))
    (superseded,) = events(project, "supersede")
    assert (superseded["fit_id"], superseded["superseded_by"]) == (A, C)


def test_finalizing_says_when_the_inputs_changed_and_goes_ahead_only_when_told(
    project: Path,
) -> None:
    data = project / "samples" / "S1" / "data" / "steady" / "REFL_1_combined.txt"
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_text("0.01 1.0 0.1\n")
    recorded(project, A, inputs=(data.relative_to(project).as_posix(),))
    data.write_text("0.01 2.0 0.1\n")  # the data moved on
    layout = layout_of(project)

    with pytest.raises(curation.InputsChanged, match="STALE") as refused:
        curation.promote(layout, A, reason="looks right")
    promotion = curation.promote(layout, A, reason="looks right", force=True)

    assert refused.value.needs == "force"
    assert promotion["forced"] is True


# --------------------------------------------------------------------------
# Discard, then delete
# --------------------------------------------------------------------------


def test_a_discarded_fit_keeps_every_file_and_its_reason(project: Path) -> None:
    directory = recorded(project, A)

    curation.discard(
        curation_layout := layout_of(project), A, reason="  wrong \n model "
    )

    said = curation.curation_of(index_of(project).entries())[A]
    assert said.discarded["reason"] == "wrong model"
    assert (directory / "manifest.json").is_file()
    assert curation.restore(curation_layout, A) == A  # the whole id, as changed


@pytest.mark.parametrize(
    ("reason", "message"), [("  ", "required"), ("x" * 501, "limit")]
)
def test_a_discard_needs_a_short_reason(
    project: Path, reason: str, message: str
) -> None:
    recorded(project, A)

    with pytest.raises(curation.CurationRefused, match=message):
        curation.discard(layout_of(project), A, reason=reason)

    assert events(project, "discard") == []


def test_the_answer_is_never_set_aside(project: Path) -> None:
    recorded(project, A)
    layout = layout_of(project)
    curation.promote(layout, A, reason="the answer")

    with pytest.raises(curation.CurationRefused, match="Finalize another fit first"):
        curation.discard(layout, A, reason="changed my mind")


def test_a_discarded_fit_is_neither_starred_nor_finalized_until_restored(
    project: Path,
) -> None:
    recorded(project, A)
    layout = layout_of(project)
    curation.discard(layout, A, reason="noisy")

    with pytest.raises(curation.CurationRefused, match="restore it"):
        curation.star(layout, A)
    with pytest.raises(curation.CurationRefused, match="restore it"):
        curation.promote(layout, A, reason="after all")


def test_files_go_only_after_a_discard_and_the_record_that_it_ran_stays(
    project: Path,
) -> None:
    directory = recorded(project, A)
    layout = layout_of(project)

    with pytest.raises(curation.CurationRefused, match="Discard .* first"):
        curation.delete_files(layout, A)
    curation.discard(layout, A, reason="diverged")
    removed = curation.delete_files(layout, A)

    assert removed.as_posix() == f"samples/S1/results/{A}"
    assert not directory.exists()
    assert index_of(project).find(A) is not None  # it ran; that stays true
    (deleted,) = events(project, "delete")
    assert deleted["reason"] == "diverged"
    with pytest.raises(curation.CurationRefused, match="nothing to restore"):
        curation.restore(layout, A)


def cited_by_a_report(project: Path) -> None:
    reports = project / "samples" / "S1" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "summary-technical.md").write_text(
        f"# S1\n\nThe film is 80 A thick ({A}).\n"
    )


def read_by_a_figure(project: Path) -> None:
    reports = project / "samples" / "S1" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "plot.py.figures.json").write_text(json.dumps({"fits": [A]}))


def read_by_another_fit(project: Path) -> None:
    # A fit started from A's fitted values, which it recorded as an input.
    values = project / "samples" / "S1" / "results" / A / "fit" / "best.par"
    values.parent.mkdir(parents=True)
    values.write_text("thickness 80\n")
    recorded(project, B, inputs=(values.relative_to(project).as_posix(),))


def exported(project: Path) -> None:
    (project / "samples" / "S1" / "results" / A / "isaac").mkdir()


@pytest.mark.parametrize(
    ("use", "said"),
    [
        (cited_by_a_report, "the report samples/S1/reports/summary-technical.md"),
        (read_by_a_figure, "the figures of samples/S1/reports/plot.py.figures.json"),
        (read_by_another_fit, f"fit {B}, which read its files"),
        (exported, "the ISAAC records made from it"),
    ],
)
def test_files_something_uses_are_never_deleted(project: Path, use, said: str) -> None:
    directory = recorded(project, A)
    use(project)
    layout = layout_of(project)
    curation.discard(layout, A, reason="superseded by a better model")

    with pytest.raises(curation.CurationRefused, match="Its files stay") as refused:
        curation.delete_files(layout, A)

    assert said in str(refused.value)
    assert (directory / "manifest.json").is_file()
    assert events(project, "delete") == []


# --------------------------------------------------------------------------
# From the terminal
# --------------------------------------------------------------------------


def nrw(project: Path, monkeypatch, *args: str):
    monkeypatch.chdir(project)
    return CliRunner().invoke(main, list(args))


def test_the_fit_commands_curate_and_nrw_ls_leaves_the_discarded_out(
    project: Path, monkeypatch
) -> None:
    recorded(project, A)
    recorded(project, B)

    starred = nrw(project, monkeypatch, "fit", "star", A)
    early = nrw(project, monkeypatch, "fit", "delete", B, "--yes")
    discarded = nrw(project, monkeypatch, "fit", "discard", B, "--reason", "diverged")
    listed = nrw(project, monkeypatch, "ls")
    everything = nrw(project, monkeypatch, "ls", "--all")
    deleted = nrw(project, monkeypatch, "fit", "delete", B, "--yes")

    assert starred.exit_code == 0 and "Starred" in starred.output
    assert early.exit_code != 0 and "first" in early.output
    assert discarded.exit_code == 0, discarded.output
    row = next(line for line in listed.output.splitlines() if A in line)
    assert row.rstrip().endswith("★")  # on its own row, not only in the legend
    assert B not in listed.output and "nrw ls --all" in listed.output
    assert f"{B}" in everything.output and "(discarded)" in everything.output
    assert deleted.exit_code == 0, deleted.output
    assert not (project / "samples" / "S1" / "results" / B).exists()


def test_deleting_from_the_terminal_says_what_goes_and_asks(
    project: Path, monkeypatch
) -> None:
    directory = recorded(project, A)
    nrw(project, monkeypatch, "fit", "discard", A, "--reason", "diverged")

    monkeypatch.chdir(project)
    declined = CliRunner().invoke(main, ["fit", "delete", A], input="n\n")

    assert f"samples/S1/results/{A}/" in declined.output
    assert "Nothing deleted" in declined.output
    assert directory.is_dir()


def test_nrw_ls_marks_each_samples_final_fit(project: Path, monkeypatch) -> None:
    # The final fit of every sample -- not only the last one promoted anywhere.
    recorded(project, A)
    recorded(project, B, sample="S2")
    layout = layout_of(project)
    curation.promote(layout, A, reason="S1's answer")
    curation.promote(layout, B, reason="S2's answer")

    rows = [
        line
        for line in nrw(project, monkeypatch, "ls").output.splitlines()
        if line.strip().startswith("2026")
    ]

    assert len(rows) == 2
    assert all(line.rstrip().endswith("*") for line in rows), rows


def test_an_agent_curates_nothing(project: Path, monkeypatch) -> None:
    from nr_workbench.agent.guard import judge

    recorded(project, A)
    monkeypatch.setenv("NRW_AGENT", "1")

    refused = nrw(project, monkeypatch, "fit", "star", A)

    assert refused.exit_code != 0
    assert "a person's judgement" in refused.output
    assert events(project, "star") == []
    assert judge(f"nrw fit discard {A} --reason x").rule == "curate"
    assert judge(f"cd proj && nrw fit delete {A}").rule == "curate"
    # A fit run whose note happens to say "discard" is a fit run.
    assert judge("nrw fit run model.py --note discard").allowed


def test_nrw_check_says_nothing_of_a_fit_deleted_on_purpose(
    project: Path, monkeypatch
) -> None:
    from nr_workbench.commands.provenance_cmd import collect_problems

    recorded(project, A)
    layout = layout_of(project)
    curation.discard(layout, A, reason="diverged")
    curation.delete_files(layout, A)

    _, problems = collect_problems(layout, index_of(project))

    assert [p for p in problems if p["fit_id"] == A] == []


# --------------------------------------------------------------------------
# From the page
# --------------------------------------------------------------------------


@pytest.fixture
def app(project: Path):
    return create_app(project, autostart=False)


@pytest.fixture
def writer(app):
    return app.test_client()


def post(client, app, fit_id: str, action: str, body: dict | None = None):
    return client.post(f"/api/experiment/fits/{fit_id}/{action}", json=body or {})


def test_curating_from_the_page_needs_a_writable_server(project: Path) -> None:
    recorded(project, A)
    read_only = create_app(project, autostart=False, writable=False)

    refused = post(read_only.test_client(), read_only, A, "star", {"starred": True})

    assert refused.status_code == 403
    assert events(project, "star") == []


def test_the_page_names_a_fit_whole_never_by_a_prefix(app, writer, project) -> None:
    recorded(project, A)

    answer = post(writer, app, A[:10], "star", {"starred": True})

    assert answer.status_code == 404
    assert events(project, "star") == []


def test_the_page_stars_discards_restores_and_deletes(
    app, writer, project: Path
) -> None:
    directory = recorded(project, A)

    starred = post(writer, app, A, "star", {"starred": True}).json
    discarded = post(writer, app, A, "discard", {"reason": "diverged"}).json
    unconfirmed = post(writer, app, A, "delete", {})
    wrong = post(writer, app, A, "delete", {"confirm": B})
    deleted = post(writer, app, A, "delete", {"confirm": A})

    assert starred["curation"]["starred"] is True
    assert discarded["curation"]["discarded"]["reason"] == "diverged"
    assert discarded["delete_refusal"] is None  # nothing uses it: it may go
    assert (unconfirmed.status_code, wrong.status_code) == (400, 400)
    assert deleted.status_code == 200, deleted.json
    assert deleted.json["removed"] == f"samples/S1/results/{A}"
    assert not directory.exists()


def test_the_page_says_before_asking_why_a_fits_files_must_stay(
    app, writer, project: Path
) -> None:
    recorded(project, A)
    exported(project)

    discarded = post(writer, app, A, "discard", {"reason": "old"}).json
    refused = post(writer, app, A, "delete", {"confirm": A})

    assert "ISAAC records" in discarded["delete_refusal"]
    assert refused.status_code == 409


def test_finalizing_from_the_page_asks_again_when_the_inputs_changed(
    app, writer, project: Path
) -> None:
    data = project / "samples" / "S1" / "data" / "steady" / "REFL_1_combined.txt"
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_text("0.01 1.0 0.1\n")
    recorded(project, A, inputs=(data.relative_to(project).as_posix(),))
    recorded(project, B)
    post(writer, app, B, "finalize", {"reason": "first answer"})
    data.write_text("0.01 2.0 0.1\n")

    asked = post(writer, app, A, "finalize", {"reason": "better"})
    forced = post(writer, app, A, "finalize", {"reason": "better", "force": True})

    assert (asked.status_code, asked.json["needs"]) == (409, "force")
    assert forced.status_code == 200, forced.json
    assert forced.json["curation"]["labels"] == ["final"]
    assert forced.json["supersedes"] == B
    listed = {r["fit_id"]: r["labels"] for r in app.config["NRW_DATA"].fits()}
    assert (listed[A], listed[B]) == (["final"], [])  # B is final no longer


def test_the_fits_list_keeps_the_discarded_behind_a_toggle(
    app, writer, project: Path
) -> None:
    recorded(project, A)
    recorded(project, B)
    post(writer, app, B, "discard", {"reason": "diverged"})

    page = writer.get("/fits").get_data(as_text=True)

    assert "Show the discarded" in page
    assert "1 recorded, and 1 discarded" in page
    assert page.count("data-discarded") == 1
    sample = writer.get("/s/S1").get_data(as_text=True)
    assert A in sample and B not in sample


# --------------------------------------------------------------------------
# What the reviews found
# --------------------------------------------------------------------------


def cited_by_another_samples_report(project: Path) -> None:
    reports = project / "samples" / "S2" / "reports"
    reports.mkdir(parents=True)
    (reports / "compare-technical.md").write_text(f"# S2\n\nAgainst S1's {A}.\n")


def read_by_another_samples_figure(project: Path) -> None:
    reports = project / "samples" / "S2" / "reports"
    reports.mkdir(parents=True)
    (reports / "p.py.figures.json").write_text(json.dumps({"fits": [A]}))


def read_by_a_project_figure(project: Path) -> None:
    reports = project / "reports"
    reports.mkdir()
    (reports / "p.py.figures.json").write_text(json.dumps({"fits": [A]}))


def cited_by_a_summary_only(project: Path) -> None:
    # The technical tier does not cite it; the plain summary does.
    reports = project / "samples" / "S1" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "summary-technical.md").write_text("# S1\n\nNothing final yet.\n")
    (reports / "summary-plain.md").write_text(f"# S1\n\nThe film is 80 A ({A}).\n")


def unreadable_figure(project: Path) -> None:
    reports = project / "samples" / "S1" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "p.py.figures.json").write_text("{not json")


@pytest.mark.parametrize(
    ("use", "said"),
    [
        (cited_by_another_samples_report, "compare-technical.md"),
        (read_by_another_samples_figure, "samples/S2/reports/p.py.figures.json"),
        (read_by_a_project_figure, "the figures of reports/p.py.figures.json"),
        (cited_by_a_summary_only, "summary-plain.md"),
        # A guard on a deletion does not fail open.
        (unreadable_figure, "which cannot be read"),
    ],
)
def test_files_used_anywhere_in_the_project_are_never_deleted(
    project: Path, use, said: str
) -> None:
    (project / "samples" / "S2").mkdir(exist_ok=True)
    directory = recorded(project, A)
    use(project)
    layout = layout_of(project)
    curation.discard(layout, A, reason="superseded")

    with pytest.raises(curation.CurationRefused, match="Its files stay") as refused:
        curation.delete_files(layout, A)

    assert said in str(refused.value)
    assert (directory / "manifest.json").is_file()


def test_a_fit_with_no_sample_is_final_in_a_slot_of_its_own(
    app, writer, project: Path
) -> None:
    recorded(project, A)
    recorded(project, C, sample=None)  # a script run from outside samples/
    layout = layout_of(project)
    curation.promote(layout, A, reason="S1's answer")

    promotion = curation.promote(layout, C, reason="the loose one's")

    assert promotion["supersedes"] is None
    assert events(project, "supersede") == []
    said = curation.curation_of(index_of(project).entries())
    assert (said[A].labels, said[C].labels) == (("final",), ("final",))
    assert writer.get(f"/f/{C}").status_code == 200


def test_another_samples_final_fit_is_not_this_samples(
    app, writer, project: Path
) -> None:
    recorded(project, A)
    recorded(project, B, sample="S2")
    curation.promote(layout_of(project), B, reason="S2's answer")

    state = app.config["NRW_CURATION"].state(A)

    assert state["final"] is None  # S1 has none: B is S2's


def test_a_restored_fit_is_listed_starred_and_finalized_again(
    app, writer, project: Path, monkeypatch
) -> None:
    recorded(project, A)
    post(writer, app, A, "discard", {"reason": "hasty"})

    restored = post(writer, app, A, "restore")
    starred = post(writer, app, A, "star", {"starred": True})
    unstarred = nrw(project, monkeypatch, "fit", "unstar", A)
    finalized = post(writer, app, A, "finalize", {"reason": "it held up"})
    discarded = post(writer, app, A, "discard", {"reason": "again"})

    assert (
        restored.status_code == 200 and restored.json["curation"]["discarded"] is None
    )
    assert starred.json["curation"]["starred"] is True
    assert "Unstarred" in unstarred.output
    assert finalized.status_code == 200, finalized.json
    assert discarded.status_code == 409  # the answer, now


def test_restoring_from_the_terminal_says_which_fit(project: Path, monkeypatch) -> None:
    recorded(project, A)
    nrw(project, monkeypatch, "fit", "discard", A[-8:], "--reason", "hasty")

    restored = nrw(project, monkeypatch, "fit", "restore", A[-8:])

    assert restored.exit_code == 0, restored.output
    assert f"Restored {A}." in restored.output  # the whole id, for a prefix typed


def test_only_a_successful_fit_is_finalized(app, writer, project: Path) -> None:
    recorded(project, A, status="failed")

    with pytest.raises(curation.CurationRefused, match="Only a successful fit"):
        curation.promote(layout_of(project), A, reason="looks right")
    refused = post(writer, app, A, "finalize", {"reason": "looks right"})

    assert refused.status_code == 409
    # It can still be set aside, and its files freed.
    assert post(writer, app, A, "discard", {"reason": "crashed"}).status_code == 200
    assert post(writer, app, A, "delete", {"confirm": A}).status_code == 200


def test_a_label_other_than_final_does_not_keep_a_fit_from_being_discarded(
    project: Path,
) -> None:
    recorded(project, A)
    layout = layout_of(project)
    curation.promote(layout, A, label="best", reason="the best so far")

    assert curation.discard(layout, A, reason="a better one came") == A


def test_nrw_check_says_nothing_of_a_discarded_fit_whose_inputs_changed(
    project: Path,
) -> None:
    from nr_workbench.commands.provenance_cmd import collect_problems

    data = project / "samples" / "S1" / "data" / "steady" / "REFL_1_combined.txt"
    data.parent.mkdir(parents=True, exist_ok=True)
    data.write_text("0.01 1.0 0.1\n")
    recorded(project, A, inputs=(data.relative_to(project).as_posix(),))
    data.write_text("0.01 2.0 0.1\n")
    curation.discard(layout_of(project), A, reason="set aside")

    _, problems = collect_problems(layout_of(project), index_of(project))

    assert [p for p in problems if p["fit_id"] == A] == []


def test_a_fit_is_curated_by_its_whole_id_beside_a_same_second_replicate(
    project: Path,
) -> None:
    recorded(project, A)
    recorded(project, A + "-2")  # `create_unique` names a replicate so

    assert curation.star(layout_of(project), A) == A
    assert curation.curation_of(index_of(project).entries())[A].starred


@pytest.mark.parametrize(
    "entry",
    [
        {"fit_id": A, "sample": "../../elsewhere"},
        {"fit_id": "..", "sample": "S1"},
        {"fit_id": "../../elsewhere/results/x", "sample": "S1"},
    ],
)
def test_an_index_entry_naming_a_directory_elsewhere_names_nothing(
    project: Path, tmp_path: Path, entry: dict
) -> None:
    # The index is committed: anyone who can commit to the project writes it.
    from nr_workbench.provenance.lookup import fit_dir

    target = tmp_path / "elsewhere" / "results" / A
    target.mkdir(parents=True)
    (target / "manifest.json").write_text("{}")

    assert fit_dir(layout_of(project), entry) is None
    assert (target / "manifest.json").is_file()


def test_a_reset_keeps_every_samples_final_and_published_fits(
    project: Path, monkeypatch
) -> None:
    # S1's final was promoted before S2's: the reset once asked only for the
    # project's last promotion, and deleted S1's answer.
    recorded(project, A)
    recorded(project, B, sample="S2")
    layout = layout_of(project)
    curation.promote(layout, A, reason="S1's answer")
    curation.promote(layout, B, reason="S2's answer")

    refused = nrw(project, monkeypatch, "sample", "reset", "S1", "--yes")

    assert refused.exit_code != 0
    assert A in refused.output and "promoted as final" in refused.output
    assert (project / "samples" / "S1" / "results" / A).is_dir()


def test_a_removal_that_fails_part_way_is_recorded_and_says_whats_left(
    project: Path, monkeypatch
) -> None:
    recorded(project, A)
    layout = layout_of(project)
    curation.discard(layout, A, reason="diverged")

    def busy(*args, **kwargs):
        raise OSError(16, "Device or resource busy")

    busy.avoids_symlink_attacks = True  # as the real one says, where it can
    monkeypatch.setattr(curation.shutil, "rmtree", busy)

    with pytest.raises(curation.CurationRefused, match="remove it by hand") as refused:
        curation.delete_files(layout, A)

    (deleted,) = events(project, "delete")
    assert deleted["partial"] is True
    left = project / "samples" / "S1" / "results"
    assert [p.name for p in left.iterdir() if p.name.startswith(".deleting-")]
    assert ".deleting-" in str(refused.value)


def test_every_samples_final_fit_is_final_to_the_check_and_the_report(
    project: Path,
) -> None:
    from nr_workbench.commands.provenance_cmd import check_reported_finality
    from nr_workbench.commands.report import sequence_markdown

    (project / "samples" / "S2").mkdir(exist_ok=True)
    (project / "samples" / "S2" / "sample.md").write_text("# S2\n")
    recorded(project, A)
    recorded(project, B, sample="S2")
    layout = layout_of(project)
    curation.promote(layout, A, reason="S1's answer")  # before S2's
    curation.promote(layout, B, reason="S2's answer")
    reports = project / "samples" / "S1" / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "summary-technical.md").write_text(f"# S1\n\nThe final fit is {A}.\n")

    problems = check_reported_finality(layout, index_of(project))
    table, _ = sequence_markdown(layout, "S1")

    assert problems == []
    assert "**[FINAL]**" in table


def test_no_library_call_curates_unattended(project: Path, monkeypatch) -> None:
    # Below the commands, as well as in them: not a way around the refusal.
    recorded(project, A)
    monkeypatch.setenv("NRW_AGENT", "1")
    layout = layout_of(project)

    for call in (
        lambda: curation.star(layout, A),
        lambda: curation.discard(layout, A, reason="x"),
        lambda: curation.promote(layout, A, reason="x"),
    ):
        with pytest.raises(curation.CurationRefused, match="NRW_AGENT is set"):
            call()

    assert [e for e in index_of(project).entries() if e.get("event") != "fit"] == []


def test_a_reset_is_a_persons_call_but_seeing_what_it_would_do_is_not(
    project: Path, monkeypatch
) -> None:
    from nr_workbench.agent.guard import judge

    recorded(project, A)
    monkeypatch.setenv("NRW_AGENT", "1")

    refused = nrw(project, monkeypatch, "sample", "reset", "S1", "--yes")
    previewed = nrw(project, monkeypatch, "sample", "reset", "S1", "--dry-run")

    assert refused.exit_code != 0 and "NRW_AGENT is set" in refused.output
    assert previewed.exit_code == 0, previewed.output
    assert (project / "samples" / "S1" / "results" / A).is_dir()
    assert judge("nrw sample reset S1 --yes").rule == "reset"
    assert judge("nrw sample reset S1 --dry-run").allowed
    # A quote the parser cannot split is judged as text, and still refused.
    assert judge("nrw sample reset S1 --yes $'it\\'s'").rule == "reset"
    assert judge("nrw sample reset S1 --dry-run $'it\\'s'").allowed
