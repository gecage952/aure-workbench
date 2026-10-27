# Plan: one registry for reduced-file formats

> **A design record, kept for its reasoning.** What the code does now is in
> the code; where it disagrees with this plan, it is right.

**Status:** **done, 2026-09-21** — differently from the design below, and the
difference is the interesting part. See "How it was actually resolved" at the
end. Raised 2026-09-16 while teaching the package to read REF_L's
`_autoreduction.dat`.

## The problem

"What a REF_L reduced file looks like" is currently hardcoded in **four
independent places**, and adding one format meant editing three of them:

| Where | What it hardcodes | Failure when it is wrong |
|---|---|---|
| `project/scan.py` | `COMBINED_RE`, `PARTIAL_RE`, `STEADY_SUFFIXES` | `nrw sample scan` says "no data found" |
| `instrument/header.py` | `META_PREFIX`, `_AUTORED_*`, the dialect dispatch | every field `None`, indistinguishable from a headerless file |
| `aure/instruments/ref_l.py` | its own `_PARTIAL_RE`, `_COMBINED_RE` | AuRE falls back to "combined curve, dQ as FWHM, no incident angle" |
| `nrw.toml` `[conventions]` | `partial_glob`, `dq_convention` | **nothing — it is never read** |

The fourth row is the worst of them. It is the only one that *looks* like the
place you would configure this, it is the first place anyone edits, and editing
it changes nothing at all. The scan still reports "no data found", so you
conclude the filename is not the problem and go looking somewhere else.

The third is out of our tree entirely: AuRE is pinned by SHA, so a fix there is
a dependency bump, and a local edit to `site-packages` would vanish on the next
reinstall while silently producing correct-looking fits in the meantime.

None of the four knows the others exist. When `_autoreduction.dat` arrived,
`scan.py` was fixed first, which made the files visible and let them flow
straight into a `nrw model new` that wrote `dq_is_fwhm: true` for a file whose
header says `sigma` — a 2.355x resolution error, under a generated comment
claiming the value had been read from the file. **Making one layer smarter made
the failure less visible, not more.**

## The shape of the fix

One registry of format handlers, each answering two questions:

```python
@dataclass(frozen=True)
class ReducedFormat:
    name: str                                   # "ref_l_partial", "ref_l_autoreduction"
    def matches(self, filename: str) -> Match | None: ...
    def read_header(self, path: Path) -> ReducedHeader: ...
```

`scan.py`, `header.py`, `nrw data check` and `nrw model new` then all ask the
registry rather than each carrying a copy. Adding a format becomes one new
handler plus its tests, and the places that consume it do not change.

Two properties matter more than the refactor itself:

1. **An unrecognised file is reported, never skipped.** The `.dat` extension
   was filtered out before any pattern ran, so the files did not even reach
   `result.unreadable`. A directory of three valid files looked empty. The
   registry must have exactly one "nothing matched" path, and it must be loud.

2. **`nrw.toml` stops lying.** Either the registry genuinely reads its patterns
   from there, or the block is removed and the command that reports formats
   (`nrw data check`, or a new `nrw data formats`) becomes the answer to "what
   can this understand?". A block that documents behaviour it does not control
   is worse than no block, because it is the first thing people edit.

## What it does not solve

AuRE keeps its own recogniser regardless; it is a separate package with its own
release cycle. The registry makes our side single-sourced, and the right
follow-up is to push the same two formats upstream into AuRE rather than to
teach `nrw aure new` to work around it. Worth checking whether `setup.yaml` can
carry an explicit `theta` and `dq_is_fwhm` per file, which would let
nr-workbench pass what it has already read instead of relying on AuRE
re-deriving it from the filename.

## Why not now

The immediate need was a correct first fit of sample1, which needed two
patterns and one header dialect. Doing the refactor at the same time would have
mixed "make this run readable" with "restructure how formats are declared", and
only the first was on the critical path. The tests added alongside the
`_autoreduction.dat` work (`tests/test_header.py`, `tests/test_register.py`)
pin the observable behaviour, so the refactor has something to move against.


## How it was actually resolved

Two things changed the shape of the fix between proposing it and doing it.

**AuRE already had the registry.** The third row of the table above is not a
place that hardcodes REF_L — it is `aure/instruments/`, a registered,
priority-ordered, entry-point-extensible seam with its own protocol and docs,
which had simply never been taught this dialect. So the work upstream was to
add a format *through* that seam, not to build one: AuRE now ships `REF_L_v1`
and `REF_L_v2`, and `aure formats` answers "what can this understand?".

**Which means the two questions split.** Enumeration — *find me the files for
run R segment S* — is something AuRE cannot answer, because it classifies a
path it is handed and does not search a directory. Classification — *what is
this file* — is something AuRE must answer, because AuRE is what builds the
fit: if nrw says angle segments and AuRE says one complete curve, AuRE's view
is the one the fit obeys, and a setup written on nrw's view is wrong however
confident that view was.

So `instrument/reduced.py` owns the filenames and delegates the verdict:

| | Where it lives now |
|---|---|
| `project/scan.py` patterns | `instrument/reduced.py` |
| `spec/resolve.py` globs | `instrument/reduced.segment_globs` / `find_segments` |
| `commands/model.py` globs | `instrument/reduced.find_segments` |
| `instrument/header.py` name pattern | `instrument/reduced.parse_segment_name` |
| "is this a partial?" | **AuRE**, via `instrument/reduced.role` |
| `nrw.toml` `[conventions]` | still descriptive, but no longer lying |

`reduced.disagreement()` is the piece the original plan did not anticipate.
The failure was never that one module had the wrong pattern — each was
internally consistent. It was that **nrw and AuRE reached different
conclusions about the same directory, and neither could see the other's**.
That is now a checkable condition rather than something discovered by fitting
a wrong thickness.

### The two properties, revisited

1. **An unrecognised file is reported, never skipped.** `STEADY_SUFFIXES`
   still gates the scan, but it is now one constant in one module, and
   anything that passes it and matches nothing reaches `result.unreadable`.
   Upstream, `aure formats` names an unrecognised file rather than quietly
   giving it default conventions.
2. **`nrw.toml` stops lying.** Not by becoming live — the patterns are
   compiled, not user preferences — but by saying, in the block itself, that
   the glob and dq keys are descriptive, which of them *are* read
   (`standard_thetas`, `tnr_theta`), and where the real patterns live. The old
   block asserted "the 4th column of every reduced file is FWHM", which was
   true when written and is now false for one of the two live reductions.

### Still open

`standard_thetas = [0.45, 1.2, 3.5]` is read by `nrw reconcile`, with a
tolerance of 0.02 deg. The measured middle angle is **1.251**, so every run
raises an `unusual-angle` finding against the nominal 1.2. That predates this
work — the v1 header carries the same measured value — and it is a judgement
about this beamline's settings rather than a format question, so it was left
alone rather than folded in here.

**Resolved (2026-09-28): the angles are gone.** The finding never fired: both
readers asked the config for an attribute it does not have, and got an empty
list. `standard_thetas` and `tnr_theta` are removed from `nrw.toml`, with the
check, and a spec reads every angle from its files.
