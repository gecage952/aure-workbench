# Experiment sources, feeds and stores: how the pieces plug in

The Experiment page (see [experiment.md](experiment.md)) answers three questions,
each behind its own small interface in `nr_workbench.experiment`, so each can be
replaced without touching the others or the page:

| Question | Interface | Built | Planned |
|---|---|---|---|
| Where does the reduced data come from? | `DataSource` (`experiment/sources/`) | a local folder | none: `nrw tiled pull --data` copies Tiled's files into it |
| How do we learn that a run exists? | `RunFeed` (`experiment/feeds/`) | files appearing in that folder | the SNS web monitor |
| Where is the organization kept? | `CatalogStore` (`experiment/store.py`) | parquet in the project | none: `nrw tiled push` publishes a copy |

They are configured independently in `nrw.toml` (`[experiment.source]`,
`[experiment.feed]`, `[experiment.catalog]`), on the Settings page of
`nrw serve`, or with `nrw experiment settings`. A planned kind is refused by
name, never replaced by the local folder: falling back would quietly watch a
path nobody chose.

**How a new kind reaches the page.** The Settings page and `nrw experiment
settings` list every kind from one registry, `SOURCE_OPTIONS`, `FEED_OPTIONS`
and `CATALOG_OPTIONS` in `project/experiment_schema.py`: the built ones to
choose from, the planned ones shown as coming. Adding a kind means building
it, registering it in `open_source`, `open_feed` or `open_store`, and setting
`available=True` on its `Option`. The kind lists the registries check, and
every surface, follow from that one flag.

## A measurement, and what a source lists

A **measurement** is what the catalog calls a run: it is keyed by its first run
number, and it is made of N angle segments, each reduced from its own subrun,
plus other **artifacts**. Today the one other artifact nrw reads is the
combined curve, every segment stitched into one file.

A source lists every artifact of a measurement it can see (`SourceRun.files`,
each with its `role`). It does not decide which ones matter. That is one rule,
in `instrument/reduced.py` beside the file-name grammar: `fitting_names` gives
the segments, or the combined curve when there are no segments. This is the
rule `nrw model new` fits by. Apply copies exactly those files
(`SourceRun.fitting_files`), so a sample holds what a fit of it reads, and the
quick look plots the same ones. A new artifact kind, such as tNR slices or a
reduction sidecar, gets a role there first.

## `DataSource`: files and their bytes, nothing else

```python
class DataSource(Protocol):
    kind: str
    def describe(self) -> dict: ...
    def inventory(self) -> Inventory: ...        # every run it can see, now
    def read_bytes(self, file: SourceFile) -> bytes: ...
```

A source never decides anything. Two decisions are made once, outside the
sources, so that every source is held to the same rules:

- **Whether a run is complete**: `experiment/status.py`.
- **How a copy is made safely**: `experiment/apply.py` (staged, whole-run,
  never overwriting).

Rules a source must keep:

- **Hand back the original bytes under the original name.** The header carries
  the incident angle (in radians) and the dQ convention (FWHM or sigma, a 2.355×
  difference). The `new_reduction` dialect records the segment number *only* in
  the filename. A source that re-encodes the data, or renames it, changes fits.
- **Name files exactly as the reduction does.** Apply accepts only names that
  `instrument/reduced.canonical_name` reproduces byte for byte.
- **`SourceFile.version` must change when the bytes do.** It is how "the facility
  re-reduced this after we copied it" is noticed. It may change when the bytes
  do not; apply then compares digests.
- **Never raise for missing data.** An unreachable source is an `Inventory` with
  `reachable=False` and a problem saying why.
- **`read_bytes` raises `SourceChangedError`** if the file is no longer the
  version listed. It is called under a deadline (`nr_workbench.bounded.Bounded`,
  with `SOURCE_TIMEOUT` from `experiment/sources`), so a slow source costs a
  request a timeout, not a thread.
- **`probe()` is optional, and cheap.** It backs the Settings page's **Check
  folder** and `nrw experiment settings --check`: a look at a source *before*
  it is chosen. It lists once and reads only the newest few runs, never the full
  inventory, and returns a `Probe`. A source without it is refused as "cannot be
  checked yet". The check builds the source exactly as the watcher would
  (`experiment.workspace.check_source`), so it reads the place that would be
  watched and no other.

**Where Tiled keeps an experiment.** On the facility server an experiment is
the container `projects/isaac/IPTS-<n>/`, which another process makes. Its
`autoreduction/` holds the reduced files as flat `bytes` nodes, named as the
files in `new_reduction/`, which carry no metadata. A Tiled *source* is not
planned: the folder stays the source, and away from the mount `nrw tiled pull
--data` copies those files into it ([Tiled](#tiled-pushed-and-pulled-by-a-person),
below). Nor is a Tiled feed: nothing the poller runs talks to Tiled.

The in-memory source in `tests/experiment_fixtures.py` is the second
implementation that keeps this interface from quietly becoming folder-shaped.

## `RunFeed`: "run N exists"

```python
class RunFeed(Protocol):
    kind: str
    def describe(self) -> dict: ...
    def poll(self, inventory: Inventory) -> FeedUpdate: ...   # a snapshot, not a delta
```

The feed is separate from the source because other feeds know different things,
and know them sooner:

- **The folder feed** (`feeds/directory.py`) learns of a run when its files
  appear. It is built on the source's own listing, so the folder is read once
  per poll.
- **The SNS web monitor** (`monitor.sns.gov`) reports each run as it is
  *acquired*, before any reduction exists. The page shows such a run as
  **awaiting reduction**, a state the folder can never show. The monitor's run
  lists need an ORNL login, so a monitor feed needs credentials. They belong
  where ISAAC's key does, in the project's `.env`, `~/.nrw` or the shell:
  never in `nrw.toml`, which is committed.

A feed returns every run it currently knows. `experiment/live.py` works out
what changed, so a feed never keeps state about what it has already said. If a
remote API only offers deltas, keep the accumulated set inside the feed.

**Acquisition is not reduction.** Each angle's `.dat` file is written once
*that angle* has been measured, so a run's segments arrive one by one, and a
run whose files have settled may still have angles to come. A later run that a
feed merely *announces* does not make an earlier run complete: that is when the
next acquisition started, and the earlier run's last segment may still be
reducing. Today only a later run with *reduced files* counts (`status.judge`,
`latest_reduced`).

**What the monitor will add.** The web monitor reports the run being measured
*now*. "The instrument is measuring run M" proves that every segment of an
earlier run with a subrun below M has been *acquired*. Once each of those has
been reduced and its files have settled, that run is complete, without waiting
for the next run's reduction. That is also what will let the last run of a
beamtime complete without a person: the monitor then reports that nothing is
being measured. A monitor feed should announce the current run with
`state="acquiring"` and leave the judgement to `status.judge`, which will need
that one rule added.

## `CatalogStore`: the organization

```python
class CatalogStore(Protocol):
    def describe(self) -> dict: ...
    def load(self) -> Catalog: ...
    def load_report(self) -> tuple[Catalog, tuple[Problem, ...]]: ...  # and what it recovered from
    def update(self, *, runs=(), samples=(), now=None) -> Catalog: ...
```

`update` takes **edits**, not a whole catalog. Each edit carries the revision of
the record it was based on:

- a concurrent edit to a *different* record is merged;
- an edit made against a *stale* record raises `RecordConflict`.

A facility service holding the same tables should keep those semantics.
Otherwise two people editing one experiment overwrite each other.

### Sharing it: a copy, not a store everyone writes

The catalog is kept in the project, and committed with it. It is shared the
way the project is, through git, and with everyone on the experiment and other
programs by `nrw tiled push`, which publishes a copy that `nrw tiled pull
--context` brings into a colleague's project (see
[Tiled](#tiled-pushed-and-pulled-by-a-person), below). No other kind of store
is planned: one that every member's project read and wrote as it went was
built and withdrawn twice, on 2026-09-28 and 2026-10-27.

Should one be built again, it implements `CatalogStore` above, and must:

- **Keep revisions.** Every record carries one, and an edit made against an
  older one is refused with `RecordConflict`, never applied over the newer.
- **Check what it is given.** Records written by other programs arrive
  unchecked. Before a value is used, it must meet the rules `experiment/model.py`
  applies to the page's own edits: sample ids that are safe as folder names,
  one-line values without `|` or line breaks, and prose without headings or
  comment markers. Every one of these is written into `sample.md`, which people
  and the unattended agent read as the sample's truth.
- **Answer within a deadline.** A page or command that waits for a service that
  stopped answering must fail after a timeout, and say which service.
- **Never fall back to the project's files.** An edit meant for everyone must not
  land where nobody else looks.

Sharing one set of parquet files between projects was built and withdrawn:
files on a network mount do not make a database. See the branch
`shared-catalog-attempt`.

The parquet store's schema follows data-assembler's lakehouse split: run rows
carry a nullable `sample_id`, and samples are their own table. Columns a store
does not know must survive a save. A newer schema version must be refused, not
read partially.

## Tiled: pushed and pulled, by a person

Tiled is reached only by `nrw tiled` commands a person runs, through the
official client: the installer's `NRW_EXTRAS=tiled` adds it
([install.md](install.md)). Nothing `nrw serve`, the poller or an agent runs
talks to Tiled, and the guard refuses every `nrw tiled` command, and Tiled's
own command line, to an agent.

| Command | What it does |
|---|---|
| `nrw tiled login`, `logout` | Tiled's device-code login through Globus (tiled.ornl.gov first asks which login: choose `globus`); the client keeps the login, and renews it, in its own token cache |
| `nrw tiled status` | whose the login is, whether the experiment is on Tiled, each record there, the experiment's context and each fit, against this project's, whether each fit there is here, and which reduced files there the data folder lacks |
| `nrw tiled push` | writes the project's catalog to the experiment's `context/`, and its starred and final fits to `analysis/`; `--only context` or `--only fits` writes one |
| `nrw tiled pull --context` | writes Tiled's runs, samples and experiment context into the catalog |
| `nrw tiled pull --data` | copies the experiment's reduced files into the data folder |
| `nrw tiled pull --fits` | rebuilds each fit here as it was recorded, with the reduced files it read, and writes its spec and script into its sample's `models/` |

The server is `TILED_URL` (tiled.ornl.gov by default), and `TILED_API_KEY`
is sent instead of a login when set: both read as nrw's other settings are,
from the shell, the project's `.env`, `~/.nrw`, then `~/.aure`.

**What a push writes.** In the experiment's node, `context/` holds `runs/`
and `samples/`, with one container per record: nrw's fields are its metadata,
and one spec names it (`context-run` or `context-sample`, version 1).
`context/` itself carries a header (`ipts`, `written_by`) and the spec
`context-experiment`, and, once it is written on the Experiment page, the
experiment's own context under `experiment`: its summary, hypotheses and
notes, written first and merged over what is there. Samples are written
before their runs. Other programs may read and write there too.

**Fits.** `analysis/` holds a container per sample, and one per fit in it:
`analysis/<sample>/<fit_id>/`, for each starred fit and each sample's final
one. Tiled 0.2.18 takes no file from a project member, so a fit's record is
its metadata (spec `analysis-fit`, version 2). It is the fit as analysed
data, in four parts, each file as text with its sha256:

- **The model**: bumps' own file of the fitted problem, `fit/<model>.json`.
  It holds the stack, every parameter at the fit's value with its bounds,
  the instrument's settings and the measured data. refl1d loads it as it is
  (`bumps.serialize.deserialize`), and computes R(q) and the SLD profile from
  it.
- **The intent**: `spec.yaml` and `model.py`, with `model.md` and `NOTES.md`.
- **The result**: each parameter with DREAM's statistics (std, median, 68%
  and 95% intervals), bumps' `.par` and `-err.json`, the chi-squared, points,
  free parameters and convergence, the assessment, and each segment's
  `-refl.dat` (Q, the data and the theory), `-slabs.dat` and SLD profile,
  thinned to at most 400 points.
- **The provenance**: the reduced files as the fit read them, with the
  headers that name their reduction, by their paths in `inputs.json`; the
  fit's index line, `inputs.json`, `manifest.json` and `env/versions.json`;
  the fitter, its settings and the seed; the curation, with each promotion's
  reason, who and when; and who pushed it, when. A reduced file changed since
  the fit read it is named, with why, and not carried.

A record is about 250 kB for a fit of three segments. The chains and the
figures stay here: `nrw fit run --from` makes them again. A fit unstarred,
discarded or deleted here keeps its record on Tiled, and the next push writes
its curation over Tiled's copy. Nothing is removed there.

**The last writer wins.** A push writes each record that differs over
Tiled's copy, keeping the fields another program keeps on it, and leaves the
rest alone, so a push of what Tiled holds writes nothing. A record only Tiled
holds is left, and `status` names it. A record's revision and the time of its
last edit are this project's own, and are left out when it is compared: a
record pulled from Tiled matches Tiled's copy. `--dry-run` says what would be
written, and Tiled keeps each version a write replaced.

**Nothing untagged.** Whatever a push makes takes its parent's access tags,
and a parent with none is refused: under Tiled's tag policy, a node made
without them is its maker's alone, invisible to everyone else on the
experiment.

**Pulling data.** `pull --data` copies each reduced file the data folder
lacks, and leaves alone every one it holds, or that appears while it runs:
neither a size nor a time tells a file reduced again from the same one, so
deleting a file is how it is copied again. Each lands under a hidden name of
its own first and is renamed whole; the folder watcher does the rest. Only
names the reduction writes are copied. It copies into the data folder
`nrw.toml` names, or `--into DIR`. `--run` narrows it to some runs, and
`nrw tiled status` says which files a pull would copy, before it is run. A
pulled run counts as complete once its files have settled, as any new file
does (five minutes by default).

**Pulling the catalog.** `pull --context` writes Tiled's runs, samples and
experiment context into this project's catalog: a record the catalog lacks is
added, and one that says something else in it is replaced by Tiled's, after
one question (`--yes` skips it). A record only the catalog holds is left.
Each edit is held to the catalog's own rules, and a record they refuse is
named and nothing is written; a save made on the Experiment page meanwhile is
not written over either: pull again.

**Pulling fits.** `pull --fits` rebuilds each fit as Tiled records it, in
`samples/<sample>/results/<fit_id>/`: its files, its curves at the thinned
profile, and its own copy of each reduced file it read, under its `data/` at
the path it read it from. Each of those files is also written where the fit
read it, in the sample's `data/`, when nothing is there: the fit's inputs are
then this project's, and its model in `models/` reads the data it was fitted
to. Its index line is then appended, as a fit line for the fit where the
record sits on Tiled, then its star and its labels, each promotion with its
reason, who and when. It is on the Fits page as it was.

Nothing is overwritten. A reduced file of the same name there already, such
as the facility's reduction applied first, is left, and the pull names it:
the fit then reads as stale, its own copy kept in its `data/`. A
`nrw experiment apply --write` afterwards leaves the pulled files alone too,
and lists each as a conflict to look at. A label another fit holds here, such
as the sample's `final`, stays that fit's, and the pull says so. A file in
the fit's directory there already is left, so a pull stopped partway is
finished by the next one, and a fit the index holds already is left whole. A
fit discarded or deleted is listed, and not pulled. So is a record that would
put a file outside its place: its sample, its id, its model and each of its
files must each be one name, and each reduced file's path must stay inside
the project, with no `..`. Anyone with write access to the experiment's node
can write a record there, so the pull holds each one to this before it writes
any of it.

Its `spec.yaml`, `model.py` and `model.md` also go into its sample's
`models/`, as `<model>.yaml`, `.py` and `.md`, to go on from. A model of that
name holding the same files is here already, and is left; one holding other
files is kept, and the fit's are written as `<model>-2`, as are a second
fit's of the same model, the final one taking the plain name. A push from the
project afterwards finds each pulled fit as Tiled holds it, and writes
nothing for it.

**Running a fit again.** `nrw fit run --from <fit_id>`, run in the project,
runs a recorded fit again from its own problem file, for a posterior of this
project's own. Its fitter, that fitter's settings and its seed are the fit's
own, but for those given on the command line; another fitter takes none of
them, and how many CPUs is this machine's to say. The new fit's inputs are
that file, with the role `problem`, and the measurements the fit recorded,
which the file holds: `nrw diff` of the two says the data are the same.
Bumps builds each class a problem file names and unpickles each function in
it, so a file that holds a pickled function, or names a class outside
refl1d's and bumps' own model classes, is refused before it loads; the fit's
script in `models/` still runs it, as code to read first. Its
settings name the fit as `regenerated_from`. It is this project's own fit,
with results of its own, to look at before going on, finalizing it or
exporting it to ISAAC. The pull prints this command for each fit with a
problem file, and the `nrw fit run` line of its script in `models/` for one
without.

**Starting from Tiled.** A colleague who joins an experiment starts a project
of their own:

1. `nrw init DIR --ipts IPTS-37740`, with the IPTS written in full:
   `nrw tiled` looks the experiment up as written.
2. A data folder of their own: `nrw experiment settings --location DIR
   --write`.
3. `nrw tiled login`.
4. `nrw tiled pull --context --data --fits`, which pulls in that order.
5. `nrw experiment apply --write`, once the pulled files have settled: it
   writes each sample's `sample.md` and copies its data. It leaves the
   reduced files the fits pull wrote, and lists as a conflict each one the
   facility's reduction holds otherwise.
6. Each fit on the Fits page, as it was recorded. `nrw fit run --from
   <fit_id>`, the line the pull prints for it, runs one again.

A push and a status run against a real Tiled 0.2.18, started with uvx apart
from nrw's environment, in `pytest -m tiled_server`. So do a fit's record of
a three-segment fit's size, read back as it was written, and a colleague's
pull of the catalog and the fit, whose problem file bumps loads at the
chi-squared recorded. A Globus login, the tags
and `pull --data` are tested against a fake of the client alone, and were
each checked once against tiled.ornl.gov (2026-10-27).

## tNR, later

Sliced time-resolved series are not handled yet. The unsliced tNR run arrives
in the same folder as an ordinary reduced file and is treated as one. Adding
series means:

- `RunKey.kind = "series"`, reserved already.
- The source listing slices (`r<run>_*.txt`) and their `*_eis_reduction.json`
  sidecar. The sidecar's interval labels define the slice names; validate them
  as strictly as segment names.
- Apply copying a series into `samples/<id>/data/tnr/<run>/` as one staged
  directory.
- Tying a series to its unsliced run, which is where `theta_for_run` reads the
  angle. A sample must never include both, because co-refining them counts the
  same neutrons twice.

## Not planned

**ONCat**, the facility's data catalog, is deliberately not used. The reduced
files and the run feed already carry what the page needs, and adding it would
mean a dependency and an account for no new information.
