"""CLI-level tests, including the import-cost guard."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest
from click.testing import CliRunner

from nr_workbench import __version__
from nr_workbench.cli import main

#: Modules that must not be imported just to print help. `aure` heads the list:
#: `aure/__init__.py` eagerly imports its workflow package, which chains through
#: every node module into langchain-core, periodictable and scipy -- roughly
#: 1.5-3 seconds. refl1d and matplotlib are nearly as expensive, and pyarrow
#: (the experiment catalog) costs about a second on its own.
FORBIDDEN_ON_HELP = (
    "aure",
    "refl1d",
    "bumps",
    "matplotlib",
    "scipy",
    "langchain_core",
    "pyarrow",
    # The official Tiled client: only `nrw tiled`'s own commands import it.
    "tiled",
)


def test_help_lists_the_command_surface() -> None:
    result = CliRunner().invoke(main, ["--help"])

    assert result.exit_code == 0
    for command in ("init", "doctor", "sample", "skills"):
        assert command in result.output


def test_version_flag_reports_the_package_version() -> None:
    result = CliRunner().invoke(main, ["--version"])

    assert result.exit_code == 0
    assert __version__ in result.output


def test_help_does_not_import_heavy_modules() -> None:
    """`nrw --help` must stay instant.

    Every subcommand is lazily imported for this reason. A single top-level
    `import aure` in cli.py or a commands module would add seconds to every
    invocation, including tab-completion and `--help`. Run in a subprocess so
    the check is not fooled by modules the test session already imported.
    """
    script = textwrap.dedent(
        f"""
        import sys
        from click.testing import CliRunner
        from nr_workbench.cli import main

        result = CliRunner().invoke(main, ["--help"])
        assert result.exit_code == 0, result.output

        leaked = [m for m in {FORBIDDEN_ON_HELP!r} if m in sys.modules]
        if leaked:
            print("LEAKED:" + ",".join(leaked))
            sys.exit(1)
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )

    assert result.returncode == 0, (
        f"`nrw --help` imported heavy modules: {result.stdout.strip()}\n"
        "Move the import inside the command callback that needs it."
    )


@pytest.mark.parametrize(
    "group", ["aure", "model", "fit", "sample", "data", "experiment", "tiled"]
)
def test_group_help_does_not_import_heavy_modules(group: str) -> None:
    """A group's own `--help` must stay as cheap as the top-level one.

    `nrw --help` only reaches the group callbacks, so a module-scope import in
    a *command* module can hide from it and still cost seconds on the help the
    user actually types. `nrw aure --help` is the case that made this worth
    parameterising: everything it does ends in AuRE.
    """
    script = textwrap.dedent(
        f"""
        import sys
        from click.testing import CliRunner
        from nr_workbench.cli import main

        result = CliRunner().invoke(main, [{group!r}, "--help"])
        assert result.exit_code == 0, result.output

        leaked = [m for m in {FORBIDDEN_ON_HELP!r} if m in sys.modules]
        if leaked:
            print("LEAKED:" + ",".join(leaked))
            sys.exit(1)
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120
    )

    assert result.returncode == 0, (
        f"`nrw {group} --help` imported heavy modules: {result.stdout.strip()}\n"
        "Move the import inside the command callback that needs it."
    )


def test_subcommands_have_help_text() -> None:
    """Each command needs a usable one-liner; the CLI is the primary UI."""
    runner = CliRunner()

    for command in ("init", "doctor", "sample", "skills", "aure"):
        result = runner.invoke(main, [command, "--help"])
        assert result.exit_code == 0, f"{command}: {result.output}"
        assert result.output.strip()


def test_skills_list_bundled_works_without_a_project(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["skills", "list", "--bundled"])

    assert result.exit_code == 0, result.output
    assert "nr-workbench-project" in result.output


def test_skills_list_without_a_project_suggests_bundled(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["skills", "list"])

    assert result.exit_code != 0
    assert "--bundled" in result.output


@pytest.mark.parametrize(
    "args", [["skills", "add", "metal-oxide-interfaces"], ["skills", "sync"]]
)
def test_skills_over_a_conflicted_lock_say_why_and_change_nothing(
    project, monkeypatch, args
) -> None:
    """Writing a few skills' entries over it would drop every other file's."""
    lock = project / ".nrw" / "scaffold.lock.json"
    lock.write_text(
        "<<<<<<< HEAD\n" + lock.read_text() + "=======\n>>>>>>> theirs\n",
        encoding="utf-8",
    )
    conflicted = lock.read_bytes()
    monkeypatch.chdir(project)

    result = CliRunner().invoke(main, args)

    assert result.exit_code == 1
    assert "conflict markers" in result.output
    assert lock.read_bytes() == conflicted


def test_skills_path_prints_an_existing_directory() -> None:
    from pathlib import Path

    result = CliRunner().invoke(main, ["skills", "path"])

    assert result.exit_code == 0
    assert Path(result.output.strip()).is_dir()


def test_an_off_menu_fitter_is_refused_at_the_cli(tmp_path) -> None:
    """`lm` is a real bumps fitter and is still not on the menu.

    Five of twelve fits in one real session differed from their predecessor in
    nothing but the optimiser, while the model underneath was broken. Click
    refuses before anything is recorded. `de` used to be the example here; it
    earned its way onto the menu, so the test moved to one that has not.
    """
    script = tmp_path / "m.py"
    script.write_text("problem = None\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["fit", "run", str(script), "--method", "lm"])

    assert result.exit_code != 0
    assert "lm" in result.output
    assert "amoeba" in result.output and "dream" in result.output


def test_the_fitters_on_the_menu_are_accepted() -> None:
    """The refusal must not have narrowed the menu."""
    help_text = CliRunner().invoke(main, ["fit", "run", "--help"]).output

    assert "amoeba" in help_text
    assert "de" in help_text
    assert "dream" in help_text
