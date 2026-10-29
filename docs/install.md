# Installing nr-workbench

The short version, on macOS and Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/neutrons-ai/aure-workbench/main/install.sh | sh
```

and on Windows, in PowerShell:

```powershell
irm https://raw.githubusercontent.com/neutrons-ai/aure-workbench/main/install.ps1 | iex
```

Then `nrw doctor`.

## What the installer actually does

Piping a script into a shell deserves an explanation, so here is the whole of it.
The script is [`install.sh`](../install.sh), and it is worth reading before you run
it — everything in it is a definition until the `main "$@"` on the last line, which
is also what stops a truncated download from running half an installation.

1. **Checks the platform** — macOS or Linux; anything else stops with a pointer to
   the PowerShell command or WSL2.
2. **Checks for `git`.** It is needed twice over: to fetch AuRE, which is pinned by
   commit SHA rather than published to PyPI, and at run time, because every fit
   records the commit of the project it ran in. `curl` is checked too, but only on
   the path that has to download uv. A missing tool gets a package-manager hint
   rather than just a complaint.
3. **Installs [uv](https://github.com/astral-sh/uv)** if you do not have it, into
   `~/.local/bin`. uv is a single binary with no dependencies of its own.
4. **Runs `uv tool install`**, which creates an isolated environment holding
   nr-workbench and everything it needs — refl1d, bumps, scipy, matplotlib, AuRE —
   and puts `nrw` and `nr-workbench` on your PATH.
5. **Verifies** by running `nrw --version`, and tells you if the `nrw` your shell
   resolves is a *different* one (an old checkout's virtualenv still ahead on PATH,
   which otherwise fails in a confusing way much later).

Nothing is installed outside your home directory and nothing needs `sudo`.

### Why uv rather than pip

nr-workbench needs Python 3.11 or newer, and the Python that comes with macOS is
3.9. `pip install` therefore starts with "first, obtain a suitable Python", which
is the step that actually stops people. uv downloads a private CPython 3.13 for
the tool environment, so the one-liner works on a machine with no usable Python at
all — and because it is an isolated *tool* rather than a virtualenv you activate,
`nrw` simply works in any shell, in any directory, afterwards.

## Options

The scripts take no arguments — a command run through `curl | sh` or `irm | iex`
has no way to receive any — so everything is an environment variable.

| Variable | Default | Meaning |
|---|---|---|
| `NRW_VERSION` | `main` | Git ref (branch, tag or commit) to install |
| `NRW_REPO` | this repository | Git URL to install from |
| `NRW_PYTHON` | `3.13` | Python version for the tool environment |
| `NRW_EXTRAS` | *(none)* | Extras to include, comma-separated: `nexus` for HDF5/NeXus reading, `isaac` for publishing fits to the ISAAC Portal, `tiled` for `nrw tiled` |
| `NRW_UV_VERSION` | *(latest)* | Pin uv to a version, e.g. `0.12.17` |
| `NRW_SYSTEM_CERTS` | `0` | `1` to use the system trust store from the start |
| `NRW_INSTALL_DRY_RUN` | `0` | `1` prints what would run and exits, changing nothing |

```bash
# a tagged release, with the NeXus extra
curl -fsSL https://raw.githubusercontent.com/neutrons-ai/aure-workbench/main/install.sh \
  | NRW_VERSION=v0.2.1 NRW_EXTRAS=nexus sh

# with the tools that export and push fits to the ISAAC Portal
curl -fsSL https://raw.githubusercontent.com/neutrons-ai/aure-workbench/main/install.sh \
  | NRW_EXTRAS=isaac sh

# see what it would do, without doing it
curl -fsSL https://raw.githubusercontent.com/neutrons-ai/aure-workbench/main/install.sh \
  | NRW_INSTALL_DRY_RUN=1 sh
```

Values are validated before they are used. They end up inside a PEP 508
requirement, and a stray `]` or `@` in `NRW_EXTRAS` would replace the direct
reference with a different repository — which `uv tool install` would then build
and execute. The installer refuses anything outside the documented character set
and prints the requirement it is actually about to install, with any credentials
in the URL stripped.

## What you are trusting

Two things, and it is better to say so than to imply otherwise:

- **This repository.** The command fetches `install.sh` from `main`, so whoever can
  push there can change what runs on your machine.
- **[astral.sh](https://astral.sh).** The script downloads uv's own installer from
  `https://astral.sh/uv/install.sh` and runs it. That fetch is HTTPS to a fixed URL
  and the download is checked before it is executed, but it is not pinned to a
  version or verified against a checksum, so it means "the current uv". Set
  `NRW_UV_VERSION` to pin it, or install uv yourself first — the script uses an
  existing uv if it finds one and never downloads anything in that case.

Everything the installer writes lives under your home directory. It never uses
`sudo`; where a missing tool needs one, it prints the command for you to run.

## Upgrading

Re-run the installer. It passes `--force --reinstall-package nr-workbench`, so a
moved `main` is fetched again rather than served from uv's cached checkout of that
ref, while the heavy scientific dependencies stay cached and the upgrade takes
seconds.

Run `nrw init` in each of your existing project folders afterwards. It never
overwrites a file you have edited, and it refreshes the generated JSON Schema and
the skills — `nrw doctor` tells you when those are stale.

## Uninstalling

```bash
uv tool uninstall nr-workbench
```

Your projects are untouched; they are ordinary directories.

## Installing manually

If you would rather not run a script, or you are setting up a development
environment, the underlying command is no secret:

```bash
uv tool install --python 3.13 \
  "nr-workbench @ git+https://github.com/neutrons-ai/aure-workbench.git@main"
```

Or with plain pip, into a virtual environment you manage yourself, on a machine
that already has Python 3.11+:

```bash
python3 -m venv venv && source venv/bin/activate
pip install git+https://github.com/neutrons-ai/aure-workbench.git
```

Note that nr-workbench is not on PyPI and cannot be: it depends on
[AuRE](https://github.com/neutrons-ai/aure) through a PEP 508 direct reference,
and PyPI rejects any distribution that carries one. Installing from git is the
only route, which is part of why the one-liner exists.

For development, clone and install editable:

```bash
git clone https://github.com/neutrons-ai/aure-workbench.git
cd aure-workbench
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"            # ".[dev,isaac]" to work on ISAAC publishing
pre-commit install
```

## Troubleshooting

**`nrw: command not found` right after a successful install.** The tool directory
(`~/.local/bin`) is not on your PATH yet. The installer runs `uv tool update-shell`,
which writes the change to your shell profile — open a new terminal, or
`export PATH="$HOME/.local/bin:$PATH"` in this one.

**`nrw` runs, but it is the wrong one.** If the installer warned that the `nrw` on
your PATH is not the one it just installed, you have an older install — usually a
virtualenv from a clone — earlier in your PATH. Remove it, or put `~/.local/bin`
first. Inside a project, `nrw doctor` reports the same conflict.

**`invalid peer certificate: UnknownIssuer`.** Your network inspects TLS, which is
normal at a lab or on a campus, and uv's bundled certificate list does not include
the proxy's root. The installer notices this and retries against the system trust
store by itself; if you want to skip the failed first attempt, set
`NRW_SYSTEM_CERTS=1`. If it still fails, set `HTTPS_PROXY`.

**The URL serves a stale script.** `raw.githubusercontent.com` is behind a
CDN with a short cache, so for a few minutes after a change to `install.sh` you
may fetch the previous one. Nothing else about it is cached; the packages come
from PyPI and GitHub directly.

**A script that checks `$LASTEXITCODE` thinks a failed install succeeded.** Only
on Windows, and only through `irm | iex`: `exit` inside a piped script would close
the user's console window, so the script sets `$LASTEXITCODE` instead and exits
non-zero only when it is run as a file. Automation should test `$LASTEXITCODE`, or
invoke the script with `pwsh -File`.

**The install is slow the first time.** It is downloading a Python interpreter and
about 400 MB of scientific packages. Later installs and upgrades reuse uv's cache.

**`git` is missing.** macOS: `xcode-select --install`. Debian or Ubuntu:
`sudo apt-get install -y git`. Fedora: `sudo dnf install -y git`. Windows:
`winget install --id Git.Git -e`, then open a new PowerShell window.

## Windows status

`install.ps1` is a faithful port of the shell installer and installs the same
package, but Windows support in nr-workbench itself is newer than the rest of the
project and is not covered by CI. Scaffolding, model specs, fitting, reporting and
`nrw serve` are expected to work. Two things are POSIX-only today: `nrw agent run`
(the unattended assistant session, which uses process groups and signals) and data
imports that link rather than copy. If you need those, use WSL2 and the Linux
one-liner.
