"""Turn a fit id --- or a prefix of one --- into its entry and directory.

Every command that takes a fit id on the command line needs the same three
steps: resolve the prefix, insist it is unambiguous, and find the directory the
run wrote. Keeping them here means one message for "no such fit" and one for
"that prefix matches four of them", rather than a slightly different pair per
command.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nr_workbench.project.layout import ProjectLayout
from nr_workbench.provenance.index import FitIndex


class FitNotFoundError(Exception):
    """No fit matches, the prefix is ambiguous, or the directory is gone."""


def fit_dir(layout: ProjectLayout, entry: dict[str, Any]) -> Path | None:
    """Locate the directory for an index entry.

    Args:
        layout: The project layout.
        entry: An index entry.

    Returns:
        The fit directory, or ``None`` if it is not on disk. The index is
        append-only, so a recorded fit whose directory was cleaned up is a
        normal state rather than an error.
    """
    fit_id = entry.get("fit_id")
    sample = entry.get("sample")
    candidates = []
    if sample:
        candidates.append(layout.sample(str(sample)) / "results" / str(fit_id))
    candidates.append(layout.root / "results" / str(fit_id))
    for candidate in candidates:
        if (candidate / "manifest.json").is_file():
            return candidate
    return None


def resolve_fit(
    layout: ProjectLayout, index: FitIndex, reference: str
) -> tuple[dict[str, Any], Path]:
    """Resolve a fit id or prefix to its entry and its directory.

    Args:
        layout: The project layout.
        index: The fit index.
        reference: A fit id or a unique prefix of one.

    Returns:
        The index entry and the directory it wrote.

    Raises:
        FitNotFoundError: If nothing matches, the prefix is ambiguous, or the
            directory is missing.
    """
    matches = index.resolve(reference)
    if not matches:
        raise FitNotFoundError(f"No fit matching {reference!r}. See `nrw ls`.")
    if len(matches) > 1:
        raise FitNotFoundError(
            f"{reference!r} matches {len(matches)} fits: "
            + ", ".join(str(m["fit_id"]) for m in matches[:5])
        )
    entry = matches[0]
    directory = fit_dir(layout, entry)
    if directory is None:
        raise FitNotFoundError(f"Fit directory for {entry['fit_id']} is missing.")
    return entry, directory
