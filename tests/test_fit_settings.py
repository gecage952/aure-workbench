"""How a fit is set up: the command line, then nrw.toml [fit], then bumps.

The resolution rules first, then what `nrw fit run` does with them -- a dry
run shows the settings without fitting, and one real amoeba fit shows they
reach the record. The examples in the README and in the scaffolded nrw.toml
are parsed here too: a documented table that does not parse is a trap.
"""

# The fixture is imported and requested by name, which ruff reads as a
# redefinition.
# ruff: noqa: F811

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from nr_workbench.cli import main
from nr_workbench.fitting.settings import (
    DEFAULT_METHOD,
    FitSettingsError,
    read_agent_limits,
    read_fit_defaults,
    resolve,
)

from .test_fit_e2e import fitted_project, run_cli  # noqa: F401 - fixture

ROOT = Path(__file__).parent.parent

UNSET = dict.fromkeys(("steps", "samples", "burn", "pop", "seed", "parallel"))


def defaults(text: str):
    return read_fit_defaults(tomllib.loads(text))


def limits(text: str):
    return read_agent_limits(tomllib.loads(text))


# --------------------------------------------------------------------------
# What nrw.toml may say
# --------------------------------------------------------------------------


def test_a_project_that_says_nothing_fits_with_dream() -> None:
    fit = resolve(defaults(""), method=None, given=UNSET)

    assert (fit.method, DEFAULT_METHOD) == ("dream", "dream")
    assert fit.origins == {"method": "nrw's default"}
    assert fit.settings == {**UNSET, "parallel": 0}


def test_the_command_line_then_nrw_toml_then_bumps() -> None:
    toml = defaults('[fit]\nmethod = "de"\nseed = 7\n[fit.de]\nsteps = 500\npop = 20\n')

    fit = resolve(toml, method=None, given={**UNSET, "pop": 30})

    assert fit.method == "de"
    assert fit.settings == {
        "steps": 500,  # [fit.de]
        "samples": None,  # not a de setting: bumps' own, untouched
        "burn": None,
        "pop": 30,  # the command line wins
        "seed": 7,  # [fit], for every fitter
        "parallel": 0,
    }
    assert fit.origins == {
        "method": "nrw.toml",
        "steps": "nrw.toml",
        "pop": "command line",
        "seed": "nrw.toml",
    }


def test_a_fitters_table_applies_to_that_fitter_only() -> None:
    # DREAM's samples in an amoeba fit would be ignored by bumps and yet
    # recorded, making two identical amoeba fits read as different runs.
    toml = defaults("[fit.dream]\nsamples = 20000\n[fit.amoeba]\nsteps = 50\n")

    amoeba = resolve(toml, method="amoeba", given=UNSET)
    dream = resolve(toml, method="dream", given=UNSET)

    assert (amoeba.settings["samples"], amoeba.settings["steps"]) == (None, 50)
    assert (dream.settings["samples"], dream.settings["steps"]) == (20000, None)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('fit = "dream"', "must be a table"),
        ('[fit]\nmethod = "lm"', "not available"),
        ("[fit]\nsamples = 100", "no setting 'samples'"),
        ("[fit]\nsteps = 100", "no setting 'steps'"),
        ("[fit.amoeba]\nsamples = 100", "amoeba takes no 'samples'"),
        ("[fit.de]\nburn = 10", "de takes no 'burn'"),
        ("[fit.dream]\nsamples = 0", "at least 1"),
        ("[fit.dream]\nsamples = true", "whole number"),
        ('[fit.dream]\nsamples = "10000"', "whole number"),
        ("[fit]\nparallel = -1", "at least 0"),
        ("[fit]\ndream = 1", "must be a table"),
    ],
)
def test_a_setting_that_changes_nothing_is_never_silently_accepted(
    text: str, message: str
) -> None:
    with pytest.raises(FitSettingsError, match=message):
        defaults(text)


def test_a_setting_the_fitter_does_not_take_is_refused_on_the_command_line() -> None:
    with pytest.raises(FitSettingsError, match="amoeba takes no 'samples'"):
        resolve(defaults(""), method="amoeba", given={**UNSET, "samples": 100})


# --------------------------------------------------------------------------
# The examples a person copies
# --------------------------------------------------------------------------


def test_the_readme_example_is_what_it_says() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Fitting options", 1)[1].split("\n## ", 1)[0]
    (example,) = re.findall(r"```toml\n(.*?)```", section, re.DOTALL)

    toml = defaults(example)

    assert toml.method == "dream"
    assert toml.settings_for("dream") == {
        "seed": 12345,
        "parallel": 0,
        "samples": 20000,
        "burn": 1000,
    }
    assert toml.settings_for("amoeba")["steps"] == 1000


def test_the_scaffolded_tables_uncommented_are_bumps_own_defaults(
    tmp_path: Path,
) -> None:
    from nr_workbench.fitting.settings import BUMPS_DEFAULTS

    root = tmp_path / "proj"
    assert CliRunner().invoke(main, ["init", str(root)]).exit_code == 0
    text = (root / "nrw.toml").read_text(encoding="utf-8")
    assert "fit" not in tomllib.loads(text)  # commented out: nothing is set
    block = text.split("# [fit]\n", 1)[1].split("\n\n", 1)[0]
    uncommented = "[fit]\n" + "\n".join(
        line[2:] if line.startswith("# ") else line.lstrip("#")
        for line in block.splitlines()
    )

    toml = defaults(uncommented)

    for method, settings in BUMPS_DEFAULTS.items():
        shown = toml.per_method.get(method, {})
        assert shown == {k: v for k, v in settings.items() if k in shown}, method
        assert shown, method
    assert toml.method == "dream"


# --------------------------------------------------------------------------
# What `nrw fit run` does with them
# --------------------------------------------------------------------------


def test_nrw_fit_run_says_it_fits_with_dream_when_nothing_says_otherwise(
    fitted_project: Path, monkeypatch
) -> None:
    result = run_cli(
        fitted_project,
        monkeypatch,
        "fit",
        "run",
        "samples/S1/models/film.py",
        "--dry-run",
    )

    assert result.exit_code == 0, result.output
    assert "settings  method dream (nrw's default)" in result.output
    assert "  method    dream" in result.output


@pytest.mark.fit
def test_nrw_fit_run_takes_what_nrw_toml_says_and_records_it(
    fitted_project: Path, monkeypatch
) -> None:
    from nr_workbench.provenance.index import FitIndex

    with (fitted_project / "nrw.toml").open("a", encoding="utf-8") as handle:
        handle.write(
            '\n[fit]\nmethod = "amoeba"\nseed = 1\nparallel = 1\n'
            "[fit.amoeba]\nsteps = 7\n[fit.dream]\nsamples = 99\n"
        )

    result = run_cli(
        fitted_project, monkeypatch, "fit", "run", "samples/S1/models/film.py"
    )

    assert result.exit_code == 0, result.output
    assert "method amoeba (nrw.toml), steps 7 (nrw.toml)" in result.output
    (entry,) = FitIndex(fitted_project / ".nrw" / "index.jsonl").fits()
    assert entry["method"] == "amoeba"
    # What it ran with, wherever that came from -- and not DREAM's samples.
    assert entry["settings"] == {
        "method": "amoeba",
        "steps": 7,
        "seed": 1,
        "parallel": 1,
    }


def test_nrw_fit_run_refuses_nrw_toml_it_cannot_use(
    fitted_project: Path, monkeypatch
) -> None:
    with (fitted_project / "nrw.toml").open("a", encoding="utf-8") as handle:
        handle.write("\n[fit.amoeba]\nsamples = 5\n")

    result = run_cli(
        fitted_project,
        monkeypatch,
        "fit",
        "run",
        "samples/S1/models/film.py",
        "--dry-run",
    )

    assert result.exit_code != 0
    assert "nrw.toml: [fit.amoeba]: amoeba takes no 'samples'" in result.output


# --------------------------------------------------------------------------
# What an unattended session may ask of a fit
# --------------------------------------------------------------------------


def test_agent_limits_hold_a_fitters_own_settings_as_maxima() -> None:
    got = limits("[agent.limits.dream]\nsamples = 100000\nburn = 1000\n")

    assert got.per_method == {"dream": {"samples": 100000, "burn": 1000}}
    assert limits("").per_method == {}
    assert limits("[agent]\n").per_method == {}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("agent = 3\n", "[agent] must be a table"),
        ("[agent]\nlimits = 3\n", "[agent.limits] must be a table"),
        ("[agent.limits.lm]\nsteps = 3\n", "[agent.limits] has no fitter 'lm'"),
        ("[agent.limits.amoeba]\nsamples = 3\n", "limits amoeba's own settings"),
        ("[agent.limits.dream]\nseed = 3\n", "limits dream's own settings"),
        ("[agent.limits.dream]\nburn = -1\n", "burn must be a whole number"),
    ],
)
def test_a_limit_that_would_limit_nothing_is_refused(text: str, message: str) -> None:
    with pytest.raises(FitSettingsError, match=re.escape(message)):
        limits(text)


def test_a_limit_is_held_against_what_the_fit_would_run_with() -> None:
    """Not against what the command line spells out: a fit that names no
    setting asks for nrw.toml's [fit] and, past that, bumps' defaults."""
    caps = limits("[agent.limits.dream]\nsamples = 5000\nburn = 1000\n")

    asked = resolve(defaults("[fit.dream]\nburn = 2000\n"), method=None, given=UNSET)
    within = resolve(defaults(""), method=None, given={**UNSET, "samples": 5000})
    amoeba = resolve(defaults(""), method="amoeba", given=UNSET)

    assert caps.exceeded(asked) == {
        "samples": (10000, 5000, "bumps' default"),
        "burn": (2000, 1000, "nrw.toml"),
    }
    assert caps.exceeded(within) == {}
    assert caps.exceeded(amoeba) == {}  # dream's limits never reach another fitter


def test_the_scaffolded_agent_limits_example_reads_as_it_says(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    assert CliRunner().invoke(main, ["init", str(root)]).exit_code == 0
    text = (root / "nrw.toml").read_text(encoding="utf-8")
    assert "agent" not in tomllib.loads(text)  # commented out: nothing is limited
    block = text.split("# [agent.limits.dream]\n", 1)[1].split("\n\n", 1)[0]
    uncommented = "[agent.limits.dream]\n" + "\n".join(
        line[2:] for line in block.splitlines()
    )

    assert limits(uncommented).per_method == {
        "dream": {"samples": 100000, "burn": 1000, "steps": 300, "pop": 10}
    }


def _limit_dream(project: Path) -> None:
    with (project / "nrw.toml").open("a", encoding="utf-8") as handle:
        handle.write("\n[agent.limits.dream]\nsamples = 5000\nburn = 1000\n")


def test_an_unattended_fit_over_its_limit_is_refused_before_it_runs(
    fitted_project: Path, monkeypatch
) -> None:
    _limit_dream(fitted_project)
    monkeypatch.setenv("NRW_AGENT", "1")

    result = run_cli(
        fitted_project,
        monkeypatch,
        "fit",
        "run",
        "samples/S1/models/film.py",
        "--burn",
        "5000",
        "--dry-run",
    )

    assert result.exit_code != 0
    assert "[agent.limits.dream] caps such a fit at samples 5000, burn 1000" in (
        result.output
    )
    assert "samples 10000 (bumps' default), burn 5000 (command line)" in result.output
    assert "ESCALATIONS.md" in result.output


def test_a_fit_within_its_limits_or_started_by_a_person_is_not_limited(
    fitted_project: Path, monkeypatch
) -> None:
    _limit_dream(fitted_project)
    monkeypatch.delenv("NRW_AGENT", raising=False)

    by_a_person = run_cli(
        fitted_project,
        monkeypatch,
        "fit",
        "run",
        "samples/S1/models/film.py",
        "--burn",
        "5000",
        "--dry-run",
    )
    monkeypatch.setenv("NRW_AGENT", "1")
    within = run_cli(
        fitted_project,
        monkeypatch,
        "fit",
        "run",
        "samples/S1/models/film.py",
        "--samples",
        "5000",
        "--burn",
        "500",
        "--dry-run",
    )

    assert by_a_person.exit_code == 0, by_a_person.output
    assert within.exit_code == 0, within.output
