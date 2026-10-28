# Getting started with the agent

Setting up nr-workbench to analyse data by itself, first for one sample, then
for measurements that are still arriving.

Every command and every output below was produced by running it, on real
REF_L data, in a project built from scratch. The one exception is marked.

> **Before you start.** Read the first section of
> [docs/agent.md](agent.md) — the one that says what is and is not automated.
> This page is the how; that page is the why, and the why is short.

---

## The minimal setup

Three things. Nothing else is required.

1. **`nrw init`** — gives the project `.claude/settings.json`, which is where
   the limits live.
2. **Write `## Fits to perform`** in the sample's `sample.md`. Without it the
   agent refuses to start.
3. **`nrw agent run <sample>`**.

You also need a coding harness on your `PATH` — `claude` by default, or set
`NRW_HARNESS` to your own command. That is the whole list. `nrw doctor` says
what it found.

No Claude Code subscription? You may not need one. It runs on an
`ANTHROPIC_API_KEY`, or on your institution's own Azure, AWS or GCP account:

```bash
# Microsoft Foundry, for example -- the whole configuration
export CLAUDE_CODE_USE_FOUNDRY=1
export ANTHROPIC_FOUNDRY_RESOURCE=your-resource-name
export ANTHROPIC_FOUNDRY_API_KEY=your-azure-api-key
```

`nrw agent run` passes the whole environment through, so nothing needs
configuring on this side. Check it with `nrw check-llm`, which makes one real
call and names the provider, the model that answered and what it cost — a
deployment that was never created looks exactly like a working setup until
something asks it a question, and you would rather that happened now than at
2am. What will *not* work is substituting an `LLM_BASE_URL` completions
endpoint — that is a text API, and the agent needs a tool-using loop.
[docs/agent.md](agent.md#you-may-not-need-a-subscription) has Bedrock, Vertex,
gateways, model pinning, and the reason a daemon needs these set somewhere
other than your shell profile.

**You do not need an LLM endpoint.** `nrw doctor` reports one if you have it
configured, and `nrw assess`, `nrw model new --from-notes` and `nrw isaac
export` will use it when you are working by hand. While an agent is driving
they do not: the harness is already a language model and the better one, so
sending the same question to a second, weaker one would replace the judgement
you wanted rather than double-check it. A blank `llm` line in `nrw doctor`
costs the agent nothing.

Everything after this point is the same three steps, slowly, with the checks
worth doing in between.

---

## Part 1 — one sample, once

### 1. Make the project

```bash
nrw init cu-thf --beamtime apr2025 --ipts IPTS-34347
cd cu-thf
nrw sample new Cu1 --title "Cu film in dTHF"
```

Then copy your reduced data into `samples/Cu1/data/steady/`:

```
REFL_218386_1_218386_partial.txt
REFL_218386_2_218387_partial.txt
REFL_218386_3_218388_partial.txt
```

`nrw init` is safe on a project that already exists and never overwrites a
file you have edited — so if your project predates this feature, run it again
to pick up `.claude/settings.json`.

### 2. Check the setup

```bash
nrw doctor
```

```
  ✓ project       ~/beamtime/cu-thf
  ✓ skills        19 installed: analysis-provenance, analyst-handoff, …
  ✓ harness       2.1.156 at ~/.local/bin/claude
```

The last line is the one that matters here: `nrw agent run` needs the harness
on `PATH`.

### 3. Say what you want fitted

Open `samples/Cu1/sample.md` and fill in **`## Fits to perform`**. In words,
not spec syntax:

```markdown
## Fits to perform

Fit run 218386 on its own. I expect ~500 A of copper on ~3 nm of titanium,
measured in dTHF through the silicon. There is probably a thin copper oxide
on the copper surface -- try it with and without and tell me which the data
supports.
```

Skip this and you get:

```
$ nrw agent run Cu1
Error: samples/Cu1/sample.md declares no task under '## Fits to perform'.
An unattended session needs to be told what to fit; deciding that for itself
is exactly what it must not do.
Write what you want out of this sample there, then run again.
```

That refusal is the most important line in the design. An agent that picks its
own problem is the failure everything else is arranged against, and before it
starts is the only reliable place to stop it.

**Also fill in the measurement table** while you are there. The scaffold leaves
it empty, and it is what the header checks compare against:

```markdown
| Run    | Type   | Condition |
|--------|--------|-----------|
| 218386 | full Q | OCV       |
```

### 4. Read the prompt before you trust it

```bash
nrw agent run Cu1 --dry-run
```

This composes the whole session and prints it without starting anything. It is
five minutes well spent exactly once — after that you will know what the agent
sees. The interesting part is the middle:

```
## What the offline checks already found

Run before you started, from the file headers, the specs and the recorded
fits. Read these before deciding anything --- most of them are the kind of
thing that is cheap to see now and expensive to discover after five fits.

STILL ARRIVING. These measurements' files changed recently, so what is on disk
may not be all of it --- a steady-state run is three angle segments written
minutes apart. Fitting a partial measurement returns a plausible number, not
an error. Prefer the measurements that are complete, and say in
ESCALATIONS.md if the task needs one of these:
  run 218386 (steady): unchanged for 24s of 300s

Data vs sample.md (`nrw data reconcile`):
  [warn] run 218386: Run 218386 has reduced data but no row in sample.md's
         measurement table. A run nobody wrote up is a run whose conditions
         are not recorded anywhere.

No fits recorded for this sample yet.
```

Both of those are worth acting on before you start, and both went away once
the files had been sitting for five minutes and the table row was filled in.
That is the dry run doing its job.

### 5. Run it

```bash
nrw agent run Cu1
```

It prints what it is doing as it goes — the tool and roughly what it is
pointed at, nothing it said or read:

```
  · session started
  · Read      samples/Cu1/sample.md
  · Read      skills/reflectometry/nrw-model-spec/SKILL.md
  · Bash      nrw data features samples/Cu1/data/steady/REFL_218386_1_218386_par…
  · Write     samples/Cu1/models/cu-thf-backrefl.yaml
  · Bash      nrw model validate samples/Cu1/models/cu-thf-backrefl.yaml
  · Bash      nrw model generate samples/Cu1/models/cu-thf-backrefl.yaml
  · Bash      nrw fit run samples/Cu1/models/cu-thf-backrefl.py --method amoeba
  · Bash      nrw assess 20260810-211523Z-43667719
  · Bash      nrw note 20260810-211523Z-43667719 -m "What I tested: a minimal 3-…
  · done in 916s, 49 turns
Session finished (exit 0); transcript .nrw/agent/20260810-210331Z-Cu1.jsonl
  ! ESCALATIONS.md exists -- read it before promoting anything
```

`--quiet` turns that off; the transcript records everything either way.

One session, one sample, and it exits. It does not wait for anything.

### What it leaves behind

The same places you would have written to yourself. There is no separate path
— the agent produces a fit by running `nrw fit run`, the command you type, so
the record is the record:

```
samples/Cu1/models/cu-thf-backrefl.{yaml,md,py}   spec, model card, script
samples/Cu1/results/20260810-211523Z-43667719/    the immutable record
    manifest.json  inputs.json  spec.yaml  model.py  NOTES.md
    env/{requirements.txt,versions.json}
    fit/…{-expt.json,-profile.dat,-slabs.dat,.par,.out}
samples/Cu1/reports/cu1-what-the-fits-show-technical.md   the full record
samples/Cu1/reports/cu1-what-the-fits-show-si.md          for a peer
samples/Cu1/reports/cu1-what-the-fits-show-plain.md       for a non-fitter
ESCALATIONS.md
```

Three reports rather than one, because a beamtime result is read by a mixed
team: someone who will argue with the model, someone writing it up, and someone
who owns the chemistry and does not fit reflectivity. They are one analysis at
three altitudes and must agree on the answer — `nrw check` fails the sample if
a tier is missing or if they give different one-line answers.

The plain tier names the ideas the analysis actually ran into — a correlated
pair, a skewed posterior, a parameter on its bound — selected from what
`nrw assess` recorded rather than from a fixed syllabus, and gives a brief for
each saying what the explanation has to cover. The explanation itself is
written by whoever writes the report, about this sample and its numbers.
`nrw report Cu1 --concepts` shows which fired and why.

`nrw ls`, `nrw whence`, `nrw diff`, `nrw check` and `nrw pack` all work on it
unchanged, and `nrw check` will hold the agent's own spec to the same
standard as yours. There is no badge marking a fit as an agent's:
`provenance.command` already records the exact command line, and a flag saying
`agent: true` would only invite trusting one record more than another. Judge
the fit by the fit.

| | |
|---|---|
| `--dry-run` | Compose and print; start nothing |
| `--turns N` | Cap on harness turns (default 200) |
| `--model NAME` | Model to run (default: the harness's own) |
| `--timeout SECONDS` | Kill the session after this long (required for OpenCode, which has no turn cap) |
| `--again` | Run even though the sample already has a written report |
| `--harness NAME` | `claude` (the default) or `opencode` |

### 6. Read what it did

In this order, and it should take five minutes:

```bash
cat ESCALATIONS.md            # what it could not decide
nrw ls --sample Cu1           # every fit, with what changed between them
ls samples/Cu1/reports/       # what it concluded
```

Then, if you agree, promote by hand:

```bash
nrw promote 20260810-231402Z-4f2a8c1e --as final \
    --reason "oxide is real; excluding it costs 0.4 in chisq and leaves a residual at 0.08"
```

The agent cannot do this, and the reason string is why. In a year, that
sentence is what a reader needs, and it is not recoverable from the artifacts.

### 7. Take over interactively

Most sessions do not end here. The agent does the systematic work overnight and
you carry on with an assistant in the morning — and that handover used to be
the most expensive part of the loop. Measured on a real beamtime project, an
assistant told *"pick up where the agent left off"* spent 28 tool calls
rebuilding a picture of the project before doing anything, ten of them
searching the filesystem for the `nrw` binary.

All of it was derivable, so it is now one command:

```bash
nrw handoff Cu1
```

It prints how to invoke `nrw` in this shell, the declared task, `ESCALATIONS.md`
in full, how the last session ended and whether it was cut off, the integrity
check, any fits with nothing written down, the offline checks recomputed now,
who is reading, and the order to read things in.

Then start your assistant and give it one instruction: *"take over from the
overnight agent"*. Do not paste context. The `analyst-handoff` skill is
installed in the project and says what to do with that output; the short
version is that it reads the escalations before writing a spec, and does not
re-derive a conclusion a report already records.

If `nrw handoff` says `nrw` is not on your PATH, that is the ten-call search
being prevented. `nrw init` writes `.nrw/bin/nrw` and `$NRW_BIN` for exactly
this, and `nrw doctor --fix-path` makes a bare `nrw` work in assistant sessions
if you want that too.

---

## Part 2 — waiting for more measurements

Same setup, one more command. `nrw agent watch` decides *when* to start a
session; everything inside one is unchanged.

### Look before you leap

```bash
nrw agent watch --dry-run
```

```
Cu1
  → run 218386 (steady)  ready        3 segment(s), settled, not yet fitted
    run 218393 (steady)  arriving     unchanged for 0s of 300s
```

The arrow marks what would be started. Run 218393's files had just landed, so
it waits — a steady-state measurement is three angle segments written minutes
apart, and fitting the first one alone gives a perfectly plausible answer out
of a third of the data.

Quiet time is read from the files' own timestamps, so one poll tells the
truth. You do not have to leave it running to get a useful answer out of
`--dry-run`.

### One measurement, as soon as it is ready

The usual beamtime shape — "analyse this one when it is complete, then stop":

```bash
nrw agent watch Cu1 --max-sessions 1
```

It polls until the measurement settles, runs one session, and returns
immediately rather than sitting out another poll interval.

This is the difference between the two one-shot modes:

| | Waits for settling? | Sessions |
|---|---|---|
| `nrw agent run Cu1` | No — warns in the prompt and proceeds | 1 |
| `nrw agent watch Cu1 --max-sessions 1` | Yes | 1 |

Use the first when you know the data is there. Use the second when you are
leaving.

### All night

```bash
nrw agent watch
```

With no sample named, it watches every sample in the project. It starts a
session for a measurement only when three things are true:

1. **Its files have stopped changing** for `--settle` seconds (default 300).
2. **Its files are coherent** — segments contiguous from 1, subruns following
   the run number, and the run not filed as both a steady state and a series.
3. **Nothing has fitted it yet** — read from the recorded fits, so a fit you
   ran by hand counts too.

| | |
|---|---|
| `--dry-run` | Report what each measurement is waiting for; start nothing |
| `--settle N` | Seconds files must be unchanged (default 300) |
| `--poll N` | Seconds between polls (default 60) |
| `--max-sessions N` | Stop after N sessions |
| `--session-timeout N` | Kill one session after N seconds (default 7200) |
| `--turns N`, `--model NAME` | Passed through to each session |

Ctrl-C stops it and prints the session count.

### When a run is held back

```
    run 218389 (steady)  quarantined  run 218389 appears as both a steady-state
                                      measurement and a time-resolved series.
                                      One of the two is a filing accident, and
                                      which one is a question for a person.
```

A quarantined run is never fitted, however long it sits there, and it is named
in the session prompt under **DO NOT FIT** so an agent working on the rest of
the sample leaves it alone. Resolving it is a curation decision — usually
moving a file — and that stays yours.

The asymmetry is deliberate: a run wrongly held back costs one question in the
morning, a run wrongly fitted costs the night.

---

## What the agent cannot do

These are refused by a `PreToolUse` hook:

- `nrw promote`, and curating fits: `nrw fit star`, `unstar`, `discard`,
  `restore` and `delete`
- publishing to ISAAC: `nrw isaac push` (even `--validate-only`) and
  `nrw isaac export --upload`
- `nrw aure run`, except with `--dry-run`
- organizing the experiment: `nrw experiment assign` and `release`, and
  `apply`, `adopt` or `settings` with `--write`
- `nrw sample reset`, except with `--dry-run`
- `nrw init --nested`
- any `--force`

[docs/agent.md](agent.md#the-limits) says why
each is a person's call, and what stays allowed.

You can check any command without running it:

```bash
$ nrw agent guard --command "nrw promote abc123 --as final --reason 'best chisq'"
Refused by nr-workbench (promote): Marking a fit as the answer is a person's
decision, and the reason recorded with it is the part that matters in a year.
Write what you would have said in ESCALATIONS.md, with the fit id and the
evidence, and stop.
$ echo $?
2
```

Exit 2 is what blocks the call. Every refusal names `ESCALATIONS.md`, because
an agent told only "no" retries.

---

## If something looks wrong

| Symptom | Look at |
|---|---|
| It refuses to start | `## Fits to perform` is empty. That is the design, not a bug. |
| It refuses: the sample already holds a written report | the task looks answered. Say what is left under `## Fits to perform`, or pass `--again` for a deliberate second pass |
| It refuses: a session is already analysing the sample | `nrw agent status` shows what is running; `nrw agent stop <sample>` ends it |
| It refuses with OpenCode: no turn cap | pass `--timeout <seconds>`; `--turns` does nothing there |
| `nrw agent run` says the harness is missing | `claude` is not on `PATH`; `nrw doctor` confirms |
| Everything says `arriving` | the files were just written; wait `--settle` seconds, or lower it |
| Everything says `done` | those runs are already in a recorded fit — `nrw ls` shows which |
| A run says `quarantined` | read the reason; it is naming a real inconsistency |
| The prompt is missing a check | run the command it names (`nrw data reconcile`, `nrw check`) directly and compare |

Transcripts are under `.nrw/agent/`, one prompt and one `.jsonl` per session.
They are gitignored, because a transcript contains everything the session saw.

---

## Where to go next

- [docs/agent.md](agent.md) — what is enforced versus merely asked for, and
  the evidence behind the split
- [docs/getting-started.md](getting-started.md) — the same analysis done by
  hand, end to end, which is worth doing once before delegating it
- `nrw agent --help`
