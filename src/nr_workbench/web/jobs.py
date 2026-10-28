"""nrw commands run for the Experiment page: one at a time, as child processes.

A fit takes minutes to hours of CPU, so it runs neither in the request nor in
the server's process: nothing could stop it there, and a crash in bumps would
take the page down with it. A job is a short list of ``nrw`` commands --
``model generate``, then ``fit run`` -- run one after another in the project,
each in a process group of its own, so that Cancel stops every process a step
started, bumps' parallel workers included. :func:`run_nrw` runs one short
command the same way, for what must not wait behind a fit.

One at a time: two fits compete for the same cores, and neither finishes sooner
than if it had waited its turn. What a job printed is kept in
``.nrw/jobs/<id>.log`` and what it was in ``<id>.json``, so the page can say
what became of a job after ``nrw serve`` was restarted under it. The folder
ignores itself in git, and keeps the newest :data:`KEEP_JOBS` jobs.

Cancel sends SIGTERM, never SIGINT: bumps' DREAM takes a keyboard interrupt as
"stop sampling" and returns the chain so far, which ``nrw fit run`` would then
record as a finished fit.
"""

from __future__ import annotations

import codecs
import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from nr_workbench.bounded import TimedOut
from nr_workbench.project.layout import ProjectLayout

#: Seconds Cancel waits after SIGTERM before SIGKILL.
CANCEL_GRACE = 5.0

#: The most of a job's log one request returns, in bytes.
LOG_CHUNK = 256 * 1024

#: How many jobs' records and logs are kept; older ones go as a new job starts.
#: A DREAM log runs to megabytes, and only the newest job is ever shown.
KEEP_JOBS = 20

#: A job id: when it started, to the microsecond, so that the newest sorts
#: last; then six hex digits.
_ID_RE = re.compile(r"\d{8}T\d{12}Z-[0-9a-f]{6}")

_log = logging.getLogger(__name__)


class CommandRefused(Exception):
    """An nrw command declined; the message is its own."""


class JobBusy(Exception):
    """A job is running, and jobs run one at a time."""


class JobNotFound(LookupError):
    """No such job, or not the current one."""


def nrw_command(*args: str) -> list[str]:
    """The command line that runs ``nrw`` with *args*, as this server does.

    ``sys.executable -m nr_workbench`` is the interpreter serving the page, so
    the child is the same nrw, whatever is first on ``PATH``. ``-P`` keeps the
    working directory -- the project -- off ``sys.path``, as the ``nrw`` script
    does: a ``json.py`` or ``click.py`` beside an analysis must not be imported
    in place of the real module.

    Args:
        *args: The command and its arguments, as typed after ``nrw``.

    Returns:
        The argument list for :mod:`subprocess`.
    """
    return [sys.executable, "-P", "-m", "nr_workbench", *args]


def child_environment() -> dict[str, str]:
    """The environment for an nrw child: this one's, less what this process
    loaded from a ``.env`` file.

    The child reads the files itself; passed down, a value would win over the
    project's ``.env`` as though the shell had set it, and a language model
    chosen on the Settings page would reach no job until the server restarted.

    Returns:
        The environment to pass.
    """
    from nr_workbench import env

    return {
        key: value
        for key, value in os.environ.items()
        if key not in env.loaded_from_files()
    }


def run_nrw(
    root: Path, *args: str, timeout: float, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one short ``nrw`` command in the project, and wait for it.

    For what is not a job -- a check that must not queue behind a fit -- run as
    a job's step is: the same command line and environment, and a process group
    of its own, so that a command out of time is stopped whole. A language
    model's CLI it started, a grandchild, is killed with it, not left running
    and billing.

    Args:
        root: The project: the command's working directory.
        *args: The command and its arguments, as typed after ``nrw``.
        timeout: Seconds to wait.
        env: Variables to set for it, over :func:`child_environment`.

    Returns:
        How it ended, and what it printed.

    Raises:
        TimedOut: It did not finish in *timeout* seconds, and was killed.
    """
    process = subprocess.Popen(
        nrw_command(*args),
        cwd=root,
        env={**child_environment(), **(env or {})},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # The whole group, and at once: nothing it was doing is kept. Its id
        # is not reused while any member lives, which is when this reaches it.
        with suppress(ProcessLookupError, PermissionError):
            if hasattr(os, "killpg"):
                os.killpg(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            else:  # pragma: no cover - no process groups: the command itself
                process.kill()
        # Bounded too: a grandchild that left the group would hold the pipes.
        with suppress(subprocess.TimeoutExpired):
            process.communicate(timeout=CANCEL_GRACE)
        raise TimedOut(
            f"`nrw {' '.join(args)}` did not finish in {timeout:.0f} seconds, and "
            "was stopped."
        ) from None
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


@dataclass
class Job:
    """One job: what it runs, and how far it got.

    Attributes:
        id: Its id, also the name of its record and its log.
        label: What the page calls it, e.g. ``dream fit of oxide``.
        sample: The sample it is for.
        model: The model it fits.
        steps: The ``nrw`` arguments of each step, run in order.
        status: ``running``; then ``ok``, ``failed`` or ``cancelled``. Or
            ``detached``: it was still running when ``nrw serve`` stopped
            without stopping it, so how it ended is not known here -- a fit
            that finished is in Fits all the same.
        step: The step running, or the last one that ran, from 1.
        exit_code: The last step's exit status.
        started_at: When it started, UTC.
        finished_at: When it ended, UTC.
        fit_id: The fit it recorded, once known.
        same_as: The fit it was refused as identical to, when it was.
    """

    id: str
    label: str
    sample: str
    model: str
    steps: list[list[str]]
    status: str = "running"
    step: int = 0
    exit_code: int | None = None
    started_at: str = ""
    finished_at: str = ""
    fit_id: str | None = None
    same_as: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """The job as the page reads it."""
        return asdict(self)


class JobRunner:
    """Runs one job at a time in the project, and remembers the last.

    Args:
        root: Project root: the jobs' working directory.
        on_finished: Called with a job as it ends, from the job's thread and
            before its status changes, so a page that sees it end sees this
            too; returns what it found -- ``fit_id``, the fit the job
            recorded, and ``same_as``, the one it was refused as identical to.
    """

    def __init__(
        self,
        root: Path,
        *,
        on_finished: Callable[[Job], dict[str, str | None]] | None = None,
    ) -> None:
        self.root = Path(root)
        self.directory = ProjectLayout(root=self.root).state_dir / "jobs"
        self.on_finished = on_finished
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._cancelled = False
        self._thread: threading.Thread | None = None
        self._job = self._last_recorded()

    def current(self) -> Job | None:
        """The job running, or the last one that ran: a copy, taken whole."""
        with self._lock:
            return replace(self._job) if self._job is not None else None

    def start(
        self, *, label: str, sample: str, model: str, steps: Sequence[Sequence[str]]
    ) -> Job:
        """Start a job, unless one is running.

        Args:
            label: What the page calls it.
            sample: The sample it is for.
            model: The model it fits.
            steps: The ``nrw`` arguments of each step, run in order until one
                fails.

        Returns:
            The job, as it starts.

        Raises:
            JobBusy: A job is running.
            OSError: Its record could not be written.
        """
        with self._lock:
            if self._job is not None and self._job.status == "running":
                raise JobBusy(
                    f"The {self._job.label} is still running, and one job runs at "
                    "a time. Cancel it, or wait for it to finish."
                )
            job = Job(
                id=datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ-")
                + secrets.token_hex(3),
                label=label,
                sample=sample,
                model=model,
                steps=[list(step) for step in steps],
                started_at=_now(),
            )
            self._prepare()
            self._save(job)
            self._job = job
            self._cancelled = False
            self._thread = threading.Thread(
                target=self._run, args=(job,), name=f"nrw-job-{job.id}", daemon=True
            )
            self._thread.start()
            return replace(job)

    def cancel(self, job_id: str) -> Job:
        """Stop the running job: SIGTERM to its step's group, SIGKILL if it lingers.

        Args:
            job_id: The job, as the page names it.

        Returns:
            The job; ``cancelled`` once its thread has seen the step end.

        Raises:
            JobNotFound: *job_id* is not the current job.
        """
        with self._lock:
            job = self._job
            if job is None or job.id != job_id:
                raise JobNotFound(f"No job {job_id!r} is running here.")
            if job.status == "running":
                self._cancelled = True
            process = self._process
            snapshot = replace(job)
        if process is not None:
            _stop(process)
        return snapshot

    def stop(self) -> Job | None:
        """Cancel whatever is running, as ``nrw serve`` stops.

        Returns:
            The job stopped, or ``None`` when nothing was running.
        """
        job = self.current()
        if job is None or job.status != "running":
            return None
        self.cancel(job.id)
        thread = self._thread
        if thread is not None:
            thread.join(CANCEL_GRACE + 1)
        return job

    def log(self, job_id: str, offset: int | None = None) -> tuple[str, int]:
        """What a job printed, and the offset to read on from.

        Args:
            job_id: The job.
            offset: The byte to read from; ``None`` for the last chunk, from the
                start of a line -- what a page opening onto a long log shows.

        Returns:
            The text, and the byte offset just past what it covers.

        Raises:
            JobNotFound: No such job.
        """
        try:
            with self._path(job_id, ".log").open("rb") as handle:
                size = os.fstat(handle.fileno()).st_size
                tail = offset is None
                start = max(0, size - LOG_CHUNK if tail else min(offset, size))
                handle.seek(start)
                chunk = handle.read(LOG_CHUNK)
        except OSError as exc:
            raise JobNotFound(f"No log of job {job_id!r} can be read here.") from exc
        if tail and start > 0:
            newline = chunk.find(b"\n") + 1
            chunk, start = chunk[newline:], start + newline
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        text = decoder.decode(chunk)
        # A character the chunk cuts in two is read whole next time; a byte that
        # is not UTF-8 at all is shown as such, and counted as read.
        pending, _ = decoder.getstate()
        return text, start + len(chunk) - len(pending)

    def output(self, job_id: str) -> str:
        """All a job printed, as text: to read what it says, not to page through.

        Raises:
            JobNotFound: No such job.
        """
        try:
            data = self._path(job_id, ".log").read_bytes()
            return data.decode("utf-8", errors="replace")
        except OSError as exc:
            raise JobNotFound(f"No log of job {job_id!r} can be read here.") from exc

    # -- the job's own thread -------------------------------------------------

    def _run(self, job: Job) -> None:
        ended: str = "failed"
        found: dict[str, str | None] = {}
        try:
            ended = self._steps(job)
            if self.on_finished is not None:
                try:
                    found = self.on_finished(job)
                except Exception:
                    _log.exception("Finding the fit job %s recorded failed", job.id)
        except Exception:
            # A full disk, a quota: whatever it was, the job ends and the log
            # says why, rather than holding every later fit off until restart.
            _log.exception("Job %s stopped unexpectedly", job.id)
        finally:
            with self._lock:
                self._process = None
                job.status, job.finished_at = ended, _now()
                job.fit_id = found.get("fit_id")
                job.same_as = found.get("same_as")
                with suppress(OSError):
                    self._save(job)

    def _steps(self, job: Job) -> str:
        """Run the job's steps in order; how the job ended."""
        with self._path(job.id, ".log").open("ab") as log:
            for number, args in enumerate(job.steps, 1):
                process = self._launch(job, number, args, log)
                if process is None:
                    break
                code = process.wait()
                with self._lock:
                    self._process = None
                    job.exit_code = code
                    if self._cancelled or code != 0:
                        break
            with self._lock:
                cancelled = self._cancelled
            if cancelled:
                log.write(b"\nCancelled.\n")
                return "cancelled"
        return "ok" if job.exit_code == 0 else "failed"

    def _launch(
        self, job: Job, number: int, args: list[str], log: IO[bytes]
    ) -> subprocess.Popen[bytes] | None:
        """Start step *number*, unless the job was cancelled; ``None`` if not."""
        with self._lock:
            if self._cancelled:
                return None
            job.step = number
            # Written before the process starts: once it runs, nothing here
            # may fail and leave it unwatched.
            self._save(job)
            log.write(f"$ nrw {' '.join(args)}\n".encode())
            log.flush()
            try:
                self._process = subprocess.Popen(
                    nrw_command(*args),
                    cwd=self.root,
                    env=child_environment(),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    # Its own group, so Cancel reaches every process the step
                    # starts -- and Ctrl-C on nrw serve does not.
                    start_new_session=True,
                )
            except OSError as exc:
                log.write(f"could not start: {exc}\n".encode())
                job.exit_code = None
                return None
            return self._process

    # -- records ---------------------------------------------------------------

    def _path(self, job_id: str, suffix: str) -> Path:
        return self.directory / f"{job_id}{suffix}"

    def _prepare(self) -> None:
        """Make the folder, ignored by git, holding only the newest jobs."""
        self.directory.mkdir(parents=True, exist_ok=True)
        ignore = self.directory / ".gitignore"
        if not ignore.exists():
            ignore.write_bytes(b"*\n")
        recorded = sorted(
            path.stem
            for path in self.directory.glob("*.json")
            if _ID_RE.fullmatch(path.stem)
        )
        for old in recorded[: max(0, len(recorded) - KEEP_JOBS + 1)]:
            for suffix in (".json", ".log"):
                with suppress(OSError):
                    (self.directory / f"{old}{suffix}").unlink()

    def _save(self, job: Job) -> None:
        path = self._path(job.id, ".json")
        partial = path.with_name(f"{path.name}.partial")
        partial.write_bytes((json.dumps(job.as_dict(), indent=2) + "\n").encode())
        os.replace(partial, path)

    def _last_recorded(self) -> Job | None:
        """The newest job recorded, as :meth:`current` shows it after a restart.

        One the server that ran it never saw end is ``detached`` -- here, not on
        disk: a read-only server writes nothing, and a second server on the
        project must not rewrite the first one's job.
        """
        try:
            recorded = sorted(
                path
                for path in self.directory.glob("*.json")
                if _ID_RE.fullmatch(path.stem)
            )
        except OSError:
            return None
        if not recorded:
            return None
        try:
            job = Job(**json.loads(recorded[-1].read_bytes().decode("utf-8")))
        except (OSError, ValueError, TypeError):
            return None  # unreadable: nothing is claimed about it
        if job.id != recorded[-1].stem or not isinstance(job.steps, list):
            return None
        if job.status == "running":
            job.status = "detached"
        return job


def _stop(process: subprocess.Popen[bytes]) -> None:
    """SIGTERM to the step's process group; SIGKILL if it outlives the grace."""
    _signal(process, signal.SIGTERM)

    def kill_if_alive() -> None:
        if process.poll() is None:
            _signal(process, getattr(signal, "SIGKILL", signal.SIGTERM))

    timer = threading.Timer(CANCEL_GRACE, kill_if_alive)
    timer.daemon = True
    timer.start()


def _signal(process: subprocess.Popen[bytes], sig: int) -> None:
    # After the job thread's wait() has returned the leader is reaped, and this
    # says so. While it waits, poll() cannot tell -- but a process group's id is
    # not reused while any of its members lives, which is when a signal is sent.
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError, PermissionError):
        if hasattr(os, "killpg"):
            os.killpg(process.pid, sig)
        else:  # no process groups: the step itself
            process.send_signal(sig)


def _now() -> str:
    # The fit record's own format, so a job's start and its fit's compare.
    from nr_workbench.provenance.record import format_timestamp, utc_now

    return format_timestamp(utc_now())
