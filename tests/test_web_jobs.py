"""The page's job runner: steps in order, one job at a time, and Cancel.

Each step here is a small Python program standing in for an ``nrw`` command,
so what is tested is the runner: order, output, stopping, and what a restart
finds. The real commands are run end to end in ``test_web_models.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from nr_workbench.web import jobs as jobs_module
from nr_workbench.web.jobs import JobBusy, JobRunner


@pytest.fixture
def programs(monkeypatch):
    """Run each step's first argument as a Python program, not as nrw."""
    monkeypatch.setattr(
        jobs_module,
        "nrw_command",
        lambda code, *rest: [sys.executable, "-c", textwrap.dedent(code), *rest],
    )


@pytest.fixture
def runner(tmp_path: Path, programs) -> Iterator[JobRunner]:
    """A runner in *tmp_path*; whatever it still runs is stopped afterwards, so a
    failing test leaves no process behind to slow the next one."""
    made = JobRunner(tmp_path)
    yield made
    made.stop()


def ended(runner: JobRunner, timeout: float = 20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = runner.current()
        if job is not None and job.status != "running":
            return job
        time.sleep(0.05)
    raise AssertionError(f"the job did not end: {runner.current()}")


def wait_for(path: Path, timeout: float = 20) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file() and path.read_text():
            return path.read_text()
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


def start(runner: JobRunner, *steps: list[str]):
    return runner.start(label="amoeba fit of m", sample="S1", model="m", steps=steps)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def record(directory: Path, job_id: str, **fields) -> Path:
    """A job's record, as a server would have left it."""
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": job_id,
        "label": "dream fit of m",
        "sample": "S1",
        "model": "m",
        "steps": [["fit", "run", "samples/S1/models/m.py"]],
        "status": "running",
        "step": 1,
        **fields,
    }
    path = directory / f"{job_id}.json"
    path.write_text(json.dumps(payload))
    return path


def test_the_steps_run_in_order_and_what_they_print_is_kept(
    tmp_path: Path, runner: JobRunner
) -> None:
    job = start(runner, ["print('generated')"], ["print('fitted')"])
    done = ended(runner)
    text, offset = runner.log(job.id, 0)

    assert (done.status, done.step, done.exit_code) == ("ok", 2, 0)
    assert text.index("generated") < text.index("fitted")
    assert text.startswith("$ nrw print('generated')\n")
    # Read on from where the page left off: nothing new.
    assert runner.log(job.id, offset) == ("", offset)
    recorded = json.loads((tmp_path / ".nrw" / "jobs" / f"{job.id}.json").read_text())
    assert recorded["status"] == "ok"


def test_a_step_that_fails_stops_the_job(runner: JobRunner) -> None:
    job = start(runner, ["import sys; print('no'); sys.exit(3)"], ["print('never')"])
    done = ended(runner)

    assert (done.status, done.step, done.exit_code) == ("failed", 1, 3)
    assert "$ nrw print('never')" not in runner.output(job.id)


def test_cancel_stops_the_step_and_every_process_it_started(
    tmp_path: Path, runner: JobRunner
) -> None:
    # A step that starts a worker of its own, as bumps does with --parallel.
    pids = tmp_path / "pids"
    step = f"""
        import os, subprocess, sys, time
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open({str(pids)!r}, "w").write(f"{{os.getpid()}} {{worker.pid}}")
        time.sleep(60)
    """
    job = start(runner, [step], ["print('never')"])
    parent, worker = map(int, wait_for(pids).split())

    runner.cancel(job.id)
    done = ended(runner)

    assert done.status == "cancelled"
    assert done.step == 1
    deadline = time.monotonic() + 10
    while (alive(parent) or alive(worker)) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not alive(parent) and not alive(worker)
    assert runner.output(job.id).rstrip().endswith("Cancelled.")


def test_cancel_sends_sigterm_and_wins_over_a_step_that_then_exits_cleanly(
    tmp_path: Path, runner: JobRunner
) -> None:
    # bumps' DREAM catches KeyboardInterrupt and returns the chain so far --
    # exit 0 -- which `nrw fit run` would record as a finished fit. The signal
    # sent must be SIGTERM, and the job cancelled however the step then exits.
    heard = tmp_path / "heard"
    step = f"""
        import signal, sys, time
        def note(number, frame):
            open({str(heard)!r}, "w").write(signal.Signals(number).name)
            sys.exit(0)
        signal.signal(signal.SIGINT, note)
        signal.signal(signal.SIGTERM, note)
        open({str(heard)!r} + ".ready", "w").write("ready")
        time.sleep(60)
    """
    job = start(runner, [step])
    wait_for(Path(f"{heard}.ready"))

    runner.cancel(job.id)
    done = ended(runner)

    assert wait_for(heard) == "SIGTERM"
    assert (done.status, done.exit_code) == ("cancelled", 0)


def test_cancel_between_steps_starts_no_more_of_them(
    tmp_path: Path, runner: JobRunner
) -> None:
    ready, go, marker = tmp_path / "ready", tmp_path / "go", tmp_path / "marker"
    # Step 1 outlives the signal, and ends cleanly once the job is cancelled.
    first = f"""
        import pathlib, signal, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        pathlib.Path({str(ready)!r}).write_text("ready")
        while not pathlib.Path({str(go)!r}).exists():
            time.sleep(0.02)
    """
    second = f"import pathlib; pathlib.Path({str(marker)!r}).write_text('ran')"
    job = start(runner, [first], [second])
    wait_for(ready)

    runner.cancel(job.id)
    go.write_text("go")
    done = ended(runner)

    assert (done.status, done.step, done.exit_code) == ("cancelled", 1, 0)
    assert not marker.exists()
    assert runner.output(job.id).rstrip().endswith("Cancelled.")


def test_one_job_runs_at_a_time(runner: JobRunner) -> None:
    job = start(runner, ["import time; time.sleep(60)"])

    with pytest.raises(JobBusy, match="one job runs at a time"):
        start(runner, ["print('second')"])

    runner.cancel(job.id)
    ended(runner)
    start(runner, ["print('second')"])
    assert ended(runner).status == "ok"


def test_stopping_the_server_stops_its_job(runner: JobRunner) -> None:
    start(runner, ["import time; time.sleep(60)"])

    stopped = runner.stop()

    assert stopped is not None
    assert runner.current().status == "cancelled"
    assert runner.stop() is None  # nothing left to stop


# --------------------------------------------------------------------------
# What a job printed
# --------------------------------------------------------------------------


def test_the_log_reads_back_whole_across_chunks_and_bytes_that_are_not_utf8(
    tmp_path: Path, runner: JobRunner, monkeypatch
) -> None:
    # Chunks of 7 bytes cut the two-byte characters in two; a byte that is not
    # UTF-8 at all is shown as such, and read once.
    monkeypatch.setattr(jobs_module, "LOG_CHUNK", 7)
    step = (
        "import sys; sys.stdout.buffer.write("
        "'chi² = 1.2 µm\\n'.encode() * 3 + b'\\xb5 not text\\n')"
    )
    job = start(runner, [step])
    ended(runner)
    data = (tmp_path / ".nrw" / "jobs" / f"{job.id}.log").read_bytes()

    text, offset = "", 0
    while True:
        more, offset = runner.log(job.id, offset)
        if not more:
            break
        text += more

    assert text == data.decode("utf-8", errors="replace")
    assert offset == len(data)


def test_a_page_opening_onto_a_long_log_reads_its_end_from_a_line(
    tmp_path: Path, runner: JobRunner, monkeypatch
) -> None:
    monkeypatch.setattr(jobs_module, "LOG_CHUNK", 64)
    job = start(runner, ["for i in range(100): print(f'line {i}')"])
    ended(runner)
    data = (tmp_path / ".nrw" / "jobs" / f"{job.id}.log").read_text()

    text, offset = runner.log(job.id)

    assert offset == len(data.encode())
    assert text.startswith("line ") and text.endswith("line 99\n")
    assert data.endswith(text)


# --------------------------------------------------------------------------
# What the job recorded, and when that is known
# --------------------------------------------------------------------------


def test_the_fit_is_found_before_the_job_is_seen_to_end(
    tmp_path: Path, programs
) -> None:
    looking, release = threading.Event(), threading.Event()

    def find(job) -> dict:
        looking.set()
        release.wait(20)
        return {"fit_id": "20260929-120000Z-abcdef01"}

    runner = JobRunner(tmp_path, on_finished=find)
    try:
        start(runner, ["print('fitted')"])
        assert looking.wait(20)

        assert runner.current().status == "running"
        release.set()
        done = ended(runner)
    finally:
        release.set()
        runner.stop()

    assert (done.status, done.fit_id) == ("ok", "20260929-120000Z-abcdef01")


def test_a_failure_to_find_the_fit_still_ends_the_job(tmp_path: Path, programs) -> None:
    def find(job) -> dict:
        raise ValueError("the index is not readable")

    runner = JobRunner(tmp_path, on_finished=find)
    try:
        start(runner, ["print('fitted')"])
        done = ended(runner)
        start(runner, ["print('again')"])  # not held off by the one before
        again = ended(runner)
    finally:
        runner.stop()

    assert (done.status, done.fit_id) == ("ok", None)
    assert again.status == "ok"


# --------------------------------------------------------------------------
# After a restart
# --------------------------------------------------------------------------


def test_a_job_no_server_saw_end_is_detached_and_another_can_start(
    tmp_path: Path, programs
) -> None:
    path = record(tmp_path / ".nrw" / "jobs", "20260929T120000000000Z-abcdef")

    runner = JobRunner(tmp_path)
    try:
        assert runner.current().status == "detached"
        # Said here, never written back: a read-only server writes nothing,
        # and a second server must not rewrite the first one's job.
        assert json.loads(path.read_text())["status"] == "running"
        start(runner, ["print('after')"])
        assert ended(runner).status == "ok"
    finally:
        runner.stop()


def test_the_job_started_last_is_the_one_shown_after_a_restart(tmp_path: Path) -> None:
    jobs = tmp_path / ".nrw" / "jobs"
    # Started in the same second; the microseconds order them.
    record(jobs, "20260929T120000000001Z-ffffff", status="failed")
    record(jobs, "20260929T120000000002Z-000000", status="running")

    assert JobRunner(tmp_path).current().id == "20260929T120000000002Z-000000"


def test_a_read_only_jobs_folder_is_read_and_never_written(tmp_path: Path) -> None:
    jobs = tmp_path / ".nrw" / "jobs"
    path = record(jobs, "20260929T120000000000Z-abcdef")
    before = path.read_text()
    jobs.chmod(0o500)
    try:
        runner = JobRunner(tmp_path)
        assert runner.current().status == "detached"
    finally:
        jobs.chmod(0o700)
    assert path.read_text() == before


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps(
            {
                "id": "../../escape",
                "label": "x",
                "sample": "S1",
                "model": "m",
                "steps": [],
                "status": "running",
            }
        ),
        json.dumps(
            {
                "id": 5,
                "label": "x",
                "sample": "S1",
                "model": "m",
                "steps": [],
                "status": "running",
            }
        ),
        json.dumps({"id": "20260929T120000000000Z-abcdef", "unknown": True}),
    ],
)
def test_a_record_that_cannot_be_read_shows_no_job_and_stops_nothing(
    tmp_path: Path, content: str
) -> None:
    jobs = tmp_path / ".nrw" / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "20260929T120000000000Z-abcdef.json").write_text(content)

    assert JobRunner(tmp_path).current() is None


# --------------------------------------------------------------------------
# The folder, which the page reads from
# --------------------------------------------------------------------------


def test_the_jobs_are_kept_out_of_git_in_any_project(
    tmp_path: Path, runner: JobRunner
) -> None:
    # git as on a fresh machine: no global excludes to hide a missing rule.
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, env=env)
    job = start(runner, ["print('a long DREAM log')"])
    ended(runner)

    ignored = subprocess.run(
        ["git", "check-ignore", "-v", f".nrw/jobs/{job.id}.log"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    ).stdout

    assert ignored.startswith(".nrw/jobs/.gitignore:1:*")


def test_only_the_newest_jobs_are_kept(
    tmp_path: Path, runner: JobRunner, monkeypatch
) -> None:
    monkeypatch.setattr(jobs_module, "KEEP_JOBS", 3)
    jobs = tmp_path / ".nrw" / "jobs"
    for n in range(5):
        job_id = f"20260929T12000000000{n}Z-abcdef"
        record(jobs, job_id, status="ok")
        (jobs / f"{job_id}.log").write_text("old")

    job = start(runner, ["print('new')"])
    ended(runner)

    kept = sorted(p.stem for p in jobs.glob("*.json"))
    assert kept == [
        "20260929T120000000003Z-abcdef",
        "20260929T120000000004Z-abcdef",
        job.id,
    ]
    assert not (jobs / "20260929T120000000000Z-abcdef.log").exists()


def test_nrw_serve_stops_the_job_it_ran_when_it_stops(
    tmp_path: Path, programs, monkeypatch
) -> None:
    from click.testing import CliRunner
    from flask import Flask

    from nr_workbench.cli import main

    root = tmp_path / "proj"
    assert CliRunner().invoke(main, ["init", str(root)]).exit_code == 0
    started = {}

    def run(self, **kwargs) -> None:
        # What a person does while the server runs: start a fit, then Ctrl-C.
        started["runner"] = self.config["NRW_MODELS"].jobs
        start(started["runner"], ["import time; time.sleep(60)"])

    monkeypatch.setattr(Flask, "run", run)

    result = CliRunner().invoke(main, ["serve", "--root", str(root)])

    assert result.exit_code == 0, result.output
    assert "Stopped the amoeba fit of m." in result.output
    assert started["runner"].current().status == "cancelled"
