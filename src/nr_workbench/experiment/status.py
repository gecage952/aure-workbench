"""When is a run finished arriving?

*Settled* -- no file has changed for a while -- is necessary and not enough.
The segments of one run are reduced as each angle finishes being measured: in
the reference corpus, 218386's three segments landed 15 and then 52 minutes
apart. A five-minute settle therefore calls the run complete twice before it
is, and a copy made then is a third of a measurement that fits perfectly well.

So *complete* needs evidence that the sequence has ended, and today there is
one kind that proves it: **the instrument moved on** -- a *later run whose
files have been reduced*. A later run that a feed merely *announces* does not
count: that is when the next acquisition started, and this run's last segment
may still be reducing.

**The header's plan can veto, but not prove.** The ``new_reduction`` header's
per-segment arrays appear to be sized by the reduction template, so they
would say how many segments there will be (:attr:`~nr_workbench.instrument.
header.ReducedHeader.n_segments`). When they say three and two are present,
the run is not complete, whatever else is true. But whether those arrays are
already full length in the *first* segment's file has not been checked on a
real file: if they grow as segments are reduced instead, a count read from
segment 1 is 1, and trusting it would copy a third of a measurement five
minutes after it landed. Until a real first-segment file settles that, a
fulfilled plan still waits for a later run (or a person).

A settled run without that evidence is *unconfirmed*. It is shown, it can be
assigned, and a person can confirm it -- but it is never copied on its own.
"""

from __future__ import annotations

from dataclasses import dataclass

from nr_workbench.arrival import settle_state
from nr_workbench.experiment.inventory import SourceRun

#: Every state a run can be in, in the order the page lists them.
STATES = (
    "awaiting",
    "arriving",
    "settling",
    "unconfirmed",
    "complete",
    "quarantined",
    "clock-skew",
)

#: How far in the future a file may be dated before the clocks are suspect.
#: File times come from the file server, "now" from this machine; a minute or
#: two of drift is ordinary and not worth a warning.
CLOCK_SKEW_TOLERANCE = 120.0


@dataclass(frozen=True)
class RunStatus:
    """What can be said about a run's arrival right now.

    Attributes:
        state: One of :data:`STATES`.
        reason: Why, in a sentence for the page.
        complete: Whether it may be copied without a person confirming.
        settled: Whether its files have stopped changing.
        quiet_for: Seconds since its files last changed.
    """

    state: str
    reason: str
    complete: bool = False
    settled: bool = False
    quiet_for: float = 0.0

    def as_dict(self) -> dict[str, object]:
        """Return the JSON form."""
        return {
            "state": self.state,
            "reason": self.reason,
            "complete": self.complete,
            "settled": self.settled,
            "quiet_for": round(self.quiet_for, 1),
        }


def judge(
    run: SourceRun | None,
    *,
    previous_fingerprint: str | None,
    now: float,
    settle_seconds: float,
    latest_reduced: int | None,
) -> RunStatus:
    """Judge one run.

    Args:
        run: What the source lists for it, or ``None`` if only a feed has
            announced it.
        previous_fingerprint: Its fingerprint on the previous poll.
        now: This machine's clock.
        settle_seconds: How long its files must be unchanged.
        latest_reduced: The highest run number with reduced files in the
            source, or ``None``.

    Returns:
        The status.
    """
    if run is None:
        return RunStatus(
            "awaiting",
            "announced, but no reduced files have appeared yet",
        )
    if run.problems:
        return RunStatus("quarantined", run.problems[0])
    if run.changed_at is not None and run.changed_at > now + CLOCK_SKEW_TOLERANCE:
        ahead = run.changed_at - now
        return RunStatus(
            "clock-skew",
            f"its files are dated {ahead:.0f}s in the future by the file server's "
            "clock, so nrw cannot tell when they stopped changing. Check this "
            "machine's clock against the data server's.",
        )

    state, quiet = settle_state(
        run.changed_at,
        run.fingerprint,
        previous_fingerprint,
        now=now,
        settle_seconds=settle_seconds,
    )
    if state != "settled":
        return RunStatus(
            state,
            f"unchanged for {max(quiet, 0.0):.0f}s of {settle_seconds:.0f}s",
            quiet_for=max(quiet, 0.0),
        )

    segments = run.segments
    planned = run.n_segments
    if planned is not None and segments != tuple(range(1, planned + 1)):
        # The veto: a plan that says more is coming wins over any later run.
        return RunStatus(
            "unconfirmed",
            f"{len(segments)} of {planned} planned segments are present. "
            "The rest may still be measured or reduced; if the measurement was "
            "stopped early, confirm it to use what is here.",
            settled=True,
            quiet_for=quiet,
        )

    if latest_reduced is not None and latest_reduced > run.last_subrun:
        return RunStatus(
            "complete",
            f"settled, and a later run ({latest_reduced}) has been reduced, so "
            "this measurement has finished",
            complete=True,
            settled=True,
            quiet_for=quiet,
        )
    if planned is not None:
        return RunStatus(
            "unconfirmed",
            f"all {planned} planned segment(s) are present and settled. It "
            "counts as complete once a later run has been reduced: the plan is "
            "read from the header, which is not yet known to be reliable in a "
            "run's first file. Confirm it to use it now.",
            settled=True,
            quiet_for=quiet,
        )
    return RunStatus(
        "unconfirmed",
        "settled, but nothing shows the measurement has ended: its header does "
        "not say how many segments were planned, and no later run has been "
        "reduced yet. Confirm it to use what is here.",
        settled=True,
        quiet_for=quiet,
    )
