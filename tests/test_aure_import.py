"""Translating a finished AuRE run into an nrw model spec.

The stack-order tests carry the weight. Getting the order wrong does not raise
anywhere: the fit converges, reports a chi-squared in the hundreds, and names
no cause. So the geometry is asserted from both ends -- which medium is first,
which is last -- rather than by comparing against a golden list that would
encode the same mistake twice.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nr_workbench.aure_import import (
    ImportError_,
    fitted_model,
    ordered_stack,
    read_final_state,
    reported_chisq,
    reported_fit,
    to_spec,
    untranslatable,
)

#: A two-layer Cu/Ti stack on silicon, in the shape AuRE writes.
MODEL = {
    "substrate": {"name": "Si", "sld": 2.07, "roughness": 3.0},
    "layers": [
        {
            "name": "Ti",
            "sld": -1.95,
            "thickness": 50.0,
            "roughness": 5.0,
            "thickness_min": 30.0,
            "thickness_max": 70.0,
            "sld_min": -2.5,
            "sld_max": -1.5,
        },
        {
            "name": "Cu",
            "sld": 6.55,
            "thickness": 500.0,
            "roughness": 8.0,
            "thickness_min": 400.0,
            "thickness_max": 600.0,
        },
    ],
    "ambient": {"name": "dTHF", "sld": 6.35},
    "back_reflection": True,
    "dq_is_fwhm": True,
    "intensity": {"value": 1.0},
}


#: What AuRE reported for a lipid bilayer on silicon, measured through the
#: substrate: its layer names are prose, one of them twice. Imported as they
#: were, they made a spec its own validator refused.
PROSE_MODEL = {
    "substrate": {"name": "silicon", "sld": 2.07, "roughness": 3},
    "layers": [
        {
            "name": "silicon oxide",
            "sld": 4.147,
            "sld_min": 1.5,
            "sld_max": 5.5,
            "thickness": 21.0,
            "thickness_min": 7.5,
            "thickness_max": 30.0,
            "roughness": 13.9,
            "roughness_max": 30.0,
        },
        {
            "name": "DPPC bilayer headgroup region",
            "sld": 5.95,
            "sld_min": -1.0,
            "sld_max": 6.0,
            "thickness": 11.7,
            "thickness_min": 5.0,
            "thickness_max": 20.0,
            "roughness": 16.2,
        },
        {
            "name": "DPPC bilayer tail region",
            "sld": 1.41,
            "sld_min": -2.5,
            "sld_max": 2.0,
            "thickness": 15.5,
            "thickness_min": 15.0,
            "thickness_max": 60.0,
            "roughness": 5.8,
        },
        {
            "name": "DPPC bilayer headgroup region",
            "sld": 5.95,
            "sld_min": -1.0,
            "sld_max": 6.0,
            "thickness": 11.7,
            "thickness_min": 5.0,
            "thickness_max": 20.0,
            "roughness": 16.2,
        },
    ],
    "ambient": {"name": "water-based solvent (unspecified contrast)", "sld": 6.32},
    "back_reflection": True,
    "dq_is_fwhm": False,
    "intensity": {"value": 1.0, "min": 0.7, "max": 1.1, "fixed": False},
}


#: A state block in the shape `nrw model new` writes, with the measured angles.
STATES = [
    {
        "name": "run218386",
        "run": 218386,
        "segments": "auto",
        "thetas": [0.45, 1.201, 3.5003],
        "data_dir": "samples/Sample1/data/steady",
    }
]


def _final_state(model: dict, **kwargs) -> dict:
    """Wrap a model the way `final_state.json` does."""
    payload = {
        "completed_at": "2026-09-13T12:00:00",
        "success": True,
        "error": None,
        "final_chi2": 1.83,
        "state": {"current_model": model, "best_chi2": 1.83},
    }
    payload.update(kwargs)
    return payload


# --------------------------------------------------------------------------
# Reading the run
# --------------------------------------------------------------------------


def test_read_final_state_names_the_missing_file(tmp_path: Path) -> None:
    """An interrupted run is the common case; the message must say what to do."""
    with pytest.raises(ImportError_, match="resume"):
        read_final_state(tmp_path)


def test_fitted_model_prefers_current_model() -> None:
    """`current_model` is what AuRE reported -- finalize and the MCMC polish
    both write into it, so it is the only key that always agrees with the
    chi-squared beside it."""
    state = _final_state(MODEL)
    state["state"]["best_model"] = {"layers": [], "substrate": {}, "ambient": {}}

    assert fitted_model(state)["layers"][0]["name"] == "Ti"


def test_fitted_model_falls_back_to_best_model() -> None:
    """A run that never reached finalize still has an answer worth importing."""
    state = _final_state(MODEL)
    del state["state"]["current_model"]
    state["state"]["best_model"] = MODEL

    assert fitted_model(state)["layers"][0]["name"] == "Ti"


def test_fitted_model_refuses_a_failed_run() -> None:
    """Importing the model of a run that errored would fake a result."""
    state = _final_state(MODEL, error="intake failed")

    with pytest.raises(ImportError_, match="failed"):
        fitted_model(state)


def test_fitted_model_refuses_a_legacy_script_model() -> None:
    """An older run stored a model as a Python string; it cannot be translated."""
    state = _final_state(MODEL)
    state["state"]["current_model"] = "problem = FitProblem(...)"

    with pytest.raises(ImportError_, match="no structured model"):
        fitted_model(state)


def test_reported_chisq_reads_the_run() -> None:
    assert reported_chisq(_final_state(MODEL)) == pytest.approx(1.83)


# --------------------------------------------------------------------------
# Stack order -- the part that fails silently
# --------------------------------------------------------------------------


def test_back_reflection_puts_the_substrate_last() -> None:
    """refl1d takes the last entry as the incident medium, and in a
    solid/liquid cell the neutron arrives through the wafer."""
    stack = ordered_stack(MODEL)

    assert [entry["name"] for entry in stack] == ["dTHF", "Cu", "Ti", "Si"]


def test_front_reflection_puts_the_ambient_last() -> None:
    """A film in air is the other way round, and nothing else changes."""
    model = {**MODEL, "back_reflection": False}

    stack = ordered_stack(model)

    assert [entry["name"] for entry in stack] == ["Si", "Ti", "Cu", "dTHF"]


def test_layers_run_substrate_first_in_aure_order() -> None:
    """The premise the reversal depends on, asserted rather than assumed.

    `layers[0]` touches the substrate. In front reflection it therefore lands
    immediately after the substrate; if AuRE ever changed that, this is the
    test that says so instead of a fit quietly getting worse.
    """
    stack = ordered_stack({**MODEL, "back_reflection": False})

    assert stack[0]["name"] == "Si"
    assert stack[1]["name"] == MODEL["layers"][0]["name"]


def test_back_reflection_gives_the_ambient_the_outer_roughness() -> None:
    """The outer surface has no roughness of its own; upstream lends it the
    outermost layer's, and a spec that dropped it would fit a sharp interface
    that was never measured."""
    stack = ordered_stack(MODEL)

    assert stack[0]["roughness"] == MODEL["layers"][-1]["roughness"]


# --------------------------------------------------------------------------
# The spec
# --------------------------------------------------------------------------


def test_to_spec_carries_every_material() -> None:
    spec = to_spec(
        model=MODEL, sample="Sample1", name="first", states=STATES, chisq=1.83
    )

    assert set(spec["materials"]) == {"dTHF", "Cu", "Ti", "Si"}
    assert spec["materials"]["Cu"]["rho"] == pytest.approx(6.55)


def test_to_spec_leaves_the_incident_medium_semi_infinite() -> None:
    """The generator emits the last entry bare; a thickness there is a lie."""
    spec = to_spec(model=MODEL, sample="Sample1", name="first", states=STATES)

    assert "thickness" not in spec["stack"][-1]
    assert "thickness" in spec["stack"][0]


def test_to_spec_turns_recorded_bounds_into_free_parameters() -> None:
    """Only bounds AuRE recorded; a range nobody chose must not appear."""
    spec = to_spec(model=MODEL, sample="Sample1", name="first", states=STATES)

    paths = {p["path"] for p in spec["parameters"]}
    assert "Ti.thickness" in paths
    assert "Ti.rho" in paths
    assert "Cu.thickness" in paths
    # Cu declared no SLD bounds, so it gets no free SLD.
    assert "Cu.rho" not in paths


def test_bounds_land_on_the_right_layer_after_the_reversal() -> None:
    """The reversal renumbers the stack; a bound that slid one place would
    put copper's thickness range on titanium and still validate."""
    spec = to_spec(model=MODEL, sample="Sample1", name="first", states=STATES)

    ranges = {p["path"]: p["range"] for p in spec["parameters"] if "range" in p}
    assert ranges["Ti.thickness"] == [30.0, 70.0]
    assert ranges["Cu.thickness"] == [400.0, 600.0]


def test_repeated_material_names_stay_distinct() -> None:
    """D2O above and below a membrane is a real stack, and an nrw layer name
    is a key -- collapsing two layers into one would lose a layer silently."""
    model = {
        "substrate": {"name": "Si", "sld": 2.07, "roughness": 3.0},
        "layers": [
            {"name": "D2O", "sld": 6.36, "thickness": 20.0, "roughness": 3.0},
            {"name": "lipid", "sld": 0.2, "thickness": 40.0, "roughness": 4.0},
        ],
        "ambient": {"name": "D2O", "sld": 6.36},
        "back_reflection": False,
    }

    spec = to_spec(model=model, sample="S", name="n", states=STATES)

    assert len(spec["materials"]) == len(spec["stack"])


def test_to_spec_records_that_aure_proposed_it() -> None:
    """The reader has to know this is a starting point, not a measurement."""
    spec = to_spec(
        model=MODEL, sample="Sample1", name="first", states=STATES, chisq=1.83
    )

    assert "AuRE" in spec["description"]
    assert "not a measurement" in spec["description"]


def test_probe_carries_the_geometry() -> None:
    spec = to_spec(model=MODEL, sample="Sample1", name="first", states=STATES)

    assert spec["probe"]["back_reflection"] is True


# --------------------------------------------------------------------------
# The whole way through
# --------------------------------------------------------------------------


@pytest.mark.parametrize("model", [MODEL, PROSE_MODEL], ids=["plain", "prose"])
def test_translated_spec_validates_and_generates(
    tmp_path: Path, project: Path, model: dict
) -> None:
    """A spec nrw's own validator rejects is not an import, it is a draft.

    This is what makes the translation worth doing in code: the result goes
    straight into `nrw model generate` without a human retyping numbers --
    whatever AuRE called its layers.
    """
    import ast

    import yaml

    from nr_workbench.codegen.generator import generate
    from nr_workbench.spec.models import load_spec
    from nr_workbench.spec.resolve import build_table, discover_measurements
    from nr_workbench.spec.validate import validate_spec

    output = tmp_path / "output"
    output.mkdir()
    (output / "final_state.json").write_text(json.dumps(_final_state(model)), "utf-8")

    from nr_workbench.commands.model import state_for_run
    from nr_workbench.project.scan import scan_sample

    steady = project / "samples" / "Sample1" / "data" / "steady"
    steady.mkdir(parents=True, exist_ok=True)
    reference = Path(__file__).parent / "data" / "reference" / "steady"
    for source in sorted(reference.glob("REFL_218386_*_partial.txt")):
        (steady / source.name).write_bytes(source.read_bytes())
    scan = scan_sample(project, "Sample1")
    block, _, _ = state_for_run(project, scan.steady[218386])

    state = read_final_state(output)
    spec = to_spec(
        model=fitted_model(state),
        sample="Sample1",
        name="first",
        states=[block],
        chisq=reported_chisq(state),
    )

    target = project / "samples" / "Sample1" / "models" / "first.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")

    loaded = load_spec(target)
    report = validate_spec(loaded, project)
    assert not report.errors, report.errors

    source = generate(build_table(loaded, discover_measurements(loaded, project)))

    ast.parse(source)


# --------------------------------------------------------------------------
# What must be refused rather than guessed
# --------------------------------------------------------------------------


def test_a_missing_layer_sld_is_refused_not_defaulted() -> None:
    """An SLD of 0 is vacuum -- legal, plausible, and nothing downstream would
    question it. Defaulting here would be the silent wrong answer."""
    model = {
        **MODEL,
        "layers": [{**MODEL["layers"][0], "sld": None}, MODEL["layers"][1]],
    }

    with pytest.raises(ImportError_, match="no sld"):
        fitted_model(_final_state(model))


def test_a_missing_ambient_sld_is_refused() -> None:
    """In back reflection the ambient IS the incident medium, so a D2O that
    failed to parse would become vacuum at the boundary that matters most."""
    model = {**MODEL, "ambient": {"name": "dTHF"}}

    with pytest.raises(ImportError_, match="ambient"):
        fitted_model(_final_state(model))


def test_a_missing_thickness_is_refused() -> None:
    model = {**MODEL, "layers": [{**MODEL["layers"][0], "thickness": None}]}

    with pytest.raises(ImportError_, match="thickness"):
        fitted_model(_final_state(model))


def test_an_interfaces_block_is_refused() -> None:
    """`interfaces` overrides the positional roughness map this translation
    reproduces, so honouring the positions anyway would move every buried
    interface one place -- and still produce a spec that validates and fits."""
    model = {**MODEL, "interfaces": [{"below": "Si", "above": "Ti", "roughness": 4.0}]}

    with pytest.raises(ImportError_, match="interfaces"):
        fitted_model(_final_state(model))


def test_a_layer_that_is_not_a_mapping_is_refused() -> None:
    with pytest.raises(ImportError_, match="not a mapping"):
        fitted_model(_final_state({**MODEL, "layers": ["oxide"]}))


def test_an_absurd_layer_count_is_refused() -> None:
    """A number that large means the file is not what we think it is."""
    model = {**MODEL, "layers": [MODEL["layers"][0]] * 500}

    with pytest.raises(ImportError_, match="layers"):
        fitted_model(_final_state(model))


def test_malformed_json_names_the_file(tmp_path: Path) -> None:
    """A truncated write is the common real failure, not a corrupted one."""
    (tmp_path / "final_state.json").write_text('{"not json', encoding="utf-8")

    with pytest.raises(ImportError_, match="final_state.json"):
        read_final_state(tmp_path)


# --------------------------------------------------------------------------
# What must be reported rather than dropped
# --------------------------------------------------------------------------


def test_constraints_are_reported_as_not_carried_over() -> None:
    """An unconstrained spec quoting a constrained fit's chi-squared would be
    describing a different model than the number beside it."""
    model = {**MODEL, "constraints": ["Cu.thickness > Ti.thickness"]}

    notes = untranslatable(model)

    assert any("Cu.thickness" in note for note in notes)


def test_a_tied_roughness_is_reported_and_left_fixed() -> None:
    """sigma = fraction x thickness is a derived parameter, not a range, so
    there is nothing to import -- but the omission has to be visible."""
    tied = {
        **MODEL["layers"][0],
        "roughness_tie": {
            "fraction_init": 0.1,
            "fraction_min": 0.0,
            "fraction_max": 0.3,
        },
        "roughness_min": 1.0,
        "roughness_max": 9.0,
    }
    model = {**MODEL, "layers": [tied, MODEL["layers"][1]]}

    notes = untranslatable(model)
    spec = to_spec(model=model, sample="S", name="n", states=STATES)

    assert any("roughness" in note for note in notes)
    assert "Ti.roughness" not in {p["path"] for p in spec["parameters"]}


def test_nothing_is_reported_when_everything_carried_over() -> None:
    assert untranslatable(MODEL) == []


# --------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------


def test_recorded_roughness_bounds_become_free_parameters() -> None:
    """AuRE fits roughness and records its bounds; pinning every interface at
    its fitted value would make the re-fit a different problem from the one
    whose chi-squared the spec advertises."""
    layer = {**MODEL["layers"][0], "roughness_min": 1.0, "roughness_max": 9.0}
    model = {**MODEL, "layers": [layer, MODEL["layers"][1]]}

    spec = to_spec(model=model, sample="S", name="n", states=STATES)

    ranges = {p["path"]: p["range"] for p in spec["parameters"] if "range" in p}
    assert ranges["Ti.roughness"] == [1.0, 9.0]


def test_a_duplicate_name_and_a_bound_resolve_together() -> None:
    """`_free_parameters` indexes the de-duplicated names AND depends on the
    reversal. A path pointing at the ambient D2O instead of the inner one
    would still validate, so the two have to be exercised at once.
    """
    model = {
        "substrate": {"name": "Si", "sld": 2.07, "roughness": 3.0},
        "layers": [
            {
                "name": "D2O",
                "sld": 6.36,
                "thickness": 20.0,
                "roughness": 3.0,
                "thickness_min": 10.0,
                "thickness_max": 30.0,
            },
            {"name": "lipid", "sld": 0.2, "thickness": 40.0, "roughness": 4.0},
        ],
        "ambient": {"name": "D2O", "sld": 6.36},
        "back_reflection": True,
    }

    spec = to_spec(model=model, sample="S", name="n", states=STATES)

    ranges = {p["path"]: p["range"] for p in spec["parameters"] if "range" in p}
    # The ambient is first in back reflection and keeps the bare name, so the
    # inner layer is the suffixed one -- and it is the one with the bound.
    assert [entry["name"] for entry in spec["stack"]] == ["D2O", "lipid", "D2O2", "Si"]
    assert ranges["D2O2.thickness"] == [10.0, 30.0]


def test_a_fixed_intensity_is_not_made_free() -> None:
    model = {**MODEL, "intensity": {"value": 1.0, "fixed": True}}

    spec = to_spec(model=model, sample="S", name="n", states=STATES)

    assert "probe.intensity" not in {p["path"] for p in spec["parameters"]}


def test_to_spec_refuses_without_states() -> None:
    """The schema rejects a spec with neither a state nor a series."""
    with pytest.raises(ImportError_, match="at least one state"):
        to_spec(model=MODEL, sample="S", name="n", states=[])


def test_reported_chisq_falls_back_to_best_chi2() -> None:
    """A run whose final_chi2 is absent still recorded its best."""
    state = _final_state(MODEL)
    del state["final_chi2"]

    assert reported_chisq(state) == pytest.approx(1.83)


def test_reported_chisq_is_none_for_garbage() -> None:
    state = _final_state(MODEL)
    state["final_chi2"] = "not a number"
    state["state"]["best_chi2"] = None

    assert reported_chisq(state) is None


def test_prose_layer_names_become_names_a_spec_takes() -> None:
    spec = to_spec(model=PROSE_MODEL, sample="S", name="n", states=STATES)

    # Through the substrate: the ambient first, the layers reversed.
    assert [entry["name"] for entry in spec["stack"]] == [
        "water_based_solvent_unspecified_contrast",
        "DPPC_bilayer_headgroup_region",
        "DPPC_bilayer_tail_region",
        "DPPC_bilayer_headgroup_region2",
        "silicon_oxide",
        "silicon",
    ]
    paths = {p["path"] for p in spec["parameters"]}
    assert "silicon_oxide.thickness" in paths
    assert "DPPC_bilayer_headgroup_region2.rho" in paths


@pytest.mark.parametrize(
    ("aure", "spec"),
    [
        ("silicon oxide", "silicon_oxide"),
        (
            "water-based solvent (unspecified contrast)",
            "water_based_solvent_unspecified_contrast",
        ),
        ("Oxyde de silicium à 5 %", "Oxyde_de_silicium_a_5"),
        ("2nd oxide", "layer1_2nd_oxide"),
        ("()", "layer1"),
        ("class", "class_layer"),
        ("SLD", "SLD_layer"),
        ("probe", "probe_layer"),
        ("oxide\nimport os", "oxide_import_os"),
    ],
)
def test_spec_layer_name(aure: str, spec: str) -> None:
    from nr_workbench.aure_import import spec_layer_name

    assert spec_layer_name(aure, "layer1") == spec


def test_a_name_made_unique_never_meets_one_already_taken() -> None:
    """ "a" twice beside an "a2" of AuRE's own: the second "a" is "a3"."""
    model = {
        "substrate": {"name": "a", "sld": 2.07, "roughness": 3.0},
        "layers": [
            {"name": "a2", "sld": 1.0, "thickness": 10.0, "roughness": 3.0},
            {"name": "a", "sld": 1.0, "thickness": 10.0, "roughness": 3.0},
        ],
        "ambient": {"name": "air", "sld": 0.0},
        "back_reflection": False,
    }

    spec = to_spec(model=model, sample="S", name="n", states=STATES)

    assert [entry["name"] for entry in spec["stack"]] == ["a", "a2", "a3", "air"]


# --------------------------------------------------------------------------
# What the model has no field for
# --------------------------------------------------------------------------

#: A 250 nm Cu film in air, in the shape AuRE wrote it for IPTS-38693's cu100:
#: each roughness with a maximum and no minimum, broadening enabled, and no
#: room in the model for what the fit made of either.
FILM = {
    "substrate": {
        "name": "Si",
        "sld": 2.07,
        "roughness": 0.0138,
        "roughness_max": 20.0,
    },
    "layers": [
        {
            "name": "SiO2",
            "sld": 3.989,
            "sld_min": 2.8,
            "sld_max": 4.0,
            "thickness": 19.96,
            "thickness_min": 7.5,
            "thickness_max": 20.0,
            "roughness": 18.16,
            "roughness_max": 25.0,
        },
        {
            "name": "Cu",
            "sld": 6.136,
            "sld_min": 4.5,
            "sld_max": 6.55,
            "thickness": 2566.6,
            "thickness_min": 600.0,
            "thickness_max": 3200.0,
            "roughness": 30.49,
            "roughness_max": 60.0,
        },
    ],
    "ambient": {"name": "air", "sld": 0.0},
    "back_reflection": False,
    "dq_is_fwhm": True,
    "intensity": {"value": 1.0, "min": 0.6, "max": 1.4, "fixed": False},
    "sample_broadening": {"enabled": True, "min": 0.0, "max": 0.5},
    "theta_offset": {"enabled": False, "min": -0.02, "max": 0.02},
}

#: The round AuRE reported for it: values and bounds, under AuRE's own names.
FILM_FIT = {
    "parameters": {
        "intensity REFL_235256_1_235256_partial": 1.0134,
        "intensity REFL_235256_2_235257_partial": 1.1070,
        "intensity REFL_235256_3_235258_partial": 1.3709,
        "sample_broadening": 0.0352,
        "Si interface": 0.0138,
        "SiO2 interface": 18.16,
        "SiO2 rho": 3.989,
        "SiO2 thickness": 19.96,
        "Cu interface": 30.49,
        "Cu rho": 6.136,
        "Cu thickness": 2566.6,
    },
    "bounds": {
        "intensity REFL_235256_1_235256_partial": [0.6, 1.4],
        "intensity REFL_235256_2_235257_partial": [0.6, 1.4],
        "intensity REFL_235256_3_235258_partial": [0.6, 1.4],
        "sample_broadening": [0.0, 0.5],
        "Si interface": [0.0, 20.0],
        "SiO2 interface": [5.0, 25.0],
        "SiO2 rho": [2.8, 4.0],
        "SiO2 thickness": [7.5, 20.0],
        "Cu interface": [5.0, 60.0],
        "Cu rho": [4.5, 6.55],
        "Cu thickness": [600.0, 3200.0],
    },
}


def _with_fits(model: dict, fits: list[dict], **state) -> dict:
    """A final state whose ``fit_results`` are *fits*, wrapped as AuRE writes it."""
    payload = _final_state(model)
    payload["state"].update({"fit_results": fits, **state})
    return payload


def test_the_reported_fit_is_the_round_behind_the_model() -> None:
    """The model's values are the selected round's, or the adopted polish's."""
    rounds = [
        {"parameters": {"x": float(n)}, "bounds": {"x": [0, 9]}} for n in range(3)
    ]

    chosen = _with_fits(MODEL, rounds, final_selection={"index": 1})
    assert reported_fit(chosen)["parameters"] == {"x": 1.0}
    polished = _with_fits(
        MODEL, rounds, final_selection={"index": 1}, final_fit={"adopted": True}
    )
    assert reported_fit(polished)["parameters"] == {"x": 2.0}
    rejected = _with_fits(
        MODEL, rounds, final_selection={"index": 1}, final_fit={"adopted": False}
    )
    assert reported_fit(rejected)["parameters"] == {"x": 1.0}
    assert reported_fit(_with_fits(MODEL, rounds))["parameters"] == {"x": 2.0}
    assert reported_fit(_final_state(MODEL)) == {"parameters": {}, "bounds": {}}


def test_a_bound_that_is_no_range_is_not_reported() -> None:
    fit = {"parameters": {"a": 1.0}, "bounds": {"a": [2, 1], "b": [0, "x"], "c": [0]}}
    assert reported_fit(_with_fits(MODEL, [fit]))["bounds"] == {}


def test_what_the_model_has_no_field_for_comes_from_the_fit() -> None:
    """The reported failure: cu100's recorded fit had a chi-squared of 137 where
    AuRE's was 13.6. The model had no field for AuRE's sample broadening or its
    three segment intensities, and gave its roughnesses no minimum, so the spec
    fitted 7 of AuRE's 14 parameters. Every one of them is carried now."""
    spec = to_spec(model=FILM, sample="cu100", name="n", states=STATES, fit=FILM_FIT)

    entries = {p["path"]: p for p in spec["parameters"]}
    ranges = {path: p.get("range") for path, p in entries.items()}
    # Each interface AuRE fitted, the substrate's too, in the bounds it used.
    assert ranges["Si.roughness"] == [0.0, 20.0]
    assert ranges["SiO2.roughness"] == [5.0, 25.0]
    assert ranges["Cu.roughness"] == [5.0, 60.0]
    assert ranges["Cu.thickness"] == [600.0, 3200.0]
    # One intensity per angle segment, as AuRE fitted them; one starting value.
    intensity = entries["probe.intensity"]
    assert intensity["per"] == "measurement"
    assert intensity["range"] == [0.6, 1.4]
    assert intensity["value"] == pytest.approx(1.1070)  # the median of AuRE's
    # The broadening, at the value AuRE fitted, shared by the state's segments.
    broadening = entries["probe.sample_broadening"]
    assert (broadening["per"], broadening["value"]) == ("state", pytest.approx(0.0352))
    assert broadening["range"] == [0.0, 0.5]
    # What AuRE neither enabled nor fitted is not invented.
    assert "probe.theta_offset" not in entries
    assert "probe.background" not in entries
    assert "air.rho" not in entries and "Si.rho" not in entries


def test_bounds_the_model_declares_win_over_the_fits() -> None:
    layer = {**FILM["layers"][1], "roughness_min": 1.0, "roughness_max": 9.0}
    model = {**FILM, "layers": [FILM["layers"][0], layer]}

    spec = to_spec(model=model, sample="S", name="n", states=STATES, fit=FILM_FIT)

    ranges = {p["path"]: p["range"] for p in spec["parameters"] if "range" in p}
    assert ranges["Cu.roughness"] == [1.0, 9.0]


def test_a_name_the_stack_repeats_takes_no_bound_from_the_fit() -> None:
    """A fit names a parameter by its layer: "D2O interface" for a D2O above
    and below a membrane would land on one of them by chance."""
    model = {
        "substrate": {"name": "Si", "sld": 2.07, "roughness": 3.0},
        "layers": [
            {"name": "D2O", "sld": 6.36, "thickness": 20.0, "roughness": 3.0},
            {"name": "lipid", "sld": 0.2, "thickness": 40.0, "roughness": 4.0},
        ],
        "ambient": {"name": "D2O", "sld": 6.36},
        "back_reflection": False,
    }
    fit = {
        "parameters": {"D2O interface": 3.0, "lipid interface": 4.0},
        "bounds": {"D2O interface": [1.0, 9.0], "lipid interface": [1.0, 9.0]},
    }

    spec = to_spec(model=model, sample="S", name="n", states=STATES, fit=fit)

    paths = {p["path"] for p in spec["parameters"]}
    assert "lipid.roughness" in paths
    assert not any(path.startswith("D2O") for path in paths)


def test_an_intensity_per_state_stays_one_per_state() -> None:
    """Across states AuRE fits one intensity per state: no segment of its own."""
    fit = {
        "parameters": {"state0 intensity": 0.97},
        "bounds": {"state0 intensity": [0.7, 1.1]},
    }

    spec = to_spec(model=FILM, sample="S", name="n", states=STATES, fit=fit)

    [intensity] = [p for p in spec["parameters"] if p["path"] == "probe.intensity"]
    assert intensity == {
        "path": "probe.intensity",
        "value": 0.97,
        "per": "state",
        "range": [0.7, 1.1],
    }


def test_a_run_with_no_fit_recorded_still_gets_its_broadening() -> None:
    """The model says broadening was on, and in what range; where the fit is
    not there to say at what value, it starts from none."""
    spec = to_spec(model=FILM, sample="S", name="n", states=STATES)

    entries = {p["path"]: p for p in spec["parameters"]}
    assert entries["probe.sample_broadening"] == {
        "path": "probe.sample_broadening",
        "range": [0.0, 0.5],
        "per": "state",
    }
    assert entries["probe.intensity"]["per"] == "state"


def test_the_import_says_the_segments_start_from_one_value() -> None:
    notes = untranslatable(FILM, FILM_FIT)

    assert any(
        note.startswith("intensity: one per angle segment")
        and "1.013, 1.107, 1.371" in note
        for note in notes
    )
    assert untranslatable(FILM) == []


def test_a_value_aure_held_is_held_not_freed() -> None:
    """The reported failure: AuRE pinned SiO2's SLD with equal bounds (3.47 and
    3.47), the spec declared it free in an empty range, and `nrw model
    generate` refused the spec, so two of a beamtime's first looks failed. An
    inverted pair is swapped, as AuRE's builder does."""
    from nr_workbench.spec.models import ParameterSpec

    pinned = {**FILM["layers"][0], "sld": 3.47, "sld_min": 3.47, "sld_max": 3.47}
    inverted = {**FILM["layers"][1], "thickness_min": 3200.0, "thickness_max": 600.0}
    model = {**FILM, "layers": [pinned, inverted]}
    fit = {
        "parameters": {**FILM_FIT["parameters"], "SiO2 rho": 3.47},
        "bounds": {**FILM_FIT["bounds"], "SiO2 rho": [3.47, 3.47]},
    }

    spec = to_spec(model=model, sample="S", name="n", states=STATES, fit=fit)

    ranges = {p["path"]: p.get("range") for p in spec["parameters"]}
    assert "SiO2.rho" not in ranges  # held at the stack's 3.47
    assert spec["materials"]["SiO2"]["rho"] == 3.47
    assert ranges["Cu.thickness"] == [600.0, 3200.0]
    for entry in spec["parameters"]:
        ParameterSpec(**entry)  # each one a parameter a spec takes
