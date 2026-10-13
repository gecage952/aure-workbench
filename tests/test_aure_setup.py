"""Composing an AuRE setup from a sample's files and its prose.

The load-bearing assertion here is :func:`test_composed_setup_loads_in_aure`:
AuRE rejects unknown top-level keys and validates a state's files against the
resolved instrument, so a document that merely *looks* right is not evidence.
Every other test in this file is about getting a specific thing right; that one
is about the whole thing being accepted by the code that will actually run it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from nr_workbench.aure_setup import (
    SetupError,
    back_reflection_stated,
    compose,
    describe_sample,
    measurement_context,
    reads_as_back_reflection,
    state_files,
)
from nr_workbench.project.scan import SteadyMeasurement, scan_sample

REFERENCE = Path(__file__).parent / "data" / "reference" / "steady"

DESCRIBED = """# Cu on Ti

## Description

50 nm copper on 5 nm titanium on a silicon wafer.

## Details

Measured in d8-THF, through the silicon substrate.
"""


#: The same sample, with what was written about its measurements: a condition
#: in the table, the sample's own measurement conditions, and a note on a run.
MEASURED = (
    DESCRIBED
    + """
## Measurements

| Run | Type | Condition |
|---|---|---|
| 218386 | steady | D2O, 25 C |

## Measurement conditions

The cell was filled the day before.

- Run 218386: realigned after mounting
- Run 218399: the beam dropped
"""
)


def _sample_with_data(project: Path, notes: str = DESCRIBED, run: int = 218386) -> None:
    """Copy the reference partials into Sample1 and write its notes."""
    steady = project / "samples" / "Sample1" / "data" / "steady"
    steady.mkdir(parents=True, exist_ok=True)
    for path in sorted(REFERENCE.glob(f"REFL_{run}_*_partial.txt")):
        shutil.copy(path, steady / path.name)
    (project / "samples" / "Sample1" / "sample.md").write_text(notes, encoding="utf-8")


# --------------------------------------------------------------------------
# Reading the prose
# --------------------------------------------------------------------------


def test_describe_sample_joins_description_and_details() -> None:
    """AuRE gets one sentence; both sections contribute to it."""
    described = describe_sample(DESCRIBED)

    assert "50 nm copper" in described
    assert "d8-THF" in described


def test_describe_sample_reads_an_untouched_template_as_empty() -> None:
    """The template's own prompt comments must not become the description.

    This is the whole reason `nrw aure new` can refuse: an unfilled section has
    to be distinguishable from a filled one, or the refusal never fires and
    AuRE is handed a paragraph of instructions to itself.
    """
    from nr_workbench.commands.sample import plan_sample_files
    from nr_workbench.project.render import RenderContext

    planned = plan_sample_files(RenderContext(project_name="p"), "Sample1")
    stub = next(p for p in planned if p.relpath.endswith("sample.md"))

    assert describe_sample(stub.content.decode("utf-8")) == ""


@pytest.mark.parametrize(
    "phrase",
    [
        "measured through the substrate",
        # The phrasing that a plain substring list missed: naming the
        # substrate material is the more natural sentence, and it is the one
        # the reference notes actually use.
        "measured through the silicon substrate",
        "the beam comes in through the wafer",
        "neutrons enter from the substrate side",
        "this is a back reflection geometry",
        "back-reflection, as usual for the cell",
        "beam enters the back of the sample",
        # A negation of something else, near the phrase, rules nothing out.
        "not deuterated, and measured through the silicon substrate",
        "the film has no oxide and is measured through the substrate",
        "No adhesion layer. Measured through the silicon substrate.",
        "no oxide—measured through the substrate",
        "- no adhesion layer\n- measured through the silicon substrate",
        "not in air but through the substrate",
        "Not from the air side: measured through the silicon substrate",
        "measured not only through the substrate",
    ],
)
def test_back_reflection_is_read_from_the_notes(phrase: str) -> None:
    """Each phrase a scientist actually writes for the same geometry."""
    assert reads_as_back_reflection(phrase) is True
    assert back_reflection_stated(phrase) is True


@pytest.mark.parametrize(
    "phrase",
    [
        "30 nm polystyrene on silicon, in air",
        # "through the <thing>" is only about geometry when <thing> is a
        # substrate; a film is part of the sample description.
        "the solvent diffuses through the polymer film",
    ],
)
def test_front_reflection_notes_do_not_set_back_reflection(phrase: str) -> None:
    """A film in air must not be flipped, and the pattern must not over-reach."""
    assert reads_as_back_reflection(phrase) is False
    assert back_reflection_stated(phrase) is None


@pytest.mark.parametrize(
    "phrase",
    [
        # Written by nr-watcher's assessment for every sample of a beamtime of
        # films in air; each one's setup came out in back reflection.
        "Ambient medium is air, SLD 0, no deuteration. The beam enters from the "
        "air side, not through the substrate.",
        # Wrapped as notes are: a line break is not the end of the clause.
        "The beam enters from the air side, not\nthrough the substrate.",
        "the beam enters from the air side (not through the substrate)",
        "air side—not through the silicon",
        "this is not a back reflection geometry",
        "no back-reflection: a film in air",
        "measured without back reflection",
        "the beam does not arrive at the sample through the substrate",
        "it isn't measured through the wafer",
        "measured from the air side rather than through the substrate",
        "neither in air nor through the substrate",
    ],
)
def test_a_negated_phrase_rules_back_reflection_out(phrase: str) -> None:
    """Notes rule a geometry out in the same words that would state it."""
    assert reads_as_back_reflection(phrase) is False
    assert back_reflection_stated(phrase) is False


def test_the_critical_edge_is_corrected_only_in_back_reflection() -> None:
    """``nrw model new --from-notes`` reads the geometry the same way: a film
    in air whose notes rule the substrate side out is not corrected for it."""
    from nr_workbench.commands.model import Substrate, _back_reflection_substrate

    document = {
        "stack": [{"name": "air", "material": "air"}, {"name": "Si", "material": "Si"}],
        "materials": {"air": {"rho": 0.0}, "Si": {"rho": 2.07}},
        "probe": {},
    }
    through = "Measured in D2O through the silicon substrate."
    air_side = "The beam enters from the air side, not through the substrate."
    assert _back_reflection_substrate(document, through) == Substrate(
        ("silicon",), 2.07
    )
    assert _back_reflection_substrate(document, air_side) is None


@pytest.mark.parametrize(
    ("notes", "named", "sld"),
    [
        # The stack below ends in Si, as the scaffold's always does: the
        # substrate has to come from the notes, or every one is silicon.
        ("Ni on sapphire, measured through the sapphire.", ("sapphire",), 5.67),
        ("Cu on Ti on a quartz block, through the substrate.", ("quartz",), 3.47),
        ("Polymer on glass, measured through the substrate.", ("glass",), None),
        ("Measured through the substrate.", (), None),
        # Two materials: the alignment wafer is not the sample's substrate,
        # and this does not choose between them.
        (
            "Cu on Ti on sapphire, in d8-THF, measured through the substrate. "
            "Alignment was done on a Si wafer first.",
            ("sapphire", "silicon"),
            None,
        ),
    ],
)
def test_the_edge_is_corrected_for_the_substrate_the_notes_name(
    notes: str, named: tuple[str, ...], sld: float | None
) -> None:
    from nr_workbench.commands.model import Substrate, _back_reflection_substrate

    document = {
        "stack": [
            {"name": "Film", "material": "Film"},
            {"name": "Si", "material": "Si"},
        ],
        "materials": {"Film": {"rho": 4.0}, "Si": {"rho": 2.07}},
        "probe": {},
    }

    assert _back_reflection_substrate(document, notes) == Substrate(named, sld)


@pytest.mark.parametrize(
    ("notes", "named"),
    [
        ("50 nm Cu on Ti on a Si wafer, through the silicon substrate.", ["silicon"]),
        # A stack written top-down: the last thing it sits on is the substrate.
        ("Cu on Ti on SiO2 on Si, in D2O, back reflection.", ["silicon"]),
        ("Not through the glass: through the sapphire.", ["sapphire"]),
        ("A sapphire substrate under 20 nm Ni.", ["sapphire"]),
        ("Not a Si wafer: this is a sapphire substrate.", ["sapphire"]),
        ("Not a glass substrate; Cu on sapphire.", ["sapphire"]),
        # The bulk under its oxide, not the oxide.
        ("Cu on a Si/SiO2 wafer, measured through the substrate.", ["silicon"]),
        (
            "Cu on sapphire. We measured the same film on Si before.",
            ["sapphire", "silicon"],
        ),
        ("Cu on fused silica, measured through the fused silica.", ["quartz"]),
        ("A copper film in air.", []),
    ],
)
def test_the_substrate_is_read_where_the_notes_name_it(
    notes: str, named: list[str]
) -> None:
    from nr_workbench.aure_setup import named_substrates

    assert named_substrates(notes) == named


def test_every_substrate_name_is_also_read_as_back_reflection() -> None:
    """One table for both readers: two lists had drifted, so "through the fused
    silica" named a substrate to one and no geometry at all to the other."""
    from nr_workbench.aure_setup import SUBSTRATE_ALIASES, named_substrates

    for alias, material in SUBSTRATE_ALIASES.items():
        notes = f"Measured through the {alias}."
        assert back_reflection_stated(notes) is True, alias
        assert named_substrates(notes) == [material], alias


def test_long_notes_are_read_in_linear_time() -> None:
    """Each match rescanned the whole note before it for a negation: 21 s on a
    160 KB note built for it. Both readers now look a bounded way back."""
    import time

    from nr_workbench.aure_setup import named_substrates
    from nr_workbench.spec.authoring import extract_json

    crafted = "not through the si " * 8400
    started = time.monotonic()
    named_substrates(crafted)
    back_reflection_stated(crafted)
    # And a reply of unclosed fences: 19.5 s through the lazy fence regex.
    extract_json("```{" * 40000)
    assert time.monotonic() - started < 5


# --------------------------------------------------------------------------
# Choosing the files
# --------------------------------------------------------------------------


def test_state_files_prefers_partials_in_segment_order() -> None:
    """Segment order is Q order; shuffled input must not survive."""
    measurement = SteadyMeasurement(
        run=218386,
        combined="samples/S/data/steady/REFL_218386_combined_data_auto.txt",
        partials={3: "third.txt", 1: "first.txt", 2: "second.txt"},
    )

    files, kind = state_files(measurement)

    assert files == ["first.txt", "second.txt", "third.txt"]
    assert kind == "partials"


def test_state_files_never_mixes_partials_with_the_combined_file() -> None:
    """AuRE refuses a mixed state; the mix must not be built in the first place.

    A run reduced both ways is the normal case, not an edge case, so this is
    the default path rather than a guard.
    """
    measurement = SteadyMeasurement(
        run=218386,
        combined="combined.txt",
        partials={1: "first.txt"},
    )

    files, _ = state_files(measurement)

    assert "combined.txt" not in files


def test_state_files_falls_back_to_the_combined_file() -> None:
    """A run reduced only as a summed dataset is still fittable."""
    measurement = SteadyMeasurement(run=218386, combined="combined.txt")

    files, kind = state_files(measurement)

    assert (files, kind) == (["combined.txt"], "combined")


def test_state_files_rejects_a_run_with_nothing_on_disk() -> None:
    """A register entry is not data."""
    with pytest.raises(SetupError, match="no reduced files"):
        state_files(SteadyMeasurement(run=218386))


# --------------------------------------------------------------------------
# Composing
# --------------------------------------------------------------------------


def test_compose_puts_every_segment_in_one_state(project: Path) -> None:
    """The three Q segments of one curve are one physical sample, so one state."""
    _sample_with_data(project)
    scan = scan_sample(project, "Sample1")

    composed = compose(
        sample="Sample1", scan=scan, notes=DESCRIBED, root=project, run=218386
    )

    states = composed.document["states"]
    assert len(states) == 1
    assert len(states[0]["data_files"]) == 3


def test_compose_sets_back_reflection_from_the_notes(project: Path) -> None:
    """'through the silicon substrate' is the whole signal."""
    _sample_with_data(project)
    scan = scan_sample(project, "Sample1")

    composed = compose(
        sample="Sample1", scan=scan, notes=DESCRIBED, root=project, run=218386
    )

    assert composed.document["states"][0]["back_reflection"] is True


def test_compose_warns_when_back_reflection_was_not_stated(project: Path) -> None:
    """Silence is not evidence of front reflection, so it is reported."""
    notes = "# S\n\n## Description\n\n30 nm polystyrene on silicon in air.\n"
    _sample_with_data(project, notes=notes)
    scan = scan_sample(project, "Sample1")

    composed = compose(
        sample="Sample1", scan=scan, notes=notes, root=project, run=218386
    )

    assert "back_reflection" not in composed.document["states"][0]
    assert any("back_reflection" in w for w in composed.warnings)


def test_compose_says_when_the_notes_rule_back_reflection_out(project: Path) -> None:
    """Notes that rule the substrate side out are evidence, not silence."""
    notes = (
        "# S\n\n## Description\n\n30 nm polystyrene on silicon, in air. The beam "
        "enters from the air side, not through the substrate.\n"
    )
    _sample_with_data(project, notes=notes)
    scan = scan_sample(project, "Sample1")

    composed = compose(
        sample="Sample1", scan=scan, notes=notes, root=project, run=218386
    )

    assert "back_reflection" not in composed.document["states"][0]
    assert (
        "back_reflection: False -- the notes say the beam does not arrive through "
        "the substrate"
    ) in composed.from_notes
    assert not any("back_reflection" in w for w in composed.warnings)


def test_compose_refuses_an_unfilled_description(project: Path) -> None:
    """The one thing AuRE cannot derive, so the one thing worth refusing over."""
    _sample_with_data(project, notes="# S\n\n## Description\n\n<!-- a comment -->\n")
    scan = scan_sample(project, "Sample1")

    with pytest.raises(SetupError, match="## Description"):
        compose(
            sample="Sample1", scan=scan, notes="# S\n\n## Description\n", root=project
        )


def test_compose_carries_the_hypothesis_from_the_notes(project: Path) -> None:
    """What the scientist already suspects is worth more than a blank slate."""
    notes = DESCRIBED + "\n## Fits to perform\n\nThere may be a CuOx skin.\n"
    _sample_with_data(project, notes=notes)
    scan = scan_sample(project, "Sample1")

    composed = compose(
        sample="Sample1", scan=scan, notes=notes, root=project, run=218386
    )

    assert "CuOx" in composed.document["hypothesis"]


def test_compose_requires_a_run_when_the_sample_has_several(project: Path) -> None:
    """Two conditions is a co-refinement, which is not a first fit."""
    _sample_with_data(project, run=218386)
    _sample_with_data(project, run=218393)
    scan = scan_sample(project, "Sample1")

    with pytest.raises(SetupError, match="--run"):
        compose(sample="Sample1", scan=scan, notes=DESCRIBED, root=project)


def test_compose_writes_no_absolute_paths(project: Path) -> None:
    """The defect this project exists to remove must not re-enter through AuRE."""
    _sample_with_data(project)
    scan = scan_sample(project, "Sample1")

    composed = compose(
        sample="Sample1", scan=scan, notes=DESCRIBED, root=project, run=218386
    )

    rendered = repr(composed.document)
    assert str(project) not in rendered
    assert "/Users/" not in rendered


# --------------------------------------------------------------------------
# The one that matters
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_compose_tells_aure_what_was_written_about_the_measurement(
    project: Path,
) -> None:
    """A contrast or a condition written for the run reaches the language model:
    in the sample's description, which every AuRE prompt reads. Not in a state's
    extra_description, which AuRE declares but no prompt of its reads."""
    _sample_with_data(project, notes=MEASURED)
    scan = scan_sample(project, "Sample1")

    composed = compose(
        sample="Sample1", scan=scan, notes=MEASURED, root=project, run=218386
    )

    description = composed.document["sample_description"]
    assert description.startswith("50 nm copper on 5 nm titanium")
    assert description.splitlines()[-3:] == [
        "The condition of run 218386: D2O, 25 C.",
        "Measurement conditions: The cell was filled the day before.",
        "Notes on run 218386: realigned after mounting",
    ]
    assert "extra_description" not in composed.document["states"][0]
    assert "run 218386's condition" in composed.from_notes[0]


def test_a_note_on_the_run_reaches_aure_though_no_table_lists_it(project: Path) -> None:
    """Notes written by hand, before any table: the catalog cannot hold such a
    note, but AuRE fitting that run should still read it."""
    notes = DESCRIBED + "\n## Measurement conditions\n\n- Run 218386: in D2O\n"
    _sample_with_data(project, notes=notes)
    scan = scan_sample(project, "Sample1")

    composed = compose(
        sample="Sample1", scan=scan, notes=notes, root=project, run=218386
    )

    assert composed.document["sample_description"].endswith(
        "\n\nNotes on run 218386: in D2O"
    )


def test_a_condition_written_as_a_sentence_gets_one_full_stop() -> None:
    notes = (
        DESCRIBED + "\n## Measurements\n\n| Run | Type | Condition |\n|---|---|---|\n"
        "| 218386 | steady | The ambient medium is D2O. |\n"
    )

    said = measurement_context(notes, 218386)

    assert said == "The condition of run 218386: The ambient medium is D2O."


def test_compose_adds_nothing_when_nothing_was_written_about_it(project: Path) -> None:
    _sample_with_data(project)
    scan = scan_sample(project, "Sample1")

    composed = compose(
        sample="Sample1", scan=scan, notes=DESCRIBED, root=project, run=218386
    )

    assert "extra_description" not in composed.document["states"][0]
    assert composed.document["sample_description"] == describe_sample(DESCRIBED)


@pytest.mark.parametrize("notes", [DESCRIBED, MEASURED], ids=["described", "measured"])
def test_composed_setup_loads_in_aure(project: Path, notes: str) -> None:
    """AuRE itself must accept the document, not just our reading of its schema.

    It rejects unknown top-level keys, resolves every data file, and validates
    the state against the instrument its filenames imply -- so this covers the
    combined/partial mixing rule and the shared-set-id rule at the same time.
    """
    pytest.importorskip("aure")
    import yaml

    from nr_workbench.aure_adapter import validate_setup
    from nr_workbench.aure_setup import setup_dir

    _sample_with_data(project, notes=notes)
    scan = scan_sample(project, "Sample1")
    composed = compose(
        sample="Sample1", scan=scan, notes=notes, root=project, run=218386
    )

    target = setup_dir(project, "Sample1", "Sample1-218386") / "setup.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(composed.document, sort_keys=False), "utf-8")

    loaded = validate_setup(target)

    assert len(loaded["states"]) == 1
    assert len(loaded["states"][0]["data_files"]) == 3


def test_choose_run_says_how_to_get_data_when_there_is_none(project: Path) -> None:
    """First contact with an empty sample; the message is the whole value."""
    from nr_workbench.aure_setup import choose_run

    scan = scan_sample(project, "Sample1")

    with pytest.raises(SetupError, match="nrw sample scan"):
        choose_run(scan, None)


def test_choose_run_lists_what_it_found_for_a_bad_run(project: Path) -> None:
    """A --run typo should not require going and looking."""
    from nr_workbench.aure_setup import choose_run

    _sample_with_data(project)
    scan = scan_sample(project, "Sample1")

    with pytest.raises(SetupError, match="218386"):
        choose_run(scan, 999999)


def test_validate_setup_reports_aure_being_absent_as_such(
    project: Path, monkeypatch
) -> None:
    """ "AuRE is not installed" is our problem; "your data file is missing" is
    not, and the two must not share an exception -- one warrants a bug report.
    """
    import builtins

    from nr_workbench.aure_adapter import AureUnavailableError

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name.startswith("aure"):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    target = project / "setup.yaml"
    target.write_text("states: []\n", encoding="utf-8")

    from nr_workbench.aure_adapter import validate_setup

    with pytest.raises(AureUnavailableError, match="aure"):
        validate_setup(target)
