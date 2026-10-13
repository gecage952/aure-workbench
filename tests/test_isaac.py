"""Staging a fit for ISAAC export.

The schema mapping belongs to `nr-isaac-format` and is not retested here. What
is tested is the contract nr-workbench hands it, where two things have to be
right or the published record misstates the experiment:

* a state's angle segments are one measurement, not several; and
* co-refined states are conditions of one sample, not unrelated samples.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from nr_workbench.cli import main
from nr_workbench.isaac import stage

from .test_lifecycle import write_partials


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project with one sample carrying two 3-segment steady states."""
    root = tmp_path / "proj"
    assert (
        CliRunner().invoke(main, ["init", str(root), "--sample", "S1"]).exit_code == 0
    )
    steady = root / "samples" / "S1" / "data" / "steady"
    write_partials(steady, 100001)
    write_partials(steady, 100005)
    return root


def run(root: Path, monkeypatch, *args: str):
    """Invoke the CLI inside a project."""
    monkeypatch.chdir(root)
    return CliRunner().invoke(main, list(args))


@pytest.fixture
def corefined(project: Path, monkeypatch) -> tuple[Path, str]:
    """A fit co-refining two states, each of three angle segments."""
    pytest.importorskip("refl1d")
    run(project, monkeypatch, "model", "new", "S1", "--name", "m")
    assert (
        run(
            project, monkeypatch, "model", "generate", "samples/S1/models/m.yaml"
        ).exit_code
        == 0
    )
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


def fit_dir(root: Path, fit_id: str) -> Path:
    """The result directory of one fit."""
    return root / "samples" / "S1" / "results" / fit_id


# --------------------------------------------------------------------------
# Angle segments are one measurement
# --------------------------------------------------------------------------


def test_a_states_angle_segments_stay_one_measurement(
    corefined: tuple[Path, str], tmp_path: Path
) -> None:
    """A REF_L steady state is measured at three angles and reduced to three
    files with three run numbers. They are one measurement of one sample, and
    listing them as separate states would claim three that never happened.
    """
    root, fit_id = corefined

    staged = stage(fit_dir(root, fit_id), root, tmp_path / "ingest")

    assert [s.segments for s in staged.states] == [3, 3], "three angles each"
    assert [len(s.files) for s in staged.states] == [1, 1], "concatenated to one"

    run_info = json.loads((tmp_path / "ingest" / "run_info.json").read_text())
    assert len(run_info["states"]) == 2, "two states, not six"
    for state in run_info["states"]:
        assert len(state["data_files"]) == 1


def test_segments_are_grouped_by_the_spec_not_by_filename(
    corefined: tuple[Path, str], tmp_path: Path
) -> None:
    """The grouping is what the fit actually used: the frozen spec's states,
    whose measurement keys are the same strings the manifest recorded."""
    root, fit_id = corefined
    directory = fit_dir(root, fit_id)

    staged = stage(directory, root, tmp_path / "ingest")

    manifest = json.loads((directory / "manifest.json").read_text())
    recorded = {m["name"].split("#")[0] for m in manifest["info"]["models"]}
    assert {s.name for s in staged.states} == recorded


def test_the_combined_curve_holds_every_recorded_point(
    corefined: tuple[Path, str], tmp_path: Path
) -> None:
    """Concatenation must lose nothing. The exported curve is derived rather
    than recorded, so the check is that its length is the sum of its sources.
    """
    import numpy as np

    root, fit_id = corefined
    directory = fit_dir(root, fit_id)

    staged = stage(directory, root, tmp_path / "ingest")

    recorded = [
        (root / e["path"]).resolve()
        for e in json.loads((directory / "inputs.json").read_text())["inputs"]
        if e["role"] != "script"
    ]
    expected = sum(len(np.loadtxt(p, ndmin=2)) for p in recorded)
    written = sum(len(np.loadtxt(f, ndmin=2)) for s in staged.states for f in s.files)
    assert written == expected


def test_the_combined_curve_is_sorted_in_q(
    corefined: tuple[Path, str], tmp_path: Path
) -> None:
    """Three angle segments cover three Q ranges; appended in file order they
    would zigzag."""
    import numpy as np

    root, fit_id = corefined

    staged = stage(fit_dir(root, fit_id), root, tmp_path / "ingest")

    q = np.loadtxt(staged.states[0].files[0], ndmin=2)[:, 0]
    assert np.all(np.diff(q) >= 0)


def test_each_segment_is_divided_by_its_fitted_intensity(
    corefined: tuple[Path, str], tmp_path: Path
) -> None:
    """The direction is not a convention, it is measurable: on the real
    Cu/THF run this takes the segment-2/3 overlap from +30% to +1%, and
    multiplying instead takes it to +68%. Getting it backwards publishes a
    curve with a visible step in it.
    """
    import numpy as np

    from nr_workbench.isaac import concatenate

    source = tmp_path / "seg.txt"
    np.savetxt(source, np.array([[0.01, 1.0, 0.1, 0.001], [0.02, 0.5, 0.05, 0.002]]))

    written = concatenate([source], [0.5], tmp_path, run="100001")

    out = np.loadtxt(written, ndmin=2)
    assert out[0, 1] == pytest.approx(2.0), "R / intensity, not R * intensity"
    assert out[0, 2] == pytest.approx(0.2), "dR scales with R"
    assert out[0, 0] == pytest.approx(0.01), "Q is geometry and does not scale"
    assert out[0, 3] == pytest.approx(0.001), "nor does dQ"


def test_the_combined_file_keeps_the_instrument_header(
    corefined: tuple[Path, str], tmp_path: Path
) -> None:
    """The reader takes the run number, IPTS and reduction version from the
    header, not the filename -- drop it and the record says "Unknown"."""
    root, fit_id = corefined
    # The synthetic fixtures carry no header; give the primary segment the one
    # a real reduced file has, which is the thing that must survive.
    steady = root / "samples" / "S1" / "data" / "steady"
    primary = sorted(steady.glob("REFL_100001_*_partial.txt"))[0]
    primary.write_text(
        "# Experiment IPTS-34347 Run 100001\n"
        "# Reduction 2.2.0\n" + primary.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    staged = stage(fit_dir(root, fit_id), root, tmp_path / "ingest")

    combined = next(f for s in staged.states for f in s.files if "100001" in f)
    text = Path(combined).read_text(encoding="utf-8")
    assert "Experiment IPTS-34347 Run 100001" in text
    assert "Reduction 2.2.0" in text
    assert "Concatenated from the angle segments" in text, "and says it is derived"


def test_an_unscaled_merge_is_reported(
    corefined: tuple[Path, str], tmp_path: Path
) -> None:
    """Without per-segment intensities the segments are merged as they are,
    and any normalisation difference shows as a step. Silence would let that
    reach a published curve unremarked."""
    root, fit_id = corefined
    directory = fit_dir(root, fit_id)
    for path in (directory / "fit").glob("*.par"):
        path.write_text("Film thickness 100.0\n", encoding="utf-8")

    staged = stage(directory, root, tmp_path / "ingest")

    assert any("merged unscaled" in p for p in staged.problems)


# --------------------------------------------------------------------------
# Co-refined states are one sample
# --------------------------------------------------------------------------


def test_co_refined_states_share_one_sample(
    corefined: tuple[Path, str], tmp_path: Path
) -> None:
    """`distinct_sample: false` is what makes the records come out
    cross-linked as conditions of one experiment rather than unrelated
    measurements. nr-workbench has no way to express the opposite, so it is
    stated rather than left to a default that could change upstream.
    """
    root, fit_id = corefined

    stage(fit_dir(root, fit_id), root, tmp_path / "ingest")

    run_info = json.loads((tmp_path / "ingest" / "run_info.json").read_text())
    assert run_info["distinct_sample"] is False


def test_each_state_carries_its_condition(
    corefined: tuple[Path, str], tmp_path: Path
) -> None:
    """Without it every state gets the same environment and the record cannot
    say what distinguished the measurements."""
    import yaml

    root, fit_id = corefined
    spec_path = fit_dir(root, fit_id) / "spec.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    spec["states"][0]["condition"] = "OCV before the potential step"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

    staged = stage(fit_dir(root, fit_id), root, tmp_path / "ingest")

    by_name = {s.name: s for s in staged.states}
    described = by_name[spec["states"][0]["name"]]
    assert described.condition == "OCV before the potential step"

    run_info = json.loads((tmp_path / "ingest" / "run_info.json").read_text())
    entries = {e["name"]: e for e in run_info["states"]}
    assert entries[described.name]["extra_description"] == described.condition
    # A state with no condition omits the key rather than sending an empty
    # string, which the assembler would take for a description.
    for state in staged.states:
        if not state.condition:
            assert "extra_description" not in entries[state.name]


# --------------------------------------------------------------------------
# The rest of the contract
# --------------------------------------------------------------------------


def test_the_serialised_problem_and_its_uncertainties_are_staged(
    corefined: tuple[Path, str], tmp_path: Path
) -> None:
    """The assembler finds sigma in an `-err.json` beside `problem.json`."""
    root, fit_id = corefined
    directory = fit_dir(root, fit_id)
    (directory / "fit" / "m-err.json").write_text('{"a": {"std": 1.0}}', "utf-8")

    stage(directory, root, tmp_path / "ingest")

    problem = json.loads((tmp_path / "ingest" / "problem.json").read_text())
    assert "references" in problem, "the bumps problem, not some other json"
    assert (tmp_path / "ingest" / "problem-err.json").is_file()


def test_a_missing_uncertainty_file_is_reported_not_hidden(
    corefined: tuple[Path, str], tmp_path: Path
) -> None:
    """An optimiser run publishes values with no error bars, and the record
    should not be the first place anyone notices."""
    root, fit_id = corefined

    staged = stage(fit_dir(root, fit_id), root, tmp_path / "ingest")

    assert any("no uncertainties" in p for p in staged.problems)


def test_chi_squared_reaches_the_assembler(
    corefined: tuple[Path, str], tmp_path: Path
) -> None:
    root, fit_id = corefined

    staged = stage(fit_dir(root, fit_id), root, tmp_path / "ingest")

    final = json.loads((tmp_path / "ingest" / "final_state.json").read_text())
    assert final["final_chi2"] == pytest.approx(staged.chisq)


def test_a_fit_with_no_serialised_problem_is_refused(
    corefined: tuple[Path, str], tmp_path: Path
) -> None:
    """An ISAAC record carries the fitted model. Exporting without one would
    publish a measurement claiming to be an analysis."""
    root, fit_id = corefined
    directory = fit_dir(root, fit_id)
    for path in (directory / "fit").glob("*.json"):
        path.unlink()

    with pytest.raises(FileNotFoundError, match="fitted model"):
        stage(directory, root, tmp_path / "ingest")


def test_a_script_with_no_spec_exports_as_one_state(
    corefined: tuple[Path, str], tmp_path: Path
) -> None:
    """A forked or hand-written script has no states to group by. One state is
    the honest answer -- segments still assemble into one measurement -- and
    the loss of per-condition records is said out loud rather than guessed at
    from filenames."""
    root, fit_id = corefined
    directory = fit_dir(root, fit_id)
    (directory / "spec.yaml").unlink()

    staged = stage(directory, root, tmp_path / "ingest")

    assert len(staged.states) == 1
    assert staged.n_files == 6, "every segment still travels, unconcatenated"
    assert any("one state" in p for p in staged.problems)


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------


def without_tools(monkeypatch, tmp_path: Path) -> None:
    """Make the schema tools unfindable, whether or not they are installed.

    Clearing PATH is not enough: `_find` also looks beside the running
    interpreter, and this repo's own venv gains both tools the moment anyone
    installs the extra -- so a PATH-only version of this test passes or fails
    depending on who ran pip last.
    """
    import shutil as shutil_module

    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(shutil_module, "which", lambda *a, **k: None)
    monkeypatch.setattr("sys.executable", str(tmp_path / "nowhere" / "python"))


def test_export_says_what_is_missing_rather_than_failing_obscurely(
    corefined: tuple[Path, str], monkeypatch, tmp_path: Path
) -> None:
    """The schema tools are an optional extra, so their absence is the normal
    case for someone who has not opted in."""
    root, fit_id = corefined
    without_tools(monkeypatch, tmp_path)

    result = run(
        root, monkeypatch, "isaac", "export", fit_id, "--out", str(tmp_path / "o")
    )

    assert result.exit_code != 0
    assert "data-assembler is not installed" in result.output
    # Not `pip install 'nr-workbench[isaac]'`: nothing on PyPI answers it.
    assert "NRW_EXTRAS=isaac" in result.output
    assert "pip install 'nr-workbench" not in result.output


def test_export_reports_the_assembly_before_it_needs_any_tool(
    corefined: tuple[Path, str], monkeypatch, tmp_path: Path
) -> None:
    """Staging is nr-workbench's half of the job and is worth seeing even when
    the rest cannot run."""
    root, fit_id = corefined
    without_tools(monkeypatch, tmp_path)

    result = run(
        root, monkeypatch, "isaac", "export", fit_id, "--out", str(tmp_path / "o")
    )

    assert "3 angle segments concatenated" in result.output


def test_upload_is_never_implied_by_export(
    corefined: tuple[Path, str], monkeypatch, tmp_path: Path
) -> None:
    """Publishing to a shared portal is not straightforwardly retractable."""
    root, fit_id = corefined
    calls: list[list[str]] = []

    import nr_workbench.commands.isaac_cmd as module

    monkeypatch.setattr(module, "_assemble", lambda ingest: None)
    monkeypatch.setattr(
        module, "_convert", lambda i, r, n: r.mkdir(parents=True, exist_ok=True)
    )
    monkeypatch.setattr(module, "_validate", lambda records: None)
    monkeypatch.setattr(module, "_upload", lambda *a, **k: calls.append(["upload"]))

    result = run(
        root, monkeypatch, "isaac", "export", fit_id, "--out", str(tmp_path / "o")
    )

    assert result.exit_code == 0, result.output
    assert calls == [], "no upload without the flag"
    assert "Not uploaded" in result.output


def test_the_fits_note_becomes_the_record_context(
    corefined: tuple[Path, str], monkeypatch, tmp_path: Path
) -> None:
    """The record is read by people who will never see this project, so the
    reasoning is worth more there than anywhere else it is kept."""
    root, fit_id = corefined
    run(root, monkeypatch, "note", fit_id.rpartition("-")[2], "-m", "oxide required")

    from nr_workbench.commands.isaac_cmd import _notes_for
    from nr_workbench.project.layout import ProjectLayout
    from nr_workbench.provenance.index import FitIndex

    layout = ProjectLayout(root=root)
    entry = FitIndex(layout.index_file).find(fit_id)

    notes = _notes_for(layout, fit_dir(root, fit_id), entry)

    assert notes is not None
    assert "oxide required" in notes


# --------------------------------------------------------------------------
# Re-exporting
# --------------------------------------------------------------------------


def test_re_staging_replaces_rather_than_accumulates(
    corefined: tuple[Path, str], tmp_path: Path
) -> None:
    """The assembler names its outputs by uuid, so a second run into the same
    directory writes a whole new set beside the first -- and the converter
    then sees twice the states and emits twice the records.

    Found in the field, not here: every other test in this file stages into a
    fresh tmp_path and so could never see it.
    """
    root, fit_id = corefined
    ingest = tmp_path / "ingest"

    stage(fit_dir(root, fit_id), root, ingest)
    # Stand in for what data-assembler leaves behind: uuid-named output that
    # a second run would not overwrite.
    leftover = ingest / "reflectivity" / "facility=SNS"
    leftover.mkdir(parents=True)
    (leftover / "0a1b2c3d.parquet").write_bytes(b"stale")

    stage(fit_dir(root, fit_id), root, ingest)

    assert not (leftover / "0a1b2c3d.parquet").exists(), (
        "a second export must start from an empty directory"
    )
    run_info = json.loads((ingest / "run_info.json").read_text())
    assert len(run_info["states"]) == 2, "still two states, not four"


def test_staging_refuses_a_directory_it_did_not_write(
    corefined: tuple[Path, str], tmp_path: Path
) -> None:
    """Emptying a directory is only safe when we put everything in it."""
    root, fit_id = corefined
    theirs = tmp_path / "mine"
    theirs.mkdir()
    (theirs / "thesis.tex").write_text("do not delete me", encoding="utf-8")

    with pytest.raises(FileExistsError, match="carries no marker"):
        stage(fit_dir(root, fit_id), root, theirs)

    assert (theirs / "thesis.tex").read_text() == "do not delete me"


def test_force_replaces_a_directory_nrw_did_not_write(
    corefined: tuple[Path, str], tmp_path: Path
) -> None:
    """The escape hatch, needed once by anyone who exported before the
    ownership marker existed -- their directory has no sentinel and is
    otherwise unrecoverable without a manual delete."""
    root, fit_id = corefined
    stale = tmp_path / "stale"
    stale.mkdir()
    (stale / "leftover.parquet").write_bytes(b"from a previous run")

    staged = stage(fit_dir(root, fit_id), root, stale, force=True)

    assert not (stale / "leftover.parquet").exists()
    assert staged.n_files == 2


def test_an_empty_directory_is_fine_to_stage_into(
    corefined: tuple[Path, str], tmp_path: Path
) -> None:
    """Refusing here would make `--out ./somewhere-i-just-made` fail."""
    root, fit_id = corefined
    empty = tmp_path / "empty"
    empty.mkdir()

    staged = stage(fit_dir(root, fit_id), root, empty)

    assert staged.n_files == 2


def test_exporting_twice_leaves_one_record_per_state(
    corefined: tuple[Path, str], monkeypatch, tmp_path: Path
) -> None:
    """The symptom as reported: six records for three measurements."""
    root, fit_id = corefined
    out = tmp_path / "out"
    import nr_workbench.commands.isaac_cmd as module

    def fake_convert(ingest: Path, records: Path, notes: str | None) -> None:
        records.mkdir(parents=True, exist_ok=True)
        states = json.loads((ingest / "run_info.json").read_text())["states"]
        for state in states:
            (records / f"isaac_record_{state['name']}.json").write_text("{}", "utf-8")

    monkeypatch.setattr(module, "_assemble", lambda ingest: None)
    monkeypatch.setattr(module, "_convert", fake_convert)
    monkeypatch.setattr(module, "_validate", lambda records: None)

    for _ in range(2):
        result = run(root, monkeypatch, "isaac", "export", fit_id, "--out", str(out))
        assert result.exit_code == 0, result.output

    assert len(list((out / "records").glob("*.json"))) == 2


# --------------------------------------------------------------------------
# Experimental conditions
# --------------------------------------------------------------------------

SAMPLE_MD = """# expt11

## Measurements

| Run    | Type   | Condition   |
|--------|--------|-------------|
| 218386 | full Q | OCV         |
| 218393 | full Q | -0.5 mA/cm2 |
"""


def test_the_condition_comes_out_of_the_measurement_table() -> None:
    """A fit knows nothing about applied potential. The scientist wrote it in
    sample.md before any of this ran, and without carrying it over the record
    says only "ex_situ"."""
    from nr_workbench.conditions import from_table

    assert from_table(SAMPLE_MD, "218386") == "OCV"
    assert from_table(SAMPLE_MD, "218393") == "-0.5 mA/cm2"


def test_an_unlisted_run_gets_nothing_rather_than_a_neighbour() -> None:
    """Returning the wrong row would label a measurement with someone else's
    conditions, which is worse than labelling it with none."""
    from nr_workbench.conditions import from_table

    assert from_table(SAMPLE_MD, "999999") is None
    assert from_table("no table here", "218386") is None


def test_the_table_and_the_spec_are_combined_not_chosen_between() -> None:
    """The table says what was applied; the spec says what it was for."""
    from nr_workbench.conditions import describe

    found = describe(
        states=[("s1", "218393", "sample realigned")],
        sample_markdown=SAMPLE_MD,
        use_llm=False,
    )

    assert found["s1"].source == "table"
    assert "-0.5 mA/cm2" in found["s1"].text
    assert "sample realigned" in found["s1"].text


def test_conditions_fall_back_to_the_spec_when_the_table_is_silent() -> None:
    from nr_workbench.conditions import describe

    found = describe(
        states=[("s1", "999999", "under potential")],
        sample_markdown=SAMPLE_MD,
        use_llm=False,
    )

    assert found["s1"].source == "spec"
    assert found["s1"].text == "under potential"


def test_nothing_is_invented_when_nothing_is_known() -> None:
    """A record that says less is better than one that says something untrue."""
    from nr_workbench.conditions import describe

    found = describe(states=[("s1", "999999", None)], sample_markdown="", use_llm=False)

    assert found["s1"].text == ""
    assert found["s1"].source == "none"


def test_a_language_model_reply_that_names_an_unknown_state_is_ignored(
    monkeypatch,
) -> None:
    """The reply is parsed, not trusted: a hallucinated state name must not
    become a condition on a measurement that exists."""
    import nr_workbench.conditions as module

    monkeypatch.setattr(
        "nr_workbench.aure_adapter.llm_available", lambda: True, raising=False
    )
    monkeypatch.setattr(
        "nr_workbench.aure_adapter.complete",
        lambda s, u: '{"s1": "OCV in dTHF", "ghost": "invented"}',
        raising=False,
    )

    found = module.describe(
        states=[("s1", "218386", None)], sample_markdown=SAMPLE_MD, use_llm=True
    )

    assert set(found) == {"s1"}
    assert found["s1"].source == "llm"


def test_an_unusable_language_model_reply_leaves_the_table_answer(
    monkeypatch,
) -> None:
    """Falling back to a worse answer beats failing the export."""
    import nr_workbench.conditions as module

    monkeypatch.setattr(
        "nr_workbench.aure_adapter.llm_available", lambda: True, raising=False
    )
    monkeypatch.setattr(
        "nr_workbench.aure_adapter.complete", lambda s, u: "not json", raising=False
    )

    found = module.describe(
        states=[("s1", "218386", None)], sample_markdown=SAMPLE_MD, use_llm=True
    )

    assert found["s1"].text == "OCV"
    assert found["s1"].source == "table"


def test_analysis_prose_never_reaches_the_condition_field(
    corefined: tuple[Path, str], monkeypatch, tmp_path: Path
) -> None:
    """The record's electrochemistry falls back to parsing the measurement
    description when no structured condition was recognised. A shared note
    reading "co-refinement (OCV/potential/OCV)" therefore relabelled the
    galvanostatic state as open circuit -- in all three records.
    """
    root, fit_id = corefined
    run(root, monkeypatch, "note", fit_id.rpartition("-")[2], "-m", "OCV/potential/OCV")

    captured: dict[str, object] = {}
    import nr_workbench.commands.isaac_cmd as module

    monkeypatch.setattr(module, "_assemble", lambda ingest: None)
    monkeypatch.setattr(
        module,
        "_convert",
        lambda i, r, n: (
            captured.update(notes=n),
            r.mkdir(parents=True, exist_ok=True),
        ),
    )
    monkeypatch.setattr(module, "_validate", lambda records: None)

    result = run(
        root,
        monkeypatch,
        "isaac",
        "export",
        fit_id,
        "--out",
        str(tmp_path / "o"),
        "--no-llm",
    )

    assert result.exit_code == 0, result.output
    assert captured["notes"] is None, (
        "with several states the per-state condition owns that field"
    )
    ingest = json.loads((tmp_path / "o" / "ingest" / "run_info.json").read_text())
    for state in ingest["states"]:
        assert "OCV/potential/OCV" not in (state.get("extra_description") or "")


def test_a_state_with_no_condition_is_named(
    corefined: tuple[Path, str], tmp_path: Path
) -> None:
    """The per-state warning sat inside the branch for a state that HAD a
    condition, so it could never fire, and one silent state among described
    ones went unreported."""
    import yaml

    root, fit_id = corefined
    spec_path = fit_dir(root, fit_id) / "spec.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    spec["states"][0]["condition"] = "OCV before the potential step"
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
    silent = spec["states"][1]["name"]

    staged = stage(fit_dir(root, fit_id), root, tmp_path / "ingest", use_llm=False)

    named = [p for p in staged.problems if "No condition found for" in p]
    assert len(named) == 1 and silent in named[0]
    assert not any("No experimental conditions" in p for p in staged.problems)


def test_no_conditions_at_all_is_said_once(
    corefined: tuple[Path, str], tmp_path: Path
) -> None:
    root, fit_id = corefined

    staged = stage(fit_dir(root, fit_id), root, tmp_path / "ingest", use_llm=False)

    assert any("No experimental conditions" in p for p in staged.problems)
    assert not any("No condition found for" in p for p in staged.problems)
