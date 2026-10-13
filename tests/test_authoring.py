"""Filling in the physics half of a spec from the scientist's notes.

The safety property under test is a division of labour: a language model may
propose what the sample *is*, never what was *measured*. Angles, runs, file
paths and series come from the files on disk and are already exact, so a
plausible wrong answer there would be unrecoverable -- the fit would converge
and be wrong. The filter is structural, not a request in the prompt.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from nr_workbench.spec.authoring import (
    CORE_SKILLS,
    PROPOSABLE,
    STACK_ORDER,
    AuthoringError,
    Proposal,
    agent_instructions,
    build_prompt,
    describe_measurements,
    find_skills,
    is_placeholder,
    merge_proposal,
    missing_relevant,
    parse_proposal,
    relevant_skills,
)

SKELETON = {
    "schema": "nrw-model/1",
    "name": "m",
    "sample": "S1",
    "stack": [{"name": "Film", "material": "Film", "thickness": 100, "roughness": 5}],
    "states": [
        {
            "name": "ocv1",
            "run": 218386,
            "thetas": [0.45, 1.201, 3.5003],
            "data_dir": "samples/S1/data/steady",
        }
    ],
    "series": [{"name": "tnr", "run": 218389, "theta": 0.5997}],
    "constraints": [
        {
            "series": "tnr",
            "form": "linear_in_time",
            "from": "ocv1",
            "to": "ocv2",
            "paths": ["Film.thickness"],
        }
    ],
}

PROPOSED = {
    "description": "Cu on Ti on Si in d8-THF.",
    "materials": {"THF": {"rho": 6.35}, "Cu": {"rho": 6.55}, "Si": {"rho": 2.07}},
    "stack": [
        {"name": "THF", "material": "THF", "thickness": 0, "roughness": 20},
        {"name": "Cu", "material": "Cu", "thickness": 500, "roughness": 10},
        {"name": "Si", "material": "Si"},
    ],
    "parameters": [{"path": "Cu.thickness", "range": [400, 600], "per": "model"}],
    "notes": "The oxide thickness is a guess.",
}


# --------------------------------------------------------------------------
# The safety boundary
# --------------------------------------------------------------------------


def test_measured_facts_survive_a_proposal_that_tries_to_change_them() -> None:
    """Angles come from file headers and must not be overwritten.

    theta feeds `dT = dq/q * tan(theta)`, so a plausible wrong angle broadens
    every fringe and the fit absorbs it into roughness. This is the property
    the whole split exists to guarantee.
    """
    hostile = dict(PROPOSED)
    hostile["states"] = [{"name": "WRONG", "thetas": [9.9]}]
    hostile["series"] = [{"name": "WRONG", "theta": 9.9}]
    hostile["sample"] = "OTHER"

    proposal = parse_proposal(json.dumps(hostile))
    merged = merge_proposal(SKELETON, proposal)

    assert merged["states"] == SKELETON["states"]
    assert merged["series"] == SKELETON["series"]
    assert merged["sample"] == "S1"
    assert sorted(proposal.rejected) == ["sample", "series", "states"]


def test_only_the_physics_keys_are_accepted() -> None:
    """The allowed set is explicit, so a new key upstream cannot slip through."""
    proposal = parse_proposal(json.dumps(PROPOSED))

    assert set(proposal.document) <= set(PROPOSABLE)
    assert "description" in proposal.document
    assert proposal.notes == "The oxide thickness is a guess."


def test_a_proposal_replacing_the_stack_repairs_the_leftover_constraint() -> None:
    """The scaffold constrains `Film.thickness`; a new stack has no Film.

    Leaving it produces a spec that fails its own `nrw model validate` -- an
    authored spec that does not validate is worse than the placeholder it
    replaced.
    """
    merged = merge_proposal(SKELETON, parse_proposal(json.dumps(PROPOSED)))

    assert "constraints" not in merged, "a constraint with no valid path is dropped"


def test_a_per_model_path_is_removed_from_the_constraint() -> None:
    """`per: model` says a quantity does not change; a constraint says it does.

    The two contradict and the spec will not validate. The declaration wins --
    it is the more specific statement of intent, and it is usually what the
    notes say in so many words ("use the steady states to pin down the copper
    SLD").
    """
    skeleton = dict(SKELETON)
    skeleton["constraints"] = [
        {
            "series": "tnr",
            "form": "linear_in_time",
            "from": "a",
            "to": "b",
            "paths": ["Cu.rho", "Cu.thickness"],
        }
    ]
    proposal = dict(PROPOSED)
    proposal["parameters"] = [
        {"path": "Cu.rho", "range": [5, 7], "per": "model"},
        {
            "path": "Cu.thickness",
            "range": [400, 600],
            "per": "state",
            "in": ["ocv1", "ocv2"],
        },
    ]

    parsed = parse_proposal(json.dumps(proposal))
    merged = merge_proposal(skeleton, parsed)

    assert merged["constraints"][0]["paths"] == ["Cu.thickness"]
    assert "Cu.rho" in parsed.dropped_paths, "and it is reported, not silent"


def test_a_constrained_parameter_is_scoped_away_from_the_series() -> None:
    """`per: state` with no `in:` covers the series and collides.

    The prompt asks for the `in:` and it is omitted about half the time, which
    is the signal that it belongs in code: producing a spec that validates is a
    correctness property, not a request.
    """
    skeleton = dict(SKELETON)
    skeleton["states"] = [{"name": "ocv1"}, {"name": "ocv2"}]
    skeleton["constraints"] = [
        {
            "series": "tnr",
            "form": "linear_in_time",
            "from": "ocv1",
            "to": "ocv2",
            "paths": ["Cu.thickness"],
        }
    ]
    proposal = dict(PROPOSED)
    proposal["parameters"] = [
        {"path": "Cu.thickness", "range": [400, 600], "per": "state"}
    ]

    merged = merge_proposal(skeleton, parse_proposal(json.dumps(proposal)))

    assert merged["parameters"][0]["in"] == ["ocv1", "ocv2"]
    # and a deliberate scoping is never overridden
    proposal["parameters"][0]["in"] = ["ocv1"]
    again = merge_proposal(skeleton, parse_proposal(json.dumps(proposal)))
    assert again["parameters"][0]["in"] == ["ocv1"]


def test_a_constraint_keeps_the_paths_that_still_exist() -> None:
    """Only the dead paths go, not the whole constraint."""
    skeleton = dict(SKELETON)
    skeleton["constraints"] = [
        {
            "series": "tnr",
            "form": "linear_in_time",
            "from": "a",
            "to": "b",
            "paths": ["Film.thickness", "Cu.thickness", "probe.intensity"],
        }
    ]
    proposal = dict(PROPOSED)
    proposal["parameters"] = [
        {
            "path": "Cu.thickness",
            "range": [400, 600],
            "per": "state",
            "in": ["ocv1", "ocv2"],
        }
    ]

    merged = merge_proposal(skeleton, parse_proposal(json.dumps(proposal)))

    assert merged["constraints"][0]["paths"] == ["Cu.thickness", "probe.intensity"]


# --------------------------------------------------------------------------
# Parsing what a model actually returns
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "wrapper",
    [
        "{body}",
        "```json\n{body}\n```",
        "```\n{body}\n```",
        "Here is the spec you asked for:\n\n{body}\n\nLet me know if...",
    ],
)
def test_json_is_found_however_it_is_wrapped(wrapper: str) -> None:
    """Models fence, prefix and chatter. None of that should lose the answer."""
    reply = wrapper.format(body=json.dumps(PROPOSED))

    proposal = parse_proposal(reply)

    assert proposal.document["materials"]["Cu"]["rho"] == 6.55


def test_a_reply_with_no_json_is_an_error_not_an_empty_spec() -> None:
    """Silently writing the placeholder would hide that the call failed."""
    with pytest.raises(AuthoringError, match="no JSON object"):
        parse_proposal("I am unable to help with that request.")


def test_an_empty_proposal_leaves_the_skeleton_alone() -> None:
    merged = merge_proposal(SKELETON, Proposal())

    assert merged == SKELETON


# --------------------------------------------------------------------------
# Skill selection
# --------------------------------------------------------------------------


def test_core_skills_are_always_sent(project: Path) -> None:
    """The schema, the physics and the beamline conventions are never optional."""
    skills = find_skills(project)

    chosen = relevant_skills("", skills)

    assert [name for name in CORE_SKILLS if name in skills] == chosen


def test_notes_select_extra_skills(project: Path) -> None:
    """A sample in THF should bring the solvent skill, if it is installed."""
    skills = dict.fromkeys(
        [*CORE_SKILLS, "solvent-contrast-matching", "polymer-films"],
        Path("x"),
    )

    chosen = relevant_skills("Copper electrode in d8-THF", skills)

    assert "solvent-contrast-matching" in chosen
    assert "polymer-films" not in chosen, "nothing in the notes calls for it"


METAL, POLYMER, SOLVENT, THIN = (
    "metal-oxide-interfaces",
    "polymer-films",
    "solvent-contrast-matching",
    "thin-layer-degeneracy",
)


@pytest.mark.parametrize(
    ("notes", "chosen", "not_chosen"),
    [
        # Substrings once: "cu" in "curve", "thin" in "nothing".
        ("A curve of R against Q, nothing else.", [], [METAL, THIN]),
        # Every sample is a film; "film" chose polymer physics for copper.
        ("A Cu film in d8-THF.", [METAL, SOLVENT], [POLYMER]),
        ("50 nm Ti in dTHF.", [METAL, SOLVENT], []),
        # A stem alone: nothing else in these notes is a trigger.
        ("A deuterated film.", [SOLVENT], [POLYMER]),
        # A stem and a plural.
        ("Swelling of the brushes.", [POLYMER], []),
        ("Native oxides.", [METAL, THIN], []),
        # A plural on an element symbol: "cu"+"es", "ti"+"es".
        ("Visual cues only.", [], [METAL]),
        ("The data ties them.", [], [METAL]),
        # Formulas, which a whole-word match on "cu" missed.
        ("Cu2O at the interface.", [METAL], []),
        ("A CuO layer.", [METAL], []),
    ],
)
def test_skills_are_chosen_by_word_not_by_substring(
    notes: str, chosen: list[str], not_chosen: list[str]
) -> None:
    available = dict.fromkeys(
        [
            *CORE_SKILLS,
            "metal-oxide-interfaces",
            "polymer-films",
            "solvent-contrast-matching",
            "thin-layer-degeneracy",
        ],
        Path("x"),
    )

    picked = relevant_skills(notes, available)

    for name in chosen:
        assert name in picked, name
    for name in not_chosen:
        assert name not in picked, name


def test_relevant_but_uninstalled_skills_are_reported() -> None:
    """The most relevant skills are exactly the ones init does not seed.

    Silently proceeding without them wastes the context that makes the answer
    good, so they are named and `nrw skills sync` is suggested.
    """
    installed = dict.fromkeys(CORE_SKILLS, Path("x"))

    absent = missing_relevant("Copper oxide electrode in d8-THF", installed)

    assert "metal-oxide-interfaces" in absent
    assert "solvent-contrast-matching" in absent


# --------------------------------------------------------------------------
# The prompt, and its printed twin
# --------------------------------------------------------------------------


def test_the_prompt_carries_the_skills_and_forbids_the_facts(project: Path) -> None:
    skills = find_skills(project)

    system, user = build_prompt(skeleton=SKELETON, notes="Cu in THF", skills=skills)

    assert "nrw-model/1" in system
    assert "Do NOT return" in system
    for forbidden in ("states", "thetas", "data_dir"):
        assert forbidden in system
    assert "ocv1" in user, "the model needs the group names to write `in:` lists"
    assert "0.45" in user


def test_the_prompt_includes_measured_facts_when_given(project: Path) -> None:
    _, user = build_prompt(
        skeleton=SKELETON,
        notes="",
        skills=find_skills(project),
        facts="ocv1: critical edge Qc=0.01467, an SLD step of 4.28",
    )

    assert "critical edge" in user


def test_agent_instructions_name_the_files_and_the_boundary() -> None:
    """The assistant can read the repo, so point at files rather than inline them."""
    text = agent_instructions(
        spec_path="samples/S1/models/m.yaml",
        notes_path="samples/S1/sample.md",
        skills=["nrw-model-spec", "refl-bl4b-instrument"],
        sample="S1",
    )

    assert "samples/S1/models/m.yaml" in text
    assert "samples/S1/sample.md" in text
    assert "skills/reflectometry/nrw-model-spec/SKILL.md" in text
    assert "Leave `states`, `series`, `thetas`" in text
    assert "nrw model validate" in text
    # the angles: none added, and a blank left for the person who measured it
    assert "add no angles" in text
    assert "never fill it in" in text
    # the ambient: from each measurement's condition, and never assumed
    assert "never assume air" in " ".join(text.split())


def test_both_requests_give_the_same_stack_order(project: Path) -> None:
    """The endpoint was told "AMBIENT FIRST, SUBSTRATE LAST" for every sample
    -- back reflection -- while the assistant was told the last entry is the
    medium the beam enters through. One rule now, said word for word in both.
    """
    system, _ = build_prompt(skeleton=SKELETON, notes="", skills=find_skills(project))
    instructions = agent_instructions(
        spec_path="m.yaml", notes_path="sample.md", skills=[], sample="S1"
    )

    for text in (system, instructions):
        flat = " ".join(text.split())
        assert STACK_ORDER in flat
        # In any case: the spec skill, which goes into the same prompt, said
        # "Declare the stack, ambient first".
        assert "ambient first" not in flat.lower()


def test_the_scaffold_states_the_rule_where_it_writes_the_stack(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every scaffolded spec carried "# ambient -> substrate" above its stack."""
    from click.testing import CliRunner

    from nr_workbench.cli import main

    root = sample_with_data(project)
    monkeypatch.chdir(root)

    result = CliRunner().invoke(main, ["model", "new", "Sample1", "--name", "m"])

    assert result.exit_code == 0, result.output
    text = (root / "samples" / "Sample1" / "models" / "m.yaml").read_text()
    assert "ambient -> substrate" not in text
    assert "enters through the LAST entry" in text


def test_the_templates_own_examples_choose_no_skill() -> None:
    """The scaffolded sample.md's guidance holds examples of other samples --
    "~30 nm ionomer ... on 20 nm copper ... in D2O" -- and read as the
    scientist's words they chose every contextual skill for every sample."""
    blank = (
        Path(__file__).parent / "data" / "experiment" / "blank_sample.md"
    ).read_text(encoding="utf-8")
    installed = dict.fromkeys(CORE_SKILLS, Path("x"))
    # Every contextual skill on offer, or choosing none proves nothing.
    every = dict.fromkeys([*CORE_SKILLS, METAL, POLYMER, SOLVENT, THIN], Path("x"))

    assert missing_relevant(blank, installed) == []
    assert relevant_skills(blank, every) == list(CORE_SKILLS)


def test_the_endpoint_is_sent_the_notes_without_the_templates_guidance(
    project: Path,
) -> None:
    notes = "<!-- e.g. ~30 nm ionomer on copper -->\nA Cu film in d8-THF.\n"

    _, user = build_prompt(skeleton=SKELETON, notes=notes, skills=find_skills(project))

    assert "A Cu film in d8-THF." in user
    assert "ionomer" not in user


def test_the_request_asks_for_json_in_the_user_half_too(project: Path) -> None:
    """The claude_code provider decides from the user half alone whether a
    reply should be JSON and may be retried (docs/ground_truths.md, 2026-10-09)."""
    _, user = build_prompt(skeleton=SKELETON, notes="", skills=find_skills(project))

    assert "json" in user.lower()


def test_the_assistant_may_write_the_constraints_it_is_told_to_add() -> None:
    """Step 5 asks for a `constraints:` block; step 3 once forbade writing it."""
    text = agent_instructions(
        spec_path="m.yaml", notes_path="sample.md", skills=[], sample="S1"
    )

    editable = text.split("Edit ONLY these parts of the spec:")[1].split("\n\n")[0]
    assert "constraints" in editable
    assert "constraints:` block" in text


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------


def sample_with_data(project: Path) -> Path:
    """Give the scaffolded project a sample with two segments and notes."""
    import numpy as np

    steady = project / "samples" / "Sample1" / "data" / "steady"
    steady.mkdir(parents=True, exist_ok=True)
    q = np.linspace(0.01, 0.2, 40)
    r = 1e-3 * (0.01 / q) ** 4
    body = "\n".join(
        f"{a:.6e} {b:.6e} {c:.6e} {d:.6e}"
        for a, b, c, d in zip(q, r, 0.05 * r, 0.02 * q, strict=True)
    )
    for segment, theta in ((1, 0.007853), (2, 0.020960)):
        (steady / f"REFL_100001_{segment}_10000{segment}_partial.txt").write_text(
            f'# Meta:{{"theta": {theta}, "run_number": 100001, '
            f'"sequence_number": {segment}, "norm_run": 99999}}\n' + body,
            encoding="utf-8",
        )
    (project / "samples" / "Sample1" / "sample.md").write_text(
        "# Sample1\n\nCopper electrode in d8-THF with a native oxide.\n",
        encoding="utf-8",
    )
    return project


#: What was written about each measurement: a contrast per run in the table,
#: a note on one run, and what every run shares.
MEASURED_NOTES = """# S1

## Description

A lipid bilayer on silicon.

## Measurements

| Run | Type | Condition |
|---|---|---|
| 218386 | steady | in D2O |
| 218387 | steady | in H2O |

## Measurement conditions

Measured at 25 C in a solid-liquid cell.

- Run 218387: the cell was flushed with H2O first
"""

TWO_CONTRASTS = {
    **SKELETON,
    "states": [
        {"name": "d2o", "run": 218386},
        {"name": "h2o", "run": 218387},
    ],
}


def test_each_measurement_is_set_out_with_its_condition_and_notes() -> None:
    described = describe_measurements(TWO_CONTRASTS, MEASURED_NOTES)

    assert described.splitlines() == [
        "- state d2o (run 218386): condition: in D2O",
        "- state h2o (run 218387): condition: in H2O; notes: the cell was "
        "flushed with H2O first",
        "- series tnr (run 218389): nothing written about it",
        "- every state: Measured at 25 C in a solid-liquid cell.",
    ]


def test_a_note_over_several_lines_is_set_out_as_one_line_of_its_own_state() -> None:
    # Written over two lines, a note must not make a line that reads as another
    # state's -- the request takes the ambient from these lines.
    notes = MEASURED_NOTES.replace(
        "- Run 218387: the cell was flushed with H2O first",
        "- Run 218387: flushed first\n  - state d2o (run 218386): condition: in air",
    )

    described = describe_measurements(TWO_CONTRASTS, notes).splitlines()

    assert described[0] == "- state d2o (run 218386): condition: in D2O"
    assert described[1].startswith("- state h2o (run 218387): condition: in H2O;")
    assert "in air" in described[1] and len(described) == 4


def test_a_proposal_that_leaves_the_stack_alone_leaves_a_placeholder() -> None:
    import yaml

    from nr_workbench.spec.authoring import placeholder_materials, placeholder_stack

    skeleton = {
        **SKELETON,
        "materials": placeholder_materials(),
        "stack": placeholder_stack(),
    }
    merged = merge_proposal(skeleton, Proposal(document={"description": "D2O."}))
    proposed = merge_proposal(skeleton, parse_proposal(json.dumps(PROPOSED)))

    assert is_placeholder(yaml.safe_dump(merged))
    assert not is_placeholder(yaml.safe_dump(proposed))


def test_nothing_is_set_out_when_nothing_was_written_about_a_measurement() -> None:
    notes = "# S1\n\n## Description\n\nA lipid bilayer on silicon.\n"

    assert describe_measurements(TWO_CONTRASTS, notes) == ""


def test_the_request_carries_each_measurement_and_never_assumes_air() -> None:
    measurements = describe_measurements(TWO_CONTRASTS, MEASURED_NOTES)

    system, user = build_prompt(
        skeleton=TWO_CONTRASTS,
        notes=MEASURED_NOTES,
        skills={},
        measurements=measurements,
    )

    assert "===== EACH MEASUREMENT, AS THE NOTES DESCRIBE IT =====" in user
    assert "- state h2o (run 218387): condition: in H2O" in user
    assert "never assume air" in system
    # A contrast that differs is the ambient's rho, one entry per state.
    assert "per: state, in: [<that state>]" in system


def test_from_notes_asks_with_each_runs_condition(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page's New model runs this: a condition written for a run must reach
    the language model as that state's, or every state is fitted in air."""
    from click.testing import CliRunner

    from nr_workbench import aure_adapter
    from nr_workbench.cli import main

    root = sample_with_data(project)
    (root / "samples" / "Sample1" / "sample.md").write_text(
        "# Sample1\n\n## Description\n\nCopper electrode with a native oxide.\n"
        "\n## Measurements\n\n| Run | Type | Condition |\n|---|---|---|\n"
        "| 100001 | steady | in d8-THF |\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        aure_adapter,
        "llm_info",
        lambda: {"available": True, "provider": "stub", "model": "test-model"},
    )
    asked: list[str] = []

    def complete(system: str, user: str, *, temperature: float = 0.0) -> str:
        asked.append(user)
        return json.dumps(PROPOSED)

    monkeypatch.setattr(aure_adapter, "complete", complete)

    result = CliRunner().invoke(
        main, ["model", "new", "Sample1", "--name", "m", "--from-notes"]
    )

    assert result.exit_code == 0, result.output
    (user,) = asked
    assert "- state run100001 (run 100001): condition: in d8-THF" in user


def test_print_prompt_writes_the_skeleton_it_tells_the_agent_to_edit(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pointing an assistant at a file that does not exist is not a workflow."""
    from click.testing import CliRunner

    from nr_workbench.cli import main

    root = sample_with_data(project)
    monkeypatch.chdir(root)

    result = CliRunner().invoke(
        main, ["model", "new", "Sample1", "--name", "m", "--print-prompt"]
    )

    assert result.exit_code == 0, result.output
    assert (root / "samples/Sample1/models/m.yaml").is_file()
    assert "Fill in the model spec at" in result.output


def test_from_notes_without_an_endpoint_explains_the_other_path(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No endpoint is a normal state; the placeholder spec is still useful."""
    from click.testing import CliRunner

    from nr_workbench import aure_adapter
    from nr_workbench.cli import main

    root = sample_with_data(project)
    monkeypatch.chdir(root)
    monkeypatch.setattr(aure_adapter, "llm_info", lambda: {"available": False})

    result = CliRunner().invoke(
        main, ["model", "new", "Sample1", "--name", "m", "--from-notes"]
    )

    assert result.exit_code == 0, result.output
    assert "No language-model endpoint" in result.output
    assert "--print-prompt" in result.output
    assert (root / "samples/Sample1/models/m.yaml").is_file()


def test_from_notes_merges_a_proposal_and_records_who_made_it(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proposed stack must be labelled as proposed, not presented as measured."""
    import yaml
    from click.testing import CliRunner

    from nr_workbench import aure_adapter
    from nr_workbench.cli import main

    root = sample_with_data(project)
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        aure_adapter,
        "llm_info",
        lambda: {"available": True, "provider": "stub", "model": "test-model"},
    )
    monkeypatch.setattr(
        aure_adapter, "complete", lambda system, user, **kw: json.dumps(PROPOSED)
    )

    result = CliRunner().invoke(
        main, ["model", "new", "Sample1", "--name", "m", "--from-notes"]
    )

    assert result.exit_code == 0, result.output
    written = (root / "samples/Sample1/models/m.yaml").read_text(encoding="utf-8")

    assert "PROPOSED by stub/test-model" in written
    assert "PLACEHOLDER" not in written
    document = yaml.safe_load(written)
    assert [layer["name"] for layer in document["stack"]] == ["THF", "Cu", "Si"]
    # and the angles are still the files' own: the spec carries none, so no
    # proposal can have changed them, and resolving reads each header.
    from nr_workbench.spec.models import load_spec
    from nr_workbench.spec.resolve import discover_measurements

    assert "thetas" not in document["states"][0]
    spec = load_spec(root / "samples/Sample1/models/m.yaml")
    resolved = discover_measurements(spec, root)["run100001"]
    assert [round(m.theta, 4) for m in resolved] == [0.4499, 1.2009]


def test_from_notes_keeps_the_placeholder_when_the_reply_is_unusable(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal or a garbled reply must not produce a broken spec."""
    from click.testing import CliRunner

    from nr_workbench import aure_adapter
    from nr_workbench.cli import main

    root = sample_with_data(project)
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        aure_adapter,
        "llm_info",
        lambda: {"available": True, "provider": "stub", "model": "test-model"},
    )
    monkeypatch.setattr(
        aure_adapter, "complete", lambda system, user, **kw: "I cannot help with that."
    )

    result = CliRunner().invoke(
        main, ["model", "new", "Sample1", "--name", "m", "--from-notes"]
    )

    assert result.exit_code == 0, result.output
    assert "Keeping the placeholder stack" in result.output
    written = (root / "samples/Sample1/models/m.yaml").read_text(encoding="utf-8")
    assert "PLACEHOLDER" in written
    assert is_placeholder(written)  # what the page's badge reads


def test_an_omitted_constraint_is_rebuilt_from_the_per_state_parameters() -> None:
    """A proposal often describes the constraint change instead of returning it.

    A real reply said "the existing linear_in_time constraint should be moved
    from Film.thickness to CuOx.thickness" -- in `notes` -- and omitted the
    block. Dropping it leaves every slice refitting the whole structure
    independently, which is never what was wanted and is invisible in the spec.

    The right paths are derivable: a parameter declared `per: state` across
    both endpoints is by definition something the experiment changed between
    them.
    """
    skeleton = {
        "stack": [{"name": "Film", "material": "Film", "thickness": 100}],
        "states": [{"name": "ocv1"}, {"name": "ocv2"}],
        "series": [{"name": "tnr"}],
        "constraints": [
            {
                "series": "tnr",
                "form": "linear_in_time",
                "from": "ocv1",
                "to": "ocv2",
                "paths": ["Film.thickness"],
            }
        ],
    }
    reply = json.dumps(
        {
            "stack": [
                {"name": "CuOx", "material": "CuOx", "thickness": 40},
                {"name": "Cu", "material": "Cu", "thickness": 500},
                {"name": "Si", "material": "Si"},
            ],
            "parameters": [
                {
                    "path": "CuOx.thickness",
                    "range": [10, 80],
                    "per": "state",
                    "in": ["ocv1", "ocv2"],
                },
                {"path": "CuOx.rho", "range": [4, 5.5], "per": "model"},
                {"path": "probe.intensity", "value": 1.0, "pm": 0.1, "per": "state"},
            ],
        }
    )

    merged = merge_proposal(skeleton, parse_proposal(reply))

    assert merged["constraints"] == [
        {
            "series": "tnr",
            "form": "linear_in_time",
            "from": "ocv1",
            "to": "ocv2",
            "paths": ["CuOx.thickness"],
        }
    ]


def test_the_rebuild_excludes_model_scoped_and_probe_parameters() -> None:
    """Only quantities that differ between the endpoints should interpolate.

    A `per: model` value is the same in both states, so interpolating it would
    be a no-op dressed up as physics; an intensity is a nuisance, not structure.
    """
    skeleton = {
        "stack": [{"name": "Film", "material": "Film"}],
        "states": [{"name": "a"}, {"name": "b"}],
        "series": [{"name": "s"}],
        "constraints": [
            {
                "series": "s",
                "form": "linear_in_time",
                "from": "a",
                "to": "b",
                "paths": ["Film.thickness"],
            }
        ],
    }
    reply = json.dumps(
        {
            "stack": [{"name": "Cu", "material": "Cu"}],
            "parameters": [
                {"path": "Cu.rho", "range": [5, 7], "per": "model"},
                {"path": "probe.intensity", "value": 1.0, "pm": 0.1, "per": "state"},
            ],
        }
    )

    merged = merge_proposal(skeleton, parse_proposal(reply))

    assert "constraints" not in merged, "nothing varies between the states"


def test_a_returned_constraint_is_preferred_over_a_rebuild() -> None:
    """The rebuild is a fallback, not an override."""
    skeleton = {
        "stack": [{"name": "Film", "material": "Film"}],
        "states": [{"name": "a"}, {"name": "b"}],
        "series": [{"name": "s"}],
        "constraints": [
            {
                "series": "s",
                "form": "linear_in_time",
                "from": "a",
                "to": "b",
                "paths": ["Film.thickness"],
            }
        ],
    }
    reply = json.dumps(
        {
            "stack": [{"name": "Cu", "material": "Cu"}],
            "parameters": [
                {
                    "path": "Cu.thickness",
                    "range": [1, 2],
                    "per": "state",
                    "in": ["a", "b"],
                },
            ],
            "constraints": [
                {
                    "series": "s",
                    "form": "logistic",
                    "from": "a",
                    "to": "b",
                    "paths": ["Cu.thickness"],
                }
            ],
        }
    )

    merged = merge_proposal(skeleton, parse_proposal(reply))

    assert merged["constraints"][0]["form"] == "logistic"


def test_the_prompt_lists_the_constraint_forms_and_how_to_choose(
    project: Path,
) -> None:
    """ "How do I ask for a linear constraint" must have an answer in the prompt."""
    system, _ = build_prompt(skeleton=SKELETON, notes="", skills=find_skills(project))

    for form in ("linear_in_time", "logistic", "exponential", "piecewise_linear"):
        assert form in system
    assert "RETURN a `constraints` block" in system
    assert "oscillatory" in system.lower()


# --------------------------------------------------------------------------
# What the data says, and how the geometry reads it
# --------------------------------------------------------------------------

REFERENCE = Path(__file__).parent / "data" / "reference" / "steady"


def _facts(tmp_path: Path, notes: str) -> str:
    """`nrw model new`'s measured facts for the reference segment, under *notes*."""
    pytest.importorskip("aure")
    from nr_workbench.commands.model import _measured_facts
    from nr_workbench.project.layout import ProjectLayout

    segment = REFERENCE / "REFL_218386_1_218386_partial.txt"
    (tmp_path / "data").mkdir(exist_ok=True)
    shutil.copy(segment, tmp_path / "data" / segment.name)
    document = {
        "states": [{"name": "ocv1", "run": 218386, "data_dir": "data"}],
        "probe": {},
    }
    return _measured_facts(ProjectLayout(tmp_path), document, notes)


def _step(facts: str) -> float:
    found = re.search(r"an SLD step of (-?[\d.]+)", facts)
    assert found, facts
    return float(found.group(1))


ADD_THE_SUBSTRATE = "add the SLD of the substrate the notes name"


def test_the_reference_edge_is_a_step_the_substrate_is_added_to(
    tmp_path: Path,
) -> None:
    """The number the reading exists for: a step of 4.28 is no solvent; plus
    silicon's 2.07 it is 6.35, d8-THF exactly. Offered as "a topmost SLD near
    4.28", it had a proposal set the solvent's range to [4.0, 4.7]."""
    facts = _facts(tmp_path, "Cu on Ti on Si, in d8-THF, measured through the silicon.")

    assert _step(facts) == pytest.approx(4.28, abs=0.005)
    assert ADD_THE_SUBSTRATE in facts
    assert "the edge is the measurement" in facts


def test_the_facts_are_the_same_whatever_the_substrate(tmp_path: Path) -> None:
    """The substrate is the model's to name, from the notes: nothing here adds
    one. Reading the scaffold's placeholder silicon corrected every sample as
    silicon, and a table of substrate names, tried next, is never complete."""
    facts = {
        _facts(tmp_path, notes)
        for notes in (
            "Cu on Ti on Si, measured through the silicon.",
            "Cu on Ti on sapphire, measured through the sapphire.",
            "Cu on Ti, measured through the substrate.",
            "Cu on sapphire, measured through the substrate. "
            "Aligned on a Si wafer first.",
        )
    }

    assert len(facts) == 1, facts
    assert ADD_THE_SUBSTRATE in facts.pop()


def test_a_film_in_air_is_told_only_what_a_step_is(tmp_path: Path) -> None:
    facts = _facts(tmp_path, "30 nm polystyrene on silicon, in air.")

    assert "from air, it is the reflecting medium's own SLD" in facts
    assert ADD_THE_SUBSTRATE not in facts


def test_no_edge_is_given_no_reading(tmp_path: Path) -> None:
    """How to read a step is said only where there is a step to read."""
    from nr_workbench.commands.model import _measured_facts
    from nr_workbench.project.layout import ProjectLayout

    document = {
        "states": [{"name": "ocv1", "run": 218386, "data_dir": "data"}],
        "probe": {"back_reflection": True},
    }

    assert _measured_facts(ProjectLayout(tmp_path), document, "") == ""


def test_from_notes_sends_the_edge_and_how_to_read_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the command, on the reference data: the reading for back
    reflection reaches the prompt only if the notes reach the facts."""
    pytest.importorskip("aure")
    from click.testing import CliRunner

    from nr_workbench import aure_adapter
    from nr_workbench.cli import main

    root = tmp_path / "proj"
    assert (
        CliRunner().invoke(main, ["init", str(root), "--sample", "S1"]).exit_code == 0
    )
    steady = root / "samples" / "S1" / "data" / "steady"
    steady.mkdir(parents=True, exist_ok=True)
    for segment in REFERENCE.glob("REFL_218386_*_partial.txt"):
        shutil.copy(segment, steady / segment.name)
    (root / "samples" / "S1" / "sample.md").write_text(
        "# S1\n\n## Description\n\n"
        "Cu on Ti on Si, in d8-THF, measured through the silicon.\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(root)
    monkeypatch.setattr(
        aure_adapter,
        "llm_info",
        lambda: {"available": True, "provider": "stub", "model": "test-model"},
    )
    asked: list[str] = []

    def complete(system: str, user: str, *, temperature: float = 0.0) -> str:
        asked.append(user)
        return json.dumps(PROPOSED)

    monkeypatch.setattr(aure_adapter, "complete", complete)

    result = CliRunner().invoke(
        main, ["model", "new", "S1", "--name", "m", "--from-notes"]
    )

    assert result.exit_code == 0, result.output
    (user,) = asked
    assert "an SLD step of 4.28" in user
    assert ADD_THE_SUBSTRATE in user
