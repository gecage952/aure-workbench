---
name: structural-hypothesis-ranking
description: >
  Enumerate and rank candidate structural models before fitting, and revise the
  list as evidence arrives, instead of iterating on whichever model was tried first.
  USE FOR: starting a new sample, deciding what to try when a fit will not
  improve, judging whether a model change is justified, deciding when to stop.
  DO NOT USE FOR: the mechanics of a single fit (see steady-state-corefinement)
  or whether a thin layer is resolvable (see thin-layer-degeneracy).
version: 2
metadata:
  facility: SNS
  instruments: [REF_L, BL-4B]
  techniques: [reflectometry, model-selection]
  tags: [hypothesis, model-selection, workflow, bic, ranking, strategy]
  source:
    repo: neutrons-ai/aure
    path: src/aure/skills/structural-hypothesis-ranking/SKILL.md
    adaptation: >
      Restructured into the v2 anatomy and shortened. The ranking criteria and
      the baseline hypothesis list are AuRE's. Rewritten around committed specs
      and fit records -- a ranked list is only useful if each entry leaves a
      result you can compare, which is what the provenance layer provides.
---

# Ranking structural hypotheses

## Overview

The failure mode this prevents: fit the first model that comes to mind, find it
imperfect, add a layer, find it better, add another, and finish with a model
nobody chose and nobody can defend.

The alternative is to write down the candidates **before** fitting, ranked by
prior plausibility, and work down the list. Each candidate becomes a committed
spec with its own fit record, so "we tried four models and this one won" is a
statement you can support rather than a recollection.

## When to Use

- Starting a new sample, before writing the first spec.
- When a fit will not improve and you are about to add a layer.
- When someone asks why the model has the layers it has.
- When a change is being justified by χ² alone.
- Deciding whether to stop.

## Process

### 1. Write the list before you fit

Rank by prior plausibility, using in order:

1. **Sample preparation.** What was deposited, in what order, and what has it
   been exposed to since. This dominates.
2. **Contrast.** Can the measurement even distinguish these candidates? Two
   models that differ only where there is no contrast are one hypothesis.
3. **Resolution.** `2π/Q_max` — about 30 Å here. Candidates differing only below
   that are not separable (see `thin-layer-degeneracy`).
4. **Parsimony.** Fewer layers first, always.

Baseline candidates almost always worth listing for this beamline:

- the nominal stack as deposited;
- the nominal stack **plus a native oxide** on any air-exposed metal;
- the nominal stack **plus an interfacial layer** at the substrate — SiO₂, or
  intermixing with the adhesion layer;
- for anything in solvent: the film **solvated**, with SLD between dry and
  ambient;
- for a film in solvent: a **graded** interface rather than a sharp one.

Candidates to avoid unless something specific justifies them: more than one
sub-30 Å layer, layers with SLDs no available material has, and any layer added
solely because χ² improved.

### 2. Record the list where it will be read

In `sample.md`, before the first fit:

```markdown
## Fits to perform

1. Cu / Ti / Si, no oxide          -- baseline, 4 free
2. + CuOx (native)                 -- most likely; sample saw air
3. + SiO2 at the substrate         -- if 2 leaves residual at high Q
4. CuOx solvated (rho free to 6.4) -- if 2 leaves residual at low Q
```

That is the hypothesis list, it is version-controlled, and `nrw sample scan`
leaves it alone.

`sample.md` holds the *intent* — what you set out to try. The **outcome** of
ranking them is a finding about the sample, so it goes in
`samples/<id>/reports/`, citing the fit id of each candidate:

```bash
nrw note --sample <id> --title "ranking the oxide hypotheses" -m \
  "Candidate 2 wins: BIC 1003 against 5602 for candidate 1 (fits
   20260807-141425Z-0f8ffbf5 and 20260807-142039Z-5bd5314d). Candidate 3
   was never tried -- no residual at high Q to motivate it."
```

Keeping the two apart matters: a plan that has been overwritten with its own
results is no longer a record of what you expected, which is the half that
tells you whether you were surprised.

### 3. One spec per candidate

```bash
nrw model new Sample6 --name cu-baseline
nrw model new Sample6 --name cu-with-oxide
```

Each is committed, each gets a fit record, each has a hash. This is why variants
are separate specs rather than a `USE_OXIDE = True` toggle: a toggle means the
file that produced a result does not determine the result, and `nrw fit run`
would record the same script hash for both settings.

### 4. Compare on more than χ²

```bash
nrw ls
nrw diff <fit_a> <fit_b>
```

`nrw diff` separates the three reasons two fits differ — model, data, or
optimizer settings — which a χ² column cannot.

Then ask, in order:

- **Is the extra structure physically available?** A layer of a material that
  was never present is not a candidate however well it fits.
- **Did the complex model actually optimise?** Check for railed parameters
  before believing a rejection (`thin-layer-degeneracy`).
- **Are the added parameters determined?** A layer whose thickness has an
  uncertainty as large as itself has not been measured, whatever it did to χ².
- **Is the improvement in the residuals somewhere meaningful?** A χ² drop
  concentrated in three high-Q points is noise.

### 5. Revise the list when evidence arrives

The list is not fixed at intake. Specific evidence promotes a candidate:

- `nrw data features` reporting a critical edge whose implied SLD contradicts
  the top layer, once read as the step it is (plus the substrate's SLD in back
  reflection) → promote a reinterpretation of the ambient.
- `nrw tnr assess` reporting `oscillatory -> thickness change` → promote
  thickness-change hypotheses over contrast-change ones.
- A systematic residual at one Q range → promote a candidate that has structure
  at the corresponding length scale.

When evidence contradicts an accepted model, rewind to the last candidate
consistent with everything, rather than patching the current one.

### 6. Stop deliberately

Stop when the next candidate is not resolvable, when the added parameters would
not be determined, or when several candidates fit comparably. That last one is a
result: report the ambiguity instead of resolving it by preference.

Promote the chosen one so the choice is recorded with a reason:

```bash
nrw promote <fit_id> --as final --reason "oxide required; baseline leaves 3-sigma residual at 0.08"
```

## Rationalizations

**"Lowest χ² wins."** χ² always improves with parameters. The question is
whether the parameters are determined and physically available.

**"I'll enumerate after I see how the first fit goes."** Then the list is shaped
by the first result, which is the bias this exists to avoid.

**"BIC picked it, so it's justified."** BIC compares best achievable fits. Check
the complex model was optimised before trusting a rejection.

**"Adding a layer always helps, so the model must be incomplete."** Adding a
layer always helps *χ²*. Whether it helps the model is a different question.

**"Two models fit equally well, I'll take the simpler."** Reasonable as a
default, but say the other fits too. A reader deciding whether to trust a
thickness needs to know it was one of two.

## Red Flags

- A model with layers nobody can trace to a preparation step.
- A `sample.md` with no hypothesis list, and a `models/` directory with six specs.
- A structural change justified only by a χ² number.
- Layers accumulating monotonically across a fitting session — the signature of
  patching rather than choosing.
- A final result whose spec differs from every candidate written down at intake,
  with nothing recording why.
- Two comparable fits where only one was promoted and the other is unmentioned.

## Verification

```bash
nrw ls                      # every candidate tried, with its chi-squared
nrw diff <a> <b>            # model vs data vs settings
nrw check                   # nothing stale, nothing hand-edited
```

Three checks before calling a model final:

1. **Every candidate in `sample.md` has a fit record, or a note saying why not.**
   An untried candidate is fine; an untried candidate nobody mentions is not.
2. **The promoted fit has a reason** naming the evidence, not just the χ².
3. **The runner-up is reported** when it fits comparably. That is a statement
   about what the measurement could determine, and it belongs in the paper.
