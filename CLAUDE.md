# Claude Code Instructions

This repository builds **nr-workbench**, a Python package that scaffolds
neutron reflectometry analysis projects for SNS REF_L (BL-4B). It follows the
shared AI-assisted development workflow defined below.

@.github/copilot-instructions.md

## Claude-Specific Notes

The instructions above were written for GitHub Copilot but apply equally here.
Where they say "Copilot", read "the AI assistant".

### Scope: stick to the plan

This is a research project. These rules override the workflow above:

- **Build exactly the approved plan.** Its items, their tests and their docs,
  and nothing more: no extra features, no "while I'm here" improvements, no
  moving work between branches or phases. If something outside the plan
  seems needed, say so in one line and ask.
- **No protections for use-cases that will never happen.** The person using
  nrw writes its text and runs its commands; do not harden against them (no
  prompt-injection defences, hidden-character rules or hostile-input checks
  on their own data). Guard against what goes wrong in real use: a crash,
  lost data, a wrong result.
- **The security review runs once per PR, at the very end.** Run the
  `security-reviewer` agent after everything else is done, report what it
  found, and fix none of it until the person says which findings to act on.
- **Comments, docstrings and docs describe the code as it is.** Never explain
  it by an earlier version of the code or of the requirements ("this used
  to…", "changed from…", "before this…"). State the facts a reader needs to
  understand the code; give a reason only when an implementation, an
  equation or a design decision cannot be understood without it. This
  narrows "comments that explain *why*" above.

### What this package is

`nrw init` writes a project skeleton into a user's directory. Two consequences
shape almost every decision:

1. **Everything under `src/nr_workbench/templates/` and
   `src/nr_workbench/skills/` is data that must reach the wheel.** It has no
   `__init__.py`, so `packages.find` will not carry it — only the
   `[tool.setuptools.package-data]` globs will. Those globs do *not* match
   dotfiles by default. If you add a template,
   `tests/test_packaging.py` will tell you whether it ships.
2. **`nrw init` runs on top of live working directories.** It must never
   overwrite a file the user has edited. The three-way logic lives in
   `project/scaffold.py` and is the most safety-critical code here.

### History

The original milestone plan (M0 scaffold+skills → M1 provenance → M2 tNR
assessment → M3 spec+generator → M4 forms → M5 web UI) is implemented. Its
requirement, `docs/project.md`, was retired in bcd1c66
(`git show bcd1c66^:docs/project.md` recovers it). `docs/plan-*.md` are design
records of later work.

### Skills

Shared standards live once in [`skills/`](skills/) at the repo root — the
review standards (`review-*`) and authoring references (`python-*`) from
`ai-project-template`. The design and security reviewer subagents in
[`.claude/agents/`](.claude/agents/) are thin dispatchers that load them; review
tests yourself, against `skills/review-test`.

`src/nr_workbench/skills/` is different: those are the **reflectometry domain
skills we ship to users**, not standards for this repo. They follow the
neutron-skills v2 anatomy (Overview / When to Use / Process / Rationalizations
/ Red Flags / Verification), and `tests/test_skills.py` enforces it.

### Working with AuRE

Every `aure` import must be function-local and must live in
`src/nr_workbench/aure_adapter.py`. Importing it costs 1.5-3 seconds and pulls
the whole LLM stack; `tests/test_cli.py` fails if `nrw --help` touches it.
AuRE also declares no stable API, so the adapter is the one place a signature
change has to be fixed.

### Verification

```bash
pytest                        # ~2,900 tests, under two minutes; fits nothing
pytest -m fit                 # the 33 that run real fits, for fitting code
pytest -m tiled_server        # nrw tiled against a real Tiled 0.2.18 (uvx)
pytest --record-fits TESTS    # re-record fits the default run replays
ruff check src tests          # lint
ruff format --check src tests # format
pre-commit run --all-files    # exactly what CI runs
```
