# Ground Truths

Key findings, decisions, and verified facts discovered during development. AI
assistants do not remember previous conversations; this file is how context
survives between sessions.

Record what is **non-derivable** from the code: API quirks, packaging traps,
why a design went one way. Do not record what the code or git history already
says.

## Findings

### 2026-08-05: Skills live at the repo root, never `.claude/skills/`

Inherited from `ai-project-template`'s 2026-07-13 decision, and it applies here
too: **GitHub Copilot cannot read `.claude/skills/`.** A repo-root `skills/`
folder is the only tool-neutral home, and both assistants reach it by `Read`.

Neither tool auto-discovers that folder, which is why every skill also gets a
**thin dispatcher agent** in both `.claude/agents/` and `.github/agents/`. The
pair is byte-identical except that the `.claude` copy carries
`tools: Read, Grep, Glob, Bash` — Copilot's agent schema flags those names as
unknown, so it is omitted there. `tests/test_skills.py` asserts exactly that
difference.

To change a standard, edit the `SKILL.md`. Never the dispatcher.

### 2026-08-05: setuptools package-data globs do not match dotfiles

`templates/**/*` follows glob semantics: `*` matches neither a leading dot nor
anything inside a dot-prefixed directory. With only that pattern, the wheel
shipped without `.gitignore`, `.github/copilot-instructions.md`,
`.vscode/settings.json` and `.vscode/extensions.json` — so an installed
nr-workbench would scaffold projects with **no Copilot instructions and no
editor schema wiring**, while an editable install looked perfectly fine.

Fixed with explicit dot patterns in `[tool.setuptools.package-data]`:
`templates/**/.*`, `templates/*/.*/*`, `templates/*/.*/**/*`.

`tests/test_packaging.py` builds a real wheel and compares its contents against
the source tree, so this cannot silently reopen. Note that the test copies the
repo to a temp directory **with `*.egg-info` stripped** first: a `SOURCES.txt`
left by an earlier build is reused by setuptools and re-includes files the
current configuration no longer packages. Without that strip the test passes
even with the fix reverted — measured, not theorised.

### 2026-08-05: AuRE ships no SKILL.md files in its wheel — FIXED, merged as `3021fee`

AuRE's `[tool.setuptools.package-data]` covered only `"aure.web"`, so none of
its eight `SKILL.md` files reached the wheel or sdist. Invisible under
`pip install -e .`, but `Dockerfile:8` installs non-editable — so
`ghcr.io/neutrons-ai/aure` ran with an empty skill registry, injecting no
domain knowledge into any prompt, with no error and no log line.
`SkillRegistry._scan` only warned when the *directory* was missing, which it
never was.

Fixed upstream (`neutrons-ai/aure` PR #20, merged 2026-08-05 as `3021fee`) and
verified from this side: a wheel built from aure main, installed non-editable
into a clean venv, exposes all eight `SKILL.md` files.

**Consequence for later milestones.** nr-workbench can now read AuRE's domain
skills from the installed package instead of vendoring copies. The three skills
shipped in M0 are unaffected — `nr-workbench-project` is new,
`tnr-change-assessment` comes from experiments-2025, and
`neutron-reflectometry` is a merge of both sources rewritten into the v2
anatomy — but the four planned pure-AuRE skills (`thin-layer-degeneracy`,
`solvent-contrast-matching`, `metal-oxide-interfaces`, `polymer-films`) should
be *read from upstream*, not copied. One fewer divergent copy to keep in sync.

### 2026-08-05: Enumerate aure's skills without importing aure

Confirmed working against the installed wheel:

```python
import importlib.util
from pathlib import Path

spec = importlib.util.find_spec("aure")          # does NOT run aure/__init__.py
skills_root = Path(spec.submodule_search_locations[0]) / "skills"
names = sorted(p.parent.name for p in skills_root.glob("*/SKILL.md"))
```

`find_spec` on a *top-level* package locates it without executing its
`__init__.py`, so this costs nothing and avoids the 1.5-3s LLM-stack import.
Do not use `find_spec("aure.skills")` — resolving a submodule imports the
parent, which defeats the whole point.

### 2026-08-05: Any `import aure` costs 1.5-3 seconds

`aure/__init__.py` eagerly imports `.state` and `.workflow`; the latter chains
through all eight node modules into `langchain_core`, `periodictable` and
`scipy`. There is **no lightweight entry point** — importing
`aure.tools.feature_tools` pays the full cost.

So every aure import in this package is function-local, confined to
`src/nr_workbench/aure_adapter.py`, and `tests/test_cli.py::
test_help_does_not_import_heavy_modules` runs `nrw --help` in a subprocess and
fails if `aure` (or refl1d, bumps, matplotlib, scipy, langchain_core) reaches
`sys.modules`.

To locate aure's package directory without executing its `__init__.py`, use
`importlib.util.find_spec("aure")`.

### 2026-08-05: Pin aure by commit SHA, never by tag or `@main` — AMENDED 2026-09-13

Its `pyproject.toml` has said `version = "0.1.0"` across every release, while
tags reach `v0.1.3` — and `v0.1.0` and `v0.1.1` point at the *same* commit. pip
cannot tell two aure builds apart from their metadata, so a tag pin plus
resolver caching can silently give you the wrong one.

Consequence for provenance: the version string is worthless as an identifier.
`commands/doctor.py::_aure_commit` reads the real commit from pip's
`direct_url.json`, and that is what belongs in a fit manifest.

Also note aure declares `requires-python = ">=3.9"`, which is wrong:
`model_builder.py` uses `int | None` in an evaluated annotation with no
`from __future__ import annotations`, so `import aure` raises on 3.9. We
require >= 3.11 independently.

AuRE v1.0.0 finally reports a real version, so the "every build says 0.1.0"
half of this is history. **The conclusion is unchanged**: we pin a SHA on
`main`, which moves between releases, so two installs can report `1.0.0` and be
different code. The commit is still the identifier. See the 2026-09-13 entry.

### 2026-08-05: `nrw init` must preserve project identity on re-run

`created` is stamped into `nrw.toml` and `README.md`. Regenerating it each run
made those two files differ every time, so `init` reported "upgrade 2 file(s)"
forever and was not idempotent — and the field came to mean "last init" rather
than "created", which is not what a provenance record wants.

`commands/init_cmd.py::_build_context` now reads the existing `nrw.toml` and
reuses `created`, `name`, `beamtime` and `ipts` unless overridden on the
command line. So a bare `nrw init` in an existing project is a true no-op and
does not blank fields the user set earlier.

### 2026-08-05: Observe a script's inputs; do not guess them from its source

Every real fit script in experiments-2025 builds its data path the same way:

```python
DATA = os.path.join(os.path.dirname(__file__), "..", "data", "steady", fname)
```

The full path never appears as a literal, and `fname` is often assembled from a
run number, so scanning the AST for string literals finds nothing. Measured: the
first implementation recorded 1 input (the script) for a script that read a data
file.

`fitting/runner.py::track_opened_files` installs a `sys.addaudithook` on the
`open` event and records every genuine file open during `runpy.run_path`. That
is exact rather than heuristic and works regardless of how the path was
assembled — through `numpy.loadtxt`, refl1d's loaders, or runtime string
building.

Two consequences worth knowing:

- **Audit hooks cannot be removed once installed.** One hook is installed lazily
  and gated on a module-level collector that the context manager sets and
  restores, so nesting works and nothing accumulates after the block.
- **The script is loaded once**, and the loaded problem is handed to `run_fit`.
  Loading it twice would double every side effect a script has.

`--dry-run` cannot observe anything, so it falls back to the literal scan plus a
unique-basename search of the project, and the two paths can disagree. That is
accepted: the recorded path is the exact one.

### 2026-08-05: bumps `fit(export=...)` still silently drops the uncertainty block

Inherited from nr-analyzer and **re-verified against bumps 1.0.4**, because a
workaround nobody rechecks becomes folklore. Measured on the same problem:

| call | files written | `-err.json` | `-chain.mc.gz` |
|---|---|---|---|
| `fit(problem, export=dir)` | 9 | no | no |
| `fit(problem)` then `export_fit(dir, problem, result)` | 19 | yes | yes |

`fit(export=...)` forwards `result.state` — a bare `MCMCDraw` — where
`export_fit` expects an `OptimizeResult`. Its
`getattr(fit, "fit_state", getattr(fit, "state", None))` resolves to `None` and
the entire uncertainty branch is skipped, with no error.

`tests/test_runner.py::test_bumps_export_kwarg_still_drops_the_uncertainty_block`
pins the bug deliberately: when bumps fixes it, that test fails and tells us the
workaround can go.

### 2026-08-05: fit_id collides for same-second replicates

`fit_id` is a second-resolution timestamp plus a content hash, so two forced
replicates of an identical run started in the same second produce the same id
and the second `mkdir` fails. `record.py::create_unique` appends `-2`, `-3`, …
Because `mkdir` is atomic, the same loop is what makes concurrent fits safe:
whichever process loses the race takes the next suffix.

### 2026-08-05: `ruff format` will reformat vendored files unless excluded — SUPERSEDED 2026-08-12

`_vendor/result_manifest.py` is a byte-identical copy of a contract shared
across analyzer_tools, data-assembler and nr_isaac_format. A plain
`ruff format src tests` reformatted it and broke that guarantee — caught by
`tests/test_vendor.py` on its first real run. `extend-exclude` in
`[tool.ruff]` now covers `src/nr_workbench/_vendor`.

The `_vendor/` mechanism this describes is gone — see the 2026-08-12 entry
below. The file now lives at `provenance/result_manifest.py`, is linted and
formatted like any other module, and is no longer tracked as byte-identical
to anything external.

### 2026-08-05: The tNR refactor is pinned by golden files, not by review

`tnr_chi2.py` was 1992 lines in one module. Splitting it was worth doing, but a
subtle change to a coadd weight or a variance sign would be invisible in review
and would quietly corrupt every future assessment.

So the split was done **mechanically**, extracting source by line range so the
numerics moved byte-for-byte, and the ASCII tables in `tests/data/tnr_golden/`
were captured from the **original** tool before any of it started. All six
tables still compare byte-for-byte identical.

Two consequences worth keeping:

- `tests/data/tnr_golden/` is only regenerated when a numerics change is
  deliberate, and the commit has to say so.
- `zip(strict=)` is *not* added in `tnr/ascii.py` or `tnr/plots.py`, because it
  changes behaviour — raising instead of truncating — in code nobody has
  re-derived. Ruff has a per-file ignore for B905 there.

Structural notes: `type_style`/`ordered_types` moved to `tnr/intervals.py`
because both the metrics and the plots need them and neither should drag
matplotlib into the other; `plots.py` forces the Agg backend at import;
`click.echo(..., err=True)` became `tnr/notify.py` so `metrics/` has no CLI
dependency.

### 2026-08-05: Three interpretation bugs the fixture caught

The synthetic run in `tests/tnr_fixture.py` injects a *known* sigmoidal
trajectory and a *known* oscillatory Q template, so the assessment can be
checked against ground truth rather than merely for plausibility. That caught
three wrong answers that all looked reasonable:

1. **Trajectory: chord-residual tests break on overshoot.** Comparing a(t)
   against a straight line through its endpoints classifies a textbook sigmoid
   as `non-monotonic` whenever the curve overshoots and settles back, because
   the chord then ends below the plateau. Slope *concentration* is the actual
   defining property of a sigmoid and is unaffected by where the endpoints land.
2. **Flat needs error bars.** Without `sigma_a` the classifier has no noise
   scale, so pure noise reliably reads as `non-monotonic` — it wanders. The
   test is now whether the first and last thirds differ by more than 3 combined
   standard errors.
3. **Variogram: a fixed threshold is wrong.** γ = 1 is the noise floor and the
   metric reports its own spread, so "is it rising?" is a significance question.
   Real run 223995 sits at γ = 1.16 with an error of 0.032 — a 5σ rise that the
   original threshold of γ > 1.3 called flat. That produced output which said
   "nothing is changing" directly above "max |a/σ| = 7.3".

Related: when the variogram and the amplitude disagree, `build_verdict` now
says `ambiguous` and names the likely cause (a reference block spanning a
period when the sample was already moving) rather than silently believing the
amplitude. The reading order says *if the variogram is flat, stop*, and the
verdict has to honour that.

### 2026-08-05: bumps CAN express logistic and exponential constraints

The plan flagged this as a spike that might force `exponential` and `logistic`
to be deferred. It does not. Verified against bumps 1.0.4:

- `bumps.parameter.exp` does **not** exist, which is what makes it look
  impossible at first glance. But `bumps.parameter.pmath` carries the full set
  (`exp`, `log`, `sqrt`, `sin`, `tanh`, …), and `np.exp(parameter)` also
  dispatches correctly and returns an `Expression`.
- A `FitProblem` whose stack contains such an expression **serializes and
  round-trips** through `bumps.serialize`.

So every constraint form ships. Do not conclude from a missing
`bumps.parameter.exp` that transcendental constraints are unavailable.

### 2026-08-05: the model gate, and the two things it caught

`tests/test_model_gate.py` drives the hand-written
`Cu-THF-218386-full-sequence.py` and the generated script from the *same*
parameter vector and compares chi-squared. It passes at 1.7e-16 — floating-point
noise — across 21 experiments and 40 free parameters, with a 62-line spec
standing in for 343 lines of Python.

Getting there required two corrections that no amount of reading would have
found:

**1. The apr2025 scripts used a different resolution convention.** The initial
generated script differed from the reference by ~1e-4 in chi-squared — small
enough to look like rounding, large enough to be real. The cause was `dL`: the
reference computes the SNS moderator emission-time polynomial, while
`angular_only` sets `dL = 0`.

**Resolved 2026-08-05: BL-4B standardises on angular-only (`dL = 0`), and the
moderator variant is not supported.** It computed
`dL = delta_wl_over_wl(wl) * q` — multiplied by q rather than by wl, which is
dimensionally wrong. Carrying both would mean two sets of results that cannot
be compared, and only one of them is right.

Consequences, all live:

- A spec asking for `resolution: moderator` is **rejected with an explanation**,
  not silently accepted. Failing loudly matters here because the alternative is
  a fit that runs fine and produces numbers nobody can compare with anything.
- **Fits made under the moderator convention must be re-run, not compared.**
  The same model on the same data gives χ² 101.983 under moderator and 101.994
  under angular-only.
- The vendored gate reference is normalised to `dL = 0` **on both sides**, so
  `tests/test_model_gate.py` measures the model structure — co-refinement,
  parameter sharing, constraints — rather than a convention that has been
  dropped. Its banner says so.

**2. Angle segments do not always share a normalisation.** The reference gives
ocv1's 3.5° segment its own intensity with a wider range, and says why:
*"The third run has a different intensity, so we don't share that parameter
with the first two runs."* The schema's original `per: model|state|measurement`
could not express "share within the state, except this one segment".

Fixed by letting `in:` name a single measurement as `group#index`, with a
specificity rule: a measurement-level declaration outranks a group-level one.
Unreferenced parameters are then pruned, so a fully-overridden group parameter
does not linger and inflate the count.

### 2026-08-05: `nrw check` polices generated scripts, not hand-written ones

The first version of the script-drift check flagged any `models/*.py` with no
spec as an `orphan-script`. It broke an M1 test, and the test was right: running
an existing hand-written script **unchanged, with no spec and no migration** is
the adoption path this package promises. Flagging every such file would make
`check` useless for exactly the case it is meant to support.

The rule is now: only files that *claim* to be generated are policed.

| condition | reported as |
|---|---|
| generated banner, self-hash mismatch | `hand-edited-script` |
| generated banner, spec changed since | `stale-script` |
| generated banner, spec gone | `missing-spec` |
| `HAND-OWNED SCRIPT` banner (a fork) | skipped -- hand-owned by design |
| no banner at all | skipped -- it is just a script |

A hand-written script is still fully tracked: `nrw fit run` records its hash,
its inputs and the environment exactly as for a generated one. Provenance does
not require the generator.

### 2026-08-05: a scaffold that does not validate teaches the pattern backwards

`nrw model new` initially emitted a spec that failed its own `nrw model
validate`: it declared a structural parameter `per: state` (which defaults to
*every* group, series included) while also constraining that path across the
series -- the double-assignment error. First contact with the schema would have
been an error message about a file the tool itself wrote.

`_scaffold_document` now scopes structural parameters to the states with an
explicit `in:` whenever a constraint owns the series, and omits the constraint
entirely when there is only one state to anchor it. The scaffold validates,
generates, and builds as written.

### 2026-08-05: A NUL byte is the binary test, not a failed decode

`_render_diff` originally detected binary content with `try: decode('utf-8')`.
That is insufficient: control bytes including NUL are valid UTF-8 code points,
so `b"\x00\x01\x02"` decodes cleanly and got fed into a text diff. Now
`_is_binary` checks for a NUL byte first, which is what git does.

### 2026-08-05: bumps numbers its exports by position and ignores `Experiment.name`

`FitProblem.save` is `for i, f in enumerate(self.models): f.save(basename + "-%d" % (i + 1))`.
The name is used in plot titles but never in a filename, so
`cu-thf-218389-full-sequence-7-refl.dat` says nothing about which measurement
it holds. Anything reading those files afterwards has to know the build order.

Reconstructing that order by re-parsing the script is guesswork that fails
quietly -- a curve labelled `ocv2` showing `ocv1`'s data looks entirely
reasonable. So the ordering is now recorded at fit time, from the object that
was actually fitted: `describe_models(problem)` walks `problem.models` and
writes `info.models: [{index, name, n_points}]` into the manifest. The
generator additionally passes `name=<slot key>` (`ocv1#0`, `tnr#12`) to every
`Experiment`, which also makes bumps' own plots self-describing.

A hand-written script may name nothing. The UI then labels by position and
says so, rather than inventing spec slot names that were never declared.

Corollary for any `<basename>-<n>` glob: sort on the parsed integer. Lexical
order puts model 10 between 1 and 2.

### 2026-08-05: the tNR heatmap must not pick its own reference

`series_data` computes the fractional residual with `tnr.run.load` plus
`tnr.reference.fractional_residuals` -- the same functions `nrw tnr assess`
uses. Choosing a reference block independently in the web layer would let the
picture and the numbers drift apart, so that the map shows change against one
baseline while `a(t)` beneath it is normalised against another.

Unmeasured cells are `NaN`, not 0. On a diverging colour scale zero-fill and
"did not change" are the same colour, and only one of them is a measurement.

### 2026-08-05: analysis warnings are data, not chatter

"101 interval file(s) listed in JSON not found" is a fact about the run that
the scientist needs. It was going to stderr, which for a web request means a
server log nobody reads. `tnr.notify.collect()` now redirects the channel on a
thread-local, so `ProjectData` returns those messages in the response and the
page shows them beside the plot they qualify.

### 2026-08-05: refl1d's profile grid is an integration grid, not a display grid

`-profile.dat` is written at refl1d's `dz` -- 0.1 Å here, giving 7,108 points
per profile and 149k points for a 21-model fit. That is one to two orders of
magnitude finer than anything physical in these models: the thinnest oxide is
tens of angstroms and the smallest interfacial roughness a few.

The reader thins to a 0.5 Å spacing, well below the roughness, keeping the
final point regardless of where the stride lands so the profile still reaches
the substrate. Measured on a real fit: worst-case interpolation error is
1.65e-4 of the SLD span, and the payload drops from 2.44 MB to 614 KB. Pass
`max_spacing=0` for the unthinned read; a numerical consumer should.

### 2026-08-05: `json.dumps` does not escape `</script>`

Data embedded in a `<script>` block is not protected by Jinja's autoescaping,
and the HTML tokeniser ends the block at the first literal `</script>` -- even
inside a JSON string. A run label or a fit note containing that text would
truncate the page's JavaScript and spill the remainder as markup. The template
filter escapes `</` to `<\/`, which is inert in JSON and invisible to the
tokeniser.

### 2026-08-05: `sample.md`'s commented example must not reach the rendered page

The scaffolded `sample.md` carries a worked measurement table inside an HTML
comment, listing runs (230594, 230597, 230600) that were never measured.
`web.prose.render` strips comments before rendering, for the same reason
`scan._runs_mentioned` does: showing that example beside the real data would
be actively misleading. Raw HTML is not rendered either -- a `sample.md`
travels between beamtimes and collaborators, and prose in a side panel has no
reason to run script.

### 2026-08-05: AuRE returns one flat feature mapping, and confidence is load-bearing

`extract_all_features` does not nest. The keys are `critical_edges`,
`oscillation_periods`, `n_fringes`, `estimated_total_thickness` +
`thickness_uncertainty` + `thickness_confidence`, `estimated_roughness`,
`estimated_n_layers`, `q_min`/`q_max`/`n_points`/`has_error_bars`. Guessing at
nested keys like `kiessig_fringes` yields empty results with no error, which is
how the first version of `aure_adapter` was wrong.

On the real 218386 curve the thickness comes back as **444 +/- 555 A** with
confidence `medium`. The uncertainty exceeds the value, so it constrains
nothing -- but printed alone it looks like a number you could seed a model
with. `Estimate.usable` encodes that test, and `nrw data features` says
"uncertainty exceeds the value; not a constraint" rather than leaving the
reader to notice.

### 2026-08-05: segment overlap independently found the reference script's special case

`nrw data overlap Sample6` measures run 218386's 1.2 deg and 3.5 deg segments
as disagreeing by **+27.58%, 14.8 sigma**, while every other pair in the sample
sits within 3 sigma.

The hand-written reference script has, at line 243:

    # The third run has a different intensity, so we don't share that
    # parameter with the first two runs.

So the original author found the same thing by eye and worked around it with a
free per-segment intensity. The check turns that into a number with an error
bar, before a model is written rather than after one misbehaves.

Where two angle settings overlap they measure the same sample, so a significant
ratio is a normalisation error. The fit will otherwise absorb it into a layer
thickness or an SLD, which is invisible on a log-R plot.

### 2026-08-05: interpolate reflectivity log-log, or the overlap ratio is biased

Comparing two segments means interpolating one onto the other's Q grid.
`np.interp` on R against Q biases the ratio **0.22% low** on a realistic
overlap: Fresnel decay goes as Q^-4, and a chord between two points of a convex
curve sits above it, so every interpolated value is slightly high. The bias is
one-directional, so it does not average away, and it is a fifth of the 1% scale
this check reports at.

Logging R alone leaves 0.043% -- `log R` is straight in `log Q`, not in `Q`.
Log-log is exact for a power law: measured residual 2e-14 on a synthetic
Fresnel curve, and 0.13% on one with strong fringes, which is honest since
fringes are not a power law.

### 2026-08-06: which direct beam normalised a segment is recorded only in the template XML

The reduced ASCII does not say. `REF_L_<run>_auto_template.xml` does:

    run 218386 <- direct beam 218274
    run 218387 <- direct beam 218275
    run 218388 <- direct beam 218338      <- measured much later

That is the 3.5 deg segment `nrw data overlap` reports 27.6% out at 14.8 sigma.
Different angles legitimately use different direct beams, so this is where to
look first rather than a diagnosis -- but two segments normalised against
references measured far apart are exactly the ones that come out on different
scales, and `nrw data overlap` now prints the mapping whenever it flags a pair.

Templates are named after the *first* run of a measurement, so looking one up
by segment 218388's own number finds nothing; every template in the directory
has to be read. Only one template exists in the apr2025 data (for 218386), so
218393's normalisation history cannot be checked the same way.

### 2026-08-06: the BL-4B detector moved, twice

Sample-to-detector was 1830 mm until 2024-08-26, then 1355 mm, then back to
1830 mm on 2025-01-01. Source-to-detector went 15750 -> 15282 -> 15750 mm.
A run reduced with the wrong distance is wrong in a way that looks like a real
sample. `instrument/geometry.py` holds the dated table and `spans_a_change`
answers the question it exists for: may these runs be co-refined?

The apr2025 data this project is built around sits after the move back, so it
uses 1830/15750.

### 2026-08-06: nr-analyzer's theta-offset physics is untested upstream

`tests/test_theta_offset_json.py` writes an empty file and mocks
`compute_theta_offset` entirely. So the 613 lines of NeXus event loading, peak
fitting and gravity correction have no test anywhere.

Only the dated geometry table was ported here. Vendoring the rest would mean
shipping unverifiable physics into a package whose whole claim is that results
are checkable -- and there is no raw event data in a workbench project to test
it against, since `data/raw/` is gitignored by design.

### 2026-08-06: a default argument bound at import cannot be overridden

`sync_upstream.check(manifest_path: Path = MANIFEST)` looked fine and was
untestable: the default is evaluated once when the module is imported, so
monkeypatching `sync_upstream.MANIFEST` changed nothing and a caller passing an
override to `load_manifest` was silently ignored. Both now take `None` and
resolve inside the function. Caught by a test that asserted `main()` exits
non-zero on drift and got 0.

### 2026-08-06: upstream.toml entries without a commit were not actually tracked

Six of eight `[[adapted]]` entries recorded a repo and a path but no commit, so
there was nothing to compare against and the drift check could only skip them.
The manifest looked like it was doing its job. Commits are backfilled from the
checked-out source repos, and `test_every_manifest_entry_names_a_commit`
now fails if a new entry omits one.

The manifest also mixes key spellings -- one entry has several `paths` from a
single `repo`, another has parallel `repos`/`paths`/`commits` -- so anything
reading it has to handle both.

### 2026-08-06: a refl1d script is identified by its last line, not its first

`problem = FitProblem(...)` is the closing line by convention -- in the oct2025
reference it is line 344 of 344 -- and these scripts import via
`from refl1d.names import *`, so the name never appears near the top. The
importer's first version sniffed the leading 4 KB, found the imports, missed
the identifying token, and left every loose model script unclassified.

### 2026-08-06: `\b` finds no boundary between `r` and a digit

`re.search(r"\b(\d{6})\b", "r223995_eis_reduction.json")` matches nothing: `r`
and `2` are both word characters, so there is no boundary between them. The
pattern silently returned None and sent every tNR file into one flat directory
instead of one per run. Now `(?:^|[^0-9])r?(\d{6})(?![0-9])`.

### 2026-08-06: resolve both sides before asking whether one path contains another

`_link_target` resolved the source but compared it against an unresolved root.
On macOS `/tmp` resolves to `/private/tmp` and `/home` goes through autofs, so
a source genuinely inside the project was judged foreign and silently linked
with an absolute path. Any symlinked component on either side does this.

### 2026-08-06: old fit outputs must not be imported

`nrw import` recognises `.dat`, `.par`, `.err`, `.mc.gz` and `results/` and
deliberately leaves them behind -- 310 files in apr2025, 527 in june2026.

A `results/<fit_id>/` directory here means a manifest with the hash of every
input, the resolved environment, the git SHA and the exact command. A `.dat`
dumped by a bumps run in 2025 has none of that. Putting it where `nrw whence`
promises an answer would fake provenance nobody recorded. Re-running the script
through `nrw fit run` takes minutes and produces the real thing.

The distinction from "not recognised" is reported separately for the same
reason: "I know what this is and it should not come" is different information
from "I do not know what this is".

### 2026-08-06: two planned skills were not written, on purpose

The M6 skill list included `tnr-plotting` and `partial-data-assessment`.
Neither was written, and the reasons are worth keeping so nobody adds them
later to tick the box.

**`tnr-plotting`** was to be adapted from `experiments-2025/docs/tNR-plotting.md`
(422 lines). Read closely, that file is a *development plan* -- Phase 0 through
Phase 7, a "Files touched" section, a "Decisions taken" section about extending
`tnr_chi2.py`. Its one durable section, "Numerical pitfalls", lists guards that
are already implemented in `tnr/metrics/` and pinned by the characterization
tests: the R <= 0 guard in `fractional_residuals`, the floor on
`dR_i^2 - dR_ref^2`, the boxcar edge handling, `--variogram-min-pairs 3`. Those
are tested code, not guidance a reader acts on.

**`partial-data-assessment`** would have covered what you can conclude from
per-angle `_partial.txt` files: segment consistency and the Q-range limit on
what is resolvable. Both are already covered -- consistency in
`refl-bl4b-instrument` (with `nrw data overlap` behind it), resolvability in
`thin-layer-degeneracy` (the 2*pi/Q_max rule). A third telling would compete
with those rather than add to them.

Twelve skills ship and are seeded by `nrw init`. Four more exist in the bundle
for `nrw skills sync`. A skill nobody needed is worse than a missing one: the
retriever scores against every skill's tags, so filler dilutes the ones that
matter.

### 2026-08-06: the copper-oxide SLDs in circulation are wrong, and CuO hides in Cu

Three sources disagreed on the copper oxides. Computed from CRC bulk densities
and coherent scattering lengths (`periodictable`):

    Cu        6.55      CuO   6.46      Cu2O  5.36      Cu(OH)2  2.46

AuRE's skill table gives CuO 5.0 and Cu2O 4.0. The first draft of this repo's
`metal-oxide-interfaces` skill gave CuO 4.2-4.5 and Cu2O 4.9-5.2 -- both wrong
*and inverted relative to each other*. The same first-principles calculation
reproduces AuRE's TiO2 (2.63 vs 2.6) and SiO2 (3.47 vs 3.47) exactly, so the
method is sound and the copper entries specifically are the bad ones.

The physical consequence is the reason this matters: **stoichiometric CuO at
6.46 is nearly contrast-matched to copper at 6.55.** A dense fully-oxidised CuO
layer on a copper electrode is close to invisible to neutrons. Looking for one
and finding nothing is not evidence it is absent.

Cu2O is the visible one at 5.36, and a real native oxide is porous and hydrated
rather than bulk-dense -- Cu2O at 80% of bulk density is 4.29 -- so a fitted
CuOx in **4.2-5.5** is an ordinary cuprous oxide and the width of that range is
porosity.

Also: `sld("Cu2O")` raises. Densities come from `periodictable`, which has
them for elements only, plus a short table in `nr_workbench.materials` for the
solvents and substrates this repo names -- not for these oxides, so a density
must be passed explicitly. The skill's example says so now; the first version
claimed it worked and did not. (Until 2026-09-08 the table was AuRE's; it
retired `aure.database.materials` and the arithmetic moved here.)

### 2026-08-06: the incident angle is in the file header, in radians

Every reduced steady-state REF_L file carries a `# Meta:` line holding one JSON
object with `theta` (radians), `norm_run`, `dq_over_q`, `sequence_number`,
`scaling_factors` and the timestamps. `nrw model new` was instead writing
`[0.45, 1.2, 3.5][:n]` -- the group's usual settings, truncated to the segment
count.

For a three-segment measurement at the usual settings that is right to 0.08%,
which is why it went unnoticed. It is badly wrong otherwise:

* Run 218389 has **one** segment in `data/steady` -- the summed tNR dataset --
  so it got `[0.45]`. The header says **0.5997**. That is 25% off, and theta
  enters both `wl = 4*pi*sin(theta)/q` and `dT = dq/q*tan(theta)`, so the
  wavelength axis and the resolution were both wrong by 25%.
* Any two-segment measurement got `[0.45, 1.2]` regardless of its real angles.

A wrong theta does not raise. It broadens or sharpens every fringe and the fit
absorbs it into roughness.

Measured values, for the record: 218386 is 0.4500 / 1.2010 / 3.5003 and 218393
is 0.4499 / 1.2009 / 3.5002. The fixed-width `TwoTheta(deg)` column agrees with
the JSON `theta` to seven digits, which is a free check on the radians
conversion.

**This is a parsing problem, not an inference problem.** An LLM call was
considered and rejected: the value is a field in a JSON object, so a parser is
exact, offline, deterministic and testable, while a model can return a
plausible number that silently corrupts the resolution. Where a model helps is
reading an *unfamiliar* header once and writing a parser for it -- a
code-generation task with a test, not a per-file inference.

### 2026-08-06: a tNR run is also reduced whole into data/steady

Time-resolved slices carry no header at all, and the angle appears in neither
the `*_eis_reduction.json` sidecar nor the tNR template XML. It looked like the
angle simply was not recorded.

It is: the same run is *also* reduced as a summed dataset into `data/steady`
under the same run number -- `REFL_218389_4_218389_partial.txt` -- and that file
has the full `# Meta:` block. `theta_for_run` reads it.

Two consequences beyond the angle:

* That summed file must **not** be co-refined alongside the series. It is the
  sum of the very slices the series contributes, so including both puts the
  same neutrons into the fit twice and roughly doubles that run's weight.
  `nrw model new` now drops the state and says why.
* `sequence_number: 4` in the header matches `scan_index: 4` in
  `reduction_options.json`, so the two records agree on which angle setting the
  tNR run used.

### 2026-08-06: filling the spec from notes, and the boundary that makes it safe

`nrw model new` fills in facts -- runs, files, measured angles. It cannot fill
in the stack, which is in the scientist's head and, if written down, in
`sample.md`. Two paths close that:

* `--from-notes` calls a configured endpoint.
* `--print-prompt` writes the skeleton and prints the same request for the
  coding assistant already open on the repository.

Both build the same prompt, in `spec/authoring.py`, so they cannot drift.

**A model may propose only `description`, `materials`, `stack`, `parameters`
and `constraints`.** Anything it returns for `states`, `series`, `thetas`,
`data_dir` or `run` is dropped in `merge_proposal`. The filter is structural,
not a request in the prompt, because those are exact measurements and a
plausible wrong angle is unrecoverable -- it broadens every fringe and the fit
absorbs it into roughness without raising.

Two things that only showed up on a real run:

* A proposal that replaces the stack leaves the scaffold's constraint pointing
  at `Film.thickness`, a layer that no longer exists, and the spec then fails
  its own `nrw model validate`. `_repair_constraints` keeps the paths that
  survive and drops the constraint when none do.
* The critical edge from `nrw data features` is worth putting in the prompt.
  A real run came back reasoning that "the measured topmost SLD near 4.3 [is]
  consistent with a porous/native cuprous oxide" -- which is the 4.2-5.5 range
  derived from first principles in `metal-oxide-interfaces`.

### 2026-08-06: a reduced-data scaffold that builds to infinity

`nrw model new` scaffolds a series with no `select:`, so it takes every slice.
14 of run 218389's 130 slices contain points with `dR = 0`, chi-squared divides
by `dR`, and the whole problem returns `inf`. refl1d does not warn.

The guard belongs in the generated `create_probe`, not in the scaffold: it
fixes every spec rather than only scaffolded ones, and it matches what the rest
of the package already does -- `fractional_residuals` has had the same check
since M2. Points with non-positive or non-finite `dR` are dropped at load.

Verified not to change anything it should not: the M3 numerical gate still
passes, and the guide's model builds to the same chi-squared 101.632 over the
same 5380 points, because its `select: {labels: ["*_eis_*"]}` never included a
bad slice.

### 2026-08-06: .env, and why the scaffold must gitignore it

Settings load shell > project `.env` > `~/.nrw` > `~/.aure`, mirroring AuRE.
The `~/.aure` fallback is deliberate -- a machine already configured for AuRE
works here unchanged -- and is reported by `nrw doctor` rather than being
silent, because configuration arriving from a file you did not know was read is
hard to debug.

AuRE's `load_env()` is called by AuRE's own CLI, so importing `aure.llm.config`
from here sees only the shell. Without loading it ourselves, a user with a
working `.env` would find `nrw` says "no endpoint" while `aure` works.

The scaffolded `.gitignore` did **not** cover `.env`. A beamtime directory gets
shared with collaborators, archived by the facility, and sometimes published
alongside a paper, so an API key in one is a real exposure. It is ignored now,
a `.env.example` ships, and a test asserts both.

Loading is lazy: `python-dotenv` costs ~40 ms and `nrw --help` is run
constantly. A test asserts `dotenv` is not in `sys.modules` after `--help`.

### 2026-08-06: the model explanation is derived, never authored

`nrw model generate` writes `<name>.md` beside `<name>.py`: what is being
fitted, what is held equal to what, what each instrument parameter absorbs, and
every assumption the fit makes silently.

Every sentence comes from the resolved `ParameterTable`, so it cannot describe
a model other than the one that will run. A hand-written explanation drifts the
first time someone edits one and not the other, and a stale explanation is
worse than none because it reads as authoritative. `nrw check` reports
`stale-explanation` and `missing-explanation` against the recorded spec hash.

It is suppressed when the *script* is already stale -- one message about the
spec having moved on is enough.

### 2026-08-06: theta_offset and sample_broadening are partials-only

Both describe the *incident angle*. A combined file is the reduction's stitch
of every angle setting, so it has no single angle to offset or broaden. refl1d
accepts the parameter and fits it to something meaningless, which is the worst
of the three possible behaviours -- so `nrw model validate` rejects them on a
`kind: combined` state. AuRE draws the same line via its `_NUISANCE_KEYS`.

Canonical ranges, from AuRE's `_TRIPLET_DEFAULTS`:

    theta_offset       [-0.02, 0.02]
    sample_broadening  [ 0.0,  0.05]
    background         [ 0.0,  1e-5]

Declaring `sample_broadening` at all changes which resolution path refl1d
takes: the guide's model builds to chi-squared 101.632 without it and 101.689
with it pinned at 0.0. So a spec carrying it at zero is not equivalent to one
without it -- add it when there is a reason, not by default.

`sample.md` gained a **Measurement conditions** section for describing these in
words ("sample bowed slightly after mounting"), and both authoring paths read
it: the prompt maps alignment/curvature/background language onto the matching
parameter.

### 2026-08-06: an f-string return value will eat a YAML example

`agent_instructions` returns an f-string, and the instruction text contains
`{path: probe.theta_offset, ...}` as an example. Python read that as a format
field and `--print-prompt` raised `NameError: path`. Ruff's F821 caught it
before a user did. Braces in an f-string template must be doubled -- worth
remembering for any function that returns example code.

### 2026-08-06: the assessment already chooses the constraint form; use it

`nrw tnr assess` ends with a verdict that names the form the trajectory calls
for -- "a(t) is monotonic, so a linear-in-time constraint fits; the template
oscillates in Q, so free a thickness" -- and the authoring prompt was ignoring
it entirely, leaving the model to guess from prose.

`_measured_facts` now quotes the verdict, the trajectory shape, the peak
significance and the template classification. Running `nrw tnr assess` before
`nrw model new --from-notes` is therefore the whole answer to "how do I ask for
a linear constraint": the data already said so.

### 2026-08-06: a proposal will describe a constraint instead of returning one

A real reply put this in its `notes` field:

    the existing linear_in_time constraint should be moved from
    Film.thickness to CuOx.thickness

and then omitted the `constraints` block. The leftover constraint referenced a
layer the new stack does not have, `_repair_constraints` dropped it, and the
series ended up with every slice refitting the whole structure independently --
never what was wanted, and invisible in the spec.

The paths are derivable, so the fallback does not need the model: a parameter
declared `per: state` across both endpoint states is by definition something
the experiment changed between them, so interpolating it across the series is
exactly what the form is for. `_rebuild_constraints` re-aims the constraint at
those, excluding `per: model` values (identical at both ends, so interpolation
is a no-op) and `probe.*` nuisances.

The prompt was also strengthened to say RETURN the block rather than describe
it, which fixed it on the next real run -- but the deterministic fallback stays,
because a prompt is a request and this is a correctness property.

### 2026-08-06: the explanation states the arithmetic, not a paraphrase

`<name>.md` now carries the formula the generated script actually evaluates:

    p_i  =  p[ocv1] + (p[ocv2] - p[ocv1]) * f_i
    f_i  =  (t_i - t_0) / (t_last - t_0)

plus the per-slice fraction table, the free parameters the form introduces
(`tau`, `t_half`/`width`, knots) or a statement that it introduces none, and
the arithmetic: "90 slice values (6 paths x 15 slices) computed from no new
free parameters".

It also quantifies time-vs-index for the actual data rather than asserting they
differ. On run 218389's full 130 slices the worst disagreement is slice 17 at
0.120 against 0.132 -- 1.2% along the trajectory. Small here, and the reader
can see it is small instead of taking it on trust.

### 2026-08-06: constraint endpoints can be fitted, not only anchored

`from`/`to` normally name the steady states either side of a series, and that
is why the interpolating forms cost nothing -- they borrow parameters the
steady-state data already constrains. Writing `free` for either fits that
endpoint instead: one extra parameter per path per free endpoint.

    from: ocv1  to: ocv2     21 free   both anchored
    from: ocv1  to: free     27 free   +6, one per path
    from: free  to: free     33 free   +12

Three situations call for it, all real: a series with no bracketing
measurement; a series where something happened between the steady measurement
and the run, so anchoring asserts a continuity that is not there; and a series
where where the sample finished *is* the result rather than an assumption.

The range is borrowed from the path's existing `parameters` declaration -- a Cu
thickness plausible for the steady states is plausible during the series, and
repeating it would be a second place for it to be wrong. `endpoint_range`
supplies one where no declaration exists. Missing both is an error, not a
default: an unbounded endpoint drags the whole trajectory with it.

The explanation had to learn about this. Left alone it rendered `p[free]` in
the formula and claimed "No new free parameters" while six had just been
created -- the plausible-looking wrong document that is worse than none.

### 2026-08-06: sample.yaml was written and never read

`nrw sample scan` wrote the register; nothing consumed it. `nrw model new`
re-scanned the disk instead, so curating the register -- deleting the alignment
scans, the aborted runs, the other conditions -- did nothing at all. A
write-only register is decoration.

`load_register` reads it back, and `nrw model new` prefers it. The drift
between register and disk is reported, not resolved: a stale register and a
deliberately curated one are indistinguishable on disk, and silently re-adding
a run would undo a choice.

Two traps in doing this:

* The scaffold ships `steady: []` and `series: []`. Treating an empty register
  as a curated empty set would make `nrw model new` report "no data found" for
  every sample whose owner copied files in before running `nrw sample scan`.
  An empty register is a stub; fall back to scanning.
* `yaml.YAMLError` derives from `Exception`, not `ValueError`, so
  `except (OSError, ValueError)` sails straight past a malformed register.

### 2026-08-06: theta_offset scope is a claim about remounting, not about change

The skill said to fit `theta_offset` and `sample_broadening` `per: state`,
reasoning that "a realignment between two states is exactly the kind of thing
that makes them differ". That is backwards as a default here.

Both describe *how the sample sits in the beam* -- its alignment and its
flatness -- so the question is not "did the sample change?" but "was it
remounted?". An in-situ electrochemical cell measured continuously (OCV, tNR,
OCV) is mounted once and never touched: one alignment for the whole experiment,
so `per: model`.

Fitting one per state on a sample that never moved is several free parameters
describing one physical quantity, and they will absorb the real structural
differences between the states -- the thing being measured.

`probe.intensity` is the exception and is nearly always `per: state`: each
reduction used its own direct beam, and those genuinely differ.

`sample.md` now asks the deciding question outright, because it is not
derivable from the data.

### 2026-08-06: an error message that names only the wrong fixes

The double-assignment error said "remove it from `parameters` or from the
constraint's `paths`". On the case that actually produces it, both are wrong:
removing the declaration loses the range -- which a `free` endpoint borrows --
and removing it from the constraint leaves the series unfitted.

The real cause is nearly always a structural parameter declared `per: state`
with no `in:`, which scopes it to *every* group including the series. The fix
is to scope it to the steady states, and the message now says exactly that,
with the state names filled in.

It also reports **every** collision rather than the first. The mistake is made
once, in one habit, and applies to every structural parameter in the spec -- on
the real five-layer model that was eight of them, so raising on the first
turned one edit into eight validate-fix cycles.

### 2026-08-06: the critical edge in back reflection needs the substrate added

A critical edge measures a step in SLD: `Qc = 4 sqrt(pi * step)`, where the
step is the SLD of the medium that reflects the beam totally less that of the
medium the beam enters through. AuRE's `estimated_SLD` is that step. From air
it is the reflecting medium's own SLD. In back reflection the beam enters
through the substrate, so the medium on the far side of the film sits at
`step + rho_substrate`.

On expt11 that is the difference between a nonsense answer and the right one:

    raw estimate      4.28      not any solvent
    + rho_Si (2.074)  6.354     d8-THF, tabulated 6.35

`sample.md` said only "THF", which would be 0.18. The corrected edge said
deuterated, and it was right -- the notes were wrong. Where the two disagree
about deuteration, the edge is the measurement.

Uncorrected, this was actively harmful: the prompt reported "implies a topmost
SLD near 4.28" and a proposal duly set the solvent range to [4.0, 4.7], which
is neither isotope. `_back_reflection_substrate` reads the geometry from the
notes -- it runs while building the prompt, before anything has been proposed
-- and reports both numbers. *(Since 2026-10-13 the prompt gives the step and
how to read it, and the model, which reads the notes, adds the substrate's
SLD: see that day's entry.)*

### 2026-08-06: repairs the model will not reliably do itself

`nrw model new --from-notes` scored 8/11 on a real, detailed `sample.md`, and
the three misses were all instructions the prompt had just gained. Two turned
out to be a silently failed edit: the `probe` and `series_select` output keys
never reached the prompt, so the model was told to use them in the rules and
never told they were valid output. **String-replacement edits to a prompt need
an assertion**; a prompt that quietly loses an instruction looks exactly like a
model that ignored one.

Three more repairs moved into `merge_proposal`, because each is a correctness
property rather than a request:

* `per: state` with no `in:` covers the series and collides with the
  constraint. Asked for in the prompt, omitted about half the time.
* `per: model` on a path that is also constrained is a contradiction --
  "pinned everywhere" against "has a trajectory". The declaration wins.
* A constrained path with a `free` endpoint and no declared range cannot be
  bounded. Dropped and named, because inventing a range for an SLD is the kind
  of plausible guess this package exists to avoid.

With those, the same notes produce a spec that validates and builds first try.
The remaining prompt-only instructions are the ones where a wrong answer is
visible in the spec rather than fatal to it.

### 2026-08-06: bumps runs on one CPU unless told otherwise

`bumps.fitters.fit` takes `parallel: int = 1`, and nothing here was passing it,
so every fit ran on a single core regardless of the machine. `parallel=0` means
all cores; `nrw fit run` now defaults to that and exposes `--parallel N`.

Measured on the 21-experiment Cu/THF co-refinement, dream with 8000 samples on
a 20-core laptop: **169 s serial, 30 s parallel** at 879% CPU. Population
fitters evaluate their whole population per generation and scale; amoeba is
sequential and gains nothing.

`parallel` is in `FIT_SETTING_KEYS`, so it is part of the settings digest and
appears in the record -- it changes how the fit ran.

Two things to know about the pool:

* bumps starts a `multiprocessing.Manager` and spawns workers that re-import
  `__main__`. A benchmark run from a heredoc died on exactly this
  (`FileNotFoundError: .../<stdin>`); the real CLI is fine. Since the failure
  is environmental and the model is not at fault, a pool failure falls back to
  serial with a warning rather than losing the fit.
* The fallback is deliberately narrow: it triggers on multiprocessing-shaped
  errors only, so a genuinely broken model still fails once and fast instead of
  being retried pointlessly.

### 2026-08-06: the fit record was not freezing the spec

`freeze_script` copied `model.py` and nothing else, so a result directory held
generated Python and no statement of intent -- which layers are tied, what the
constraint asserts, which endpoints are anchored. It could be re-run but not
re-reasoned about, and anything reading the model back had to reconstruct it
from generated code. `spec.yaml` and `model.md` are frozen alongside now. A
hand-written script has neither, which is normal rather than an error.

### 2026-08-06: `index` in bumps' -err.json is the chain column already

`point.mc.gz` is `(n_samples, 1 + n_parameters)` with logp in column 0, and
each parameter's `index` in `-err.json` runs 1..n -- it is the column, not an
offset into the parameter block. Adding one shifts every parameter onto its
neighbour.

The result is the dangerous kind of wrong: nothing raises, no value looks
impossible in isolation, and every band is a real interval from a real
parameter. It showed up as a copper *roughness* of 5 A carrying a
[500, 530] band, because it had been handed the thickness column.

Caught by asserting each fitted value sits on the same scale as its own band.
That check needs a tolerance of one band width rather than zero: the reported
value is the maximum-likelihood point, which is not obliged to lie inside a
*central* 68% interval and routinely sits just outside it when a parameter
rails against a bound.

### 2026-08-06: a trajectory band needs paired posterior samples

A constrained slice value is a deterministic function of the fitted endpoints,
so the band is computed by evaluating the constraint once per posterior sample.
Propagating each endpoint's `std` independently would be wrong in a specific,
visible way: for an interpolating form the endpoints are strongly
anti-correlated, so the band should *narrow* towards the middle of the series
and independent propagation widens it there instead.

Values come from `-slabs.dat` rather than the same evaluation -- that is the
layer table the fit actually used for each slice, so it needs no recomputation
and works for an optimiser run with no posterior at all.

### 2026-08-06: an old fit does not need re-running to describe itself

The trajectory view needs the spec, and fits made before `spec.yaml` was frozen
into the record do not carry one. Re-running is not the answer: the generated
script records the digest of the spec it came from, so a spec still on disk
that hashes to it is provably the same file and using it is exact.

If the spec has since been edited, it describes a *different* model, and
plotting its trajectory against this fit's numbers would be worse than plotting
nothing. That case reports the reason and stops -- and `nrw check` is already
calling the fit stale independently.

Both branches are visible in the UI. The failure that prompted this was not a
wrong plot, it was **no plot and no explanation**: an empty page is
indistinguishable from a broken one.

### 2026-08-06: SLD profiles belong on the substrate, not the surface

refl1d writes the profile with z = 0 at the *top* of the stack, so two models
whose total thickness differs are drawn offset from each other -- a 3 A change
in copper shifts the titanium and the substrate with it, and across 15 tNR
slices the buried layers smear. Referencing z to the substrate surface anchors
the one interface that cannot move, so only the layer that actually changed
moves.

The offset is the sum of every thickness except the substrate's, which is
refl1d's own `align=-1` (`uncertainty._find_offset`). Computed here from the
`-slabs.dat` table rather than by importing refl1d's plotting stack.

### 2026-08-06: best fit or posterior median -- report the best, show both

They differ. On a real DREAM fit here the worst gap is **1.2 sigma**
(`ocv1 probe intensity`, best 1.089 against median 1.029), and every varying
trajectory sits 0.15 to 0.54 of a band width away from its median.

**Report the best.** It is a single self-consistent parameter vector -- one
point the model was actually evaluated at, and the one the plotted curves,
`-slabs.dat` and the quoted chi-squared all come from. The marginal median is
not a parameter vector at all: each parameter's median taken independently can
describe a stack no posterior sample contains, and which fits worse than
either. For correlated parameters -- the endpoints of an interpolating
constraint, intensity against thickness -- that is not hypothetical.

**Show the median too**, faintly. The gap between them is diagnostic: a
best-fit sitting a sigma from the median means the posterior is skewed or
something is railing against a bound, and that is invisible if only one is
drawn.

The same reasoning explains why a fitted value can sit outside its own 68%
band: the maximum-likelihood point is under no obligation to lie inside a
*central* interval.

### 2026-08-06: the index is append-only, so deleted results must be marked

Removing a result directory left the fit in `nrw serve`'s table, linking to a
404. Pruning the index would be wrong -- that a fit ran stays true after
someone clears disk space, and the append-only log is deliberate. So the row
stays, marked `deleted`, and does not link. Silence was the bug in both this
and the missing trajectory panel: an empty page is indistinguishable from a
broken one.

### 2026-08-06: bumps draws its figures *before* it saves the chain

`export_fit` calls `problem.plot()` and `fit_state.show()` and only then
`fit_state.save()`. A rendering failure -- which a 21-model co-refinement
provokes reliably, because bumps opens one figure per model -- therefore
destroys the MCMC chain and every uncertainty output of an hour-long DREAM
run. Exactly the shape of the `export=` trap recorded above: the expensive
result is lost to a cosmetic step.

`nrw fit run` now defaults to `--no-plots`, patching `problem.plot`,
`errplot.show_errors` and `errplot.calc_errors` to no-ops for the duration.
`MCMCDraw.show` needs more care than the others: `bumps.dream.views.plot_all`
computes the variable statistics and writes `-err.json` *before* it imports
matplotlib, so blanket-patching it silently discards the uncertainties. The
replacement (`_stats_without_plots`) keeps the `var_stats` + `save_vars` half
and drops the drawing. The statistics live in `bumps.dream.stats`, not
`bumps.dream.varplot`.

Verified on a real DREAM run: zero PNGs, and `err.json`, `point.mc.gz` and all
21 `slabs.dat` present. Wall time 19.4 s.

### 2026-08-06: a fit_id is second-resolution, so ties need the index order

Turning the plots off made fits fast enough to finish inside the same second,
and two lifecycle tests started failing. The cause was not the collision --
`create_unique` already suffixes a clashing directory -- but the ordering:
`FitIndex.fits()` sorted on `started_at`, which has second precision, and
`sorted(..., reverse=True)` is *stable*, so tied entries came back oldest
first. `nrw ls` then reported the wrong "latest fit" and `nrw diff` compared
the wrong pair. Ties now break on position in the append-only index, which is
the true chronology.

The general trap: a stable sort with `reverse=True` does not reverse ties, so
any "newest first" built on a low-resolution timestamp is wrong the moment two
events share a tick.

### 2026-08-06: compute the derived view at write time, not at read time

`ProjectData.trajectory` re-read every slice's `slabs.dat` and the DREAM
posterior on each page load: 1149 ms. The inputs are frozen the moment the fit
finishes, so the answer can only be computed once. `nrw fit run` now writes
`trajectory.json` into the result directory and the web layer loads it --
1.7 ms, 666x faster, same 20 traces. The fallback recomputation stays for fits
made before this, and for a result directory whose cache was deleted.

Two ordering constraints, both learned the hard way: the summary must be
written *after* `index.append`, because it looks the fit up by id; and it must
never be able to fail the fit, so it is wrapped and merely warns.

The SLD credible bands went the other way -- deleted rather than cached. They
cost a posterior resample per profile and, as the user put it, were "too time
consuming and not informative": the interesting uncertainty is in the layer
parameters, which the trajectory panel already shows against time.

### 2026-08-07: a fit id identifies a run but does not describe it

`fit_id` is a timestamp plus a content hash --- perfect for pointing at a
result, useless for recognising one. After a dozen runs the listing is a wall
of hashes and the only question anyone has ("which one freed the oxide?")
needs a manifest opened per row to answer.

`nrw ls` and the web tables now carry two extra lines per fit: what the run
was (the `--note`, or a phrase built from the settings) and what changed since
**the previous run of the same model**. The scoping is the whole point ---
comparing against the row above would usually compare across models, which
describes the listing rather than the work.

`provenance/summary.py` works from index entries alone, deliberately. The
index is scanned by every `ls` and every page of the UI; a description that
opened a manifest per row would make the listing slow in proportion to the
thing it exists to make usable. That constraint is why the settings dict now
lives in the index entry: without it the line can say a run differs but not
that it differs by `steps 2000 -> 20000`, which is the only form of that
answer anyone can act on.

Two details worth keeping:

- **A missing field is absence of evidence, not evidence of change.** Fits
  recorded before a field existed must not read as "changed" against fits that
  have it, or adding a field makes the whole history look churned.
- **A method change brings its own vocabulary.** amoeba counts `steps`, dream
  counts `samples` and `burn`. Reporting the knobs that merely stopped applying
  (`steps 2000 -> None`) buries the one fact that matters.

### 2026-08-07: `inputs_digest` is not the data --- the script is an input too

Found while writing the change line, and it was wrong in `nrw diff` as well.
`inputs_digest` covers every recorded input, and the script is recorded as one
(role `script`). So editing a model moved it, `changed["inputs"]` went true,
and `_diff_verdict` announced *"the DATA changed. Any comparison between these
two is about different measurements, not different models."*

That is the exact inversion of the distinction the command exists to draw: it
declared the comparison invalid at the moment it was most valid. It had been
true since M1 and no test caught it, because every diff test changed either
the data or the settings, never the script alone.

The fix is a `data_digest` --- the same digest over the inputs with the script
excluded --- recorded in the index, and `_diff_inputs` filtering the script
out so `nrw diff` reports `data` rather than `inputs`. Entries too old to have
`data_digest` say the weaker true thing ("inputs changed") instead of the
stronger false one.

The general lesson: a digest is only as honest as the set it covers, and "the
inputs" quietly means two different sets depending on who is asking.

### 2026-08-07: a model script is built at its starting values, not its answer

`nrw pack` writes a bundle a collaborator can run with only refl1d. The first
one built cleanly, unpacked cleanly, and reported a 60% chi-squared mismatch
against its own recorded result.

Nothing was wrong with the bundle. A model script *constructs* a problem --- it
sets each parameter to its initial value from the spec --- so `problem.chisq()`
straight after loading measures the starting guess. The fitted values live only
in bumps' `.par` file, which the fit wrote separately. `verify.py` now reads it
and calls `problem.setp` before comparing.

Two details of that file: parameter names contain spaces (`run218386 probe
intensity 1.0999...`), so it splits on the *last* field; and comparing the
parameter *sets* before the numbers matters, because two models with different
parameters can produce a chi-squared that agrees by accident.

This was only ever going to be caught by running the artifact. Every structural
check --- files present, hashes match, script byte-identical --- passed on the
broken bundle.

### 2026-08-07: preserve the project shape in a bundle rather than flattening it

Generated scripts locate data by walking up to `nrw.toml` and reading
`PROJECT_ROOT / "samples/<id>/data/..."`. The obvious bundle layout --- script
at the top, data in `data/` beside it --- would need the script rewritten, and
a rewritten script is no longer the one whose sha256 the manifest records. It
could not be checked against the record it claims to reproduce.

So the bundle rebuilds `samples/<id>/{models,data}/` and ships a copy of
`nrw.toml` as a root marker. The script is copied byte-for-byte, the marker
makes its root-finding deterministic rather than dependent on where the
recipient unzipped, and the bundle happens to also work as a workbench project
for anyone who has one.

### 2026-08-07: escaping a code generator that emits code has two layers

`_verify_script` builds `verify.py` with an f-string. A `\n` inside that
template is consumed when *pack.py* is parsed, so it emitted a real line break
inside a `print("` and the generated file did not parse. The generated script
runs on someone else's machine, days later, which is the worst place to
discover a syntax error.

The guard is cheap and belongs on anything that emits code:
`compile(source, "verify.py", "exec")` in a test.

### 2026-08-10: the only prose that survived was the field that was mandatory

Measured on the first real beamtime run through this tool (`apr2025/
cu-thf-expt11`, 23 fits, 19 model variants, one sample):

| surface | required? | used |
|---|---|---|
| `nrw fit run --note` | optional | **0 of 23** |
| `results/<fit_id>/NOTES.md` | optional | **0 of 25** |
| `nrw promote --reason` | **required** | **2 of 2**, both substantial |

The promote reasons are per-fit notes in all but name --- one names its
dependency on another fit by id, quotes intervals, and flags two caveats. The
same analyst wrote 609 lines of findings in `docs/ground_truths.md`. Nobody
was unwilling to write. The optional surfaces asked nothing and nothing read
them back.

Three separable causes, all fixed:

1. **No instruction targeted a fit.** All four "record what you learn"
   instructions named the project-level `docs/ground_truths.md`.
   `NOTES.md` appeared once, inside an ASCII diagram, phrased as *permission*
   ("the only file here you may edit") rather than as a question to answer.
2. **Writing there had no consequence.** One reader, `web/project.py`, which
   rendered the stub's two HTML comments into a `<pre>` on every fit page ---
   actively teaching readers the panel was noise.
3. **`reports/` was a documented empty contract.** Created with a `.gitkeep`,
   referenced by zero lines of code, excluded from `pack`, and listed in
   `importer.DERIVED_DIRS` so `nrw import` *discarded* it --- the one thing in
   a legacy directory that cannot be regenerated.

### 2026-08-10: link notes by the ids people already write in sentences

The 609-line findings file cites 11 distinct fit ids, all in prose, none in
any structured field. A linking scheme that read only frontmatter would have
found nothing in the entire corpus.

So `fits_mentioned` regexes the body as well as the header. An existing
findings file becomes a linked notebook by being moved into `reports/`, with
no edits. The regex requires the full `YYYYMMDD-HHMMSSZ-<8hex>` shape, so the
six-digit run numbers that saturate this prose (218386, 218393) cannot be
mistaken for fits.

The corollary: **an unfilled template must not count as a note.** It mentions
its own fit id, so without `is_blank` every untouched stub would appear as
evidence of thinking that never happened. Headings, HTML comments and the
blockquoted echo of `--note` are all scaffolding; only a line of prose counts.

### 2026-08-10: nr-workbench's offline fit checks beat AuRE's, so do not wrap it

`aure.nodes.evaluation._simple_evaluation` is the no-LLM path, and it is three
chi-squared bands. It reads no parameters, no bounds, no posterior;
`acceptable` is hard-coded `False`. Wrapping it would have made the offline
path look supported while saying less than a user could work out unaided.

What nr-workbench computes instead, with no AuRE import at all, from
`fit/<model>.json` (bounds), `.par` (values) and `-err.json` (posterior):
railed parameters, posteriors spanning most of their prior, best-fit points
outside their own 68% interval, per-model chi-squared spread, and BIC.

One of these is strictly better than AuRE's: `_check_boundary_hits` tests the
*point estimate* against the bound and never reads `uncertainties`. A
parameter whose **p95 edge** sits on its floor is pinned in a way that test
cannot see. Run 218389's `CuOx roughness` is exactly that case --- point
estimate 5.110 against a floor of 5 with span 6 (not a hit at 1%), but
p95 `[5.028, 8.381]`.

Validated against the analyst's own hand-written record for that fit: the
check independently produced "the posterior is pressed against the range",
which is the ground-truth entry *"the oxide slab is at its descriptive limit
-- roughnesses are lower bounds"*. Computed BIC 1048.0 against the 1047.9 in
the promote reason, and n_points 3915 exactly.

That last number is a trap worth keeping: `manifest.info.n_points` is bumps'
`dof`, not N. Summing `info.models[*].n_points` gives 3915; using `dof` (3907)
makes BIC wrong by `k log n`.

### 2026-08-10: a fit id's distinguishing half is its suffix, but only prefixes resolved

`FitIndex.resolve` matched on prefix alone, so telling two fits from the same
afternoon apart meant typing all sixteen characters of the timestamp to reach
the eight that identify the run. Fine for `whence`, fatal for a `nrw note`
command whose entire value is being lower-friction than opening the file.

Hash matching is now tried only when the prefix matches nothing, so it can
widen what resolved before but never change it.

### 2026-08-10: the ISAAC pipeline is a file contract, not an AuRE internals contract

`data-assembler ingest-workflow <dir>` looks like it needs an AuRE workflow run
directory. It does not. It reads four things:

    run_info.json     states[] with each state's data_files, or a flat list
    problem.json      the serialised bumps problem
    *-err.json        beside it, for per-parameter sigma
    final_state.json  {"final_chi2": ..., "state": {"states": [...]}}

Every one of those is already in an nr-workbench result directory. So
`nrw isaac export` rewrites a fit into that shape and drives the canonical
pipeline rather than mapping to the ISAAC schema itself --- a second mapping
would drift from a schema neither project owns.

Two properties come free from writing `states[]` rather than a flat file list,
and both are things a hand-rolled exporter would get wrong:

**Angle segments stay one measurement.** Every file in a state's `data_files`
becomes a series of one ISAAC record. A REF_L steady state is three angles with
three run numbers and is one measurement; exporting three records would claim
three that never happened. This is a bug AuRE has already fixed once, in
`assembler/workflow/assembler.py` --- the primary partial gets the full
assemble and the rest become `additional_reflectivities` of the same state.

**Co-refined states are one sample.** `distinct_sample: false` gives every
state the same sample id, and `convert-ingest` then cross-links the records
with `links[].same_sample_as`. Verified on the real 3-state Cu/THF
co-refinement: 9 files in, 1 sample + 3 environments + 1 fit out, then 3 valid
ISAAC records with 3 series each and 6 link relations.

The grouping is never inferred from filenames. `Measurement.key` from the
frozen spec (`run218386#0`) is the same string the manifest recorded in
`info.models[].name`, so resolving the spec reproduces exactly what was fitted.
A hand-written script with no spec falls back to a single state, which is the
honest answer: the segments still assemble, and the loss of per-condition
records is reported rather than guessed at.

### 2026-08-10: a direct reference blocks PyPI from inside an extra too

Adding the `isaac` extra tripped `test_wheel_declares_only_the_one_expected_
direct_reference`, which is the guard working. Worth keeping straight:

* an extra-scoped direct reference costs nothing to anyone who does not ask
  for the extra, so several are fine;
* an unconditional one is imposed on every install, so exactly one is allowed
  (`aure`);
* neither buys back PyPI. A PEP 508 direct reference blocks upload regardless
  of which extra it sits in --- the same trap already recorded in aure's own
  `export` extra.

The test now separates the two rather than counting them together.

### 2026-08-10: the assembler names its output by uuid, so re-export doubles it

Reported from real use: `nrw isaac export` produced six records for three
measurements, in pairs suffixed `_2`. The staging directory had 18 reflectivity
parquets where there should be 9, two samples and six environments.

`stage()` created its directory with `exist_ok=True` and never cleared it.
`data-assembler` writes uuid-named parquet, so a second run adds a complete new
set beside the first rather than overwriting it, and `convert-ingest` --- which
groups by `(sample_id, environment_id)` --- then sees six states and emits six
records. Nothing errors; the output is simply twice the experiment.

Every test staged into a fresh `tmp_path`, so none could see it. Re-running a
command into the same output directory is worth testing explicitly whenever the
step in between writes content-addressed or uuid-named files.

The fix has to empty the directory, and "empty this directory" must never be
pointed at one somebody else owns. So the staging dir carries a sentinel
(`.nrw-isaac-staging`) and only a directory holding it is replaced; anything
else refuses and names `--force`. Note an AuRE run directory also holds
`run_info.json` + `problem.json`, so the file layout alone is *not* proof of
ownership --- hence an explicit marker rather than a signature.

### 2026-08-10: a test that greps PATH passes or fails on who ran pip last

`test_export_says_what_is_missing` cleared `PATH` to check the not-installed
message. It passed until the `isaac` extra was actually installed into this
repo's venv, because `_find` also looks beside `sys.executable` --- which is
exactly where pip puts console scripts.

An "X is absent" test has to make X absent through every lookup path the code
uses, not just the obvious one. Here that means patching `shutil.which` *and*
`sys.executable`, which in turn exposed that `_find` would raise `OSError`
rather than degrade when handed an interpreter that does not exist.

### 2026-08-10: concatenating angle segments needs the fitted scales, not just a sort

Three REF_L angle segments are one measurement, so an ISAAC record should carry
one curve. But each segment has its own normalisation: on run 218386 the 3.5 deg
segment fits `probe intensity` 0.789, i.e. it is 21% low. Appending the raw
files publishes a curve with a visible step in it.

The correction is `R / intensity`, and the direction is measurable rather than
a convention. On that run the segment-2/3 overlap goes:

    raw            +30.4%
    R / intensity   +1.1%     <- correct
    R * intensity  +68.4%

`dR` scales with `R`; `Q` and `dQ` are geometry and do not. Only nr-workbench
has these numbers --- they are fitted parameters, so no downstream tool can
recover them.

Two details the merged file needs: the primary segment's header must be carried
verbatim (the reader takes the run number, IPTS and reduction version from
there, not from the filename --- drop it and every record says "Unknown"), and
the name must follow `REFL_<run>_combined_data_auto.txt`, which is REF_L's own
convention for a stitched curve.

### 2026-08-10: free prose in a field that is regex-parsed corrupts the parse

`nr-isaac-format` fills a record's electrochemistry from the assembler's
*structured* fields and, when those are empty, falls back to parsing the
measurement description. `--context` overrides that description for every
record at once.

So passing the analysis note as `--context` --- where a promote reason read
"3-state co-refinement (OCV/potential/OCV)" --- made all three records report
`open_circuit`, including the galvanostatic one whose structured condition was
legitimately absent. The fallback is reasonable; combining it with a shared
free-text override is not.

The rule: never put narrative prose into a field something else parses for
facts. The per-state condition owns the measurement description, and a
multi-state export passes no `--context` at all.

### 2026-08-10: the schema has no galvanostatic control

`assembler.parsers.conditions.parse_conditions` recognises open circuit, a
potential in volts, pH and molar electrolytes. It has no notion of a current
density, so `-0.5 mA/cm2` yields no structured condition at all --- and
`_classify_environment` only promotes a measurement to `operando` on an applied
*potential*, so an electrochemical cell under galvanostatic control is recorded
as `ex_situ`.

Both are wrong for this beamline's most common operando experiment. The text
still reaches `series[].notes`, so nothing is lost, but the structured fields
that make a record queryable are empty. Worth fixing upstream: a
`current_density` field plus `control_mode: galvanostatic`, and the same
operando bump a potential gets.

### 2026-08-10: three signals an autonomous agent would have read wrong

Surveying nr-workbench for autonomy turned up three defects, each of which
corrupts an input the agent would depend on. All three were verified against
the real Cu/THF corpus before being fixed.

**`converged: True` was hard-coded** into `judge_fit`, so the language model
was told every fit converged. On that corpus the one non-converged fit
(`20260807-162217Z-d143fdbc`, 17 parameters) has chi-squared 1.264 --- *better*
than the published answer's 1.285. The judge was being handed a false value on
the exact axis that disqualified the fit. `FitOutcome` now captures bumps'
warning, and the distinction that matters is three-valued: `False` warned,
`True` a sampler ran and was quiet, **`None` the fitter does not test
convergence at all**. An optimiser has no opinion, and recording that as `True`
asserts something nobody checked.

**`nrw assess --write` defeated the blank-note test.** It appends into the same
`NOTES.md` a person writes into, and its facts line (`chi-squared 1.285, 8
free, ...`) is bare prose --- so `is_blank` returned False and every assessed
fit read as "somebody thought about this" in `nrw ls`. Running assess over a
project would have erased the distinction the notebook is built on. Generated
prose is now fenced in `<!-- nrw:generated -->` markers that `is_blank` and
`Note.summary` strip. An unterminated fence hides the remainder rather than
trusting it: unmarked machine prose counted as human is the failure worth
avoiding.

**Five of twenty-five result directories were orphans.** `commands/fit.py`
wrote the directory, script, inputs and environment *before* the fit and the
manifest *after*, and only `FitError` was caught --- so a kill, an OOM or a full
disk left a directory invisible to `ls`, `whence` and `check`. One orphan held
a 298 MB posterior chain nothing could find. A provisional manifest with
`status: running` is now written first, and `nrw check` reports
`interrupted-run` for any result directory the index does not know. Confirmed:
it finds all five in the real project.

The general lesson for autonomy: **the failure modes that matter are the ones
that make a wrong answer look like a checked one.** All three were invisible
under supervision because a human was reading the terminal; none would have
been visible to a daemon.

### 2026-08-10: `\b` does not bound a token in a REF_L run title

`nrw data reconcile` compares each run's own title against how `sample.md`
describes it, and the case it exists for is the real one: a table listing
218393 as `OCV` when its title says `CA-realigned` --- an error the record says
*"sent five fits down the wrong path"*.

The first version missed it. `\bCA\b` does not match `_CA-`, because `_` is a
word character and there is therefore no boundary before the `C`. REF_L run
titles are underscore-delimited by convention
(`CuPt_d8-THF-fullQ_CA-realigned-218393-1`), so `\b` is the wrong boundary for
exactly the strings this parses. Spelled out as
`(?<![A-Za-z0-9])CA(?![A-Za-z0-9])` instead.

### 2026-08-10: precision is the feature, on a check nobody is obliged to read

Two of the first five reconciliation checks were noise, and both would have
trained the reader to skip the output:

- **Word overlap between a run title and a table cell.** A title is a
  filename-shaped label and a cell is prose; they share vocabulary by accident.
  It fired on 3 of 4 real runs, of which 1 was a true finding and the mechanism
  for that one was luck. Replaced by a comparison of the *electrochemical
  state* alone --- the thing that actually got mislabelled --- which fires on
  the real error and is silent on the other three.
- **Segments of one run using different direct beams.** That is every REF_L
  measurement ever made: one direct beam per angle. The real signal is the same
  *angle* disagreeing across runs, which is what puts another beam's intensity
  into one measurement. On the real corpus every angle is consistent
  (218274/218275/218338 for segments 1/2/3) and the only outlier is 218389,
  already caught as a blocker.

After both changes the real project reports exactly one finding, and it is
correct. A checker that cries wolf is worse than no checker: it costs attention
every run and it teaches the reader that the output is noise --- at which point
the one true finding is missed too.

### 2026-08-10: the checker's first false positive was the published fit

`contradictions.check` compares a spec's constraints against what
`nrw tnr assess` read off the data --- the Red Flag four skills state and
nothing implemented. Written per constraint, it immediately flagged
`cu-thf-tnr-reduced`: the **promoted** fit, the one in the paper.

That fit carries two constraints, `linear_in_time` on `Cu.thickness` and
another on `CuOx.rho`. Checked one at a time, the rho constraint looks like it
ignores a template implying thickness --- while the thickness constraint
sitting beside it honours the template exactly. One constraint per varying
quantity is the normal shape of a spec, so the per-constraint form of this
check flags a large fraction of correct work.

Judged over the whole spec instead: is the implied change varied *anywhere*.
Both promoted specs now read clean.

Second time in two days that the first version of a check was too eager, and
the pattern is the same both times: **a rule stated for a human reading one
thing at a time does not transfer directly to a program reading everything at
once.** The skills say "a freed parameter contradicting `implied_change`"
because a person weighs the spec as a whole without noticing they are doing it.

### 2026-08-10: `str.format` cannot template YAML

The test fixtures build specs from a YAML template, and YAML flow style is
`{name: dTHF, rho: 6.35}` --- which `str.format` reads as a replacement field
and raises `KeyError: 'name'` on. Every fixture failed at once, which at least
made it obvious. Use `.replace()` with explicit sentinels for anything
templating a language that uses braces.

### 2026-08-10: "is this SLD reached anywhere in the profile" is nearly vacuous

The check for a layer swallowed by its own interfaces was first written as
"does the fitted profile come within a slack of this layer's SLD". It never
fired, and it never could have: a profile is a continuous curve from the
ambient to the substrate, so it passes through **every** intermediate value on
the way. Asking whether 4.005 appears somewhere between -1.94 and 6.31 is
asking whether the curve is continuous.

The criterion the real analysis actually derived is arithmetic on the layer
table, not the curve: ``sigma_top + sigma_bot`` against ``t``. On the fit whose
oxide was an artefact that reads 20 + 12.99 = 32.99 against 21.29 --- 1.55x its
own thickness --- which is the finding, reproduced exactly and in one
subtraction.

Two traps on the way there, both silent:

- The roughnesses belong to different rows. ``sigma_top`` of a layer is the
  *previous* row's interface; ``sigma_bot`` is its own.
- ``read_slabs`` renames bumps' ``interface`` column to ``roughness``. Reading
  the original name returns the ``0.0`` default, so the sum was always zero and
  the check was silently inert. A `.get` with a default is how a check quietly
  stops checking.

Also worth recording: attainment must be judged **per state**. In that
co-refinement OCV1's oxide was swallowed at 21.3 A while OCV2's, at 48.3 A, was
fine. A fit-wide test sees one state attain the value and says nothing.

### 2026-08-10: a free parameter's nominal value is only where the fit started

The same check, in an earlier form, compared the profile against the SLD
declared in the spec's ``materials`` block. That flagged Ti as absent from the
promoted fit --- declared -1.978, fitted -1.662, profile minimum -1.62. The
layer was present and correct; the comparison was against a number the fit had
been free to leave behind.

When a parameter is fitted, the fitted value is what the result claims. The
declaration is a starting point, and testing a result against it asks whether
the fit moved, not whether the answer is sound.

### 2026-08-10: the checks reproduce the findings; the count does not rank the fits

Running every Stage-1 check over the 25 recorded fits of the real beamtime,
against the 17 findings the analyst wrote by hand. Nine labelled cases passed
--- the swallowed oxide at 1.55x its thickness, the roughness that caused it
named as `dTHF.roughness=20`, the ranges that permitted it flagged before any
fit, the (rho, t) ridge, the three pinned parameters, and the stray partial as
a blocker.

**The benchmark that ran them was removed on 2026-08-12.** It lived in
`tests/test_benchmark_expt11.py` and reached into a 5.8 GB corpus under
`~/Dropbox-ORNL`, so it skipped everywhere except one machine --- and silently,
which is worse than not existing: the suite reported success while the claims
here went unchecked. A test suite may not depend on data outside the repository.
The measurement above stands as a record of what was found; nothing asserts it
any more, and re-establishing it would mean vendoring the handful of numbers it
turns on rather than the corpus.

Two negative results are worth more than the positives.

**Finding count is not a score.** The promoted tNR fit has the fewest findings
of all twenty (3); the promoted steady fit has one of the most (17). Both are
the answer. The count scales with the number of parameters --- a three-state
co-refinement has forty-one and a reduced tNR has eight --- so ranking on it
measures model size. The benchmark asserts `tnr < steady` precisely so nobody
later mistakes the count for a quality signal.

**Correlation does not discriminate.** All 19 fits with a chain carry pairs
above 0.8, because correlation is the honest shape of a reflectometry
posterior, not a defect. A warning that fires on everything sorts nothing, so
it is `info`. What a strong pair changes is what may be *quoted* --- and
whether both parameters were reported independently is a judgement about the
write-up, which no check on the fit can see.

Together those say what the checks are for: they re-derive the *specific*
observations a human made from the same files, and they do not rank. Ranking
was the part that needed judgement, and the evidence that chi-squared ranks
this corpus backwards is the reason not to automate it.

### 2026-08-10: an instruction is not a mechanism, and one mechanism is not two

The unattended-session work (`nrw agent run`, `nrw agent watch`) is built
around a distinction that is easy to blur in a prompt: telling a model not to
promote a fit is a *request*, and a `PreToolUse` hook that exits 2 is a
*limit*. Only the second survives a model that decides the rule does not apply.

Both are implemented, and they are independent on purpose:

- the hook in the scaffolded `.claude/settings.json`, which refuses before the
  command runs, and
- `NRW_AGENT=1`, which `nrw agent run` sets on the session itself, and which
  makes `nrw` refuse from the inside.

A hook can be misconfigured and an environment variable can be unset, but both
failing silently at once is a different order of accident. Neither is a
sandbox; they stop the plausible mistake, which is the one that happens.

Three details cost real thought:

**Judge every segment of a command line.** `nrw ls && nrw promote abc` reads as
allowed if you look only at the first command. The guard splits on
`&& || ; | &` before judging. A mutation test confirms it: judging only the
first segment fails three cases.

**Fail *open* on an unreadable hook payload.** The first version returned a
block when the JSON would not parse. That makes our own bug into a project that
cannot run any Bash command at all. An unreadable payload is our fault, and the
second mechanism still holds.

**`--force` is a refusal, not a nuisance flag.** Every forcing flag in this
codebase exists because a check said no. An agent reaching for one has arrived
at exactly the situation a person is meant to see, so it is blocked with the
same weight as promotion.

Every refusal names `ESCALATIONS.md`. An agent told only "no" retries; one told
where to write the decision stops.

### 2026-08-10: the daemon's three questions, and why readiness is per-run but a session is per-sample

`nrw agent watch` asks only: has this measurement finished arriving, is it
trustworthy, and has anybody looked at it. Two findings from building it:

**Readiness is per-run, a session is per-sample.** Run 218389 in the reference
corpus is simultaneously quarantined (its stray whole-run reduction sits in
`data/steady/`) and ready (the real 130-slice series is complete). The watcher
correctly reports both --- so `Verdict.kind` is part of the identity, not a
label. But a session started for `expt11` would then be free to fit the
poisoned copy, so `session._observe_quarantine` runs the same check and names
the run in the prompt under "DO NOT FIT". The scheduler and the session have to
agree, and the only way to guarantee that is to share the function.

**"Already analysed" is read from the index, never from a state file.** A state
file is a second source of truth that goes stale the first time the scientist
fits something by hand, after which the daemon repeats work that is already
done. That is not a safety failure --- it is the fastest route to the failure
that actually matters, which is output nobody reads.

The output budget (`MAX_REPORT_WORDS`) is asserted in
`tests/test_agent_watch.py`, not exposed as a setting, for the same reason:
forty individually defensible records are collectively unreadable, and once the
scientist stops reading, every other safety property is a formality.

### 2026-08-10: a command-line guard must read the whole line, and a six-digit number is not a run number

Two defects found by reviewing the unattended-agent work, both of the same
shape: a recogniser that looked at part of its input and was confident about
the rest.

**The guard split tokens, not text, and looked only at the first three words.**
`shlex.split` keeps `ls;` as one token and treats a newline as ordinary
whitespace, so a token-level split never separated the commands at all. Every
one of these performed a refused action and was *allowed*:

```
if true; then nrw promote abc --reason x; fi
for f in *.py; do nrw fit run $f --force; done
cd samples/S1; ls; nrw fit run m.py --force
git add -A⏎nrw promote abc
A=1 nrw promote abc
python -m nr_workbench.cli promote abc
```

None is obfuscation. They are what batched shell work looks like when a model
writes it, and `then`/`do`/`cd`/an env-var prefix each push the real command
past any fixed window. The fix is to split the **raw string** on `\n;&|()`
quote-aware, before tokenising, and to scan **every** token for the invocation
rather than the first few. The positional window bought nothing a whole-segment
scan does not.

Related: failing open on a `shlex` parse error was too generous. `shlex` is not
bash — `$'it\'s'` raises there and runs fine in a shell. Now an unparseable
segment falls back to a raw-text check and is only allowed when it names
nothing refusable.

**A regex for six digits found the subrun as well as the run.** The watcher
decided "already fitted" by pulling every `\d{6}` out of each recorded input
path. `REFL_218386_2_218387_partial.txt` contains two: run 218386 and subrun
218387. At a beamline that numbers consecutively, 218387 is also a real,
separate, unfitted measurement — so recording one three-segment fit marked its
two neighbours as done and the daemon skipped them without saying anything.

The rule: **when a project already owns a filename grammar, never re-derive its
fields with a looser pattern.** `project/scan.py` has `PARTIAL_RE` with a named
`run` group, and it was already imported two functions away.

Both bugs were invisible in normal use, which puts them with the two worst
found so far in this repo (data files missing from the wheel, `init` not being
idempotent). All four share a signature: the code produced a plausible answer
and no error.

### 2026-08-10: a second, weaker model is not a fallback

nr-workbench can call a configured LLM endpoint in three places: `nrw assess`
(are these values physically sensible?), `nrw model new --from-notes` (propose
a stack), and `nrw isaac export` (write condition sentences). All three were
written when the endpoint was the only model in the picture.

Under a coding harness that assumption inverts. The harness *is* a language
model, and on this work a better one — that is the measured premise the whole
`agent/` package rests on. Sending the same question out to the endpoint and
handing the answer back does not add a check:

- it replaces the judgement we wanted with a worse one, and
- the harness then reads that verdict inside its own context as *evidence*,
  which is worse than no verdict at all.

Observed directly: `nrw assess` on a 3-parameter fit returned six generic
suggestions from the endpoint ("use a better optimizer only after the model is
physically appropriate"), which under a harness is noise competing with the
harness's own reading of the same numbers.

So `agent_is_driving()` gates all three, and each hands the work over rather
than calling out. `nrw model new --from-notes` degrades to exactly what
`--print-prompt` already did, which is the shape the codebase had already
found for "a coding assistant is here, give it the instruction instead".

The consequence worth writing down: **an LLM endpoint is not part of the
agent's setup.** It remains useful to somebody with no harness open.

The generalisable form: when a tool can either do a thing itself or ask its
caller to, and the caller is better at it, asking is not a degraded mode — it
is the correct one. The failure to avoid is a *plausible* second opinion, not
a missing one.

### 2026-08-10: a headless session with only a deny list does nothing at all

The first real end-to-end run of `nrw agent run` fitted nothing. Every Bash
call returned **"This command requires approval"**. Claude Code's permission
layer denies anything that would prompt, headless has nobody to prompt, and
the scaffolded `.claude/settings.json` carried a `deny` list and no `allow`.
Forty-five turns, zero commands, and then the model tried to write itself a
`.claude/settings.local.json` to escape — the self-disarm path a review had
predicted, reached for unprompted the first time it was cornered.

An `allow` list is not the fix. The same run shows why: `nrw doctor 2>&1 |
head -40` was refused because the permission matcher splits on the pipe and
`head` was not allowed. An analysis agent runs arbitrary shell; enumerating it
in advance is a losing game, and each gap is a wasted turn.

So the harness runs with `--permission-mode bypassPermissions`, which was only
adopted after verifying the thing that makes it safe:

    CALL:   Bash {"command": "nrw promote abc123 --reason test"}
    RESULT: PreToolUse:Bash hook error: Refused by nr-workbench (promote): ...

**Hooks fire independently of the permission layer.** The two mechanisms this
package actually designed and tested — the `PreToolUse` hook and `NRW_AGENT=1`
— are untouched by bypassing. What is lost is the `deny` list, which was
always the weakest of the three and the only one that needed a human at a
keyboard to mean anything.

The consequence: the hook is now load-bearing rather than belt-and-braces, so
`session.run` verifies it before *every* session. Checking once is not enough
when the thing being checked is a file the session can edit.

The general lesson, and the reason this cost a whole run to find: **a safety
layer that also blocks the work is not a safety layer, it is an off switch.**
The agent's first instinct on hitting it was to route around it, which is what
any capable agent will do. Better to remove the layer that cannot distinguish
`nrw fit run` from `nrw promote`, and keep the one that can.

### 2026-08-12: `upstream.toml` only tracks genuine upstream now

It previously carried entries for `neutrons-ai/nr-analyzer`,
`mdoucet/experiments-2025` and `mdoucet/ai-project-template` alongside AuRE, as
if all four were external dependencies being vendored in. They were not: those
three are this project's own earlier prototypes, being folded into
nr-workbench properly rather than tracked as if borrowed from elsewhere. AuRE
is the only repo nr-workbench actually depends on and does not own.

Removed the other three entries, along with the per-file "Adapted from ...,
see upstream.toml" headers that pointed at them (`fitting/runner.py`,
`instrument/geometry.py`, `skills_install.py`, `tnr/__init__.py`, several
`SKILL.md` `source:` blocks) and the matching test infrastructure
(`tests/test_vendor.py`). `_vendor/result_manifest.py` moved to
`provenance/result_manifest.py` — it was only in `_vendor/` because of the
nr-analyzer tracking entry, and the schema it implements is nr-workbench's own
code now, not a byte-identical copy of something external.

Public-repo motivation: this package is being published alongside a paper, and
these were internal prototype cross-references the wider audience has no
reason to see and the team had not deliberately signed off on publicizing.

### 2026-08-12: `nrw init` could scaffold a project inside another project

Nothing checked whether `path` (default `.`) was already inside an existing
project. `cd` into a sample's data directory out of habit and run `nrw init`
there, and it would succeed: a second `nrw.toml`, a second `.nrw/`, a second
`samples/` tree, nested inside the first, with no code anywhere that
reconciles them. `nrw sample new` was never at risk — it resolves its target
through `ProjectLayout.discover()`, which walks *up* from the cwd looking for
`nrw.toml`, so it always lands on the enclosing project regardless of which
subdirectory it is run from.

Fixed in `commands/init_cmd.py::run_init`: before scaffolding, if `path` has
no `nrw.toml` of its own (a plain re-init in place is unaffected), walk up
from its parent and refuse if an ancestor already has one. `--nested` is the
escape hatch, guarded the same way `--force` is — both the `PreToolUse` hook
(`agent/guard.py`) and `nrw` itself under `NRW_AGENT=1` — because it is the
same shape: a flag whose only job is to override a check that said no.

**`click.testing.CliRunner` never touches `sys.argv`.** The group callback in
`cli.py` that gates `--force` and `--nested` under `NRW_AGENT` reads
`sys.argv[1:]` directly, which is correct for a real `nrw` process but
invisible to `CliRunner.invoke(main, [...])` — the args list it takes goes
straight to Click's parser, never through `sys.argv`. An end-to-end test of
that gate has to `monkeypatch.setattr(sys, "argv", [...])` itself; without
that it silently passes for the wrong reason, exit code 0, no exception,
looking exactly like a working refusal. This is why the pre-existing `--force`
gate had no end-to-end test at all — only the unit-level `guard.judge`/
`guard.refuse_if_agent` calls were covered.

### 2026-08-13: `--note` was recorded, stored, and then counted as nothing

`nrw fit run --note "..."` reached `write_notes_stub` as `description`, which
the template renders as a blockquote — and `notes.is_blank` skips `>` lines by
design, because that blockquote is an echo of the launch command and counting
it as content would make every untouched stub look written-in. So a reason that
*was* stated at the one moment it is in front of the person running the fit was
captured, written to disk, and then reported by `nrw ls`, `report --check` and
`handoff` as a fit nobody had thought about.

Fixed by separating the two things that had been folded into one field. The
blockquote is now always what ran (`amoeba fit of film.`); `--note` goes through
`notes.write_section` into `## Why this run`, where the blank-note signal can
see it. `write_notes_stub` gained a `why=` parameter to keep that split at the
one place the stub is written.

The signal itself is untouched, and must stay that way: a run launched with no
`--note` still reads as blank, and nothing generates the other two sections.
The reason to resist filling `What it showed` from chisq is the same reason
`nrw assess` wraps its output in `nrw:generated` fences — a note holding only
machine prose looks written to anyone skimming it, which is precisely how 25
result directories in the reference beamtime passed for documented.

**The post-fit summary was pointing an agent at a command a hook refuses.** It
ended with `nrw whence` and `nrw promote`, and `promote` is one of the three
things `agent/guard.py` blocks outright. Under `NRW_AGENT` that is not merely
useless: an unattended session has a finite turn budget, and it was being spent
on a refusal at the exact moment the session should have been writing down what
it just learned. `_next_steps` now leads with `nrw note`, asks only for the
sections `--note` did not already answer, and drops the `promote` line when
`agent_is_driving()`.

### 2026-08-13: a fit listing that names the script has not named the model

The fits page showed model name, chi-squared, free parameters, and a change
line that read `first run of this model` for every fit that started a new
model — which is most of them early in a beamtime, and which tells a reader
nothing they cannot see from the list itself. None of it answers the question a
reflectometrist actually asks about a row, which is *what was this a fit of*.

Two additions, both to `ProjectData.fits()` so `/fits`, `/s/<id>` and
`/api/fits` get them at once:

**The stack, as `THF|Cu|Ti|Si`.** Three sources, and the order matters:

1. `provenance/stack.py::describe` reads it off the assembled refl1d `Stack` at
   fit time and `fit run` records it in the index entry. Authoritative — it is
   the object the optimizer sees — and free, since the problem is already
   loaded.
2. `from_fit_dir` reconstructs it from bumps' own `fit/*-expt.json`, whose
   `object.sample.layers[].name` survives in every result directory. This is
   what makes the feature useful on a project that already has fits rather than
   only on the next one.
3. The frozen `spec.yaml`, which is the only source left for a fit that failed
   before bumps exported anything — exactly the fit somebody is trying to tell
   apart from the one before it.

It is *not* parsed out of `model.py`. The stack expression is ordinary Python —
built in a loop, assembled in a helper, conditional on a flag — and a regex over
it fails quietly, which for a structure label is the worst available failure: a
plausible stack that is not the one that was fitted.

`problem.models` is a **generator**, not a list. Reading it twice reads it
empty; `runner.describe_models` already knew this and `stack.describe` has to.

**Why the run was made.** `summary.describe` falls back to a settings phrase
(`amoeba, 12 steps, 21 free`) when there is no note — but every field of that is
already a column of the same table. The listing now reads the *Why this run*
section of the fit's own `NOTES.md` (`notes.read_section`, the counterpart
`write_section` never had), then any other prose in that note, then the launch
`--note`, and shows nothing generated. A fit with no reason gets the command
that records one instead of a filler phrase.

`read_section` keeps paragraph breaks rather than joining the section into one
line: `nrw note --why` *appends*, so a fit asked twice has two answers under one
heading, and running them together reports a sentence nobody wrote. The listing
takes the first paragraph; the fit page shows all of it.

**One template, included twice.** `fits.html` and `sample.html` each had their
own copy of the story row and the copies had already drifted. They are now
`_fit_story.html`, and `first_run` is a Jinja *global* rather than a per-route
variable — an undefined name in Jinja is falsy, so a route that forgot to pass
it would silently start showing the useless line again with nothing to show for
it.

### 2026-08-13: `bool` subclasses `int`, and it cost a whole beamtime session

`spec/resolve.py` resolved a pinned parameter with

```python
pinned = parameter.fixed if isinstance(parameter.fixed, int | float) else value
```

`ParameterSpec.fixed` is typed `float | bool | None`, so both forms are legal:
`fixed: 31.2` means "pin here", `fixed: true` means "pin at `value`". But
**`isinstance(True, int)` is `True`** — `bool` is a subclass of `int` in Python —
so every `fixed: true` took the numeric branch and pinned the parameter to
`float(True)` == **1.0**, silently discarding the `value:` beside it.

What that did to a real analysis: `{path: Ti.thickness, value: 31.2, fixed:
true}` generated a 1 Å Ti layer, and `{path: Ti.rho, value: -1.88, fixed: true}`
generated ρ = +1.0. The stack being fitted was physically impossible while the
spec, read by eye, was exactly right. χ² went 1.31 → 16.6 on that one edit and
the session spent eleven more fits trying to optimise its way out, then wrote an
escalation concluding the *data* could not constrain the structure. Thirteen of
nineteen recorded fits carried a spuriously pinned parameter.

Three lessons, all now enforced:

1. **In any `X | bool` union, test `bool` first.** The numeric test accepts it.
   `_pinned_value` in `contradictions.py` carries the same ordering for the same
   reason.
2. **A silent default is worse than a crash.** 1.0 is a plausible-looking number
   for a thickness, an SLD, an intensity — nothing downstream could tell it from
   a value someone meant. `tests/test_spec.py` now asserts the pinned *value*,
   not just that the parameter came out fixed.
3. **A pin needs a reader-facing cross-check**, because the number was on screen
   the whole time as an unremarkable `1` in a Start column. `nrw check` now
   reports `pin-contradicts-stack` when a pin sits more than a factor of two
   (length) or `SLD_SLACK` (SLD) from what the stack declares.

### 2026-08-13: two fitters, because a fitter menu is an escape hatch

`nrw fit run --method` accepted anything bumps offers. It now accepts `amoeba`
and `dream` only — `nr_workbench/fitters.py`, enforced at the CLI
(`click.Choice`), in `FitSettings.method`, and in `run_fit` for the library path.

The reason is behavioural, not technical: `de`, `lm` and `newton` work fine. But
in the session above, five of the twelve doomed fits differed from their
predecessor in *nothing but the optimiser* — the menu was what the session
reached for instead of reverting the edit that broke the model. Two fitters with
their roles named (`amoeba` explores, `dream` quotes) make the next thing to
change obviously the model. Off-menu fitters remain reachable by running bumps
on the generated script directly, which is a deliberate speed bump and leaves
the result outside the provenance record.

Paired with it: `summary._chisq_trend` now appends `(12.7x WORSE)` past
`REGRESSION_FACTOR`, and `nrw fit run` prints a yellow warning naming the model
edit as the suspect. `chisq 1.306 -> 16.58` had been on screen all along and read
as a neutral fact, which is what it looks like.

### 2026-08-14: a committed copy of a generated schema drifts, and closed objects make that a rejection

`skills/reflectometry/nrw-model-spec/assets/nrw-model-1.json` was maintained by
hand alongside the pydantic models it claims to describe. It fell four changes
behind: no `Constraint.endpoint_range`, no `Trim`, no `probe.dq_scale`, no
`per: angle`, and it still listed a `moderator` resolution the code had dropped.

The damage is not staleness, it is that every generated `$def` carries
`"additionalProperties": false` (from `ConfigDict(extra="forbid")`). A closed
object missing a key does not ignore it — it *rejects* it. So the asset said a
valid, documented, supported spec was invalid, while the SKILL.md three
directories up documented `endpoint_range` correctly. A reader believed the
machine-readable file over the prose, concluded the key was unsupported, and
designed a model around its absence.

Two rules came out of it:

1. **Generated artifacts that must be committed get a regeneration command and
   a test, never a maintainer.** `python tools/regen_schema_asset.py` writes it;
   `test_spec.py::test_the_bundled_schema_asset_matches_the_live_models` compares
   bytes and names the command in its failure message.
2. **`nrw init` writes the project's `.nrw/schema/nrw-model-1.json` as a scaffold
   file.** It never had — `spec/schema.py`'s own docstring claimed it did, and
   `.vscode/settings.json` had mapped `samples/*/models/*.yaml` onto the missing
   path since the beginning, so real projects showed "Unable to load schema ...
   No content" and got zero editor validation. Routing it through the scaffold
   (rather than a one-off write at init) means a package upgrade refreshes it on
   the next `nrw init`, `--check` reports it pending, and a hand-edited copy is
   left alone. `nrw doctor` now reports it as missing or stale, because the
   failure was otherwise silent by construction.

### 2026-08-17: reporting a configuration is not testing a connection

`nrw doctor` reported the harness binary it found on PATH and the endpoint
variables that were set, and both lines could be green while nothing could
answer a question. Every failure mode of a third-party provider is invisible to
a configuration check by construction:

* a Foundry deployment name that was never created — Claude Code has no startup
  model check, so an unpinned or wrong `ANTHROPIC_DEFAULT_*_MODEL` fails at the
  *first request*;
* an expired key, which is a valid-looking string;
* a gateway that resolves and refuses;
* a daemon whose unit file is missing the provider variables that are in the
  operator's `.zshrc`.

All four present identically from the outside: an agent session that starts and
achieves nothing. During a beamtime that is discovered in the morning.

`nrw check-llm` makes the calls. Three decisions in it are worth keeping:

1. **It goes through `session.harness_command()`, not a hand-written argv.** A
   probe that used different flags could pass while `nrw agent run` failed,
   which is worse than no probe. This also means it exercises `NRW_HARNESS` and
   the flag contract a site's own wrapper has to meet.
2. **It probes the default model, and costs what that model costs** (a few
   cents). Probing something cheap was the obvious optimisation and it defeats
   the purpose — an undeployed model pin is the specific failure being looked
   for. The measured cost is printed instead of being hidden.
3. **A reply that comes back but is not the token asked for is a `warn`, not a
   pass.** A gateway that rewrites or summarises replies is a real
   configuration, it will mangle a session's tool calls, and it is
   indistinguishable from a healthy setup in any check that stops at "did bytes
   come back".

The generalisable form, and the fourth entry in this file with the same
signature: **a check that reads settings cannot fail the way the system fails.**
The other three — data files missing from the wheel, `init` not being
idempotent, angle segments counted as separate measurements — all produced a
plausible answer and no error, and all were found only by exercising the real
path.

### 2026-08-17: `.github/agents/` is still live, but the reason we strip `tools:` is not

Checked while making harnesses selectable, because the 2026-08-05 entry above
rests on a claim about Copilot's schema that is three months old and Copilot has
moved.

**The dispatcher tree is fine.** GitHub's custom-agents reference still reads
agent profiles from `.github/agents/`, and it deduplicates on "the
configuration file's name (minus `.md` or `.agent.md`)" — so the
`<skill-name>.md` names we write are recognised, and the newer `.agent.md`
convention is an alternative rather than a requirement. Nothing to change.

**The stated rationale is stale.** `tools:` is now a *supported* Copilot
frontmatter field, and the reference says "all unrecognized tool names are
ignored, which allows product-specific tools to be specified in an agent profile
without causing problems." So Copilot no longer rejects Claude's tool names — it
ignores them.

We still omit the line, for a different reason than the one recorded: a
guardrail written in another product's vocabulary is not a guardrail. Emitting
`tools: Read, Grep, Glob, Bash` into a Copilot profile would read as a read-only
restriction to anyone opening the file while doing nothing at all. If Copilot's
dispatchers should be constrained, that has to be written in Copilot's own tool
names, which is a separate piece of work and not one this repo has done.

The generalisable form: **a rationale ages faster than the behaviour it
justifies.** The behaviour here was still right; the sentence explaining it had
become false, and a future change would have been argued from the false half.

### 2026-08-17: OpenCode's config rejects unknown keys but allows comments

Verified against the published schema at `https://opencode.ai/config.json`, not
inferred from the prose docs. Two facts that between them decide how the
scaffolded `opencode.json` is written:

* The root `Config` definition sets **`additionalProperties: false`**. The
  `"$comment"` array trick used in `.claude/settings.json` would be *rejected*
  here, not ignored.
* The schema document sets **`allowComments: true`** (and
  `allowTrailingCommas`), so the file is JSONC and `//` comments are the
  supported way to keep the reasoning next to the rules.

So the two config files carry their rationale differently on purpose. Anything
reading `opencode.json` back — a test, a doctor check — must strip line comments
before `json.loads`, or it will fail on a file that is perfectly valid.

`permission.bash` takes a map of glob-ish command patterns to
`allow`/`ask`/`deny`, and **the last matching rule wins**, which is the opposite
of what a deny list usually implies. A rule added below the promote denials that
also matches would silently undo them.

The shipped config was validated against the fetched schema with `jsonschema`
before being committed. That check is not in the test suite because it needs the
network; the test asserts structure instead, and this entry records that the
schema check was actually run and on what date.

### 2026-08-17: The instruction body lives in AGENTS.md, and CLAUDE.md points at it

`AGENTS.md` turned out not to be an OpenCode-private file. GitHub Copilot reads
it too, *in addition to* `.github/copilot-instructions.md` — its docs say all
matching instruction sets are combined. OpenCode is the opposite: first match
wins, and an `AGENTS.md` present means `CLAUDE.md` is never read.

Writing an `AGENTS.md` that mirrored `CLAUDE.md` would therefore have created
~140 duplicated lines with three different reading rules over them. Instead:

* `AGENTS.md` holds the nr-workbench body and is installed for every project.
* `CLAUDE.md` is a wrapper that `@`-imports `.github/copilot-instructions.md`
  and `AGENTS.md`, and says so.
* `opencode.json` lists `.github/copilot-instructions.md` under `instructions`,
  because OpenCode picks up `AGENTS.md` on its own but not the shared workflow.

All three assistants end up with the same two documents by three different
mechanisms, and there is one copy of each. This is the same "thin dispatcher,
fat skill" split the skills use, applied one level up — and the reason to
prefer it is the same: two copies of a standard become two different standards.

### 2026-08-17: Each hidden directory level in `templates/` needs its own package-data glob

The 2026-08-05 entry above fixed dotfiles at one level with
`templates/*/.*/**/*`. Harness subtrees put them one level deeper —
`templates/harness/claude/.claude/settings.json` — and that pattern does not
reach it. `templates/*/*/.*/**/*` and friends were added.

The reason this was caught rather than shipped: `tests/test_packaging.py`
enumerates the template tree with `pathlib.rglob`, which **does** descend into
hidden directories (unlike `glob.glob` and unlike setuptools' own matching).
That asymmetry is what makes the test able to catch the packaging config's
blind spot. If that enumeration is ever "tidied" to use `glob`, the test starts
passing vacuously and the original 2026-08-05 bug becomes shippable again.

### 2026-08-17: OpenCode's plugin hook fires under `--auto`, and its deny list survives it

The 2026-08-10 entry established that Claude Code's `PreToolUse` hooks fire
independently of `--permission-mode bypassPermissions`, and that this was
*measured* rather than assumed. The same claim for OpenCode had to be measured
before `drives_sessions` could be turned on. Against **opencode 1.18.18**:

* **The plugin blocks.** `.opencode/plugins/nrw-guard.js` hooking
  `tool.execute.before` refused `nrw model generate spec.yaml --force` under
  `opencode run --auto`. That command is deliberately one the deny rules do
  *not* match, so only the plugin could have stopped it. The model received the
  guard's stderr and reported the `ESCALATIONS.md` instruction back.
* **The deny rules also block, independently.** With `--pure` (which skips
  external plugins), `nrw promote abc --as final` was still refused, by the
  permission rule alone.

So OpenCode has **three** independent mechanisms under `--auto` where Claude
Code has two: the plugin, the deny rules, and `NRW_AGENT=1`. `--auto`
auto-approves only what is *not* explicitly denied, whereas
`bypassPermissions` drops the deny list entirely. This is the one place the
OpenCode path is stronger than the Claude Code path.

The whole-line guard earned its keep across harnesses: an unattended session
ran `nrw promote ... 2>&1; echo "EXIT_CODE=$?"`, and the 2026-08-10 raw-string
segmentation caught it. A guard that tokenised first would have seen `nrw`,
`promote` wrapped in redirection and a second statement, and a JavaScript
reimplementation in the plugin would not have had any of that logic. The plugin
therefore contains **no refusal logic at all** -- it shells out to
`nrw agent guard --command`.

**`opencode --pure` skips external plugins, and so skips the guard.** A site
wrapping `opencode` in its own script must not add that flag.

### 2026-08-17: OpenCode has no turn cap, so `--turns` is a lie there

`opencode run` (1.18.18) has no `--max-turns` equivalent -- checked against
`opencode run --help`, not inferred. An OpenCode session is bounded by the
wall clock and nothing else.

Rather than let `--turns` silently do nothing, `nrw agent run --harness
opencode` **requires `--timeout`** and prints that the turn cap does not apply.
The alternative -- accepting `--turns 200` and ignoring it -- is the exact
shape of a limit that is not one, which is the failure this package is arranged
against.

Two smaller facts from the same session, both measured:

* **The prompt goes on stdin.** `opencode run` takes its message positionally;
  a composed session prompt is far past what belongs in argv. Piping it works.
* **`--model` wants `provider/model`**, e.g. `anthropic/claude-sonnet-4-5`.

The event stream under `--format json` is one JSON object per line with a
`part` envelope: a tool call is `{"type":"tool_use","part":{"type":"tool",
"tool":"bash","state":{"input":{"command":...}}}}`. There is no single terminal
`result` event as Claude Code has -- the reply is the last `text` part and cost
accumulates over `step-finish` parts (which report `0` on a free model, so zero
is real rather than missing).

### 2026-08-17: OpenCode reads both singular and plural config directories

`.opencode/agents/` and `.opencode/agent/` both load, likewise `plugins/` and
`plugin/` -- probed directly by dropping a marker file in each and watching
`opencode agent list --print-logs`. Plural is the current convention and is what
`nrw init` writes; singular is kept upstream for backwards compatibility.

Worth knowing because getting it wrong is silent: dispatchers in an unscanned
directory produce an assistant that behaves as though the project shipped no
skills at all. `opencode agent list` is the cheap way to check -- it prints each
discovered agent and whether it resolved as `(primary)` or `(subagent)`, which
also verifies the `mode: subagent` frontmatter took.

### 2026-08-17: Jinja drops the trailing newline, so every rendered scaffold file lacked one

`Template(...).render()` strips the final newline unless `keep_trailing_newline=True`.
Every `.j2` file `nrw init` writes — `CLAUDE.md`, `README.md`, `nrw.toml`,
`sample.md`, `sample.yaml` — had been shipping without one since the beginning.

Invisible in normal use, and that is why it lasted. It shows up as git's
`\ No newline at end of file` on every diff of those files, and a scaffolded
project that runs the same `end-of-file-fixer` pre-commit hook this repo uses
would rewrite all of them on its first commit — a diff the scientist did not
make and cannot explain.

It only surfaced because `.vscode/extensions.json` became a template (to list
each harness's editor extension). That file *had* a trailing newline while it
was copied byte-for-byte, and lost it on being rendered — so a change that
touched one file exposed a defect in five others.

Fixed at the one call site in `project/render.py`, with
`tests/test_init.py::test_every_scaffolded_text_file_ends_with_a_newline`
covering the whole scaffold rather than the file that happened to reveal it.

The generalisable form, and a variant of this file's recurring signature:
**converting a static file to a template changes it in ways the template does
not mention.** Byte-comparing the rendered output against the previous static
copy is the check; it took ten seconds and found something months old.

### 2026-09-13: pinned aure at v1.0.0, and the PyPI name is not ours to take

Bumped from `3021fee` (2026-08-05) to `9ec300d` (**v1.0.0**), 55 commits. The
whole `aure_adapter.REQUIRED` contract still resolves — all six
`feature_tools` functions, both `data_tools` functions, and
`nodes.evaluation.analyze_fit_quality_with_llm` — as do `llm.config` and
`llm.providers.get_llm`. The full suite passed on the new pin with no code
changes, only stale prose to fix. `aure evaluate` was removed upstream in
1.0.0; we never used the CLI, only the function, so it cost nothing.

**The PyPI name `aure` is taken by an unrelated project** — "util for auto
reload failed shell command", v0.0.2, `shidenggui/aure`. So `pyproject.toml`'s
note that nr-workbench is install-from-git "until aure publishes" described a
future that cannot arrive under that name. The direct reference is permanent
unless upstream renames, and `test_wheel_declares_only_the_expected_direct_references`
is guarding a property, not a temporary state.

### 2026-09-13: an aure setup file does not fully record its own run

`MODE_ENUMERATION` — the thin-layer SLD basin search that decides whether a
thin layer is found at all — has **no setup-YAML key and no CLI flag**. It is
read from the environment, and so are `THIN_LAYER_MODE_K`,
`THIN_LAYER_MODE_SEEDS`, `ROUGHNESS_MAX_OUTER`, `FINAL_SELECTION_TOL`,
`FINAL_TIER_CHI2_FACTOR` and `USE_RUN_TITLE`. AuRE's own `docs/launching.md`
states the consequence: *"a setup file therefore does not fully record the
physics policy its run used."*

Two runs from a byte-identical `setup.yaml` can therefore produce different
models with nothing on disk explaining the difference — which is the exact
failure this package exists to prevent. So `nrw aure run` sets these
explicitly in the child environment and writes them beside the setup as
`run-env.json`, rather than inheriting whatever the shell happened to hold.

Mitigation, not a fix: a knob added upstream that we do not know about goes
unrecorded, and nothing fails. Worth re-reading `docs/launching.md` §"Where the
run controls come from" on each pin bump.

### 2026-09-13: "through the substrate" does not match "through the silicon substrate"

`commands/model.py::_BACK_REFLECTION_HINTS` was a tuple of literal substrings,
and the geometry test was `any(h in lowered for h in HINTS)`. The most natural
sentence a scientist writes — *"measured through the silicon substrate"*, which
names the material — contains none of them. It was found by writing a test
fixture in the phrasing the reference notes actually use, not by reading the
list.

It never surfaced because the consequence is silent twice over. The hint list
only gated a critical-edge *correction* in the `--from-notes` prompt, so a miss
produced a worse proposed stack rather than an error; and back reflection
itself fits, converges, and reports a chi-squared in the hundreds with nothing
naming the cause.

Now `aure_setup.BACK_REFLECTION_PATTERNS`, one copy shared with `model.py`, as
regexes: an optional material word before "substrate", the substrate materials
by name, "through the wafer", and "from the substrate side". The materials are
enumerated rather than matched as any word — `through the \w+` would make
"the solvent diffuses through the polymer film" a claim about geometry.

The generalisable form: **a substring list is a guess about phrasing, and the
phrasing it misses is usually the more specific one** — because the writer who
adds detail adds it in the middle.

### 2026-09-13: a model spec needs a state, so an imported model needs the files

`ModelSpec` rejects a document with neither a state nor a series ("a model needs
at least one state or series"). So `aure_import.to_spec` cannot translate an
AuRE run on its own: AuRE reports layers, and a spec additionally needs to know
which files those layers were fitted to and at what incident angles — which
AuRE does not report back in a reusable form.

The states therefore have to be rebuilt from disk at import time, by the same
code path `nrw model new` uses. That is why `commands/model.py::state_for_run`
exists as a shared function rather than staying inline in `_scaffold_document`:
the angles come from each file's `# Meta:` header, and a second implementation
would be a second chance to fall back to the nominal 0.45/1.2/3.5 instead of the
measured 0.4500/1.2010/3.5003.

Found by writing the end-to-end test first — `to_spec` had a documented
`states: ... | None = None` parameter and a docstring claiming a stateless spec
"still validates". It does not, and nothing else would have said so.

### 2026-09-13: an aure setup file can choose the endpoint your key is sent to

`aure.setup._KNOWN_TOP_LEVEL` accepts `llm_provider`, `llm_model`,
`llm_api_key`, `llm_base_url`, `llm_temperature` and `llm_timeout`, and
`aure analyze` applies them as environment overrides for the duration of the
run. `aure.llm.config` then resolves the **api key from the ambient
environment** while taking the **base URL from the file** — so a setup that
sets only `llm_base_url` sends the caller's own `LLM_API_KEY` to whatever host
it names, and nothing in the banner says so.

That matters here because a setup is a *tracked, shareable* file by design: it
is committed, and beamtime directories get handed to collaborators and
archived. `nrw aure run` therefore refuses any setup that sets one of those
keys, names it, and says to rotate the key if the file came from somebody else.
The endpoint comes from `.env`, which is gitignored precisely so it cannot
travel with the project.

### 2026-09-13: a stub placed one level too high tests nothing

The first `nrw aure run` tests stubbed `commands.aure_cmd._invoke` and asserted
on the `overrides` dict handed to it. Every test passed with the environment
merge written **backwards** — `{**overrides, **os.environ}` — which lets any
knob the scientist exported in `.bashrc` beat the value recorded in
`run-env.json`, silently producing a different model from the one the record
claims.

Lowering the stub to `subprocess.run` and asserting on the environment the
child would actually receive catches it; verified by making the mutation and
watching the test fail. The general form: **stub at the process or network
boundary, not at the last function before it** — a stub above the code that
does the work makes that code untestable while the suite still looks green.

### 2026-09-13: AuRE's `interfaces` block silently invalidates a positional roughness map

`ModelDefinition.layers[i].roughness` describes that layer's boundary with
whatever sits **above it in the refl1d stack**, so which physical interface it
means depends on the geometry — upstream documents this and says it must not be
"corrected", because it is the convention the reference fits were built with.

`ModelDefinition.interfaces` is the escape hatch: it names a boundary by the two
materials it separates, is geometry-independent, and **overrides** the
positional map. So a translation that reproduces the positional mapping (as
`aure_import.ordered_stack` does) is only correct while `interfaces` is empty.
Honouring the positions anyway would move every buried interface one place, and
the resulting spec would validate, generate and fit.

`nrw aure import` therefore refuses a run that used `interfaces` and points at
`nrw model new --print-prompt` instead. Refusing is right here: the failure is
invisible, and a partly-correct stack is worse than no stack.

### 2026-09-13: `$` is not end-of-string, and it reached a code generator

`spec/models.py::_NAME_RE` was `^[A-Za-z_][\w-]*$`. In Python `$` also matches
*before a trailing newline*, so `"Cu\n"` passed validation — and
`codegen/generator.py` interpolated the name into Python source as
`SLD("{name}", ...)`, emitting an unterminated string literal. The `SyntaxError`
surfaces at `nrw fit run`, two commands later, blamed on a name nobody typed.

Harmless while every layer name was typed by a person. `nrw aure import` makes
layer names **language-model output**, so the margin became load-bearing.
Fixed twice over: `\Z` instead of `$`, and `repr` instead of interpolation, so
the generator is correct whatever the regex happens to allow. The lesson is the
second fix: **do not let a validator's strictness be the only thing keeping a
code generator safe** — quote properly and the regex becomes defence in depth
rather than the defence.

---

### 2026-09-15: the harness can be the endpoint, and that does not reopen the gate

The [2026-08-10 entry](#2026-08-10-a-second-weaker-model-is-not-a-fallback)
records why `nrw assess`, `nrw model new --from-notes` and `nrw isaac export`
stand down under `NRW_AGENT=1`: sending the question to a second, weaker model
and handing the answer back replaces the judgement we wanted with a worse one,
which the harness then reads as evidence.

AuRE now has a `claude_code` provider — its endpoint driven by `claude -p` as
a subprocess — so the endpoint can be the *same* model as the harness. That
looks like it invalidates the entry. It does not, and the reason is worth
writing down because the original wording made model strength carry the whole
argument when it was only ever carrying half.

The two halves:

1. **The second model was weaker.** `claude_code` answers this one.
2. **The second model has no context.** A harness verdict is formed with the
   files it has read, the fits it has run and the hypotheses it has already
   ruled out. A fresh `claude -p` starts from an empty session and gets only
   what the prompt template happens to include. Its answer then arrives back
   inside the harness's context as evidence — produced with strictly less to
   go on, and costing a second inference to produce.

Half 2 is untouched, so the gate stays. The generalisable form is the sharper
version of the original: **what makes the caller's judgement better is not only
which model it is, it is what that model has already seen.** A tool that hands
work back to its caller is not choosing a bigger model; it is choosing the one
holding the context.

Where it does change something: `nrw aure run` drives AuRE's own state machine,
which consults a model at every node and refuses to start without an endpoint.
There is no harness in that loop to hand anything back to — the loop *is* the
consumer. Before `claude_code`, a person whose only model was Claude Code had
to configure a second, weaker one purely to satisfy that check, which is this
entry's own failure mode arriving through an install detail rather than a
design decision. Now `LLM_PROVIDER=claude_code` is enough, and every "no
endpoint configured" message says so when the installed AuRE is new enough to
have it (`aure_adapter.claude_code_supported`, checked on disk so the advice
cannot outrun the pin).

It is not free: ~12k input tokens of Claude Code preamble per call, ~$0.2–0.4
per analysed curve of overhead, and ~1s of process startup. A plain endpoint is
cheaper. This one is already configured, which for a beamtime is often the
property that matters.

### 2026-09-16: `UNTRACKED` was the wrong policy for exactly one template

A project scaffolded by one analyst was cloned by a second and arrived with
`.nrw/bin/nrw` and `.claude/settings.local.json` in it — both of which hold the
absolute path of `nrw` on the first analyst's machine, and both of whose own
header comments say they are gitignored and must not be committed.

The chain, every link of which was separately defensible:

1. The repository was created on GitHub, so it already had a Python
   `.gitignore`.
2. `classify()` therefore returned `UNTRACKED` for our own `.gitignore` — "on
   disk but absent from the lock, treat as the user's" — which writes nothing,
   writes no `.nrw-new` either, and reports `left alone 1 pre-existing file(s)`
   without naming the file.
3. The rules it withheld are precisely the ones that ignore the two
   machine-local files.
4. `_install_toolpath()` then wrote both. Nothing complained; both work
   perfectly on the machine that wrote them.

**The generalisable point: a scaffold that declines to install a file is making
a policy decision, and the decision is only safe if nothing else the scaffold
does depends on that file.** `UNTRACKED` is right for instruction files,
editor settings and READMEs, all of which are inert. It was wrong for
`.gitignore`, which is the only template that *enforces* something — provenance
rule 3 — for code that runs later in the same command.

So `.gitignore` is now planned with `PlannedFile.merge` set and installed as a
marked block (`nr_workbench.project.ignore`), leaving everything outside the
markers alone. A merged file deliberately does not consult the scaffold lock:
the file is partly the user's, so a whole-file hash cannot say whether *our*
part is current, whereas the markers travel with the file and survive a lock
loss or a fresh clone.

Three further things this turned up, each worth its own line:

**`git check-ignore` reports an already-tracked path as NOT ignored, and that
is correct.** Git applies no ignore rule to a path in the index. So adding the
rule to `.gitignore` — which the merge now does — leaves an already-committed
machine-local file exactly as committable as before, and advice that says only
"add it to .gitignore" looks broken to whoever follows it. Every warning here
has to offer `git rm --cached` when the path is tracked. This also means the
check is strictly better than intended: it catches "the rule exists but the
file was committed before it did".

**`install()` refreshes `NRW_BIN` but leaves an existing `PATH` alone, which is
right in isolation and wrong after a clone.** Not writing a `PATH` is a
deliberate choice (see `path_override` — harness settings do not interpolate,
so any value is a full override). But a *committed* `PATH` survives a clone
intact and silently wins, so the second analyst's sessions ran with the first
analyst's virtualenv at the front of `PATH`. On a shared filesystem that
resolves rather than failing, which is the worse outcome — there is no error to
notice. Hence `stale_path_entry()`: the fix stays additive, but the condition is
now reported instead of being silent.

**An unconditional `write_lock()` made every `nrw init` dirty a tracked file.**
The document carries an `updated` timestamp, so a run that changed nothing
still rewrote `.nrw/scaffold.lock.json`. One person discards the diff; two
sharing a project get a merge conflict on a file neither edited, every time
either runs `init`. The write is now conditional on an entry actually moving —
*and* on the lock on disk being parseable, because a corrupt lock loads as
empty and would otherwise compare equal to a no-op run and never be repaired.

Rule 3 also had nothing enforcing it, which is why this reached a shared
remote at all. `nrw check` now runs `check_committed_paths()`: tracked
machine-local files, plus any tracked file naming a home directory. It matches
home directories — `/home/<user>/` and the `/Users/` and `/SNS/users/` forms —
rather than absolute paths in general, because `/SNS/REF_L/IPTS-1234/...` is
where the data was and is true for everyone. Flagging that would make the
check noisy enough to be ignored. Files listed in the scaffold lock are
skipped: a path in one of those is this package's bug, not the project's, and
`refl1d-script-review` quotes one deliberately as its example of what not to
do.

---

### 2026-09-16: the suite read the developer's home directory, and was 18% of itself

Two findings from asking why 1,360 tests took 176s. The count turned out not to
be the problem — 21,047 test LOC against 39,327 source LOC is lean, the
parametrization factor is 1.23, and 70% of tests carry a docstring saying why
they exist. Nothing to delete.

**Where the time was.** 244 tests (18%) accounted for 92% of the runtime; the
other ~1,116 cost under 5ms each. **Setup was 62% of measured time**, not test
bodies, and five files were 123s of the 162s measured:

| file | setup | call |
|---|---|---|
| `test_isaac.py` | 22.0s | 6.9s |
| `test_web.py` | 18.3s | 10.5s |
| `test_lifecycle.py` | 16.4s | 5.0s |
| `test_pack.py` | 15.3s | 3.9s |
| `test_fit_e2e.py` | 2.2s | **22.4s** |

Fixture chains rebuild expensive artifacts per test — `test_lifecycle`'s
`two_fits` runs two real refl1d fits for each of the tests below it.
`test_fit_e2e` is the exception, doing its work in the test bodies.

A measurement that killed the obvious fix: caching the *scaffold* buys nothing.
`conftest`'s `project` costs 64ms, `nrw init` through the CLI 59ms, and a
`copytree` of a finished one 42ms. The 1.8s is what gets layered on top, so
caching has to target the fits, not the directory. **Averages hid this
completely** — "0.13s per test" reads as a uniformly slow suite and points at
restructuring everything; the distribution points at four fixtures.

**Fixed now: parallelism.** `-n auto --dist loadscope` in `addopts` takes the
suite to ~43s. `loadscope` is load-bearing — the expensive fixtures are
module-shaped, and splitting a module across workers rebuilds them per worker
and gives the time back. The cost is that a single small file goes from 0.85s
to 3.9s, because 20 workers each import the world; `-n0` is the escape hatch
and is also required for `-s` and `pdb`.

**Fixed now: isolation, which the above depended on.** Only `test_env.py`
stubbed `USER_ENV_PATH` and `AURE_ENV_PATH`. Every other test that reached
`load_env` — which is almost every command — read the developer's real
`~/.nrw` and `~/.aure`. Observed, not theorised: a real `~/.aure` carrying
`LLM_MODEL=gpt-5.4` changed what `nrw check-llm` reported mid-run. So a green
suite on one laptop said nothing about another, or about CI. An autouse fixture
in `conftest.py` now points both paths at an empty directory, clears
`KNOWN_VARS`, and resets the `_loaded` latch — which was itself a bug in
waiting, since whichever test called `load_env` first decided for every test
after it. `test_env.py` carries a guard that fails if the fixture is removed.

The generalisable form: **a suite that reads `$HOME` is not a suite, it is a
measurement of one machine** — and it cannot be parallelised with any
confidence, because the thing you would be trusting was never isolated to
begin with.

**Still open** (measured, not done): the four module-shaped fixtures should
cache their built artifacts per module, and `test_fit_e2e` should either share
one fit or carry the `slow` marker — which only 2 tests use today, so
`-m "not slow"` buys nothing.

### 2026-09-21: the one-liner installer, and what it had to work around

`curl -fsSL https://raw.githubusercontent.com/neutrons-ai/aure-workbench/main/install.sh | sh` (and
`irm .../install.ps1 | iex` on Windows) is now the documented way in. Four
things about it are not derivable from the scripts.

**uv accepts our transitive direct reference; pip-style fears were unfounded.**
uv's documented rule is that a URL dependency reached from a *registry* package
is rejected ("URL dependencies must be expressed as direct requirements or
constraints"), which would have killed this outright, since every install of
nr-workbench drags `aure @ git+https://...@SHA` behind it. Measured: because
nr-workbench is itself a direct URL requirement, `uv tool install
"nr-workbench @ git+<repo>"` resolves the AuRE pin without complaint and
`nrw doctor` reports the right commit. 9.5 s from cold on a laptop. There is
therefore no need for the venv-plus-pip fallback that was planned.

**uv, not pipx, because of the Python floor.** We require >= 3.11 and stock
macOS ships 3.9.6, so any pipx or `python -m venv` route begins with "first
obtain a Python", which is the step that actually stops a beamline scientist.
uv is a single dependency-free binary that downloads its own CPython, so the
one-liner works on a machine with no usable Python at all.

**A TLS-inspecting proxy breaks uv specifically, and silently.** uv ships its
own certificate bundle rather than using the system trust store, so on a
network that re-signs TLS — ORNL, most campuses, this sandbox — it fails with
`invalid peer certificate: UnknownIssuer` while `curl` and `git` on the same
machine are fine. The fix is `UV_SYSTEM_CERTS=1` (older uv: `UV_NATIVE_TLS=1`,
now deprecated but still worth setting, since the user may have an older uv).
The installer detects the certificate error and retries automatically; without
that, the error names nothing a user could act on. This is the single most
likely reason an install fails at a lab, and it is invisible in CI.

**`--force` alone does not upgrade.** `uv tool install --force` overwrites the
executables but can still satisfy `@main` from uv's cached checkout of that
ref, so re-running the installer would report success and install yesterday's
commit. The scripts pass `--reinstall-package nr-workbench`, which implies
`--refresh-package` for just that package and leaves the heavy scientific
dependencies cached.

**Windows installs, but nr-workbench is not Windows-ready.** `install.ps1` is
a faithful port and puts a working `nrw.exe` on PATH, but the runtime still has
POSIX assumptions: `nrw init` writes a `#!/bin/sh` shim, `toolpath.py`'s PATH
probe shells out to `/bin/sh -lc` and treats the resulting `OSError` as a pass,
`agent/session.py` uses `os.killpg`, and `importer.py` links with
`symlink_to`. CI is ubuntu and macOS only. `docs/install.md` says so plainly
rather than letting a user discover it; fixing the runtime needs a
`windows-latest` job, or it will regress as fast as it is fixed.

**Three bugs the reviewers found that a shell script hides well.** Worth
recording because each is a shape, not a typo. (1) `{ if cmd; then echo 0;
else echo 1; fi >"$RC"; } | tee` puts the redirect on the whole `if`, so the
command's *stdout* lands in the status file ahead of the marker and a
successful install reads as a failure — latent only because uv writes progress
to stderr. Redirect the `echo`, not the compound. (2) The "is `nrw` on your
PATH?" check was asked of the PATH the script had just prepended to, so the
branch that runs `uv tool update-shell` was unreachable in exactly the
fresh-machine case it exists for. This is the trap `toolpath.py:151-164`
already documents for `resolvable_in_fresh_shell`, rediscovered in another
language: snapshot the caller's PATH first. (3) Windows PowerShell 5.1 wraps a
native command's captured stderr in a `NativeCommandError`, and
`$ErrorActionPreference = 'Stop'` promotes it to terminating — uv writes all
its progress to stderr, so `install.ps1` would have aborted on uv's first
normal line. Relax the preference around the native call and check exit codes.
PowerShell 7.1+ does not do this, so a pwsh test would never have caught it.

**The installer validates its own environment variables.** `NRW_EXTRAS` and
friends are interpolated into a PEP 508 requirement, and `]` plus `@ git+...#`
rewrites which repository `uv tool install` builds — arbitrary code execution
from one environment variable on a shared machine, while the banner still
named the right repo. They are now character-checked, the requirement actually
being installed is what gets printed, and any credentials in the URL are
stripped from the output.

Two mechanical notes. The one-liner fetches the script straight from
`raw.githubusercontent.com`, so the file a reader inspects in the repository
and the file `curl` executes are literally the same object -- a GitHub Pages
site was built first and then dropped, because it added a deployment workflow
and a second place for the URL to go stale without making the command any
shorter. And both scripts take their configuration from environment variables,
not arguments, because neither `curl | sh` nor `irm | iex` can pass any —
`NRW_INSTALL_DRY_RUN=1` is what lets `tests/test_install_scripts.py` run the
real script in CI without installing anything.

### 2026-09-21: the setup guardrail is a skill plus better refusals, not a command

A request arrives — "fit my data" — before the machinery exists: no project in
this directory, or a sample named something that is not on disk. The danger is
not the error. It is that improvising around it **looks like progress**: a
`samples/` directory made with `mkdir` that `nrw scan` does not recognise, data
copied somewhere plausible, a refl1d script written by hand whose result
carries no provenance. The tool cannot tell that apart from real work.

The obvious fix was a `nrw preflight <task>` command emitting a machine-
readable state plus the question to ask. It was **not** taken, and the reason
generalises: a fourth command that reports on the project would overlap
`doctor` (is the environment sound?) and `handoff` (where did the last session
leave off?) without owning a distinct question, and every harness would still
need prose telling it to run the thing. What was missing was never a *state
reader*; it was that the state readers already there gave answers an agent
could not act on.

So: `skills/reflectometry/nrw-preflight` carries the contract (establish the
project, then the sample, ask before creating either), and it is first in
`SEED_SKILLS` because it applies before the agent knows anything else —
including whether there is a project. `AGENTS.md` names it above the handoff,
because a skill nobody is told to read is inert: none of the three assistants
auto-discovers `skills/`. `tests/test_preflight.py` asserts that ordering.

**The refusal has to name the samples that exist.** This is the whole of the
in-command half. Nine call sites phrased "no such sample" nine ways, and not
one listed the alternatives, so `No sample 'S9'` was a dead end — an agent
could not tell a typo from a sample that genuinely needs creating, and would
guess. `ProjectLayout.missing_sample_message` now answers all three questions
at once (what is missing, what is there, what to run), and every call site uses
it. The pattern is worth reusing: **an error an agent will act on must carry
the alternatives, not just the fact of the failure.**

Two things deliberately left out. Data and model readiness are not gated —
`nrw data check` and `nrw model` already say enough, and gating them means
guessing at intent. And nothing hard-blocks: a `PreToolUse` hook would cover
Claude Code only, while the refusals cover every harness, including a
scientist typing the command themselves.

### 2026-09-25: the experiment catalog owns sample.md, so the lock records an owner

The Experiment page renders `samples/<id>/sample.md` from `experiment/*.parquet`,
through the scaffold's three-way rule. That opened a reset trap in
`classify()`: once the catalog has written the file, the lock records *its*
content as "what nrw installed", so a later `nrw sample new <id>` (or
`nrw init --sample <id>`), which plans the **blank** template, sees an
unedited file with a newer template and returns UPGRADE. The file is reset
to the blank scaffold, silently, since nothing about it is an error.

Closed twice, because one mechanism is not two:

- `experiment.render.plan_sample` is the only way a sample's files are planned,
  and it consults the catalog.
- A lock entry now carries an optional `owner`, and a plan from a *different*
  owner is DRIFTED, never UPGRADE. That also covers a fourth caller nobody
  has written yet.

`plan_sample` also refuses when the lock says the catalog wrote a file the
catalog no longer lists. `nrw experiment release <id>` drops the entry so the
file becomes its people's (UNTRACKED) instead.

### 2026-09-25: settled is not complete

`nrw agent watch` calls a run ready once its files have been unchanged for 300
seconds. The reference corpus shows why that is not "finished". Run 218386's
three segments were reduced at 09:45, 10:00 and 10:53 (its `# Reduction time:`
lines). The files sat unchanged for 15 minutes after segment 1 and for 52
minutes after segment 2, so a five-minute settle calls the run finished twice
before it is. A copy made at either point is part of a measurement, and it
fits.

The Experiment page therefore requires evidence the run ended before it copies
anything (`experiment/status.py`), and one kind counts: **a later run has
reduced files**. A later run that a feed merely *announces* does not count,
because acquisition starting is not reduction finishing. A settled run without
that evidence is *unconfirmed*, and only a person can confirm it; the last run
of a beamtime always needs one.

**The header's planned segment count only vetoes.** The `new_reduction`
header's per-segment arrays (`DB`, `scale_factor`, `ThetaShift`) appear to be
sized by the reduction template. When they say three and two segments are
present, the run is unconfirmed whatever else is true. A *fulfilled* plan was
first accepted as proof as well, and review took that back: it rests on the
arrays being full length already in segment 1's file, which no real
first-segment file has confirmed. The trimmed fixture from run 234277 suggests
so. If they grow with each segment instead, segment 1's header says "1 of 1",
and a five-minute-old third of a measurement is "complete". Once a real
first-segment file shows the arrays are full length, the plan can prove
completion too, and the last run of a beamtime will no longer need a person.

Copies are written with a fresh mtime, not the source's (`copy2` would keep
it). A copy dated an hour ago looks settled to `nrw agent watch` the moment
it lands.

### 2026-09-25: one header, two clocks, and the new dialect has neither

A `_partial.txt` header's `Run start time` is UTC. Its `Reduction time` is
local: 218386 says it started at 13:42 and was reduced at 09:45 the same
morning. The tNR sidecar's interval times are local too. Sorting runs by time
put 218389 before 218386.

The `new_reduction` header carries no start time at all. So the catalog stores
whatever start time a header gives verbatim, invents none, never compares times
across sources, and orders runs by run number.

### 2026-09-25: a binary merge conflict has no conflict markers

The catalog is parquet, the lakehouse's format. When two branches both change
it, git cannot merge it: it leaves "ours" in the working tree with no markers,
the file loads cleanly, and the next save commits the other side's loss. The
store refuses to load or save while `git ls-files -u -- experiment/` is
non-empty.

A related trap in the same place: pyarrow's `ArrowInvalid` is a `ValueError`,
so the house pattern `except (OSError, ValueError): return {}` would read a
truncated catalog as an *empty* one, and the next save would replace every
decision in it. The store's errors deliberately do not subclass `ValueError`,
and an unreadable catalog is shown read-only, never empty.

The manifest (`catalog.json`) is written after both tables, so a crash between
the two replacements is detected. The last complete pair, kept in
`.nrw/cache/experiment/`, is restored only when the manifest describes it
exactly.

### 2026-09-25: an analysis node's loopback is shared

The web server had no authentication because it only read and bound to
127.0.0.1. The Experiment page writes. On an SNS analysis node, loopback is
reachable by every logged-in account, so a token embedded in the page protects
nothing: another user can fetch the page and read it.

Writes therefore need a secret that is on no page. `nrw serve` prints a one-time
link that sets an HttpOnly, SameSite=Strict cookie. On top of that, every write
needs all of:

- the page's own token;
- a JSON body (a `text/plain` POST needs no CORS preflight);
- an `Origin` matching this server including its port (every localhost app is
  "same-site");
- a loopback peer.

The `Host` header is checked on *every* request, because DNS rebinding makes a
hostile page same-origin. Writes are disabled outright when the server is bound
elsewhere or runs under `NRW_AGENT`.

Two details learned on the way:

- **Flask resolves error handlers by status code at every level before any
  class-based handler**, so the app's HTML 403 page answered the API blueprint's
  refusals until the blueprint registered its own handler per status code.
- **`</` was not the only way out of a script block.** `<!--<script>` puts the
  HTML tokeniser into a state where the block's own `</script>` no longer
  closes it. The embedded JSON now escapes `<`, `>` and `&` as `\u` sequences.

### 2026-09-25: an empty Condition cell was read as the condition

`conditions.from_table` fell back to "the last non-empty cell" whenever the
Condition cell was empty, so `| 218393 | full Q |  |` gave the condition
`full Q`, and that is what the ISAAC record said. A Condition column is now
authoritative even when empty. This bit hand-written tables too, not only
rendered ones.

Related, for the renderer:

- **Catalog context is human prose, not generated text.** It must never be
  fenced `<!-- nrw:generated -->`: `notes.human_text` drops those regions, and
  every reader asking "did a person write this?" would discount the
  scientist's own words.
- **Run titles are never copied into the measurement table.** The title
  beside the condition is what lets `nrw data reconcile` catch 218393, a run
  whose own title says CA while the table said OCV.

### 2026-09-25: three defects only a browser found

The server-side tests were green; driving the page in headless Chrome over the
DevTools protocol found three things they could not:

- **A plan reviewed in one second was "changed" in the next.** The plan digest
  hashed every scaffold file's bytes, and a new sample's `sample.yaml` is
  stamped with the current second. The unit tests never crossed a second
  boundary *and* pinned `created` in their render context, which the real
  page does not. The digest now covers `sample.md`'s bytes and only the names
  of the other files. The regression test builds its context the way the page
  does, and fails without the fix.
- **Plotly's `scattergl` needs `'unsafe-eval'`.** regl compiles shaders at run
  time. Without it the quick-look plot fails silently under the page's CSP.
- **`replaceChildren(null)` renders the text "null".** Optional children now go
  through the page's `el()` helper, or are filtered out.

### 2026-09-25: where the experiment's data is, and what else could say so

Recorded from the user, not derivable from the code:

- **The location is provisional.** The default data location,
  `/SNS/REF_L/{ipts}/shared/autoreduce/new_reduction`, is where REF_L's
  `new_reduction` pipeline writes today, and is expected to move. It is one
  constant (`project.config.DEFAULT_EXPERIMENT_LOCATION`), rendered into the
  scaffolded `nrw.toml` rather than copied.
- **The IPTS is used verbatim.** Its digits are kept as written: `int()` would
  turn IPTS-00001 into IPTS-1, a different directory.
- **Unsliced tNR arrives as an ordinary run.** The unsliced tNR run lands in
  the same folder as an ordinary reduced file. Sliced series come later.
- **New runs can be announced by other feeds.** Besides files appearing, the
  SNS web monitor (`monitor.sns.gov`, whose run lists need an ORNL login) and
  Tiled can announce runs. The run feed is therefore its own seam, apart from
  data access.
- **ONCat is out of scope.** The facility catalog would add a dependency for
  information the files and the feed already carry.

### 2026-09-25: a lock that failed to load must not be written over

`load_lock` reads an unreadable `.nrw/scaffold.lock.json` as empty. That is
safe for a full `nrw init`, where every file then reads as UNTRACKED and is
left alone. It is not safe for a *partial* plan: `nrw sample new`, apply and
adopt each plan one sample's files, and writing their lock over one that failed
to load keeps their entries and permanently drops every other. The lock is
tracked and shared, so the likely cause is a git conflict.

`apply_scaffold` now refuses to write over a lock it could not read unless the
caller passes `rebuild_lock=True`. Only `nrw init`, which plans the whole
project, does. Conflict markers are refused even then, because the right merge
is a person's. Adopt checks the lock *before* it updates the catalog, since
refusing afterwards would leave a sample adopted in the catalog and its
`sample.md` not rewritten.

### 2026-09-25: the one-time link works once, and reads have a deadline

Two review findings about the web server's new write surface:

- **The link's secret is exactly the kind of value that leaks.** It is printed
  to a terminal and travels in a URL, so copies end up in scrollback, shell
  history and chat. Redeeming it now mints a fresh, unrelated session value for
  that browser's cookie, and the link is dead after one use. A leaked copy
  opens nothing, and a second browser needs a restarted server.
- **Reading a file on a dead NFS mount does not fail, it blocks.** Listing
  already happened only on the background poller. But apply and its review
  also read file bytes on the request thread, to copy them and to tell a
  re-reduced source from a touched one. Every such read now goes through a
  two-worker pool with a 15-second deadline (`web/experiment.py`,
  `SOURCE_TIMEOUT`). A read that times out fails that file the way any other
  read failure does, and apply releases its lock. The source is wrapped once,
  where the page gets it, so no call site has to remember the rule.

### 2026-09-25: four readers of the measurement table disagreed about its columns

ISAAC's conditions, `nrw data reconcile`, adoption into the catalog, and the
catalog's check against a second table in the prose each decided for themselves
which column is the run and which the condition. `| Run | Type | Conditions |`
gave ISAAC a condition and gave adoption an empty one. A guard that recognizes a
narrower table shape than a reader does is also a way in: prose the guard
accepts would be read as measurements. The shared vocabulary (`RUN_HEADERS`,
`CONDITION_HEADERS`, `table_cells`) now lives in `nr_workbench/sample_md.py`,
and all four use it.

### 2026-09-25: two refactors the design review asked for, and why they wait

The review's structural findings were fixed in place:

- `nr_workbench/arrival.py` holds what `nrw agent watch` and the Experiment page
  share, so the experiment's data layer no longer imports the agent.
- `fsutil.py` has the one advisory lock and the one atomic write. The shared
  scaffold lock is now written through a unique temp file. A single `.tmp`
  name let a request thread and a terminal rename each other's half-written
  lock.
- One builder for a run's row and a sample's card (`experiment/views.py`).
- The copy record is typed (`CopyEntry`), and keeps keys it does not know.
- The API maps only the errors nrw raises on purpose. Anything else is a
  logged 500, not a 400 blamed on the caller.

Two refactors were deliberately left for later:

- **Splitting `experiment/apply.py` (about 1,100 lines) into a package**
  (plan, copy, move, record). The seams are visible: `_plan_run`,
  `_copy_runs`, `_move`, `read_sources`/`write_sources`. But every piece
  shares the `FileAction` vocabulary and the same safety argument. Splitting
  it before the tNR series copy arrives would pick the boundaries twice.
- **A service layer between the surfaces and the experiment package.** The
  command line and `web/experiment.py` each still wire up the workspace, the
  poller and the catalog. With two surfaces, that is two short call sequences,
  and the error-mapping differences between them are real (a script's exit
  code versus a status code). A third surface, such as an agent tool, is the
  point where a shared service pays for itself.

### 2026-09-25: one `.dat` per angle, and what the web monitor will add

From the user, while planning the Settings page:

- **Each angle's `_autoreduction.dat` is written once that angle has been
  measured.** A file is never a partly acquired angle, but a run's segments
  still arrive one at a time. So "all planned segments present and settled" is
  not proof on its own, and completeness still waits for a later *reduced* run
  or a person (`experiment/status.py`).
- **The SNS web monitor reports the run being measured now.** That is the
  evidence a monitor feed will bring. "Measuring run M" proves every segment of
  an earlier run with a subrun below M has been *acquired*. Once those are
  reduced and settled, the run is complete, without waiting for the next run's
  reduction. The last run of a beamtime can then complete without a person
  too, because the monitor then reports that nothing is being measured.
  `docs/experiment-sources.md` words the rule; the feed waits for the
  monitor's URL and login.
- **A measurement is N segments plus other artifacts**, such as the combined
  curve stitching them. A fit co-refines the segments. The combined curve is
  fitted only when a run has no segments, which is what `nrw model new`
  already did. The rule now lives once, in `instrument/reduced.fitting_names`,
  and apply copies exactly those files. Before this, apply copied every file
  it listed, a combined curve included.

### 2026-09-25: nrw.toml must render back exactly what nrw writes into it

`nrw init` re-renders `nrw.toml` on every run, and any file that differs from
both the render and the lock is DRIFTED, which leaves `nrw.toml.nrw-new` beside
it on every later `init`. `[beamtime]` was rendered from the existing file, so
editing the IPTS by hand was fine. Two things nrw itself told people to do were
not:

- `nrw audience --ask` edited `[audience]`, which the template hard-coded.
- The template's own comment said to uncomment the `[experiment]` block.

Now everything nrw writes into `nrw.toml` renders from what the file already
says. That covers `[audience]` and the experiment block, whose one renderer
(`settings.experiment_block`) the Settings page shares. So `nrw init --check`
is clean after any save. Every string goes through one TOML encoder: a folder
name or beamtime label containing `"` or `\` used to make the file unreadable,
and every `nrw` command with it.

A save also refreshes the scaffold lock when the result is exactly the render
(`scaffold.record_installed`). Without that, the lock still holds the old
bytes. `init --check` passes, since the file matches the render, but the first
template change after an nrw upgrade finds the file "edited" and leaves a
`.nrw-new` instead of upgrading it.

### 2026-09-25: an empty active table is a trap, so the experiment block stays commented until set

The first design rendered `[experiment.source]` and `[experiment.feed]` as
active tables with every key a commented placeholder. The review caught what
that breaks: TOML allows each table once, and the docs, four test fixtures,
and anyone following older advice *append* an `[experiment.source]` table.
Against an active one, that stops every `nrw` command. So a table stays
commented out until one of its settings is set, and `load_config` names the fix
when a file declares a table twice. The docs now say to edit the existing
table.

### 2026-09-25: the poller that came back to life, and the source that outlived it

Found reviewing the reload design, both in code already committed:

- **`LiveInventory.stop()` did not stay stopped.** `touch()` restarted the
  thread whenever it was not alive, so any request still holding a replaced
  inventory brought it back. It would then poll the *old* folder for up to ten
  minutes. `close()` is permanent.
- **`source` and `live` were cached separately.** Each read the workspace
  outside the lock and cached under it, so a reload between the two could pair
  the new source with the old poller. They are now one frozen `_Wiring`, rebuilt
  whenever `nrw.toml` changes and taken whole by each request.

### 2026-09-25: a folder check reads five headers, not all of them

`LocalDirectorySource.inventory()` reads the head of every file, up to 64 KiB
each, to learn titles, angles and the IPTS. Over NFS on a real beamtime folder
that is tens of megabytes. The poller does it once and caches it by file
version; a button must not. `probe()` lists once and reads the newest five
runs' headers, which is enough to show what a folder holds and to notice one
that belongs to another experiment. The check runs on a daemon thread with at
most two in flight: a pool's workers are joined when the interpreter exits, so
one stuck on a dead mount would make stopping `nrw serve` hang too.

### 2026-09-25: every writer of the scaffold lock holds one lock

`.nrw/scaffold.lock.json` is read, changed and written by `nrw init`,
`nrw sample new`, experiment apply and adopt, `nrw experiment release`, and now
a settings save. Two of those run on `nrw serve`'s request threads while a
terminal may run the others. Two writers that each read the same lock write
back only their own entries. `scaffold.writing_scaffold` serializes them: a
process lock plus an advisory lock in `.nrw/cache/`. It is re-entrant within a
thread, because a second `flock` from the same thread would wait for itself.
Dry runs take nothing, since `nrw init --check` must write nothing, not even a
lock file.

### 2026-09-25: a save landing between `nrw init`'s plan and its write was put back

`nrw init` renders its plan from the files as they are, then writes it. A
Settings save in between recorded `nrw.toml` as nrw's own, since it was the
render with the new value. Init's plan, rendered before the save, then read as
an UPGRADE and wrote the old values back, and nothing reported it. Now `nrw
init` holds `scaffold.writing_scaffold` from planning to writing. Every writer
of `nrw.toml` goes through `project/nrwtoml.write_as_nrw`, which holds the same
lock from reading the file to recording what it wrote.

Recording is the other half. A save marks the written bytes as nrw's only if
the file was nrw's before (UNCHANGED or UPGRADE). Marking a hand-edited file as
nrw's would make the next `nrw init` read it as untouched and upgrade it over
the person's edits.

### 2026-09-25: `True == 1`, so a proof by `==` proves too little

`tomlfile.verify` proves an edit by parsing both versions and comparing them.
In Python `True == 1` and `600 == 600.0`, so with `==` a setting that turned
from a number into a flag, or from one number type into another, passed the
proof. `verify` compares types as well. The only difference it accepts is the
one nrw makes itself: a whole float is written as an integer.

### 2026-09-25: a thread pool made `nrw serve` hang on exit

This corrects "the one-time link works once, and reads have a deadline" above.
The reads' two-worker pool had the flaw the folder check was built to avoid.
`concurrent.futures` joins its workers when the interpreter exits, so one read
stuck on a dead mount made stopping `nrw serve` hang: the deadline freed the
request, not the process. Every bounded call now goes through
`nr_workbench.bounded.Bounded`, which gives each call:

- a daemon thread;
- a deadline;
- a fixed number of slots, and a call that finds them all taken is refused at
  once rather than queued behind the mount.

Reads get four slots, apply two of its own, and checks two. Each configuration
of `nrw.toml` gets its
own read slots, so reads stuck on the old folder's dead mount do not block the
new folder too. `SOURCE_TIMEOUT` (15 s) lives in `experiment/sources`, shared by
the page, the folder check and `nrw experiment settings --check`. The CLI no
longer imports the web layer for it.

### 2026-09-25: an edit can keep a file's size and modification time

`nrw serve` stats `nrw.toml` on each request, and rebuilds what reads the data
when the stat changes. Size and mtime are not enough. An edit made in place that
keeps the length (one folder name for another the same length) and restores
the mtime (`cp -p`, `rsync -t`, `touch -r`, a restore from backup) moves only
the change time. A replace moves the inode. So the stamp is inode, size, mtime
and ctime. A coarse file clock can still hide an edit made within one tick of
the previous write (a few milliseconds on Linux). That is why a save also tells
the server directly (`ExperimentData.reload`) instead of relying on the stamp.

### 2026-09-25: `pre-commit run --files $files` checks nothing in zsh

zsh does not split an unquoted variable into words. So `files=$(git diff
--name-only); pre-commit run --files $files` passes the whole list as one file
name, which matches nothing, and every hook reports as skipped. That looked
like a clean run for ten commits, until `--all-files` found fifteen unformatted
files. Use `pre-commit run --all-files`, which is what CI runs, or `${=files}`
in zsh. Run it twice: the first run may fix files, and only the second shows
they are clean. The venv's ruff can be newer than the pinned hook and format
differently; the hook's version is the one that counts.

### 2026-09-25: a fourth defect only a browser found

The browser test of the Settings page (`tests/test_settings_page_browser.py`)
failed one run in three, and the fault was the page's. After a conflict it said
"The settings were reloaded" and only then reloaded them. For a moment the
message was false, and the form still held the refused change. It now reloads
first, and says so only if the reload worked. The API tests could not see it:
the server's answers were right, and only their order on the page was wrong.

### 2026-09-26: the poller kept an old listing, and every read checked its versions

A run's signature, which decides whether it changed, is built from its files'
names, sizes and mtimes. Each file's version, which a read checks before it
trusts the bytes, also carries the inode and the change time. When a poll found
the signature unchanged, it kept the previous poll's record of the run, and with
it the old versions. Anything that gives a file a new inode or change time but
keeps its size and mtime then made the run unreadable: a quick look failed on
every file, and apply refused to copy it, until something else about the run
changed. That covers `chmod`, a hard link, and `rsync -t` or `cp -p` replacing
the file. A poll now keeps its own listing and reuses only the old generation,
so the page is not sent a change that shows nothing new.

It was found by a test that failed to fail. To prove that a plan names its data
source, the web test hard-linked the same files into a second folder, so that
only the source would differ. The plan ids still differed without the source,
because the review's plan held versions from before the links were made.

### 2026-09-26: a DevTools port answers every account on the machine

The first browser test started Chrome with `--remote-debugging-port`. That
opens the DevTools protocol on loopback with no authentication, and on a shared
analysis node another account could drive the test's browser while it ran:
open `file://` pages, and read whatever the developer can. The test now uses
`--remote-debugging-pipe`, so Chrome speaks the protocol on its descriptors 3
and 4 and opens no port at all. That needs no `websockets` dependency, and had
three traps:

- `subprocess` cannot put a descriptor on a chosen number.
- `preexec_fn` could, but it is unsafe with the test server's threads running.
- Wrapping Chrome in `sh -c 'exec ... 3<&N'` needs N below 10: POSIX promises
  only descriptors 0 to 9 in a redirection, and dash, Ubuntu's `/bin/sh`, keeps
  to that.

`os.posix_spawn` with `POSIX_SPAWN_DUP2` does it exactly. The pipe's ends are
raised above 4 first, so placing one on 3 cannot overwrite the other. Each
test's one-time link has a random token too: the in-process server is on the
same shared loopback.

### 2026-09-26: `TimeoutError` is not `TimedOut`, and other lessons from Bounded

- **The API maps only nrw's own `TimedOut` to 504.** `Bounded` passes on
  whatever the call raised. Apply's fresh poll waits for a poll already under
  way, and on a dead mount the background poller holds that lock. The wait
  raised a plain `TimeoutError`, which reached the page as a 500 "unexpected
  error", with a traceback in the log. It now raises `TimedOut`. It also gets
  half of the deadline, so a stuck poller is what the answer names.
- **Viewers must not share a writer's slots.** Anyone who can see the pages
  can ask for quick looks, and those shared four slots with apply. Four hung
  reads made apply fail at once. Apply now has slots of its own, and only a
  link holder can start an apply, one at a time. A quick look that finds its
  own slots full answers 409 once, rather than a 200 carrying the same
  sentence for every segment.
- **Give the slot back before waking the caller.** Otherwise a caller whose
  call has returned can call again at once, find the slot still taken, and be
  told "busy" by a source that answered.

### 2026-09-28: no angle is assumed

`nrw.toml` carried `standard_thetas = [0.45, 1.2, 3.5]` and `tnr_theta = 0.6`,
and its comment said `nrw reconcile` read them, to flag a segment at an
unexpected angle. It did not. Both readers wrote
`getattr(config, "standard_thetas", [])`, but `ProjectConfig` keeps them inside
`conventions`, so they always got the empty default, and the check never ran.
**`getattr` with a default, on a typed object, turns a wrong attribute name
into a silently empty value** -- nothing fails, and the comment beside it goes
on saying it works.

The live assumption was in the model spec. `State.thetas` defaulted to
`[0.45, 1.2, 3.5]` and `Series.theta` to 0.6, so a hand-written spec that left
them out was fitted at those angles, whatever was measured. With
`segments: auto`, the list's length also chose how many segments were read.
And `nrw model new` filled any angle a header did not record with the usual
setting for that position. theta sets the wavelength axis
(`wl = 4*pi*sin(theta)/q`), and `theta_offset` and `sample_broadening` depend
on it; a wrong one is absorbed into roughness.

Now:

- The spec's angles come from the files. `segments: auto` is every segment on
  disk, at the angle its header records; a series is at the angle of its
  run's summed dataset in `data/steady`.
- An angle a spec states is checked against the file's record, within the
  0.02 deg resolve already uses to group angles. Where they agree, the file's
  more precise value is used.
- A file that records none is refused until the spec gives its angle.
  `nrw model new` and `nrw aure import` leave it `null`, name the file in a
  comment, and write no angles at all when every file records its own.
- `nrw.toml`, the unusual-angle check and the unused geometry constants are
  gone. The shipped skills and the project template's assistant instructions
  no longer present the usual settings as a standard.

Still open, and the same kind of thing: `nrw model new` assumes the dQ column
is FWHM when a file does not say (`_dq_is_fwhm_from_headers`, with a warning).
And the project template's `.github/copilot-instructions.md` says "`dQ` is FWHM,
not sigma", which is false for `_autoreduction.dat`, whose 4th column is sigma.

### 2026-09-28: what the review of "no angle is assumed" found

Reading every angle from its file closed the obvious hole. The design, security
and test reviews found eight quieter ones, each of which fitted a wrong angle,
or the wrong data, without a word:

- **The summed dataset was looked for two levels up** from a series' slices.
  That is right for `data/tnr/<run>/`, and wrong for slices straight in
  `data/tnr/`, which `nrw sample new` suggests. The check then found nothing,
  so it checked nothing. It now looks beside the nearest `tnr` folder.
- **A `reduction_json` series fell back to any sidecar** when none named its
  run, and took the slices from that sidecar's run but the angle from its own.
  The two must be one run; a mismatch is refused.
- **`segments: auto` read past `sample.yaml`.** The register is where a person
  fits a subset of a run's segments. `nrw model new` writes `auto` only when it
  reads exactly the registered segments, and lists them otherwise.
- **An unknown dQ label hid a good angle.** `read_header` raised on the label
  before it returned the angle, so the file looked like it recorded none. They
  are separate facts: `recorded_angle` is the one reader of an angle, for
  resolving and scaffolding alike, and it skips the dQ check.
- **A pipe named like a reduced file hung every header read**, now reached from
  more commands. Header reads refuse anything that is not a regular file.
- **A line break in a directory name broke out of a YAML comment** that the
  scaffold writes, and the rest of the name became top-level keys of the spec,
  such as `post_build`, whose Python the generated script runs. `\n`, `\r`,
  U+0085, U+2028 and U+2029 all end a PyYAML comment. `problems.one_line`
  escapes names at the two places they are written out.
- **A stated angle was checked against the grouping tolerance**, 0.02 deg, so
  0.435 passed for a file recording 0.45. The check now has its own tolerance,
  0.005 deg, the precision of a value typed to two decimals.
- **Segments 1 and 2 of a run planned with 3** passed the gap check and
  resolved. When the headers agree on more segments than are on disk, the run
  is refused, as the Experiment page already treats it as incomplete.

The test review found why the lifecycle tests could not have caught any of
this: their synthetic data recorded 0.45, 1.2 and 3.5, the very values that
used to be assumed. A test angle must be one nobody would assume; the fixtures
use 1.251.

### 2026-09-28: a folder typed into a comment

A project's `nrw serve` kept watching nrw's default location. Its `nrw.toml`
had the folder typed over the placeholder -- `# location = "/Volumes/…/test"`
-- under a `# [experiment.source]` still commented out. The template said "a
setting left commented out follows nrw's default, shown beside it", and editing
the value shown beside it, without removing the `#`, is the natural mistake.
Nothing said so: no problem on any page, and `nrw serve` did not print the
folder it would watch.

**The project's own rule -- a setting that changes nothing is never silently
accepted -- only covered what the parser saw.** A comment is invisible to the
parser, so the check has to read the text. Every placeholder nrw writes shows
the default, so a commented setting with any other value was typed by a
person. `tomlfile.settings_in_comments` finds these, and the pages,
`nrw experiment status` and `settings`, and `nrw serve` at start-up all report
them, with the line and how to switch it on. `nrw serve` now also prints the
data folder, marked when it is nrw's default. The template says to remove the
`#` from both the setting's line and its table's.

### 2026-09-28: a shared catalog was built, reviewed, and withdrawn

The catalog was moved beside the data, so that the projects of everyone on an
experiment, and other programs, would share one set of parquet files. The
attempt is on branch `shared-catalog-attempt` (8f18ba5..2d581f4), and was
withdrawn before it was merged.

**Why.** It turned plain files on a network mount into a database. A
single-writer store had to learn:

- detecting writes by other programs, and accepting or restoring them;
- compare-and-swap saves;
- a history of its own, because git no longer kept one;
- group permissions for several accounts, and lock timeouts for NFS;
- deadlines for a mount that stops answering;
- moving existing projects over.

That came to about 8,500 lines. The design, security and test reviews then
found the bugs in exactly those seams:

- rows written by another program reached `sample.md`, and so the unattended
  agent's task, with none of the checks the page makes;
- a share from the terminal left `nrw serve` writing a stray catalog in the
  project;
- a save queued behind a share was lost to it;
- the tests could not tell which catalog a write had landed in;
- the agent guard did not know the new commands.

Each could be fixed, but all of them are the cost of the same choice.

**Decision.** The catalog stays in the project's `experiment/`, shared the way
the project is: through git. Sharing it between projects is a metadata
service's job. That service is planned as `[experiment.catalog] kind =
"api"`, listed as coming like Tiled and the web monitor. What a service store
must do is in `docs/experiment-sources.md`. It is also what these reviews
found: check records from elsewhere before rendering them, refuse stale edits
by revision, answer within a deadline, and never fall back to local files.

**Kept from the attempt**, because each fixes something in the project-local
design too:

- the sample editor keeps what was typed when the page reloads;
- adopting a hand-written `sample.md` keeps the runs assigned on the page;
- `nrw sample new` renders with the project's own context.

### 2026-09-29: a run's notes are written into sample.md, under Measurement conditions

Until now a run's note lived only in the catalog. Two reasons kept it out of
`sample.md`:

- `nrw sample scan` counts any six-digit number in the prose as a documented
  run;
- notes were checked with looser rules, since nothing rendered them.

People were writing per-run remarks into the sample's *Measurement
conditions* anyway, and there nobody can tell later which run a remark is
about. So each included run's notes are now written after the sample's own
text, one entry per run: `- Run 218386: realigned after mounting`. Later lines
of a note are indented under it.

**Why there, and not in the table:**

- The table's cells are parsed by four readers: ISAAC export, `nrw data
  reconcile`, adopt, and the second-table guard. A note column would be a
  fifth thing each of them has to skip.
- `nrw model new --from-notes` and the assistant already read *Measurement
  conditions* for what becomes a nuisance parameter.

The scan concern turned out to be a notice, not a failure: a run number in a
note that is not on disk is reported, as one in the sample's own prose always
was. An excluded run's notes are still not written, so a run slid to *bad* does
not show up as missing data.

**Consequences:**

- **Notes are now held to the `sample.md` prose rules when saved.** A note
  saved before this change may break them. Rendering refuses such a note with a
  `SampleRenderError` naming the run, so Apply leaves that sample alone and says
  why. It is not rewritten silently.
- **Pulling a catalog-written file changes a run's notes only where the file
  has an entry for the run.** A missing entry keeps the notes, because a file
  rendered before this change never listed any. Clearing on absence would have
  wiped them. An empty entry (`- Run N:`) clears them.
- **A type never set shows as `steady`**, in the table and on the page
  (`RunEntry.measurement_type`), and adopt compares it that way. Otherwise a
  rendered `steady` would read as a change.
- **The bulk bar's type starts at *unchanged*.** Starting at `steady` would
  reset a run already recorded as `tNR` whenever runs are reassigned.

### 2026-09-29: fitting from the Experiment page

The page fits a spec by running `nrw model generate` and then `nrw fit run` as
child processes (`python -m nr_workbench`, the server's own interpreter). The
fit is never run inside the server. Four findings shaped this.

**bumps' DREAM takes Ctrl-C as "done".** `bumps.dream.core.Dream.sample`
catches `KeyboardInterrupt` and returns the chain so far, and `nrw fit run`
then records that as a finished fit. So Cancel sends SIGTERM, never SIGINT. A
test pins which signal the step receives.

**A step runs in a session of its own** (`start_new_session=True`). That way
Cancel's `killpg` reaches the processes the step starts, such as bumps'
parallel workers. It also means Ctrl-C on `nrw serve` never reaches the fit,
so `run_serve` stops the running job explicitly on the way out. If the server
is killed, the next one marks the job `detached` and never signals a pid left
over from the last server's life.

**A cancelled fit is left as an interrupted run.** SIGTERM ends `nrw fit run`
without its `except` clauses running. The provisional manifest therefore stays
`running`, and `nrw check` reports it, as it does for any fit that was killed.
Recording "cancelled" instead would need a SIGTERM handler in the fit command.
That is possible, but it is a change to the provenance record and was not made
here.

**`.nrw/` is not ignored by git as a whole.** The fit index lives there and is
committed. Job logs go in `.nrw/jobs/`, which writes a `.gitignore` of `*`
into itself. The project's own `.gitignore` learns the rule only when
`nrw init` runs again, and a DREAM log should never be committed in between.

### 2026-09-29: what the reviews of fitting from the page found

The design, security and test reviews of the page's models and fits found
these. Each is fixed.

**`nrw model generate` restamped an unchanged script.** Every run rewrote the
script with a new `generated:` time. That changed its hash, and with it the
identity `nrw fit run` uses to refuse an identical run. The page generates
before every fit, so no page fit was ever refused as identical, and `--force`
meant nothing. The same happened to anyone who ran `generate` before each fit
in a terminal. Now a script that would come out the same but for its time is
left as it is ("up to date with its spec").

**A job found its fit by guessing.** It took the newest fit of the same model
started after the job did. A fit of that model run in a terminal while the page
job ran, which is normal here, was taken for the job's. Now the job reads the
id from its own `nrw fit run` output (`RUNNING_LINE` in `commands/fit.py`) and
checks that the index has it. A cancelled fit is not in the index, so it links
nothing.

**`python -m` puts the working directory first on `sys.path`.** The page ran
its commands with the project as working directory, so a `click.py` or
`json.py` beside an analysis would have been imported instead of the real
module, in every step and in the fit script. Children now run with `-P`, as
the `nrw` script effectively does.

**A job record written on start-up.** A restarted server marked a job it found
`running` as `detached` and wrote that back. That broke a read-only server,
and on a second server it rewrote the first server's live job. Now `detached`
is said in memory only.

**A job whose thread failed never ended.** A full disk while writing its
record left it `running`, and every later fit was refused until restart. Now
the thread always ends the job.

**Job files are opened never through a symbolic link.** Reads of the page are
open to every account on the node, so a link planted in a shared project's
`.nrw/jobs/` would have served whatever it pointed at.

### 2026-09-29: spec text is never code in a generated script

The generator wrote a spec's `description` between the `"""` of the script's
docstring as it was, and its `name` into the `# model:` comment as it was. A
description holding three quotes closed the docstring, so what followed was a
statement, run by every fit of the spec. A name with a line break ended the
comment, with the same effect. The schema allowed both.

`nrw model new --from-notes` takes the description from a language model,
which reads sample.md. So a prompt injection in a sample's notes could have
become code that runs when someone clicks **Fit**. The page's Fit made that one
click, and the promise that the page only runs "what the spec says" rested on
it.

Now a description line holding `"""`, a backslash or an unprintable
character is escaped whole. So is a name that is not printable, which is
written as its `repr`. Plain text is written exactly as before, so no existing
script changes, and neither does the identity of its fits. A test parses the
generated module and checks that the docstring is the description and that
imports come next.

Two smaller rules came out of the same review. `conditions.from_table` now
ends a table where its rows end, as `reconcile` does. Before, a row-shaped line
in a run's notes, further down, supplied the condition of a run the table did
not list. And `nrw model new --name` applies the plain-name rule that
`nrw aure` and the page apply.

### 2026-09-29: AuRE names layers in prose, and a spec takes identifiers

The first quick fit on real data got through `aure new`, `aure run` and
`aure import`, then failed at `nrw model generate` with thirteen validation
errors. AuRE had named the layers `silicon oxide`, `DPPC bilayer headgroup
region` and `water-based solvent (unspecified contrast)`. `aure import` wrote
them into the spec as they were, but a spec's layer name must match
`^[A-Za-z_][\w-]*$`, because it is also a parameter path and a variable in the
generated script. The import tests used AuRE models whose layers were called
`Ti`, `Cu` and `Si`, so this was never exercised.

`spec_layer_name` in `aure_import.py` now makes each name an ASCII identifier:

- accents are dropped;
- every run of other characters becomes one underscore;
- a name that would start with a digit is prefixed with its position's name,
  and one left empty is that name;
- a Python keyword, `SLD` or `probe` is suffixed.

Repeats are then made unique, as they were before. The import prints what it
renamed, and the spec keeps AuRE's names as comments, so it can still be read
against AuRE's report. The test model is the one AuRE reported for that
bilayer.

### 2026-09-30: fit settings live in nrw.toml, and the default fitter is DREAM

A fit's settings now come from the first of these that sets them:

1. `nrw fit run`'s options, or the Experiment page's Fit form;
2. the project's `nrw.toml`: `[fit]` for the fitter, `seed` and `parallel`,
   and one table per fitter for its own settings (`[fit.dream]`, `[fit.de]`,
   `[fit.amoeba]`);
3. bumps' default.

When nothing names a fitter it is DREAM. `fitting/settings.py` is the one
place this is decided. The README's "Fitting options" section is the user's
account of it, including how `nrw.toml` and `~/.aure` divide the work.

**A spec's `fit:` block was never read.** `FitSettings` said it was the
default "every later `nrw fit run` inherits", but nothing consumed it. Every
spec `nrw model new` or `nrw aure import` wrote carried
`fit: {method: amoeba, steps: 1000}`: a setting in plain view that did
nothing. It was not wired in now either, because wiring it in would have made
every existing spec say "amoeba" and override the project's new DREAM default.
New specs no longer get the block, and `nrw model validate` warns about one it
finds. A per-model override could be added later as something a person writes
on purpose, not as a scaffold default.

**A fitter's table applies only to that fitter.** bumps ignores settings a
fitter does not take, but the fit record keeps them. DREAM's `samples` in an
amoeba fit would therefore make two identical amoeba fits read as different
runs. A setting a fitter does not take is refused, in `nrw.toml` and on the
command line alike.

**AuRE's fits are not configured here.** `--budget quick` pins them in the
setup. Otherwise AuRE reads `FIT_METHOD`, `FIT_STEPS` and `FIT_BURN` from the
environment nrw passes on: the shell, then the project's `.env`, `~/.nrw`,
`~/.aure`. nrw loads all keys from those files, not only its own. nrw pins
AuRE's physics knobs for every run, over the shell's too.

### 2026-09-30: a quick fit could not be run again, and AuRE never read about the measurement

Asked to run a quick fit again after adding information about the
measurements, the page offered nothing. Two things stood in the way.

**The name was taken.** A quick fit refused any model name that already had a
spec or an AuRE folder, so the second quick fit of a model was impossible. The
page also kept showing the finished job, as though the fit it had done were
the answer. Now:

- A spec `nrw aure import` writes carries a `self sha256` line, the same check
  as a generated script, plus the AuRE output it came from. Edited, even in a
  comment, it no longer verifies (`is_unedited_proposal`).
- A model whose spec is an unedited proposal can be quick-fitted again. Each
  run of AuRE gets a new folder (`aure/<name>-2/`, ...), so none is ever
  overwritten.
- `nrw aure import --replace-unedited` replaces the spec only if it still
  verifies. The check happens as the file is written, not when the job
  started, so an edit made while AuRE ran is kept. An edited spec is refused
  when the job is asked for, too.
- The Fit panel no longer shows a job that ended before the page opened; past
  fits are in Fits. A fit refused as identical names the fit it matched
  (`IDENTICAL_LINE` in `commands/fit.py`, the job's `same_as`) and offers
  **Run again anyway**.

**AuRE never saw the measurement.** `nrw aure new` sent AuRE the sample's
*Description* and *Details*, and *Fits to perform* as a hypothesis. It never
sent *Measurement conditions*, the run's condition, or its notes, so
information added about the measurements could not reach the proposal, however
many times it was re-run. The state's `extra_description`, which AuRE appends
to the description when it prompts the model, now carries all three for the
run being fitted. *(Wrong: AuRE never reads it. They go into
`sample_description` now; see "AuRE never reads a state's
`extra_description`" below.)* They are read with the catalog's own parser, which also keeps
a hand-written note on a run no table lists.

**Edits reach AuRE through `sample.md`.** The page's edits reach the file when
they are applied. `sample_md_pending` says when the catalog has edits the file
does not have yet, in `nrw aure new` and in the Models panel.

### 2026-09-30: a model written on the page was always in air

Fitted from the Experiment page, a sample was always in air, although its notes
said it was measured in a liquid. There were two causes.

**The page never asked for the notes.** Its **New model** ran `nrw model new`
without `--from-notes`, so every spec it wrote had the placeholder stack: air
on a film on Si. Nothing on the page said so, and **Fit…** fitted it.

- New model now runs `nrw model new --from-notes`, as a job: a language
  model's answer does not fit in a request. The synchronous runner it used
  (`run_nrw`, with a 120 s limit) had no other caller, and is gone. *(Since
  re-added, for the Language model **Check**, with a process group of its own;
  see the review entry below.)*
- A spec whose materials and stack are still the placeholder's is marked
  *placeholder stack*, and Fit… on it says so first. `is_placeholder` compares
  the stack itself, not the header comment that announces it, because a person
  replacing the layers may well leave the comment where it is.

**The request left each run to be matched up.** `--from-notes` sent
`sample.md` whole. A model reading it had to connect a Measurements row
(`218386 | steady | in D2O`) to the skeleton's state `run218386` by itself.

- `describe_measurements` now sets out one line per state and series, with its
  run, condition and notes, plus one line for what every run shares.
- The system prompt says to take the ambient from those lines and never assume
  air. A contrast that differs between states is the ambient's `rho`,
  `per: state`, one entry `in:` each state.
- `measurements_of` (in `experiment/adopt.py`) is the one reader of the three,
  for this request and for AuRE's `extra_description` (now its
  `sample_description`, as above).

### 2026-09-30: the page chooses the language model in `.env`, and the server loads none

The Settings page's *Language model* section writes `LLM_PROVIDER=claude_code`
and `LLM_MODEL` into the project's `.env`, so New model and AuRE's quick fit can
use Claude through the Claude Code CLI without `~/.aure`. Three findings shaped
it.

**A value loaded into the server would pin every job.** `load_dotenv(override=
False)` puts a file's values into `os.environ`. A child process inherits
`os.environ` as its environment, and the environment wins over every file. So
had `nrw serve` ever called `load_env()`, each job would run with the provider
the server read at that moment, whatever `.env` said afterwards. The server
does not call it today; the page only needs `is_available()`, which finds AuRE
without importing it. Two things keep it that way:

- `where_set` works out each setting from the files, without loading them.
- `load_env` records what it loaded (`loaded_from_files()`), and
  `child_environment()` leaves those out, so a child reads the files itself.

**`LLM_MODEL` must be written, even empty.** A machine that has run AuRE with
another provider has `LLM_MODEL=gpt-4o` in `~/.aure`. With only
`LLM_PROVIDER=claude_code` in `.env`, AuRE would run `claude --model gpt-4o`.
python-dotenv sets `KEY=` to `""`, so an empty `LLM_MODEL=` in `.env` stops
`~/.aure`'s from being taken. AuRE's own default for `claude_code` is the empty
model, which is the CLI's default.

**`.env` is the person's file.** It holds keys and other tools' settings, so
`project/envfile.py` follows these rules:

- It touches only the `NAME=value` lines of the variables asked for.
- It removes a later duplicate, because the later line would win.
- It keeps line endings and the file's mode. A new `.env` is 0600.
- It refuses a symbolic link.
- It makes the change against the revision the page read.

**Check** runs `nrw check-llm --endpoint --json` in a child, as a job would,
one at a time and for 180 s at most. *(Now `LLM_TIMEOUT` plus a minute, capped
at ten minutes: see the next entry.)* It is behind the link, because the call
is billed. Reading the section needs no link, so it never shows a key, not even
redacted: it says only whether one is set.

### 2026-09-30: what the review of the Language model section changed

The design, security and test reviews of the two commits above found no
critical problem. Their findings changed these things:

- **`.env` is edited through python-dotenv's own parser** (`dotenv.parser.
  parse_stream`), not by splitting lines.
  - Splitting lines turned a form feed into a line break and rewrote an
    `LLM_MODEL=` inside a quoted value.
  - A bare `LLM_PROVIDER` line after the one nrw set made a reader unset the
    provider, so Save answered 200 while a job used `~/.aure`.
  - Each piece the parser gives back is kept byte for byte, including the blank
    lines that belong to it, and a later line for a managed name is removed. The
    header comment is gone, so undoing a choice restores the file exactly.
  - These are python-dotenv's internal modules, so the dependency is bounded
    `<2`.
- **`.env` is read without following a link** (`O_NOFOLLOW | O_NONBLOCK`,
  `fstat`, at most 1 MiB). Reading it is open to anyone who can reach the page,
  and a planted link to a FIFO or to `/dev/zero` would have hung or exhausted
  the server.
- **Revisions are HMACs with a key made per process.** A plain SHA-256 of a
  file of secrets, served openly, confirms a guess of its contents.
- **The shared atomic writer set the mode after writing the data.** The temp
  file sat under the umask, usually 0644, for the moment it held the new
  contents. That was harmless for `nrw.toml` but not for `.env`'s keys. Now it
  is created with the mode it is given, and `fchmod` runs before any byte is
  written. `mode=` lets `.env` pass the mode it read through its own
  descriptor, so the target is never looked up by name, which would follow a
  link swapped in meanwhile. This applies to every caller of
  `atomic_write_bytes`.
- **`where_set` replays python-dotenv exactly.** It uses one list of files
  (`_candidates`, now with `is_project`) and python-dotenv's own interpolation,
  against the environment as it would stand at each file. So a `${VAR}` in
  `~/.aure` sees what `.env` set, a bare name sets nothing, and
  `PYTHON_DOTENV_DISABLED` is honoured before each file. A test compares the
  result with a real child.
- **The Check could leave a billed `claude` running.** `subprocess.run(timeout)`
  kills only the direct child, and AuRE starts `claude` as a grandchild. Now:
  - `jobs.run_nrw` gives the Check its own process group and kills the whole
    group;
  - `LLM_MAX_RETRIES=0` makes "one real call" exactly one;
  - the deadline follows the effective `LLM_TIMEOUT`, plus a minute, capped at
    ten minutes.
- **The section has its own class** (`web/llm_settings.py`), and AuRE's
  provider name, its default timeout and its rule for finding `claude` live in
  `aure_adapter`.
- **Provider errors are scrubbed of keys** (`aure_adapter.scrubbed`) before a
  job prints them, because job logs are open to read.
- **Each measurement goes into the request as one line**, marked as data, so a
  note written over several lines cannot pass for another state's.

**Decided, and open to change: New model shares the one job lane.** New model
became a job in bdbfa3a, so it now waits behind a running fit, which the old
synchronous command did not. The page disables the button and says so rather
than refusing the click. A second lane for language-model jobs would need a
second runner with its own records folder and a panel that follows two jobs.
That is not done yet.

### 2026-09-30: AuRE never reads a state's `extra_description`

The entry "a quick fit could not be run again, and AuRE never read about the
measurement" said AuRE appends a state's `extra_description` to the sample's
description when it prompts the model. **That was wrong.** AuRE declares it
that way (`state.py`: "Appended to sample_description when prompting the
LLM"), but no prompt at the pinned 1.0.2, nor on AuRE's `main` today, reads
it. The field is only listed, copied, displayed and checkpointed.

The consequence was the one that entry meant to fix. A DPPC sample whose
Measurements table said "The ambient medium is D2O" for run 232736 had that
line in its `extra_description`, and AuRE still proposed `air` as the ambient.

`compose` now appends the run's condition, the sample's *Measurement
conditions* and the run's notes to `sample_description`, which AuRE's intake
and modeling prompts do read, and writes no `extra_description`. A quick fit
fits one run, so one description serves it. **Check what AuRE reads from its
prompts' code, not its docstrings:** AuRE declares no stable API
(`aure_adapter`), and a documented field can be unwired.

### 2026-09-30: curating fits, where their evidence is

Launching fits stays on the Experiment page, where their inputs are. Curation
(star, finalize, discard, delete) goes on the fit and Fits pages, because a
person judges a fit while looking at it. All of it goes through the one gated
write API.

**Recorded as index events.** `star`, `unstar`, `discard`, `restore` and
`delete` are appended to `.nrw/index.jsonl` beside `promote`, with who and
when. They merge in git and never lose history. `provenance/curation.py`
replays them (`curation_of`): the last word on a fit is what it is.

**Discard, then delete, a decision of the user's.** Discarding keeps every
file. Deleting is a second, explicit step, and it is refused while anything
uses the fit:

- a report cites it (`notes_about`);
- a figure manifest lists it (`"fits"`);
- another fit read its files, as an input under its directory;
- or it has ISAAC records (`isaac/`).

Deleting never goes through a link, and the index keeps the record that the fit
ran. `nrw check` no longer reports a discarded or deleted fit, and `nrw ls`
leaves them out unless `--all`.

**A fit superseded as final still showed `final`.** The Fits list, the sample
page and the fit page badged every label a fit had ever held. `nrw ls` asked for
the last `final` across the whole project, so with two samples only one fit got
its mark. Every reader now takes the labels from `curation_of`, where the last
promotion of a label per sample holds it. *(Not every reader did: see "One
reader of labels" below. `curation.replay()` is the one reader now, and
`curation_of` returns its `fits`.)*

**The fit pages carry the write token now,** so they get the strict script
policy (`page_csp`, with a nonce), as the Experiment and Settings pages do.
They render project text: notes, the spec and the script. Only the pages' own
scripts may run where a token is.

**The page names a fit whole.** The terminal accepts a prefix, but a prefix in
a URL could come to match a later fit.

### 2026-09-30: publishing a final fit to ISAAC from its page

**A key in `~/.nrw` never reached the push.** `nrw isaac export --upload` ran
`nr-isaac-format push` with nrw's own environment, but never called
`load_env()`. The tool reads `ISAAC_URL` and `ISAAC_KEY` from its environment,
or from a `.env` that python-dotenv finds by walking up from *the tool's own
install* (not the working directory, as its comment says), never from
`~/.nrw`. Under `--no-llm` nothing else loaded the settings either, so a key
kept in `~/.nrw` reached the push only by accident. `ISAAC_URL` and `ISAAC_KEY`
are now known settings, with the key secret: the page says where each is
set, and never shows the key. *(`nrw doctor` lists them, the key redacted,
among its "llm settings", but does not say where each is set, and shows a
project `.env` value that the push ignores. Not fixed yet.)* See the next entry for how the
push reads them now.

**What is validated is what is pushed.** An export asks a language model for
the conditions, so exporting twice can make different records. `nrw isaac push
FIT_ID [--validate-only]` sends the records the export already wrote, and the
page's Validate and Push do the same. `export --upload` still works, going the
same way.

**A push that half-fails has still published.** `nr-isaac-format push` exits
1 if any record fails, even after the portal accepted others. The push path
reads the "created (record_id=...)" lines whatever the exit code, and appends
a `publish` event to the index: when, by whom, the portal's host, each record
made, and `complete`. *(Now two events sharing an `attempt` id, the first
written before the push runs: see "A push is recorded before it runs"
below.)* The curation replay carries these events, so a published
fit's files can never be deleted, even with its `isaac/` removed by hand.

**Only the final fit is published** (the user's decision), by `nrw isaac push`
and by the page. Validating needs no finalization: asking the server whether
records would be accepted publishes nothing. The guard counts `nrw isaac push`
without `--validate-only` as an upload. *(Superseded: the guard refuses every
`nrw isaac push`, because validating sends the records and the key too. See
the next entry.)*

### 2026-09-30: what the reviews of curation and ISAAC changed

The design, security and test reviews of 194645c and 074d3b6 found one
CRITICAL problem, in older code, and several that had to be fixed before the
features could be trusted.

**One reader of labels.** "Which fit holds which label" had three
implementations: the new replay, `FitIndex.current_label()` (where
`sample=None` meant *any* sample), and scans of `promotions()` written in each
caller. Every reader not moved to the replay was wrong for a project with more
than one sample:

- `nrw sample reset` protected a sample's final fit only if it was the
  project's *last* promotion. With S1 finalized before S2, it deleted S1's
  final fit, published or not, and `forget()` erased its publish record.
- `nrw check` failed correct reports ("names this fit as the answer, but
  nothing is promoted"), and generated reports lost their `[FINAL]` marker.
- Promoting a fit with no sample (`<root>/results/`) wrote a false
  `supersede` against another sample's final.

`current_label` is gone. `curation.replay()` returns a `CurationState`:
`holder(sample, label)` (a fit with no sample in a slot of its own) and
`of(fit_id).promotions`. Every reader uses it, and the reset refuses while
any of its sample's fits holds a label or was pushed.

**Deletion could leave the project.** `delete_files` compared path *names*.
The index is committed, so anyone who can commit to the project writes it,
and a `sample` of `../../other`, a fit id of `..`, or a linked sample folder
steered `rmtree` elsewhere (shown in a scratch project). Now:

- `lookup.fit_dir` refuses a fit id or sample that is not a plain name.
- The resolved path must be exactly `samples/<s>/results/<id>` (or
  `results/<id>`), so a link anywhere on the way is refused.
- The delete walks directory descriptors without following links, renames
  the fit out of the way, records the deletion, and only then removes the
  files. A removal that fails part way leaves a recorded deletion and a
  hidden directory, never a half-deleted fit that says it is whole.
- `used_by` looks at every sample's reports and figures, and the project's
  own `reports/`. Evidence it cannot read counts as a use.

**The project's `.env` chose where the key went.** `load_env()` read the
project's `.env` before `~/.nrw`, so an `ISAAC_URL` there sent the person's
key, and the records, to any host. The portal and the key now come from the
person's own settings only (`where_set(..., skip_project=True)`). The tool
gets `--url`, and its environment has `PYTHON_DOTENV_DISABLED=1`, so it takes
no portal of its own choosing. That environment is the shell's: nothing nrw
loaded from a file, so a project's `PYTHONPATH` reaches no tool. The host
shown is `urlsplit(...).hostname`: a `real.host@evil.example` URL shows
`evil.example`. Validate asks first, as Push does. The page sends the host it
showed, and the job refuses if the portal is another by then
(`--expect-host`). Under `NRW_AGENT` both validate and push are refused,
because both send the records and the key off the machine.

**A push is recorded before it runs.** A cancelled, killed or timed-out push
had made records while the index said nothing. Now a `publish` event with an
`attempt` id and each file's digest comes first, and a second event carries
the records made and `complete`. An attempt with no outcome reads as
"may have published". What was sent is copied to `isaac/published/<attempt>/`,
so re-exporting cannot change the record of it. The real tool goes on past a
failed record, only an authentication error stops it early, and a contract
test runs it against a local stand-in portal.

### 2026-09-30: what the documentation review found

A review of every document against the code found about sixty stale or wrong
statements. Most came from four patterns, which are worth knowing because
each will recur.

**A limit added in code did not reach the lists of limits.** The refused
commands grew from three (promote, `--upload`, `--force`) to eight rules, and
at least seven places listed them: `guard._REASONS` (the source), the table in
`docs/agent.md` (the reference the others link to), the walkthrough in
`docs/getting-started-with-agent.md`, "The person's commands" in
`AGENTS.md.j2`, the `$comment` in the Claude harness's `settings.json`, the
README's summary, and the analyst-handoff skill's table. Each still said
three. Change `_REASONS` and those seven together.

**The Claude Code hook applies to interactive sessions too.** `nrw agent
guard` judges every Bash call in any Claude Code session in the project; only
`NRW_AGENT`'s refusals are for unattended sessions alone. So `AGENTS.md`'s
rows saying an assistant may promote or curate "when the person asks" could
never be followed. They now say the command is the person's, to hand to them.

**`pip install 'nr-workbench[isaac]'` cannot work.** nr-workbench is not on
PyPI (its `aure` dependency is a direct reference), so that command installs
nothing, and it was the hint in `nrw isaac export --help`, the missing-tool
error and the ISAAC panel. The hint is now `isaac_cmd.INSTALL`: re-run the
installer with `NRW_EXTRAS=isaac`, or `pip install -e '.[isaac]'` in a clone.

**A user's decision did not reach the walkthrough.** "Only the final fit is
published" and "the ISAAC key is set in `~/.nrw`" changed the code and
`docs/experiment.md`, while `docs/getting-started.md` still showed
`nrw isaac export --upload` with credentials in `.env`.

Code changed with the review:

- **`nrw sample reset` is refused under `NRW_AGENT`** (rule `reset`), and by
  the hook, except with `--dry-run`. It deletes every fit and model of a
  sample: the record of what was tried.
- **The guard's text floor** (the fallback for a command `shlex` cannot split,
  such as one with `$'...'`) had no `reset` or `aure-run` rule, so a quoting
  accident let either through. Both are there now, with the same `--dry-run`
  exemption.
- **A fit pushed while it was final keeps its pushes on its page.** The ISAAC
  panel hid itself for any fit not final, and with it the record that the fit
  had been published.
- **Ruff no longer reads Markdown** (`extend-exclude = ["*.md"]`). Ruff 0.16
  formats the Python inside Markdown code blocks, the pinned pre-commit ruff
  does not, so `ruff format --check src tests` failed on four shipped SKILL.md
  files and `ruff format src` rewrote them.
- `nrw serve`'s start-up lines and help, and the page's quick-fit-again button,
  now say what the pages do: curate, publish, set the language model, and fit
  a proposed model's own run again.

### 2026-10-01: the quick fit ignored `[fit]`

`nrw.toml`'s `[fit]` (89c6af2: DREAM unless it says otherwise) reached
**Fit…** and `nrw fit run`, but the quick fit's last step still passed
`--method=amoeba`, written before `[fit]` existed. So every quick fit's
recorded fit had no uncertainties, and its ISAAC records carried values
without error bars. The step now passes no `--method`, and `nrw fit run`
resolves the fitter as it does for any fit. A DREAM fit takes minutes rather
than seconds; a project that wants quick fits fast sets `method = "amoeba"`
under `[fit]`, which changes **Fit…**'s default too.

### 2026-10-04: a large `-p @<file>` prompt never reaches Claude Code's model

`nrw agent run` started Claude Code as `claude -p @<prompt-file> ...`, which
asks Claude Code to attach the file. Past some size it does not: the model gets
the bare reference in place of the file's text, and the session starts without
its instructions. Measured with claude 2.1.232, one tool-less call each:

| Prompt | As `-p @<file>` | On stdin |
|---|---|---|
| 20 KB | arrived | |
| 120 KB | did not arrive: the model spent its turns looking for the file (`error_max_turns`) | arrived whole |

It was first seen in nr-watcher's assessment call, which passed its prompt the
same way. Prompts up to 64 KB arrived; a 74 KB one did not, and the model
answered that the briefing file "was referenced but its contents were not
delivered". The exact limit was not measured. The session prompts seen so far
were 8 to 13 KB, but nothing caps them, so a longer one would fail the same way.

The prompt now goes on stdin, as OpenCode's already did
(`Invocation.prompt_on_stdin`). `claude -p` with no prompt argument reads
stdin, and says so when it gets nothing: "Input must be provided either through
stdin or as a prompt argument when using --print". The prompt file is still
written, as the record of what the session was told. A harness wrapper written
for the old `-p @<prompt-file>` form must now read stdin (docs/agent.md).

`_stream` writes the prompt from a thread now. A prompt longer than the pipe
buffer (64 KB) blocks its write until the harness reads it, and that write came
before the session's timer was started, so a harness that never read its stdin
held `nrw agent run` with a timeout that never fired.

Both directions of the pipe are UTF-8 by name (`HARNESS_ENCODING`). Python
opens a text pipe in the locale's encoding: cp1252 on Windows, and ASCII under
a bare C locale with Python's UTF-8 handling off. The harnesses read and write
UTF-8 whatever the locale. Left to the locale, moving to stdin would have lost
the prompt file's explicit UTF-8. A character outside the codec (ρ, σ, →)
killed the thread feeding stdin, leaving the harness waiting for its prompt,
and cp1252's `—` arrived as an invalid byte. Reading the harness's UTF-8
output with the locale codec was already wrong before, and the same setting
fixes it.
\n
### 2026-10-04: OpenCode 2 loaded no guard, and ran sessions nrw could not stop

Two findings from OpenCode 2.0.22, run unattended the way `nrw agent run` runs
it.

**The guard plugin never loaded.** OpenCode 2 refused `nrw-guard.js` at load
time, and only a WARN line in `~/.local/share/opencode/log/opencode.log` said
so: "Plugin must export a default definition with an id and an effect or setup
function". `nrw check --force` under `NRW_AGENT=1` ran, and only nrw's own
inside refusal stopped it. OpenCode 2 also renamed the tool that runs a
command, from `bash` to `shell`, and its input is still `{ command }`. Its
published plugin docs still describe the OpenCode 1 form, so the form it loads
was read from its own error messages and its bundled source instead:

- a default export that is a plain object (a function with the same properties
  is refused: "Expected object");
- `setup(ctx)` receives `ctx.location.directory`;
- the hook is registered with `ctx.tool.hook("execute.before", fn)`, and `fn`
  receives `{ tool, input, sessionID, ... }`;
- a hook that throws refuses the call before the command runs, and the model
  is shown the message (the tool part ends in `error`).

OpenCode 1.18.18 calls every export of a plugin file as a function and throws
on any other. One file therefore cannot serve both versions, and `nrw init`
writes `nrw-guard-opencode2.js` beside `nrw-guard.js`. Both contain no refusal
logic, only the call to `nrw agent guard`. With both files in place, the same
`nrw check --force` came back from OpenCode 2 refused before it ran.

**The session outlived its timeout.** `opencode run` connects to a shared
background service unless given `--standalone` (OpenCode 1 has neither). A
session told to `sleep 20 && touch` a file was killed three seconds into the
command, the way nrw kills a session at its timeout, by its process group. The
file appeared anyway: the session belonged to the service. The service also
keeps the plugins it loaded, whatever `nrw init` has written since. With
`--standalone`, the private server (`opencode serve --stdio`, in its own
process group) exited when the session's process group was killed. A command
already running could still finish, the same limitation Claude Code's sessions
have. `opencode_argv` now adds `--standalone` unless the launcher reports
version 1, which it asks once per launcher.

The tool's environment and working directory were the `opencode run`
process's in both modes, so `NRW_AGENT=1` reached the commands either way.
\n
### 2026-10-04: an unattended fit's limits are checked in `nrw fit run`, not in a hook

nr-watcher, which watches REF_L, runs `nrw agent run` for every new
measurement of a beamtime. On its first day, sessions asked DREAM
for up to 600,000 samples with 30,000 burn-in steps, and two such runs took 68
and 42 minutes. It then added a Claude Code `PreToolUse` hook that refused
over-budget `nrw fit run` commands. The hook refused a fit four times that
day, and each time the session ran the fit again within the limits. That hook has
two flaws, and `[agent.limits.<fitter>]` replaces it:

- **It saw only the command line.** `nrw fit run model.py` names no setting
  and asks for whatever `[fit.dream]` and bumps' defaults say. A hook can't
  know that without re-implementing `fitting/settings.py`. `nrw fit run`
  already knows: it compares the settled settings (`ResolvedFit`) and reports
  each value's origin.
- **It was a Claude Code hook.** OpenCode never reads one, and its plugin is
  JavaScript (see the entry above). A check inside `nrw`, under `NRW_AGENT`,
  holds for every harness. It is the same second mechanism `refuse_if_agent`
  already gives the guard's refusals.

The limits are off unless `nrw.toml` sets them, as the compute guidance in
docs/agent.md has always argued. Limits are a site's or a scientist's decision
about unattended work, not nrw's.

### 2026-10-07: "not through the substrate" read as back reflection

`reads_as_back_reflection` matched its phrases anywhere, so the notes of a
film in air that ruled the substrate side out in so many words -- "The beam
enters from the air side, not through the substrate." -- set
`back_reflection: true` in AuRE's setup. nr-watcher's assessment writes that
sentence for every sample in air, so on IPTS-38693 all ten samples of a
beamtime of dry films came out in back reflection. AuRE happened to read the
description itself and fitted from the air side, and the spec `nrw aure
import` wrote said so; the setup's state still said otherwise, which AuRE
honours when it builds a multi-state problem. The same reading drives the
critical-edge correction of `nrw model new --from-notes`, which would have
moved the medium above the film by the silicon's SLD.

A phrase now counts against back reflection when a negation stands up to four
words before it in the same clause ("not", "no", "never", "without", "rather
than", any "n't", ...). The clause ends at punctuation, a dash, a paragraph or
list item, or a word that starts a new claim ("and", "but", "whereas", ...),
so "no oxide, and measured through the silicon substrate" still reads as back
reflection, and one phrase stating it outweighs another ruling it out. A
single line break does not end a clause: notes are wrapped mid-sentence, and
"not\nthrough the substrate" is the same negation. Notes that only rule the
substrate side out are told as such by `nrw aure new` ("the notes say the beam
does not arrive through the substrate"), with no warning, since that is
evidence rather than silence.

### 2026-10-07: the fit of AuRE's model was not the problem AuRE fitted

On IPTS-38693's cu100 (a 250 nm Cu film in air), AuRE kept a round at
chi-squared 13.6, and the fit `nrw fit run` recorded from the spec `nrw aure
import` wrote reached 137. The spec froze or dropped half of what AuRE had
fitted: 7 free parameters where AuRE had 14. Recomputed with bumps, nrw's
problem at AuRE's values was 147; adding AuRE's sample broadening (0.035) took
it to 21.7, and AuRE's three segment intensities (1.013, 1.107, 1.371) to
13.1, with per-segment chi-squared equal to AuRE's (4.75, 2.52, 30.7; the
total differs only by its degrees of freedom).

Two things lost them:

- **`current_model` has no field for the probe.** AuRE's finalize writes the
  selected round's layer values into it, but "4 fitted parameter(s) had no
  ModelDefinition field": each file's intensity and the sample broadening (a
  theta offset or background would be the same). Their values, and the bounds
  of every free parameter, are in the round's own `fit_results` entry, as
  `parameters` and `bounds` under AuRE's names (`Cu interface`, `intensity
  <file>`, `sample_broadening`). `reported_fit` reads that round: the adopted
  final MCMC polish, else the one `final_selection` chose.
- **The import read ranges only from `current_model`.** AuRE wrote each
  `roughness_max` and no `roughness_min` (its builder takes min(5 A, value)
  then), and the substrate's interface has no layer entry at all, so every
  interface was frozen; the one intensity was per state, at 1.0 +- 0.1, which
  cannot reach a 3.4 degree segment at 1.37.

The import now fills what `current_model` does not declare from the fit's
recorded bounds, by the stack entry's own name (a name the stack repeats is
skipped: it would not say which layer), frees the probe's parameters as AuRE
fitted them, one intensity per segment when it fitted one per file, and says
so. A spec gives a parameter one starting value, so the segments start at the
median of AuRE's intensities. Refitted that way, with nr-watcher's DREAM
limits, cu100 reached 12.6, and the intensities came back to 1.009, 1.054 and
1.397.

Two other looks of that beamtime failed outright at `nrw model generate`:
AuRE had pinned SiO2's SLD with equal bounds (`sld_min` = `sld_max` = 3.47),
and the import declared it free in that empty range, which the spec refuses.
A declared pair is now read as AuRE's builder reads it: an inverted one is
swapped, and an equal one holds the value the stack has, with no parameter.

### 2026-10-09: AuRE's `max_refinements` counts evaluations, so `--budget quick` refines nothing

AuRE passes a setup's `max_refinements` to its runner as `max_iterations`
(`aure/cli.py`, `run_analysis(max_iterations=max_refinements)`). The iteration
counter advances only in `evaluation_node`, and `route_after_evaluation` ends
the run once `iteration >= max_iterations` (`aure/nodes/routing.py`). The
first evaluation makes the counter 1, so `max_refinements: 1` means one fit,
one evaluation and no `_refine_model` call at all. AuRE's own flag help calls
it "Maximum number of refinement iterations"; the iterations it counts include
the first fit.

So `nrw aure run --budget quick`, which writes `max_refinements: 1`, never
refines. `--help`, `docs/first-fit.md` and the README all say "one
refinement". Fixed on 2026-10-13: quick now writes `max_refinements: 2`, the
one refinement those words promise. `tests/test_aure_cmd.py` checks it against
AuRE's own initial state and router; the one step between them it cannot run,
`evaluation_node` adding one per evaluation, is read off the source above. `standard` (AuRE's 5)
allows up to four refinement rounds, and fewer when a round only widens a bound
(`bounds_only_refinement` re-fits without asking the model).

### 2026-10-09: the prompt diagrams, and three things drawing them showed

`docs/llm-calls.svg` and the five `docs/*-llm.svg` show how each command's
prompt is put together. They were drawn from prompts captured on the real
code paths with only the network call stubbed, then checked line by line
against the source. Three findings outlive the drawing:

- **SVG collapses runs of spaces.** Every aligned column in a prompt box
  (`"description"   one or two sentences`) rendered as one space. Chrome
  ignores `xml:space="preserve"` inherited from the root, so the diagrams also
  carry `<style>text { white-space: pre; }</style>`. Keep it when editing them
  by hand, or the alignment goes without any error.
- **AuRE's evaluator wrote a sentence for every slot nobody filled.**
  `judge_fit` never passed `per_file_results`, `features`,
  `residual_analysis`, `fit_history` or `structural_hypotheses`, so every
  `nrw assess` prompt said "(single-file fit — not applicable)", co-refinement
  or not, and "(no structured residual oscillations detected)" when nothing
  was analysed. AuRE's boundary-hit text also said "Range has been
  auto-expanded for the next fit", which is true only inside AuRE's own loop.
  This is why `nrw assess` has its own prompt now (2026-10-13).
- **The `claude_code` provider's JSON retry reads only the user half.**
  `_wants_json` is `"json" in user_text.lower()`. `nrw isaac export` puts
  "Reply with JSON only" in the system half, so it never gets the retry.
  `nrw model new --from-notes` got it only when its skeleton happened to
  contain `reduction_json`. Since 2026-10-13 its user half, and `nrw assess`'s,
  end by asking for the JSON reply, so both get it; isaac export's still does
  not.

### 2026-10-13: what the prompt diagrams' findings changed

Drawing each command's prompt (the 2026-10-09 entry above) turned up seven
things the prompts said or did that were not so. All seven are fixed, each
with a test that fails if it comes back.

- **`nrw assess` writes its own prompt** (`fitting/judge.py`). AuRE's evaluator
  is written for AuRE's loop, and nothing from this side could stop its
  template telling the judge things that are untrue of a finished nrw fit.
  Every section of the new prompt is what the fit's record holds or is left
  out: the arithmetic findings themselves, chi-squared by model, ranges and 68%
  intervals, and convergence in its three states. The verdict keeps the four
  keys `as_markdown` writes. A reply with none of them is no verdict, never a
  stand-in: AuRE's chi-squared heuristic had been written up under "What a
  language model made of it". `aure.nodes.evaluation` has left the adapter's
  contract.
- **The judge gets the skills its notes call for**, chosen as `nrw model new`
  chooses them, not every installed skill. On the reference sample the prompt
  went from about 230 KB to about 94 KB.
- **The stack-order rule is one text, `authoring.STACK_ORDER`, in both
  requests.** The endpoint had been told "ordered AMBIENT FIRST, SUBSTRATE LAST"
  for every sample, which `spec/geometry.py` reads as back reflection, while
  `--print-prompt` said the last entry is the medium the beam enters through.
  The `nrw-model-spec` skill, which goes into the same prompt, said "ambient
  first" and now says what the order means. `--print-prompt` also let the
  assistant edit only four keys while its step 5 asked for `constraints`.
- **The critical edge is given as the step it measures, and the substrate is
  the model's to add.** The back-reflection correction had read the scaffold's
  placeholder stack, so every sample was corrected as if it sat on silicon.
  The prompt now gives each edge as an SLD step, says what a step is, and in
  back reflection asks the model to add the SLD of the substrate the notes
  name. The sentence that quoted one sample's numbers into every
  back-reflection prompt ("on this sample it turns 4.28 into 6.35 ... the
  notes said only 'THF'") is gone.
- **Skill triggers match words, not substrings.** "cu" had chosen copper physics
  for "curve" and "thin" thin-layer degeneracy for "nothing". A trailing `*`
  marks a stem (`deuterat*`). "film" is no longer a polymer trigger: every
  sample is a film, and it chose polymer physics for a copper one.
- **`--budget quick` refines once** (the entry above).
- **Two smaller ones.** The agent prompt's no-skills fallback named
  `nrw skills install`, which does not exist; it says `nrw skills sync`, and a
  test now checks every command the prompt names against the CLI. And `nrw
  isaac export`'s per-state "No condition found" sat inside the branch for a
  state that had one, so it could never fire.

The design, security and test reviews of those fixes then changed more. The
two that matter most outside this list:

- **A verdict is filed as one inert line** (`notes.inert`): no newline, no
  control character, no comment marker. The generated fence in NOTES.md ends at
  the first close marker, so a reply carrying one and a newline made what
  followed count as the scientist's own note -- in `nrw ls`, on the fit page,
  and in the ISAAC record's notes. Reproduced, then fixed in `read_verdict` and
  again in `as_markdown`. AuRE's path had the same gap.
- **The template's comments are not the scientist's words** (`notes.
  strip_comments`). The scaffolded `sample.md` puts its guidance in comments,
  with examples ("~30 nm ionomer ... on 20 nm copper ... in D2O"), and
  matched as notes they chose all four contextual skills for every sample that
  kept them -- the default. They no longer choose skills or reach a prompt.

Also from the reviews: element symbols trigger in their own case (`Cu` is in
CuO and Cu2O, not in "cues"); an unreadable skill is skipped and named rather
than aborting `nrw assess`; the negation look-back and `extract_json`'s fence
search run in linear time (21 s and 19.5 s on crafted 160 KB inputs before);
the generated model page (`spec/explain.py`) and the scaffold's stack comment
no longer say "ambient first, substrate last"; and `aure.llm.config` and
`aure.llm.providers` are in the adapter's contract, since every judge call now
goes through them.

**The substrate is not named in code.** The first fix had `named_substrates`
read it from the notes, through a table of substrate names shared with the
back-reflection patterns, and corrected the edge itself. That was withdrawn on
review: a table of names is never complete, and the model reads the notes and
knows a substrate's SLD. So the prompt gives the step and how to read it, the
patterns enumerate the words they always did, and `aure_adapter.substrate_sld`,
which only the correction used, is gone.

**Nor does anything else read the edge as the top layer's SLD.** With the
addition the model's to make, a skill that says otherwise sits in the same
request. `refl-bl4b-instrument`, sent with every one, said a model at 6.2 is
wrong against an edge of 4.3, which through silicon is backwards;
`metal-oxide-interfaces` called the edge "the topmost SLD"; and
`structural-hypothesis-ranking`, the `--print-prompt` instructions and the
scaffold's materials comment compared it with the top layer as it stood. Each
now says the edge measures a step. Claude Haiku, standing in for the endpoint
on the captured prompt with the old skills still in it, added silicon's 2.07
and proposed d8-THF at 6.35, so this removes a contradiction rather than a
failure anyone saw.

Each of 52 reintroduced bugs fails a test.
