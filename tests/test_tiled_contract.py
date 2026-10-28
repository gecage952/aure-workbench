"""`nrw tiled` against a real Tiled 0.2.18: run with `pytest -m tiled_server`.

The server runs apart from nrw's environment, through `uvx --from
"tiled[server]==0.2.18"`, on this machine's loopback, with its key in
`TILED_SINGLE_USER_API_KEY` (never on its command line, and never
`--public`). The first run downloads it. Skipped without uv, or without the
client (the `tiled` extra).

What this tests is a push and a status as the official client and the
server answer them: connecting with a key, reading the experiment's node,
making containers with specs, replacing metadata, reading it back. Not
tested here: a Globus login (no test server has one), the tags (a
single-user server has no access policy, so it keeps none: the test stands in
for the policy's where nrw reads them), and `pull --data` (whose `bytes`
nodes another process registers). Those are the fakes' to test, in
tests/test_tiled_sync.py.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from nr_workbench.cli import main
from nr_workbench.tiled import context as context_module

from .test_tiled_sync import catalogued, own_settings

pytestmark = pytest.mark.tiled_server

#: Three segments of one REF_L measurement, as the reduction wrote them.
REFERENCE = Path(__file__).parent / "data" / "reference" / "steady"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def local_tiled(tmp_path_factory) -> Iterator[tuple[str, str]]:
    """A Tiled 0.2.18 server of the test's own, and its key."""
    uvx = shutil.which("uvx")
    if uvx is None:
        pytest.skip("needs uv: the server runs apart from nrw's environment")
    pytest.importorskip("tiled.client")
    import httpx

    key = secrets.token_hex(16)  # the server takes only letters and digits
    port = _free_port()
    log = tmp_path_factory.mktemp("tiled") / "server.log"
    with log.open("w") as out:
        server = subprocess.Popen(
            [
                uvx,
                "--from",
                "tiled[server]==0.2.18",
                "tiled",
                "serve",
                "catalog",
                "--temp",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env={**os.environ, "TILED_SINGLE_USER_API_KEY": key},
            stdout=out,
            stderr=subprocess.STDOUT,
        )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 300  # the first run downloads it
        while True:
            try:
                if httpx.get(f"{url}/api/v1/", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if server.poll() is not None or time.monotonic() > deadline:
                pytest.fail(f"Tiled did not start:\n{log.read_text()[-2000:]}")
            time.sleep(0.5)
        yield url, key
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


@pytest.fixture
def on_local_tiled(local_tiled, project: Path, monkeypatch, tmp_path: Path):
    """The person's settings naming the local server, and the experiment's
    node on it, made as another process makes it."""
    from tiled.client import from_uri

    url, key = local_tiled
    own_settings(f"TILED_URL={url}\nTILED_API_KEY={key}\n")
    monkeypatch.setenv("TILED_CACHE_DIR", str(tmp_path / "tiled-cache"))
    # No access policy, so no tags: what the policy would have given.
    monkeypatch.setattr(context_module, "tags_of", lambda node: ["isaac"])
    root = from_uri(url, api_key=key)
    ipts = f"IPTS-{secrets.randbelow(10**8):08d}"
    for part in ("projects", "isaac"):
        root = root[part] if part in root else root.create_container(part)
    root.create_container(ipts)
    toml = project / "nrw.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace("IPTS-00001", ipts), encoding="utf-8"
    )
    return from_uri(url, api_key=key)[f"projects/isaac/{ipts}"]


def nrw(project: Path, *args: str):
    return CliRunner().invoke(main, ["tiled", *args, "--root", str(project)])


def real_fit(project: Path) -> float:
    """A starred fit of the three segments, exported as bumps exports a fit,
    at its starting values: a record of a real fit's size, whose problem file
    refl1d loads. Its chisq."""
    import hashlib

    import numpy as np
    from bumps.serialize import serialize
    from refl1d.names import SLD, Experiment, FitProblem, QProbe

    from .test_tiled_analysis import fitted, starred

    steady = project / "samples" / "S1" / "data" / "steady"
    steady.mkdir(parents=True, exist_ok=True)
    thf, cu, si = SLD("THF", rho=6.2), SLD("Cu", rho=6.5), SLD("Si", rho=2.07)
    stack = thf(0, 5) | cu(500, 8) | si
    stack["Cu"].thickness.range(400, 600)
    experiments, data = [], []
    for source in sorted(REFERENCE.glob("REFL_218386_*_partial.txt")):
        copy = steady / source.name
        shutil.copyfile(source, copy)
        q, r, dr, dq = np.loadtxt(copy).T[:4]
        experiments.append(Experiment(sample=stack, probe=QProbe(q, dq, R=r, dR=dr)))
        data.append(
            {
                "role": f"data:steady:{copy.stem}",
                "path": copy.relative_to(project).as_posix(),
                "sha256": hashlib.sha256(copy.read_bytes()).hexdigest(),
                "bytes": copy.stat().st_size,
            }
        )
    problem = FitProblem(experiments)
    directory = fitted(project, chisq=problem.chisq())
    inputs = json.loads((directory / "inputs.json").read_text())
    scripts = [entry for entry in inputs["inputs"] if entry["role"] == "script"]
    (directory / "inputs.json").write_text(json.dumps({"inputs": data + scripts}))
    fit = directory / "fit"
    for path in fit.iterdir():
        path.unlink()
    problem.save(str(fit / "film"))
    (fit / "film.json").write_text(json.dumps(serialize(problem)))
    (fit / "film.par").write_text(
        "".join(
            f"{n} {v:.15g}\n"
            for n, v in zip(problem.labels(), problem.getp(), strict=True)
        )
    )
    starred(project)
    return problem.chisq()


def test_a_push_writes_the_records_and_reads_them_back(
    project: Path, on_local_tiled
) -> None:
    catalogued(project)

    shown = nrw(project, "push", "--yes")
    status = json.loads(nrw(project, "status", "--json").output)

    assert shown.exit_code == 0, shown.output
    assert "Pushed 3 record(s)." in shown.output
    context = on_local_tiled["context"]
    assert context.metadata["written_by"] == "nr-workbench"
    assert [(s.name, s.version) for s in context.specs] == [("context-experiment", "1")]
    run = context["runs/234277-steady"]
    assert (run.metadata["run"], run.metadata["condition"]) == (234277, "OCV")
    assert [(s.name, s.version) for s in run.specs] == [("context-run", "1")]
    assert context["samples/S1"].metadata["title"] == "Cu/Pt"
    assert len(status["records"]["same"]) == 3 and status["only_on_tiled"] == []


def test_the_experiments_context_is_written_into_contexts_own_metadata(
    project: Path, on_local_tiled
) -> None:
    """Made with context/, then merged over it: the one replace of context/'s
    own metadata a push makes."""
    from nr_workbench.experiment.model import ExperimentChange
    from nr_workbench.experiment.store import ParquetCatalogStore

    store = ParquetCatalogStore.for_project(project)
    store.update(experiment=ExperimentChange(0, {"summary": "First."}))
    nrw(project, "push", "--yes")
    store.update(experiment=ExperimentChange(1, {"summary": "Second."}))

    shown = nrw(project, "push", "--yes")
    status = json.loads(nrw(project, "status", "--json").output)

    assert shown.exit_code == 0, shown.output
    metadata = on_local_tiled["context"].metadata
    assert metadata["experiment"]["summary"] == "Second."
    assert metadata["written_by"] == "nr-workbench"
    assert status["experiment_context"]["state"] == "same"


def test_a_second_push_writes_only_what_changed_and_keeps_others_fields(
    project: Path, on_local_tiled
) -> None:
    from nr_workbench.experiment.model import SampleChange
    from nr_workbench.experiment.store import ParquetCatalogStore

    catalogued(project)
    nrw(project, "push", "--yes")
    sample = on_local_tiled["context/samples/S1"]
    sample.replace_metadata(metadata={**sample.metadata, "beamline_note": "theirs"})
    ParquetCatalogStore.for_project(project).update(
        samples=[SampleChange("S1", 1, {"title": "Cu/Pt, annealed"})]
    )

    same = nrw(project, "push", "--yes", "--dry-run")
    changed = nrw(project, "push", "--yes")

    assert "0 new, 1 changed, 2 the same" in same.output
    assert changed.exit_code == 0, changed.output
    kept = on_local_tiled["context/samples/S1"].metadata
    assert (kept["title"], kept["beamline_note"]) == ("Cu/Pt, annealed", "theirs")


def test_a_record_of_a_real_fits_size_is_kept_as_it_was_written(
    project: Path, on_local_tiled
) -> None:
    """Three segments make a record of about 270 kB: the server gives it back
    as it was written, so a push again finds it the same."""
    from nr_workbench.tiled.analysis import fit_records
    from nr_workbench.tiled.context import Remote

    from .test_tiled_analysis import A

    real_fit(project)

    pushed = nrw(project, "push", "--only", "fits", "--yes")
    again = nrw(project, "push", "--only", "fits", "--dry-run")

    assert pushed.exit_code == 0, pushed.output
    kept = dict(on_local_tiled[f"analysis/S1/{A}"].metadata)
    (record,), _ = fit_records(project, Remote())
    assert len(json.dumps(kept)) > 250_000
    assert kept == {**record.fields, "pushed_at": kept["pushed_at"]}
    assert "0 new, 0 changed, 1 the same" in again.output, again.output


def test_a_fit_pushed_is_pulled_into_a_colleagues_project_as_it_was_recorded(
    project: Path, on_local_tiled, tmp_path_factory, context
) -> None:
    """The hand-over: the whole record through the server, and the fit rebuilt
    from it, whose problem file bumps loads at the chisq recorded."""
    from nr_workbench.commands.init_cmd import plan_project_files
    from nr_workbench.experiment.store import ParquetCatalogStore
    from nr_workbench.fitting.runner import load_problem_file
    from nr_workbench.project.config import load_config
    from nr_workbench.project.scaffold import apply_scaffold
    from nr_workbench.provenance.index import FitIndex

    from .test_tiled_analysis import SCRIPT, A

    catalogued(project)
    real_fit(project)
    pushed = nrw(project, "push", "--yes")
    colleague = tmp_path_factory.mktemp("colleague")
    apply_scaffold(colleague, plan_project_files(context))
    toml = colleague / "nrw.toml"
    toml.write_text(
        toml.read_text(encoding="utf-8").replace(
            "IPTS-00001", load_config(project).ipts
        ),
        encoding="utf-8",
    )

    shown = nrw(colleague, "pull", "--context", "--fits", "--yes")

    assert pushed.exit_code == 0, pushed.output
    assert shown.exit_code == 0, shown.output
    ours = project / "samples" / "S1" / "results" / A
    theirs = colleague / "samples" / "S1" / "results" / A
    for name in ("model.py", "fit/film.json", "fit/film.par", "fit/film-3-refl.dat"):
        assert (theirs / name).read_bytes() == (ours / name).read_bytes(), name
    for source in REFERENCE.glob("REFL_218386_*_partial.txt"):
        read = f"samples/S1/data/steady/{source.name}"
        assert (colleague / read).read_bytes() == source.read_bytes()
        assert (theirs / "data" / read).read_bytes() == source.read_bytes()
    recorded = FitIndex(colleague / ".nrw" / "index.jsonl").find(A)["chisq"]
    loaded = load_problem_file(theirs / "fit" / "film.json")
    assert loaded.problem.chisq() == pytest.approx(recorded, rel=1e-12)
    models = colleague / "samples" / "S1" / "models"
    assert (models / "film.py").read_bytes() == SCRIPT
    ours, theirs = (
        ParquetCatalogStore.for_project(root).load() for root in (project, colleague)
    )
    assert {k: e.content() for k, e in theirs.runs.items()} == {
        k: e.content() for k, e in ours.runs.items()
    }
