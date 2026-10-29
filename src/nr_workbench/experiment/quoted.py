"""The experiment's own context, as a language model is given it: in the
unattended agent's prompt, and in model authoring's request. Each written
field is quoted as it is saved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from nr_workbench.experiment.model import EXPERIMENT_FIELDS, ExperimentContext

#: How each field is headed where it is quoted.
HEADINGS = {"summary": "Summary", "hypotheses": "Hypotheses", "notes": "Notes"}


@dataclass(frozen=True)
class Quotable:
    """The experiment's context, as it is quoted.

    Attributes:
        fields: Each written field, by name, in order.
        unreadable: The catalog could not be read, so nothing of it is known.
    """

    fields: dict[str, str] = field(default_factory=dict)
    unreadable: bool = False

    @property
    def empty(self) -> bool:
        """Whether there is nothing to say: nothing written, nothing wrong."""
        return not (self.fields or self.unreadable)

    def text(self) -> str:
        """Each field under its heading, or why there is none.

        Returns:
            Plain text, a block per field; ``""`` when :attr:`empty`.
        """
        parts = [f"{HEADINGS[name]}:\n{text}" for name, text in self.fields.items()]
        if self.unreadable:
            parts.append(
                "(The experiment's catalog could not be read, so its context is "
                "not here; `nrw experiment status` says why.)"
            )
        return "\n\n".join(parts)


def quotable_record(record: ExperimentContext) -> Quotable:
    """Each written field of *record*, as it is saved.

    Args:
        record: The experiment's record, as loaded.
    """
    return Quotable(
        fields={
            name: getattr(record, name)
            for name in EXPERIMENT_FIELDS
            if getattr(record, name)
        }
    )


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
