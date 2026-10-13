"""The analysis notebook: linking prose to the fits it is about.

The design question behind these tests is why 25 result directories in a real
beamtime all held the untouched template. Three answers, each tested here:
nothing asked, nothing read it back, and nothing carried it anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nr_workbench import notes
from nr_workbench.cli import main
from nr_workbench.notes import (
    NOTES_TEMPLATE,
    fits_mentioned,
    frontmatter,
    is_blank,
    notes_about,
    read_note,
    sample_notes,
)

from .test_lifecycle import write_partials

FIT_A = "20260807-163359Z-0103d9c7"
FIT_B = "20260806-205951Z-78c5736e"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project with one sample carrying real steady-state data."""
    root = tmp_path / "proj"
    result = CliRunner().invoke(main, ["init", str(root), "--sample", "S1"])
    assert result.exit_code == 0, result.output
    write_partials(root / "samples" / "S1" / "data" / "steady", 100001)
    return root


def run(root: Path, monkeypatch, *args: str):
    """Invoke the CLI inside a project."""
    monkeypatch.chdir(root)
    return CliRunner().invoke(main, list(args))


@pytest.fixture
def fitted(project: Path, monkeypatch) -> tuple[Path, str]:
    """A project with one real fit."""
    pytest.importorskip("refl1d")
    run(project, monkeypatch, "model", "new", "S1", "--name", "m")
    run(project, monkeypatch, "model", "generate", "samples/S1/models/m.yaml")
    result = run(
        project,
        monkeypatch,
        "fit",
        "run",
        "samples/S1/models/m.py",
        "--method",
        "amoeba",
        "--steps",
        "20",
        "--seed",
        "1",
        "--parallel",
        "1",
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(run(project, monkeypatch, "ls", "--json").stdout)
    return project, rows[0]["fit_id"]


# --------------------------------------------------------------------------
# Linking: the one piece of structure imposed on a note
# --------------------------------------------------------------------------


def test_a_fit_is_linked_by_being_named_in_the_prose() -> None:
    """People cite fits in sentences. A scheme that only read frontmatter
    would find nothing in the 609 lines of findings a real beamtime produced.
    """
    text = f"Two tNR models, {FIT_A} and {FIT_B}, differ only in the oxide."

    assert fits_mentioned(text) == [FIT_A, FIT_B]


def test_frontmatter_links_are_merged_with_prose_ones(tmp_path: Path) -> None:
    path = tmp_path / "n.md"
    path.write_text(
        f"---\nfits: [{FIT_B}]\n---\n\n# t\n\nSee {FIT_A}.\n", encoding="utf-8"
    )

    note = read_note(path, tmp_path, scope="sample")

    assert set(note.fits) == {FIT_A, FIT_B}


def test_a_collision_suffixed_fit_id_still_links() -> None:
    """`create_unique` appends -2 when two fits share a second."""
    assert fits_mentioned(f"see {FIT_A}-2 for the replicate") == [f"{FIT_A}-2"]


def test_a_run_number_is_not_mistaken_for_a_fit_id() -> None:
    """Six-digit run numbers are everywhere in this prose."""
    assert fits_mentioned("runs 218386 and 218393 were co-refined") == []


def test_malformed_frontmatter_does_not_lose_the_prose(tmp_path: Path) -> None:
    """A note is prose first; a YAML slip must not make it unreadable."""
    path = tmp_path / "n.md"
    path.write_text("---\nfits: [unclosed\n---\n\nThe oxide is real.\n", "utf-8")

    note = read_note(path, tmp_path, scope="sample")

    assert note is not None
    assert frontmatter(path.read_text()) == {}
    assert "oxide is real" in note.text


# --------------------------------------------------------------------------
# An unfilled template is not a note
# --------------------------------------------------------------------------


def test_the_template_reads_as_blank() -> None:
    """Treating a stub as content is how a UI ends up showing HTML comments
    to every visitor, which teaches readers the panel is noise."""
    assert is_blank(NOTES_TEMPLATE.format(fit_id="x", description="amoeba fit"))


def test_a_single_sentence_makes_it_no_longer_blank() -> None:
    filled = (
        NOTES_TEMPLATE.format(fit_id="x", description="d")
        + "\nThe oxide is required.\n"
    )

    assert not is_blank(filled)


def test_the_old_two_comment_stub_reads_as_blank() -> None:
    """Every result directory of the first real beamtime holds this."""
    assert is_blank(
        "<!-- The only file in this directory you should edit. -->\n"
        "<!-- Everything else is a record of what ran. -->\n\n"
    )


def test_a_blank_note_is_not_offered_as_evidence_of_thinking(tmp_path: Path) -> None:
    path = tmp_path / "NOTES.md"
    path.write_text(NOTES_TEMPLATE.format(fit_id=FIT_A, description="d"), "utf-8")
    note = read_note(path, tmp_path, scope="fit", fit_id=FIT_A)

    assert notes_about([note], FIT_A) == []


def test_the_template_asks_questions_rather_than_granting_permission() -> None:
    """Its predecessor said which file you were allowed to edit and was never
    once written in. A blank page with no question is a file you close."""
    text = NOTES_TEMPLATE.format(fit_id="x", description="d")

    assert "?" in text
    assert "## Why this run" in text
    assert "## Caveats" in text


def test_the_run_note_is_quoted_so_it_never_wins_the_summary(tmp_path: Path) -> None:
    """The template echoes --note back. If that counted as the summary, the
    listing would show you your own command instead of your conclusion."""
    path = tmp_path / "NOTES.md"
    path.write_text(
        NOTES_TEMPLATE.format(fit_id=FIT_A, description="a dream run")
        + "\nThe oxide is required.\n",
        encoding="utf-8",
    )

    note = read_note(path, tmp_path, scope="fit", fit_id=FIT_A)

    assert note.summary == "The oxide is required."


def test_a_persons_words_outrank_the_generated_assessment(tmp_path: Path) -> None:
    """`nrw assess` usually runs first, so without this the listing would
    always show a chi-squared back to you rather than what you concluded."""
    path = tmp_path / "NOTES.md"
    path.write_text(
        "# f\n\nThe ceiling was the problem.\n\n"
        "## Assessment\n\nchi-squared 1.2, 8 free\n",
        encoding="utf-8",
    )

    note = read_note(path, tmp_path, scope="fit", fit_id=FIT_A)

    assert note.summary == "The ceiling was the problem."


# --------------------------------------------------------------------------
# The CLI
# --------------------------------------------------------------------------


def test_note_appends_to_a_fit_and_reads_it_back(
    fitted: tuple[Path, str], monkeypatch
) -> None:
    root, fit_id = fitted
    short = fit_id.rpartition("-")[2]

    written = run(root, monkeypatch, "note", short, "-m", "the oxide is required")
    assert written.exit_code == 0, written.output

    shown = run(root, monkeypatch, "note", short)

    assert "the oxide is required" in shown.output


def test_a_fit_resolves_by_its_hash_not_only_its_timestamp(
    fitted: tuple[Path, str], monkeypatch
) -> None:
    """The half of a fit id that distinguishes two runs from one afternoon is
    the hash; prefix-only matching would mean typing the other sixteen
    characters to reach it."""
    root, fit_id = fitted

    result = run(root, monkeypatch, "note", fit_id.rpartition("-")[2][:5], "-m", "x")

    assert result.exit_code == 0, result.output


def test_a_sample_report_lands_in_reports(
    fitted: tuple[Path, str], monkeypatch
) -> None:
    root, _ = fitted

    result = run(
        root,
        monkeypatch,
        "note",
        "--sample",
        "S1",
        "--title",
        "why the tNR is fitted alone",
        "-m",
        "because of the gap",
    )

    assert result.exit_code == 0, result.output
    written = root / "samples" / "S1" / "reports" / "why-the-tnr-is-fitted-alone.md"
    assert written.is_file()
    assert "because of the gap" in written.read_text(encoding="utf-8")


def test_a_report_citing_a_fit_shows_up_on_that_fit(
    fitted: tuple[Path, str], monkeypatch
) -> None:
    """The whole linking mechanism, end to end: name the id in a sentence."""
    root, fit_id = fitted
    run(
        root,
        monkeypatch,
        "note",
        "--sample",
        "S1",
        "--title",
        "the argument",
        "-m",
        f"Both {fit_id} and the next one rail the thickness.",
    )

    shown = run(root, monkeypatch, "note", fit_id.rpartition("-")[2])

    assert "1 report(s) mention this fit" in shown.output
    assert "the argument" in shown.output


def test_naming_both_a_fit_and_a_sample_is_refused(
    fitted: tuple[Path, str], monkeypatch
) -> None:
    root, fit_id = fitted

    result = run(root, monkeypatch, "note", fit_id, "--sample", "S1", "-m", "x")

    assert result.exit_code != 0
    assert "not both" in result.output


def test_note_with_no_target_explains_the_two_kinds(project: Path, monkeypatch) -> None:
    result = run(project, monkeypatch, "note")

    assert result.exit_code != 0
    assert "--sample" in result.output


def test_ls_marks_which_fits_have_reasoning_recorded(
    fitted: tuple[Path, str], monkeypatch
) -> None:
    """Reading the notebook is what makes writing in it worthwhile."""
    root, fit_id = fitted
    before = run(root, monkeypatch, "ls").output
    assert "nothing written down" in before

    run(
        root,
        monkeypatch,
        "note",
        fit_id.rpartition("-")[2],
        "-m",
        "the ceiling was wrong",
    )
    after = run(root, monkeypatch, "ls").output

    assert "the ceiling was wrong" in after
    assert "nothing written down" not in after


# --------------------------------------------------------------------------
# Notes reach a collaborator
# --------------------------------------------------------------------------


def test_pack_carries_the_fit_note_and_the_reports_that_cite_it(
    fitted: tuple[Path, str], monkeypatch, tmp_path: Path
) -> None:
    """A bundle that carries the numbers and not the argument transmits a
    result without transmitting what anyone concluded from it."""
    root, fit_id = fitted
    run(
        root,
        monkeypatch,
        "note",
        fit_id.rpartition("-")[2],
        "-m",
        "the oxide is required",
    )
    run(
        root,
        monkeypatch,
        "note",
        "--sample",
        "S1",
        "--title",
        "the argument",
        "-m",
        f"See {fit_id}: the ceiling was the problem.",
    )
    run(
        root,
        monkeypatch,
        "note",
        "--sample",
        "S1",
        "--title",
        "unrelated",
        "-m",
        "This one names no fit at all.",
    )

    bundle = tmp_path / "b"
    result = run(root, monkeypatch, "pack", fit_id, "--dir", "--out", str(bundle))
    assert result.exit_code == 0, result.output

    assert "the oxide is required" in (
        bundle / "original-results" / "NOTES.md"
    ).read_text(encoding="utf-8")
    assert (bundle / "notes" / "the-argument.md").is_file()
    assert not (bundle / "notes" / "unrelated.md").exists(), (
        "a bundle is one fit; a report about other results is not about this one"
    )
    assert "the-argument.md" in (bundle / "README.md").read_text(encoding="utf-8")


def test_a_bundle_with_no_prose_says_so_rather_than_staying_quiet(
    fitted: tuple[Path, str], monkeypatch, tmp_path: Path
) -> None:
    """Silence reads as "there was nothing to say"."""
    root, fit_id = fitted
    bundle = tmp_path / "b"

    result = run(root, monkeypatch, "pack", fit_id, "--dir", "--out", str(bundle))

    assert "carries no reasoning" in result.output
    readme = (bundle / "README.md").read_text(encoding="utf-8")
    assert "Nothing was written down about this fit" in readme


def test_the_web_renders_notes_and_hides_unfilled_templates(
    fitted: tuple[Path, str], monkeypatch
) -> None:
    """The stub is truthy, so the old panel rendered its HTML comments to
    every visitor -- which taught readers the panel was noise."""
    from nr_workbench.web.app import create_app

    root, fit_id = fitted
    client = create_app(root).test_client()

    bare = client.get(f"/f/{fit_id}").get_data(as_text=True)
    assert "The only file in this directory" not in bare
    assert "nrw note" in bare, "and it says how to fix that"

    run(
        root,
        monkeypatch,
        "note",
        fit_id.rpartition("-")[2],
        "-m",
        "the oxide is required",
    )
    filled = create_app(root).test_client().get(f"/f/{fit_id}").get_data(as_text=True)

    assert "the oxide is required" in filled


def test_reports_are_no_longer_discarded_on_import() -> None:
    """`nrw import` listed `reports` as derived, so it skipped exactly the
    prose the layout tells users to put there -- the one thing in a legacy
    directory that cannot be regenerated."""
    from nr_workbench.project.importer import DERIVED_DIRS

    assert "reports" not in DERIVED_DIRS


def test_sample_notes_ignores_a_directory_with_no_reports(tmp_path: Path) -> None:
    assert sample_notes(tmp_path, "nope") == []


def test_a_generated_assessment_does_not_count_as_thinking() -> None:
    """`nrw assess` writes into the same NOTES.md a person writes into. Its
    facts line is bare prose, so before the fence every assessed fit read as
    "somebody thought about this" in `nrw ls` -- defeating the guard that
    exists to stop unfilled templates counting as evidence.
    """
    from nr_workbench.fitting.assess import Assessment, as_markdown

    stub = NOTES_TEMPLATE.format(fit_id=FIT_A, description="a dream run")
    assessed = (
        stub
        + "\n"
        + as_markdown(
            Assessment(fit_id=FIT_A, chisq=1.285, n_free=8, n_points=3915, bic=1047.9)
        )
    )

    assert is_blank(assessed), "generated prose is not a note"
    assert not is_blank(assessed + "\nThe oxide is required.\n")


def test_a_persons_words_survive_the_fence(tmp_path: Path) -> None:
    """Stripping the generated region must not take the human's text with it,
    wherever it sits relative to the assessment."""
    from nr_workbench.fitting.assess import Assessment, as_markdown

    generated = as_markdown(Assessment(fit_id=FIT_A, chisq=1.2))
    path = tmp_path / "NOTES.md"
    path.write_text(
        f"# {FIT_A}\n\nBefore the assessment.\n\n{generated}\nAfter it.\n",
        encoding="utf-8",
    )

    note = read_note(path, tmp_path, scope="fit", fit_id=FIT_A)

    assert note.summary == "Before the assessment."
    assert not note.blank


def test_an_unterminated_fence_hides_the_rest_rather_than_trusting_it() -> None:
    """The safe direction: unmarked generated prose counted as human is the
    failure worth avoiding, so a truncated write reads as blank."""
    from nr_workbench.notes import GENERATED_OPEN

    assert is_blank(f"# f\n\n{GENERATED_OPEN}\nchi-squared 1.2, 8 free\n")


# --------------------------------------------------------------------------
# Filling the template's sections
#
# The template asks three questions and `-m` appended below all of them, so
# every note in the reference experiment has three empty headings and one
# paragraph at the bottom answering some mixture of them.
# --------------------------------------------------------------------------

FILLED_TEMPLATE = NOTES_TEMPLATE.format(
    fit_id="20260807-163359Z-0103d9c7", description="dream fit of backrefl3."
)


def test_a_message_lands_under_its_own_heading() -> None:
    result = notes.write_section(FILLED_TEMPLATE, "Why this run", "testing the oxide")

    why = result.index("## Why this run")
    showed = result.index("## What it showed")
    assert why < result.index("testing the oxide") < showed


def test_the_prompt_comment_is_kept_and_the_prose_follows_it() -> None:
    """The comment is guidance for the next reader; deleting it is not our call."""
    result = notes.write_section(FILLED_TEMPLATE, "Caveats", "rho was pinned")

    assert "degeneracy you broke" in result
    assert result.index("degeneracy you broke") < result.index("rho was pinned")


def test_writing_to_every_section_keeps_them_in_template_order() -> None:
    text = FILLED_TEMPLATE
    for heading, message in (
        ("Why this run", "why text"),
        ("What it showed", "showed text"),
        ("Caveats", "caveat text"),
    ):
        text = notes.write_section(text, heading, message)

    assert (
        text.index("why text") < text.index("showed text") < text.index("caveat text")
    )


def test_prose_goes_before_a_generated_assessment_block() -> None:
    """`## Caveats` is the last template section, and `nrw assess` appends a
    block containing its own `## Assessment` heading. The section boundary has
    to be that heading, not the end of the file."""
    text = FILLED_TEMPLATE + (
        "\n<!-- nrw:generated -->\n## Assessment\n\nchi-squared 2.94\n"
        "<!-- /nrw:generated -->\n"
    )

    result = notes.write_section(text, "Caveats", "rho was pinned")

    assert result.index("rho was pinned") < result.index("<!-- nrw:generated -->")


def test_a_missing_heading_is_appended_rather_than_dropped() -> None:
    """A note predating the template, or one whose author deleted a heading."""
    result = notes.write_section("# 20260807-163359Z-0103d9c7\n", "Caveats", "careful")

    assert "## Caveats" in result
    assert "careful" in result


def test_an_empty_message_changes_nothing() -> None:
    assert notes.write_section(FILLED_TEMPLATE, "Caveats", "   ") == FILLED_TEMPLATE


def test_writing_twice_to_one_section_keeps_both(tmp_path: Path) -> None:
    text = notes.write_section(FILLED_TEMPLATE, "Caveats", "first")

    result = notes.write_section(text, "Caveats", "second")

    assert result.index("first") < result.index("second")
    assert result.index("second") < result.index("## Caveats") + len(result)


def test_filled_sections_count_as_a_person_having_written_something() -> None:
    """The blank-note signal has to see them, or `nrw ls` still nags."""
    filled = notes.write_section(FILLED_TEMPLATE, "Why this run", "testing the oxide")

    assert not notes.is_blank(filled)
    assert notes.is_blank(FILLED_TEMPLATE), "the bare template is still blank"


# --------------------------------------------------------------------------
# The stub `nrw fit run` writes
#
# `--note` is the reason, typed at the one moment it is in front of the person
# running the fit. It used to go into the blockquote, which `is_blank` skips by
# design -- so the sentence was written, stored, and counted as nothing.
# --------------------------------------------------------------------------


def _stub(tmp_path: Path, **kwargs: str) -> str:
    """Write a stub into a directory and return its text."""
    from nr_workbench.provenance.record import FitDirectory

    FitDirectory(tmp_path).write_notes_stub(fit_id=FIT_A, **kwargs)
    return (tmp_path / "NOTES.md").read_text(encoding="utf-8")


def test_the_launch_reason_lands_under_the_heading_that_asks_for_it(
    tmp_path: Path,
) -> None:
    text = _stub(tmp_path, description="amoeba fit of film.", why="testing the oxide")

    why = text.index("## Why this run")
    showed = text.index("## What it showed")
    assert why < text.index("testing the oxide") < showed


def test_a_fit_run_with_a_reason_does_not_read_as_unwritten(tmp_path: Path) -> None:
    """The whole point: `nrw ls` and `report --check` must stop counting a fit
    whose reason *was* stated among the ones nobody thought about."""
    text = _stub(tmp_path, description="amoeba fit of film.", why="testing the oxide")

    assert not is_blank(text)


def test_the_blockquote_still_says_what_ran_not_why(tmp_path: Path) -> None:
    """Two different things. Folding the reason into the description is what
    put it in scaffolding in the first place."""
    text = _stub(tmp_path, description="amoeba fit of film.", why="testing the oxide")

    assert "> amoeba fit of film." in text


def test_a_fit_run_with_no_reason_is_still_blank(tmp_path: Path) -> None:
    """Nothing here may invent the human half. A run launched without `--note`
    has had no reason recorded, and must go on saying so."""
    assert is_blank(_stub(tmp_path, description="amoeba fit of film."))


# --------------------------------------------------------------------------
# Reading a section back
#
# `write_section` had no counterpart, so a listing that wanted to show *why* a
# fit was run could only show how it was configured -- and every field of that
# is already a column of the table.
# --------------------------------------------------------------------------


def test_a_written_section_reads_back() -> None:
    text = notes.write_section(FILLED_TEMPLATE, "Why this run", "testing the oxide")

    assert notes.read_section(text, "Why this run") == "testing the oxide"


def test_an_untouched_section_reads_as_empty_not_as_its_own_prompt() -> None:
    """The template's questions rendered as answers is how a panel teaches
    its readers to ignore it."""
    assert notes.read_section(FILLED_TEMPLATE, "Why this run") == ""
    assert notes.read_section(FILLED_TEMPLATE, "Caveats") == ""


def test_a_section_stops_at_the_next_heading() -> None:
    text = notes.write_section(FILLED_TEMPLATE, "Why this run", "the why")
    text = notes.write_section(text, "What it showed", "the result")

    assert notes.read_section(text, "Why this run") == "the why"


def test_a_generated_assessment_is_not_read_as_a_persons_words() -> None:
    """`nrw assess` appends its own `## Assessment`; a listing that quoted it
    would show a chi-squared back to someone asking what they concluded."""
    text = FILLED_TEMPLATE + (
        "\n<!-- nrw:generated -->\n## Assessment\n\nchi-squared 2.94\n"
        "<!-- /nrw:generated -->\n"
    )

    assert notes.read_section(text, "Assessment") == ""


def test_a_wrapped_sentence_is_rejoined() -> None:
    text = "## Why this run\n\ntesting whether the oxide\nlayer is required\n"

    assert notes.read_section(text, "Why this run") == (
        "testing whether the oxide layer is required"
    )


def test_two_answers_to_one_question_stay_separate() -> None:
    """`nrw note --why` appends, so a fit asked twice has two answers. Running
    them into one sentence reports something nobody wrote."""
    text = notes.write_section(FILLED_TEMPLATE, "Why this run", "first thought")
    text = notes.write_section(text, "Why this run", "second thought")

    assert notes.read_section(text, "Why this run") == (
        "first thought\n\nsecond thought"
    )


def test_a_missing_heading_reads_as_empty() -> None:
    assert notes.read_section(FILLED_TEMPLATE, "Nonexistent") == ""


def test_comments_are_stripped_and_prose_kept() -> None:
    from nr_workbench.notes import strip_comments

    text = "<!-- e.g. ionomer on copper -->\nA Cu film.\n<!-- multi\nline -->end"

    assert strip_comments(text) == "\nA Cu film.\nend"


def test_inert_text_is_one_printable_line_that_cannot_touch_a_fence() -> None:
    """A model's verdict is filed inside a generated fence; a newline and a
    close marker would end it early, and an escape sequence would drive the
    terminal it is printed to."""
    from nr_workbench.notes import GENERATED_CLOSE, inert

    text = inert(f"pinned\n{GENERATED_CLOSE}\n## Why this run\x1b[2J ok")

    assert "\n" not in text
    assert "<!--" not in text and "-->" not in text
    assert all(ch.isprintable() for ch in text)
    assert text.startswith("pinned") and "ok" in text
    assert len(inert("x" * 5000)) == 600
