# GitHub Copilot Instructions

This repository builds **nr-workbench**, a Python package that scaffolds
neutron reflectometry analysis projects for SNS REF_L (BL-4B). This file is
also imported by [`CLAUDE.md`](../CLAUDE.md), so Claude Code follows the same
rules.

> **Where the detail lives.** Reference standards (code style, testing,
> framework patterns) are `SKILL.md` files under [`skills/`](../skills/), read
> on demand so this always-loaded file stays short.

## 🎯 Core Principles

1. **Always assess before acting** — understand the current state before proposing changes
2. **Always provide itemized plans** — clear, independently testable steps
3. **Focus on progress tracking** — the user should always know what is happening and what is next
4. **Test incrementally** — each step should be verifiable
5. **Review after major changes** — use the reviewer subagents

## 🔄 Standard Workflow

**assess → plan → implement → test → review** for every request:

1. **Assess** — read the relevant files, tests and conventions; summarize what exists and what must change.
2. **Plan** — numbered steps, each independently testable; note dependencies and the tests each needs.
3. **Implement incrementally** — one item at a time; say which step you are on.
4. **Test** — run the tests, show the results, fix failures before moving on.
5. **Review (major changes)** — invoke the design / security / test reviewer subagents, address findings, update docs.

For a **bug**: assess the root cause → fix → add a regression test. For
**choosing an approach**: compare options with a recommendation before
implementing.

## 🧭 What this package is, and what that implies

`nrw init` writes a project skeleton into a user's directory. Two things follow:

- **`src/nr_workbench/templates/` and `src/nr_workbench/skills/` are package
  data.** They have no `__init__.py`, so only the
  `[tool.setuptools.package-data]` globs get them into the wheel — and those
  globs do not match dotfiles by default. If you add a template or skill, run
  `pytest tests/test_packaging.py`, which builds a real wheel and checks.
- **`nrw init` runs on top of live working directories.** It must never
  overwrite a user-edited file. `project/scaffold.py` holds that logic and is
  the most safety-critical code in the repo.

**Never import `aure` at module scope.** It costs 1.5-3 seconds and drags the
whole LLM stack. All aure access is function-local inside
`src/nr_workbench/aure_adapter.py`, and a test enforces it.

## 🛠️ Technology Stack

- **CLI**: Click, one group, lazily-imported subcommands
- **Web**: Flask + Jinja2 + Bootstrap (Plotly from CDN, no npm build)
- **Validation**: pydantic v2
- **Data**: numpy, scipy; plotting with matplotlib
- **Science**: refl1d + bumps, and AuRE for probe construction and feature extraction
- **Testing**: pytest (+ pytest-cov)
- **Lint & format**: ruff — one tool for both (`ruff check` and `ruff format`). No black.
- **Dependency audit**: pip-audit

## 📝 Code Quality

Type hints on every parameter and return, Google-style docstrings on public
functions and classes, specific exceptions with clear messages, input
validation at boundaries, and comments that explain *why*. Full templates:
[`skills/python-code-standards`](../skills/python-code-standards/SKILL.md).

## 🧪 Testing

Arrange-Act-Assert; cover normal, edge, error and integration cases; name them
`test_<function>_<scenario>_<expected>`. Favor real behavior over heavy
mocking. How-to: [`skills/python-testing`](../skills/python-testing/SKILL.md).

Tests that guard against silent failure are worth extra care here — the two
worst bugs found so far (data files missing from the wheel, `init` not being
idempotent) were both invisible in normal use.

## 🔍 Review

Trigger a review after a new feature (~3+ functions), a significant refactor,
or before marking major work complete. Invoke the `design-reviewer` and
`security-reviewer` subagents — each loads its standard from
`skills/review-*/SKILL.md`. Review the tests yourself, against
`skills/review-test/SKILL.md`.

## 📚 Reference standards (read on demand)

| When you are… | Consult |
|---|---|
| writing/refactoring Python or docstrings | [`skills/python-code-standards`](../skills/python-code-standards/SKILL.md) |
| writing or changing tests | [`skills/python-testing`](../skills/python-testing/SKILL.md) |
| scaffolding a Flask/FastAPI/Click app | [`skills/python-integration-patterns`](../skills/python-integration-patterns/SKILL.md) |
| reviewing design / security / tests | `skills/review-design`, `review-security`, `review-test` |

## 🎓 Educational Approach

Users may be scientists new to software engineering. Explain *why*, not just
*what*, in plain language.

## 🧪 Skills

Shared standards live once in [`skills/`](../skills/) as `SKILL.md` files
following the [agentskills.io](https://agentskills.io/specification) spec. This
folder is the single source of truth for both assistants, and where installed
domain skills land (`skills/<domain>/<name>/`). The agents in
[`.github/agents/`](agents/) are thin dispatchers that `Read` these standards —
**to change a standard, edit its `SKILL.md`, never the dispatcher.**

Note the distinction: `skills/` holds standards *for developing this repo*,
while `src/nr_workbench/skills/` holds the reflectometry domain skills this
package *ships to users*. The latter follow the neutron-skills v2 anatomy and
are enforced by `tests/test_skills.py`.

---

**Remember**: every interaction should leave working, tested, documented code
and a clear understanding of what changed and why.
