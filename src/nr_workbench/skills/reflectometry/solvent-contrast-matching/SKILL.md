---
name: solvent-contrast-matching
description: >
  Get the ambient and solvent SLD right, detect a wrong isotope from the data,
  and design or read a contrast-variation series.
  USE FOR: setting an ambient SLD, diagnosing an unexplained critical edge or
  low-Q upturn, choosing a D/H ratio to match a layer, checking whether a
  stated solvent matches the one that was measured.
  DO NOT USE FOR: solid-layer materials (see metal-oxide-interfaces or
  polymer-films) or resolution conventions (see refl-bl4b-instrument).
version: 2
metadata:
  facility: SNS
  instruments: [REF_L, BL-4B]
  techniques: [reflectometry, contrast-variation]
  tags: [solvent, contrast, deuteration, sld, isotope, ambient, d2o, thf]
  source:
    repo: neutrons-ai/aure
    path: src/aure/skills/solvent-contrast-matching/SKILL.md
    adaptation: >
      Restructured into the v2 anatomy. The SLD table and isotope-confusion
      logic are unchanged. Pointed at `nrw data features` for the critical-edge
      check and at the aure_adapter for SLD lookup and match ratios.
---

# Solvents, contrast, and getting the isotope right

## Overview

The ambient is a layer like any other, and it is the one most often wrong in a
model — not because it is hard, but because it is assumed. A protonated and a
deuterated solvent differ by ~6 × 10⁻⁶ Å⁻², which is larger than the contrast
of most things being studied. Getting it wrong does not produce a bad fit; it
produces a good fit to a different sample.

**Always suspect unspecified deuteration.** If a sample description says "in
THF" and the fit wants an ambient near 6.3, it was d8-THF.

## When to Use

- Setting up any measurement in a liquid.
- A critical edge or low-Q upturn the model cannot explain.
- A fitted ambient SLD far from the stated solvent's.
- Designing a contrast series, or deciding what a second contrast would buy.
- Interpreting a layer whose SLD sits between the dry material and the solvent —
  it is probably solvated.

## Process

### 1. Know the numbers

| Solvent | H-form | D-form | D-form name |
|---|---|---|---|
| Water | −0.56 (H₂O) | 6.36 (D₂O) | D₂O |
| THF | 0.18 | 6.35 | d8-THF |
| Toluene | 0.94 | 5.66 | d-toluene |
| Cyclohexane | −0.28 | 6.68 | d12-cyclohexane |
| Ethanol | −0.34 | 6.00 | d6-ethanol |
| Methanol | −0.37 | 5.80 | d4-methanol |

Units are 10⁻⁶ Å⁻². For anything not in the table:

```python
from periodictable import formula, neutron_sld

neutron_sld(formula("Cu2O"), density=6.0)[0]   # 5.36
```

`periodictable` ships with refl1d and does the physics; you supply the density.
It has densities for elements only, so any compound needs one stated — which is
the honest situation, since the density is what you are assuming.

### 2. Check the ambient against the data, not the label

`nrw data features <file>` reports the critical edge and the SLD it implies:

```
    critical edges
      Qc = 0.01467   implied SLD 4.279   (high)
```

For a front-reflection measurement, a critical edge appears when the ambient SLD
is **below** the layer beneath it. In back-reflection through silicon
(SLD 2.07), an edge at low Q says something above the substrate has SLD > 2.07.
The implied SLD is the step from the silicon, so the 4.279 above is a solvent at
4.279 + 2.07 = 6.35.

If the stated solvent is protonated THF (0.18) and the fitted ambient comes back
near 6.3, the sample was in d8-THF. That is not a fit problem to be tuned away —
it is the data telling you what was in the cell.

### 3. Fit the ambient SLD, unless it is air

Give the ambient a range spanning both isotopes on the first fit of a new
sample:

```yaml
materials:
  THF: {rho: 6.2}
parameters:
  - {path: THF.rho, range: [-1.0, 7.0], per: model}
```

Where it settles tells you what you measured. Once you know, tighten the range
to the known value ± a little — **keep it a `parameters:` entry**, not a
number pinned in `materials:` with nothing in `parameters:` to free it — and
note the tightened range in `sample.md`. A permanently wide ambient is a free
parameter absorbing other people's errors; a permanently *fixed* one is a
claim that the book value is exactly what was in the cell.

Book values assume a pure, fully isotoped liquid at a stated temperature.
What was actually in the cell rarely matches: atmospheric H/D exchange,
isotopic purity short of 100%, a run temperature different from the one the
value was tabulated at, dissolved gas, trace carryover from the cell or a
previous sample. Each moves the real SLD by a few tenths — small next to the
~6-unit gap that told you which isotope it is, not small next to the contrast
of whatever layer sits against it. A range of ±0.3–0.5 around the book value
survives all of that; a pinned number asserts none of it happened.

**Air is the one ambient worth fixing.** Its SLD really is 0 to the precision
reflectometry reaches, so there is no equivalent uncertainty to fit away.

### 4. Choose a match point deliberately

To make a layer invisible, match the solvent SLD to it:

```python
from nr_workbench.aure_adapter import contrast_match_ratio

contrast_match_ratio(2.07)  # 0.38 -> 38% D2O matches silicon
```

The layer contributes nothing at its match point, which is how you isolate
everything else. A contrast series is most informative when the points bracket
the layer you care about rather than clustering at the extremes.

### 5. Read a solvated layer for what it is

A polymer with a dry SLD of 1.5 measured in D₂O (6.36) that fits to 3.5 is not a
different polymer. It is roughly

```
φ_solvent ≈ (ρ_fit − ρ_dry) / (ρ_solvent − ρ_dry) = (3.5 − 1.5) / (6.36 − 1.5) ≈ 0.41
```

about 41% solvent by volume. Report the volume fraction, which is the physical
quantity, rather than an SLD nobody can interpret.

## Rationalizations

**"The label says THF."** The label says what was intended. The critical edge
says what was measured. When they disagree, the data wins.

**"The ambient fitted to 6.3 instead of 0.18, but χ² is fine."** χ² being fine
is the problem. The model found a consistent story about the wrong sample.

**"It's clearly D₂O/H₂O, so I'll pin the SLD at the book value."** Knowing the
isotope is not the same as knowing the SLD. Purity, H/D exchange with the
atmosphere, and temperature all move the real value by a few tenths, and
fixing it pushes that error into a layer thickness where it is much harder to
see. Fit it with a tight range around the book value instead — unless the
ambient is air, which has no equivalent uncertainty to fit away.

**"Contrast matching means the layer disappears from the fit."** It disappears
from the *signal*. It is still in the model, and its thickness is now
unconstrained — do not report the number the fit prints for it.

**"Mixed solvent, so I'll interpolate SLD linearly by mass."** Interpolate by
**volume** fraction. Mass and volume fractions differ enough to matter at the
precision reflectometry reaches.

## Red Flags

- A stated protonated solvent with a fitted ambient above 5.
- An ambient SLD fitted outside the H-form to D-form range of the stated solvent.
- A critical edge in the data with no critical edge in the model.
- A layer SLD sitting between its dry value and the solvent's, reported as if
  it were the material's SLD.
- A contrast series whose points all sit on one side of the layer being studied.
- An ambient with a wide free range in a fit being quoted as final.
- An ambient other than air pinned in `materials:` with no `parameters:` entry
  freeing its `rho` — a solvent SLD that was never checked against the data.

## Verification

```bash
nrw data features <file>        # the critical edge and its implied SLD
```

Four checks:

1. **Implied SLD from `Qc` against the stated solvent.** They should agree to
   better than a few tenths.
2. **Fitted ambient against the table above.** Within ~0.2 of an H- or D-form
   value, not floating between them — a value halfway between suggests either
   a mixed solvent you did not declare or a fit absorbing an error.
3. **Any solvated layer converted to a volume fraction** and sanity-checked:
   between 0 and 1, and consistent with what the film should do in that solvent.
4. **The ambient's `rho` is a `parameters:` entry, not a number pinned in
   `materials:`** — unless the ambient is air.
