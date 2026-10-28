"""Tests for the model lifecycle: scan, new, fork, check drift, diff.

These close the loop between "the docs promise this command" and "the command
exists and does what the docs say" -- three of them were cited in error
messages and skills before they were written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from nr_workbench.cli import main

pytestmark = pytest.mark.integration


#: The angle each synthetic segment records, in degrees, as REF_L's header
#: does (in radians). What was measured, for this data -- not a default, and
#: the middle one is not the usual 1.2, so a test that sees it read the file.
SEGMENT_ANGLES = (0.45, 1.251, 3.5)


def write_partials(
    directory: Path, run: int, segments: int = 3, angles: tuple[float, ...] = ()
) -> None:
    """Write plausible REF_L partial files for one run, each recording its angle."""
    import math

    directory.mkdir(parents=True, exist_ok=True)
    angles = angles or SEGMENT_ANGLES
    for segment in range(1, segments + 1):
        meta = f'# Meta:{{"theta": {math.radians(angles[segment - 1])!r}}}\n'
        rows = "\n".join(
            f"{0.01 + 0.001 * i:.6f} {1e-3 / (i + 1):.6e} {1e-4:.6e} {2e-4:.6e}"
            for i in range(30)
        )
        (
            directory / f"REFL_{run}_{segment}_{run + segment - 1}_partial.txt"
        ).write_text(meta + rows + "\n", encoding="utf-8")


def write_slices(directory: Path, run: int, count: int = 6, step: int = 240) -> None:
    """Write time-binned tNR slices."""
    directory.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        rows = "\n".join(
            f"{0.01 + 0.001 * j:.6f} {1e-3 / (j + 1):.6e} {1e-4:.6e} {2e-4:.6e}"
            for j in range(30)
        )
        (directory / f"r{run}_t{i * step:06d}.txt").write_text(
            rows + "\n", encoding="utf-8"
        )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project with one sample carrying two steady runs and a tNR series."""
    root = tmp_path / "proj"
    result = CliRunner().invoke(main, ["init", str(root), "--sample", "S1"])
    assert result.exit_code == 0, result.output

    data = root / "samples" / "S1" / "data"
    write_partials(data / "steady", 100001)
    write_partials(data / "steady", 100005)
    write_slices(data / "tnr" / "100003", 100003)
    # The same run, summed, as the reduction also writes it: the slices carry
    # no header, so this is where the series' angle is recorded.
    write_partials(data / "steady", 100003, segments=1, angles=(0.6,))
    return root


def run(root: Path, monkeypatch: pytest.MonkeyPatch, *args: str):
    """Invoke the CLI with the project as the working directory."""
    monkeypatch.chdir(root)
    return CliRunner().invoke(main, list(args))


# --------------------------------------------------------------------------
# sample scan
# --------------------------------------------------------------------------


def test_scan_registers_what_is_on_disk(project: Path, monkeypatch) -> None:
    result = run(project, monkeypatch, "sample", "scan", "S1", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)[0]
    # 100003 too: the series' run, summed, as the reduction also writes it.
    assert payload["steady_runs"] == [100001, 100003, 100005]
    assert len(payload["series"]) == 1
    assert payload["series"][0]["n_slices"] == 6


def test_scan_writes_sample_yaml(project: Path, monkeypatch) -> None:
    import yaml

    run(project, monkeypatch, "sample", "scan", "S1")

    document = yaml.safe_load((project / "samples" / "S1" / "sample.yaml").read_text())
    assert document["schema"] == "nrw-sample/1"
    assert [entry["run"] for entry in document["steady"]] == [100001, 100003, 100005]


def test_scan_detects_a_uniform_time_step(project: Path, monkeypatch) -> None:
    """Knowing the step is what lets `model new` write a `select` block."""
    result = run(project, monkeypatch, "sample", "scan", "S1", "--json")

    series = json.loads(result.stdout)[0]["series"][0]
    assert series["t_step"] == 240.0
    assert series["t_start"] == 0.0


def test_scan_reports_data_the_prose_does_not_mention(
    project: Path, monkeypatch
) -> None:
    """The gap between sample.md and the disk is usually the interesting part."""
    result = run(project, monkeypatch, "sample", "scan", "S1", "--json")

    payload = json.loads(result.stdout)[0]
    assert set(payload["present_but_undocumented"]) == {100001, 100003, 100005}


def test_scan_reports_documented_runs_with_no_data(project: Path, monkeypatch) -> None:
    """A run written up but absent is usually one that failed."""
    sample_md = project / "samples" / "S1" / "sample.md"
    sample_md.write_text(
        sample_md.read_text(encoding="utf-8") + "\n| 100001 | full Q | OCV |\n"
        "| 999999 | full Q | never arrived |\n",
        encoding="utf-8",
    )

    result = run(project, monkeypatch, "sample", "scan", "S1", "--json")

    payload = json.loads(result.stdout)[0]
    assert payload["documented_but_absent"] == [999999]


def test_scan_ignores_runs_in_the_templates_commented_example(
    project: Path, monkeypatch
) -> None:
    """The shipped sample.md has a worked example inside an HTML comment.

    Counting those would report every new sample as documenting runs it has
    never had.
    """
    result = run(project, monkeypatch, "sample", "scan", "S1", "--json")

    assert json.loads(result.stdout)[0]["documented_but_absent"] == []


def test_scan_with_no_write_leaves_the_register_alone(
    project: Path, monkeypatch
) -> None:
    register = project / "samples" / "S1" / "sample.yaml"
    before = register.read_text(encoding="utf-8")

    run(project, monkeypatch, "sample", "scan", "S1", "--no-write")

    assert register.read_text(encoding="utf-8") == before


# --------------------------------------------------------------------------
# model new
# --------------------------------------------------------------------------


def test_model_new_produces_a_spec_that_validates(project: Path, monkeypatch) -> None:
    """A scaffold that fails on first contact teaches the pattern backwards."""
    created = run(project, monkeypatch, "model", "new", "S1", "--name", "m")
    assert created.exit_code == 0, created.output

    checked = run(project, monkeypatch, "model", "validate", "samples/S1/models/m.yaml")

    assert checked.exit_code == 0, checked.output


@pytest.mark.parametrize("name", ["../x", "a/b", "x\n"])
def test_model_new_refuses_a_name_that_is_a_path(
    project: Path, monkeypatch, name: str
) -> None:
    before = sorted(p for p in (project / "samples").rglob("*") if p.is_file())

    result = run(project, monkeypatch, "model", "new", "S1", "--name", name)

    assert result.exit_code != 0
    assert "plain name" in result.output
    assert sorted(p for p in (project / "samples").rglob("*") if p.is_file()) == before


def test_a_new_spec_says_nothing_about_how_it_is_fitted(
    project: Path, monkeypatch
) -> None:
    """Its `fit:` block was never read, yet said `method: amoeba` in every spec:
    how a model is fitted is nrw.toml's, or the command line's."""
    import yaml

    run(project, monkeypatch, "model", "new", "S1", "--name", "m")
    spec = project / "samples" / "S1" / "models" / "m.yaml"

    assert "fit" not in yaml.safe_load(spec.read_text(encoding="utf-8"))


def test_validate_says_a_specs_fit_block_is_not_read(
    project: Path, monkeypatch
) -> None:
    run(project, monkeypatch, "model", "new", "S1", "--name", "m")
    spec = project / "samples" / "S1" / "models" / "m.yaml"
    with spec.open("a", encoding="utf-8") as handle:
        handle.write("fit: {method: amoeba, steps: 1000}\n")

    result = run(project, monkeypatch, "model", "validate", str(spec))

    assert result.exit_code == 0, result.output
    assert "`fit:` block is not read" in result.output


def test_model_generate_leaves_a_script_that_would_not_change_as_it_is(
    project: Path, monkeypatch
) -> None:
    # Restamped, an unchanged script would hash differently, and a fit of it
    # would no longer be known for the identical run it is.
    run(project, monkeypatch, "model", "new", "S1", "--name", "m")
    spec = project / "samples" / "S1" / "models" / "m.yaml"
    assert run(project, monkeypatch, "model", "generate", str(spec)).exit_code == 0
    script = spec.with_suffix(".py")
    first = script.read_bytes()

    again = run(project, monkeypatch, "model", "generate", str(spec))
    spec.write_text(spec.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")
    edited = run(project, monkeypatch, "model", "generate", str(spec))

    assert again.exit_code == 0, again.output
    assert "up to date with its spec" in again.output
    assert edited.exit_code == 0, edited.output
    assert "Wrote samples/S1/models/m.py" in edited.output
    assert script.read_bytes() != first


def test_model_new_writes_no_angles_when_every_file_records_its_own(
    project: Path, monkeypatch
) -> None:
    """Each is read from its file when the spec is resolved: no copy to go stale."""
    run(project, monkeypatch, "model", "new", "S1", "--name", "m")
    text = (project / "samples/S1/models/m.yaml").read_text(encoding="utf-8")
    document = yaml.safe_load(text)

    assert [state.get("thetas") for state in document["states"]] == [None, None]
    assert "theta" not in document["series"][0]
    assert "BLANK" not in text


def fresh_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    result = CliRunner().invoke(main, ["init", str(root), "--sample", "S1"])
    assert result.exit_code == 0, result.output
    return root


def test_model_new_leaves_a_blank_for_a_file_that_records_no_angle(
    tmp_path: Path, monkeypatch
) -> None:
    """Never a guess: the spec is refused until a person gives the angle."""
    root = fresh_project(tmp_path)
    steady = root / "samples/S1/data/steady"
    write_partials(steady, 100001)
    unrecorded = steady / "REFL_100001_2_100002_partial.txt"
    unrecorded.write_text(
        "".join(line for line in unrecorded.read_text().splitlines(True)[1:]),
        encoding="utf-8",
    )

    created = run(root, monkeypatch, "model", "new", "S1", "--name", "m")
    spec_path = root / "samples/S1/models/m.yaml"
    text = spec_path.read_text(encoding="utf-8")
    refused = run(root, monkeypatch, "model", "validate", "samples/S1/models/m.yaml")

    assert created.exit_code == 0, created.output
    assert f"no incident angle recorded for: {unrecorded.name}" in created.output
    assert f"#   {unrecorded.name}" in text
    assert yaml.safe_load(text)["states"][0]["thetas"] == [0.45, None, 3.5]
    assert refused.exit_code != 0
    assert unrecorded.name in refused.output

    document = yaml.safe_load(text)
    document["states"][0]["thetas"][1] = 1.2  # a person fills in the blank
    spec_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    filled = run(root, monkeypatch, "model", "validate", "samples/S1/models/m.yaml")
    assert filled.exit_code == 0, filled.output


def test_model_new_leaves_a_series_angle_blank_when_nothing_records_it(
    tmp_path: Path, monkeypatch
) -> None:
    """Slices carry no header; with no summed dataset, nothing on disk says."""
    root = fresh_project(tmp_path)
    data = root / "samples/S1/data"
    write_partials(data / "steady", 100001)
    write_partials(data / "steady", 100005)
    write_slices(data / "tnr" / "100003", 100003)

    created = run(root, monkeypatch, "model", "new", "S1", "--name", "m")
    text = (root / "samples/S1/models/m.yaml").read_text(encoding="utf-8")
    refused = run(root, monkeypatch, "model", "validate", "samples/S1/models/m.yaml")

    assert created.exit_code == 0, created.output
    assert yaml.safe_load(text)["series"][0]["theta"] is None
    assert "#   100003 (series" in text
    assert refused.exit_code != 0
    assert "no file of run 100003 in samples/S1/data/steady" in refused.output


def test_the_generated_fit_uses_the_angle_each_file_records(
    project: Path, monkeypatch
) -> None:
    """The last place a wrong angle could come from: the probe the fit builds."""
    import re

    run(project, monkeypatch, "model", "new", "S1", "--name", "m")
    generated = run(
        project, monkeypatch, "model", "generate", "samples/S1/models/m.yaml"
    )
    script = (project / "samples/S1/models/m.py").read_text(encoding="utf-8")

    assert generated.exit_code == 0, generated.output
    angles = {
        Path(name).name: float(theta)
        for name, theta in re.findall(
            r"create_probe\(PROJECT_ROOT / '([^']+)', ([\d.]+)", script
        )
    }
    assert angles["REFL_100001_2_100002_partial.txt"] == pytest.approx(1.251)
    assert angles["r100003_t000000.txt"] == pytest.approx(0.6)


def test_model_new_keeps_the_segments_sample_yaml_lists(
    project: Path, monkeypatch
) -> None:
    """The register is where a person says "only these"; `auto` would read all."""
    run(project, monkeypatch, "sample", "scan", "S1")
    register = project / "samples/S1/sample.yaml"
    document = yaml.safe_load(register.read_text(encoding="utf-8"))
    entry = next(e for e in document["steady"] if e["run"] == 100001)
    entry["segments"] = entry["segments"][:2]
    register.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    run(project, monkeypatch, "model", "new", "S1", "--name", "m")
    spec_path = project / "samples/S1/models/m.yaml"
    state = next(
        s
        for s in yaml.safe_load(spec_path.read_text(encoding="utf-8"))["states"]
        if s["run"] == 100001
    )

    from nr_workbench.spec.models import load_spec
    from nr_workbench.spec.resolve import discover_measurements

    assert state["segments"] != "auto"
    resolved = discover_measurements(load_spec(spec_path), project)[state["name"]]
    assert [m.theta for m in resolved] == [pytest.approx(0.45), pytest.approx(1.251)]


@pytest.mark.parametrize("recorded", [True, False], ids=["recorded", "blank"])
def test_a_combined_run_is_scaffolded_like_any_other(
    tmp_path: Path, recorded: bool
) -> None:
    import math

    from nr_workbench.commands.model import state_for_run
    from nr_workbench.project.scan import SteadyMeasurement

    steady = tmp_path / "samples/S1/data/steady"
    steady.mkdir(parents=True)
    name = "REFL_100001_combined_data_auto.txt"
    meta = f'# Meta:{{"theta": {math.radians(0.5)!r}}}\n' if recorded else ""
    (steady / name).write_text(meta + "0.01 1.0 0.1 0.001\n", encoding="utf-8")

    block, _, blank = state_for_run(
        tmp_path,
        SteadyMeasurement(run=100001, combined=f"samples/S1/data/steady/{name}"),
    )

    if recorded:
        assert "thetas" not in block and blank == []
    else:
        assert block["thetas"] == [None] and blank == [name]


@pytest.mark.parametrize("breaker", ["\n", "\r", "\x85", "\u2028", "\u2029"], ids=repr)
def test_a_directory_name_cannot_write_keys_into_the_spec(
    tmp_path: Path, monkeypatch, breaker: str
) -> None:
    """Each ends a YAML comment; a name carrying one reaches the blank-angle note."""
    root = fresh_project(tmp_path)
    data = root / "samples/S1/data"
    write_partials(data / "steady", 100001)
    hostile = f"x{breaker}post_build: \"print('run')\"{breaker}#"
    write_slices(data / "tnr" / hostile, 999999)

    created = run(root, monkeypatch, "model", "new", "S1", "--name", "m")
    text = (root / "samples/S1/models/m.yaml").read_text(encoding="utf-8")

    from nr_workbench.spec.models import load_spec

    assert created.exit_code == 0, created.output
    assert "post_build" not in yaml.safe_load(text)
    assert load_spec(root / "samples/S1/models/m.yaml").post_build is None


def test_model_new_produces_a_spec_that_generates(project: Path, monkeypatch) -> None:
    run(project, monkeypatch, "model", "new", "S1", "--name", "m")

    generated = run(
        project, monkeypatch, "model", "generate", "samples/S1/models/m.yaml"
    )

    assert generated.exit_code == 0, generated.output
    assert (project / "samples" / "S1" / "models" / "m.py").is_file()


def test_model_new_picks_up_both_states_and_the_series(
    project: Path, monkeypatch
) -> None:
    import yaml

    run(project, monkeypatch, "model", "new", "S1", "--name", "m")

    spec = yaml.safe_load(
        (project / "samples" / "S1" / "models" / "m.yaml").read_text()
    )
    assert len(spec["states"]) == 2
    assert len(spec["series"]) == 1
    assert spec["constraints"][0]["form"] == "linear_in_time"


def test_model_new_refuses_to_clobber(project: Path, monkeypatch) -> None:
    run(project, monkeypatch, "model", "new", "S1", "--name", "m")

    again = run(project, monkeypatch, "model", "new", "S1", "--name", "m")

    assert again.exit_code != 0
    assert "already exists" in again.output


def test_model_new_on_a_sample_with_no_data_says_so(project: Path, monkeypatch) -> None:
    run(project, monkeypatch, "sample", "new", "Empty")

    result = run(project, monkeypatch, "model", "new", "Empty", "--name", "m")

    assert result.exit_code != 0
    assert "No data found" in result.output


# --------------------------------------------------------------------------
# model fork
# --------------------------------------------------------------------------


@pytest.fixture
def generated(project: Path, monkeypatch) -> Path:
    """A project with a generated script."""
    run(project, monkeypatch, "model", "new", "S1", "--name", "m")
    result = run(project, monkeypatch, "model", "generate", "samples/S1/models/m.yaml")
    assert result.exit_code == 0, result.output
    return project


def test_fork_creates_a_hand_owned_copy(generated: Path, monkeypatch) -> None:
    result = run(
        generated,
        monkeypatch,
        "model",
        "fork",
        "samples/S1/models/m.yaml",
        "--name",
        "mine",
    )

    assert result.exit_code == 0, result.output
    fork = generated / "samples" / "S1" / "models" / "mine.py"
    assert fork.is_file()
    source = fork.read_text(encoding="utf-8")
    assert "HAND-OWNED SCRIPT" in source
    assert "GENERATED BY nr-workbench" not in source


def test_fork_records_where_it_came_from(generated: Path, monkeypatch) -> None:
    """Escaping the generator must not mean escaping provenance."""
    run(
        generated,
        monkeypatch,
        "model",
        "fork",
        "samples/S1/models/m.yaml",
        "--name",
        "mine",
    )

    source = (generated / "samples" / "S1" / "models" / "mine.py").read_text(
        encoding="utf-8"
    )

    assert "forked from: samples/S1/models/m.yaml" in source
    assert "spec sha256:" in source


def test_a_fork_still_runs(generated: Path, monkeypatch) -> None:
    """A fork that does not execute would be a trap, not an escape hatch."""
    import subprocess
    import sys

    run(
        generated,
        monkeypatch,
        "model",
        "fork",
        "samples/S1/models/m.yaml",
        "--name",
        "mine",
    )
    fork = generated / "samples" / "S1" / "models" / "mine.py"

    result = subprocess.run(
        [sys.executable, str(fork)], capture_output=True, text=True, timeout=600
    )

    assert result.returncode == 0, result.stderr[-1500:]


def test_fork_requires_a_generated_script_first(project: Path, monkeypatch) -> None:
    run(project, monkeypatch, "model", "new", "S1", "--name", "m")

    result = run(project, monkeypatch, "model", "fork", "samples/S1/models/m.yaml")

    assert result.exit_code != 0
    assert "model generate" in result.output


def test_fork_will_not_overwrite(generated: Path, monkeypatch) -> None:
    run(
        generated,
        monkeypatch,
        "model",
        "fork",
        "samples/S1/models/m.yaml",
        "--name",
        "mine",
    )

    again = run(
        generated,
        monkeypatch,
        "model",
        "fork",
        "samples/S1/models/m.yaml",
        "--name",
        "mine",
    )

    assert again.exit_code != 0


# --------------------------------------------------------------------------
# check: script drift
# --------------------------------------------------------------------------


def test_check_passes_on_a_freshly_generated_script(
    generated: Path, monkeypatch
) -> None:
    result = run(generated, monkeypatch, "check")

    assert result.exit_code == 0, result.output


def test_check_detects_a_hand_edited_script(generated: Path, monkeypatch) -> None:
    """The unsupported way to take control; `nrw model fork` is the supported one."""
    script = generated / "samples" / "S1" / "models" / "m.py"
    script.write_text(
        script.read_text(encoding="utf-8") + "\n# sneaky\n", encoding="utf-8"
    )

    result = run(generated, monkeypatch, "check", "--json")

    assert result.exit_code == 1
    kinds = {p["kind"] for p in json.loads(result.stdout)["problems"]}
    assert "hand-edited-script" in kinds


def test_check_detects_a_script_older_than_its_spec(
    generated: Path, monkeypatch
) -> None:
    spec = generated / "samples" / "S1" / "models" / "m.yaml"
    spec.write_text(
        spec.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8"
    )

    result = run(generated, monkeypatch, "check", "--json")

    assert result.exit_code == 1
    kinds = {p["kind"] for p in json.loads(result.stdout)["problems"]}
    assert "stale-script" in kinds


def test_check_leaves_forks_alone(generated: Path, monkeypatch) -> None:
    """A fork is hand-owned by design; flagging it would make `check` noise."""
    run(
        generated,
        monkeypatch,
        "model",
        "fork",
        "samples/S1/models/m.yaml",
        "--name",
        "mine",
    )
    fork = generated / "samples" / "S1" / "models" / "mine.py"
    fork.write_text(
        fork.read_text(encoding="utf-8") + "\n# my own edit\n", encoding="utf-8"
    )

    result = run(generated, monkeypatch, "check")

    assert result.exit_code == 0, result.output


def test_check_leaves_plain_hand_written_scripts_alone(
    generated: Path, monkeypatch
) -> None:
    """Running a hand-written script unchanged is the adoption path.

    Flagging every such file would make `check` useless for exactly the case
    the package promises to support. Only files that claim to be generated are
    policed.
    """
    (generated / "samples" / "S1" / "models" / "handwritten.py").write_text(
        "problem = None\n", encoding="utf-8"
    )

    result = run(generated, monkeypatch, "check")

    assert result.exit_code == 0, result.output


def test_check_flags_a_generated_script_whose_spec_is_gone(
    generated: Path, monkeypatch
) -> None:
    """This one *is* orphaned: it claims a spec that no longer exists."""
    (generated / "samples" / "S1" / "models" / "m.yaml").unlink()

    result = run(generated, monkeypatch, "check", "--json")

    assert result.exit_code == 1
    kinds = {p["kind"] for p in json.loads(result.stdout)["problems"]}
    assert "missing-spec" in kinds


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------


@pytest.fixture
def two_fits(generated: Path, monkeypatch) -> tuple[Path, str, str]:
    """A project with two fits differing only in optimizer settings."""
    script = "samples/S1/models/m.py"
    first = run(
        generated,
        monkeypatch,
        "fit",
        "run",
        script,
        "--method",
        "amoeba",
        "--steps",
        "8",
        "--seed",
        "1",
        # One CPU: a pool on every core starves the browser tests on the
        # other xdist workers, as every other module's fits do.
        "--parallel",
        "1",
    )
    assert first.exit_code == 0, first.output
    second = run(
        generated,
        monkeypatch,
        "fit",
        "run",
        script,
        "--method",
        "amoeba",
        "--steps",
        "14",
        "--seed",
        "1",
        # One CPU: a pool on every core starves the browser tests on the
        # other xdist workers, as every other module's fits do.
        "--parallel",
        "1",
    )
    assert second.exit_code == 0, second.output

    listed = run(generated, monkeypatch, "ls", "--json")
    rows = json.loads(listed.stdout)
    return generated, rows[1]["fit_id"], rows[0]["fit_id"]


def test_diff_attributes_a_settings_change(two_fits, monkeypatch) -> None:
    root, a, b = two_fits

    result = run(root, monkeypatch, "diff", a, b, "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["changed"]["settings"] is True
    assert payload["changed"]["inputs"] is False
    assert payload["changed"]["script"] is False
    assert "fit settings only" in payload["verdict"]


def test_diff_does_not_call_a_model_edit_a_data_change(two_fits, monkeypatch) -> None:
    """The script is one of the recorded inputs, so `inputs_digest` moves when
    the model is edited. Reading that as "the data changed" inverts the one
    distinction this command exists to draw -- it would tell you a comparison
    is invalid at exactly the moment it is most valid.
    """
    root, a, _ = two_fits
    script = root / "samples" / "S1" / "models" / "m.py"
    script.write_text(
        script.read_text(encoding="utf-8") + "\n# a comment\n", encoding="utf-8"
    )
    later = run(
        root,
        monkeypatch,
        "fit",
        "run",
        "samples/S1/models/m.py",
        "--method",
        "amoeba",
        "--steps",
        "8",
        "--seed",
        "1",
        # One CPU: a pool on every core starves the browser tests on the
        # other xdist workers, as every other module's fits do.
        "--parallel",
        "1",
    )
    assert later.exit_code == 0, later.output
    b = json.loads(run(root, monkeypatch, "ls", "--json").stdout)[0]["fit_id"]

    payload = json.loads(run(root, monkeypatch, "diff", a, b, "--json").stdout)

    assert payload["changed"]["script"] is True
    assert payload["changed"]["data"] is False
    assert "DATA changed" not in payload["verdict"]
    assert "attributable to the model" in payload["verdict"]


def test_ls_says_what_each_fit_was_and_what_changed(two_fits, monkeypatch) -> None:
    """A fit id identifies a run but does not describe it, and by the tenth
    row the listing is a wall of hashes."""
    root, _, _ = two_fits

    rows = json.loads(run(root, monkeypatch, "ls", "--json").stdout)

    assert [r["change"] for r in rows][-1] == "first run of this model"
    assert "steps 8 -> 14" in rows[0]["change"], rows[0]["change"]
    assert rows[0]["compared_to"] == rows[1]["fit_id"]

    text = run(root, monkeypatch, "ls").output
    assert "steps 8 -> 14" in text, text


def test_ls_shows_the_note_a_fit_was_run_with(project: Path, monkeypatch) -> None:
    """The scientist's own words beat anything generated."""
    run(project, monkeypatch, "model", "new", "S1", "--name", "m")
    run(project, monkeypatch, "model", "generate", "samples/S1/models/m.yaml")
    run(
        project,
        monkeypatch,
        "fit",
        "run",
        "samples/S1/models/m.py",
        "--method",
        "amoeba",
        "--steps",
        "6",
        "--note",
        "oxide freed",
        "--parallel",
        "1",
    )

    text = run(project, monkeypatch, "ls").output

    assert "oxide freed" in text


def test_diff_calls_out_a_data_change_above_everything_else(
    two_fits, monkeypatch
) -> None:
    """The distinction that matters: a better chi-squared on different data
    is not an improvement, and the verdict has to say so."""
    root, a, b = two_fits
    data = root / "samples" / "S1" / "data" / "steady"
    target = next(data.glob("*.txt"))
    target.write_text(
        target.read_text(encoding="utf-8") + "0.2 1e-6 1e-7 2e-4\n", encoding="utf-8"
    )
    third = run(
        root,
        monkeypatch,
        "fit",
        "run",
        "samples/S1/models/m.py",
        "--method",
        "amoeba",
        "--steps",
        "8",
        "--seed",
        "1",
        # One CPU: a pool on every core starves the browser tests on the
        # other xdist workers, as every other module's fits do.
        "--parallel",
        "1",
    )
    assert third.exit_code == 0, third.output
    c = json.loads(run(root, monkeypatch, "ls", "--json").stdout)[0]["fit_id"]

    result = run(root, monkeypatch, "diff", a, c, "--json")

    payload = json.loads(result.stdout)
    assert payload["changed"]["inputs"] is True
    assert "DATA changed" in payload["verdict"]


def test_diff_reports_identical_runs_as_replicates(two_fits, monkeypatch) -> None:
    root, a, _ = two_fits
    again = run(
        root,
        monkeypatch,
        "fit",
        "run",
        "samples/S1/models/m.py",
        "--method",
        "amoeba",
        "--steps",
        "8",
        "--seed",
        "1",
        # One CPU: a pool on every core starves the browser tests on the
        # other xdist workers, as every other module's fits do.
        "--parallel",
        "1",
    )
    assert again.exit_code == 0, again.output
    replicate = json.loads(run(root, monkeypatch, "ls", "--json").stdout)[0]["fit_id"]

    result = run(root, monkeypatch, "diff", a, replicate, "--json")

    assert "replicates" in json.loads(result.stdout)["verdict"]


def test_diff_rejects_an_unknown_fit(two_fits, monkeypatch) -> None:
    root, a, _ = two_fits

    result = run(root, monkeypatch, "diff", a, "nonesuch")

    assert result.exit_code != 0
    assert "No fit matching" in result.output


def test_check_finds_a_result_directory_the_index_never_recorded(
    two_fits, monkeypatch
) -> None:
    """`commands/fit.py` only catches FitError; a kill, an OOM or a full disk
    during an hour-long DREAM run leaves a directory with a script, inputs and
    an environment but no index line -- invisible to ls, whence and check. That
    happened five times in twenty-five in the first real beamtime, and one
    orphan held a 298 MB posterior nothing could find.
    """
    root, fit_id, _ = two_fits
    orphan = root / "samples" / "S1" / "results" / "20260101-000000Z-deadbeef"
    (orphan / "fit").mkdir(parents=True)
    (orphan / "fit" / "big.mc.gz").write_bytes(b"a posterior nobody can see")

    result = run(root, monkeypatch, "check", "--json")

    problems = json.loads(result.stdout)["problems"]
    orphans = [p for p in problems if p["kind"] == "interrupted-run"]
    assert [p["fit_id"] for p in orphans] == ["20260101-000000Z-deadbeef"]
    assert "interrupted" in orphans[0]["detail"]


def test_a_fit_records_a_manifest_before_it_starts(two_fits, monkeypatch) -> None:
    """So an interruption leaves something findable rather than nothing."""
    root, fit_id, _ = two_fits
    manifest = json.loads(
        (root / "samples" / "S1" / "results" / fit_id / "manifest.json").read_text()
    )

    assert manifest["status"] == "ok", "a completed fit is not left as running"

    import inspect

    from nr_workbench.commands import fit as fit_module

    source = inspect.getsource(fit_module.run_fit_command)
    before, _, after = source.partition("outcome = run_fit(")
    assert 'record.status = "running"' in before
    assert "write_manifest" in before, "the manifest is written before the fit"
    assert 'record.status = "ok"' in after


# --------------------------------------------------------------------------
# The dQ width convention reaches the spec from the files
#
# `probe.dq_is_fwhm` used to be hardcoded True at scaffold time. FWHM is what
# every reduction has written so far, and it is expected to change to sigma --
# at which point a hardcoded True scales every resolution by 2.355 and the fit
# absorbs it into roughness rather than raising.
# --------------------------------------------------------------------------

COLUMNS = "# Q [1/Angstrom]  R  dR  dQ [{label}]\n"


def write_partials_labelled(directory: Path, run: int, label: str, segments: int = 3):
    """Partials whose column-title line states the dQ width convention."""
    write_partials(directory, run, segments)
    for segment in range(1, segments + 1):
        path = directory / f"REFL_{run}_{segment}_{run + segment - 1}_partial.txt"
        path.write_text(
            COLUMNS.format(label=label) + path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )


def test_model_new_reads_sigma_out_of_the_headers(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "proj"
    assert (
        CliRunner().invoke(main, ["init", str(root), "--sample", "S1"]).exit_code == 0
    )
    write_partials_labelled(root / "samples/S1/data/steady", 100001, "sigma")

    result = run(root, monkeypatch, "model", "new", "S1", "--name", "m")

    assert result.exit_code == 0, result.output
    spec = yaml.safe_load((root / "samples/S1/models/m.yaml").read_text())
    assert spec["probe"]["dq_is_fwhm"] is False


def test_model_new_reads_fwhm_out_of_the_headers(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "proj"
    assert (
        CliRunner().invoke(main, ["init", str(root), "--sample", "S1"]).exit_code == 0
    )
    write_partials_labelled(root / "samples/S1/data/steady", 100001, "FWHM")

    result = run(root, monkeypatch, "model", "new", "S1", "--name", "m")

    assert result.exit_code == 0, result.output
    spec = yaml.safe_load((root / "samples/S1/models/m.yaml").read_text())
    assert spec["probe"]["dq_is_fwhm"] is True


def test_model_new_refuses_a_set_that_mixes_conventions(
    tmp_path: Path, monkeypatch
) -> None:
    """One boolean cannot describe two conventions; splitting is the scientist's call."""
    root = tmp_path / "proj"
    assert (
        CliRunner().invoke(main, ["init", str(root), "--sample", "S1"]).exit_code == 0
    )
    steady = root / "samples/S1/data/steady"
    write_partials_labelled(steady, 100001, "FWHM")
    write_partials_labelled(steady, 100005, "sigma")

    result = run(root, monkeypatch, "model", "new", "S1", "--name", "m")

    assert result.exit_code != 0
    assert "do not share a dQ convention" in result.output
    assert not (root / "samples/S1/models/m.yaml").exists()


def test_model_new_says_so_when_no_file_states_the_convention(
    project: Path, monkeypatch
) -> None:
    """The fixture's partials carry no header. Assuming is fine; silence is not."""
    result = run(project, monkeypatch, "model", "new", "S1", "--name", "m")

    assert result.exit_code == 0, result.output
    assert "FWHM or sigma" in result.output
    spec = yaml.safe_load((project / "samples/S1/models/m.yaml").read_text())
    assert spec["probe"]["dq_is_fwhm"] is True
