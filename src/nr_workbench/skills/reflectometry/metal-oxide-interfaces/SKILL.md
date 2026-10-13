---
name: metal-oxide-interfaces
description: >
  Model metal films, their native oxides and their adhesion layers without
  inventing structure the data cannot support.
  USE FOR: deciding whether an oxide layer belongs in the model, setting
  starting values for Cu/Ti/Cr/Au stacks, diagnosing an adhesion layer whose
  parameters drift, interpreting an oxide that grows or dissolves in a series.
  DO NOT USE FOR: judging whether a thin layer is resolvable at all (see
  thin-layer-degeneracy, which comes first) or solvent SLD (see
  solvent-contrast-matching).
version: 2
metadata:
  facility: SNS
  instruments: [REF_L, BL-4B]
  techniques: [reflectometry, electrochemistry]
  tags: [metal, oxide, copper, titanium, adhesion, cuox, electrode, sld]
  source:
    repo: neutrons-ai/aure
    path: src/aure/skills/metal-oxide-interfaces/SKILL.md
    adaptation: >
      Restructured into the v2 anatomy. The SLD tables and the
      add-an-oxide/don't rules are unchanged. The electrochemical-series
      section is new -- the Cu/CuOx work on this beamline is mostly about
      watching an oxide change under potential, which a static framing misses.
---

# Metal films, their oxides, and their adhesion layers

## Overview

A sputtered metal electrode on silicon is almost never just metal on silicon.
There is an adhesion layer under it, and if it has seen air there is a native
oxide on top. Both are thin, both sit in the degenerate regime, and both are
where a fit puts errors it cannot put anywhere else.

The recurring stack on this beamline:

```
ambient (D2O, d8-THF, air)
CuOx        20-80 A     native oxide, or an electrochemically grown one
Cu          200-1000 A  the electrode
Ti or Cr    20-60 A     adhesion layer
Si                      substrate
```

## When to Use

- Any metal film on silicon, which is most samples here.
- Deciding whether to add an oxide layer.
- An adhesion layer whose SLD or thickness wanders between fits.
- A tNR series where the oxide is the thing changing.
- A metal SLD that fits well below its bulk value.

## Process

### 1. Know the numbers

| Material | Bulk SLD (10⁻⁶ Å⁻²) | Density used |
|---|---|---|
| Cu | **6.55** | 8.96 |
| CuO (tenorite) | **6.46** | 6.31 |
| Cu₂O (cuprite) | **5.36** | 6.00 |
| Cu(OH)₂ | 2.46 | 3.37 |
| Ti | −1.91 | 4.506 |
| TiO₂ | 2.63 | 4.23 |
| Cr | 3.03 | 7.19 |
| Au | 4.66 | 19.3 |
| SiO₂ | 3.47 | 2.196 |
| Si | 2.07 | 2.329 |

Computed from CRC bulk densities and coherent scattering lengths, not copied
from a table — several published quick-reference tables for the copper oxides
disagree with each other and with this.

**Two things in that table matter more than the rest.**

*Stoichiometric CuO is nearly contrast-matched to copper*: 6.46 against 6.55.
A dense, fully-oxidised CuO layer is close to invisible in a neutron
measurement of a copper electrode. If you are looking for one and see nothing,
that is a plausible reason — not evidence it is absent.

*Cu₂O is the one you can see*, at 5.36 against copper's 6.55. And a real native
oxide is porous and hydrated rather than bulk-dense, which drops it further:
Cu₂O at 80% of bulk density is 4.29. So a fitted CuOx anywhere in **4.2–5.5**
is a physically ordinary cuprous oxide, and the width of that range is
porosity, not measurement error.

Recompute for anything else rather than trusting a table:

```python
from periodictable import formula, neutron_sld

neutron_sld(formula("Cu2O"), density=6.0)[0]   # 5.36
```

`periodictable` ships with refl1d and does the physics; you supply the density.
It has densities for elements only, so any compound needs one stated — which is
the honest situation, since the density is what you are assuming.

A fitted metal SLD well below bulk means porosity, roughness being absorbed, or
solvent ingress — not a different metal.

### 2. Decide whether the oxide belongs there

**Add one** when the sample has been exposed to air (nearly always), when the
metal is one that oxidises readily (Cu, Ti, Al), when the fit leaves a
systematic residual at mid Q, or when an electrochemical step should have grown
or reduced one.

**Do not add one** when the film was made and measured without air exposure,
when the added layer collapses to its minimum thickness *and* nothing adjacent
is railed at a bound, or when you are adding it only because χ² improved
slightly — two extra free parameters will always improve χ² slightly.

The middle case is the trap, and it is `thin-layer-degeneracy`'s subject: a
layer collapsing while a neighbour rails is a failed optimisation, not evidence
of absence.

### 3. Start it somewhere plausible and bound it physically

```yaml
materials:
  CuOx: {rho: 5.0, irho: 0.0}
stack:
  - {name: CuOx, material: CuOx, thickness: 60, roughness: 20}
parameters:
  - {path: CuOx.rho,       range: [4.0, 6.5], per: model}   # cuprous, porous to dense
  - {path: CuOx.thickness, range: [10, 120],  per: state, in: [ocv1, ocv2]}
  - {path: CuOx.roughness, range: [3, 33],    per: state, in: [ocv1, ocv2]}
```

That range runs from a porous cuprous oxide up to just under bulk copper. A
CuOx that fits at the top of it is not necessarily an oxide: bulk CuO (6.46)
and copper (6.55) are barely distinguishable, so a value up there means either
dense cupric oxide or no oxide at all, and reflectivity alone will not tell you
which.

### 4. Treat the adhesion layer as a nuisance, not a result

Ti and Cr layers are 20–60 Å — below the resolution limit, buried under a much
thicker metal, and contributing little. They are still worth including, because
leaving them out pushes their contrast into the substrate roughness.

Tie them across states. The adhesion layer is under the electrode; nothing an
experiment does to the surface can change it:

```yaml
  - {path: Ti.thickness, range: [25, 60], per: state, in: [ocv1, ocv2]}
```

If it drifts substantially between two states of the same physical sample,
something else in the model is wrong and the adhesion layer is absorbing it.
That is a diagnostic, not a discovery.

### 5. Read an oxide in a time series

This is what the Cu/THF work is about. In a tNR run, three things distinguish
the plausible interpretations:

- **The oxide thickening or thinning** gives an oscillatory change template in Q
  with node spacing `ΔQ`, implying a thickness change of order `π/ΔQ`. That is
  what `nrw tnr assess` reports as `oscillatory -> thickness change`.
- **The oxide changing composition** at constant thickness gives a one-sign
  template — an SLD contrast change.
- **Solvent penetrating** the oxide moves its SLD toward the ambient, which also
  reads as a contrast change but in a specific direction.

Model the first as a thickness varying between the two steady states:

```yaml
constraints:
  - series: tnr
    form: linear_in_time
    from: ocv1
    to: ocv2
    paths: [CuOx.thickness, CuOx.roughness]
```

The endpoints are the steady-state parameters, so this adds no free parameters.

## Rationalizations

**"χ² improved, so the oxide is real."** Two free parameters always improve χ².
Ask whether the improvement is worth the parameters, and whether the complex
model was optimised properly before you compare.

**"The oxide fitted to 2 Å, so there isn't one."** Check whether an adjacent
layer is railed at a bound first. A collapsed layer next to a railed neighbour
is a local minimum.

**"The Ti layer came out at 26 Å in one state and 45 Å in the other."** Nothing
reaches the adhesion layer through 500 Å of copper. Tie it, and find what it was
absorbing.

**"Cu fitted to 5.8, so it's partly oxidised throughout."** More often it is
roughness or porosity being absorbed into the SLD. Look at the roughness first.

**"I'll add both Cu₂O and CuO layers to be thorough."** Two adjacent layers with
SLDs 0.7 apart, both below the resolution limit, are not separable. You are
adding parameters that trade off against each other.

## Red Flags

- An oxide SLD above the parent metal's.
- An adhesion layer differing between states of one physical sample.
- A metal SLD more than ~10% below bulk with unremarkable roughness.
- An oxide thickness at a bound.
- A model with more sub-30 Å layers than the Q range can resolve — count them
  against `2π/Q_max`.
- An oxide added *and* a large increase in the metal's roughness: both are
  describing the same interfacial smearing.

## Verification

```bash
nrw data features <file>    # critical edge -> an SLD step; fringes -> thickness
nrw model preview <spec>    # the free-parameter count and bounds
```

Three checks:

1. **The critical edge measures an SLD step**, from the medium the beam enters
   through to the one that reflects it totally. For a metal in a solvent,
   measured through the substrate, the solvent is the step plus the substrate's
   SLD (2.07 for silicon); for a film in air, the step is the film's own SLD
   when it is thick enough to reflect, else the substrate's. If it disagrees
   with the model, the top of the stack is wrong.
2. **Total thickness from fringe spacing** against the sum of your layers. The
   estimate is often loose (`nrw data features` says when the uncertainty
   exceeds the value), but an order-of-magnitude disagreement is real.
3. **Every oxide fit at least twice**, from different starting SLDs, per
   `thin-layer-degeneracy`. Compare with `nrw diff`.
