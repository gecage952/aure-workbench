"""Planning the files of one sample directory.

Moved here from ``commands/sample.py`` so that the experiment catalog, which
also creates samples, can plan them without importing a command module --
``commands`` depends on ``experiment``, which depends on ``project``, and never
the other way round. ``commands/sample.py`` re-exports both names.
"""

from __future__ import annotations

import dataclasses
import re

from nr_workbench.project.layout import SAMPLE_SUBDIRS
from nr_workbench.project.render import RenderContext, render_tree
from nr_workbench.project.scaffold import PlannedFile

#: Sample IDs become directory names and appear in generated scripts, so keep
#: them to characters that are safe in both.
_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def validate_sample_id(sample_id: str) -> str:
    """Check a sample identifier is usable as a directory and script token.

    Args:
        sample_id: The proposed identifier.

    Returns:
        The identifier, unchanged.

    Raises:
        ValueError: If it is empty or contains characters outside
            ``[A-Za-z0-9_-]``.
    """
    if not sample_id:
        raise ValueError("Sample ID must not be empty")
    bad = sorted(set(sample_id) - _ALLOWED)
    if bad:
        raise ValueError(
            f"Sample ID {sample_id!r} contains disallowed character(s) {bad}. "
            "Use letters, digits, hyphen, and underscore only."
        )
    return sample_id


def plan_sample_files(
    context: RenderContext,
    sample_id: str,
    *,
    title: str | None = None,
) -> list[PlannedFile]:
    """Plan every file for one sample directory.

    Args:
        context: The project's render context, used for facility and beamtime.
        sample_id: The sample identifier.
        title: Human-readable title. Defaults to the sample ID.

    Returns:
        Planned files: the rendered sample templates plus a ``.gitkeep`` in each
        standard subdirectory, so the layout is visible before data arrives.

    Raises:
        ValueError: If the sample ID is not usable.
    """
    validate_sample_id(sample_id)
    prefix = f"samples/{sample_id}"

    # `replace`, not a field-by-field copy. The copy this replaces rebuilt the
    # context by naming each field, so any field added to RenderContext later
    # would have been silently dropped here -- and the sample templates are
    # exactly where the experiment catalog's prose now arrives.
    sample_context = dataclasses.replace(
        context, sample_id=sample_id, title=title or sample_id
    )

    planned = render_tree("sample", sample_context, prefix=prefix)

    for subdir in SAMPLE_SUBDIRS:
        planned.append(
            PlannedFile(
                relpath=f"{prefix}/{subdir}/.gitkeep",
                content=b"",
                template_id=f"dir/sample/{subdir}",
            )
        )

    return planned


#: A plain name: never a path. Matched whole, with :func:`re.fullmatch` --
#: ``$`` would also match before a trailing newline.
PLAIN_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")


def validate_model_name(name: object) -> str:
    """Check a model name is a plain name, usable as its spec's filename.

    Args:
        name: The proposed name, from a command line or a page.

    Returns:
        The name, unchanged.

    Raises:
        ValueError: With the reason.
    """
    if not isinstance(name, str) or not PLAIN_NAME_RE.fullmatch(name):
        raise ValueError(
            f"model name {name!r} must be a plain name: letters, digits, '.', "
            "'-' or '_', not a path."
        )
    return name
