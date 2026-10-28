---
name: analysis-provenance
description: >
  Keep every result traceable to the script, data and environment that produced
  it, and know how to answer "what made this figure?".
  USE FOR: running a fit, deciding whether a result can be cited, tracing a
  figure, marking a result final, responding to a stale-result warning.
  DO NOT USE FOR: the project directory layout (see nr-workbench-project) or
  reflectometry physics (see neutron-reflectometry).
version: 2
metadata:
  facility: SNS
  instruments: [REF_L, BL-4B]
  techniques: [reflectometry]
  tags: [provenance, reproducibility, fitting, records, workflow]
---

# Analysis Provenance

## Overview

This project exists because a previous folder of scripts made it impossible to
say which one produced a published figure. The machinery that prevents a repeat
is simple and it only works if you go through it.

Every fit writes an **immutable directory** containing:

```
results/<fit_id>/
  manifest.json   what ran, with chi-squared and settings
  model.py        the script exactly as executed
  inputs.json     every input file with its sha256
  env/            exact package versions, and a git patch if the tree was dirty
  fit/            the bumps output
  figures/        stamped with the fit_id, so they survive being copied out
  NOTES.md        yours: what this run was for and what it showed
```

`fit_id` is a UTC timestamp plus a hash of the run's identity, so it sorts
chronologically *and* identifies its content — two runs of the same script on
the same data share a suffix.

## When to Use

Whenever you run a fit, cite a result, or are asked where a number came from.
Read it before running your first fit in a project.

## Process

### 1. Run fits through `nrw`

```bash
nrw fit run samples/<id>/models/<name>.py --method dream --samples 5000 \
    --seed 12345 --note "why this run exists"
```

Hand-written scripts work as they are — no migration, no schema. The script
must define a module-level `problem = FitProblem(...)`.

`nrw` observes which files the script actually opens, so inputs are recorded
exactly even when the path is built at runtime from a run number.

### 2. Expect an identical re-run to be recorded as a replicate

If nothing changed — script, inputs, settings, environment — `nrw` says so,
names the existing result, and records the new run as a replicate of it.
Before re-running, check whether the result you want is already there.

### 3. Trace anything with `whence`

```bash
nrw whence figures/fig3.svg      # a figure, even one copied out of the project
nrw whence samples/S4/data/steady/REFL_230597_combined_data_auto.txt
nrw whence <fit_id>
```

On a **data file** it lists every fit that consumed it — the "this file changed,
what do I need to redo?" direction.

### 4. Treat a STALE result as unusable

A result is stale when an input's sha256 no longer matches what the fit
consumed. `nrw ls` marks it, `nrw whence` says so in red, and `nrw check` exits
non-zero.

Stale means the number on the plot was computed from bytes that are no longer
on disk. Re-run before citing it. Do not reason about how much the data
"probably" changed.

### 5. Say what the run was for, while you still know

```bash
nrw assess <fit_id>                # the automatic checks, into its NOTES.md
nrw note <fit_id> -m "what you were testing and what you now believe"
```

The record captures what ran. It cannot capture why you ran it, what you
expected, or what you would warn a reader against concluding — and those are
the parts that decay fastest. Do it for the fits you abandon too: "the oxide
went to zero thickness, so this parameterisation is unusable" is a result.

A note is prose, and prose is enough. The only convention is to name fit ids
when you mean them, which is what links a note to a fit.

### 6. Mark the answer explicitly

```bash
nrw promote <fit_id> --as final --reason "converged; SLD band excludes null"
```

Latest is not final and lowest chi-squared is not automatically final — a
person decides, and the reason is the part worth keeping. Promotion is refused
on a stale fit. Superseding an earlier decision records both; the history of
what was once considered final is provenance too.

### 7. Every derived number gets a script

A fit record accounts for the fit. It accounts for nothing you compute **afterwards**
— and a report's headline numbers are usually that arithmetic, not a value read out of
a `.par` file.

**If a number is not a direct quote from a fit record, it needs a script in
`samples/<id>/reports/`, and the report must name that script.** No exemption for
"it's just a subtraction".

| Derived quantity | Why it is a choice, not a readout |
|---|---|
| A change `Δ = end − start`, with an interval | Must come from the **joint posterior**. Differencing two medians and adding errors in quadrature overstates the uncertainty whenever the endpoints are correlated — by ~2x at r = 0.85. |
| A stoichiometry from an SLD | Depends on scattering lengths, an assumed number density, and a conservation assumption. |
| An areal quantity (`rho x t`) | Often the only combination the data determine when the factors are degenerate — and differencing the factor instead of the product can flip a conclusion. |
| BIC or any model comparison | `nrw` reports chi-squared and `n_free`, never BIC. `n`, `k` and the chi-squared convention all have to be stated. |
| A significance in sigma | Which spreads were combined, and how. |
| A correlation coefficient | Which fit's chain, and which two parameters. |
| Anything from a header or timestamp | Timezone, and which file. |

**State your uncertainty convention once and compute every derived number with it.**
bumps' `-err.json` reports the 68% credible half-width; the moment standard deviation
of the draws is a different number, and for a heavy-tailed posterior the two differ by
up to 2x. Mixing them shifts quoted significances by ~15%, which is invisible unless
one script owns all of them.

Rules that keep such a script honest:

- **Read only from `results/<fit_id>/`,** with the fit_ids as constants at the top, so
  the script states which fits it depends on.
- **Never hardcode a value that came from a fit.** Load it. A hardcoded number is how a
  report keeps quoting a superseded result after the fit is re-run.
- **A value a fit held fixed comes from that fit's own `spec.yaml`**, not from
  recomputing it out of the upstream fit's posterior — the two drift apart.
- **Print the formula next to the value.** A reader must be able to check the algebra
  without reading the code, and so must you.
- **Compute from the draws** (`*-point.mc.gz`), not from summary statistics, wherever
  the quantity is non-linear or its inputs are correlated.
- Physical constants are hardcoded, but sourced in a comment and taken from
  `periodictable` where it can supply them.

This script is part of the provenance package. It is the answer to "where did that
number come from?" in the way the fit record is the answer to "where did this
chi-squared come from?" — so it belongs beside the report, not in a notebook or a
terminal transcript.

### 8. Check before you commit or publish

```bash
nrw check
```

### 9. Package it before you send it

```bash
nrw pack <fit_id>
```

Sending a result directory sends a description of a fit, not a fit: it records
the hashes of its data rather than the data. A bundle carries the measurements
at the paths the frozen script expects, and a `verify.py` that applies the
recorded parameters and checks chi-squared. Refused when an input has drifted
— a bundle asserts that its data produced its result.

A bundle covers the fit. If the result being sent includes derived numbers, send
`reports/` alongside it — the bundle can reproduce the chi-squared but not the
arithmetic layered on top of it (§7).

## Rationalizations

| Excuse | Rebuttal |
|---|---|
| "I'll run bumps directly, it's faster." | It is a few seconds faster and produces a result nobody can trace. Every shortcut here is how the previous repo happened. |
| "I'll record the provenance afterwards." | Afterwards the environment has moved and the data may have been re-reduced. The record is only true at the moment it is written. |
| "The data only changed slightly, the fit is still basically right." | Then re-running is cheap. "Basically right" is not a claim the hashes can support, and STALE is not a warning to reason around. |
| "I'll just edit the generated script, it's quicker than the spec." | Use `nrw model fork`. It gives you a hand-owned script that still has a full record. Escaping the generator must not mean escaping provenance. |
| "`--reason` is obvious from the chi-squared." | Chi-squared says which fit was numerically best, not why it is the answer. The reason is what a reader in a year needs. |
| "I'll re-run with `--force` to get a cleaner number." | Running until you like the answer is not a method. If a re-run is warranted, say why in `--note`. |
| "It is a subtraction, I did it in my head." | Then nobody can check it — and if the endpoints are correlated it is also wrong, because a difference of correlated parameters needs the joint posterior rather than two medians. Write the script (§7). |
| "The derived number is in the report, that is the record." | The report says what the number is, not how it was obtained. Nobody can re-derive a stoichiometry from the stoichiometry, including you in six months. |
| "I will paste the fitted values into the script so it runs standalone." | Then it is a transcript, not a derivation, and it keeps reporting the old answer after the fit is re-run. Load from `results/<fit_id>/`. |

## Red Flags

- A figure in a report that `nrw whence` cannot resolve.
- A result cited while `nrw ls` shows it STALE.
- A `results/` directory edited after the fact.
- `nrw check` failing and the failure worked around rather than fixed.
- A promotion with a reason like "best fit" or "final".
- Several fits with the same run key, none marked as replicates.
- A fit whose record lists no data inputs — the script read nothing, or read it
  in a way that could not be observed. Either way the record is incomplete.
- A number in a report that no script produces and no fit record contains.
- A `Δ` whose uncertainty was propagated by hand from two correlated endpoints
  instead of taken from the joint posterior.
- A derived-quantities script with a fitted value typed into it rather than loaded.
- Two `±` conventions in one report — an interval half-width in one table and a
  moment standard deviation in another.

## Verification

Before citing or sharing a result:

- [ ] `nrw check` exits zero.
- [ ] `nrw whence <figure>` names a fit, and its inputs are FRESH.
- [ ] The fit is promoted, with a reason that says why rather than what.
- [ ] `env/versions.json` records the versions, and a patch is present if the
      tree was dirty.
- [ ] Every number in the report is either a direct quote from a fit record or is
      produced by a named script in `reports/` (§7).
- [ ] That script prints its formulae, loads every fitted input rather than
      hardcoding it, and names the fit_ids it depends on.
- [ ] One uncertainty convention, stated, used in every derived significance.
- [ ] Any hand-owned script is a registered fork, not an edited generated file.
