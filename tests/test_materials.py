"""Contrast-match arithmetic.

What used to be here was a name-to-density table with an `sld()` wrapper over
`periodictable`. It is gone. AuRE retired its equivalent because the SLDs in a
fitted model come from its intake LLM rather than a table, and the same applies
here with one more reason on top: the models this repository is used with supply
a compound density on request, and `periodictable` does the physics. The table
in between drifted -- six SLDs quoted in the skills had diverged from it, two of
them disagreeing with the table sitting beside them in the package. The
substrate lookup that outlived it is gone too: the one prompt that used it
leaves the substrate's SLD to the model, which reads the notes.

`tests/test_skill_slds.py` is what replaced it: the numbers the skills quote are
checked against the physics directly, which is where the error actually was.
"""

from __future__ import annotations

import pytest

from nr_workbench.aure_adapter import (
    D2O_SLD,
    H2O_SLD,
    contrast_match_ratio,
    mixture_sld,
)


def test_silicon_is_matched_at_38_percent_d2o() -> None:
    """`contrast_match_ratio(2.07)  # 0.38`, quoted in solvent-contrast-matching."""
    assert contrast_match_ratio(2.07) == pytest.approx(0.38, abs=0.01)


def test_the_ends_of_the_series_are_the_pure_solvents() -> None:
    assert mixture_sld(0.0) == pytest.approx(H2O_SLD)
    assert mixture_sld(1.0) == pytest.approx(D2O_SLD)


@pytest.mark.parametrize("target,expected", [(-5.0, 0.0), (9.9, 1.0)])
def test_an_unreachable_target_clamps_rather_than_raising(
    target: float, expected: float
) -> None:
    """The caller asked which mixture is closest, not for an error."""
    assert contrast_match_ratio(target) == expected


@pytest.mark.parametrize("fraction", [0.0, 0.25, 0.38, 0.5, 0.75, 1.0])
def test_the_two_are_inverses(fraction: float) -> None:
    assert contrast_match_ratio(mixture_sld(fraction)) == pytest.approx(fraction)


def test_identical_ends_cannot_be_matched() -> None:
    with pytest.raises(ValueError, match="same SLD"):
        contrast_match_ratio(1.0, low=2.07, high=2.07)


def test_the_arithmetic_does_not_import_aure() -> None:
    """nr-workbench carries these six lines itself; AuRE retired its table."""
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from nr_workbench.aure_adapter import contrast_match_ratio; "
            "contrast_match_ratio(2.07); "
            "assert 'aure' not in sys.modules, 'contrast arithmetic imported aure'",
        ],
        check=True,
    )
