"""``nrw pack`` -- turn a fit into something a collaborator can run.

A result directory is complete but not portable. It records the *hashes* of the
data rather than the data, and its script reaches out to the project around it.
Send someone the directory and they get a perfect description of a fit they
cannot run.

A bundle closes that gap. It carries the frozen script, the actual measurement
files at the paths the script expects, the recorded settings, and enough of the
original outputs to check the answer against. The receiving end needs refl1d,
bumps and numpy --- no nr-workbench, no project, no network.

Three decisions shape the layout:

**The project structure is preserved rather than flattened.** The generated
script finds its root by walking up for ``nrw.toml`` and reads data as
``PROJECT_ROOT / "samples/<id>/data/..."``. Rebuilding that shape means the
script is copied byte-for-byte --- the same bytes whose sha256 is in the
manifest --- instead of rewritten. A bundle whose script had been edited to
suit the bundle would be a different script, and could not be checked against
the record it claims to reproduce.

**A fit whose inputs have drifted cannot be packed.** The point of the bundle
is that its data is the data that produced the result. If a file on disk no
longer matches its recorded hash, shipping it would quietly ship a different
experiment.

**It verifies itself.** ``verify.py`` recomputes chi-squared and compares it
with the recorded value, so the first thing the recipient learns is whether
their environment reproduces yours --- before they change anything.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

import click

from nr_workbench.project.layout import ProjectLayout, ProjectNotFoundError
from nr_workbench.provenance.hashing import MODEL_ROLES, sha256_file
from nr_workbench.provenance.index import FitIndex
from nr_workbench.provenance.lookup import FitNotFoundError, resolve_fit
from nr_workbench.provenance.record import FitDirectory
from nr_workbench.provenance.whence import Freshness, check_inputs

BUNDLE_SCHEMA = "nrw-bundle/1"

#: Packages the bundled script actually needs. The recorded environment lists
#: everything installed in the project's virtualenv -- flask, click, aure --
#: none of which a collaborator has to install to run a refl1d model.
RUNTIME_PACKAGES = ("refl1d", "bumps", "numpy", "scipy")

#: Chain files are excluded by default: a long DREAM run's posterior is the
#: largest thing in a result directory by a wide margin, and it is evidence
#: about the original fit rather than an input to reproducing it.
CHAIN_SUFFIXES = (".mc.gz", ".mcmc", ".chain.gz")


def run_pack(
    *,
    fit_id: str,
    out: str | None = None,
    archive: bool = True,
    with_chain: bool = False,
    force: bool = False,
) -> None:
    """Package one fit for someone who does not have nr-workbench.

    Args:
        fit_id: The fit to package, or a unique prefix of one.
        out: Where to write the bundle. Defaults to ``<fit_id>.zip`` (or a
            directory of that name) in the current directory.
        archive: Write a ``.zip``. When false, leave the directory in place.
        with_chain: Include the MCMC chain, which is usually the largest file.
        force: Pack even when a recorded input no longer matches on disk.

    Raises:
        click.ClickException: If there is no project, the fit cannot be
            resolved, or an input has drifted and ``force`` was not given.
    """
    try:
        layout = ProjectLayout.discover()
    except ProjectNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    index = FitIndex(layout.index_file)
    try:
        entry, directory = resolve_fit(layout, index, fit_id)
    except FitNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    resolved = str(entry["fit_id"])
    manifest = FitDirectory(directory).read_manifest()
    inputs = FitDirectory(directory).read_inputs()

    statuses, freshness = check_inputs(directory, layout.root)
    drifted = [s for s in statuses if s.changed]
    if drifted and not force:
        listing = "\n".join(f"    {s.state}  {s.path}" for s in drifted[:8])
        raise click.ClickException(
            f"{len(drifted)} recorded input(s) no longer match what {resolved} "
            f"consumed:\n{listing}\n"
            "A bundle asserts that its data produced its result, so packing "
            "these would ship a claim that is not true.\n"
            "  re-run the fit to record the current data, or\n"
            "  --force   pack anyway; the drift is recorded in MANIFEST.json"
        )

    destination = _destination(resolved, out, archive)
    if destination.exists():
        raise click.ClickException(
            f"{destination} already exists. Remove it or pass --out elsewhere."
        )

    staging = destination.with_suffix("") if archive else destination
    if staging.exists():
        raise click.ClickException(f"{staging} already exists (staging directory).")

    try:
        report = _build(
            staging=staging,
            layout=layout,
            fit_dir=directory,
            entry=entry,
            manifest=manifest,
            inputs=inputs,
            statuses=statuses,
            freshness=freshness,
            with_chain=with_chain,
        )
        if archive:
            _zip(staging, destination)
            shutil.rmtree(staging)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    _report(destination, report, drifted=bool(drifted), with_chain=with_chain)


def _destination(fit_id: str, out: str | None, archive: bool) -> Path:
    """Work out where the bundle goes."""
    if out is not None:
        path = Path(out)
        if archive and path.suffix != ".zip":
            path = path.with_suffix(".zip")
        return path.resolve()
    name = f"{fit_id}.zip" if archive else fit_id
    return (Path.cwd() / name).resolve()


def _build(
    *,
    staging: Path,
    layout: ProjectLayout,
    fit_dir: Path,
    entry: dict[str, Any],
    manifest: dict[str, Any],
    inputs: list[dict[str, Any]],
    statuses: list[Any],
    freshness: Freshness,
    with_chain: bool,
) -> dict[str, Any]:
    """Assemble the bundle directory and return what went into it."""
    staging.mkdir(parents=True)

    provenance = manifest.get("provenance", {})
    script_entry = next((e for e in inputs if e.get("role") == "script"), None)
    if script_entry is None and any(e.get("role") == "problem" for e in inputs):
        raise click.ClickException(
            "This fit ran another fit's problem file (`nrw fit run --from`), "
            "not a script, and a bundle reproduces a script's run: pack the "
            "fit it came from."
        )
    if script_entry is None:
        raise click.ClickException(
            "This fit recorded no script, so there is nothing to reproduce. "
            "Fits from before the script was recorded cannot be packed."
        )

    # 1. The script, byte-for-byte from the frozen copy, at the path it ran
    #    from -- which is what makes its own relative data paths resolve.
    script_rel = Path(str(script_entry["path"]))
    _copy(fit_dir / "model.py", staging / script_rel)

    # 2. Every measurement, at its recorded path.
    data_entries = [e for e in inputs if e.get("role") not in MODEL_ROLES]
    copied = []
    for item in data_entries:
        relative = Path(str(item["path"]))
        source = layout.root / relative
        if not source.is_file():
            raise click.ClickException(
                f"Recorded input is missing from disk: {relative}\n"
                "The bundle would be incomplete, so nothing was written."
            )
        _copy(source, staging / relative)
        copied.append(str(relative))

    # 3. The root marker. The script looks for nrw.toml before falling back to
    #    counting directories; shipping one makes the answer deterministic
    #    rather than dependent on where the recipient unzipped.
    if layout.config_file.is_file():
        _copy(layout.config_file, staging / "nrw.toml")
    else:
        (staging / "nrw.toml").write_text(
            "# Marks the bundle root so the model script can find its data.\n"
            "contract_version = 1\n",
            encoding="utf-8",
        )

    # 4. The human-readable companions, where they exist.
    for name, target in (
        ("spec.yaml", script_rel.with_suffix(".yaml")),
        ("model.md", script_rel.with_suffix(".md")),
    ):
        if (fit_dir / name).is_file():
            _copy(fit_dir / name, staging / target)

    # 5. What the original run produced, to check against.
    results = staging / "original-results"
    skipped_chain = _copy_fit_outputs(fit_dir / "fit", results / "fit", with_chain)
    for name in ("manifest.json", "inputs.json", "NOTES.md"):
        if (fit_dir / name).is_file():
            _copy(fit_dir / name, results / name)
    if (fit_dir / "env").is_dir():
        _copy_tree(fit_dir / "env", results / "env")
    figures = fit_dir / "figures"
    if figures.is_dir() and any(figures.iterdir()):
        _copy_tree(figures, results / "figures")

    # 6. The thinking. A bundle that carries the numbers and not the argument
    #    reproduces a result without transmitting what anyone concluded from
    #    it -- and the argument is the part a collaborator cannot rederive.
    notes = _copy_notes(staging, layout, fit_dir, entry)

    versions = _versions(fit_dir)
    report = {
        "fit_id": str(entry["fit_id"]),
        "script": str(script_rel),
        "data_files": copied,
        "chisq": (manifest.get("info") or {}).get("chisq"),
        "par_file": _par_file(results / "fit", staging),
        "skipped_chain": skipped_chain,
        "versions": versions,
        "freshness": str(freshness),
        "notes": notes,
    }

    (staging / "MANIFEST.json").write_text(
        json.dumps(
            _bundle_manifest(entry, manifest, inputs, statuses, report),
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    (staging / "requirements.txt").write_text(_requirements(versions), encoding="utf-8")
    (staging / "verify.py").write_text(_verify_script(report), encoding="utf-8")
    (staging / "README.md").write_text(
        _readme(entry, manifest, provenance, report), encoding="utf-8"
    )
    return report


def _copy(source: Path, target: Path) -> None:
    """Copy one file, creating parents, preserving mtime."""
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _copy_tree(source: Path, target: Path) -> None:
    """Copy a directory."""
    shutil.copytree(source, target, dirs_exist_ok=True)


def _copy_fit_outputs(source: Path, target: Path, with_chain: bool) -> list[str]:
    """Copy the fit outputs, reporting any chain files left behind."""
    if not source.is_dir():
        return []
    skipped = []
    for item in sorted(source.rglob("*")):
        if not item.is_file():
            continue
        if not with_chain and item.name.endswith(CHAIN_SUFFIXES):
            skipped.append(item.name)
            continue
        _copy(item, target / item.relative_to(source))
    return skipped


def _copy_notes(
    staging: Path, layout: ProjectLayout, fit_dir: Path, entry: dict[str, Any]
) -> dict[str, Any]:
    """Copy the prose about this fit into the bundle.

    Two sources, matching the two kinds of note: the fit's own ``NOTES.md``
    (already carried under ``original-results/``) and every report about its
    sample that names it. Reports that do not mention this fit are left out --
    a bundle is one fit, and shipping a sample's whole notebook would send a
    collaborator arguments about results they do not have.

    Args:
        staging: The bundle being assembled.
        layout: The project layout.
        fit_dir: The result directory.
        entry: The fit's index entry.

    Returns:
        What was copied: the fit note's state and the report paths.
    """
    from nr_workbench.notes import fit_note, notes_about, sample_notes

    fit_id = str(entry.get("fit_id", ""))
    sample = entry.get("sample")
    own = fit_note(layout.root, fit_dir, fit_id, str(sample) if sample else None)

    reports: list[str] = []
    if sample:
        for note in notes_about(sample_notes(layout.root, str(sample)), fit_id):
            source = layout.root / note.path
            if source.is_file():
                _copy(source, staging / "notes" / Path(note.path).name)
                reports.append(Path(note.path).name)

    return {
        "fit_note": bool(own and not own.blank),
        "reports": reports,
    }


def _par_file(fit_outputs: Path, staging: Path) -> str | None:
    """Locate the best-fit parameter file, relative to the bundle root.

    Without it a bundle can rebuild the model but not the answer: a model
    script constructs its problem at the *starting* values, so chi-squared
    straight after loading measures the initial guess.
    """
    candidates = sorted(fit_outputs.glob("*.par")) if fit_outputs.is_dir() else []
    if not candidates:
        return None
    return candidates[0].relative_to(staging).as_posix()


def _versions(fit_dir: Path) -> dict[str, Any]:
    """Read the recorded environment, or an empty mapping."""
    path = fit_dir / "env" / "versions.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _requirements(versions: dict[str, Any]) -> str:
    """Pin only what the bundled script imports."""
    packages = versions.get("packages") or {}
    lines = [
        "# What the model script needs, at the versions that produced the",
        "# result. `original-results/env/requirements.txt` holds the full",
        "# environment if you need to match it exactly.",
    ]
    for name in RUNTIME_PACKAGES:
        version = packages.get(name)
        lines.append(f"{name}=={version}" if version else name)
    return "\n".join(lines) + "\n"


def _bundle_manifest(
    entry: dict[str, Any],
    manifest: dict[str, Any],
    inputs: list[dict[str, Any]],
    statuses: list[Any],
    report: dict[str, Any],
) -> dict[str, Any]:
    """The machine-readable description of the bundle."""
    drift = {s.path: str(s.state) for s in statuses if s.changed}
    return {
        "schema": BUNDLE_SCHEMA,
        "fit_id": report["fit_id"],
        "sample": entry.get("sample"),
        "model": entry.get("model"),
        "script": report["script"],
        "expected": {
            "chisq": report["chisq"],
            "n_free": (manifest.get("info") or {}).get("n_free"),
            "n_points": (manifest.get("info") or {}).get("n_points"),
        },
        "settings": manifest.get("params") or {},
        "inputs": inputs,
        "environment": report["versions"],
        "provenance": manifest.get("provenance") or {},
        # Recorded rather than hidden: a forced pack is still a usable bundle,
        # but the recipient has to be able to see that it was forced.
        "input_drift_at_pack_time": drift,
        "omitted": {"chain_files": report["skipped_chain"]},
    }


def _verify_script(report: dict[str, Any]) -> str:
    """A self-check the recipient can run before changing anything.

    Setting the fitted parameters is the whole trick. The script builds the
    problem at its *starting* values -- that is what a model script is -- so
    recomputing chi-squared straight after loading it measures the initial
    guess, not the result. The recorded answer lives in the ``.par`` file.
    """
    chisq = report["chisq"]
    expected = "None" if chisq is None else repr(float(chisq))
    return f'''"""Check that this bundle reproduces the fit it came from.

Loads the model, applies the recorded best-fit parameters, recomputes
chi-squared and compares. Needs refl1d, bumps and numpy -- nothing else.

    python verify.py

A mismatch is information, not a failure of the bundle: it means your refl1d
or bumps differs from the one in requirements.txt in a way that changes the
answer, which is exactly what you want to find out before building on it.
"""

import json
import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / {report["script"]!r}
PAR_FILE = HERE / {report["par_file"]!r} if {report["par_file"]!r} else None
EXPECTED_CHISQ = {expected}

#: Chi-squared is a float computed through thousands of operations; bit
#: equality is not the standard. A part in 1e-6 is far tighter than any
#: difference that would change a conclusion, and far looser than noise.
TOLERANCE = 1e-6


def read_par(path):
    """Parse bumps' `.par`: one `<name> <value>` per line.

    Parameter names contain spaces ("run218386 probe intensity"), so the
    split is on the last field, not the first.
    """
    values = {{}}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, number = line.rpartition(" ")
        if name:
            values[name.strip()] = float(number)
    return values


def main() -> int:
    if not SCRIPT.is_file():
        print(f"Model script not found: {{SCRIPT}}", file=sys.stderr)
        return 2

    print(f"Loading {{SCRIPT.relative_to(HERE)}} ...")
    namespace = runpy.run_path(str(SCRIPT))
    problem = namespace.get("problem")
    if problem is None:
        print("The script defined no `problem`.", file=sys.stderr)
        return 2

    labels = list(problem.labels())
    print(f"  free parameters  {{len(labels)}}")

    if PAR_FILE is None or not PAR_FILE.is_file():
        print("\\nNo recorded parameter file, so only the model was checked.")
        print(f"  chisq at the starting values  {{problem.chisq():.6g}}")
        return 0

    recorded = read_par(PAR_FILE)

    # Compare the parameter *sets* before the numbers. If they disagree the
    # model itself has changed shape, and a chi-squared comparison would be
    # meaningless -- worse, it might accidentally agree.
    missing = [name for name in labels if name not in recorded]
    extra = [name for name in recorded if name not in labels]
    if missing or extra:
        print("\\nMISMATCH: the model does not have the parameters that were fitted.")
        for name in missing:
            print(f"  in the model, not in the record:  {{name}}")
        for name in extra:
            print(f"  in the record, not in the model:  {{name}}")
        return 1

    problem.setp([recorded[name] for name in labels])
    chisq = float(problem.chisq())
    print(f"  chisq            {{chisq:.6g}}")

    if EXPECTED_CHISQ is None:
        print("\\nNo chi-squared was recorded, so there is nothing to compare.")
        return 0

    print(f"  recorded         {{EXPECTED_CHISQ:.6g}}")
    relative = abs(chisq - EXPECTED_CHISQ) / max(abs(EXPECTED_CHISQ), 1e-30)
    if relative <= TOLERANCE:
        print("\\nREPRODUCED: this environment gives the recorded result.")
        return 0

    print(f"\\nMISMATCH: differs by {{relative:.3g}} relative.")
    versions = HERE / "original-results" / "env" / "versions.json"
    if versions.is_file():
        recorded_env = json.loads(versions.read_text())
        print("  produced with: " + ", ".join(
            f"{{k}} {{v}}" for k, v in sorted(recorded_env.get("packages", {{}}).items())
        ))
    print("  see requirements.txt")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _readme(
    entry: dict[str, Any],
    manifest: dict[str, Any],
    provenance: dict[str, Any],
    report: dict[str, Any],
) -> str:
    """The page the recipient reads first."""
    info = manifest.get("info") or {}
    params = manifest.get("params") or {}
    packages = (report["versions"].get("packages") or {}) if report["versions"] else {}
    versions = ", ".join(
        f"{name} {packages[name]}" for name in RUNTIME_PACKAGES if name in packages
    )
    chisq = info.get("chisq")
    note = provenance.get("note")
    method = params.get("method", "?")
    settings = ", ".join(
        f"{key} {params[key]}"
        for key in ("steps", "samples", "burn", "seed")
        if params.get(key) is not None
    )

    lines = [
        f"# {entry.get('model') or 'Reflectometry fit'} — {report['fit_id']}",
        "",
        "A complete, self-contained copy of one neutron reflectometry fit: the",
        "model, the data it was fitted to, and the result it produced. It runs",
        "under plain refl1d — you do not need nr-workbench or anything else",
        "from the originating project.",
        "",
    ]
    if note:
        lines += [f"> {note}", ""]

    lines += [
        "## Run it",
        "",
        "```bash",
        "pip install -r requirements.txt",
        "python verify.py",
        "```",
        "",
        "`verify.py` recomputes chi-squared at the recorded best-fit parameters",
        "and compares it with the value below. Run it before anything else: it",
        "tells you whether your environment reproduces the original result, and",
        "a mismatch means your refl1d or bumps differs in a way that matters.",
        "",
        "To refit from here:",
        "",
        "```bash",
        f"refl1d {report['script']} --fit={method}"
        + (f" --steps={params['steps']}" if params.get("steps") else "")
        + " --store=my-refit",
        "```",
        "",
        "## What it produced",
        "",
        "| | |",
        "|---|---|",
        f"| chi-squared | {chisq:.6g} |"
        if chisq is not None
        else "| chi-squared | — |",
        f"| free parameters | {info.get('n_free', '—')} |",
        f"| data points | {info.get('n_points', '—')} |",
        f"| fitted with | {method}{', ' + settings if settings else ''} |",
        f"| ran | {provenance.get('started_at', '—')} |",
        "",
        "## What was concluded",
        "",
        *_notes_section(report),
        "## What is in here",
        "",
        "```",
        f"{report['script']}",
        "    the model, byte-identical to the one that was fitted",
        "samples/*/data/",
        f"    the {len(report['data_files'])} measurement file(s) it reads",
        "original-results/",
        "    what the original run produced: fitted curves, SLD profiles,",
        "    parameter values and uncertainties, the full environment",
        *(
            ["notes/", "    reports about this fit, written during the analysis"]
            if report["notes"]["reports"]
            else []
        ),
        "MANIFEST.json",
        "    every input with its sha256, the settings, and the provenance",
        "```",
        "",
        "The script locates its data by walking up to `nrw.toml`, which is why",
        "the directory structure is preserved. Keep the tree together and it",
        "runs from anywhere; move the script on its own and it will not find",
        "the data.",
        "",
    ]

    if report["skipped_chain"]:
        lines += [
            "The MCMC chain was left out to keep the bundle small:",
            "",
            "```",
            *(f"{name}" for name in report["skipped_chain"]),
            "```",
            "",
            "Everything derived from it — the parameter uncertainties, the",
            "credible intervals — is in `original-results/fit/`. Ask for the",
            "chain itself if you want to resample the posterior.",
            "",
        ]

    lines += [
        "## Provenance",
        "",
        f"- fit id: `{report['fit_id']}`",
        f"- script sha256: `{(provenance.get('identity') or {}).get('script_sha256', '—')}`",
        f"- produced with: {versions or 'see original-results/env/versions.json'}",
        f"- python {report['versions'].get('python', '—')}",
    ]
    git = (report["versions"].get("git") or {}) if report["versions"] else {}
    if git.get("available"):
        state = " (dirty)" if git.get("dirty") else ""
        lines.append(
            f"- git: `{(git.get('commit') or '')[:12]}` on {git.get('branch')}{state}"
        )
    if report["freshness"] != str(Freshness.FRESH):
        lines += [
            "",
            "**This bundle was forced.** At least one input no longer matched",
            "its recorded hash when the bundle was made; see",
            "`input_drift_at_pack_time` in MANIFEST.json for which.",
        ]
    lines.append("")
    return "\n".join(lines)


def _notes_section(report: dict[str, Any]) -> list[str]:
    """README lines pointing at the prose, or saying plainly that there is none."""
    prose = report["notes"]
    if not prose["fit_note"] and not prose["reports"]:
        return [
            "Nothing was written down about this fit. The numbers below are",
            "reproducible, but the reasoning behind them was not recorded --",
            "ask the sender what they concluded.",
            "",
        ]
    lines = []
    if prose["fit_note"]:
        lines.append(
            "- `original-results/NOTES.md` — why this run was made and what it showed"
        )
    for name in prose["reports"]:
        lines.append(
            f"- `notes/{name}` — a report about this sample that cites this fit"
        )
    lines.append("")
    return lines


def _zip(source: Path, destination: Path) -> None:
    """Archive the staging directory, keeping its name as the top-level folder."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(source.rglob("*")):
            if item.is_file():
                archive.write(item, source.name / item.relative_to(source))


def _report(
    destination: Path, report: dict[str, Any], *, drifted: bool, with_chain: bool
) -> None:
    """Tell the user what was written."""
    size = (
        destination.stat().st_size
        if destination.is_file()
        else sum(p.stat().st_size for p in destination.rglob("*") if p.is_file())
    )
    click.echo(f"  bundle    {destination}")
    click.echo(f"  fit       {report['fit_id']}")
    click.echo(f"  script    {report['script']}")
    click.echo(f"  data      {len(report['data_files'])} file(s)")
    if report["chisq"] is not None:
        click.echo(f"  chisq     {report['chisq']:.6g}  (verify.py checks this)")
    click.echo(f"  size      {size / 1e6:.1f} MB")
    prose = report["notes"]
    if prose["fit_note"] or prose["reports"]:
        parts = []
        if prose["fit_note"]:
            parts.append("the fit's own note")
        if prose["reports"]:
            parts.append(f"{len(prose['reports'])} report(s)")
        click.echo(f"  notes     {' + '.join(parts)}")
    else:
        click.secho(
            "  notes     none -- the bundle carries no reasoning, only numbers",
            fg="yellow",
        )
        click.secho(
            f'            nrw note {report["fit_id"][:8]} -m "what this run showed"',
            dim=True,
        )
    if report["skipped_chain"] and not with_chain:
        click.echo(
            f"  omitted   {len(report['skipped_chain'])} chain file(s); "
            "pass --with-chain to include them"
        )
    if drifted:
        click.secho(
            "  ! forced: an input had drifted from its recorded hash", fg="yellow"
        )
    click.echo()
    click.echo("  The recipient needs refl1d, bumps and numpy. Nothing else.")


def digest(path: Path) -> str:
    """Hash a file, exposed for tests that check the bundle's copies."""
    return sha256_file(path)
