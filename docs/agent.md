# Running the harness unattended

Overnight during a beamtime, data keeps arriving and nobody is watching. This
is how to set nr-workbench up so a coding harness works the queue by itself,
and how to decide whether you want that.

Read the first section even if you skip the rest. It is the part that explains
why this looks the way it does.

---

## What is and is not automated

nr-workbench does not contain a decision policy, and that is deliberate. We
measured. The reference experiment — `apr2025/cu-thf-expt11`, one week of
expert analysis, 25 fits, 17 findings written down as they happened — was
replayed against every automatic check in this package. Of the 17 findings:

| | Count | Meaning |
|---|---|---|
| Reachable by arithmetic | 1 | A program can find it and say what it means |
| Signal detectable, conclusion not | 5 | A program can point at it; a person says what it is |
| Needs judgement | 11 | No artifact on disk distinguishes the right answer |

Two results from that corpus say the same thing from the other side:

- **χ² ranks it backwards.** Both promoted fits are *worse* in χ² than the best
  fit in their arm (1.285 against 1.193; 1.698 against 1.411). A loop that
  optimises χ² gets both of the decisions that produced the paper wrong.
- **A pinned parameter implies three different correct actions.** In one fit,
  three parameters sat on their bounds. `Ti.rho` at −2.0 is bulk titanium and
  widening it would have destroyed the result — it *was* the result.
  `CuOx.rho` on its floor meant the material assignment was wrong.
  `Cu.roughness` carried no information at all. Nothing in the artifacts tells
  them apart.

So the checks report; the harness decides; you promote. What this package adds
around the harness is four things: **observations** it reads before it starts,
**limits** it cannot talk past, a **bounded session**, and a **transcript**.

---

> **Want the walkthrough instead?**
> [docs/getting-started-with-agent.md](getting-started-with-agent.md) does the
> setup step by step on real data — one sample first, then the overnight case.
> This page is the reference.

## Setting it up

### 1. Scaffold or upgrade the project

```bash
nrw init            # in an existing project, this is safe and idempotent
```

That writes `.claude/settings.json`, which is where the limits live. A project
scaffolded before this existed will not have it; `nrw init` adds it without
touching anything you have edited. Check with:

```bash
nrw doctor
```

```
  ✓ harness       2.1.156 at ~/.local/bin/claude
  ✓ agent limits  PreToolUse hook + 2 deny rule(s)
```

Those two lines fail independently, and the combination worth noticing is a
harness with no limits — `nrw agent run` works and nothing stops a promotion:

```
  ✓ harness       2.1.156 at ~/.local/bin/claude
  ! agent limits  no .claude/settings.json; run `nrw init` to add the hook
                  that refuses promote, --upload and --force
```

### 2. Say what you want fitted

An unattended session reads `## Fits to perform` in the sample's `sample.md`
and does that and nothing else. **With that section empty, `nrw agent run`
refuses to start.** This is the most important line in the design: an agent
that chooses for itself what is interesting is the failure everything else is
arranged against, and the only reliable place to stop it is before it begins.

Write it in words, not in spec syntax:

```markdown
## Fits to perform

Co-refine the two OCV states with the tNR run measured between them. The Cu
layer should thicken monotonically — a linear-in-time constraint on
Cu.thickness. I do not know whether the oxide is real; try it both ways and
tell me which the data supports.
```

Whoever starts a session can give it its task instead, with `nrw agent run
<sample> --task-file <file>`; the session is told it takes the place of `##
Fits to perform`. nr-watcher does this for the samples its plan names, since it
plans the whole experiment and has a task for each of them whether or not their
`sample.md` holds it yet. The task is still words a person or a planner wrote,
never one the session chooses.

Everything in `sample.md` matters here, not just this section — the
measurement table is what the header checks are compared against, and the
alignment notes are what decide whether `probe.theta_offset` is `per: model` or
`per: state`.

### 3. Look at the prompt before you trust it

```bash
nrw agent run Sample4 --dry-run
```

This composes the session and prints it without running anything. You get the
experiment's context (when one is written on the Experiment page), the
declared task, the offline observations, the skills it will read, and the
limits — the whole of what the harness will see. Reading it once is worth more
than any amount of configuration. [agent-run-llm.svg](agent-run-llm.svg) shows
where each part of it comes from.

### 4. Run one session

```bash
nrw agent run Sample4
```

```
Session finished (exit 0); transcript .nrw/agent/20260810-231402Z-Sample4.jsonl
  ! ESCALATIONS.md exists -- read it before promoting anything
```

This is one session over one sample, and it exits. It does not wait for
anything — if a measurement's files are still arriving, it says so in the
prompt and lets the agent work around it rather than refusing, because you
asked for the session and may know something the timestamps do not.

If you want the waiting *and* a single session — the usual beamtime shape,
"analyse this one as soon as it is complete, then stop":

```bash
nrw agent watch Sample4 --max-sessions 1
```

That polls until the measurement has settled, runs one session, and returns
immediately afterwards rather than sitting out another poll interval.

The prompt and the full transcript are kept under `.nrw/agent/`. Options:

| | |
|---|---|
| `--dry-run` | Compose and print; start nothing |
| `--turns N` | Cap on harness turns (default 200) |
| `--model NAME` | Model to run (default: the harness's own) |
| `--timeout SECONDS` | Kill the session after this long (required for OpenCode, which has no turn cap) |
| `--again` | Run even though the sample already has a written report |
| `--harness NAME` | Which harness to drive: `claude` (the default) or `opencode` |

---

## The limits, and why there are two of them

These are refused, and the preview beside each is not:

| Refused | Still allowed |
|---|---|
| `nrw promote` — marking a fit as the answer | `nrw ls`, `nrw assess`, `nrw diff` |
| Curating fits: `nrw fit star`, `unstar`, `discard`, `restore` and `delete` | |
| `nrw isaac push`, even with `--validate-only`, and `nrw isaac export --upload` | `nrw isaac export`, which writes the records on disk |
| `nrw aure run` | `nrw aure run --dry-run`, which validates the setup |
| Organizing the experiment: `nrw experiment assign` and `release`, and `apply`, `adopt` or `settings` with `--write` | `nrw experiment status`, and the same three without `--write` |
| `nrw sample reset` | `nrw sample reset --dry-run` |
| `nrw init --nested` | `nrw sample new <ID>` in the existing project |
| Every `nrw tiled` command, and Tiled's own command line (`tiled login`, `tree`, ...) | Saying in `ESCALATIONS.md` what to push or fetch: each acts with the person's login |
| Any `--force` | |

Most are plain. Which fit is the answer, which are good and which are set
aside, which sample a run belongs to and under what condition are a person's
judgements, and a reset deletes the record of what was tried. A push publishes
outside the project, and a validate-only push is refused too because it sends
the records and the key off the machine. An AuRE run makes billed
language-model calls for as long as it takes. The last is the one worth
explaining: **every forcing flag in nr-workbench exists because a check said
no** — drifted inputs, a hand-edited script, an identical run already recorded,
a directory somebody else wrote. An agent reaching for `--force` has arrived at
exactly the situation a person is meant to see.

Two independent mechanisms enforce them:

1. **A `PreToolUse` hook** in `.claude/settings.json` running `nrw agent
   guard` (for OpenCode, a plugin that calls the same command; see
   [OpenCode](#opencode)). It exits 2, which blocks the call before it runs and
   shows the model why. It splits the command line on newlines, `;`, `&`, `|`,
   parentheses and backticks first, so neither `nrw ls && nrw promote abc` nor
   `$(nrw promote abc)` slips past.
2. **`NRW_AGENT=1`**, which `nrw agent run` sets on the session itself. Under
   it, each of them refuses from the inside — `--force` is checked once at the top
   of the CLI rather than in each command, so a subcommand added next year is
   covered without anyone remembering to.

Both, because a hook can be misconfigured and an environment variable can be
unset, but both failing silently at the same time is a different order of
accident. Neither is a sandbox — a determined process can do anything the user
can. They stop the plausible mistake, which is the one that actually happens.

The guard reads the whole command line, not its first few words. These are all
refused, and every one of them was allowed by the first version:

```bash
if true; then nrw promote abc --reason x; fi
for f in *.py; do nrw fit run $f --force; done
cd samples/S1; ls; nrw fit run m.py --force
A=1 nrw promote abc
python -m nr_workbench.cli promote abc
bash -c 'nrw promote abc'
```

None of those is obfuscation — they are how batched shell work looks when an
agent writes it. Things that only *mention* a refused word still run:
`grep promote src/`, `echo 'do not promote this'`, `nrw ls --sample
promotion-study`.

Every refusal names `ESCALATIONS.md`, at the project root. An agent that is
only told "no" retries; one that is told where to write the decision has
somewhere to put it. Read that file first in the morning.

To check any command without running it:

```bash
nrw agent guard --command "nrw promote abc123 --reason 'best chisq'"
```

### Giving it more room

`.claude/settings.json` is yours to edit. Removing the hook entry removes the
first mechanism; unsetting `NRW_AGENT` would remove the second, but `nrw agent
run` sets it deliberately, so the honest way to allow promotion is to promote
by hand after reading what the session did. That is the workflow this is built
for.

---

## What the session reads before it starts

Every check below is deterministic, offline, and needs no model. They run
during `nrw agent run` and their output goes into the prompt. You can run each
one yourself:

| Check | Command | Catches |
|---|---|---|
| Headers against `sample.md` | `nrw data reconcile <sample>` | A run mislabelled in the notes; a different direct beam; a time-resolved reduction filed as a steady state |
| Spec self-consistency | `nrw check` | A constraint whose `form` contradicts the measured trajectory; roughness ranges that permit σ > t/4; parameters silently held at scaffold defaults |
| Fit assessment | `nrw assess <fit-id>` | Parameters on bounds, unconstrained posteriors, ρ–t correlations above 0.8, a layer swallowed by its own interfaces |

This is where automation earns the most. In the reference experiment, the
errors that cost the most time were visible in the file headers **before the
first fit ran** — a run written up as OCV that was not, which sent five fits
down the wrong path, and a 27.6% segment normalisation difference. Nothing was
looking at them. These checks help whoever is driving, agent or not.

---

## Running all night: `nrw agent watch`

`nrw agent run` is one session. The watcher decides *when* to start one and
over what, and nothing else — it is a scheduler, not a second decision-maker.

```bash
nrw agent watch                     # every sample
nrw agent watch Sample4 Sample7     # or a few
nrw agent watch --dry-run           # what each measurement is waiting for
```

The dry run is the one to look at first. On the reference experiment:

```
expt11
    run 218386 (steady)  done         already fitted
    run 218389 (steady)  quarantined  run 218389 appears as both a steady-state
                                      measurement and a time-resolved series.
                                      One of the two is a filing accident, and
                                      which one is a question for a person.
    run 218393 (steady)  done         already fitted
    run 218397 (steady)  done         already fitted
    run 218389 (series)  done         already fitted
```

That is a finished experiment: nothing to do, and one run held back. Note run
218389 appearing twice — the real 130-slice series *and* a stray whole-run
reduction of it that was filed under `data/steady/`. The watcher reports both,
because the pair is itself the fingerprint.

Quiet time is read from the files' own modification times, so a single poll
tells the truth. A `--dry-run` that had to watch for changes across polls would
report everything as "arriving" no matter how old it was.

### Three questions, and only three

**Has it finished arriving?** A steady-state measurement is three angle
segments written minutes apart. A daemon reacting to file *creation* fits the
first segment alone and gets a perfectly plausible answer out of a third of the
data. So a run is started only once its files have been unchanged for
`--settle` seconds (default 300). Polling, not `watchdog`: these land on NFS,
and reduction takes minutes anyway.

For a time-resolved series, counting slices says nothing — they arrive one at a
time across the whole electrochemistry. The reduction sidecar is the only thing
that knows how many to expect, so the series is complete when every interval it
names has a file.

**Is it trustworthy?** Any one of three fingerprints quarantines a run:

- it appears as both a steady-state measurement and a time-resolved series;
- its angle segments are not contiguous from 1;
- a segment's subrun is not `run + segment - 1`.

The asymmetry is deliberate. A run wrongly held back costs one question in the
morning; a run wrongly fitted costs a night. The third fingerprint is why the
watcher parses filenames rather than counting files —
`REFL_218386_2_218387_partial.txt` is the second angle segment of run 218386,
and a program that never reads that string cannot notice when it is wrong.

A quarantined run is also named in the session prompt, since a session is
per-sample while the quarantine is per-run: the watcher can hold one
measurement back and still start a session over the sample containing it.

**Has anybody looked at it yet?** Read off the recorded fits, not a state file.
A state file is a second source of truth that goes stale the moment you fit
something by hand, and then the daemon repeats work you already did.

### Options

| | |
|---|---|
| `--dry-run` | Report what each measurement is waiting for; start nothing |
| `--settle N` | Seconds files must be unchanged (default 300) |
| `--poll N` | Seconds between polls (default 60) |
| `--max-sessions N` | Stop after N sessions (default: until interrupted) |
| `--session-timeout N` | Kill one session after N seconds (default 7200) |
| `--turns N`, `--model NAME` | Passed through to each session |

One session at a time, and never a second for a sample whose first is still
running. Concurrency buys nothing here — the beam is slower than the fits — and
costs the thing that matters, which is a transcript you can follow.

---

## The agent does not need an LLM endpoint

nr-workbench can be pointed at a language-model endpoint (`LLM_PROVIDER`,
`LLM_API_KEY`; `nrw doctor` reports what it sees). Four commands use it: `nrw
assess` asks whether fitted values look physically sensible, `nrw model new
--from-notes` proposes a stack, `nrw isaac export` writes condition
sentences, and `nrw aure run` hands AuRE the whole fit.

**None of them consults it while an agent is driving.** Under `NRW_AGENT=1`
`nrw aure run` is refused (see [the limits](#the-limits-and-why-there-are-two-of-them)),
and the other three hand the work to the harness instead:

| Command | Endpoint configured, no agent | Agent driving |
|---|---|---|
| `nrw assess` | the endpoint judges | reports the measurable findings and says the judgement is yours |
| `nrw model new --from-notes` | the endpoint proposes a stack | scaffolds the spec and prints the instruction, as `--print-prompt` does |
| `nrw isaac export` | the endpoint writes condition sentences | uses the measurement table; write the conditions there |

This is not caution about the endpoint. It is that **the harness is already a
language model, and the better one.** Sending the same question to a second,
weaker model and handing its answer back does not add a check — it replaces
the judgement you wanted with a worse one, and then presents it as evidence
inside the harness's own context.

The consequence worth stating plainly: **you do not need an endpoint
configured to run the agent.** The three commands in the table work; they simply
route the judgement to the thing already doing the judging. An endpoint is
still useful when nobody has a harness open.

This does not change when the endpoint happens to be Claude Code itself (see
[The harness can be the endpoint](#the-harness-can-be-the-endpoint)). The
argument above has two halves, and being the same model only answers one of
them. The other is that the harness has *this conversation* — the files it has
read, the fits it has run, what it already ruled out — and a fresh `claude -p`
subprocess has none of it. Its verdict still arrives back inside the harness's
own context as evidence, and it is now evidence produced with less to go on,
at the cost of a second inference. So the gate stays where it is.

---

## Without a Claude Code subscription

Two different things get called "bringing your own model", and only one of
them substitutes for the harness.

### An endpoint is not a harness

The `LLM_BASE_URL` / `LLM_API_KEY` endpoint nr-workbench can use is a
completions API: you send text, you get text back. A harness is a tool-using
loop — it reads a file, runs `nrw fit run`, looks at what came out, and
decides what to do next. You cannot substitute the first for the second
without writing the loop in between, and writing that loop is precisely what
this package does not do, because the measurement says a good harness beats
one we would write.

So `nrw agent run` needs a harness. `LLM_BASE_URL` will not stand in for it,
and the error says so rather than leaving you to conclude the install is
broken.

### The harness can be the endpoint

The substitution fails in one direction and works in the other. A completions
API cannot be a harness, because the loop is missing. But a harness can answer
a completions call — you just ask it one question and take the text back,
using none of the loop.

AuRE does exactly that with its `claude_code` provider, which runs
`claude -p` as a subprocess:

```bash
LLM_PROVIDER=claude_code
```

No key, no base URL. Whatever `claude` is already authenticated with — a
subscription, `ANTHROPIC_API_KEY`, or the Foundry / Bedrock / Vertex variables
below — is what the endpoint uses, so a machine set up for `nrw agent run`
needs nothing further.

**What this is for.** `nrw aure run` drives AuRE's own state machine, which
consults a model at every node and therefore refuses to start without an
endpoint. Until this existed, a person whose only model was Claude Code had to
configure a second, weaker one purely to get past that check — which is the
exact trade the section above argues against, made unavoidable by an install
detail.

**What it costs.** Each call carries Claude Code's own system prompt and tool
definitions: about 12k input tokens AuRE did not send. Measured on Sonnet 5,
roughly $0.08 for a run's first call and $0.017 for each one after, since the
prompt cache is reused across processes for an hour. Expect $0.2–0.4 per
analysed curve of pure overhead, plus about a second of process startup per
call. A plain endpoint is cheaper and faster; this one is simply already
configured.

**What it does not change.** `nrw assess`, `nrw model new --from-notes` and
`nrw isaac export` still stand down under `NRW_AGENT=1`, for the reason given
above: same model, less context, and a second inference to produce it.

### You may not need a subscription

Claude Code is not subscription-only. It authenticates with an
`ANTHROPIC_API_KEY` (billed per token), and it runs against **Microsoft
Foundry**, **Amazon Bedrock** and **Google Cloud's Agent Platform (Vertex)**
using your organisation's own cloud credentials and billing. If your
institution already has an Azure, AWS or GCP account with Claude models
enabled, that is enough.

All of it is selected by environment variables, and `nrw agent run` passes the
**whole environment** through to the harness. So there is nothing to configure
in nr-workbench: set the provider variables in your shell (or in the daemon's
unit file — see the warning below) and the agent uses them.

#### Microsoft Foundry

In the [Foundry portal](https://ai.azure.com/), create a resource and a
deployment for each Claude model you want, noting the deployment names. Then:

```bash
export CLAUDE_CODE_USE_FOUNDRY=1
export ANTHROPIC_FOUNDRY_RESOURCE=your-resource-name
export ANTHROPIC_FOUNDRY_API_KEY=your-azure-api-key
```

Omit the API key to use Microsoft Entra ID instead — Claude Code falls back to
the Azure default credential chain, so `az login` works locally and a managed
identity works on a server.

**Pin your model versions.** Foundry has no startup model check, so an
unpinned alias that is not deployed in your account fails at the first
request rather than at launch. Set these to *your deployment names*:

```bash
export ANTHROPIC_DEFAULT_OPUS_MODEL='claude-opus-4-8'
export ANTHROPIC_DEFAULT_SONNET_MODEL='claude-sonnet-5'
export ANTHROPIC_DEFAULT_HAIKU_MODEL='claude-haiku-4-5'
```

This matters more for a beamtime than for interactive use: a wrong model name
surfaces at 2am as a session that failed on turn one.

#### Amazon Bedrock

```bash
export CLAUDE_CODE_USE_BEDROCK=1
export AWS_REGION=us-east-1
```

Authentication uses your normal AWS credentials.

#### Google Cloud's Agent Platform (Vertex)

```bash
export CLAUDE_CODE_USE_VERTEX=1
export CLOUD_ML_REGION=us-east5
export ANTHROPIC_VERTEX_PROJECT_ID=your-project-id
```

#### Behind a gateway or proxy

A corporate proxy is `HTTPS_PROXY`. An LLM gateway is `ANTHROPIC_BASE_URL`, or
the provider-specific `ANTHROPIC_FOUNDRY_BASE_URL` /
`ANTHROPIC_BEDROCK_BASE_URL` / `ANTHROPIC_VERTEX_BASE_URL`. With Bedrock or
Vertex, add `CLAUDE_CODE_SKIP_BEDROCK_AUTH=1` or
`CLAUDE_CODE_SKIP_VERTEX_AUTH=1` when the gateway handles cloud auth itself.

#### Check it before the beam does

```bash
nrw check-llm
```

This makes a real call. `nrw doctor` reports what is *configured* — the
binary it found, the variables that are set — and a deployment name that was
never created, an expired key and a gateway that resolves but refuses all look
configured. `check-llm` runs one turn through exactly the invocation `nrw
agent run` uses, and reports the provider it selected, the model that actually
answered, and what the call cost:

```
  provider  microsoft foundry
            CLAUDE_CODE_USE_FOUNDRY=1
            ANTHROPIC_FOUNDRY_RESOURCE=your-resource-name
            ANTHROPIC_FOUNDRY_API_KEY=set (32 chars, ...9f2c)
            ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-8

  ✓ harness   answered
              model    claude-opus-4-8
              took     3.6s, $0.0333
  · endpoint  no endpoint configured. Set LLM_PROVIDER and LLM_API_KEY, or
              LLM_BASE_URL for an OpenAI-compatible one. Nothing an unattended
              session does needs this.
```

Keys are never printed, only their length and last four characters — enough to
tell two apart, not enough to use one. It exits non-zero when something it was
asked for did not answer, so it belongs in whatever script you run before a
beamtime.

It probes both language models this package talks to, and `--harness` or
`--endpoint` narrows it to one. The endpoint being absent is not a failure:
that is the distinction the top of this section is about. `--model NAME` tests
a specific deployment, which is the way to verify each of your
`ANTHROPIC_DEFAULT_*_MODEL` pins actually exists before an overnight run finds
out for you.

The harness probe is a real turn against your default model, so it costs what
that model costs — a few cents. Probing something cheap would defeat the
purpose: an undeployed model pin is precisely the failure being looked for.

Claude Code's own `/status`, typed in an interactive session, reports the same
provider and resource without making a call.

> **A daemon has no shell profile.** `nrw agent watch` started from systemd,
> launchd or cron does not read `.bashrc` or `.zshrc`, so provider variables
> set there are invisible to it. Put them in the unit file, the plist, or a
> wrapper script named by `NRW_HARNESS`. This is the most likely reason an
> agent that works by hand does nothing overnight.

These variable names are current as of writing and come from Claude Code's own
documentation — [Microsoft Foundry](https://code.claude.com/docs/en/microsoft-foundry),
[Amazon Bedrock](https://code.claude.com/docs/en/amazon-bedrock),
[Vertex](https://code.claude.com/docs/en/google-vertex-ai),
[LLM gateways](https://code.claude.com/docs/en/llm-gateway). Those pages are
the authority; this one will go stale first.

#### Which model the session uses

`nrw agent run --model NAME` passes straight through to the harness. On a
third-party provider that is a *deployment name*, not an Anthropic model ID.
Leave it off and the harness uses whatever your `ANTHROPIC_DEFAULT_*_MODEL`
pinning resolves to.

### Or point us at a different harness

```bash
export NRW_HARNESS="my-agent"                       # a name or a path
export NRW_HARNESS="claude --settings /etc/site.json"   # or a command
export NRW_HARNESS="$HOME/bin/harness-wrapper"      # or a wrapper script
```

`nrw doctor` reports what resolved, and marks it when the override is in play:

```
  ✓ harness       2.1.156 at ~/.local/bin/claude --settings /etc/site.json  [NRW_HARNESS]
```

**What we promise, and what we do not.** Claude Code is the only harness this
is tested against. The contract your command has to meet is:

```
<harness> -p --max-turns N --output-format stream-json --verbose
          --permission-mode bypassPermissions [--model M]   < prompt
```

…reading the prompt from stdin as UTF-8, and emitting Claude Code's
newline-delimited JSON events on stdout, also UTF-8. A wrapper script that translates those arguments is the
intended seam. One was verified end to end while the prompt was still named as
`-p @<prompt-file>`; a wrapper written for that form must read stdin now,
because Claude Code stops delivering a named file's text once it is large
(docs/ground_truths.md, 2026-10-04).

If your harness emits a different event format, everything still works except
the progress lines, which go quiet — `describe_event` returns nothing for a
shape it does not recognise rather than failing. The transcript, the fits and
the records are unaffected, because those come from `nrw` commands, not from
the harness's output.

**The two limits do not depend on the harness.** `NRW_AGENT=1` is set on the
session whatever runs it. The `PreToolUse` hook is Claude Code's mechanism, so
a different harness must provide its own equivalent — if yours cannot, the
environment variable is the only layer left, and `nrw agent run` will still
refuse to start without a configured hook. That refusal is deliberate; read it
as "this harness is not yet set up safely", not as a bug.

### OpenCode

`nrw init --harness opencode` scaffolds a project OpenCode can work in: an
`AGENTS.md` it reads directly, an `opencode.json` naming the shared workflow
file and denying `nrw promote`, a dispatcher per skill in `.opencode/agents/`,
and the guard plugin below. `nrw agent run --harness opencode` drives it.

```bash
nrw agent run Sample4 --harness opencode --timeout 7200
nrw agent run Sample4 --harness opencode --timeout 7200 \
    --model anthropic/claude-sonnet-4-5
```

**The limits are three, not two.** `.opencode/plugins/nrw-guard.js` hooks
`tool.execute.before` and shells out to `nrw agent guard`, so every bash command
is judged before it runs by the same code the Claude Code hook calls — no
refusal logic is duplicated in JavaScript. The deny rules in `opencode.json` are
the second, and `NRW_AGENT=1` the third. That is one more than Claude Code gets,
because `--auto` auto-approves only what is *not* explicitly denied, whereas
`--permission-mode bypassPermissions` drops the deny list entirely.

Measured against opencode 1.18.18, not assumed: a `--force` command that the
deny rules do **not** match was refused by the plugin under `--auto`, and with
the plugin disabled via `--pure` the deny rules refused `nrw promote` on their
own. `nrw agent run` refuses to start if either is missing.

**OpenCode 2 needs its own plugin file, and its own server.** Measured on
2.0.22:

- **The plugin.** It refuses to load `nrw-guard.js`, and says so only in its own
  log, so a project with that file alone has no plugin guard at all under
  OpenCode 2. `.opencode/plugins/nrw-guard-opencode2.js` is the same guard in the
  form OpenCode 2 loads: a default export with an `id` and a `setup`, hooking
  the tool's `execute.before`. The tool that runs a command is now `shell`, not
  `bash`. The two files cannot be merged into one, because OpenCode 1 calls
  every export as a function. Each version therefore warns about the other's
  file in its log, which is expected. `nrw agent run` refuses to start if
  either file is missing, and `nrw init` writes both.
- **The server.** By default, `opencode run` hands the session to a shared
  background service, and the session belongs to the service. Killed at its
  timeout, the `opencode run` nrw had started went away, and the session's
  command still ran. With `--standalone`, each session gets a private server
  that exits with it, so `nrw agent run` passes `--standalone` to any OpenCode
  that does not report version 1. A command already running when a session is
  stopped can still finish, as with Claude Code.

**`--turns` does nothing here.** `opencode run` has no `--max-turns` equivalent,
so an OpenCode session is bounded by the clock and nothing else. `nrw agent run
--harness opencode` therefore *requires* `--timeout`, and says so rather than
letting `--turns` look like a limit it is not. This is a genuinely weaker
guarantee than the Claude Code path and is worth knowing before leaving one
running overnight.

One smaller difference: `--model` wants `provider/model`, e.g.
`anthropic/claude-sonnet-4-5`, not a bare model name. The prompt goes in on
stdin, as it does for Claude Code: `opencode run` takes its message
positionally, and a composed session prompt is far past what belongs in argv.

One sharp edge: **`opencode --pure` skips external plugins**, and therefore
skips the guard. Nothing here passes it, but a site wrapping `opencode` in its
own script should not add it.

### If you only have an endpoint

(If what you have is Claude Code and no endpoint, the reverse also works —
see [The harness can be the endpoint](#the-harness-can-be-the-endpoint).)

Everything except `nrw agent run` and `nrw agent watch` works exactly as
documented, by hand — and the endpoint powers `nrw assess`'s judgement,
`nrw model new --from-notes`, and `nrw isaac export`'s condition sentences,
since those only step aside when a harness is driving.
[docs/getting-started.md](getting-started.md) is that workflow end to end.

---

## Permissions, and why the hook still holds

The harness runs with `--permission-mode bypassPermissions`. That is not a
shortcut; without it an unattended session accomplishes nothing.

Measured: a 45-turn run with only a `deny` list configured spent every turn
being told **"This command requires approval"** — headless mode has nobody to
approve — and then tried to write itself a `.claude/settings.local.json` to
get out. Not one `nrw` command ran.

Bypassing turns off Claude Code's permission layer, including the `deny` rules
in `.claude/settings.json`. It does **not** turn off the two mechanisms this
package relies on, which was verified under that exact flag:

```
CALL:   Bash {"command": "nrw promote abc123 --reason test"}
RESULT: PreToolUse:Bash hook error: Refused by nr-workbench (promote): …
```

Hooks are independent of permissions, and `NRW_AGENT=1` refuses from inside
`nrw` regardless. The `deny` list was always the weakest of the three layers,
and the only one that needed a person at a keyboard to mean anything.

Because the hook is now load-bearing, `nrw agent run` **checks it before every
session** and refuses to start without it. A session can edit
`.claude/settings.json`; checking once would leave the next session
unprotected, and a previous session having removed it is worth knowing before
you trust what it wrote.

---

## Compute, during a beamtime

The session is told to explore with `--method amoeba` while the beam is
running and the model is still moving, and to switch to `--method dream`
before quoting any uncertainty, claiming two fits differ, or writing the note
that settles on a model — amoeba has no posterior, so none of those are
answerable from it. That matches how this is done by hand and by AuRE, and the
reason amoeba leads is throughput: it keeps pace with arriving data, DREAM does
not. One fit at a time.

By default nothing enforces this. It is guidance in the prompt, not a limit,
because a harness that decides a long DREAM run is the right call at 2am is
probably right.

A project that wants a ceiling anyway sets one in `nrw.toml`:

```toml
[agent.limits.dream]
samples = 100000
burn = 1000
steps = 300
pop = 10
```

Under `NRW_AGENT`, `nrw fit run` then refuses a fit that asks for more, before
it starts, and names the limits and where each value came from, so the session
can run it again within them. The comparison is with what the fit would
actually run with, which is the command line, then `[fit]`, then bumps'
defaults. A fit that names no setting at all is checked too, unlike the
command line a hook sees. Each fitter has its own table
(`[agent.limits.de]`, `[agent.limits.amoeba]`), holding that fitter's own
settings. A setting with no limit is not limited, and a person's own fits
never are. The check is inside `nrw`, so it holds whichever harness is
driving.

`nrw init` keeps the table, and `[fit]` with it. It never re-renders a file a
person has edited, and it renders the values back into a file that a tool
wrote through nrw's own editor, as nr-watcher does.

---

## When it goes wrong at 3am

The watcher is the only part designed to run for eight hours with nobody
watching, so its failure handling is deliberate rather than incidental:

- A **poll that raises** — a sample directory removed mid-run, an NFS `stat`
  that failed — is reported and retried. It does not end the night.
- A **measurement that cannot be assessed** is quarantined with the exception
  in its reason, not skipped silently and not fatal to the other samples.
- A **session that hangs** is killed at `--session-timeout`, and killed as a
  process group, so the `refl1d` fit it started dies with it rather than
  carrying on writing into the project.
- A **session that fails** for any other reason is reported and the loop moves
  to the next sample.

What is *not* handled that way: a run whose files look incoherent. That stops
being fitted entirely and waits for you. A false quarantine costs one question
in the morning; a false pass costs the night.

---

## Reading it in the morning

The output rule is one report per sample under `samples/<id>/reports/`,
rewritten rather than appended, plus `ESCALATIONS.md` at the root. The report
is one analysis at three altitudes, in three files that `nrw report --check`
holds to the same answer: `-technical.md` (every branch, and what abandoned
it), `-si.md` (for a peer reading the paper) and `-plain.md` (for a colleague
who does not fit reflectivity). This is a property of
the design, not a preference. Forty individually defensible records are
collectively unreadable, and once you stop reading them, every other safety
property here is a formality.

The order to read in:

1. `ESCALATIONS.md` — what it could not decide.
2. `nrw ls --sample <id>` — every fit, newest first, with what changed between
   each and the one before.
3. `samples/<id>/reports/` — what it concluded.
4. `nrw whence`, `nrw diff`, `nrw assess` — anything you want to check.

Then promote by hand, with a reason — here, or with **Finalize** on the fit's
page in `nrw serve`:

```bash
nrw promote 20260810-231402Z-4f2a8c1e --as final \
    --reason "oxide layer is real; excluding it costs 0.4 in chisq and leaves a residual at 0.08"
```

The reason is the part that matters in a year, which is most of why this step
is still yours.

---

## Attribution

There is no agent badge on a fit record, on purpose. `provenance.command`
already records the exact command line that produced every fit, so the record
answers "what produced this" without a new field — and a flag saying
`agent: true` would invite exactly the wrong habit of trusting a record more
because a person's name is on it. Judge the fit by the fit.

---

## What this deliberately does not do

- **Decide anything.** No Python scoring loop. The evidence above says a
  harness is better at this, and a worse copy of it inside nr-workbench would
  be used instead of the good one.
- **Promote or upload.** Both refused, twice.
- **Rewrite `sample.yaml`.** `nrw sample scan` stays a human gesture — it is
  the one that removes a mis-filed run from the register, which is a curation
  decision.
- **Offer an MCP surface.** The CLI is the tool surface and the harness already
  drives it; a second surface is drift with no new capability.
