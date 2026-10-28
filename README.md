# AuRE Workbench
A project workbench for neutron reflectometry analysis at the SNS Liquids
Reflectometer (REF_L / BL-4B).

This project is a companion to the AuRE - Automated Reflectivity Evaluator repository.

`nr-workbench init` scaffolds an analysis project: one directory layout for
every sample, a curated `skills/` folder that Claude Code, GitHub Copilot and
OpenCode all read, and a provenance ledger that keeps every result linked to the
script, data, and environment that produced it.

## Install

macOS and Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/neutrons-ai/aure-workbench/main/install.sh | sh
```

Windows, in PowerShell:

```powershell
irm https://raw.githubusercontent.com/neutrons-ai/aure-workbench/main/install.ps1 | iex
```

That installs [uv](https://github.com/astral-sh/uv) if you do not already have
it, then nr-workbench as an isolated tool with its own private Python 3.13 — so
`nrw` is on your PATH with no virtual environment to activate, and a machine
whose only Python is the 3.9 that ships with macOS is fine. It needs `git` (and
`curl`, to fetch uv the first time), and nothing else. Then:

```bash
nrw doctor
```

[docs/install.md](docs/install.md) explains what the script does line by line,
how to pin a version or add an extra, how to upgrade, how to uninstall, and how
to install manually if you would rather not pipe a script into a shell.
[CONTRIBUTING.md](CONTRIBUTING.md) has the development setup.

## Why

The previous way of working was a directory per beamtime, each with its own
layout, and fitting scripts named `…-corefine-tNR-test.py`, `…-t0.py`,
`… copy.py`. It became impossible to say which script produced a published
figure. Writing a time-resolved co-refinement by hand made it worse: the
reference model is 341 lines, ~200 of them copy-pasted parameter tying, with
absolute paths belonging to a different user baked in.

nr-workbench replaces that with:

- **One layout.** `samples/<id>/` for everything; beamtime is metadata.
- **Immutable, attributed results.** Every fit writes a new directory recording
  the spec, the script, every input hash, and the environment.
- **A declarative model spec.** Steady-state co-refinement *and* time-resolved
  series with functional constraints in one schema, generating a readable,
  standalone refl1d script.
- **Skills the agent actually reads**, in the tool-neutral repo-root layout.

## Use

```bash
mkdir my-beamtime && cd my-beamtime
nrw init --beamtime june2026 --ipts IPTS-34567
nrw doctor
nrw sample new Sample4 --title "ionomer on copper"
```

Then copy reduced data into `samples/Sample4/data/steady/` and
`samples/Sample4/data/tnr/`, open the folder in VS Code, and work with Claude
Code. `nrw init` is idempotent and safe to run on top of an existing beamtime
folder — it never overwrites a file you have edited.

### Organizing a beamtime's runs

While an experiment is running, `nrw serve` has an **Experiment** page that
lists every run as the reduction writes it, with whether it has finished
arriving. From an empty folder, `nrw init` then `nrw serve` is enough:
**Settings** is where you set the IPTS and the data folder, and check the
folder before choosing it. Select runs, assign them to a sample with a condition, describe the
sample, and **apply**. The data is copied into the sample and its `sample.md`
is written. Nothing is overwritten, and only complete runs are copied. The same
from the command line:

```bash
nrw experiment status
nrw experiment assign 234277 234280 --sample Sample4 --condition OCV
nrw experiment apply --write
```

The organization is kept in `experiment/*.parquet`.
**[docs/experiment.md](docs/experiment.md)** walks through it.

To share it with everyone on the experiment, `nrw tiled push` writes it, and
the starred and final fits, to the experiment's node on tiled.ornl.gov, and
`nrw tiled status` says what is there. A colleague starting fresh runs `nrw
tiled pull --context --data --fits` to bring back the catalog, the reduced
files, and each fit as it was recorded; `nrw fit run --from <fit_id>` runs
one again. Each acts with your own Tiled login (`nrw tiled login`), and needs
the installer's `NRW_EXTRAS=tiled`.

### Choosing an assistant

A scaffolded project carries the instructions, subagent stubs and limits for
each assistant it is set up for. Claude Code and GitHub Copilot are the default;
`--harness` names the set explicitly and it is remembered in `nrw.toml`:

```bash
nrw init --harness claude --harness opencode   # both
nrw init --harness opencode                    # OpenCode alone
```

Sites that cannot install Claude Code, or that need to point an assistant at a
locally hosted model, want OpenCode: it takes a provider and model in its own
`opencode.json` rather than being tied to one vendor.

Two things to know. **`nrw agent run` drives Claude Code or OpenCode**
(`--harness opencode`), each limited by its own guard: Claude Code's
`PreToolUse` hook, or OpenCode's guard plugin and deny rules.
OpenCode has no turn cap, so a session there needs `--timeout`, and refuses to
start without one rather than run unbounded. And **narrowing the set never
deletes anything**: files for an assistant you drop stay on disk, and nrw stops
updating them, because a scientist may have edited them.

### Your first fit

If you have data and no model yet, the stack is the hard part — and it is the
one thing the tooling cannot read off the files. `nrw aure` hands that to
[AuRE](https://github.com/neutrons-ai/aure), which proposes a layer stack from a
plain-English description and iterates it against the data:

```bash
# answer six questions in samples/Cu4/sample.md first -- what the layers are,
# what it sits in, and which side the beam enters
nrw aure new Cu4               # --run N when the sample has several steady runs
nrw aure run samples/Cu4/aure/Cu4-218386/setup.yaml
nrw aure import samples/Cu4/aure/Cu4-218386/output --sample Cu4 --name first
nrw model generate samples/Cu4/models/first.yaml
nrw fit run samples/Cu4/models/first.py
```

That leaves an ordinary `samples/Cu4/models/first.yaml`, and everything after
it is the normal path. **An AuRE run is reconnaissance**: it carries no fit
record, so the fit that counts is the `nrw fit run` on the script generated
from the imported spec. It needs a language-model endpoint; without one,
`nrw model new --print-prompt` gives your coding assistant the same job with the
facts already filled in.

**[docs/first-fit.md](docs/first-fit.md) walks it through**, and
`skills/reflectometry/aure-first-fit/SKILL.md` is what an assistant follows.

**[docs/getting-started.md](docs/getting-started.md) walks the whole thing
through on real data**: two OCV states either side of an EIS sequence,
co-refined with the 15 time-resolved slices measured during it, from an empty
directory to a promoted χ² = 1.83 result. Every command and number in it was
produced by running it.

Run `nrw --help` for the full command surface.

## Fitting options

A project fits in two ways, and each is set up in its own place.

**nrw's fits** are the ones recorded in the fit index and shown under Fits. They
come from `nrw fit run`, or from **Fit…** on the Experiment page, which runs
`nrw fit run`. Each setting comes from the first of these that sets it:

1. the command line (`nrw fit run model.py --method amoeba --steps 500`), or
   the page's Fit form;
2. with `nrw fit run --from <fit_id>`, which runs a recorded fit again from
   its own problem file, the fitter, settings and seed that fit ran with;
3. the project's `nrw.toml`, under `[fit]`;
4. bumps' own default for that fitter.

When nothing names a fitter, it is **DREAM**: it samples the posterior, so its
answer comes with uncertainties. In `nrw.toml`, `[fit]` names the fitter and
holds what every fitter takes. Each fitter's own settings go in its own table,
and apply only when that fitter runs:

```toml
[fit]
method = "dream"    # dream | de | amoeba
seed = 12345        # a fixed seed makes a fit repeatable; none by default
parallel = 0        # CPUs to use: 0 means all of them

[fit.dream]
samples = 20000     # bumps' default: 10000
burn = 1000         # bumps' default: 100

[fit.de]
steps = 2000        # bumps' default: 1000

[fit.amoeba]
steps = 1000        # bumps' default: 1000
```

With these settings:

- `nrw fit run model.py` runs DREAM with 20000 samples and 1000 burn-in steps.
- `nrw fit run model.py --method amoeba` runs amoeba for 1000 steps.
- A setting a fitter does not take, such as `samples` for amoeba, is refused
  rather than ignored.

Each fit prints which settings it took from `nrw.toml`. Its record keeps every
setting it ran with, so it can be reproduced after `nrw.toml` changes. A fit an
unattended session starts can also be held to ceilings, `[agent.limits.<fitter>]`:
see [docs/agent.md](docs/agent.md), "Compute, during a beamtime".
`nrw init` writes these tables into a new project's `nrw.toml` commented out,
holding bumps' own defaults (with `pop` for DREAM and DE), so uncommenting one
changes nothing until you edit a value. For an older project, paste them in.

A model spec's `fit:` block is **not** read, and `nrw model validate` says so.
Put fit settings in `nrw.toml` or on the command line.

**AuRE's fits** are reconnaissance: they happen inside `nrw aure run`, and in
the page's **Quick fit with AuRE**. AuRE sets them up itself, from these
sources:

- **`--budget quick`**, the default: nrw writes DE, 300 steps and one
  refinement into the run's `setup.yaml`, and that wins over everything else.
- **`--budget standard`**: nrw leaves the fit to AuRE. AuRE reads `FIT_METHOD`,
  `FIT_STEPS` and `FIT_BURN` from its environment, else uses its defaults
  (DREAM, 1000 steps, 1000 burn-in). For a run nrw starts, that environment is
  the first of: the shell, the project's `.env`, `~/.nrw`, `~/.aure`.
- **AuRE's physics knobs** (`MODE_ENUMERATION` and the others): nrw sets every
  one for each run, over the shell's too, and records them in `run-env.json`
  beside the setup. A value for one of them in `~/.aure` is not used by a run
  that nrw starts.
- **The fit you keep**: after a quick fit on the page, the spec AuRE proposed is
  fitted with `nrw fit run`, starting from AuRE's values, with the project's
  fitter: `[fit]` in `nrw.toml`, else DREAM, so it carries uncertainties. That
  fit is the one that appears under Fits.
- **Again**: **Quick fit again** on a model AuRE proposed asks AuRE again, for
  example after you have added to `sample.md`. What AuRE reads there includes
  the run's condition and notes. A spec you have edited is never replaced.

`nrw.toml` says nothing about AuRE's fits, and `~/.aure` says nothing about
nrw's.

**Which file holds what:**

| File | Belongs to | Holds | Committed |
|---|---|---|---|
| `nrw.toml` | the project | the IPTS, the data folder, the watcher, the assistants, and how nrw fits (`[fit]`) | yes |
| `.env` in the project | you, on this machine | the language-model endpoint and its key; the Settings page's *Language model* writes it | no (gitignored) |
| `~/.nrw` | you | the same, for all your projects; and the ISAAC Portal and its key (`ISAAC_URL`, `ISAAC_KEY`) | — |
| `~/.aure` | you, and AuRE | the endpoint too, and AuRE's own fit defaults (`FIT_METHOD`, `FIT_STEPS`, `FIT_BURN`) | — |

For the language-model endpoint, nrw takes each variable from the first of:

1. the shell's environment;
2. the project's `.env`;
3. `~/.nrw`;
4. `~/.aure`.

The ISAAC Portal's `ISAAC_URL` and `ISAAC_KEY` follow the same order.

`nrw doctor` lists the files it read and the settings it found, with keys
redacted. Keys and endpoints never go in `nrw.toml`, because it is committed
and shared. `.env.example` in the project lists the variables.

To use Claude through the Claude Code CLI, which needs no key, choose it under
*Language model* on the Settings page of `nrw serve`. That writes
`LLM_PROVIDER=claude_code` into the project's `.env`, which then wins over
`~/.aure`, and **Check** makes one call to prove it answers. See
[docs/experiment.md](docs/experiment.md#language-model).

**What is sent, and to which model.** Four commands ask the endpoint for an
answer — `nrw model new --from-notes`, `nrw assess`, `nrw isaac export` and
`nrw aure run` — and `nrw agent run` drives a harness instead; while a harness
is driving, those four stand down. [docs/llm-calls.svg](docs/llm-calls.svg)
maps them, and one diagram per command shows how its prompt is assembled:
[model new](docs/model-new-llm.svg), [assess](docs/assess-llm.svg),
[isaac export](docs/isaac-export-llm.svg), [aure run](docs/aure-run-llm.svg)
and [agent run](docs/agent-run-llm.svg).

## Provenance

Run any refl1d script -- including one you wrote by hand years ago -- and it
comes out with a complete, queryable record:

```bash
nrw fit run samples/Cu/models/cu-d2o.py --method dream --samples 5000
nrw ls                        # every fit, newest first, with freshness
nrw whence figures/fig3.svg   # what produced this?
nrw promote <fit_id> --as final --reason "converged; SLD band excludes null"
nrw check                     # CI-able: fails if a result went stale
nrw pack <fit_id>             # a zip a collaborator runs with only refl1d
```

Each fit writes an immutable directory holding the frozen script, every input
file with its sha256, the exact package versions and git state (with a patch if
the tree was dirty), and the bumps output. `nrw whence` traces a figure back to
that record even after it has been copied out of the project, because the
figures a fit writes are stamped with its id when it finishes.

Re-running an identical fit is recorded as a replicate of the first, and a
result whose data has changed underneath it is reported as `STALE` everywhere
it appears.

Fits are curated on the fit pages of `nrw serve` — star, finalize, discard,
restore and delete — or with `nrw fit star`, `unstar`, `discard`, `restore`
and `delete`; **Finalize** on a page is `nrw promote --as final`. A discarded
fit leaves the listings (`nrw ls --all` shows it) but keeps its files until you
delete them, which is refused while another fit reads them, and the record that it
ran is never lost. A sample's final fit is published to the ISAAC Portal from
its page, or with `nrw isaac export` and `nrw isaac push`, and each push is
recorded with the fit. See
[docs/experiment.md](docs/experiment.md#curating-fits).

`nrw pack` closes the last gap. A result directory records the *hashes* of its
data, not the data, so it describes a fit nobody else can run. A bundle carries
the measurements themselves at the paths the frozen script expects, plus a
`verify.py` that applies the recorded best-fit parameters and checks
chi-squared against the value it should get. The recipient needs refl1d, bumps
and numpy.

## Running unattended

During a beamtime, data keeps arriving and nobody is watching. nr-workbench can
hand each settled measurement to a coding harness and let it work:

```bash
nrw agent watch --dry-run     # what each measurement is waiting for
nrw agent watch               # analyse each one as it settles
nrw agent run Sample4         # or one session, by hand
```

It contains no decision policy, deliberately. We measured against a week of
expert analysis with the findings written down as they happened: of 17
findings, 1 was reachable by arithmetic and 11 needed judgement, and
chi-squared ranks that corpus *backwards* — both promoted fits are worse in
chi-squared than the best in their arm. So the harness decides, and this
package supplies what has to exist around it: the offline checks it reads
first, limits it cannot talk past, a bounded session, and a transcript.
Promoting or curating fits, publishing to ISAAC, `nrw aure run`, organizing the
experiment, `nrw sample reset`, `init --nested` and any `--force` are refused by
the harness's hook; the full list, with what stays allowed, is in
[docs/agent.md](docs/agent.md#the-limits).

A session refuses to start unless `## Fits to perform` in the sample's notes
says what you want. Deciding that is the one thing it must not do for itself.

**[docs/getting-started-with-agent.md](docs/getting-started-with-agent.md)
walks the setup through** — one sample first, then measurements that are still
arriving. [docs/agent.md](docs/agent.md) is the reference: what is enforced,
what is only asked for, and the evidence behind the split.

## Status

Working. The scaffold and skills, the provenance spine, the tNR assessment
tools, the model spec and generator, the web UI — its Experiment page, fit
curation and ISAAC publishing included — and the data tools are all
implemented, along with `nrw pack`, `nrw note`, `nrw assess`, `nrw isaac
export` and `push`, `nrw tiled`, and the unattended agent above.

## Development

```bash
pytest                          # tests
ruff check src tests            # lint (ruff lints and formats; no black)
ruff format --check src tests   # format
pre-commit run --all-files      # exactly what CI runs
```

`pre-commit` only checks files git tracks, so stage a new file before running
it.

The workflow, code standards, and review process are in
[.github/copilot-instructions.md](.github/copilot-instructions.md), imported by
[CLAUDE.md](CLAUDE.md). Shared standards live once in [skills/](skills/).

## License

BSD 3-Clause. See [LICENSE](LICENSE).
