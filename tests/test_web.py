"""The web layer: ProjectData without Flask, then Flask over it.

The split matters enough to test directly. :class:`ProjectData` must stay
importable and usable with no web framework present, because that is what keeps
it testable and what stops the route handlers from accumulating analysis --
the failure mode this design exists to avoid.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

from nr_workbench.web.project import ProjectData, _numbered
from nr_workbench.web.prose import render, strip_comments
from nr_workbench.web.readers import (
    DataFormatError,
    read_amplitude_txt,
    read_profile_dat,
    read_reduced,
)

from . import tnr_fixture


def write_reduced(path: Path, *, n: int = 40, scale: float = 1.0) -> None:
    """Write a four-column reduced file with a plausible decay."""
    q = np.linspace(0.01, 0.2, n)
    r = scale * 1e-3 * (0.01 / q) ** 4
    dr = 0.05 * r
    dq = 0.02 * q
    path.write_text(
        "\n".join(
            f"{a:.6e} {b:.6e} {c:.6e} {d:.6e}"
            for a, b, c, d in zip(q, r, dr, dq, strict=True)
        ),
        encoding="utf-8",
    )


@pytest.fixture
def populated(project: Path) -> Path:
    """The scaffolded project, plus two steady runs and a tNR series."""
    steady = project / "samples" / "Sample1" / "data" / "steady"
    steady.mkdir(parents=True, exist_ok=True)
    for segment in (1, 2, 3):
        write_reduced(
            steady / f"REFL_100001_{segment}_10000{segment}_partial.txt",
            scale=1.0 + segment / 10,
        )
    write_reduced(steady / "REFL_100002_combined_data_auto.txt")

    series = project / "samples" / "Sample1" / "data" / "tnr" / "100003"
    series.mkdir(parents=True, exist_ok=True)
    tnr_fixture.build(series)
    return project


# --------------------------------------------------------------------------
# ProjectData works with no web framework
# --------------------------------------------------------------------------


def test_project_data_imports_without_flask() -> None:
    """The data layer must not drag a web framework in.

    Guards the boundary the whole module split exists to hold: if a route
    helper ever leaks into project.py, this fails immediately rather than in
    six months when someone tries to reuse ProjectData from a script.
    """
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import nr_workbench.web.project as p; "
            "assert 'flask' not in sys.modules, sorted("
            "m for m in sys.modules if 'flask' in m); print('clean')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_overview_of_an_empty_project_lists_the_sample(project: Path) -> None:
    """A scaffolded project with no data still renders."""
    overview = ProjectData(project).overview()

    assert overview["name"] == "test-project"
    assert overview["beamtime"] == "june2026"
    assert overview["n_fits"] == 0
    assert [card["id"] for card in overview["samples"]] == ["Sample1"]
    assert overview["samples"][0]["n_steady"] == 0


def test_overview_counts_data_found_on_disk(populated: Path) -> None:
    """Sample cards report what the scan found."""
    card = ProjectData(populated).overview()["samples"][0]

    assert card["n_steady"] == 2
    assert card["n_series"] == 1


def test_sample_reports_prose_and_register(populated: Path) -> None:
    """The sample view carries both the human and machine records."""
    detail = ProjectData(populated).sample("Sample1")

    assert detail["id"] == "Sample1"
    assert "# Sample1" in detail["markdown"]
    assert [entry["run"] for entry in detail["steady"]] == [100001, 100002]
    assert detail["series"][0]["n_slices"] > 1
    assert detail["series"][0]["plottable"] is True


def test_sample_reports_data_the_prose_does_not_mention(populated: Path) -> None:
    """The gap between sample.md and disk is surfaced, not hidden."""
    detail = ProjectData(populated).sample("Sample1")

    assert 100001 in detail["present_but_undocumented"]


def test_missing_sample_raises_rather_than_returning_empty(project: Path) -> None:
    """Asking for a sample that does not exist is an error, not an empty page."""
    with pytest.raises(FileNotFoundError, match="Nope"):
        ProjectData(project).sample("Nope")


# --------------------------------------------------------------------------
# Curves
# --------------------------------------------------------------------------


def test_steady_curves_keeps_segments_separate(populated: Path) -> None:
    """Angle segments are not stitched: the overlap is diagnostic."""
    payload = ProjectData(populated).steady_curves("Sample1")

    labels = [curve["label"] for curve in payload["curves"]]
    assert labels == ["100001#1", "100001#2", "100001#3", "100002 combined"]
    assert not payload["problems"]


def test_steady_curves_reports_an_unreadable_file_and_keeps_going(
    populated: Path,
) -> None:
    """One corrupt file must not blank the whole panel."""
    broken = (
        populated
        / "samples"
        / "Sample1"
        / "data"
        / "steady"
        / "REFL_100001_2_100002_partial.txt"
    )
    broken.write_text("not numbers at all\n", encoding="utf-8")

    payload = ProjectData(populated).steady_curves("Sample1")

    assert len(payload["curves"]) == 3
    assert any("100001#2" in p["scope"] for p in payload["problems"])


def test_reduced_reader_rejects_a_file_with_too_few_columns(tmp_path: Path) -> None:
    """A two-column file is not a reduced file, and saying so beats guessing."""
    path = tmp_path / "bad.txt"
    path.write_text("0.01 1.0\n0.02 0.5\n", encoding="utf-8")

    with pytest.raises(DataFormatError, match="at least Q, R and dR"):
        read_reduced(path, label="x")


# --------------------------------------------------------------------------
# The tNR series and its assessment
# --------------------------------------------------------------------------


def test_series_data_returns_a_residual_map_against_the_shared_reference(
    populated: Path,
) -> None:
    """The heatmap uses the same reference block the assessment resolves."""
    payload = ProjectData(populated).series_data("Sample1", "100003")

    assert len(payload["residual"]) == payload["n_intervals"]
    assert len(payload["residual"][0]) == len(payload["q"])
    assert payload["reference"]["indices"]
    assert (
        "reference" in payload["reference"]["description"]
        or payload["reference"]["description"]
    )


def test_series_residual_is_near_zero_across_the_reference_block(
    populated: Path,
) -> None:
    """A reference interval differs from its own coadd only by noise.

    Anchors the sign and normalisation of the map: if the residual were
    inverted or scaled, the reference rows would not sit at zero.
    """
    payload = ProjectData(populated).series_data("Sample1", "100003")

    for index in payload["reference"]["indices"]:
        row = [v for v in payload["residual"][index] if v is not None]
        assert abs(float(np.median(row))) < 0.05


def test_series_warnings_are_returned_not_printed(
    populated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Loader diagnostics belong in the response, where a reader will see them."""
    payload = ProjectData(populated).series_data("Sample1", "100003")

    captured = capsys.readouterr()
    assert "Skipped" not in captured.err
    assert isinstance(payload["problems"], list)


@pytest.fixture
def assessed(populated: Path) -> Path:
    """The populated project with a real tNR assessment written."""
    from nr_workbench.tnr.notify import collect
    from nr_workbench.tnr.report import assess
    from nr_workbench.tnr.run import load

    out = populated / "samples" / "Sample1" / "assessments" / "r900001"
    series = populated / "samples" / "Sample1" / "data" / "tnr" / "100003"
    with collect():
        assess(load(series), out, label="r900001", plots=False)
    return populated


def test_assessment_amplitude_tracks_the_injected_trajectory(
    assessed: Path,
) -> None:
    """The a(t) the UI plots is the sigmoid that was put into the data.

    Compared by correlation, not value: ``a`` is normalised so the late block
    projects to 1, and the fixture's late block is not the asymptote, so the
    scale differs by a constant. Same reasoning as the metric's own test --
    what matters here is that no step between the file and the plot reorders,
    drops, or rescales the series.
    """
    payload = ProjectData(assessed).assessment("Sample1", "r900001")
    recovered = np.array(payload["amplitude_series"]["a"], dtype=float)
    expected = tnr_fixture.expected_amplitudes()
    finite = np.isfinite(recovered)

    assert recovered.shape == expected.shape
    correlation = float(np.corrcoef(recovered[finite], expected[finite])[0, 1])
    assert correlation > 0.97, (
        f"a(t) does not track the injection (r={correlation:.3f})"
    )


def test_assessment_series_matches_the_file_it_came_from(assessed: Path) -> None:
    """The parsed series must equal the ASCII the scientist reads.

    The plot and the file are the same numbers or the UI is lying about the
    data -- so this compares against the file directly rather than against
    another copy of the parser.
    """
    directory = assessed / "samples" / "Sample1" / "assessments" / "r900001"
    rows = [
        line.split("\t")
        for line in (directory / "r900001_amplitude.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line and not line.startswith("#")
    ]

    payload = ProjectData(assessed).assessment("Sample1", "r900001")
    series = payload["amplitude_series"]

    assert len(series["times"]) == len(rows)
    assert series["times"] == [float(row[0]) for row in rows]
    assert series["a"] == [float(row[1]) for row in rows]
    assert series["labels"] == [row[7] for row in rows]


def test_assessment_exposes_the_verdict_and_figures(assessed: Path) -> None:
    """The verdict is the headline; the UI must not have to recompute it."""
    payload = ProjectData(assessed).assessment("Sample1", "r900001")

    assert payload["assessment"]["verdict"]
    assert payload["label"] == "r900001"


def test_assessment_label_is_discovered_despite_the_filename_prefix(
    assessed: Path,
) -> None:
    """Outputs are named ``<label>_assessment.json``, not ``assessment.json``."""
    assert ProjectData(assessed).assessment_labels("Sample1") == ["r900001"]


def test_amplitude_reader_rejects_a_truncated_row(tmp_path: Path) -> None:
    """A short data line means the format changed; fail loudly."""
    path = tmp_path / "amplitude.txt"
    path.write_text("# reference: leading\n0.0\t1.0\t0.1\n", encoding="utf-8")

    with pytest.raises(DataFormatError, match="expected 8"):
        read_amplitude_txt(path)


def test_amplitude_reader_keeps_the_provenance_header(tmp_path: Path) -> None:
    """The header says what a=0 and a=1 mean; the plot is wrong without it."""
    path = tmp_path / "amplitude.txt"
    path.write_text(
        "# reference: leading 3 intervals\n"
        "# late_block: trailing block\n"
        "# time_s\ta\tsigma_a\tsignificance\tchi2_res\tn_q\tinterval_type\tlabel\n"
        "0.0\t0.1\t0.01\t10.0\t1.02\t200\thold\tfirst\n",
        encoding="utf-8",
    )

    series = read_amplitude_txt(path)

    assert series.header["reference"] == "leading 3 intervals"
    assert series.labels == ["first"]
    assert series.times == [0.0]


# --------------------------------------------------------------------------
# Fits
# --------------------------------------------------------------------------


def test_numbered_sorts_by_integer_not_lexically(tmp_path: Path) -> None:
    """Model 10 must not sort between 1 and 2.

    Lexical ordering here would pair curves with the wrong labels -- a plot
    that looks entirely reasonable and is simply wrong.
    """
    for index in (1, 2, 10, 11, 21):
        (tmp_path / f"run-{index}-refl.dat").write_text("", encoding="utf-8")

    assert [i for i, _ in _numbered(tmp_path, "-refl.dat")] == [1, 2, 10, 11, 21]


def test_profile_thinning_preserves_the_curve(tmp_path: Path) -> None:
    """Display thinning must not move an interface.

    refl1d writes its profile on the grid its integration needs, which is far
    finer than any feature in the model. Thinning it is worth doing, but only
    if the shape survives -- so compare the thinned curve, interpolated back,
    against every original point.
    """
    z = np.arange(-50.0, 650.0, 0.1)
    rho = 3.0 + 3.0 * np.tanh((z - 100.0) / 8.0) - 2.0 * np.tanh((z - 400.0) / 5.0)
    path = tmp_path / "m-1-profile.dat"
    path.write_text(
        "# z rho irho\n"
        + "\n".join(f"{a:.6f} {b:.6f} 0.0" for a, b in zip(z, rho, strict=True)),
        encoding="utf-8",
    )

    full = read_profile_dat(path, label="x", max_spacing=0)
    thin = read_profile_dat(path, label="x")

    assert thin.stride > 1
    assert len(thin.z) < len(full.z) / 2
    worst = np.abs(np.interp(full.z, thin.z, thin.rho) - np.array(full.rho)).max()
    assert worst < 0.01 * (max(full.rho) - min(full.rho))


def test_fit_curves_are_labelled_from_the_recorded_model_names() -> None:
    """Export position maps to a name via the manifest, not via build order."""
    from nr_workbench.web.project import _model_names

    names = _model_names(
        [{"index": 1, "name": "ocv1#0"}, {"index": 2}, {"index": 3, "name": "tnr#0"}]
    )

    assert names == {1: "ocv1#0", 3: "tnr#0"}


def test_describe_models_records_names_and_positions() -> None:
    """The runner reads the ordering off the object it actually fitted."""
    from nr_workbench.fitting.runner import describe_models

    class Model:
        def __init__(self, name: str | None) -> None:
            self.name = name

        def numpoints(self) -> int:
            return 42

    class Problem:
        models = [Model("ocv1#0"), Model(None), Model("tnr#3")]

    assert describe_models(Problem()) == [
        {"index": 1, "name": "ocv1#0", "n_points": 42},
        {"index": 2, "n_points": 42},
        {"index": 3, "name": "tnr#3", "n_points": 42},
    ]


def test_describe_models_tolerates_a_problem_without_models() -> None:
    """A bumps object that exposes no models must not break the record."""
    from nr_workbench.fitting.runner import describe_models

    assert describe_models(object()) == []


# --------------------------------------------------------------------------
# Prose rendering
# --------------------------------------------------------------------------


def test_markdown_drops_commented_examples(populated: Path) -> None:
    """The template's worked example must not appear as real measurements."""
    source = (populated / "samples" / "Sample1" / "sample.md").read_text(
        encoding="utf-8"
    )

    rendered = render(source)

    assert "<h1>Sample1</h1>" in rendered
    assert "230594" not in rendered
    assert "One line per run" not in rendered


def test_markdown_renders_the_measurement_table() -> None:
    """The register in sample.md is a pipe table and must render as one."""
    rendered = render("| Run | Type |\n|-----|------|\n| 1 | full Q |\n")

    assert "<table>" in rendered
    assert "<td>full Q</td>" in rendered


def test_markdown_does_not_pass_through_raw_html() -> None:
    """Prose in a side panel has no reason to be able to run script."""
    rendered = render("Hello <script>alert(1)</script> there")

    assert "<script>" not in rendered


def test_strip_comments_handles_multiline_blocks() -> None:
    """The scaffold's comments span many lines."""
    assert strip_comments("a\n<!-- x\ny\nz -->\nb").strip() == "a\n\nb"


# --------------------------------------------------------------------------
# Flask
# --------------------------------------------------------------------------


@pytest.fixture
def client(populated: Path):
    """A test client over the populated project."""
    from nr_workbench.web.app import create_app

    return create_app(populated).test_client()


def test_create_app_refuses_a_directory_that_is_not_a_project(
    tmp_path: Path,
) -> None:
    """Pointing the server at the wrong directory must say so."""
    from nr_workbench.web.app import create_app

    with pytest.raises(FileNotFoundError, match="nrw init"):
        create_app(tmp_path)


@pytest.mark.parametrize(
    "url",
    [
        "/",
        "/fits",
        "/s/Sample1",
        "/api/overview",
        "/api/samples/Sample1",
        "/api/samples/Sample1/curves",
        "/api/samples/Sample1/series/100003",
        "/api/samples/Sample1/series/100003/curves",
        "/api/samples/Sample1/assessments",
        "/api/fits",
    ],
)
def test_route_renders(client, url: str) -> None:
    """Every route serves without error on a real project."""
    assert client.get(url).status_code == 200


@pytest.mark.parametrize(
    "url",
    ["/s/Nope", "/api/samples/Nope", "/api/fits/nosuchfit", "/f/nosuchfit"],
)
def test_missing_things_are_404_not_500(client, url: str) -> None:
    """A wrong URL is the user's mistake; it should read like one."""
    assert client.get(url).status_code == 404


def test_api_errors_are_json_not_html(client) -> None:
    """The API's consumer is a script, so its errors must parse."""
    response = client.get("/api/samples/Nope")

    assert response.status_code == 404
    assert "error" in response.get_json()


def test_api_mirrors_project_data(populated: Path, client) -> None:
    """The API adds nothing of its own, so the two must agree exactly."""
    direct = ProjectData(populated).overview()
    served = client.get("/api/overview").get_json()

    assert served == json.loads(json.dumps(direct, default=str))


def test_sample_page_embeds_parseable_payloads(client) -> None:
    """A malformed embedded payload is a blank page, so parse it here."""
    import re

    html = client.get("/s/Sample1").get_data(as_text=True)

    for name in ("curves", "series", "sampleId"):
        match = re.search(rf"const {name} = (.*?);\n", html, re.S)
        assert match, f"{name} is not embedded in the page"
        json.loads(match.group(1))


def test_help_does_not_import_flask() -> None:
    """`nrw --help` must not pay for the web stack.

    The serve command lazy-imports Flask for the same reason every other
    command lazy-imports its dependencies: the CLI is used constantly and a
    slow --help is a tax on every single invocation.
    """
    import subprocess

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from click.testing import CliRunner; "
            "from nr_workbench.cli import main; "
            "CliRunner().invoke(main, ['--help']); "
            "bad = [m for m in ('flask', 'aure') if m in sys.modules]; "
            "assert not bad, bad; print('clean')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_serve_is_listed_in_help() -> None:
    """The command must be discoverable."""
    from click.testing import CliRunner

    from nr_workbench.cli import main

    result = CliRunner().invoke(main, ["--help"])

    assert "serve" in result.output


# --------------------------------------------------------------------------
# The fit view, against a real fit
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_fit_view_labels_curves_from_a_generated_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generated script's spec slots survive all the way to the plot legend.

    bumps names its exports by position only, so this is the end-to-end check
    that the name recorded at fit time is what the UI shows -- the alternative
    is a legend reconstructed from build order, which is wrong silently.
    """
    pytest.importorskip("refl1d")
    from click.testing import CliRunner

    from nr_workbench.cli import main

    runner = CliRunner()
    root = tmp_path / "proj"
    assert runner.invoke(main, ["init", str(root), "--sample", "S1"]).exit_code == 0

    steady = root / "samples" / "S1" / "data" / "steady"
    steady.mkdir(parents=True, exist_ok=True)
    for segment in (1, 2):
        write_reduced(steady / f"REFL_100001_{segment}_10000{segment}_partial.txt")

    spec = root / "samples" / "S1" / "models" / "m.yaml"
    spec.write_text(
        "schema: nrw-model/1\n"
        "name: m\n"
        "sample: S1\n"
        "materials:\n"
        "  D2O: {rho: 6.19}\n"
        "  Film: {rho: 4.0}\n"
        "  Si: {rho: 2.07}\n"
        "stack:\n"
        "  - {name: D2O, material: D2O, thickness: 0, roughness: 5}\n"
        "  - {name: Film, material: Film, thickness: 120, roughness: 5}\n"
        "  - {name: Si, material: Si}\n"
        "probe: {resolution: angular_only}\n"
        "states:\n"
        "  - name: ocv1\n"
        "    run: 100001\n"
        "    segments: auto\n"
        "    thetas: [0.45, 1.2]\n"
        "    data_dir: samples/S1/data/steady\n"
        "parameters:\n"
        "  - {path: Film.thickness, range: [50, 200], per: model}\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(root)
    generated = runner.invoke(main, ["model", "generate", "samples/S1/models/m.yaml"])
    assert generated.exit_code == 0, generated.output

    fitted = runner.invoke(
        main,
        ["fit", "run", "samples/S1/models/m.py", "--method", "amoeba", "--steps", "6"],
    )
    assert fitted.exit_code == 0, fitted.output

    data = ProjectData(root)
    fit_id = data.fits("S1")[0]["fit_id"]
    detail = data.fit(fit_id)

    assert [curve["label"] for curve in detail["curves"]] == ["ocv1#0", "ocv1#1"]
    assert all(curve["named"] for curve in detail["curves"])
    assert not detail["problems"]
    assert detail["curves"][0]["theory"], "the model curve must be plotted too"
    assert detail["profiles"], "an SLD profile must be available"

    from nr_workbench.web.app import create_app

    client = create_app(root).test_client()
    page = client.get(f"/f/{fit_id}")
    assert page.status_code == 200
    assert b"ocv1#0" in page.data
    assert b"Provenance" in page.data


@pytest.mark.integration
def test_fit_view_says_so_when_a_script_named_no_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hand-written script may not name its experiments; admit it.

    Labelling those curves "ocv1#0" would be an invention. Position is all the
    fit recorded, so position is all the UI claims.
    """
    pytest.importorskip("refl1d")
    from click.testing import CliRunner

    from nr_workbench.cli import main

    runner = CliRunner()
    root = tmp_path / "proj"
    assert runner.invoke(main, ["init", str(root), "--sample", "S1"]).exit_code == 0

    steady = root / "samples" / "S1" / "data" / "steady"
    steady.mkdir(parents=True, exist_ok=True)
    write_reduced(steady / "REFL_100001_combined_data_auto.txt")

    models = root / "samples" / "S1" / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "hand.py").write_text(
        "import os\n"
        "import numpy as np\n"
        "from refl1d.names import SLD, Experiment, FitProblem, QProbe\n"
        "DATA = os.path.join(os.path.dirname(__file__), '..', 'data', 'steady',\n"
        "                    'REFL_100001_combined_data_auto.txt')\n"
        "q, r, dr, dq = np.loadtxt(DATA).T\n"
        "probe = QProbe(q, dq / 2.355, data=(r, dr))\n"
        "sample = SLD('D2O', rho=6.19)(0, 5) | SLD('Film', rho=4.0)(120, 5) "
        "| SLD('Si', rho=2.07)\n"
        "sample['Film'].thickness.range(50, 200)\n"
        "problem = FitProblem(Experiment(sample=sample, probe=probe))\n",
        encoding="utf-8",
    )

    monkeypatch.chdir(root)
    fitted = runner.invoke(
        main,
        [
            "fit",
            "run",
            "samples/S1/models/hand.py",
            "--method",
            "amoeba",
            "--steps",
            "6",
        ],
    )
    assert fitted.exit_code == 0, fitted.output

    data = ProjectData(root)
    detail = data.fit(data.fits("S1")[0]["fit_id"])

    assert detail["curves"][0]["label"] == "model 1"
    assert detail["curves"][0]["named"] is False
    assert any("no model names" in p["message"] for p in detail["problems"])


# --------------------------------------------------------------------------
# Layer parameters through time
# --------------------------------------------------------------------------


def sample_with_series(project: Path) -> Path:
    """A project with two steady states and a three-slice series."""
    import numpy as np

    q = np.linspace(0.01, 0.2, 25)
    r = 1e-3 * (0.01 / q) ** 4
    body = "\n".join(
        f"{a:.6e} {b:.6e} {c:.6e} {d:.6e}"
        for a, b, c, d in zip(q, r, 0.05 * r, 0.02 * q, strict=True)
    )
    steady = project / "samples" / "Sample1" / "data" / "steady"
    steady.mkdir(parents=True, exist_ok=True)
    for run in (100001, 100002):
        (steady / f"REFL_{run}_1_{run}_partial.txt").write_text(body)
    tnr = project / "samples" / "Sample1" / "data" / "tnr" / "100003"
    tnr.mkdir(parents=True, exist_ok=True)
    for seconds in (0, 240, 480):
        (tnr / f"r100003_t{seconds:06d}.txt").write_text(body)

    models = project / "samples" / "Sample1" / "models"
    models.mkdir(parents=True, exist_ok=True)
    (models / "m.yaml").write_text(
        "schema: nrw-model/1\nname: m\nsample: Sample1\n"
        "materials: {D2O: {rho: 6.36}, Cu: {rho: 6.55}, Si: {rho: 2.07}}\n"
        "stack:\n"
        "  - {name: D2O, material: D2O, thickness: 0, roughness: 5}\n"
        "  - {name: Cu, material: Cu, thickness: 500, roughness: 5}\n"
        "  - {name: Si, material: Si}\n"
        "probe: {resolution: angular_only}\n"
        "states:\n"
        "  - {name: ocv1, run: 100001, segments: auto, thetas: [0.45],\n"
        "     data_dir: samples/Sample1/data/steady}\n"
        "  - {name: ocv2, run: 100002, segments: auto, thetas: [0.45],\n"
        "     data_dir: samples/Sample1/data/steady}\n"
        "series:\n"
        "  - {name: tnr, run: 100003, reduced_dir: samples/Sample1/data/tnr/100003,\n"
        "     theta: 0.6, time_from: filename}\n"
        "parameters:\n"
        "  - {path: Cu.thickness, range: [400, 600], per: state, in: [ocv1, ocv2]}\n"
        "constraints:\n"
        "  - series: tnr\n    form: linear_in_time\n    from: ocv1\n    to: ocv2\n"
        "    paths: [Cu.thickness]\n",
        encoding="utf-8",
    )
    return project


def test_slabs_are_read_in_stack_order(tmp_path: Path) -> None:
    """bumps writes thickness, interface, rho, irho -- ambient row first."""
    from nr_workbench.web.trajectory import read_slabs

    path = tmp_path / "m-1-slabs.dat"
    path.write_text(
        "#  thickness   interface   rho   irho\n"
        "0    14.5   5.94   0\n"
        "44.9  8.07   5.19   0\n"
        "0     0      2.07   0\n",
        encoding="utf-8",
    )

    rows = read_slabs(path)

    assert len(rows) == 3
    assert rows[1]["thickness"] == pytest.approx(44.9)
    assert rows[1]["roughness"] == pytest.approx(8.07)
    assert rows[1]["rho"] == pytest.approx(5.19)


def test_posterior_columns_are_taken_verbatim_from_err_json(tmp_path: Path) -> None:
    """`index` in -err.json is already the chain column, not an offset.

    Column 0 is logp and the indices start at 1, so adding one shifts every
    parameter onto its neighbour. That produces bands which look entirely
    plausible and describe a different quantity -- a copper *roughness* with a
    500 A interval, because it got the thickness column. Nothing crashes and
    no value is obviously wrong; only bracketing the fitted value catches it.
    """
    import gzip

    import numpy as np

    from nr_workbench.web.trajectory import load_posterior

    samples = np.column_stack(
        [
            np.full(50, -10.0),  # logp
            np.full(50, 5.0),  # parameter at index 1
            np.full(50, 500.0),  # parameter at index 2
        ]
    )
    with gzip.open(tmp_path / "m-point.mc.gz", "wt") as handle:
        np.savetxt(handle, samples)
    (tmp_path / "m-err.json").write_text(
        json.dumps(
            {
                "a roughness": {"index": 1, "best": 5.0},
                "a thickness": {"index": 2, "best": 500.0},
            }
        ),
        encoding="utf-8",
    )

    loaded, columns = load_posterior(tmp_path)

    assert loaded is not None
    assert columns == {"a roughness": 1, "a thickness": 2}
    assert loaded[0, columns["a roughness"]] == pytest.approx(5.0)
    assert loaded[0, columns["a thickness"]] == pytest.approx(500.0)


def test_the_band_is_evaluated_from_paired_samples() -> None:
    """Correlated endpoints must stay correlated.

    A trajectory is a function of two fitted endpoints, and for an
    interpolating form they are strongly anti-correlated. Propagating each
    parameter's `std` independently would widen the band in the middle of the
    series; using paired posterior samples narrows it there, which is the
    physically right answer.

    The tuple is ``(lo, median, hi)``: the median rides along because the gap
    between it and the reported best fit is diagnostic.
    """
    import numpy as np

    from nr_workbench.web.trajectory import band_for

    # Perfectly anti-correlated endpoints: their midpoint is exactly constant.
    n = 400
    start = np.linspace(90.0, 110.0, n)
    end = 200.0 - start
    samples = np.column_stack([np.zeros(n), start, end])
    columns = {"S": 1, "E": 2}

    band = band_for(
        {
            "L.t@s#0": "P['a'] + (P['b'] - P['a']) * 0.0",
            "L.t@s#1": "P['a'] + (P['b'] - P['a']) * 0.5",
            "L.t@s#2": "P['a'] + (P['b'] - P['a']) * 1.0",
        },
        {"a": "S", "b": "E"},
        samples,
        columns,
    )

    width = {k: hi - lo for k, (lo, _median, hi) in band.items()}
    assert width["L.t@s#1"] < width["L.t@s#0"] / 10, (
        "the midpoint band must collapse; it did not, so the samples were not paired"
    )


def test_an_expression_naming_an_unknown_parameter_is_skipped() -> None:
    """Better no band than one computed from a parameter we could not find."""
    import numpy as np

    from nr_workbench.web.trajectory import band_for

    samples = np.column_stack([np.zeros(10), np.ones(10)])

    band = band_for({"L.t@s#0": "P['missing'] * 2"}, {"a": "S"}, samples, {"S": 1})

    assert band == {}


@pytest.mark.fit
@pytest.mark.integration
def test_a_trajectory_band_brackets_the_fitted_values(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end property, and the one the column bug violated.

    Every fitted slice value must lie inside its own credible band. That is
    true by construction when the columns line up and false in an obvious way
    when they do not.
    """
    pytest.importorskip("refl1d")
    from click.testing import CliRunner

    from nr_workbench.cli import main

    root = sample_with_series(project)
    monkeypatch.chdir(root)

    runner = CliRunner()
    generated = runner.invoke(
        main, ["model", "generate", "samples/Sample1/models/m.yaml"]
    )
    assert generated.exit_code == 0, generated.output
    fitted = runner.invoke(
        main,
        [
            "fit",
            "run",
            "samples/Sample1/models/m.py",
            "--method",
            "dream",
            "--samples",
            "2000",
            "--burn",
            "50",
            "--pop",
            "6",
            "--seed",
            "1",
            "--parallel",
            "1",
        ],
    )
    assert fitted.exit_code == 0, fitted.output

    data = ProjectData(root)
    result = data.trajectory(data.fits("Sample1")[0]["fit_id"])

    assert result["traces"], "a series fit must produce trajectories"
    banded = [t for t in result["traces"] if "lo" in t]
    assert banded, "a dream fit must produce a band"

    # The band must describe the same quantity as the value. Tolerance of one
    # band width, not zero: the reported value is the maximum-likelihood point,
    # which is not obliged to sit inside a *central* 68% interval and routinely
    # sits just outside it for a parameter railed against its bound.
    #
    # An order of magnitude is what the column bug produced -- a copper
    # roughness of 5 A carrying the thickness column's [500, 530] band -- and
    # that is what this catches.
    for trace in banded:
        for lo, value, hi in zip(
            trace["lo"], trace["values"], trace["hi"], strict=True
        ):
            width = max(hi - lo, abs(value) * 1e-6)
            assert lo - width <= value <= hi + width, (
                f"{trace['path']}: fitted {value} is not on the same scale as "
                f"its band [{lo}, {hi}] -- the posterior column is probably "
                "matched to the wrong parameter"
            )


def test_a_fit_without_a_frozen_spec_uses_the_live_one_when_it_matches(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fits made before spec.yaml was frozen should not need re-running.

    The generated script records the digest of the spec it came from, so a
    spec still on disk that hashes to it is provably the same file. Using it is
    exact, not a guess.
    """
    pytest.importorskip("refl1d")
    from click.testing import CliRunner

    from nr_workbench.cli import main

    root = sample_with_series(project)
    monkeypatch.chdir(root)
    runner = CliRunner()
    assert (
        runner.invoke(
            main, ["model", "generate", "samples/Sample1/models/m.yaml"]
        ).exit_code
        == 0
    )
    fitted = runner.invoke(
        main,
        [
            "fit",
            "run",
            "samples/Sample1/models/m.py",
            "--method",
            "amoeba",
            "--steps",
            "6",
            "--parallel",
            "1",
        ],
    )
    assert fitted.exit_code == 0, fitted.output

    data = ProjectData(root)
    fit_id = data.fits("Sample1")[0]["fit_id"]
    frozen = root / "samples" / "Sample1" / "results" / fit_id / "spec.yaml"
    assert frozen.is_file(), "new fits freeze it"
    frozen.unlink()  # simulate a fit made before that

    result = data.trajectory(fit_id)

    assert result["traces"], result.get("problems")
    assert not result.get("problems")


def test_an_edited_spec_is_refused_rather_than_used(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An edited spec describes a different model than the one that ran.

    Showing its trajectory against this fit's numbers would be worse than
    showing nothing, so the reason is reported instead.
    """
    pytest.importorskip("refl1d")
    from click.testing import CliRunner

    from nr_workbench.cli import main

    root = sample_with_series(project)
    monkeypatch.chdir(root)
    runner = CliRunner()
    runner.invoke(main, ["model", "generate", "samples/Sample1/models/m.yaml"])
    runner.invoke(
        main,
        [
            "fit",
            "run",
            "samples/Sample1/models/m.py",
            "--method",
            "amoeba",
            "--steps",
            "6",
            "--parallel",
            "1",
        ],
    )

    data = ProjectData(root)
    fit_id = data.fits("Sample1")[0]["fit_id"]
    results = root / "samples" / "Sample1" / "results" / fit_id
    (results / "spec.yaml").unlink()
    # and the precomputed answer, so the fallback is what is exercised
    (results / "trajectory.json").unlink(missing_ok=True)
    spec = root / "samples" / "Sample1" / "models" / "m.yaml"
    spec.write_text(spec.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")

    result = data.trajectory(fit_id)

    assert not result["traces"]
    assert any("edited since this fit ran" in p["message"] for p in result["problems"])


def test_the_fit_page_says_why_there_is_no_trajectory(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty panel with no explanation is the thing to avoid."""
    pytest.importorskip("refl1d")
    from click.testing import CliRunner

    from nr_workbench.cli import main
    from nr_workbench.web.app import create_app

    root = sample_with_series(project)
    monkeypatch.chdir(root)
    runner = CliRunner()
    runner.invoke(main, ["model", "generate", "samples/Sample1/models/m.yaml"])
    runner.invoke(
        main,
        [
            "fit",
            "run",
            "samples/Sample1/models/m.py",
            "--method",
            "amoeba",
            "--steps",
            "6",
            "--parallel",
            "1",
        ],
    )
    fit_id = ProjectData(root).fits("Sample1")[0]["fit_id"]
    results = root / "samples" / "Sample1" / "results" / fit_id
    (results / "spec.yaml").unlink()
    (results / "trajectory.json").unlink(missing_ok=True)
    spec = root / "samples" / "Sample1" / "models" / "m.yaml"
    spec.write_text(spec.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")

    page = create_app(root).test_client().get(f"/f/{fit_id}").get_data(as_text=True)

    assert "Layer parameters through time" in page
    assert "edited since this fit ran" in page


def test_profiles_are_aligned_to_the_substrate_surface() -> None:
    """refl1d puts z = 0 at the top, so profiles drift as thickness changes.

    The substrate is the one interface that cannot move, so anchoring it puts
    every profile on a common footing and only the layer that actually changed
    moves. This is refl1d's own `align=-1`.
    """
    from nr_workbench.web.trajectory import substrate_offset

    slabs = [
        {"thickness": 0.0},  # ambient
        {"thickness": 27.4},
        {"thickness": 481.3},
        {"thickness": 33.6},
        {"thickness": 0.0},  # substrate
    ]

    assert substrate_offset(slabs) == pytest.approx(542.3)
    assert substrate_offset([{"thickness": 5.0}]) == 0.0, "no substrate, no offset"


def test_two_profiles_of_different_total_thickness_share_a_zero() -> None:
    """The property the alignment exists for."""
    from nr_workbench.web.trajectory import substrate_offset

    thin = [
        {"thickness": 0.0},
        {"thickness": 480.0},
        {"thickness": 30.0},
        {"thickness": 0.0},
    ]
    thick = [
        {"thickness": 0.0},
        {"thickness": 520.0},
        {"thickness": 30.0},
        {"thickness": 0.0},
    ]

    # The substrate sits at the offset in each, so both land on zero.
    assert 510.0 - substrate_offset(thin) == pytest.approx(0.0)
    assert 550.0 - substrate_offset(thick) == pytest.approx(0.0)


def test_a_deleted_result_directory_is_marked_not_hidden(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The index is append-only: that a fit ran stays true after a cleanup.

    But its artifacts may be gone, and a row linking to a 404 is worse than one
    that says so.
    """
    pytest.importorskip("refl1d")
    import shutil

    from click.testing import CliRunner

    from nr_workbench.cli import main

    root = sample_with_series(project)
    monkeypatch.chdir(root)
    runner = CliRunner()
    runner.invoke(main, ["model", "generate", "samples/Sample1/models/m.yaml"])
    runner.invoke(
        main,
        [
            "fit",
            "run",
            "samples/Sample1/models/m.py",
            "--method",
            "amoeba",
            "--steps",
            "6",
            "--parallel",
            "1",
        ],
    )

    data = ProjectData(root)
    fit_id = data.fits("Sample1")[0]["fit_id"]
    assert data.fits("Sample1")[0]["present"] is True

    shutil.rmtree(root / "samples" / "Sample1" / "results" / fit_id)

    rows = data.fits("Sample1")
    assert len(rows) == 1, "the record survives the directory"
    assert rows[0]["present"] is False

    from nr_workbench.web.app import create_app

    page = create_app(root).test_client().get("/fits").get_data(as_text=True)
    assert "deleted" in page
    assert f'href="/f/{fit_id}"' not in page, "it must not link to a 404"


def test_the_fit_table_says_what_each_row_was_and_what_changed(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fit id identifies a run but does not describe it. Three of them in a
    table are indistinguishable without opening each one."""
    pytest.importorskip("refl1d")
    from click.testing import CliRunner

    from nr_workbench.cli import main
    from nr_workbench.web.app import create_app

    root = sample_with_series(project)
    monkeypatch.chdir(root)
    runner = CliRunner()
    runner.invoke(main, ["model", "generate", "samples/Sample1/models/m.yaml"])
    for steps, note in ((6, "first look"), (14, None)):
        command = [
            "fit",
            "run",
            "samples/Sample1/models/m.py",
            "--method",
            "amoeba",
            "--steps",
            str(steps),
            "--parallel",
            "1",
        ]
        if note:
            command += ["--note", note]
        assert runner.invoke(main, command).exit_code == 0

    rows = ProjectData(root).fits("Sample1")
    assert "steps 6 -> 14" in rows[0]["change"], rows[0]["change"]
    assert rows[1]["change"] == "first run of this model"
    assert rows[1]["description"] == "first look"

    client = create_app(root).test_client()
    for url in ("/fits", "/s/Sample1"):
        page = client.get(url).get_data(as_text=True)
        assert "steps 6 -&gt; 14" in page, url
        assert "first look" in page, url

    # And the same two lines on the fit itself, so a bookmark says as much as
    # the table it was reached from.
    detail = client.get(f"/f/{rows[0]['fit_id']}").get_data(as_text=True)
    assert "steps 6 -&gt; 14" in detail
    assert f'href="/f/{rows[1]["fit_id"]}"' in detail, "links to what it changed from"


# --------------------------------------------------------------------------
# What the story row under each fit actually says
#
# It said "first run of this model" more often than anything else, which tells
# a reader nothing they cannot see from the list. What they want is the
# structure and the reason.
# --------------------------------------------------------------------------


@pytest.fixture
def two_fits(project: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project with two real fits of a D2O|Cu|Si stack, one with a reason."""
    pytest.importorskip("refl1d")
    from click.testing import CliRunner

    from nr_workbench.cli import main

    root = sample_with_series(project)
    monkeypatch.chdir(root)
    runner = CliRunner()
    runner.invoke(main, ["model", "generate", "samples/Sample1/models/m.yaml"])
    for steps, note in ((6, "checking whether the copper is thick enough"), (14, None)):
        command = [
            "fit",
            "run",
            "samples/Sample1/models/m.py",
            "--method",
            "amoeba",
            "--steps",
            str(steps),
            "--parallel",
            "1",
        ]
        if note:
            command += ["--note", note]
        assert runner.invoke(main, command).exit_code == 0
    return root


def test_each_fit_row_carries_the_structure_it_was_a_fit_of(two_fits: Path) -> None:
    """`THF|Cu|Ti|Si` is how a reflectometrist names a model out loud."""
    rows = ProjectData(two_fits).fits("Sample1")

    assert [row["stack"] for row in rows] == ["D2O|Cu|Si", "D2O|Cu|Si"]


def test_the_structure_shows_on_both_pages_that_list_fits(two_fits: Path) -> None:
    from nr_workbench.web.app import create_app

    client = create_app(two_fits).test_client()

    for url in ("/fits", "/s/Sample1"):
        assert "D2O|Cu|Si" in client.get(url).get_data(as_text=True), url


def test_the_reason_shown_is_the_analysts_not_the_fitters_settings(
    two_fits: Path,
) -> None:
    """ "amoeba, 6 steps, 21 free" answers how it was configured, and every
    field of that is already a column of the table."""
    rows = ProjectData(two_fits).fits("Sample1")

    assert rows[1]["why"] == "checking whether the copper is thick enough"
    assert rows[0]["why"] == "", "nothing was said about the second run"


def test_a_reason_written_after_the_fit_reaches_the_listing(
    two_fits: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`nrw note --why` is the documented way to record this, and it writes
    into NOTES.md rather than into the index."""
    from click.testing import CliRunner

    from nr_workbench.cli import main

    rows = ProjectData(two_fits).fits("Sample1")
    monkeypatch.chdir(two_fits)
    result = CliRunner().invoke(
        main,
        ["note", rows[0]["fit_id"], "--why", "does more amoeba help at all?"],
    )
    assert result.exit_code == 0, result.output

    assert ProjectData(two_fits).fits("Sample1")[0]["why"] == (
        "does more amoeba help at all?"
    )


def test_first_run_of_this_model_is_not_shown_as_though_it_said_something(
    two_fits: Path,
) -> None:
    """It is true and it is useless: a reader looking at the first fit of a
    model can see that from the list."""
    from nr_workbench.provenance.summary import FIRST_RUN
    from nr_workbench.web.app import create_app

    client = create_app(two_fits).test_client()

    for url in ("/fits", "/s/Sample1"):
        assert FIRST_RUN not in client.get(url).get_data(as_text=True), url


def test_a_fit_with_no_reason_names_the_command_that_records_one(
    two_fits: Path,
) -> None:
    """The gap is the point. An empty line teaches nothing; this one says what
    to type."""
    from nr_workbench.web.app import create_app

    page = create_app(two_fits).test_client().get("/fits").get_data(as_text=True)

    assert "why not recorded" in page
    assert "--why" in page


def test_the_template_alone_is_not_mistaken_for_a_reason(two_fits: Path) -> None:
    """Every result directory holds a NOTES.md whether or not anyone wrote in
    it. Rendering its prompts would put "What were you testing?" on the page
    as though it were an answer."""
    rows = ProjectData(two_fits).fits("Sample1")
    notes = (
        two_fits / "samples" / "Sample1" / "results" / rows[0]["fit_id"] / "NOTES.md"
    )

    assert "What were you testing" in notes.read_text(encoding="utf-8")
    assert rows[0]["why"] == ""


def test_the_structure_survives_a_project_that_predates_the_indexed_stack(
    two_fits: Path,
) -> None:
    """Every fit in any project that already exists was recorded before the
    index carried a stack. Reading it back out of the frozen bumps export is
    what makes this useful today rather than on the next fit."""
    index = two_fits / ".nrw" / "index.jsonl"
    index.write_text(
        "\n".join(
            json.dumps({k: v for k, v in json.loads(line).items() if k != "stack"})
            for line in index.read_text().splitlines()
            if line.strip()
        )
        + "\n",
        encoding="utf-8",
    )

    rows = ProjectData(two_fits).fits("Sample1")

    assert [row["stack"] for row in rows] == ["D2O|Cu|Si", "D2O|Cu|Si"]


def test_the_structure_survives_the_result_directory_being_deleted(
    two_fits: Path,
) -> None:
    """The index is append-only so a cleaned-up fit still appears. A row with
    no structure and no link is the least useful row in the table."""
    import shutil

    rows = ProjectData(two_fits).fits("Sample1")
    shutil.rmtree(two_fits / "samples" / "Sample1" / "results" / rows[0]["fit_id"])

    after = ProjectData(two_fits).fits("Sample1")[0]

    assert after["present"] is False
    assert after["stack"] == "D2O|Cu|Si", "recorded in the index, not derived on demand"


def test_reflectivity_is_plotted_log_log() -> None:
    """Fresnel decay is a power law, so log-log straightens it.

    On a linear Q axis four decades of fringes pile up at the left.
    """
    source = (
        Path(__file__).resolve().parent.parent / "src/nr_workbench/web/static/nrw.js"
    ).read_text(encoding="utf-8")

    # Every reflectivity x-axis declares a log type.
    assert source.count('title: { text: "Q (Å⁻¹)" },\n          type: "log"') >= 2


def test_the_trajectory_is_precomputed_at_fit_time(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Assembling it costs about a second; the inputs never change.

    Resolving the spec, reading 21 slab tables and evaluating the constraint
    across the posterior is a second of work paid on every page load if it is
    done on demand -- and a fit directory is immutable once written, so the
    answer cannot go stale.
    """
    pytest.importorskip("refl1d")
    from click.testing import CliRunner

    from nr_workbench.cli import main

    root = sample_with_series(project)
    monkeypatch.chdir(root)
    runner = CliRunner()
    runner.invoke(main, ["model", "generate", "samples/Sample1/models/m.yaml"])
    fitted = runner.invoke(
        main,
        [
            "fit",
            "run",
            "samples/Sample1/models/m.py",
            "--method",
            "amoeba",
            "--steps",
            "6",
            "--parallel",
            "1",
        ],
    )
    assert fitted.exit_code == 0, fitted.output

    data = ProjectData(root)
    fit_id = data.fits("Sample1")[0]["fit_id"]
    cached = root / "samples" / "Sample1" / "results" / fit_id / "trajectory.json"

    assert cached.is_file(), "the fit should have written it"
    from_disk = data.trajectory(fit_id)
    assert from_disk["traces"]

    # and the cached answer is the one that would have been computed
    cached.unlink()
    computed = data.trajectory(fit_id)
    assert len(computed["traces"]) == len(from_disk["traces"])
    assert computed["series"] == from_disk["series"]


def test_a_summary_failure_does_not_fail_the_fit(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fit that ran is worth recording even if it cannot be summarised."""
    pytest.importorskip("refl1d")
    from click.testing import CliRunner

    from nr_workbench.cli import main

    root = sample_with_series(project)
    monkeypatch.chdir(root)
    runner = CliRunner()
    runner.invoke(main, ["model", "generate", "samples/Sample1/models/m.yaml"])

    import nr_workbench.web.project as project_module

    def explode(self, fit_id):
        raise RuntimeError("summary is broken")

    monkeypatch.setattr(project_module.ProjectData, "trajectory", explode)

    result = runner.invoke(
        main,
        [
            "fit",
            "run",
            "samples/Sample1/models/m.py",
            "--method",
            "amoeba",
            "--steps",
            "6",
            "--parallel",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "could not summarise" in result.output
    assert ProjectData(root).fits("Sample1"), "the fit is still recorded"
