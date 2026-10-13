# Getting started: a full Cu/THF sequence, start to finish

This walks through a complete analysis on real REF_L data — two steady-state
measurements either side of an electrochemistry sequence, co-refined with the
time-resolved run measured *during* it.

| | Run | What it is |
|---|---|---|
| **ocv1** | 218386 | OCV before the EIS sequence, 3 angle segments |
| **tnr** | 218389 | 15 slices measured during the sequence, 0.6° |
| **ocv2** | 218393 | OCV after the sequence, 3 angle segments |

Every command and every number below was produced by running it. Timings are
from a laptop; the whole thing takes about five minutes, of which fifteen
seconds is the fit.

> **Prerequisite.** `curl -fsSL https://raw.githubusercontent.com/neutrons-ai/aure-workbench/main/install.sh | sh`
> (Windows: `irm https://raw.githubusercontent.com/neutrons-ai/aure-workbench/main/install.ps1 | iex`;
> from a checkout: `pip install -e .`). Run `nrw doctor` afterwards — it reports
> every dependency plus the resolved AuRE commit, which matters because we pin a
> SHA on AuRE's `main`: two installs can report the same version and be different
> code.

---

## 1. Make the project

```bash
mkdir -p ~/beamtime && cd ~/beamtime
nrw init cu-thf-expt11 --beamtime apr2025 --ipts IPTS-34347
cd cu-thf-expt11
```

```
Scaffolded .../cu-thf-expt11
  for        Claude Code, GitHub Copilot
  create   74 file(s)
```

`nrw init` is safe to run in a directory that already has files in it, and safe
to run twice — it never overwrites a file you have edited. Those files are the
layout, the editor config, and 19 reflectometry skills with dispatcher agents
for each assistant the project is set up for, so an assistant opened in this
folder already knows REF_L conventions.

The second line says which assistants those are. Claude Code and GitHub Copilot
are the default. `--harness` sets the whole list, once per assistant:
`nrw init --harness claude --harness copilot --harness opencode` adds OpenCode
to the two, and `nrw init --harness opencode` alone sets up OpenCode only. The
choice is recorded in `nrw.toml`, so later runs keep it without the flag.

One of them is `.nrw/schema/nrw-model-1.json`, the JSON Schema for a model spec,
generated from the code rather than written by hand. `.vscode/settings.json`
points every `samples/*/models/*.yaml` at it, so a mistyped key is underlined in
the editor rather than surfacing as a validation error at fit time. Re-run
`nrw init` after upgrading nr-workbench to refresh it — `nrw doctor` says so if
it is missing or stale.

Check the environment:

```bash
nrw doctor
```

```
  ✓ refl1d        1.0.1
  ✓ bumps         1.0.4
  ✓ aure          1.0.1 @ d6e38e481fcf
  ✓ instrument    SNS REF_L
  ! samples       none yet; run `nrw sample new <ID>`
  ✓ skills        19 installed: analysis-provenance, analyst-handoff, ...
```

## 2. Make the sample and bring the data in

```bash
nrw sample new Sample6 --title "Cu/Pt in d8-THF, 1 M LiBF4 (expt 11)"
```

> **Measuring right now?** The Experiment page in `nrw serve` does this step
> for you. It watches the reduction's output folder, lets you assign each run to
> a sample, and copies the data in only once a run has finished arriving. Its
> **Settings** page sets the IPTS and the data folder if `nrw init` was run
> without `--ipts`; the one-time link `nrw serve` prints opens it. See
> [experiment.md](experiment.md). The manual route below is what it automates
> — and it is still the route for a sliced time-resolved series like this
> walkthrough's, which the page does not handle yet
> ([Not yet](experiment.md#not-yet)).

Copy the reduced ASCII in. Raw NeXus is gitignored; reduced data is small and
belongs in the repository with the analysis.

```bash
SRC=~/git/experiments-2025/apr2025/data

cp $SRC/steady/REFL_218386_{1,2,3}_*_partial.txt samples/Sample6/data/steady/
cp $SRC/steady/REFL_218393_{1,2,3}_*_partial.txt samples/Sample6/data/steady/

mkdir -p samples/Sample6/data/tnr/218389
cp $SRC/tNR/reduced/r218389_*.txt            samples/Sample6/data/tnr/218389/
cp $SRC/tNR/reduced/r218389_eis_reduction.json samples/Sample6/data/tnr/218389/
```

**Copy the whole reduced directory, not just the slices you plan to fit.** The
sequence has 15 `eis` intervals and 101 short `hold` intervals. You will only
co-refine the `eis` ones, but the assessment coadds the leading holds into its
reference block, and 17 coadded holds are a much better baseline than 3 eis
slices: median `dR/R` drops from 0.050 to 0.035, and the peak significance of
the change rises from 26.5σ to 35.1σ. Leaving them out costs you resolution
you already paid beam time for.

> **Already have a beamtime directory?** `nrw import` reads the four layouts
> that exist in `experiments-2025` and maps them into this one. It **symlinks**
> data rather than copying it, so there is one copy of every byte and the
> original tree keeps working, and it writes nothing until you say `--write`:
>
> ```bash
> nrw import ~/git/experiments-2025/oct2025            # plan only
> nrw import ~/git/experiments-2025/oct2025 --write    # do it
> ```
>
> Outputs of previous fits are deliberately left behind — 310 files in
> apr2025, 527 in june2026. A `results/` directory here carries a manifest with
> input hashes, the environment and the exact command; a `.dat` from a 2025
> bumps run has none of that, and putting it where `nrw whence` promises an
> answer would fake provenance nobody recorded. Re-run the script through
> `nrw fit run` and you get the real thing in minutes.

Now register what actually arrived:

```bash
nrw sample scan Sample6
```

```
Sample6
  steady  218386  3 segment(s)
  steady  218393  3 segment(s)
  series  218389  15 slice(s) [labelled]
  ! data on disk for [218386, 218389, 218393], not mentioned in sample.md
  wrote samples/Sample6/sample.yaml
```

`sample.yaml` is the machine register — the scan maintains it. `sample.md` is
yours: what the sample is, what was done to it, what you expect. The tool never
edits it (the one exception is a sample the Experiment page manages: its
`sample.md` is written from the page, and its first lines say so). The `!` line
is it telling you the two disagree. Write the runs up in `sample.md` and the
warning goes away.

**`sample.yaml` is also how you co-refine a subset.** It is a normal file, and
`nrw model new` builds from it rather than from the disk. A beamtime directory
routinely holds alignment scans, aborted runs and other conditions that belong
to the sample without belonging to *this* model — delete those entries from the
register and the model leaves them out:

```bash
nrw sample scan Sample6          # refresh the register from disk
$EDITOR samples/Sample6/sample.yaml   # keep only what this model is about
nrw model new Sample6 --name subset
```

```
  note  using samples/Sample6/sample.yaml, which does not list run(s) 218400
        that are on disk.
        If that is deliberate, nothing to do. If the register is stale, run
        `nrw sample scan Sample6`.
```

It reports the difference rather than resolving it: a stale register and a
curated one look identical on disk, and silently re-adding the run would undo
a deliberate choice.

## 3. Check the data before modelling it

This step exists because both of the things it finds are invisible on a log-R
plot, and a fit will happily absorb either one into a layer thickness.

```bash
nrw data check Sample6
```

```
  ! samples/Sample6/data/steady/REFL_218386_1_218386_partial.txt
      R > 1 detected (max=1.188): data may not be normalized
  ! samples/Sample6/data/steady/REFL_218386_3_218388_partial.txt
      Negative R values detected
  ...
  4 of 6 file(s) have issues.
```

Both are normal for per-angle REF_L data: `R > 1` on the low-angle segment is a
normalisation the fit is expected to absorb, and negative R at the top of the
high-angle segment is the noise floor. Worth seeing, not worth stopping for.

Then the one that matters:

```bash
nrw data overlap Sample6
```

```
  run 218386
    #1 / #2    +1.80%  ( 1.4 sigma)   chi2  1.51   n=47   Q 0.0282-0.0354
  ! #2 / #3   +27.58%  (14.8 sigma)   chi2  1.70   n=30   Q 0.0819-0.0946

  run 218393
    #1 / #2    -0.17%  ( 0.1 sigma)   chi2  1.67   n=47   Q 0.0282-0.0354
    #2 / #3    -4.06%  ( 2.4 sigma)   chi2  0.88   n=29   Q 0.0819-0.0941
```

Where two angle settings overlap in Q they are measuring the same sample, so
they have to agree. Run 218386's 3.5° segment is **27.6% off, at 14.8σ** —
that is a normalisation error, not the sample.

This is worth dwelling on, because it is the reason this check exists. The
hand-written script this analysis replaces has, buried at line 243:

```python
# The third run has a different intensity, so we don't share that
# parameter with the first two runs.
```

Someone found this by eye and worked around it. `nrw data overlap` turns it
into a number with an error bar, before a model is written rather than after
one misbehaves. Section 5 shows how the spec says so explicitly.

If the reduction template is beside the data, the check also prints which
direct beam normalised each segment:

```
      direct beams: 218386<-218274  218387<-218275  218388<-218338
```

Segments 1 and 2 were divided by adjacent direct-beam runs; segment 3 by one
measured much later. Different angles legitimately use different direct beams,
so that is where to look first rather than a diagnosis on its own — but it is
the obvious suspect.

One more check worth knowing, on a single curve:

```bash
nrw data features samples/Sample6/data/steady/REFL_218386_3_218388_partial.txt
```

```
    fringes counted: 12
      total thickness  406 +/- 577.5   (medium)   <- uncertainty exceeds the value; not a constraint
      roughness        10.43   (low)
      layer count      3   (low)
```

Note what it says about the thickness. The number alone looks like something
you could seed a model with; the uncertainty says it constrains nothing. Over a
single angle segment there is not enough Q range to do better, which is itself
worth knowing before you trust a fringe-counting estimate.

## 4. Ask what the time-resolved run actually did

Before choosing how to model the change, find out whether there *is* one, and
what kind.

```bash
nrw tnr assess samples/Sample6/data/tnr/218389 \
    --label r218389 --out samples/Sample6/assessments/r218389
```

```
Loaded 116 intervals from samples/Sample6/data/tnr/218389
  eis: 15  hold: 101
  time range: 0.0 - 4241.0 s
  reference:  leading contiguous 'hold' block: 17 intervals, t=0.0-508.0 s
  median dR_ref/R_ref: 0.0350

  1. variogram: rising, knee near 578 s
  2. amplitude: a in [-0.1263, 1.1294], max |a/sigma| = 35.079
     chi2_res median 1.0357 (one template suffices)
     trajectory: monotonic
  3. template: oscillatory -> thickness change, order 598 A
  4. chi2 over the reference block: 1.0136   (should be ~1)

  verdict: changing; a single reaction coordinate describes it; a(t) is
  monotonic, so a linear-in-time constraint fits; the template oscillates
  in Q, so free a thickness
```

Read it in that order, because each line qualifies the next:

1. **variogram** — is anything changing at all? It needs no reference block, so
   it cannot be fooled by a bad choice of one. Rising means yes.
2. **amplitude** — how much, and how significantly. `a` is defined as 0 at the
   reference block and 1 at the late block. `chi2_res ≈ 1` is the important
   number: it says a *single* Q-shape describes the whole change, so one
   reaction coordinate is enough and you are not averaging two processes.
3. **template** — what kind of change. Oscillatory in Q means a **thickness**
   change; a one-sign template would mean an **SLD contrast** change instead.
4. **chi2 over the reference block ≈ 1** — a sanity check on the error bars. If
   this were 5, the uncertainties in the reduced files would be wrong and every
   significance above would be inflated.

The verdict is deliberately phrased as model advice: *monotonic* → use
`linear_in_time`, *oscillatory* → free a thickness. That is the loop from
assessment to spec, closed.

## 5. Write the model

Scaffold from what the scan found:

```bash
nrw model new Sample6 --name cu-thf-218389
```

That writes a valid spec with a placeholder stack and — because it read the
same data — the two states, the series, and a `linear_in_time` constraint
already wired between them. Replace the stack with the real one.

**The angles come from the files, and only from the files.** Every reduced
REF_L file carries a header recording its incident angle, and resolving the
spec reads it: 0.4500, 1.2010, 3.5003 for run 218386. So the spec `nrw model
new` writes has no angles in it at all — none to assume, and none to go stale.
It matters because theta sets the wavelength axis, `wl = 4π·sin(θ)/q`, so a
wrong angle is absorbed into roughness instead of raising.

Time-resolved slices carry no header at all — but the same run is *also*
reduced as a summed dataset into `data/steady`, and that file does. So the
series angle is read from there: **0.5997°** for run 218389, not the 0.6 anyone
would assume.

A file that records no angle is left **blank** — a `null` in `thetas`, named in
a comment at the top of the spec — and `nrw model validate` refuses the spec
until you give it. An angle you do give for a file that records its own is
checked against it.

That summed dataset is deliberately left out of the fit. It is the sum of the
very slices the series contributes, so including both would put the same
neutrons in twice. `nrw model new` says so when it skips it.

### Filling in the stack

What `nrw model new` writes is *facts*: which runs exist, which files belong to
them, each segment's measured angle. What it cannot know is the **stack** —
what the sample is made of, in what order, roughly how thick. That is in your
head and, if you wrote it down, in `sample.md`.

Three ways to close that gap. All three produce the same file; pick whichever
matches what you have to hand. The first two are `nrw model new` with a flag,
so use one *instead of* the plain command above — run after it, they stop at
the spec it already wrote, unless you add `--force` to replace it.

**1. An LLM endpoint, if you have one configured.**

```bash
nrw model new Sample6 --name cu-thf-218389 --from-notes
```

It reads `sample.md`, picks the skills your notes call for, sends the
skeleton and the measured critical edge, and merges back a proposed stack:

```
  asking openai/gpt-4o-mini (5 skill(s))...
  model notes: Oxide thickness is a guess; sample.md gives no value.
Wrote samples/Sample6/models/cu-thf-218389.yaml
```

The written file says who proposed it:

```yaml
# The stack below was PROPOSED by openai/gpt-4o-mini
# from sample.md and the project's skills. It is a starting point, not a
# measurement -- check every layer and range before fitting. States,
# series and angles were read from the data files and were not proposed.
```

**A model may only propose `description`, `materials`, `stack`, `parameters`
and `constraints`** — plus two choices your notes make and the files cannot:
`series_select`, which slices to model, and `probe.back_reflection`, which side
the beam enters. Anything it returns for `states`, `series`, `thetas`,
`data_dir` or `run` is discarded — those were read from the files and are
already exact. That filter is in the code, not just in the prompt: a wrong
angle would broaden every fringe and the fit would quietly absorb it into
roughness.

Configure it in `.env` — `nrw init` ships a `.env.example` listing every
variable, and `.env` itself is gitignored, which matters because a beamtime
directory gets shared, archived and sometimes published. Or set it on the
Settings page of `nrw serve`: its *Language model* section writes the same
file, makes one call to check it, and also offers **Claude through the Claude
Code CLI**, which needs no key ([experiment.md](experiment.md#language-model)).

```bash
cp .env.example .env      # then fill in one of the provider blocks
```

Settings are read from the shell first, then `.env`, then `~/.nrw`, then
`~/.aure` — so a machine already set up for AuRE works here with no extra
configuration. `nrw doctor` reports which of those it actually read, and shows
the key as a shape rather than a value:

```
  ✓ config        ~/.aure
  ✓ llm           local/gpt-5.4 @ https://.../openai/v1
  ✓ llm settings  LLM_API_KEY=set (32 chars, ...fa0b), LLM_MODEL=gpt-5.4, ...
```

**2. The coding assistant already open on the project.**

This is the common case in VS Code — Claude Code or Copilot can read every file
in the repo, so it needs an instruction rather than an endpoint:

```bash
nrw model new Sample6 --name cu-thf-218389 --print-prompt
```

That writes the skeleton and prints something you can paste straight in:

```
Fill in the model spec at samples/Sample6/models/cu-thf-218389.yaml for sample Sample6.

1. Read these skills first and follow them:
   - skills/reflectometry/nrw-model-spec/SKILL.md
   - skills/reflectometry/neutron-reflectometry/SKILL.md
   - skills/reflectometry/refl-bl4b-instrument/SKILL.md
   - skills/reflectometry/thin-layer-degeneracy/SKILL.md

2. Read samples/Sample6/sample.md for what the sample is and what was done to it -- and
   for each measurement: its condition in the Measurements table, and its
   notes under Measurement conditions. Take the ambient from them, never
   assume air. ...

3. Edit ONLY these parts of the spec:
     description, materials, stack, parameters

   Leave `states`, `series`, `thetas`, `data_dir`, `run` and `reduced_dir`
   exactly as they are. ...
```

It names the skills your notes actually call for, and tells you when a relevant
one is bundled but not installed:

```
  These bundled skills match your notes but are not installed here.
  Install them first so the assistant has them:
      nrw skills sync
      - metal-oxide-interfaces
      - solvent-contrast-matching
```

**3. By hand**, which is what the rest of this section shows. Worth doing once
even if you plan to use the other two, because reviewing a proposed stack is
much easier when you know what a good one looks like.

Whichever you use, the check is the same — and it is the reason none of this
is risky:

```bash
nrw model validate <spec>
nrw model preview  <spec> --build
```

A proposed stack that does not validate, or builds to a nonsense χ², is caught
in a second. Nothing is trusted because of where it came from.

The finished `samples/Sample6/models/cu-thf-218389.yaml`, in full:

```yaml
schema: nrw-model/1
name: cu-thf-218389
sample: Sample6

materials:
  THF:  {rho: 6.2}
  CuOx: {rho: 5.0, irho: 0.0}
  Cu:   {rho: 6.3}
  Ti:   {rho: -3.0}
  Si:   {rho: 2.07}

stack:                                      # measured through the Si, so it is last
  - {name: THF,  material: THF,  thickness: 0,   roughness: 25}
  - {name: CuOx, material: CuOx, thickness: 60,  roughness: 20}
  - {name: Cu,   material: Cu,   thickness: 500, roughness: 5}
  - {name: Ti,   material: Ti,   thickness: 40,  roughness: 10}
  - {name: Si,   material: Si}

probe: {resolution: angular_only, dq_is_fwhm: true}

states:
  # No angles: each is read from its file's header when the spec is resolved.
  - {name: ocv1, run: 218386, segments: auto, data_dir: samples/Sample6/data/steady}
  - {name: ocv2, run: 218393, segments: auto, data_dir: samples/Sample6/data/steady}

series:
  - name: tnr
    run: 218389
    reduced_dir: samples/Sample6/data/tnr/218389
    # its angle, 0.5997, is read from run 218389's summed dataset in data/steady
    time_from: reduction_json
    select:
      labels: ["*_eis_*"]        # assess on all 116, co-refine the 15

parameters:
  # structure shared across the whole problem
  - {path: Cu.rho,   range: [5.0, 7.0],   per: model}
  - {path: CuOx.rho, range: [1.0, 6.5],   per: model}
  - {path: Ti.rho,   range: [-4.0, -1.0], per: model}
  - {path: THF.rho,  range: [5.5, 6.4],   per: model}

  # structure, one value per steady state
  - {path: Cu.thickness,   range: [400, 600], per: state, in: [ocv1, ocv2]}
  - {path: CuOx.thickness, range: [10, 120],  per: state, in: [ocv1, ocv2]}
  - {path: Ti.thickness,   range: [25, 60],   per: state, in: [ocv1, ocv2]}
  - {path: Cu.roughness,   range: [1, 30],    per: state, in: [ocv1, ocv2]}
  - {path: CuOx.roughness, range: [3, 33],    per: state, in: [ocv1, ocv2]}
  - {path: THF.roughness,  range: [5, 40],    per: state, in: [ocv1, ocv2]}

  # nuisance: one intensity per state, shared across its angle segments...
  - {path: probe.intensity, value: 1.0, pm: 0.1, per: state}
  # alignment error, from the notes. Range is AuRE's default.
  - {path: probe.theta_offset, range: [-0.02, 0.02], per: state, in: [ocv1, ocv2]}
  # ...except ocv1's 3.5 deg segment -- the 27.6% that `nrw data overlap`
  # found. Measurement-level targeting outranks the state-level line above.
  - {path: probe.intensity, range: [0.5, 1.1], per: measurement, in: [ocv1#2],
     name: Intensity_386_3}
  - {path: probe.theta_offset, range: [-0.01, 0.01], per: state, in: [tnr]}

constraints:
  - series: tnr
    form: linear_in_time
    from: ocv1
    to: ocv2
    paths: [Cu.thickness, CuOx.thickness, Ti.thickness,
            Cu.roughness, CuOx.roughness, THF.roughness]
```

The spec says nothing about how it is fitted: that is `nrw fit run`'s
options, or the project's `nrw.toml` [fit] (see the README, "Fitting
options").

Four things in there are doing most of the work.

**Angle segments alias automatically.** `segments: auto` gives each state three
Experiments that share one set of structural parameters. You never write the
tying. The hand-written script this replaces is 364 lines, **52 of which** are
assignments of the form
`exp_list_ocv1[1].sample["THF"].interface = exp_list_ocv1[0].sample["THF"].interface`.
That is the single most common place to introduce a silent error: miss one and
two segments quietly fit independent values for a parameter that is physically
one number.

**`per:` is the whole parameter-identity system.** `per: model` is one value
for the entire problem, `per: state` is one per state, `per: measurement` is one
per slice. That is all there is to it.

**The 27.6% is declared, not absorbed.** `in: [ocv1#2]` targets one measurement
— the third segment of ocv1 — and gives it its own intensity with a wide range.
Measurement-level targeting outranks the state-level line above it. The
alternative is letting a copper thickness drift to compensate.

**The constraint adds no free parameters.** `linear_in_time` interpolates each
listed path from its ocv1 value to its ocv2 value using the real timestamps,
so the 15 tNR slices are described entirely by the two steady states. Time,
not index: the intervals are unequally spaced, so the two genuinely differ.

**Anchored endpoints, or fitted ones.** `from: ocv1, to: ocv2` is why the
constraint is free: it borrows parameters the steady-state data already
constrains. Write `free` for either endpoint to fit it instead:

```yaml
    from: ocv1        # anchored: the OCV measurement just before the run
    to: free          # fitted: where the sample actually finished
```

| | Cost | Use when |
|---|---|---|
| `ocv1` → `ocv2` | nothing | the states bracket the series and you trust them |
| `ocv1` → `free` | 1 per path | you know where it started; the end is the result |
| `free` → `free` | 2 per path | there is no bracketing state at all |

On this model that is 21 free parameters anchored, 27 with a fitted end, 33
with both. The steady states are longer counts over a wider Q range than any
single slice, so anchoring imports a much better constraint and is the right
default. Free an endpoint when there is no bracketing measurement, when
something happened between the steady measurement and the run, or when where
the sample finished is the number you are after rather than an assumption.

A free endpoint borrows its range from the path's `parameters` declaration — a
Cu thickness plausible for the steady states is plausible here too. Add
`endpoint_range: [min, max]` when there is no such declaration.

Check it before generating anything:

```bash
nrw model validate samples/Sample6/models/cu-thf-218389.yaml
nrw model preview  samples/Sample6/models/cu-thf-218389.yaml --build
```

```
  21 experiment(s) in 3 group(s)
    ocv1           3 measurement(s)
    ocv2           3 measurement(s)
    tnr           15 measurement(s), t = 507.962..4241.03 s

  21 free parameter(s), 90 constrained
    ...
    probe.intensity@ocv1#2           1   [0.5, 1.1]
    probe.theta_offset@tnr           0   [-0.01, 0.01]

  built ok: chisq 101.632, 5380 data point(s)
```

`--build` constructs the real refl1d problem in-process and reports the starting
χ². **21 free parameters describing 21 experiments and 5380 points** — that
ratio is the point of the whole exercise.

## 6. Generate and fit

```bash
nrw model generate samples/Sample6/models/cu-thf-218389.yaml
```

```
Wrote samples/Sample6/models/cu-thf-218389.py
      samples/Sample6/models/cu-thf-218389.md  (what it assumes)
```

Two files. The `.md` explains the model in English — what is being fitted, what
is held equal to what, what each instrument parameter absorbs, and every
assumption the fit makes without saying so:

> **`probe.sample_broadening`** — extra angular divergence beyond the
> calculated resolution, from sample curvature or mosaic. It damps the fringes,
> so leaving it fixed when it is real makes every interface look rougher than
> it is.
>
> **Layers below the resolution limit.** `CuOx` (60 Å). Reflectivity constrains
> a thin layer mainly through the product of contrast and thickness, so its SLD
> and thickness are individually poorly determined even when their product is not.

It is **derived from the spec, not written**, so it cannot describe a different
model than the one that will run — and `nrw check` reports it stale if the spec
moves on without it. That is the file to hand a collaborator, or to read
yourself in six months.

The `.py` is a standalone 778-line refl1d script. It runs under plain
refl1d/bumps with nr-workbench uninstalled, uses no absolute paths, and is
meant to be committed and read. It ends with runtime assertions that every
parameter which should be shared actually *is* the same object — the aliasing
mistake made structurally impossible rather than warned about.

```bash
nrw fit run samples/Sample6/models/cu-thf-218389.py \
    --method amoeba --steps 2000 --seed 1 \
    --note "first co-refinement of the full sequence"
```

```
  fit_id   20260805-191540Z-732f4286
  chisq    1.83
  free     21 parameter(s)
  output   samples/Sample6/results/20260805-191540Z-732f4286
```

**χ² 101.6 → 1.83, in fifteen seconds.**

For a production run use `--method dream --samples 100000 --burn 10000`, which
also gives you parameter uncertainties. Amoeba finds a minimum; it does not
tell you how wide it is.

Fits run on **all cores by default** (`--parallel 0`). bumps itself defaults to
one, which is worth knowing if you have ever wondered why a DREAM run took the
afternoon: on a 20-core laptop the same run drops from 169 s to 30 s. Population
fitters — `dream`, `de` — evaluate their whole population each generation and
scale well; `amoeba` is sequential and gains nothing. Use `--parallel 1` to
force serial, and `--parallel N` for a specific count when sharing a machine.

If the worker pool cannot start, the fit falls back to a single CPU and says
so, rather than losing the run.

bumps' own diagnostic PNGs are **off** by default. It draws them *before* it
saves the chain, one figure per model, so on a 21-model co-refinement the
rendering falls over and takes an hour of DREAM sampling with it. Nothing is
lost by skipping them: the uncertainties still land in `err.json`, the chain in
`point.mc.gz`, and `nrw serve` draws everything from the numbers. Pass
`--plots` if you want them anyway.

Instead, the fit writes `trajectory.json` — every layer parameter through time
with its credible interval, computed once while the inputs are still open
rather than on every page load.

## 7. Look at it

```bash
nrw serve
```

```
  cu-thf-expt11  ~/beamtime/cu-thf-expt11
  1 sample(s), 1 fit(s)

  http://127.0.0.1:8765/
  http://127.0.0.1:8765/experiment      the experiment's runs
  http://127.0.0.1:8765/settings        its IPTS, data folder, watcher and language model
  http://127.0.0.1:8765/api/overview    the same data as JSON
  ...
  To edit the experiment, curate fits or change the settings, open this link
  in your browser:
    http://127.0.0.1:8765/auth/<token>
  It works once, for one browser, and is kept out of the request log.
  Without it the pages are view-only.
```

`/s/Sample6` is the sample landscape: every steady-state segment as R(Q), the
tNR run as a time–Q map of fractional change, and a(t) beneath it on the **same
time axis**. Click or drag on either lower panel to move the time cursor and
that slice's R(Q) overlays onto the top panel, so you can see what the sample
looked like at the moment the change was happening.

`/f/<fit_id>` is the fit: data and model per experiment with a residual strip,
all 21 SLD profiles, the parameter table, and a provenance panel. Opened
through the link, it also curates: **Star** a fit worth coming back to,
**Finalize** it as the sample's answer, with the reason (what `nrw promote --as
final` does in the next step), or **Discard** it and, later, delete its files.
See [Curating fits](experiment.md#curating-fits).

Every page has a JSON twin under `/api/…`, which is what an assistant should
read rather than scraping HTML.

## 8. Make the result citable

```bash
nrw promote 20260805-191540Z-732f4286 --as final \
    --reason "chi2 1.83; segment normalisation handled explicitly"
nrw check
```

```
Checked 1 fit(s).
  no problems found
```

The result directory is immutable. It holds a frozen byte-identical copy of the
spec and the script, the sha256 of all 22 input files, the resolved versions of
python/refl1d/bumps/numpy *and the AuRE commit*, the git SHA, and the exact
command line. `NOTES.md` is the only file in there you should edit.

Ask any artifact where it came from:

```bash
nrw whence samples/Sample6/results/20260805-191540Z-732f4286/fit/cu-thf-218389.par
```

```
  -> fit 20260805-191540Z-732f4286
  sample     Sample6
  model      cu-thf-218389  (script)
  script     model.py  sha256 dbb85fa736a2
  fit        amoeba, 2000 steps, seed 1
  chisq      1.83   free 21
  versions   python 3.14.5rc1, aure 1.0.1, bumps 1.0.4, ... refl1d 1.0.1
  aure       d6e38e481fcf
  inputs     22 file(s), fresh
```

It works on more than fit outputs. Ask it about a *data* file and it lists every
fit that consumed it; ask about a figure the fit wrote into its `figures/`
directory and it identifies the fit even if that figure has since been copied
out of the project and emailed, because the fit stamps the figures there with
its `fit_id` when it finishes (PNG `tEXt`, SVG `<desc>`). A figure saved there
afterwards is not stamped.

`nrw check` is the guard: it verifies every input hash still matches, that no
generated script has been hand-edited, that none is stale against its spec, and
that every promoted pointer resolves. Run it before you call anything final, and
in CI.

## 9. Write down what you found

A fit records what ran. It cannot record why you ran it, what you expected, or
what you would warn a reader against concluding — and those decay fastest.

Start with the automatic checks:

```bash
nrw assess 20260805-191540Z-732f4286
```

The output below is from a different fit, `…0103d9c7`: a DREAM fit of the tNR
run alone, which the sample note further down explains. It is shown because
the interval checks read a DREAM posterior, and the amoeba fit above has none.

```
  fit       20260807-163359Z-0103d9c7
  chisq     1.285   BIC 1048.0
  free      8   points 3915

  warn    tnr218389 CuOx roughness's 95% interval reaches its bound even though
          the best-fit value does not. The posterior is pressed against the range.
  info    tnr218389 CuOx roughness's best-fit value 5.1104 lies outside its own
          68% interval [5.18437, 6.83067] — a skewed or multimodal posterior.
          Quote the median and the interval, not the point.

  written to samples/Sample6/results/.../NOTES.md
```

Those checks need no language model: they read the bumps problem for each
parameter's range, the `.par` file for where it landed, and the DREAM
`-err.json` for the posterior. A parameter on its bound is the bound you chose,
not a measurement. A 95% interval spanning most of its prior means the fit
handed your range back. With an LLM endpoint configured (`.env`), it also asks
whether the values are physically sensible given `sample.md` and the installed
skills — labelled as a model's opinion, because it has not seen the data.

Then add the sentence no check can write:

```bash
nrw note 732f4286 -m "Ti.rho is conditional, not measured: the rho/thickness
degeneracy was broken by fixing t, so the interval reflects that choice."
```

**Two kinds of note, two homes.** The above attaches to one run. A finding
about how several fits *relate* belongs to the sample:

```bash
nrw note --sample Sample6 --title "why the tNR is fitted alone" -m \
  "The 76-minute gap after the last eis slice means the endpoints cannot be
   tied to the steady states: see 20260807-163359Z-0103d9c7."
```

That lands in `samples/Sample6/reports/`. **Naming a fit id in the prose is the
whole linking mechanism** — no frontmatter required. `nrw ls` marks which fits
have been reasoned about, the fit page shows every report that cites it, and
`nrw pack` carries them to your collaborator.

```
  20260807-163359Z-0103d9c7  cu-thf-tnr-reduced  ok  1.285  fresh
      settings: samples 100k -> 200k; chisq 1.31 -> 1.285
      ✎ Ti.rho is conditional, not measured · in 1 report(s)
```

`docs/ground_truths.md` is still there, narrowed to what is true regardless of
sample: an instrument quirk, a reduction convention, a package version that
broke something. **If a finding names a run number or a fit id, it belongs to
the sample** — where it will be shown beside the fit and travel in a bundle.

> The reason this matters: in the first real beamtime run through this tool,
> 609 lines of excellent findings went into `docs/ground_truths.md` — and not
> one of the 25 result directories was ever written in. One entry opened *"Read
> this before using that fit for anything"* about a named fit, in a file that
> `nrw promote` and `nrw pack` never opened.

## 10. Send it to a collaborator

A result directory is complete but not portable: it records the *hashes* of the
data, not the data, and its script reaches out to the project around it. Email
someone the directory and they get a perfect description of a fit they cannot
run.

```bash
nrw pack 20260805-191540Z-732f4286
```

```
  bundle    ./20260805-191540Z-732f4286.zip
  fit       20260805-191540Z-732f4286
  script    samples/Sample6/models/cu-thf-218389.py
  data      22 file(s)
  chisq     1.83  (verify.py checks this)
  size      2.4 MB
  omitted   3 chain file(s); pass --with-chain to include them

  The recipient needs refl1d, bumps and numpy. Nothing else.
```

The bundle carries the frozen script, the measurement files at the paths the
script expects, the recorded settings and outputs, and a README stating what
result to expect. The first thing the recipient runs is:

```bash
pip install -r requirements.txt
python verify.py
```

```
Loading samples/Sample6/models/cu-thf-218389.py ...
  free parameters  21
  chisq            1.83
  recorded         1.83

REPRODUCED: this environment gives the recorded result.
```

That check is the point. It loads the model, applies the best-fit parameters
from the `.par` file, recomputes χ² and compares — so before anyone changes
anything, they know whether their refl1d gives your answer. A mismatch is
information, not a failure: it means a version difference that *changes the
result*, which is exactly what you want surfaced early.

Two refusals worth knowing about. `nrw pack` will not package a fit whose data
has changed on disk since the run — a bundle asserts that its data produced its
result, and shipping drifted files would make that quietly false. (`--force`
does it anyway and records the drift in `MANIFEST.json` and the README, so the
recipient can see it.) And nothing is left half-written: if any recorded input
is missing, you get an error and no bundle, because a partial bundle looks
complete.

The directory structure is preserved rather than flattened. The script finds
its data by walking up to `nrw.toml`, so rebuilding that shape means the script
is copied byte-for-byte — the same bytes whose sha256 is in the manifest. A
script rewritten to suit the bundle could not be checked against the record it
claims to reproduce.

Use `--dir` to get a directory instead of a zip (for `rsync`), and
`--with-chain` to include the MCMC posterior, which is otherwise the largest
thing in the result and is evidence about the original fit rather than an input
to reproducing it.

---

## 11. Publish it to ISAAC

Only a sample's final fit is published — the one promoted in step 8, or
finalized on its page. Exporting writes the records on disk and sends nothing:

```bash
nrw isaac export 20260807-155810Z-ec6d0134
```

The output below is from another project's final fit, a co-refinement of three
steady conditions of sample `expt11`, because three conditions show what the
export does with a co-refinement:

```
  fit       20260807-155810Z-ec6d0134
  state     run218386: 3 angle segments concatenated -> run 218386
  state     run218393: 3 angle segments concatenated -> run 218393
  state     run218397: 3 angle segments concatenated -> run 218397
  chisq     1.69822

  records   3 written to samples/expt11/results/.../isaac/records
            isaac_record_218386.json
            isaac_record_218393.json
            isaac_record_218397.json
  linked    3 records share one sample id, so the portal reads them as
            conditions of one experiment

  Not uploaded. `nrw isaac push 20260807-155810Z-ec6d0134` publishes them -- the final fit only -- after `--validate-only` asks the portal first.
```

Two lines in that output are the whole point.

**"3 angle segments concatenated."** A REF_L steady state is measured at three
incident angles and reduced to three files with three run numbers. They are one
measurement, and the record carries one curve.

The concatenation is not just a merge. Each segment has its own normalisation —
on run 218386 the 3.5° segment fits an intensity of **0.789**, so it is 21% low
— and the co-refinement is what determined those numbers. Each segment's R and
dR are divided by its fitted intensity before merging, which takes the
segment-2/3 overlap from +30% to +1%. Appending the raw files instead would
publish a curve with a visible step in it.

**"3 records share one sample id."** A co-refinement is several conditions of
one sample, so each condition becomes its own record and they are cross-linked
by `same_sample_as`. The portal then reads them as one experiment rather than
three unrelated measurements. The grouping comes from your spec's `states:`,
never from filenames — so what is published is what was fitted.

**Conditions come from `sample.md`.** A fit knows nothing about applied
potential; you wrote it in the measurement table before any of this ran. The
export reads the row for each run, and with an LLM endpoint configured also
reads the prose around it:

```
notes: Under a galvanostatic hold of -0.5 mA/cm2 in dTHF electrolyte.
```

That sentence was assembled from a table cell reading `-0.5 mA/cm2` and a spec
condition reading `under applied potential (chronoamperometry)`. Use `--no-llm`
for the table-only version.

One caveat worth knowing: the downstream schema recognises open circuit and a
potential in volts, but **not a current density**, so a galvanostatic run
carries its conditions as text with the structured `electrochemistry` field
left empty rather than filled with something wrong.

Nothing is invented. If neither the table nor the spec says anything, the
record says nothing about conditions.

To publish what the export wrote:

```bash
nrw isaac push <fit_id> --validate-only   # the portal checks them, and keeps nothing
nrw isaac push <fit_id>                   # asks before publishing
```

Publishing is never implied by exporting, and the push confirms first, naming
what leaves the project: the data, the fitted model and the notes —
`sample.md`, the fit's `NOTES.md`, the reason it was finalized, and any report
that cites it. A record on a shared portal is not straightforwardly
retractable, so each push is recorded with the fit before it runs, and what was
sent is copied under the fit's `isaac/published/`. The fit's page in
`nrw serve` offers the same steps: **Export**, **Validate with the server** and
**Push…**.

The portal and the key are yours, not the project's: set `ISAAC_URL` and
`ISAAC_KEY` in `~/.nrw` (or your shell, or `~/.aure`). The project's `.env` is
never read for them: anyone who can write the project can write that file, and
it would choose where your key and your records are sent.

nr-workbench does not map anything to the ISAAC schema itself — `data-assembler`
and `nr-isaac-format` own that, and a second copy would drift from a schema
neither project controls. They are the optional `isaac` extra; re-run the
installer with it ([install.md](install.md#options)):

```bash
curl -fsSL https://raw.githubusercontent.com/neutrons-ai/aure-workbench/main/install.sh \
  | NRW_EXTRAS=isaac sh
```

Without them the export stages the fit, tells you what it assembled, and says
plainly which tool is missing.

---

## What to do next

**Keep track of what you tried.** By the tenth run the listing is a wall of
hashes, so `nrw ls` says what each fit was and what you changed to get it:

```
  FIT_ID                     MODEL             STATUS      CHISQ  FRESH
  20260807-150822Z-b2cef12a  cu-thf-218389     ok          1.83   fresh
      model changed; method amoeba -> dream; chisq 2.14 -> 1.83
      “posterior after freeing the oxide layer”
  20260807-150819Z-b82662ed  cu-thf-218389     ok          2.14   STALE
      steps 2000 -> 20000; chisq 2.31 -> 2.14
  20260807-150817Z-e40ceabd  cu-thf-218389     ok          2.31   STALE
      first run of this model
```

Each fit is compared with the previous run of **the same model**, which is the
edit you actually made; comparing against the row above would usually cross
models. The quoted line is your own `--note`, which is reason enough to pass
one. The same two lines appear in `nrw serve`, on the fit table and on the fit
itself.

**Try a different model.** Copy the spec, change it, fit it, and compare:

```bash
nrw diff <fit_a> <fit_b>
```

It tells you whether a χ² difference came from the model, the data, or just the
optimizer settings — three things that look identical in a table of numbers.
Note that "data" there means the measurements only: your script is a recorded
input too, but editing it is a *model* change and is reported as one.

**If you need to hand-edit the script**, do it properly:

```bash
nrw model fork samples/Sample6/models/cu-thf-218389.yaml
```

That hands you ownership of the file and keeps full provenance. Editing a
generated script in place is the one thing that breaks the chain, and `nrw
check` will tell you off for it. Running an ordinary hand-written script through
`nrw fit run` is supported and always will be — provenance does not require the
generator.

Once you own a script, the `refl1d-script-review` skill is worth reading. Its
headline is the aliasing pitfall: co-refinement works by making Experiments
share one `Parameter` *object*, and reassigning one of them afterwards silently
unties it. The fit still converges, χ² even improves, and the answer is wrong.
Generated scripts end with identity assertions that make this impossible; a
fork keeps them, and a hand-written script should gain them.

**Ask the assistant.** With this project open in Claude Code or Copilot, the
skills installed by `nrw init` are already loaded. `docs/ground_truths.md` is
where you and it should record anything non-obvious you learn — it is the
project's memory between sessions.
