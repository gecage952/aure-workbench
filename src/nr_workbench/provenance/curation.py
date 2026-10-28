"""Which fits a person stars, finalizes, sets aside, deletes or publishes.

Curation is recorded the way a promotion always has been: appended to
``.nrw/index.jsonl`` and never rewritten, with who and when. It merges in git,
and a fit's history -- the bad ones included -- stays readable.

**One reader.** :func:`replay` is the only code that reads these events: what
a fit is now is the last word on it -- starred until unstarred, discarded until
restored -- and a label is held by the last fit of its sample promoted to it,
a fit with no sample in a slot of its own. Everything that asks "is this fit
final" asks the replay, because every reader that worked it out for itself got
it wrong for a project with more than one sample.

**Discard, then delete.** Discarding hides a fit from the listings and keeps
every file, with a reason. Deleting frees the disk: a second, explicit step,
only for a fit already discarded, and only when nothing uses it -- see
:func:`used_by`. The index keeps the record that it ran.

**The answer is never discarded.** The fit that is ``final`` for its sample is
not set aside; finalize another first. A discarded fit is not finalized or
starred until it is restored.

**A push is recorded before it runs.** Its attempt -- the files it sends, and
their digests -- is appended first, and its outcome after: a push cancelled or
killed half way has still made records a portal keeps, and the index says it
may have.

These are a person's decisions: each refuses under ``NRW_AGENT``, as the
commands and the read-only web server do. Nothing here imports click; the
commands and the page wrap it.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nr_workbench.project.layout import ProjectLayout
from nr_workbench.provenance.index import EVENT_PROMOTE, EVENT_SUPERSEDE, FitIndex
from nr_workbench.provenance.lookup import fit_dir as find_fit_dir
from nr_workbench.provenance.record import format_timestamp, utc_now

EVENT_STAR = "star"
EVENT_UNSTAR = "unstar"
EVENT_DISCARD = "discard"
EVENT_RESTORE = "restore"
EVENT_DELETE = "delete"
#: Records of the fit sent to the ISAAC Portal, which keeps them: one event as
#: a push starts, and one with its outcome, sharing an ``attempt`` id.
EVENT_PUBLISH = "publish"

#: The label a fit is finalized with: the answer, for its sample.
FINAL = "final"

#: The longest reason kept: it is read in a listing, beside the fit.
MAX_REASON = 500


class CurationRefused(ValueError):
    """Not done: the message says why, and what to do instead."""


class NoSuchFit(CurationRefused):
    """No fit matches, or the prefix matches several."""


class InputsChanged(CurationRefused):
    """Its inputs changed since it ran: finalizing it needs saying so."""

    #: What the request must say to go ahead, as the page's API reports it.
    needs = "force"


@dataclass(frozen=True)
class Curation:
    """What a person has said about one fit, as it stands.

    Attributes:
        starred: Whether it is starred.
        discarded: Why, by whom and when it was set aside; ``None`` if not.
        deleted: By whom and when its files were deleted; ``None`` if not.
        promotions: Each label it holds now, with the promotion that gave it.
        published: Each push of its records, oldest first: when, by whom,
            where, the files sent, the records the portal made, and
            ``complete`` -- absent for a push whose outcome was never
            recorded, which may have made records all the same.
    """

    starred: bool = False
    discarded: dict[str, Any] | None = None
    deleted: dict[str, Any] | None = None
    promotions: dict[str, dict[str, Any]] = field(default_factory=dict)
    published: tuple[dict[str, Any], ...] = ()

    @property
    def labels(self) -> tuple[str, ...]:
        """The labels it holds now -- ``final``, say."""
        return tuple(sorted(self.promotions))

    def as_dict(self) -> dict[str, Any]:
        """The JSON form."""
        return {
            "starred": self.starred,
            "discarded": self.discarded,
            "deleted": self.deleted,
            "labels": list(self.labels),
            "published": list(self.published),
        }


#: The curation of a fit nothing has been said about.
NONE = Curation()


@dataclass(frozen=True)
class CurationState:
    """Every fit's curation, and who holds each label -- the one reading.

    Attributes:
        fits: Fit id to its curation, for the fits something was said about.
        holders: ``(sample, label)`` to the promotion holding it; ``sample``
            is ``None`` for a fit with no sample, a slot of its own.
    """

    fits: dict[str, Curation]
    holders: dict[tuple[str | None, str], dict[str, Any]]

    def of(self, fit_id: Any) -> Curation:
        """One fit's curation; :data:`NONE` for a fit nothing was said about."""
        return self.fits.get(str(fit_id), NONE)

    def holder(self, sample: Any, label: str = FINAL) -> dict[str, Any] | None:
        """The promotion holding *label* for *sample*, if any."""
        return self.holders.get((_sample_key(sample), label))


def replay(entries: list[dict[str, Any]]) -> CurationState:
    """Every fit's curation, replayed from the index's entries in order.

    Args:
        entries: The index, oldest first (:meth:`FitIndex.entries`).

    Returns:
        The state: each fit's curation, and each label's holder.
    """
    starred: dict[str, bool] = {}
    discarded: dict[str, dict[str, Any]] = {}
    deleted: dict[str, dict[str, Any]] = {}
    attempts: dict[str, dict[str, dict[str, Any]]] = {}
    holders: dict[tuple[str | None, str], dict[str, Any]] = {}
    for entry in entries:
        event, fit_id = entry.get("event"), str(entry.get("fit_id") or "")
        if not fit_id:
            continue
        if event in (EVENT_STAR, EVENT_UNSTAR):
            starred[fit_id] = event == EVENT_STAR
        elif event == EVENT_DISCARD:
            discarded[fit_id] = {k: entry.get(k) for k in ("reason", "who", "at")}
        elif event == EVENT_RESTORE:
            discarded.pop(fit_id, None)
        elif event == EVENT_DELETE:
            deleted[fit_id] = {k: entry.get(k) for k in ("who", "at", "partial")}
        elif event == EVENT_PROMOTE:
            key = (_sample_key(entry.get("sample")), str(entry.get("label")))
            holders[key] = entry
        elif event == EVENT_PUBLISH:
            # An attempt, then its outcome; an event with no attempt id is a
            # push recorded whole, as nrw first recorded them.
            fit_attempts = attempts.setdefault(fit_id, {})
            key = str(entry.get("attempt") or f"#{len(fit_attempts)}")
            push = fit_attempts.setdefault(key, {"attempt": key})
            push.update(
                {
                    k: entry[k]
                    for k in ("at", "who", "portal", "files", "records", "complete")
                    if k in entry and not (k in ("at", "who") and k in push)
                }
            )
    promotions: dict[str, dict[str, dict[str, Any]]] = {}
    for (_, label), promotion in holders.items():
        promotions.setdefault(str(promotion.get("fit_id")), {})[label] = promotion
    fits = {
        fit_id: Curation(
            starred=starred.get(fit_id, False),
            discarded=discarded.get(fit_id),
            deleted=deleted.get(fit_id),
            promotions=promotions.get(fit_id, {}),
            published=tuple(attempts.get(fit_id, {}).values()),
        )
        for fit_id in {*starred, *discarded, *deleted, *promotions, *attempts}
    }
    return CurationState(fits=fits, holders=holders)


def curation_of(entries: list[dict[str, Any]]) -> dict[str, Curation]:
    """Each fit's curation: :func:`replay`'s ``fits``."""
    return replay(entries).fits


# --------------------------------------------------------------------------
# Deciding
# --------------------------------------------------------------------------


def star(layout: ProjectLayout, fit_id: str, *, starred: bool = True) -> str:
    """Star a fit, or take its star away.

    Args:
        layout: The project.
        fit_id: The fit, or a unique prefix of its id.
        starred: Star it (``True``) or unstar it.

    Returns:
        The fit's whole id; ``""`` when nothing changed.

    Raises:
        CurationRefused: No such fit, it is discarded, or this runs unattended.
    """
    _refuse_if_agent("curate")
    index, entry, state = _look_up(layout, fit_id)
    if starred and state.discarded:
        raise CurationRefused(
            f"{entry['fit_id']} is discarded: restore it before starring it."
        )
    if state.starred == starred:
        return ""
    _append(index, entry, EVENT_STAR if starred else EVENT_UNSTAR)
    return str(entry["fit_id"])


def discard(layout: ProjectLayout, fit_id: str, *, reason: str) -> str:
    """Set a fit aside: out of the listings, every file kept.

    Args:
        layout: The project.
        fit_id: The fit, or a unique prefix of its id.
        reason: Why -- kept with it, and shown when it is restored.

    Returns:
        The fit's whole id; ``""`` when nothing changed.

    Raises:
        CurationRefused: No such fit, no reason, it is its sample's final fit,
            or this runs unattended.
    """
    _refuse_if_agent("curate")
    reason = _reason(reason)
    index, entry, state = _look_up(layout, fit_id)
    if state.discarded:
        return ""
    if FINAL in state.labels:
        raise CurationRefused(
            f"{entry['fit_id']} is the final fit of {_sample_name(entry)}: an "
            "answer is not set aside. Finalize another fit first."
        )
    _append(index, entry, EVENT_DISCARD, reason=reason)
    return str(entry["fit_id"])


def restore(layout: ProjectLayout, fit_id: str) -> str:
    """Bring a discarded fit back into the listings.

    Returns:
        The fit's whole id; ``""`` when nothing changed.

    Raises:
        CurationRefused: No such fit, its files were deleted, or this runs
            unattended.
    """
    _refuse_if_agent("curate")
    index, entry, state = _look_up(layout, fit_id)
    if not state.discarded:
        return ""
    if state.deleted:
        raise CurationRefused(
            f"{entry['fit_id']}'s files were deleted: there is nothing to restore."
        )
    _append(index, entry, EVENT_RESTORE)
    return str(entry["fit_id"])


def delete_files(layout: ProjectLayout, fit_id: str) -> Path:
    """Delete a discarded fit's files; the record that it ran stays.

    The directory is renamed out of the way first and the deletion recorded
    before a file goes: a removal that fails half way leaves a fit recorded as
    deleted and a hidden directory to remove by hand -- never a fit whose files
    are half gone while its record says they are all there.

    Args:
        layout: The project.
        fit_id: The fit, or a unique prefix of its id.

    Returns:
        The directory removed, relative to the project.

    Raises:
        CurationRefused: As :func:`deletable` says; or its files could not all
            be removed, which the message says, and where what is left is.
    """
    _refuse_if_agent("curate")
    index, entry, state, parts = _deletable(layout, fit_id)
    hidden = f".deleting-{parts[-1]}-{secrets.token_hex(4)}"
    error: OSError | None = None
    parent = layout.root.joinpath(*parts[:-1])
    (parent / parts[-1]).rename(parent / hidden)
    try:
        shutil.rmtree(parent / hidden)
    except OSError as exc:
        error = exc
    _append(
        index,
        entry,
        EVENT_DELETE,
        reason=(state.discarded or {}).get("reason"),
        partial=True if error else None,
    )
    relative = Path(*parts)
    if error is not None:
        raise CurationRefused(
            f"{entry['fit_id']} is recorded as deleted, but not all its files "
            f"could be removed ({error.strerror or error}). What is left is in "
            f"{relative.parent / hidden}: remove it by hand."
        )
    return relative


def deletable(layout: ProjectLayout, fit_id: str) -> Path:
    """The directory deleting a fit's files would remove -- asked before asking.

    Args:
        layout: The project.
        fit_id: The fit, or a unique prefix of its id.

    Returns:
        The fit's result directory.

    Raises:
        CurationRefused: No such fit; not discarded; already deleted; or used
            by a report, a figure, another fit or an ISAAC record.
    """
    return layout.root / Path(*_deletable(layout, fit_id)[3])


def _deletable(
    layout: ProjectLayout, fit_id: str
) -> tuple[FitIndex, dict[str, Any], Curation, tuple[str, ...]]:
    index, entry, state = _look_up(layout, fit_id)
    resolved = str(entry["fit_id"])
    if not state.discarded:
        raise CurationRefused(
            f"Discard {resolved} first: deleting its files is the second step, "
            "for a fit already set aside."
        )
    if state.deleted:
        raise CurationRefused(f"{resolved}'s files were deleted already.")
    directory = find_fit_dir(layout, entry)
    if directory is None:
        raise CurationRefused(f"{resolved} has no files left to delete.")
    users = used_by(layout, index, entry, directory, state=state)
    if users:
        raise CurationRefused(
            f"{resolved} is used by " + "; ".join(users) + ". Its files stay."
        )
    sample = entry.get("sample")
    parts = (
        ("samples", str(sample), "results", resolved)
        if sample
        else ("results", resolved)
    )
    return index, entry, state, parts


def used_by(
    layout: ProjectLayout,
    index: FitIndex,
    entry: dict[str, Any],
    directory: Path,
    *,
    state: Curation | None = None,
) -> list[str]:
    """What would lose its footing if this fit's files were deleted.

    Reports and figures in every sample count -- one sample's report can
    compare against another's fit -- and so do other fits that read its files,
    its ISAAC records, and every push of them. Evidence that cannot be read
    counts as a use: a guard on a deletion does not fail open.

    Args:
        layout: The project.
        index: The fit index.
        entry: The fit's index entry.
        directory: Its result directory.
        state: Its curation, when the caller has it.

    Returns:
        One phrase per user, for a person to read; empty when nothing uses it.
    """
    from nr_workbench.commands.report import FIGURE_MANIFEST_SUFFIX
    from nr_workbench.notes import fits_mentioned, notes_about, sample_notes
    from nr_workbench.provenance.record import FitDirectory

    fit_id = str(entry["fit_id"])
    users: list[str] = []
    # Every folder under samples/, not only those `list_samples` counts: a
    # report is a report whether or not its sample.md is there.
    samples = (
        sorted(d.name for d in layout.samples_dir.iterdir() if d.is_dir())
        if layout.samples_dir.is_dir()
        else []
    )
    for sample in samples:
        for note in notes_about(sample_notes(layout.root, sample), fit_id):
            users.append(f"the report {note.path}")
    # The project's own reports/ as well as each sample's: a figure script may
    # live in either.
    folders = [layout.root / "reports"] + [
        layout.sample(sample) / "reports" for sample in samples
    ]
    for report in sorted((layout.root / "reports").glob("**/*.md")):
        try:
            cited = fit_id in fits_mentioned(report.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            cited = True  # cannot be read: counted as a use
        if cited:
            users.append(f"the report {report.relative_to(layout.root).as_posix()}")
    for reports in folders:
        for manifest in sorted(reports.glob(f"**/*{FIGURE_MANIFEST_SUFFIX}")):
            shown = manifest.relative_to(layout.root).as_posix()
            try:
                read = json.loads(manifest.read_text(encoding="utf-8")).get("fits")
            except (OSError, ValueError, AttributeError):
                users.append(f"{shown}, which cannot be read")
                continue
            if isinstance(read, list) and fit_id in read:
                users.append(f"the figures of {shown}")
    if (directory / "isaac").is_dir():
        # Exported, pushed or not: nrw before 2026-09-30 pushed without
        # recording it, and these are what a push would have sent.
        users.append("the ISAAC records made from it (isaac/)")
    if (state if state is not None else replay(index.entries()).of(fit_id)).published:
        users.append("the records pushed from it to the ISAAC Portal")
    own = directory.relative_to(layout.root).as_posix() + "/"
    for other in index.fits():
        other_id = str(other.get("fit_id") or "")
        other_dir = find_fit_dir(layout, other) if other_id != fit_id else None
        if other_dir is None:
            continue
        try:
            inputs = FitDirectory(other_dir).read_inputs()
        except (OSError, ValueError):
            users.append(f"fit {other_id}, whose inputs cannot be read")
            continue
        paths = [i.get("path") for i in inputs if isinstance(i, dict)]
        if any(isinstance(p, str) and p.startswith(own) for p in paths):
            users.append(f"fit {other_id}, which read its files")
    return users


def promote(
    layout: ProjectLayout,
    fit_id: str,
    *,
    label: str = FINAL,
    reason: str,
    force: bool = False,
) -> dict[str, Any]:
    """Mark a fit as the answer for its sample, recording who decided and why.

    "Final" is never implicit. The most recent fit is not the answer, and the
    lowest chi-squared is not automatically the answer: a person decides, and
    that decision is itself provenance. The fit of the same sample that held
    the label before is superseded, and that is recorded too. A fit with no
    sample holds a label of its own, and supersedes no sample's.

    Args:
        layout: The project.
        fit_id: The fit, or a unique prefix of its id.
        label: The label to apply.
        reason: Why this fit is the answer. Required.
        force: Promote it though its inputs have changed since it ran.

    Returns:
        The promotion recorded, with ``supersedes``: the fit that held the
        label before, if another.

    Raises:
        CurationRefused: No reason; no such fit; it did not succeed, is
            discarded, or its files are gone; or this runs unattended.
        InputsChanged: Its inputs changed, and ``force`` was not given.
    """
    from nr_workbench.provenance.whence import Freshness, check_inputs

    _refuse_if_agent("promote")
    reason = _reason(reason, what="A reason is required: it is the part worth keeping.")
    index, entry, state = _look_up(layout, fit_id)
    resolved = str(entry["fit_id"])
    if entry.get("status") != "ok":
        raise CurationRefused(
            f"{resolved} has status '{entry.get('status')}'. Only a successful fit "
            "can be promoted."
        )
    if state.discarded:
        raise CurationRefused(
            f"{resolved} is discarded: restore it before finalizing it."
        )
    directory = find_fit_dir(layout, entry)
    if directory is None:
        raise CurationRefused(f"Fit directory for {resolved} is missing.")
    _, freshness = check_inputs(directory, layout.root)
    changed = freshness not in (Freshness.FRESH, Freshness.UNKNOWN)
    if changed and not force:
        # The caller says what can be done: --force, or a button.
        raise InputsChanged(
            f"{resolved} is {freshness.value.upper()}: its inputs have changed "
            "since it ran."
        )
    previous = replay(index.entries()).holder(entry.get("sample"), label)
    supersedes = (
        str(previous.get("fit_id"))
        if previous and str(previous.get("fit_id")) != resolved
        else None
    )
    now = format_timestamp(utc_now())
    if supersedes:
        # Superseding is recorded, never erased: what was once considered
        # final is part of the story.
        index.append(
            {
                "fit_id": supersedes,
                "sample": entry.get("sample"),
                "label": label,
                "superseded_by": resolved,
                "at": now,
            },
            event=EVENT_SUPERSEDE,
        )
    promotion = {
        "fit_id": resolved,
        "sample": entry.get("sample"),
        "model": entry.get("model"),
        "label": label,
        "reason": reason,
        "who": current_user(),
        "at": now,
        "forced": bool(force) and changed,
        "supersedes": supersedes,
    }
    index.append(promotion, event=EVENT_PROMOTE)
    return promotion


# --------------------------------------------------------------------------
# Publishing
# --------------------------------------------------------------------------


def publish_refusal(entry: dict[str, Any], state: Curation) -> str | None:
    """Why a fit's records may not be published, or ``None`` when they may.

    The one rule, for the terminal and the page: only the final fit of its
    sample is published.
    """
    if FINAL in state.labels:
        return None
    return (
        f"{entry.get('fit_id')} is not the final fit of {_sample_name(entry)}, and "
        "only a finalized fit is published. Finalize it first."
    )


def record_publish_attempt(
    index: FitIndex,
    entry: dict[str, Any],
    *,
    portal: str,
    files: list[Path],
) -> str:
    """Record, before it runs, that a push of these files is starting.

    Args:
        index: The fit index.
        entry: The fit's index entry.
        portal: Where they go: the portal's URL, as it may be shown.
        files: The record files sent.

    Returns:
        The attempt's id, for :func:`record_publish_result`.
    """
    # A directory name too: the published copy is kept under it.
    attempt = f"{utc_now().strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"
    _append(
        index,
        entry,
        EVENT_PUBLISH,
        attempt=attempt,
        portal=portal,
        files=[
            {
                "file": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in files
        ],
    )
    return attempt


def record_publish_result(
    index: FitIndex,
    entry: dict[str, Any],
    attempt: str,
    *,
    records: list[dict[str, str]],
    complete: bool,
) -> None:
    """Record what a push made: each record's id, and whether it finished."""
    _append(
        index,
        entry,
        EVENT_PUBLISH,
        attempt=attempt,
        records=records,
        complete=complete,
    )


def current_user() -> str:
    """Best-effort identity of whoever made the decision."""
    import getpass

    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - an identity is best-effort
        return "unknown"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _look_up(
    layout: ProjectLayout, fit_id: str
) -> tuple[FitIndex, dict[str, Any], Curation]:
    """The index, the fit's entry, and what has been said about it."""
    index = FitIndex(layout.index_file)
    matches = index.resolve(fit_id)
    if not matches:
        raise NoSuchFit(f"No fit matching {fit_id!r}. See `nrw ls`.")
    if len(matches) > 1:
        raise NoSuchFit(
            f"{fit_id!r} matches {len(matches)} fits: "
            + ", ".join(str(m["fit_id"]) for m in matches[:5])
        )
    entry = matches[0]
    return index, entry, replay(index.entries()).of(entry["fit_id"])


def _append(index: FitIndex, entry: dict[str, Any], event: str, **more: Any) -> None:
    index.append(
        {
            "fit_id": entry["fit_id"],
            "sample": entry.get("sample"),
            "who": current_user(),
            "at": format_timestamp(utc_now()),
            **{k: v for k, v in more.items() if v is not None},
        },
        event=event,
    )


def _reason(reason: str, *, what: str = "") -> str:
    """A reason: said, one line, and short enough to read beside the fit."""
    text = " ".join(str(reason or "").split())
    if not text:
        raise CurationRefused(
            what or "A reason is required: it is what makes the choice legible."
        )
    if len(text) > MAX_REASON:
        raise CurationRefused(
            f"The reason is {len(text)} characters; the limit is {MAX_REASON}."
        )
    return text


def _sample_key(sample: Any) -> str | None:
    return str(sample) if sample else None


def _sample_name(entry: dict[str, Any]) -> str:
    sample = entry.get("sample")
    return str(sample) if sample else "the project's fits with no sample"


def _refuse_if_agent(action: str) -> None:
    """The commands' refusal, here too: a library call is not a way around it."""
    from nr_workbench.agent.guard import AGENT_ENV, reason_for

    if os.environ.get(AGENT_ENV):
        raise CurationRefused(
            f"{AGENT_ENV} is set, so this is running unattended. {reason_for(action)}"
        )
