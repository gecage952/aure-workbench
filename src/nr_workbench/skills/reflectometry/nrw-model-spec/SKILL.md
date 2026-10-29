---
name: nrw-model-spec
description: >
  Write an nrw-model/1 spec: one layer stack, any number of steady-state
  states and time-resolved series, with the parameter sharing stated rather
  than hand-coded.
  USE FOR: creating or editing a model, deciding how a parameter should be
  shared, expressing multi-angle co-refinement, debugging a validation error.
  DO NOT USE FOR: choosing a constraint form for a time series (see
  tnr-functional-constraints) or reflectometry physics (see
  neutron-reflectometry).
version: 2
metadata:
  facility: SNS
  instruments: [REF_L, BL-4B]
  techniques: [reflectometry, time-resolved]
  tags: [model, spec, schema, corefinement, refl1d, yaml]
---

# Writing an nrw-model/1 Spec

## Overview

A spec declares a model; `nrw model generate` writes the refl1d script. You
never write the Python — that is deliberate. A generated script can be checked
against the spec, and hand-written co-refinement is where the errors live: the
reference this replaces is 343 lines, about 200 of them copy-pasted parameter
tying, with another user's home directory baked into one variant and a
`# DON'T FORGET TO UPDATE THE DATA DIRECTORIES ABOVE` comment at the bottom.

The equivalent spec is 62 lines and produces a numerically identical problem.

Three ideas carry the design.

**One stack, many states.** Declare the layers once; states instantiate them.

**Parameter identity is the primitive.** Every slot — `(state, measurement,
attribute)` — declares *which parameter object* it reads, with `per`:

| `per` | one parameter per… | use for |
|---|---|---|
| `model` | the whole problem | a solvent SLD that cannot change |
| `state` | each state or series | most structural parameters |
| `measurement` | each angle segment or time slice | per-segment normalisation |

**Within a state, angle segments share automatically.** `per: state` means one
parameter that every segment of that state reads. You never write the aliasing,
and that default alone removes ~180 lines from the reference.

**A series is a state with a time axis.** Adding `series:` and `constraints:`
is what makes time-resolved data first-class.

## When to Use

Whenever creating or changing a model. Also when a validation error is unclear
— the messages point at the rule, and the rules are here.

## Process

### 1. Start from the sample

`nrw model new <SAMPLE> --name <model>` scaffolds a spec from `sample.yaml`.
Or copy [assets/example-corefine-tnr.yaml](assets/example-corefine-tnr.yaml),
which is the full worked case: two OCV states, three angle segments each, and
fifteen tNR slices constrained between them.

### 2. Declare the stack, the medium the beam enters through last

```yaml
materials:
  THF: {rho: 6.2}
  Cu:  {rho: 6.3}
  Si:  {rho: 2.07}
stack:                                # the beam enters through the last entry
  - {name: THF, material: THF, thickness: 0,   roughness: 25}
  - {name: Cu,  material: Cu,  thickness: 500, roughness: 5}
  - {name: Si,  material: Si}
```

refl1d takes the **last** entry as the medium the beam enters from, so the order
is the measurement geometry. This cell is measured through the silicon (back
reflection), so silicon is last, with no thickness or roughness, and the THF
behind the film is first, at thickness 0. A film measured from the air side is
the other way round: substrate first, air last. Backwards, the fit converges
and reports a chi-squared in the hundreds; `nrw model validate` checks the order
against the measured critical edge.

The thickness and roughness here are the **starting values**. `range` says
where a parameter may go; the stack says where it starts.

### 3. Set the probe convention

```yaml
probe: {resolution: angular_only, dq_is_fwhm: true}
```

- `dq_is_fwhm` — **a measured property of the reduction, not a default.** Every
  reduction written so far puts FWHM in the 4th column, and there is an
  intention to move to sigma. The two differ by 2.355, and a resolution wrong by
  that factor does not raise: the fit absorbs it into roughness and reports a
  confident wrong interface width. The convention is stated in the column-title
  line — `# Q [1/Angstrom]  R  dR  dQ [FWHM]` — so `nrw model new` parses it and
  writes the value it read, and `nrw data check` reports it per file. Treat it
  like `theta`: read, never tidied.
- `resolution: angular_only` derives dT from dQ at the known incident angle and
  sets dL = 0. It is the only supported convention.
- `dq_scale` multiplies the dQ column by a constant, for a reduction whose
  resolution estimate is **demonstrably** wrong — measured against something,
  not guessed to improve χ². Default 1.0.

**`dq_scale` is fixed, not fitted, and that is refl1d's constraint rather than a
preference.** `Probe.parameters()` exposes five knobs — `intensity`,
`background`, `back_absorption`, `theta_offset`, `sample_broadening` — and
`Probe.dQ` is a property derived from a fixed array plus the broadening. There is
nothing to bind a fitted scale to.

To **fit** a resolution, use `probe.sample_broadening` with `per: measurement`.
Because dθ = f·tanθ is a constant dQ/Q, one broadening per angle *is* a fitted
relative resolution. Use `dq_scale` for the different case where you have
established the reduction's number is wrong and want the correction recorded in
the spec rather than absorbed by a fit.

Where a total-reflection plateau exists, the observed roll-off before Q_c
measures the resolution directly, so it is the thing to establish `dq_scale`
against.

### 3b. Cut data that no parameter can absorb

Some data is wrong in a way the model cannot express. The case that matters here:
a segment whose required scale varies *across its own wavelength band*, typically
at the short-λ edge where the direct-beam spectrum is weakest. `probe.intensity`
is one number per segment, so it cannot follow that — and left in, the fit pays
for it with a thickness or a roughness.

```yaml
trim:
  # applies to every measurement
  - {lambda_min: 3.5, reason: "direct-beam spectrum unreliable below 3.5 A"}
  # narrows the blanket cut for one segment; later entries win field by field
  - {in: [run226649#0], q_max: 0.022,
     reason: "required scale drifts 15% across this segment's band"}
```

- **`reason` is required.** A cut nobody explained is indistinguishable from a
  mistake six months later.
- **State a band-edge cut in λ, not Q.** It is a property of the wavelength, and
  one λ cut maps to a different Q in every segment: λ = 3.5 Å is Q = 0.023 at
  0.37° and Q = 0.219 at 3.5°. In λ it is right everywhere; in Q it is right once.
- The cut lives in the spec, so it is hashed with it, appears in the generated
  script, and shows up as a reduced point count in `nrw model preview`. Dropping
  data is never invisible.
- A trim that keeps no points **raises** rather than handing refl1d an empty probe.

**This is not a way to improve χ² by deleting the disagreeing points.** Establish
that the data is wrong first — the overlap with a neighbouring segment, or a
required scale that varies within one segment — and write that evidence into
`reason`. Re-reducing is better than trimming whenever it is available.

**Runs whose files disagree cannot be co-refined.** `dq_is_fwhm` is one boolean
for the whole spec, so a set mixing conventions would have half its resolution
wrong by 2.355 with nothing in the record to show it. `nrw model new` refuses,
rather than picking. Fit each convention as its own spec.

Some older hand-written scripts instead computed dL from the SNS moderator
emission-time polynomial as `delta_wl_over_wl(wl) * q` — multiplied by q rather
than wl, which is dimensionally wrong. A spec asking for `resolution: moderator`
is rejected with an explanation. **Fits made under that convention are not
numerically comparable with these** (the same model gives χ² 101.983 there
against 101.994 here), so re-run rather than compare.

### 4. Declare the measurements

```yaml
states:
  - {name: ocv1, run: 218386, segments: auto, data_dir: samples/Sample6/data/steady}
series:
  - name: tnr
    run: 218389
    reduced_dir: samples/Sample6/data/tnr/218389
    time_from: reduction_json          # or `filename` for r<run>_t<sec>.txt
    select: {labels: ["sequence_*_eis_*"]}
```

`segments: auto` is every segment of the run in `data_dir` — either reduction's
file names — and fails if one is ambiguous or missing from the middle, which is
the check that replaces the DON'T-FORGET comment.

**No angle is written, and none is assumed.** Each is read from its own file's
header; a series' from its run's summed dataset in `data/steady`, because the
slices carry none. `thetas` (one per segment, `null` for any read from its file)
or a series' `theta` is only for a file that records no angle — `nrw model new`
leaves those `null`, for you to fill in, and validation refuses the spec until
you do. An angle you state for a file that records its own is checked against
it and refused if they disagree. A misalignment belongs in `probe.theta_offset`,
not in `thetas`.

### 5. Declare the parameters

```yaml
parameters:
  - {path: THF.rho, range: [4, 7], per: model}          # never changes
  - {path: Cu.thickness, range: [400, 600], per: state, in: [ocv1, ocv2]}
  - {path: probe.intensity, value: 1.0, pm: 0.1, per: state}
```

Paths are `Layer.attr` where `attr` is `thickness`, `roughness`, `rho` or
`irho`, plus `probe.{intensity, background, theta_offset, sample_broadening}`.
Note `roughness`, not refl1d's `interface`.

`in:` restricts to named groups. It also accepts a **single measurement** as
`group#index`, and a measurement-level declaration outranks a group-level one:

```yaml
  - {path: probe.intensity, value: 1.0, pm: 0.1, per: state}
  - {path: probe.intensity, range: [0.5, 1.1], per: measurement, in: [ocv1#2]}
```

That says "the segments share a normalisation, except the 3.5° one" — a real
pattern, and exactly what the hand-written reference does.

### 6. Constrain the series

See `tnr-functional-constraints`. In short:

```yaml
constraints:
  - series: tnr
    form: linear_in_index
    from: ocv1
    to: ocv2
    paths: ["*.roughness", CuOx.thickness, Ti.rho]
```

### 7. Check before you fit

```bash
nrw model validate spec.yaml    # paths, layers, bounds, missing files, DOF
nrw model preview  spec.yaml    # the parameter table and how it is grouped
nrw model preview  spec.yaml --build   # also builds it and reports chisq
nrw model generate spec.yaml
```

`preview` is the one that saves time: it says *47 free parameters, 1183 data
points* before you spend an hour on DREAM.

## Instrument nuisance parameters

Four probe parameters, and the reason each exists is that leaving it out does
not remove its effect -- it moves the effect into a layer.

| Path | Range | Absorbs | Cost of omitting |
|---|---|---|---|
| `probe.intensity` | `value: 1.0, pm: 0.1` | incident-beam normalisation | a layer thickness compensates |
| `probe.background` | `[0.0, 1.0e-5]` | flat additive background | high-Q points drag the fit |
| `probe.theta_offset` | `[-0.02, 0.02]` | sample misalignment | shifts Q; becomes a thickness error |
| `probe.sample_broadening` | `[0.0, 0.15]` **`per: measurement`** | aperture-limited divergence at small angles, curvature, mosaic | damps fringes; every interface reads rougher |

Ranges other than the broadening one are AuRE's defaults, which this beamline's
reductions already assume. See `sample-broadening` for why that one is both wider
and scoped differently.

**`theta_offset` and `sample_broadening` are partials-only.** Both describe the
*incident angle*, and a combined file has already been stitched across several
of them, so there is no single angle to offset or broaden. They require states
with `segments: auto`; `nrw model validate` rejects them on a `kind: combined`
state.

**Scope `theta_offset` by asking whether the sample physically moved.**

`theta_offset` is a property of *how the sample sits in the beam*. So the question
is not "did the sample change?" but "was it remounted?".

For an in-situ cell measured continuously — a sample under potential, an OCV
before and after, a tNR run in between — the sample never moves. There is one
alignment for the whole experiment, so:

```yaml
parameters:
  # the sample was mounted once and never touched
  - {path: probe.theta_offset, range: [-0.02, 0.02], per: model}
  # aperture-limited divergence is a property of the ANGLE, not the mounting:
  # large at 0.37 deg, near zero at 3.5 deg. See `sample-broadening`.
  - {path: probe.sample_broadening, range: [0.0, 0.15], per: measurement}
  # normalisation is genuinely per state: each reduction used its own direct beam
  - {path: probe.intensity, value: 1.0, pm: 0.1, per: state}
```

**`sample_broadening` does not follow that rule.** It is the exception in this
table: on BL-4B the dominant contribution is a beam-definition/aperture effect
that only bites at very small incident angles, so it belongs to the *angle*, not
to the mounting. Because refl1d adds ω to the divergence, one shared ω becomes
`dQ/Q = (dθ_nominal + ω)/tan θ`, which diverges as θ → 0 — so a single value
over-smears the lowest-angle segment or under-smears the rest, and no choice of
it is right for all three. Scope it `per: measurement` unless you have
independent reason to think the cause is sample curvature or a pressed window,
which really are one width for every angle.

**Use `per: angle`, not `per: measurement`, when the states share their angles.**
The aperture effect belongs to the incident angle, so on a three-state ×
three-angle co-refinement `per: angle` is three parameters where
`per: measurement` is nine of the same physical quantity:

```yaml
  - {path: probe.sample_broadening, range: [0.0, 0.15], per: angle}
```

Angles are grouped with a tolerance, because each theta is read from its own
file: 0.37 and 0.3698 are one setting, and 1.2002 and 1.2001 are another. The
parameter is named for the nominal angle — `0.37deg` — so the key does not move
when another state joins the fit.

Reach for `per: measurement` only when you have reason to think the same angle
behaved differently in different runs, e.g. the sample was remounted mid-series.
Measuring one thing nine times is how a nuisance starts absorbing structure.

Fitting `theta_offset` `per: state` on a sample that never moved is worse than
cosmetic. It is several free parameters describing one physical quantity, and
they will absorb real structural differences between the states — the very
thing the experiment is trying to measure.

Use `per: state` only when the sample was **physically remounted or
realigned** between measurements: taken out of the cell, moved to a different
holder, re-aligned after a beam trip.

```yaml
  # sample remounted between the dry and wet measurements
  - {path: probe.theta_offset, range: [-0.02, 0.02], per: state, in: [dry, wet]}
```

`probe.intensity` is the exception that is nearly always `per: state`: each
angle segment was normalised against its own direct beam, and those genuinely
differ.

Declaring `sample_broadening` at all changes the resolution path refl1d takes,
so a spec with it at 0.0 is not identical to one without it. Add it when there
is a reason, not by default.

## Rationalizations

| Excuse | Rebuttal |
|---|---|
| "Editing the generated `.py` is quicker than editing the spec." | It is, once. Then the spec and the script disagree, `nrw check` flags it, and you are back to not knowing which file is real. Edit the spec, or `nrw model fork` to take ownership properly. |
| "I'll add a `USE_OXIDE = True` flag so one file covers both cases." | A toggle inside a model means the file no longer identifies what it produced. Two specs, two hashes, two records. The schema deliberately cannot express it. |
| "I'll set `per: measurement` everywhere, it's the most flexible." | It multiplies your parameter count by the number of segments and lets the fit absorb noise. `per: state` is right for structure; reach for `measurement` only where the physics differs — as it does for `sample_broadening`, where the cause depends on the incident angle. |
| "One `sample_broadening` for the whole sample is the physical choice." | Only if the cause is the sample. On BL-4B the dominant cause is aperture-limited divergence at small angles, and one shared value cannot represent it: `dQ/Q = (dθ + ω)/tan θ` is a different number at every angle. |
| "The validator complains a constraint anchors on a constant — I'll just fix the bounds." | Read it again: it means the endpoint state has no free parameter for that path, so the interpolation would run between two fixed numbers and freeze every slice. That looks like a working fit and is not one. |
| "`post_build` is easier than working out the schema." | It is supported, but every use is flagged, and it is a report that the schema is missing something real. Say what you needed. |
| "I'll copy the spec and change a number for the variant." | Good — that is the intended workflow. Two files, two hashes, two sets of results. |

## Red Flags

- A path both listed in `parameters` and named in a constraint's `paths` for
  the same measurement. Rejected, because whichever ran last would silently win.
- `linear_in_index` on a series whose slices are unevenly spaced in time.
- A parameter with no `range`, `pm`, or `fixed` — rejected, because it would be
  silently frozen.
- `resolution` changed from what earlier fits on this sample used.
- `probe.dq_is_fwhm` set to anything nobody traced to a file's column titles.
- `probe.sample_broadening` declared `per: model` on multi-angle partials with no
  stated reason to think the cause is sample curvature.
- Free parameters approaching a tenth of the data-point count.
- A generated `.py` edited by hand; `nrw model generate` refuses to overwrite it.
- An absolute path anywhere in the spec.
- A `trim:` whose `reason` restates the bound (`"cut below 3.5"`) instead of the
  evidence for it.
- A band-edge cut expressed in `q_min`/`q_max` across several angles — the same
  physical cut is a different Q in each, so at most one of them is right.
- `dq_scale` moved to improve χ² rather than to match a measured resolution.

## Verification

- [ ] `nrw model validate` reports no errors.
- [ ] `nrw model preview` shows the parameter count and grouping you intended —
      particularly that segments share what they should.
- [ ] `nrw model preview --build` reports a finite starting chi-squared.
- [ ] The generated script's `_check_links()` passes: `python <model>.py`.
- [ ] `resolution` and `dq_is_fwhm` match the convention used for earlier fits.
- [ ] Any constraint's `from`/`to` states have that path free.
