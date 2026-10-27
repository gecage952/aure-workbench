"""The experiment's own context, as a language model is given it.

The record is checked when it is saved on the Experiment page, but a catalog is
committed and pulled: the one loaded may never have passed through the page.
So every reader takes it from here -- the unattended agent's prompt, model
authoring's request, and ``nrw tiled push``, which writes it where everyone
on the experiment reads it -- each field held to the page's own rule again.
A field that fails is left out, by name, and never quoted or pushed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from nr_workbench.experiment.model import (
    EXPERIMENT_FIELDS,
    CatalogValidationError,
    ExperimentContext,
    clean_experiment_field,
)

#: How each field is headed where it is quoted.
HEADINGS = {"summary": "Summary", "hypotheses": "Hypotheses", "notes": "Notes"}


@dataclass(frozen=True)
class Quotable:
    """What of the experiment's context may be quoted.

    Attributes:
        fields: Each written field that passes the rules, by name, in order.
        left_out: The written fields that do not, by name.
        unreadable: The catalog could not be read, so nothing of it is known.
    """

    fields: dict[str, str] = field(default_factory=dict)
    left_out: tuple[str, ...] = ()
    unreadable: bool = False

    @property
    def empty(self) -> bool:
        """Whether there is nothing to say: nothing written, nothing wrong."""
        return not (self.fields or self.left_out or self.unreadable)

    def text(self) -> str:
        """Each field under its heading, then what could not be quoted.

        Returns:
            Plain text, a block per field; ``""`` when :attr:`empty`.
        """
        parts = [f"{HEADINGS[name]}:\n{text}" for name, text in self.fields.items()]
        if self.left_out:
            *rest, last = [HEADINGS[name].lower() for name in self.left_out]
            listed = f"{', '.join(rest)} and {last}" if rest else last
            parts.append(
                f"(Left out: the {listed}. What is saved there breaks the rules "
                "the Experiment page holds this text to.)"
            )
        if self.unreadable:
            parts.append(
                "(The experiment's catalog could not be read, so its context is "
                "not here; `nrw experiment status` says why.)"
            )
        return "\n\n".join(parts)


def quotable_record(record: ExperimentContext) -> Quotable:
    """Hold each written field of *record* to the page's rules again.

    Args:
        record: The experiment's record, as loaded.

    Returns:
        The fields that pass, and the names of those that do not.
    """
    fields: dict[str, str] = {}
    left_out: list[str] = []
    for name in EXPERIMENT_FIELDS:
        value = getattr(record, name)
        if not value:
            continue
        try:
            fields[name] = clean_experiment_field(name, value)
        except CatalogValidationError:
            left_out.append(name)
    return Quotable(fields=fields, left_out=tuple(left_out))


def quotable(root: Path) -> Quotable:
    """The project's experiment context, ready to quote.

    Args:
        root: Project root.

    Returns:
        What may be quoted. Empty for a project with no catalog: nothing is
        opened then, not even the lock a read takes.
    """
    from nr_workbench.experiment.store import CatalogError, ParquetCatalogStore

    store = ParquetCatalogStore.for_project(Path(root))
    if not store.exists():
        return Quotable()
    try:
        record = store.load().experiment
    except CatalogError:
        return Quotable(unreadable=True)
    return quotable_record(record)
