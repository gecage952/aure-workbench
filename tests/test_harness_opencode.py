"""What an OpenCode-scaffolded project must contain.

OpenCode reads a different config file, a different instructions file and a
different agents directory from Claude Code. None of those failures are loud:
a config it rejects, or dispatchers in a directory it does not scan, both
present as an assistant that simply behaves as though the project said nothing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from nr_workbench.commands.init_cmd import plan_project_files
from nr_workbench.project.render import RenderContext
from nr_workbench.project.scaffold import apply_scaffold

#: The published schema sets `allowComments`, so the shipped config carries its
#: reasoning inline the way `.claude/settings.json` does. Anything reading it
#: back in a test has to strip them first.
_LINE_COMMENT = re.compile(r"^\s*//.*$")


def load_jsonc(path: Path) -> dict:
    """Parse a JSONC file by stripping whole-line ``//`` comments."""
    text = path.read_text(encoding="utf-8")
    return json.loads(
        "\n".join(_LINE_COMMENT.sub("", line) for line in text.splitlines())
    )


@pytest.fixture
def opencode_project(tmp_path: Path, context: RenderContext) -> Path:
    """A project scaffolded for OpenCode alone."""
    apply_scaffold(
        tmp_path, plan_project_files(replace(context, harnesses=("opencode",)))
    )
    return tmp_path


def test_opencode_project_has_its_config_and_instructions(
    opencode_project: Path,
) -> None:
    assert (opencode_project / "opencode.json").is_file()
    assert (opencode_project / "AGENTS.md").is_file()
    assert not (opencode_project / "CLAUDE.md").exists()


def test_opencode_config_is_valid_jsonc_naming_the_published_schema(
    opencode_project: Path,
) -> None:
    config = load_jsonc(opencode_project / "opencode.json")

    assert config["$schema"] == "https://opencode.ai/config.json"


def test_opencode_config_denies_promote(opencode_project: Path) -> None:
    """The same action `.claude/settings.json` denies, in OpenCode's grammar.

    This is the weaker of the mechanisms -- it matches the command as written,
    so it does not catch `A=1 nrw promote x` -- but a project that has it and a
    project that does not are meaningfully different.
    """
    config = load_jsonc(opencode_project / "opencode.json")

    bash = config["permission"]["bash"]
    assert bash["nrw promote*"] == "deny"
    assert bash["nr-workbench promote*"] == "deny"


def test_opencode_config_points_at_the_shared_instruction_body(
    opencode_project: Path,
) -> None:
    """AGENTS.md is picked up automatically; the shared workflow is not."""
    config = load_jsonc(opencode_project / "opencode.json")

    assert ".github/copilot-instructions.md" in config["instructions"]
    assert (opencode_project / ".github" / "copilot-instructions.md").is_file(), (
        "the file the config points at must actually be installed"
    )


def test_opencode_dispatchers_land_in_the_directory_opencode_scans(
    opencode_project: Path,
) -> None:
    stubs = sorted(
        p.name for p in (opencode_project / ".opencode" / "agents").glob("*.md")
    )

    assert stubs, "expected dispatcher agents for OpenCode"
    assert "tnr-variogram.md" in stubs


def test_opencode_dispatchers_declare_themselves_subagents(
    opencode_project: Path,
) -> None:
    """Without `mode: subagent` OpenCode treats the file as a primary agent,
    which puts 17 skill stubs in the session picker instead of behind it."""
    stub = (opencode_project / ".opencode" / "agents" / "tnr-variogram.md").read_text(
        encoding="utf-8"
    )

    assert "mode: subagent" in stub
    assert "tools: Read, Grep, Glob, Bash" not in stub, (
        "Claude's tool names mean nothing here"
    )


def test_opencode_project_gets_the_tool_neutral_skills(
    opencode_project: Path,
) -> None:
    """OpenCode can read `.claude/skills/`, which does not change the answer.

    The point of the repo-root folder is one place every assistant reaches,
    not the union of their private ones.
    """
    assert (
        opencode_project / "skills" / "reflectometry" / "tnr-variogram" / "SKILL.md"
    ).is_file()
    assert not (opencode_project / ".opencode" / "skills").exists()
    assert not (opencode_project / ".claude").exists()


def test_opencode_can_be_driven_unattended() -> None:
    """Enabled only after measuring that the guard plugin actually blocks.

    Against opencode 1.18.18: a `--force` command the deny list does *not*
    match was refused by the plugin under `--auto`, and the model was handed
    the guard's reason. Without that measurement this flag would be a guess
    that lets a session run unlimited.
    """
    from nr_workbench.harness import resolve

    assert resolve(["opencode"])[0].drives_sessions is True


def test_opencode_reports_that_it_has_no_turn_cap() -> None:
    """`opencode run` has no --max-turns equivalent.

    Callers have to know, because a session bounded only by the clock is a
    different promise from one bounded by turns, and `--turns` silently doing
    nothing is exactly the shape of a limit that is not one.
    """
    from nr_workbench.harness.driver import claude_argv, opencode_argv

    assert opencode_argv(Path("p.md"), turns=200, model=None).turn_cap is False
    assert claude_argv(Path("p.md"), turns=200, model=None).turn_cap is True


def test_opencode_takes_its_prompt_on_stdin() -> None:
    """`opencode run` takes the message positionally, and a composed session
    prompt is far past what belongs in argv."""
    from nr_workbench.harness.driver import opencode_argv

    invocation = opencode_argv(Path("p.md"), turns=200, model=None)

    assert invocation.prompt_on_stdin is True
    assert "p.md" not in " ".join(invocation.argv)


def test_opencode_runs_with_auto_not_a_blanket_bypass() -> None:
    """--auto approves only what is not explicitly denied.

    Measured: with the plugin disabled via --pure, `nrw promote` was still
    refused by the deny rule alone. That is one more independent mechanism than
    Claude Code has under --permission-mode bypassPermissions, which drops its
    deny list entirely.
    """
    from nr_workbench.harness.driver import opencode_argv

    argv = opencode_argv(Path("p.md"), turns=1, model=None).argv

    assert "--auto" in argv
    assert "--format" in argv and "json" in argv


def test_opencode_project_has_a_guard_plugin_for_each_opencode_version(
    opencode_project: Path,
) -> None:
    """OpenCode 1 calls every export of a plugin file as a function; OpenCode 2
    loads only a default export that is a plain object with an id and a setup.
    Measured on 2.0.22: given the OpenCode 1 file alone, it refused it at load
    and nothing was refused before it ran."""
    plugins = opencode_project / ".opencode" / "plugins"
    first = (plugins / "nrw-guard.js").read_text(encoding="utf-8")
    second = (plugins / "nrw-guard-opencode2.js").read_text(encoding="utf-8")

    assert "export default" not in first
    assert re.search(r"^export default \{$", second, re.MULTILINE)
    assert 'id: "nrw-guard"' in second
    assert 'ctx.tool.hook("execute.before"' in second
    assert '"shell"' in second  # the tool OpenCode 2 runs a command with


#: Loads a guard plugin the way its OpenCode version does, fires its hook for
#: three commands, and prints what each one met.
_DRIVE_PLUGIN = """
import { pathToFileURL } from "node:url"
const [plugin, form, directory] = process.argv.slice(2)
const mod = await import(pathToFileURL(plugin).href)
let before
if (form === "1") {
  // OpenCode 1 calls every export as a plugin function.
  const all = await Promise.all(Object.values(mod).map((f) => f({ directory })))
  before = (tool, command) =>
    Promise.all(all.map((h) => h["tool.execute.before"]({ tool }, { args: { command } })))
} else {
  // OpenCode 2 checks for a plain object, then runs its setup.
  if (typeof mod.default !== "object") throw new Error("default export is not an object")
  const hooks = {}
  await mod.default.setup({
    location: { directory },
    tool: { hook: async (name, fn) => { hooks[name] = fn } },
  })
  before = (tool, command) => hooks["execute.before"]({ tool, input: { command } })
}
const met = async (tool, command) => {
  try { await before(tool, command); return "ran" } catch (error) { return error.message }
}
const shell = form === "1" ? "bash" : "shell"
console.log(JSON.stringify({
  allowed: await met(shell, "nrw check"),
  refused: await met(shell, "nrw promote x"),
  not_a_shell: await met("read", "nrw promote x"),
}))
"""

_JS = shutil.which("node") or shutil.which("bun")


@pytest.mark.skipif(
    _JS is None or sys.platform == "win32",
    reason="needs node or bun; the stand-in nrw is a POSIX script",
)
@pytest.mark.parametrize(
    ("plugin", "form"), [("nrw-guard.js", "1"), ("nrw-guard-opencode2.js", "2")]
)
def test_opencode_guard_plugin_refuses_what_nrw_agent_guard_refuses(
    opencode_project: Path, tmp_path: Path, plugin: str, form: str
) -> None:
    """Each plugin, loaded the way its OpenCode version loads it, asks
    `nrw agent guard` and turns a refusal into the error that stops the call."""
    shim = opencode_project / ".nrw" / "bin" / "nrw"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text(
        "#!/bin/sh\n"
        "if [ \"$4\" = 'nrw promote x' ]; then\n"
        "  echo 'Refused by nr-workbench (promote): ask a person.' >&2; exit 2\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    module = tmp_path / "plugin.mjs"  # .mjs: ESM wherever it is loaded from
    shutil.copy(opencode_project / ".opencode" / "plugins" / plugin, module)
    driver = tmp_path / "drive.mjs"
    driver.write_text(_DRIVE_PLUGIN, encoding="utf-8")

    done = subprocess.run(
        [_JS, str(driver), str(module), form, str(opencode_project)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {
        "allowed": "ran",
        "refused": "Refused by nr-workbench (promote): ask a person.",
        "not_a_shell": "ran",
    }


def _launcher(tmp_path: Path, name: str, version: str | None) -> list[str]:
    """A stand-in for `opencode` that answers `--version` with *version*."""
    script = tmp_path / f"{name}.py"
    body = "raise SystemExit(1)" if version is None else f"print({version!r})"
    script.write_text(body + "\n", encoding="utf-8")
    return [sys.executable, str(script)]


def test_opencode_2_runs_each_session_on_its_own_server(tmp_path: Path) -> None:
    """Without --standalone, OpenCode 2 runs the session in its shared
    background service: killed at its timeout, the `opencode run` nrw started
    went away and the session's command ran on (measured on 2.0.22).
    OpenCode 1 has no such flag; a launcher that will not say its version
    gets it, because that failure is loud and the other one is not."""
    from nr_workbench.harness.driver import opencode_argv

    def argv(version: str | None) -> list[str]:
        launcher = _launcher(tmp_path, f"v{version}", version)
        return opencode_argv(Path("p.md"), turns=1, model=None, launcher=launcher).argv

    assert argv("opencode v2.0.22")[:2] == ["run", "--standalone"]
    assert "--standalone" not in argv("1.18.18")
    assert argv(None)[:2] == ["run", "--standalone"]
