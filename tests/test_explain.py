"""The generated model explanation.

Derived from the resolved table, never authored, so it cannot describe a model
other than the one that will be fitted. That is the property worth testing: a
hand-written explanation drifts the first time someone edits one and not the
other.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from nr_workbench.spec.explain import explain
from nr_workbench.spec.models import load_spec
from nr_workbench.spec.resolve import build_table, discover_measurements

FIXED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

SPEC = """\
schema: nrw-model/1
name: demo
sample: S1
description: A copper film in D2O.
materials:
  D2O: {rho: 6.36}
  CuOx: {rho: 5.0}
  Cu: {rho: 6.55}
  Si: {rho: 2.07}
stack:
  - {name: D2O, material: D2O, thickness: 0, roughness: 5}
  - {name: CuOx, material: CuOx, thickness: 20, roughness: 5}
  - {name: Cu, material: Cu, thickness: 500, roughness: 5}
  - {name: Si, material: Si}
probe: {resolution: angular_only, dq_is_fwhm: true}
states:
  - name: ocv1
    run: 100001
    segments: auto
    thetas: [0.45, 1.201]
    data_dir: samples/S1/data/steady
parameters:
  - {path: Cu.rho, range: [5.0, 7.0], per: model}
  - {path: Cu.thickness, range: [400, 600], per: state}
  - {path: probe.intensity, value: 1.0, pm: 0.1, per: state}
  - {path: probe.theta_offset, range: [-0.02, 0.02], per: state}
fit: {method: amoeba, steps: 100}
"""


def build(project: Path, source: str = SPEC):
    """Write data and a spec, and resolve it."""
    import numpy as np

    steady = project / "samples" / "S1" / "data" / "steady"
    steady.mkdir(parents=True, exist_ok=True)
    q = np.linspace(0.01, 0.2, 30)
    r = 1e-3 * (0.01 / q) ** 4
    body = "\n".join(
        f"{a:.6e} {b:.6e} {c:.6e} {d:.6e}"
        for a, b, c, d in zip(q, r, 0.05 * r, 0.02 * q, strict=True)
    )
    for segment in (1, 2):
        (steady / f"REFL_100001_{segment}_10000{segment}_partial.txt").write_text(body)

    spec_path = project / "samples" / "S1" / "models" / "demo.yaml"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(source, encoding="utf-8")
    spec = load_spec(spec_path)
    return spec_path, build_table(spec, discover_measurements(spec, project))


@pytest.fixture
def document(project: Path) -> str:
    spec_path, table = build(project)
    return explain(table, spec_path=Path("samples/S1/models/demo.yaml"), now=FIXED)


def test_it_says_what_is_shared_and_at_what_scope(document: str) -> None:
    """The question a reader actually has: what is tied to what."""
    assert "Shared across everything" in document
    assert "`Cu.rho@model`" in document
    assert "One per state" in document
    assert "`Cu.thickness@ocv1`" in document


def test_it_explains_each_nuisance_parameter_and_what_it_absorbs(
    document: str,
) -> None:
    """A reader has to know why probe.theta_offset is in the model at all."""
    assert "probe.theta_offset" in document
    assert "sample alignment" in document
    assert "pushes the error into a layer thickness" in document


def test_it_names_the_nuisance_parameters_that_are_absent(document: str) -> None:
    """Not fitting one is an assumption, and assumptions belong in the list."""
    assert "Not fitted:" in document
    assert "`probe.sample_broadening`" in document


def test_it_lists_the_assumptions_including_the_resolution_convention(
    document: str,
) -> None:
    """Fits under different resolution conventions are not comparable."""
    assert "## Assumptions" in document
    assert "Angular-only resolution" in document
    assert "dL = 0" in document or "`dL = 0`" in document
    assert "FWHM" in document


def test_it_flags_layers_below_the_resolution_limit(document: str) -> None:
    """A 20 A oxide's SLD and thickness are not separately determined."""
    assert "resolution limit" in document
    assert "`CuOx` (20 Å)" in document
    assert "thin-layer-degeneracy" in document


def test_it_names_the_unfitted_materials(document: str) -> None:
    """An SLD taken from literature is an assumption, not a measurement."""
    assert "taken from literature, not fitted" in document
    assert "`Si` = 2.07" in document


def test_it_reports_the_measured_angles_not_the_nominal_ones(document: str) -> None:
    assert "1.201°" in document
    assert "`# Meta:` header" in document


def test_it_reports_the_degrees_of_freedom(document: str) -> None:
    assert "## Degrees of freedom" in document
    assert "free parameters" in document


def test_it_carries_the_spec_hash_for_staleness_detection(project: Path) -> None:
    """`nrw check` compares this against the spec on disk."""
    spec_path, table = build(project)
    text = explain(table, spec_path=Path("x.yaml"), spec_sha256="a" * 64, now=FIXED)

    assert "spec sha256: " + "a" * 64 in text
    assert "nrw:spec" in text


def test_it_says_which_end_of_the_stack_the_beam_enters(document: str) -> None:
    """It said "Ambient first, substrate last" for every model, which is back
    reflection only. Now it reads the order: this stack ends in Si."""
    assert "The beam enters through Si, the last entry" in document
    assert "ambient first" not in document.lower()


def test_it_says_not_to_edit_it(document: str) -> None:
    """It is derived. An edit would be silently overwritten."""
    assert "DO NOT EDIT" in document
    assert "Edit the spec, not this" in document


def test_a_constraint_is_described_in_english() -> None:
    """`linear_in_time` asserts something specific and should say it.

    In particular that it interpolates in *time*, not slice index -- the tNR
    intervals are unequally spaced, so the two genuinely differ.
    """
    from nr_workbench.spec.explain import _form_prose

    class Constraint:
        form = "linear_in_time"
        from_ = "ocv1"
        to = "ocv2"

    prose = _form_prose(Constraint())

    assert "linearly **in time**" in prose
    assert "`ocv1`" in prose and "`ocv2`" in prose
    assert "not equally spaced" in prose


def test_generate_writes_the_explanation_beside_the_script(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It must exist without being asked for, or nobody will have one."""
    from click.testing import CliRunner

    from nr_workbench.cli import main

    build(project)
    monkeypatch.chdir(project)

    result = CliRunner().invoke(
        main, ["model", "generate", "samples/S1/models/demo.yaml"]
    )

    assert result.exit_code == 0, result.output
    notes = project / "samples/S1/models/demo.md"
    assert notes.is_file()
    assert "## Assumptions" in notes.read_text(encoding="utf-8")


def test_it_matches_the_spec_it_was_generated_from(project: Path) -> None:
    """The whole point: it describes the model that will be fitted.

    Cross-checked against the spec rather than against another copy of the
    generator, so a change to either shows up.
    """
    spec_path, table = build(project)
    document = explain(table, spec_path=Path("x.yaml"), now=FIXED)
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))

    for layer in spec["stack"]:
        assert f"`{layer['name']}`" in document
    for parameter in spec["parameters"]:
        head = parameter["path"]
        assert head.split(".")[0] in document, f"{head} is not described"
