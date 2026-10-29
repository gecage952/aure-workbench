---
name: refl-bl4b-instrument
description: >
  Apply the SNS REF_L (BL-4B) geometry, resolution and filename conventions
  when reading, checking or modelling reduced data from this beamline.
  USE FOR: building a probe, choosing a resolution convention, decoding a
  reduced filename, deciding whether two segments should agree, judging whether
  runs may be co-refined across a detector move.
  DO NOT USE FOR: the physics of reflectivity itself (see
  neutron-reflectometry) or the nrw-model/1 schema (see nrw-model-spec).
version: 2
metadata:
  facility: SNS
  instruments: [REF_L, BL-4B]
  techniques: [reflectometry]
  tags: [bl4b, refl, instrument, geometry, resolution, filenames, direct-beam, normalisation]
---

# REF_L (BL-4B) instrument conventions

## Overview

REF_L is the liquids reflectometer at the SNS. It measures a reflectivity
curve in **several angle settings**, each reduced separately against its own
direct beam, and it writes them as plain ASCII with conventions that are not
self-describing. Getting any of those conventions wrong produces a fit that
converges to a wrong answer rather than one that fails.

The four that matter:

| | Convention here |
|---|---|
| Angles | chosen per experiment — **read from each file's header**, never assumed |
| Resolution | **angular-only**: `dT = dQ/Q · tan(θ)`, `dL = 0` |
| `dQ` column | **FWHM today** — stated in the column titles, and read, never assumed |
| Normalisation | one direct-beam run per segment, recorded only in the template XML |

The `dQ` row is the one that is going to change: the reduction intends to move
from FWHM to sigma. It says which it wrote on the column-title line, so read
that line rather than carrying the convention in your head — see step 2.

## When to Use

Reach for this when you are:

- building a probe from a reduced file and need to know what the fourth column
  means;
- deciding whether two angle segments *ought* to agree, and what it means when
  they do not;
- decoding `REFL_218386_2_218387_partial.txt`;
- co-refining runs from different beamtimes, where the detector may have moved;
- setting a starting `theta_offset` or `sample_broadening`.

Not for the physics of reflectivity, and not for the spec schema.

## Process

### 1. Build the probe with angular-only resolution

The fourth column is `dQ`, and **the file says whether it is FWHM or sigma** —
read that before using it. `make_probe` wants FWHM, so a sigma column must be
multiplied by 2.355 first. Convert to the angular divergence refl1d wants and
set the wavelength spread to zero:

```python
import numpy as np
from refl1d.probe import make_probe


def create_probe(data_file, theta):
    q, data, errors, dq = np.loadtxt(data_file).T
    wl = 4 * np.pi * np.sin(np.pi / 180 * theta) / q
    dT = dq / q * np.tan(np.pi / 180 * theta) * 180 / np.pi
    dL = 0 * q  # angular-only: BL-4B's standing convention
    return make_probe(
        T=theta,
        dT=dT,
        L=wl,
        dL=dL,
        data=(data, errors),
        radiation="neutron",
        resolution="uniform",
    )
```

`dL = 0` is a decision, not an approximation nobody noticed. An earlier
moderator-spread option computed the wavelength term as
`delta_wl_over_wl(wl) * q` — multiplying by `q` rather than `wl`, which is
dimensionally wrong — and it is retired. **Fits made under the two conventions
are not numerically comparable**; re-run rather than compare.

For a *combined* file, `load4(data_file, FWHM=...)` is correct and simpler — but
the flag has to match the file, not the habit. Use the per-segment form when you
want `theta_offset` or `sample_broadening` to be fittable, which requires an
angle-based probe.

**Where the convention is written.** The column-title line states it:

```
# Q [1/Angstrom]        R                     dR                    dQ [FWHM]
```

Every reduction so far has written `FWHM`, and the intention is to move to
`sigma`. So this is read per file, at intake, and never defaulted:
`read_header(path).dq_convention` returns `"fwhm"`, `"sigma"`, or `None` when the
file does not say — a time-resolved slice has no header, and there the
convention must be declared rather than inherited. An unrecognised label raises
instead of falling back to FWHM, because a silent 2.355 is exactly the error
this whole skill exists to prevent.

`nrw data check` reports the convention per file, and `nrw model new` writes what
it read into `probe.dq_is_fwhm`. Runs whose files disagree cannot go in one
spec — that flag is a single boolean for the whole model.

### 2. Decode the filename

```
REFL_218386_2_218387_partial.txt
     ^run   ^seg ^subrun
REFL_218386_combined_data_auto.txt      all segments stitched by the reduction
r218389_sequence_3_eis_3.txt            one time-resolved slice
r218389_eis_reduction.json              the interval order, types and timestamps
```

The **first** number is the measurement; the third is the individual run that
produced that segment. They differ, and the segment run is the one the template
XML refers to.

For a time-resolved run the sidecar JSON is authoritative for ordering.
Filenames sort lexically, which puts `t001000` before `t000240`.

### 3. Check that the segments agree before modelling

Where two angle settings overlap in Q they measure the same sample, so they
must agree. Run:

```bash
nrw data overlap <sample>
```

A ratio significantly different from 1 is a **normalisation error**, not a
feature of the sample. It is invisible on a log-R plot, and a fit will absorb
it into a layer thickness or an SLD if you let it.

Two legitimate responses:

- re-reduce that segment against a better direct beam; or
- give it its own `probe.intensity` in the spec, so the fit accounts for it
  explicitly:

  ```yaml
  - {path: probe.intensity, value: 1.0, pm: 0.1, per: state}
  - {path: probe.intensity, range: [0.5, 1.1], per: measurement, in: [ocv1#2]}
  ```

  Measurement-level targeting outranks the state-level line.

### 4. Find out which direct beam normalised each segment

It is recorded **only** in `REF_L_<run>_auto_template.xml`, never in the
reduced ASCII:

```
run 218386 <- direct beam 218274
run 218387 <- direct beam 218275
run 218388 <- direct beam 218338      <- measured much later
```

Segments normalised against direct beams taken far apart are the ones that come
out on different scales. Different angles legitimately use different direct
beams, so this is where to look first — not a diagnosis on its own.

### 5. Check the geometry if the runs span beamtimes

The detector moved in on **2024-08-26** and back out on **2025-01-01**:

| Period | sample→detector | source→detector |
|---|---|---|
| 2014-10-10 → 2024-08-25 | 1830 mm | 15750 mm |
| 2024-08-26 → 2024-12-31 | 1355 mm | 15282 mm |
| 2025-01-01 → | 1830 mm | 15750 mm |

```python
from nr_workbench.instrument.geometry import geometry_on, spans_a_change

geometry_on("2025-04-15").sample_detector_mm  # 1830.0
spans_a_change("2024-08-01", "2024-09-01")  # True
```

Reduced data already has the geometry baked in, so this matters when you are
recomputing an angle from pixel positions, or sanity-checking a resolution.
Co-refining runs from either side of a move is not automatically wrong, but it
should be a decision you made rather than one you did not notice.

### 6. Motor and log paths, for raw NeXus

```
entry/DASlogs/BL4B:Mot:thi.RBV/value        incident angle
entry/DASlogs/BL4B:Mot:ths.RBV/value        sample angle
entry/DASlogs/BL4B:Mot:tthd.RBV/value       detector two-theta
entry/DASlogs/BL4B:Mot:xi.RBV/average_value slit position
entry/start_time                            for the geometry lookup above
entry/bank1_events/event_time_offset        TOF, microseconds
```

nr-workbench does not process raw events. Compute a theta offset separately
and record the number.

## Rationalizations

**"The segments overlap fine visually."** On a log-R plot spanning six decades,
a 27% step is a barely perceptible kink. Run the check; it reports 14.8σ.

**"I'll let the fit sort out the scale."** It will — by moving a thickness or
an SLD. The intensity and the structure are degenerate over a limited Q range,
and the fit has no way to know which one you meant.

**"dQ is dQ."** It is FWHM in some reductions and sigma in others. A factor of
2.355 in the resolution broadens or sharpens every fringe, which the fit
compensates for with roughness.

**"REF_L always writes FWHM, so I can skip the check."** It always has, and it
is not going to keep doing so — the move to sigma is intended. A convention you
carry in your head is one that silently stops being true, and this one stops
being true without a single fit failing. Read the column title.

**"I'll use the moderator resolution, it's more physical."** The retired
implementation was dimensionally wrong, and the whole group's fits use
angular-only. A more physical resolution that nobody else uses makes your
numbers incomparable with everyone else's.

**"Different beamtimes, same instrument."** Between 2024-08-26 and 2025-01-01
the detector was half a metre closer.

## Red Flags

- A fitted `probe.intensity` far from 1 with no per-segment override in the
  spec — the fit is absorbing a normalisation error.
- `R > 1` on anything but the lowest-angle segment.
- A `theta_offset` fitting to the edge of its range: usually a real
  misalignment, worth computing properly rather than absorbing.
- Fringe contrast mismatched between model and data with nobody having compared
  two overlapping segments at equal Q (see `sample-broadening`) — the fit is then
  damped by whichever parameter is cheapest, not by the mechanism that is
  actually damping the data.
- A roughness quoted with a tight interval on a fit whose fringe contrast was
  never checked. A resolution error and an interfacial width damp fringes with the
  same `exp(−cQ²)` form; the fit cannot tell them apart and will not say so.
- Co-refined runs whose dates straddle 2024-08-26 or 2025-01-01.
- A combined file and its own partials used in the same fit: the same neutrons
  counted twice.

## Verification

```bash
nrw data check <sample>       # per-file validation
nrw data overlap <sample>     # segment consistency, exits non-zero if bad
nrw data features <file>      # critical edge -> implied SLD, fringes -> thickness
```

Three specific checks:

1. **Critical edge.** `nrw data features` reports `Qc` and the SLD it implies,
   which is a step: the SLD of the medium that reflects the beam totally, less
   that of the medium the beam enters through. From air, compare it with that
   medium's SLD in your model. Through the substrate, add the substrate's SLD
   first — d8-THF measured through silicon gives a step of about 4.3, which is
   no solvent. If they still disagree, the model is wrong or the sample is not
   what you think.
2. **Segment ratios within 3σ**, or an explicit per-segment intensity saying
   why not.
3. **`chi2` over a tNR reference block ≈ 1.** If it is 5, the uncertainties in
   the reduced files are wrong and every significance you quote is inflated.
