# Skills

This folder is the **single source of truth** for the reusable "standards" the
AI assistants apply in this repository. It is tool-neutral: both **Claude Code**
and **GitHub Copilot** consume the same `SKILL.md` files, so there is no
duplicated content to keep in sync.

Skills follow the [Agent Skills specification](https://agentskills.io/specification),
with a few extra frontmatter fields (`version`, `scope`, `edit_policy`, `review`,
`metadata`) that the spec permits as extra metadata.

## Layout

```
skills/
├── review-design/SKILL.md                 # design / architecture review standard
├── review-security/SKILL.md               # security review standard
├── review-test/SKILL.md                   # test-quality review standard
├── python-code-standards/SKILL.md         # how to write Python here (code + docstrings)
├── python-testing/SKILL.md                # how to write tests here
├── python-integration-patterns/SKILL.md   # Flask / FastAPI / FastMCP / Click starters
└── <domain>/<skill-name>/SKILL.md         # domain skills, grouped by domain
```

Three kinds of skill live here:

- **Review standards** (`review-*/`, flat) — the criteria the reviewer subagents
  apply. Repo-tooling, not a science domain.
- **Reference / authoring standards** (`python-*/`, flat) — the code, testing, and
  framework guidance that would otherwise bloat the always-loaded instruction
  files. They have **no dispatcher agent**; the assistant reads them on demand,
  guided by the "Reference standards" pointers in
  [`.github/copilot-instructions.md`](../.github/copilot-instructions.md).
- **Installed domain skills** (`<domain>/<skill-name>/`, domain-grouped) — the
  science standards, grouped by domain; may carry `assets/`, `scripts/`, or
  `references/` subfolders. In a *scaffolded project* these are installed from
  the package by `nrw skills sync`; in this repo they are the sources under
  `src/nr_workbench/skills/`.

## How skills are consumed (thin dispatcher + fat skill)

Neither tool auto-discovers this folder, but both natively discover their own
agents directory. So the design and security review standards each have a
matching **thin dispatcher agent** in `.claude/agents/` (Claude Code) and
`.github/agents/` (Copilot). `review-test` has none: the assistant applies it
itself when it reviews tests. The dispatcher is a ~20-line stub — a role
sentence, a numbered procedure, and an output contract — whose first step is:

> Read `skills/review-<x>/SKILL.md` and follow it exactly.

All the actual criteria live in the `SKILL.md`. The dispatcher files in the two
trees are kept **byte-identical**, so the substantive content exists exactly
once, here.

`nrw skills sync` uses the same pattern: it copies a domain skill's full folder
into `skills/<domain>/<name>/` and auto-generates a matching thin dispatcher in
both agents directories so the skill is immediately invocable.

> **Edit policy:** to change a standard, edit its `SKILL.md` here — never the
> dispatcher agents, which only load and apply it.

The **reference / authoring skills** (`python-*`) work differently: they have no
dispatcher and are not invoked as subagents. The instruction files point to them
by path (e.g. "when writing tests, consult `skills/python-testing/SKILL.md`") and
the assistant reads them on demand. This keeps the always-loaded instructions
short while keeping the detail one `Read` away for either tool.

## Authoring a skill

- The skill directory name **must match** the frontmatter `name:` (lowercase,
  hyphens only).
- Keep `SKILL.md` under ~500 lines. Move long reference material to a
  `references/` subfolder, scripts to `scripts/`, and templates/data to
  `assets/`.
- Write the `description` as an imperative statement of what the skill does plus a
  "Use when…" trigger clause — this is what the assistant matches against.
- Populate `metadata.tags` (the neutron-skills retriever scores against
  `tags`/`instruments`/`techniques`).

### Frontmatter contract

```yaml
---
name: review-design            # must equal the folder name
description: >
  What the skill does. Use when <trigger>.
version: 1                     # this template's baseline
scope: project-review-standard
edit_policy: >
  Single source of truth; edit here, not in the dispatcher agents.
review:                        # human-review provenance (optional to fill)
  status: pending
  reviewer: null
  reviewed_on: null
  basis: []
  notes: null
metadata:
  tags: [design, review-standard]
---
```

### Body anatomy

The review standards follow this section order:

1. `## Overview`
2. `## When to Use` — including a "do **not** use this for X — see sibling skill"
   cross-link
3. `## Operating rules` — read-only; every finding cites `path:line`; prioritize
   by impact; heuristics are signals, not laws
4. `## Severity ladder` — an impact-based CRITICAL/HIGH/MEDIUM/LOW/INFO table
5. `## Review categories` — numbered
6. `## Output format` — a prose markdown report (no machine-readable block)
7. `## Rationalizations` — an Excuse | Rebuttal table that pre-empts under-reporting
8. `## Red Flags` — meta-checks on the review's own quality
9. `## Verification` — a checkbox self-audit the reviewer runs before returning

Reference / authoring skills (`python-*`) are guidance, not reviews, so they use a
lighter shape — `## Overview`, `## When to Use`, then the standards/examples — and
omit the severity ladder, rationalizations, and verification sections.

Domain skills fetched from neutron-skills use that library's v2 anatomy
(Overview / When to Use / Process / Rationalizations / Red Flags / Verification);
keep them as authored.
