# Your first fit

You have reduced data and no model yet. This is the short path from an empty
directory to a fitted starting model with a provenance record, using AuRE to
propose the layer stack.

It assumes nothing about reflectometry beyond knowing what your sample is made
of. If you want the long version — where you write the stack yourself and every
number is explained — read [getting-started.md](getting-started.md) instead.

> **You need a language-model endpoint for this page.** AuRE is LLM-driven and
> will not fit without one. `nrw doctor` says whether you have one, and
> `.env.example` lists the variables. Without one, skip to
> [Writing the stack yourself](#writing-the-stack-yourself).

---

## 1. Make the project and bring the data in

```bash
mkdir my-beamtime && cd my-beamtime
nrw init --beamtime sep2026 --ipts IPTS-34347
nrw sample new Cu4 --title "Cu on Ti in dTHF"
cp /path/to/reduced/REFL_218386_*_partial.txt samples/Cu4/data/steady/
nrw sample scan Cu4
```

`nrw sample scan` registers what is on disk. It should report your run with its
angle segments.

## 2. Say what the sample is

Open `samples/Cu4/sample.md` and fill in two sections. This is the whole input
to the model, so it is worth two minutes:

```markdown
## Description

50 nm copper on 5 nm titanium on a silicon wafer.

## Details

In d8-THF. Measured through the silicon substrate.
```

Three things have to be in there, and none of them is in the data:

- **The layers**, substrate upwards, with rough thicknesses.
- **What the sample sits in** — and its deuteration. d8-THF is about 6.35;
  protonated THF is about 0.4. Whichever you leave ambiguous, the fit will
  absorb into a layer and still converge.
- **Which side the beam enters.** A solid/liquid cell is measured *through the
  wafer*. Say so in those words, or "back reflection" — `nrw aure new` looks for
  them, and reads them negated ("from the air side, not through the
  substrate") as the ambient side. Getting this backwards produces a fit that
  converges and reports a chi-squared in the hundreds with nothing naming the
  cause.

If you already suspect something — a native oxide, a swollen layer — write it
under `## Fits to perform`. AuRE will rank it as a candidate and test it.

**Working with a coding assistant?** Just tell it you want a first fit. It will
ask you these questions and hand you the answers to paste in here: `sample.md`
is the one file it does not write for you. The standard it follows is
`skills/reflectometry/aure-first-fit/SKILL.md`.

## 3. Write the setup

```bash
nrw aure new Cu4
```

```
Wrote samples/Cu4/aure/Cu4-218386/setup.yaml

  read from the files (exact, do not edit):
    - run 218386, 3 partials file(s), in segment order
    - data_dir: ../../../.. (the project root)
    - state kind: partials
  read from sample.md (yours, check it):
    - sample_description: ## Description + ## Details (101 chars)
    - back_reflection: True -- the notes say the beam arrives through the substrate

  3 data file(s) resolved.
```

The two lists mean different things. The first was read off the files and is
exact — the segment order is Q order, and you should not edit it. The second
came from your prose, and is the part worth re-reading.

The three angle segments become **one AuRE state**, because they are one curve
of one sample. AuRE validates that for us: it refuses to mix combined and
partial files in a state, and refuses partials from different runs.

## 4. Run it

```bash
nrw aure run samples/Cu4/aure/Cu4-218386/setup.yaml
```

The default budget is `quick` — few steps, one refinement — because the first
question is whether your description was right, not how well it can be polished.
`--budget standard` uses AuRE's own defaults and takes considerably longer.

It ends by printing the stack it found and its chi-squared.

### If the answer looks wrong

**If the description was wrong** — wrong solvent, missing layer, wrong geometry
— fix `sample.md` and go back to step 3. No optimiser setting rescues a sample
that is in the wrong solvent.

**If a thin layer looks suspicious** — chi-squared poor with fringes left in the
residuals, a layer under about 30 Å, or a roughness that has grown to a large
fraction of its own layer — retry with:

```bash
nrw aure run samples/Cu4/aure/Cu4-218386/setup.yaml --mode-enumeration
```

A thin layer sits on a contrast×thickness ridge where a single optimiser run can
land in the wrong basin and report a confident wrong answer.
`--mode-enumeration` re-seeds each thin layer's SLD across discrete levels and
starts from the best one. `skills/reflectometry/thin-layer-degeneracy/SKILL.md`
is the physics.

Re-running the same thing with a bigger budget is not a third option: it
optimises the same model harder.

## 5. Bring it back under provenance

An AuRE run is reconnaissance. It has no fit record, so nothing can trace a
figure back to it. This is the step that fixes that:

```bash
nrw aure import samples/Cu4/aure/Cu4-218386/output --sample Cu4 --name cu-thf-first
nrw model validate samples/Cu4/models/cu-thf-first.yaml
```

```
  info     733 data point(s) across 3 experiment(s)
  info     beam enters Si last in the stack, i.e. through the substrate (back
           reflection); dTHF is the backing
  info     3 experiment(s), 4 free parameter(s), 0 constrained value(s)
  ok
```

**Read that geometry line.** It is derived from the stack ordering and checked
against the measured critical edge, independently of what you wrote in
`sample.md`. If it contradicts what you believe about your own measurement,
something is wrong and it is worth finding out which of the two before fitting.

The spec frees what AuRE's fit freed, in the bounds it fitted them in: each
layer's thickness, SLD and interface, the substrate's interface, an intensity
for each angle segment, and the sample broadening, theta offset or background
when AuRE fitted them, at the values it found. So the fit that follows starts
where AuRE ended. The segments' intensities are the exception: a spec gives
them one starting value, and the import says what AuRE's were.

The written spec says at the top who proposed it:

```yaml
# The stack below was PROPOSED by AuRE 1.0.1 @ d6e38e481fcf,
# from sample.md and the data. It is a starting point, not a
# measurement -- check every layer and range before fitting.
# States, angles and data_dir were read from the files' own headers
# and were not proposed.
```

The angles are the measured ones — 1.201, not 1.2. Do not tidy them: theta sets
the resolution through `dT = dq/q · tan(θ)`, so a rounded angle gets absorbed
into roughness.

## 6. Fit it properly, and write down what it means

```bash
nrw model generate samples/Cu4/models/cu-thf-first.yaml
nrw fit run samples/Cu4/models/cu-thf-first.py --method amoeba \
    --note "first fit, stack proposed by AuRE"
nrw assess <fit_id>
nrw note <fit_id> --showed "..." --caveat "the stack was proposed, not measured"
```

That fit has a `fit_id`, input hashes, the environment it ran in, and a frozen
copy of the script. `nrw whence` can trace any figure back to it.

**The caveat above always applies to a first fit.** A good chi-squared means the
curve is described, not that the structure is right — and a layer AuRE added to
improve chi-squared is a hypothesis until something independent supports it.
Explore with `amoeba`, then quote uncertainties from a `--method dream` run.

## Writing the stack yourself

No endpoint, or you would rather not hand the model to a language model:

```bash
nrw model new Cu4 --name cu-thf-first --print-prompt
```

This writes a spec with every fact filled in — runs, files, measured angles —
and prints the instruction for filling in the stack, including which skills to
read. Hand that to your coding assistant, or do it by hand. The rest of the path
from step 5 is identical.

## What is recorded, and what is not

`samples/<id>/aure/<name>/` holds the scouting run:

| File | Tracked | What it is |
|---|---|---|
| `setup.yaml` | yes | What was asked for |
| `run-env.json` | yes | The physics knobs it ran under |
| `output/` | **no** | The run itself |

`run-env.json` exists because AuRE's setup file does not fully record its own
run: `MODE_ENUMERATION` and several other knobs that change what model comes out
have no setup key and are read from the environment. Two runs from an identical
`setup.yaml` could otherwise differ with nothing on disk explaining why.

`output/` is deliberately *not* committed. Beyond its size, AuRE resolves data
files to absolute paths and writes them into `final_state.json`, and that file
also holds the full prompt-and-response transcript of the run — both of which
are the kind of thing that should not be in a directory that gets shared,
archived and sometimes published.

Nothing is lost by that. The durable record of an AuRE run is the model spec
`nrw aure import` writes: it is committed, it names the AuRE version and commit
that proposed the stack, and the fit that follows it has a full provenance
record of its own.
