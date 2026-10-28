# The experiment page: organizing a beamtime's runs

During a beamtime, reduced runs land in a folder on the data mount one after
another. The **Experiment page** shows all of them in one table, lets you say
which sample each belongs to and how it was measured, and writes that into the
project: the data into `samples/<id>/data/`, and the context into
`samples/<id>/sample.md`, where the rest of the workflow already reads it.

Nothing downstream changes. `nrw model new`, `nrw data reconcile`,
`nrw agent watch` and the rest see a sample the page organized exactly as they
see one you set up by hand.

## Start it

From an empty folder:

```bash
nrw init
nrw serve
```

```
  http://127.0.0.1:8765/experiment      the experiment's runs
  http://127.0.0.1:8765/settings        its IPTS, data folder, watcher and language model

  Data folder  /SNS/REF_L/{ipts}/shared/autoreduce/new_reduction  (nrw's default)
  ! The data location /SNS/REF_L/{ipts}/shared/autoreduce/new_reduction needs the
    project's IPTS, and nrw.toml has none. …

  This experiment is not set up yet: nrw needs its IPTS, or the
  folder its reduced data is in, before it can watch anything.
```

On a project that is not set up yet, start with **Settings** (see
[Settings](#settings) below). The pages edit the project when the server is
bound to loopback, as it is by default; started with `--host`, it only shows it.

The same organization is available from the command line, for scripts and for
anyone who prefers it:

```bash
nrw experiment settings --ipts 34347 --write   # or the Settings page
nrw experiment status              # every run, its state and its sample
nrw experiment assign 234277 234280 --sample Sample6 --type steady --condition OCV
nrw experiment apply               # shows what would be written; changes nothing
nrw experiment apply --write       # writes it
```

## Settings

The Settings page sets what the Experiment page watches. Everything it saves
goes into `nrw.toml`, which you can also edit by hand, except the language
model, which is saved in the project's `.env` ([Language model](#language-model)).

- **The experiment.** Its IPTS and beamtime label. When the project's own path
  names an IPTS (`/SNS/REF_L/IPTS-34347/shared/…`), the page offers it.
- **Where the data is.** Either nrw's default location (see below), shown
  resolved as you type the IPTS, or another folder. **Check folder** says what
  a folder holds before you choose it: how many runs and their range, the
  newest runs, files it does not recognise, and the IPTS their headers name.
  A run whose headers name an experiment other than the IPTS on the page is
  flagged, run by run.
- **How new runs are noticed.** Files appearing in the data folder. The SNS web
  monitor is listed as coming and cannot be chosen yet.
- **Where samples and assignments are kept.** Parquet files in this project's
  `experiment/`, committed with it. `nrw tiled push` shares a copy with
  everyone on the experiment; see [experiment-sources.md](experiment-sources.md).
- **Advanced**: the settle time and the poll interval.

A save changes only the lines of `nrw.toml` that hold these settings, keeps
every comment and hand edit, and keeps the previous file under
`.nrw/backups/`. It takes effect in the running server straight away, with no
restart. If `nrw.toml` was edited by hand since the page loaded it, the save is
refused and the page reloads, so nothing is written over. If the file writes a
setting in a shape nrw does not edit (an inline table, say), the page shows the
lines to change by hand instead. Changing the IPTS of an experiment whose
catalog already holds runs asks first.

`nrw experiment settings` does the same from the command line. With no options
it shows each setting and whether it follows nrw's default. With options it
shows the change as a diff, and `--write` saves it. `--check` looks at the
folder first, as **Check folder** does, with the same 15-second deadline.
`--json` prints one object, whichever options are given: `{"settings",
"change", "check"}`, where `change` and `check` are `null` unless asked for.

### Language model

**New model**, **Quick fit with AuRE** and ISAAC's **Export** ask a language
model. The *Language model* section says which one they use now, and which file
each part of that comes from. Its choices are:

- **Claude, through the Claude Code CLI.** AuRE runs `claude -p`, which answers
  as whatever it is logged in as: a subscription, an API key, or Bedrock,
  Vertex or Foundry. No key is needed. Leave the model blank for the CLI's
  default, or name one: `sonnet`, `opus`, `haiku`, or a full model or
  deployment name.
- **As set outside this project**: whatever `~/.nrw` or `~/.aure` says.

A third, **As this project's `.env` sets it**, is shown only when `.env` names
another provider, written there by hand. It cannot be chosen, only kept or
left: choosing Claude replaces those lines, and choosing *as set outside this
project* removes them.

Unlike the rest of the page, this is saved in the project's `.env`, not in
`nrw.toml`. `nrw.toml` is committed and shared, while a language model belongs
to one person on one machine, and `.env` is gitignored. Choosing Claude writes
two lines:

```
LLM_PROVIDER=claude_code
LLM_MODEL=
```

`LLM_MODEL` is written even when blank. Otherwise a model that `~/.aure` names
for another provider, such as `gpt-4o`, would be passed to `claude`. Choosing
*as set outside this project* removes the `LLM_PROVIDER` and `LLM_MODEL` lines,
whoever wrote them: after a choice of Claude, that leaves `.env` exactly as it
was before. Nothing else in `.env` is changed. The section says
so, and changes nothing, when `.env` is something it should not edit: a
file that is not text, or one changed by hand since the page read it.

Each job reads `.env` as it starts, so a choice applies from the next one, with
no restart. The exception is a variable set in the environment `nrw serve` was
started with: that wins over `.env`, and the section says so.

**Check** makes one real call to the saved model, as `nrw check-llm --endpoint`
does, and says what answered and how long it took. It makes exactly one call,
with AuRE's retries off, and it is billed like any other. It waits as long as
AuRE waits for a call (`LLM_TIMEOUT`, 120 seconds by default) and a minute more,
never more than ten minutes, then stops, along with any `claude` it started.
`nrw doctor` shows what is configured without making a call.

## Where the runs come from

By default the page watches the folder REF_L's `new_reduction` pipeline writes:

```
/SNS/REF_L/{ipts}/shared/autoreduce/new_reduction
```

with `{ipts}` taken from `[beamtime] ipts` in `nrw.toml`. **That location is
provisional** and expected to move. A project that keeps the default follows it
when nrw's default moves. To point somewhere else, use the Settings page or
`nrw experiment settings --location PATH`. By hand, edit the
`[experiment.source]` table `nrw.toml` already has:

```toml
[experiment.source]
location = "/SNS/REF_L/{ipts}/shared/autoreduce/new_reduction"
```

Uncomment it if it is still commented out, but do not add a second one: TOML
allows each table once, and a file that declares it twice stops every `nrw`
command until one is removed.

`nrw experiment status` says whether the folder can be reached, and names any
key in `[experiment]` it does not recognise rather than ignoring it.

The folder is re-read every 30 seconds while the page is open, and not at all
once nobody has looked for ten minutes. If the data mount stops answering, the
page keeps showing what it last saw and says so, rather than hanging.

## What each state means

| State | Meaning | Applied? |
|---|---|---|
| **complete** | Its files have stopped changing, *and* there is evidence the measurement ended. | Yes |
| **unconfirmed** | Its files have stopped changing, but nothing shows the measurement ended. | Only if you confirm it |
| **arriving** / **settling** | A file changed recently. | Not yet |
| **awaiting** | A run feed announced it, but no reduced files exist yet. | Not yet |
| **quarantined** | Its files do not look like one measurement. The page says why. | No |
| **clock skew** | Its files are dated in the future by the file server's clock. | No |

**Why "stopped changing" is not enough.** A run's angle segments are reduced as
each angle finishes measuring. In the reference experiment, run 218386's three
segments landed 15 and then 52 minutes apart. Five quiet minutes after the
first segment, it looks finished, and a copy made then is a third of a
measurement. It fits perfectly well, which is the problem.

So *complete* needs evidence the run ended, and one kind counts: **a later run
has been reduced.** Once the instrument has moved on and the next run's files
exist, this one is not getting more segments.

- **The planned segment count can only say "not yet".** When the
  `new_reduction` header says three segments were planned and two are here,
  the run is not complete, however much later data has arrived. When all three
  are here, that alone does not make it complete either: nobody has yet checked
  on a real file that the *first* segment's header already carries the whole
  plan. If it does not, a header read after segment 1 says "1 of 1" and would
  call a third of a measurement finished.
- **A run the feed announces does not count as the later run.** The
  announcement means the next acquisition started. This run's last segment may
  still be reducing.

A settled run without that evidence is *unconfirmed*. The last run of a
beamtime always is, because nothing comes after it. If the measurement is really
over, or was stopped early, tick "use run N as it is" when you review apply
(`--confirm N` on the command line).

## Organizing

Select runs in the table, choose a sample (or type a new id), optionally a
**type** and a **condition** (`OCV`, `-0.5 mA/cm2`, …), and **Assign**.
**Exclude** keeps a run in the sample but out of its data.

The type is **steady** or **tNR**, or one you add with **Add a type…**; a type
added once is offered from then on. A run whose type was never set is steady.
In the bar above the table the type starts at *unchanged*, so assigning runs
leaves the type each one already has.

**The experiment**, the panel above the samples, is what every sample here
is part of: a **Summary** of what the proposal sets out to learn, the
**Hypotheses** the measurements should confirm or rule out, and **Notes** for
anything else every measurement needs. It opens by itself until something is
written in it, and then folds away, showing its first line. It is kept in the
catalog and written to no file, so this panel is the one place to change it;
`nrw experiment status --json` shows it. It is written into no `sample.md`,
so none of the rules below apply to it. The unattended agent's instructions
quote it ahead of each sample's task, **New model** sends it beside the
sample's notes, and `nrw tiled push` writes it to the experiment's `context/`
on Tiled.

Click a sample to fill in its context. Each box becomes the section of
`sample.md` it is named after:

- **Description** and **Details**: what the sample is.
- **Was the sample moved between measurements?** This decides whether alignment
  is fitted once or once per state, and it cannot be read from the data.
- **Measurement conditions**: what holds for every measurement.
- **Measurements**: one line per run, with its type, its condition, a
  **good** switch, and notes on that measurement alone: realigned, bowed, a
  segment that looks high. Type and condition go into the measurement table.
  Each run's notes go under *Measurement conditions*, with the run named first
  (`- Run 218386: realigned after mounting`), so nobody has to guess later
  which measurement a note is about.
- **Fits to perform**: an unattended agent session will not start without it.

A bad run is slid to **bad: not used**, which excludes it: the next apply
leaves it out of the table and moves its copy out of `data/steady/`. Say why in
its notes. They stay in the catalog and on the page, but are not written into
`sample.md`, whose readers would otherwise look for the run's data.

The preview shows the `sample.md` that will be written.

Some text is refused, with the reason, because a reader of `sample.md` would
misread it:

- **A heading line inside a box.** A `## Fits to perform` typed into the
  Description would become the agent's task.
- **`<!--` or `-->`.** These hide text from readers that strip comments.
- **A `|` in a condition.** It splits the table cell.
- **A second table with a Run column.**

A run's notes may not hold `<!--`, `-->` or a table with a Run column either.
A heading line in them is kept: sample.md indents a note's lines under its
run, where no reader takes one for a heading.

## Applying

**Review** shows exactly what apply would do:

- **+ copy**: new files into `samples/<id>/data/steady/`. These are the files a
  fit reads: each run's angle segments, which fits co-refine. A combined curve
  beside them stays at the source. A run that has only a combined curve is
  copied as that.
- **− move-out**: copies of runs you have since excluded, or moved to another
  sample, go to `samples/<id>/data/excluded/<run>/`. They are moved back if you
  include the run again. They are never deleted.
- **sample.md**: created, updated, or left alone, with the reason.

Nothing is ever overwritten. Each case below is shown and left alone for you to
decide:

- **source-changed**: the facility's file was re-reduced after it was copied.
- **local-edited**: the copy in the sample was edited.
- **conflict**: a different file with the same name was already there.
- **sample.md edited by hand**: kept as it is. The catalog's version goes
  beside it as `sample.md.nrw-new`.

`samples/<id>/data/sources.json` records what apply copied and from which
version of the source file; it is how those cases are told apart.

Only runs that are *complete*, or that you confirmed, are copied. A run is
copied whole or not at all.

## Models

Once a sample has data, the **Models** panel below Apply writes a model spec
for it: give the model a name and click **New model**. The spec is exactly what
`nrw model new <sample> --name <name> --from-notes` writes:

- **Its states come from the data** in `samples/<id>/data/`: one per run, each
  segment's angle read from its own file.
- **Its stack is proposed by the language model** from `sample.md`: the
  *Description* and *Details*, each run's condition in the Measurements table
  and its notes, and the sample's *Measurement conditions*. Each state is sent
  with its own condition, and the model is told to take the ambient from the
  notes, never to assume air, and to fit a contrast that differs between runs
  (D2O for one, H2O for another) as the ambient's SLD per state. It is told,
  not forced: check the ambient in what it proposes.

Writing it is a job, followed in the Fit panel, because the model's answer can
take a minute. One job runs at a time, so while a fit runs, New model waits and
the panel says so. Without a language model the job says so, and writes nrw's
placeholder stack instead: air on a film on Si. The panel marks such a spec
*placeholder stack* until its layers are edited, and **Fit…** on it says that
its fit would mean nothing before it runs one.

The panel lists the sample's specs, and marks those a script has been
generated from. Like AuRE, New model reads `sample.md` as it is on disk; the
panel says when there are edits saved on the page that it does not have yet.

A spec that already exists is never overwritten: choose another name, or edit
that one. A name is a plain name (letters, digits, `.`, `-`, `_`), because it
becomes the spec's filename.

### A quick fit with AuRE

**Quick fit with AuRE** writes a new model whose stack is proposed rather than
a placeholder. AuRE fits one run on its quick budget; choose the run when the
sample has more than one. The spec it proposes models that one run: to
co-refine several, write one with **New model**. From `sample.md` it reads:

- the sample's *Description* and *Details*. A sample with nothing under
  *Description* is refused, because AuRE builds the whole model from it. Say
  which side the beam enters in words AuRE's setup looks for, such as "measured
  through the silicon substrate" or "back reflection": without them, or with
  them negated ("not through the substrate"), the setup takes the beam to
  arrive from the ambient side;
- *Fits to perform*, as a hypothesis;
- for the run it fits, its condition in the Measurements table, the sample's
  *Measurement conditions*, and the notes on that run.

AuRE reads `sample.md` as it is on disk. Edits saved on this page reach the
file only when you **Apply**, and the Models panel says so when there are some
it does not have yet.

AuRE's own run is reconnaissance: the fit that counts is the one `nrw fit run`
records. So the page goes on to import the spec AuRE proposed, generate its
script, and fit it from AuRE's values with the project's fitter, as **Fit…**
does: the `method` in `nrw.toml`'s `[fit]`, else **dream**, so the fit carries
uncertainties. That fit is the one in **Fits**. A DREAM fit takes minutes
rather than seconds; set `method = "amoeba"` under `[fit]` for a fast one. The
whole job is the five commands you could type yourself:

```bash
nrw aure new <sample> --name=<name> --run=<run>
nrw aure run samples/<id>/aure/<name>/setup.yaml --budget=quick
nrw aure import samples/<id>/aure/<name>/output --sample=<id> --name=<name>
nrw model generate samples/<id>/models/<name>.yaml
nrw fit run samples/<id>/models/<name>.py
```

AuRE needs a language-model endpoint. Without one, the job stops at `aure run`
and says how to set one. `nrw doctor` shows what is configured, and
`nrw check-llm` makes one real call to prove it answers. AuRE's
working files stay in `samples/<id>/aure/<name>/`.

**Quick fit again** beside a model AuRE proposed asks AuRE again, about the
same run it fitted before, for example after you have added to `sample.md`:

- The new proposal replaces the spec, and is fitted like the first. Both fits
  stay in **Fits**.
- Each run of AuRE keeps a folder of its own (`aure/<name>/`, then
  `aure/<name>-2/`, ...), so earlier runs are never overwritten.
- A spec you have edited since AuRE proposed it is never replaced, even when
  the edit is made while AuRE is running. Fit it as it is with **Fit…**, or
  quick-fit under another name.

AuRE names layers in prose, such as `silicon oxide`. A spec's layer names are
identifiers, because they are also parameter paths (`silicon_oxide.rho`) and
variables in the generated script. So the import writes `silicon_oxide`, says
what it renamed, and keeps AuRE's own names as comments at the top of the spec.

### Fitting a model

**Fit…** beside a spec starts a fit of it. The fitter starts at the project's
default, marked as such: the `method` in `nrw.toml`'s `[fit]`, else **dream**.
The choices are:

- **dream** samples. It is the only one that gives uncertainties, and it takes
  **samples** and **burn** as well as **steps**.
- **de** explores the whole range of every parameter. Use it when amoeba stalls
  on the starting point rather than on the model.
- **amoeba** explores downhill from the starting values, fast. Use it while you
  are still changing the model.

Each box starts at the value the fit would use: the project's, from
`nrw.toml`, else bumps' default. A line under the boxes says which is which.
DREAM's steps follow from its samples, so that box says "from samples". Only
what you change is sent: the rest `nrw fit run` takes from the same place, so a
fit started here is recorded just as one typed in a terminal. The README's
"Fitting options" section has the `[fit]` tables. The note, if you give one, heads
the fit's `NOTES.md` as why it was run, so it is one line, and does not start
with `#`. The page runs the same two commands you would, one after the other:

```bash
nrw model generate samples/<id>/models/<name>.yaml
nrw fit run samples/<id>/models/<name>.py --method=dream --verbose ...
```

So the fit is recorded exactly as `nrw fit run` records it, and appears in
**Fits** with its full provenance. The script is generated from the spec every
time. A script someone edited by hand is refused, not run, so a fit started
from the page always runs what its spec says.

The **Fit** panel follows the job: its output as it runs, then a link to the
fit it recorded, until you close it. It shows the job running now, not the ones
before it: past fits are on the **Fits** page. One job runs at a time for the
project, whichever sample it is for, and that includes ISAAC's steps started
from a fit's page: the panel here follows those too, and its **Cancel** is the
one that stops them. Its output is kept in `.nrw/jobs/`, which
git ignores, for the newest 20 jobs. Like the rest of the page, it can be read
by anyone who can open the page, link or not.

Fitting again when nothing has changed -- the spec, the data, the settings --
records a replicate of the fit you have, as `nrw fit run` does, and its log
names that fit. Notes in `sample.md` are not among what a fit reads: to fit
what you wrote there, put it in the spec, or ask AuRE again.

**Cancel** stops the fit and every process it started. What the fit had written
so far stays where it is, and `nrw check` lists it as an interrupted run, as it
does for a fit stopped in a terminal. Stopping `nrw serve` stops its running
job too. If the server was killed instead, the page says so the next time it
starts, and a fit that went on to finish is in Fits all the same.

## Curating fits

Setting up and running fits happens on this page. Judging them happens where
their evidence is: a fit's own page (**Fits**, then a fit), with its curves,
residuals, parameters and the other fits of its model.

- **☆ Star** marks a fit worth coming back to, and **★ Starred** takes the
  star away. The Fits list has a star beside each fit, which does the same.
- **Finalize…** makes a fit the answer for its sample, which is `nrw promote
  --as final` from the page. It asks why, and the reason is kept with the fit.
  It says which fit it replaces as final, and that one stays in the history. A
  fit whose data changed since it ran is not finalized unless you say so again,
  and that is recorded too. A fit that failed is offered the button but
  refused: only a fit that finished can be the answer.
- **Discard…** sets a fit aside, with a reason. It leaves the listings, and
  the Fits list keeps it behind **Show the discarded**. Every file is kept, and
  **Restore** brings it back. The final fit cannot be discarded: finalize
  another first.
- **Delete files…** frees the disk, the fit's `NOTES.md` included. It is
  offered only for a fit already discarded, and only after you confirm. A fit
  whose files another fit read keeps them, and the page names that fit. The
  record that the fit ran stays in the index. The Fits list shows it as deleted,
  behind **Show the discarded**, and its own page is gone with its files.

Each of these is recorded in the fit index (`.nrw/index.jsonl`) with who and
when, as promotions always have been, so it travels with the project in git.
The terminal does the same:

```bash
nrw fit star <fit_id>
nrw fit unstar <fit_id>
nrw fit discard <fit_id> --reason "the Ti layer diverged"
nrw fit restore <fit_id>
nrw fit delete <fit_id>      # a discarded fit's files; asks first
nrw promote <fit_id> --as final --reason "..."
nrw ls --all                 # the discarded fits too
```

An unattended agent can do none of them: which fits are good, set aside or
deleted is a person's judgement.

### Publishing to ISAAC

The final fit of a sample can be published to the ISAAC Portal as AI-Ready
Records. Only the final fit: its page shows an **ISAAC** panel once it is
finalized. The panel has three steps, each run as a job:

1. **Export** writes the records into the fit's `isaac/`, as `nrw isaac export`
   does, and checks each against the schema. With a language model configured
   it also asks it to read `sample.md` for each run's conditions, a billed
   call; `nrw isaac export --no-llm` uses the Measurements table alone.
2. **Validate with the server** sends those records, with your key, to the
   portal, which checks them and keeps nothing. It asks first, naming the
   portal.
3. **Push…** publishes exactly those records, after you confirm. What leaves
   the project is the data, the fitted model and the notes: `sample.md`, the
   fit's `NOTES.md`, the reason it was finalized, and any report that cites
   it. It does not export again, so what the server checked is what it gets.
   Once the push has run, it is recorded with the fit: the files it sent and
   their digests, the id of every record the portal made, and whether it
   finished. Pushing again adds new records and replaces none, and the page
   says so first.

Once another fit is finalized in its place, the panel offers nothing more, but
a fit that was pushed keeps its list of pushes on its page.

The panel needs `data-assembler` and `nr-isaac-format`, which come with the
`isaac` extra (re-run the installer with `NRW_EXTRAS=isaac`; see
[install.md](install.md#options)), and the portal and its key. Put them in
`~/.nrw`, your own file:

```
ISAAC_URL=https://isaac.slac.stanford.edu/portal/api
ISAAC_KEY=...
```

They are read as nrw's other settings are: the shell, the project's
`.env`, `~/.nrw`, then `~/.aure`. The panel says where each is set, and the
portal's host. It never shows the key. From the terminal:

```bash
nrw isaac export <fit_id>
nrw isaac push <fit_id> --validate-only
nrw isaac push <fit_id>          # the final fit only; asks first
```

A fit whose records were pushed keeps its files, even once it is discarded.
Neither validating nor pushing is done by an unattended agent: both send the
records, and the key, off the machine.

## A sample that already exists

A sample written by hand before the page existed can be taken into the catalog:

```bash
nrw experiment adopt Sample6                    # shows what would be recorded
nrw experiment adopt Sample6 --write            # records it; sample.md untouched
nrw experiment adopt Sample6 --write --rewrite  # and lets the catalog write sample.md
```

Its sections become the sample's context and its measurement table becomes the
run assignments. Runs you assigned to the sample on the page that its table
leaves out are kept as they are: the file never listed them, so leaving one out
is not removing it. Anything with no place in the catalog, such as your own
`## Notes` section or a comment you wrote, is listed as a **leftover**. A
leftover blocks `--rewrite`, because the rewrite would lose it.

The same step pulls hand edits back. If you edit a catalog-written `sample.md`
in your editor, the page shows **edited by hand** and offers **Pull hand edits**
to bring your changes into the catalog.

An entry `- Run 218386: …` under *Measurement conditions* becomes that run's
notes, continuing on the lines indented under it. A pull changes a run's notes
only where the file has an entry for the run; an entry left empty
(`- Run 218386:`) clears them. A run with no entry keeps its notes, because a
file written by hand, or before nrw wrote notes, never had any.

`--rewrite` backs the old file up under `.nrw/backups/`. That directory is
gitignored, so the backup exists on this machine only; commit first if the file
matters.

To stop the catalog writing a sample's `sample.md` altogether:

```bash
nrw experiment release Sample6
```

## Where it is stored

| File | What | Committed |
|---|---|---|
| `experiment/runs.parquet` | One row per run: its sample, type, condition, good or not, its notes | Yes |
| `experiment/samples.parquet` | One row per sample: its context | Yes |
| `experiment/experiment.parquet` | One row, once written: the experiment's own context, its summary, hypotheses and notes | Yes |
| `samples/<id>/data/sources.json` | What apply copied | Yes |
| `nrw.toml` (`[beamtime]`, `[experiment.*]`) | The IPTS, where the data is, and how new runs are noticed | Yes |

The tables are parquet: the same shape the facility's data lakehouse uses,
so that a later version can read the catalog from a facility service instead.
Parquet is binary, which has two consequences:

- **git cannot merge it.** If two people organize the same experiment on
  different branches, the merge leaves one side in place with no conflict
  markers: keep one side, and make the other's edits again on the page.
- **A diff does not show what changed.** `nrw experiment status --json` does.

A catalog file that exists but cannot be read is never treated as empty. The
page shows it read-only, and nothing is saved over it until it is restored.

**Sharing it on Tiled.** `nrw tiled push` writes the catalog to the
experiment's `context/` on tiled.ornl.gov, where everyone on the experiment,
and other programs, can read it; `nrw tiled status` says what is there, and
`nrw tiled pull --data` copies the reduced files in when the data mount is out
of reach. The catalog stays here: Tiled holds a copy, as of the last push,
and the last writer wins. See [experiment-sources.md](experiment-sources.md),
"Tiled".

## Unattended agents

An assistant can read the experiment, with `nrw experiment status --json` and
the page's API, and can preview `apply` and `settings`. It cannot assign,
apply, adopt, release or change the settings. Which sample a run belongs to is a claim about the experiment, like
promoting a fit, so the project's hook refuses those commands, and the agent
is told to write its proposal in `ESCALATIONS.md`.

## Not yet

- **Sliced time-resolved (tNR) series.** The unsliced tNR run arrives in the same
  folder as an ordinary reduced run and is treated as one. Series support comes
  later: slices plus their reduction sidecar, tied to the unsliced run, which
  gives the angle and must never be co-refined with its own slices.
- **Other sources of runs.** The SNS web monitor, or Tiled. See
  [experiment-sources.md](experiment-sources.md).
