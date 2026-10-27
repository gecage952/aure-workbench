"""Everything the UI knows about a project, with no Flask anywhere.

This module is deliberately free of any web framework. AuRE's equivalent grew
to 1861 lines with the data access tangled into the route handlers, at which
point neither half could be tested without the other. Keeping the two apart
means :class:`ProjectData` is testable with nothing but a directory, and
:mod:`nr_workbench.web.app` stays thin enough to read in one sitting.

Every public method returns JSON-serialisable plain data, so
:mod:`nr_workbench.web.api` can mirror this class one-to-one and an agent can
``curl`` exactly what the browser renders.

Nothing here raises on missing data. A live beamtime project is half-finished
by definition -- a sample with no fits yet, an assessment that has not been
run, a series whose reduction JSON has not arrived. Each of those is reported
as a fact about the project rather than an error, because a UI that refuses to
render until everything is present is a UI nobody can use during an experiment.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from nr_workbench.problems import Problem
from nr_workbench.project.layout import ProjectLayout
from nr_workbench.project.scan import scan_sample
from nr_workbench.provenance.index import FitIndex
from nr_workbench.provenance.summary import annotate, annotation_for
from nr_workbench.web.readers import (
    Curve,
    DataFormatError,
    Profile,
    read_amplitude_txt,
    read_profile_dat,
    read_reduced,
    read_refl_dat,
)

#: Cap on how many points a single curve sends to the browser. Reduced REF_L
#: files run to a few hundred points, so this only ever trips on something
#: unusual -- but one pathological file should degrade the plot, not the tab.
MAX_CURVE_POINTS = 20_000

#: Longest "why this run" a listing row will carry. A note can be paragraphs;
#: a table row that carries all of them stops being a table. The full text is
#: one click away on the fit's own page.
WHY_LIMIT = 110


def _trim(text: str) -> str:
    """Collapse prose to one line a table row can hold.

    The first paragraph only. `nrw note --why` appends, so a fit run twice
    against the same question has two answers under one heading, and a row that
    concatenates them reads as one sentence that nobody wrote. The rest is on
    the fit's own page, one click away.
    """
    first = text.strip().split("\n\n", 1)[0]
    line = " ".join(first.split())
    return line if len(line) <= WHY_LIMIT else line[: WHY_LIMIT - 1].rstrip() + "…"


#: Cap on heatmap cells (intervals x Q bins). A 21 x 250 series is 5k.
MAX_HEATMAP_CELLS = 400_000


class ProjectData:
    """Read-only access to a workbench project, shaped for plotting.

    Args:
        root: Project root, the directory holding ``nrw.toml``.
    """

    def __init__(self, root: Path) -> None:
        self.layout = ProjectLayout(root=Path(root).resolve())
        self.root = self.layout.root
        self.index = FitIndex(self.layout.index_file)

    # ------------------------------------------------------------------
    # Project level
    # ------------------------------------------------------------------

    def config(self) -> Any:
        """Return the parsed ``nrw.toml``, or ``None`` if it cannot be read.

        A project with an unreadable config is still worth rendering -- the
        samples and fits are on disk regardless -- so this reports absence
        rather than raising.
        """
        from nr_workbench.project.config import ProjectConfigError, load_config

        try:
            return load_config(self.root)
        except (ProjectConfigError, OSError):
            return None

    def overview(self) -> dict[str, Any]:
        """Summarise the project and every sample in it.

        Returns:
            The project block and one card per sample.
        """
        config = self.config()

        fits_by_sample: dict[str, list[dict[str, Any]]] = {}
        for row in self.index.fits():
            fits_by_sample.setdefault(str(row.get("sample")), []).append(row)

        cards = []
        for sample_id in self.layout.list_samples():
            rows = fits_by_sample.get(sample_id, [])
            scan = self._scan(sample_id)
            cards.append(
                {
                    "id": sample_id,
                    "title": self._sample_title(sample_id),
                    "n_steady": len(scan.steady) if scan else 0,
                    "n_series": len(scan.series) if scan else 0,
                    "n_fits": len(rows),
                    "latest_fit": rows[0].get("fit_id") if rows else None,
                    "latest_chisq": rows[0].get("chisq") if rows else None,
                    "assessments": self.assessment_labels(sample_id),
                }
            )

        return {
            "root": str(self.root),
            "name": config.name if config else self.root.name,
            "beamtime": config.beamtime if config else None,
            "ipts": config.ipts if config else None,
            "instrument": config.instrument if config else None,
            "facility": config.facility if config else None,
            "samples": cards,
            "n_fits": len(self.index.fits()),
        }

    # ------------------------------------------------------------------
    # Sample level
    # ------------------------------------------------------------------

    def sample(self, sample_id: str) -> dict[str, Any]:
        """Describe one sample: its prose, its data register, and its fits.

        Args:
            sample_id: The sample identifier.

        Returns:
            The sample detail block.

        Raises:
            FileNotFoundError: If the sample does not exist.
        """
        directory = self.layout.sample(sample_id)
        if not directory.is_dir():
            raise FileNotFoundError(f"No sample {sample_id!r} in {self.root}")

        scan = self._scan(sample_id)
        problems: list[Problem] = []

        steady = []
        if scan:
            for run in sorted(scan.steady):
                measurement = scan.steady[run]
                steady.append(
                    {
                        "run": run,
                        "combined": measurement.combined,
                        "segments": [
                            measurement.partials[k]
                            for k in sorted(measurement.partials)
                        ],
                    }
                )

        series = []
        if scan:
            for found in scan.series:
                entry = found.as_dict()
                entry["name"] = Path(found.directory).name
                entry["plottable"] = found.reduction_json is not None
                if found.reduction_json is None:
                    problems.append(
                        Problem(
                            scope=f"series:{entry['name']}",
                            message=(
                                "No *_eis_reduction.json in this directory, so "
                                "the interval order and timings are unknown. "
                                "The slices are listed but not plotted against "
                                "time -- the same limitation `nrw tnr assess` "
                                "has on this directory."
                            ),
                        )
                    )
                series.append(entry)

        return {
            "id": sample_id,
            "title": self._sample_title(sample_id),
            "markdown": self._read_text(directory / "sample.md"),
            "steady": steady,
            "series": series,
            "assessments": self.assessment_labels(sample_id),
            "models": sorted(p.name for p in (directory / "models").glob("*.yaml")),
            "scripts": sorted(p.name for p in (directory / "models").glob("*.py")),
            "fits": self.fits(sample_id),
            "documented_but_absent": scan.documented_but_absent if scan else [],
            "present_but_undocumented": (scan.present_but_undocumented if scan else []),
            "problems": [p.as_dict() for p in problems],
        }

    def steady_curves(self, sample_id: str) -> dict[str, Any]:
        """Read every steady-state measurement for a sample.

        Each angle segment stays a separate curve rather than being stitched:
        the overlap between segments is where a scaling error shows up, and
        stitching hides exactly that.

        Args:
            sample_id: The sample identifier.

        Returns:
            ``curves`` plus any ``problems`` encountered reading them.
        """
        scan = self._scan(sample_id)
        curves: list[dict[str, Any]] = []
        problems: list[Problem] = []
        if scan is None:
            return {"curves": curves, "problems": []}

        for run in sorted(scan.steady):
            measurement = scan.steady[run]
            files: list[tuple[str, str]] = []
            for segment in sorted(measurement.partials):
                files.append((f"{run}#{segment}", measurement.partials[segment]))
            if measurement.combined and not files:
                files.append((f"{run} combined", measurement.combined))

            for label, relative in files:
                try:
                    curve = read_reduced(
                        self.root / relative, label=label, root=self.root
                    )
                except (DataFormatError, OSError) as exc:
                    problems.append(Problem(scope=f"steady:{label}", message=str(exc)))
                    continue
                if curve.n_points > MAX_CURVE_POINTS:
                    problems.append(
                        Problem(
                            scope=f"steady:{label}",
                            message=(
                                f"{curve.n_points} points exceeds the "
                                f"{MAX_CURVE_POINTS} plotted; showing the first."
                            ),
                        )
                    )
                    curve = _truncate(curve, MAX_CURVE_POINTS)
                payload = curve.as_dict()
                payload["run"] = run
                curves.append(payload)

        return {"curves": curves, "problems": [p.as_dict() for p in problems]}

    def series_data(self, sample_id: str, name: str) -> dict[str, Any]:
        """Load a time-resolved series as a time-Q residual map.

        The residual is computed against the same coadded reference block that
        ``nrw tnr assess`` resolves, using the same functions. That is the point
        of reusing them rather than picking a reference here: the heatmap and
        the assessment numbers cannot drift apart.

        Args:
            sample_id: The sample identifier.
            name: The series directory name, e.g. ``218389``.

        Returns:
            Times, the Q grid, the fractional residual map, and the reference
            description.

        Raises:
            FileNotFoundError: If the series directory does not exist.
            ValueError: If the run cannot be loaded.
        """
        import numpy as np

        from nr_workbench.tnr.notify import collect
        from nr_workbench.tnr.reference import fractional_residuals
        from nr_workbench.tnr.run import load

        directory = self._series_dir(sample_id, name)
        with collect() as warnings:
            run = load(directory)

        y, sigma, _, use = fractional_residuals(
            run.R,
            run.dR,
            run.valid,
            run.R_ref,
            run.dR_ref,
            run.ref_valid,
            run.in_ref,
            loo=run.options.loo,
            min_ref_snr=run.options.min_ref_snr,
        )

        # Mask rather than zero-fill: an unmeasured cell and a cell that
        # genuinely did not change both read as 0.0 on a diverging colour
        # scale, and only one of them is a measurement.
        masked = np.where(use, y, np.nan)
        cells = masked.size
        if cells > MAX_HEATMAP_CELLS:
            raise ValueError(
                f"Series {name} would render {cells} cells, over the "
                f"{MAX_HEATMAP_CELLS} limit."
            )

        return {
            "name": name,
            "sample": sample_id,
            "times": [float(t) for t in run.times],
            "durations": [float(d) for d in run.durations],
            "types": list(run.types),
            "labels": list(run.labels),
            "q": [float(v) for v in run.q],
            "residual": [[_or_none(v) for v in row] for row in masked],
            "sigma": [
                [_or_none(v) for v in row] for row in np.where(use, sigma, np.nan)
            ],
            "reference": {
                "description": run.ref_desc,
                "indices": [int(i) for i in run.ref_idx],
                "median_rel_err": _or_none(run.median_ref_rel_err),
            },
            "late": {
                "description": run.late_desc,
                "indices": [int(i) for i in run.late_idx],
            },
            "n_intervals": run.n_intervals,
            "type_counts": run.type_counts,
            "problems": [
                Problem(scope=f"series:{name}", message=w).as_dict() for w in warnings
            ],
        }

    def series_curves(self, sample_id: str, name: str) -> dict[str, Any]:
        """Read a series as individual R(Q) curves, one per interval.

        Args:
            sample_id: The sample identifier.
            name: The series directory name.

        Returns:
            One curve per interval, in time order.

        Raises:
            FileNotFoundError: If the series directory does not exist.
        """
        from nr_workbench.tnr.notify import collect
        from nr_workbench.tnr.run import load

        directory = self._series_dir(sample_id, name)
        with collect():
            run = load(directory)

        curves = []
        for i in range(run.n_intervals):
            curves.append(
                {
                    "label": run.labels[i],
                    "time": float(run.times[i]),
                    "type": run.types[i],
                    "q": [float(v) for v in run.q],
                    "r": [_or_none(v) for v in run.R[i]],
                    "dr": [_or_none(v) for v in run.dR[i]],
                }
            )
        return {"name": name, "sample": sample_id, "curves": curves}

    # ------------------------------------------------------------------
    # Assessments
    # ------------------------------------------------------------------

    def assessment_labels(self, sample_id: str) -> list[str]:
        """List assessment directories holding a written assessment.

        ``nrw tnr assess`` prefixes its outputs with the run label, so the file
        is ``<label>_assessment.json`` rather than a bare name. Globbing keeps
        this working for an assessment directory assembled by hand, or one
        whose label differs from its directory name.
        """
        directory = self.layout.sample(sample_id) / "assessments"
        if not directory.is_dir():
            return []
        return sorted(
            entry.name
            for entry in directory.iterdir()
            if entry.is_dir() and _find_suffix(entry, "assessment.json")
        )

    def assessment(self, sample_id: str, label: str) -> dict[str, Any]:
        """Read one tNR assessment and its a(t) trajectory.

        Args:
            sample_id: The sample identifier.
            label: The assessment directory name.

        Returns:
            The assessment payload plus the parsed amplitude series where one
            was written.

        Raises:
            FileNotFoundError: If there is no such assessment.
        """
        directory = self.layout.sample(sample_id) / "assessments" / label
        payload_file = _find_suffix(directory, "assessment.json")
        if payload_file is None:
            raise FileNotFoundError(f"No assessment {label!r} for {sample_id}")

        payload = json.loads(payload_file.read_text(encoding="utf-8"))
        result: dict[str, Any] = {
            "sample": sample_id,
            "label": label,
            "assessment": payload,
            "figures": sorted(p.name for p in directory.glob("*.png")),
        }

        amplitude_file = _find_suffix(directory, "amplitude.txt")
        if amplitude_file is not None:
            try:
                result["amplitude_series"] = read_amplitude_txt(
                    amplitude_file
                ).as_dict()
            except (DataFormatError, OSError) as exc:
                result["problems"] = [
                    Problem(scope=f"amplitude:{label}", message=str(exc)).as_dict()
                ]
        return result

    # ------------------------------------------------------------------
    # Fits
    # ------------------------------------------------------------------

    def fits(self, sample_id: str | None = None) -> list[dict[str, Any]]:
        """List fit records, newest first.

        Args:
            sample_id: Restrict to one sample.

        Returns:
            One row per fit, with the promoted label attached where it has
            one, plus the ``description`` and ``change`` lines that tell a
            long list of hashes apart.
        """
        from nr_workbench.provenance.curation import NONE, curation_of

        # The labels a fit holds now -- a fit superseded as final is not final
        # -- and whether it is starred, set aside, or its files deleted.
        curated = curation_of(self.index.entries())

        rows = []
        for row in annotate(self.index.fits(sample=sample_id)):
            entry = dict(row)
            fit_id = str(row.get("fit_id"))
            state = curated.get(fit_id, NONE)
            entry["labels"] = list(state.labels)
            entry["curation"] = state.as_dict()
            # The index is append-only on purpose -- that a fit happened stays
            # true even after someone clears out disk space. But its artifacts
            # may be gone, and a row that links to a 404 is worse than one that
            # says so.
            directory = self._fit_dir_or_none(fit_id, row.get("sample"))
            entry["present"] = directory is not None
            entry["stack"] = self._stack_for(row, directory)
            entry["why"] = self._why_for(row, directory)
            rows.append(entry)
        return rows

    def _stack_for(self, row: dict[str, Any], directory: Path | None) -> str:
        """The structure this fit was of, as ``THF|Cu|Ti|Si``.

        The index first, because it is the only source that survives the result
        directory being deleted --- and because it costs no file read. Fits
        recorded before the stack was indexed fall back to the frozen bumps
        export, which is what makes this useful on a project that already has
        fits in it rather than only on the next one.
        """
        from nr_workbench.provenance import stack

        recorded = row.get("stack")
        if isinstance(recorded, str) and recorded:
            return recorded
        return stack.from_fit_dir(directory) if directory is not None else ""

    def _why_for(self, row: dict[str, Any], directory: Path | None) -> str:
        """Why this run was made, in the analyst's words.

        Three sources, most deliberate first: the *Why this run* section of the
        fit's own NOTES.md, then any other prose in that note, then the
        ``--note`` given at launch. What is *not* here is the generated
        fallback -- "amoeba, 12 steps, 2 free" answers "how was it configured",
        and every field in it is already a column of the table.
        """
        from nr_workbench.notes import SECTIONS, fit_note, read_section

        if directory is not None:
            sample = row.get("sample")
            own = fit_note(
                self.root,
                directory,
                str(row.get("fit_id")),
                str(sample) if sample else None,
            )
            # `blank` is the check that keeps the template's own prompts off
            # the page: "What were you testing?" rendered as an answer reads
            # like one, and taught readers the line was noise.
            if own is not None and not own.blank:
                written = read_section(own.text, SECTIONS["why"]) or own.summary
                if written:
                    return _trim(written)

        note = row.get("note")
        return _trim(str(note)) if isinstance(note, str) and note.strip() else ""

    def _fit_dir_or_none(self, fit_id: str, sample: Any) -> Path | None:
        """Locate a fit directory without raising when it is gone."""
        try:
            return self._fit_dir(fit_id, sample)
        except FileNotFoundError:
            return None

    def fit(self, fit_id: str) -> dict[str, Any]:
        """Load one fit in full: curves, profiles, parameters, provenance.

        Args:
            fit_id: The fit identifier, or a unique prefix of one.

        Returns:
            Everything the fit-detail view needs.

        Raises:
            FileNotFoundError: If no fit matches.
            ValueError: If a prefix matches more than one fit.
        """
        record = self._resolve_fit(fit_id)
        resolved = str(record["fit_id"])
        sample = record.get("sample")
        directory = self._fit_dir(resolved, sample)

        manifest = self._read_json(directory / "manifest.json")
        info = manifest.get("info", {}) if manifest else {}
        names = _model_names(info.get("models") or [])

        curves, profiles, problems = self._fit_arrays(directory / "fit", names)

        # The same two lines the listing shows, so arriving at a fit from a
        # bookmark tells you as much as arriving at it from the table.
        annotated = annotation_for(self.index.fits(), resolved) or {}

        return {
            "fit_id": resolved,
            "sample": sample,
            "model": record.get("model"),
            "description": annotated.get("description", ""),
            "change": annotated.get("change", ""),
            "compared_to": annotated.get("compared_to"),
            "record": record,
            "manifest": manifest,
            "labels": self._labels_for(resolved),
            "curves": curves,
            "profiles": profiles,
            "parameters": self._parameters(directory / "fit"),
            "spec": self._read_text(directory / "spec.yaml"),
            "script": self._read_text(directory / "model.py"),
            # An unfilled template is not a note. Rendering it put the
            # stub's HTML comments on every fit page, which taught readers
            # that the panel was noise.
            "notes": self._notes_for(resolved, sample, directory),
            "figures": sorted(
                p.name for p in (directory / "figures").glob("*") if p.is_file()
            ),
            "directory": directory.relative_to(self.root).as_posix(),
            "problems": [p.as_dict() for p in problems],
        }

    def _notes_for(self, fit_id: str, sample: Any, directory: Path) -> dict[str, Any]:
        """The prose about one fit: its own note, and the reports citing it.

        Args:
            fit_id: The resolved fit id.
            sample: The owning sample.
            directory: The result directory.

        Returns:
            ``own`` (markdown or ``None``) and ``reports`` (one entry each).
        """
        from nr_workbench.notes import fit_note, notes_about, sample_notes

        name = str(sample) if sample else None
        own = fit_note(self.root, directory, fit_id, name)
        reports = notes_about(sample_notes(self.root, name), fit_id) if name else []
        return {
            "own": None if own is None or own.blank else own.text,
            "reports": [{**note.as_dict(), "text": note.text} for note in reports],
        }

    def trajectory(self, fit_id: str) -> dict[str, Any]:
        """Layer parameters against time, for a fit that includes a series.

        Args:
            fit_id: The fit identifier, or a unique prefix.

        Returns:
            One trace per layer property, with a credible band where the fit
            produced a posterior. Empty ``traces`` when the fit has no series,
            which is the common case and not an error.

        Raises:
            FileNotFoundError: If no fit matches.
        """
        from nr_workbench.web import trajectory as traj

        record = self._resolve_fit(fit_id)
        resolved = str(record["fit_id"])
        directory = self._fit_dir(resolved, record.get("sample"))

        # `nrw fit run` precomputes this. The inputs are immutable once a fit
        # is written, so a cached answer can never be out of date -- and
        # rebuilding it costs a second or two of spec resolution, slab reading
        # and posterior arithmetic on every page load.
        cached = directory / "trajectory.json"
        if cached.is_file():
            stored = self._read_json(cached)
            if stored.get("traces"):
                return stored
        manifest = self._read_json(directory / "manifest.json")
        names = _model_names((manifest.get("info") or {}).get("models") or [])

        if not names:
            return {
                "fit_id": resolved,
                "traces": [],
                "series": None,
                "problems": [
                    Problem(
                        "trajectory",
                        "This fit recorded no model names, so its slices cannot "
                        "be matched to times. Fits run before that was recorded "
                        "need re-running to show a trajectory.",
                    ).as_dict()
                ],
            }

        spec_path, note = self._spec_for(directory)
        if spec_path is None:
            return {
                "fit_id": resolved,
                "traces": [],
                "series": None,
                "problems": [Problem("trajectory", note).as_dict()],
            }

        try:
            table = self._resolve_frozen_spec(spec_path)
        except Exception as exc:
            return {
                "fit_id": resolved,
                "traces": [],
                "series": None,
                "problems": [Problem("trajectory", str(exc)).as_dict()],
            }

        spec = table.spec
        if not spec.series:
            return {"fit_id": resolved, "traces": [], "series": None}

        series = spec.series[0].name
        measurements = table.measurements.get(series, [])
        times = [
            m.time if m.time is not None else float(i)
            for i, m in enumerate(measurements)
        ]
        constrained = {
            expression.key.split("@", 1)[0] for expression in table.expressions
        }

        traces = traj.build(
            directory / "fit",
            model_names=names,
            series=series,
            times=times,
            layers=[layer.name for layer in spec.stack],
            constrained=constrained,
        )
        self._attach_band(directory / "fit", table, series, traces)

        return {
            "fit_id": resolved,
            "sample": record.get("sample"),
            "series": series,
            "n_slices": len(measurements),
            "traces": [trace.as_dict() for trace in traces],
        }

    def _spec_for(self, directory: Path) -> tuple[Path | None, str]:
        """Find the spec that produced a fit, or explain why there is none.

        Fits made before the record froze `spec.yaml` can still be described,
        because the generated script records the digest of the spec it came
        from. If the spec still on disk hashes to that, it is provably the same
        file and using it is exact rather than a guess.

        If it has since been edited, it describes a *different* model, and
        showing that model's trajectory against this fit's numbers would be
        worse than showing nothing.

        Args:
            directory: The fit's result directory.

        Returns:
            ``(path, note)``. Path is ``None`` when no spec can be trusted.
        """
        frozen = directory / "spec.yaml"
        if frozen.is_file():
            return (frozen, "")

        script = directory / "model.py"
        if not script.is_file():
            return (None, "This fit has no frozen script.")

        source = script.read_text(encoding="utf-8", errors="replace")
        recorded = re.search(r"spec sha256: ([0-9a-f]{64})", source)
        declared = re.search(r"#\s+spec:\s+(\S+)", source)
        if not recorded or not declared:
            return (
                None,
                "This fit ran a hand-written script, so there is no spec "
                "describing which parameters vary with time.",
            )

        live = self.root / declared.group(1)
        if not live.is_file():
            return (
                None,
                f"The spec this fit came from ({declared.group(1)}) is no longer "
                "on disk, and it predates spec.yaml being frozen into the "
                "record. Re-run the fit to get a trajectory.",
            )

        # Ignores a deprecation banner, matching `nrw check` and the generated
        # header: a label about a spec's status is not a change to its model.
        from nr_workbench.spec.deprecation import identity_hash

        actual = identity_hash(live)
        if actual != recorded.group(1):
            return (
                None,
                f"{declared.group(1)} has been edited since this fit ran, so it "
                "describes a different model. This fit predates spec.yaml being "
                "frozen into the record, so there is no copy of the original. "
                "Re-run the fit to get a trajectory -- `nrw check` will be "
                "reporting it as stale anyway.",
            )

        return (live, "")

    def _resolve_frozen_spec(self, spec_path: Path):
        """Resolve the spec frozen inside a fit directory.

        Resolved against the *project*, not the result directory, because the
        data paths in a spec are project-relative and the frozen copy is a
        record rather than a working tree.
        """
        from nr_workbench.spec.models import load_spec
        from nr_workbench.spec.resolve import build_table, discover_measurements

        spec = load_spec(spec_path)
        return build_table(spec, discover_measurements(spec, self.root))

    def _attach_band(
        self, fit_dir: Path, table: Any, series: str, traces: list[Any]
    ) -> None:
        """Fill in credible bands from the posterior, when there is one."""
        from nr_workbench.web import trajectory as traj

        samples, columns = traj.load_posterior(fit_dir)
        if samples is None or not columns:
            return

        key_to_display = {p.key: p.display for p in table.free}
        sources = {
            expression.key: expression.source
            for expression in table.expressions
            if f"@{series}#" in expression.key
        }
        band = traj.band_for(sources, key_to_display, samples, columns)
        if not band:
            return

        for trace in traces:
            lo: list[float] = []
            mid: list[float] = []
            hi: list[float] = []
            for index in range(len(trace.values)):
                edges = band.get(f"{trace.path}@{series}#{index}")
                if edges is None:
                    lo, mid, hi = [], [], []
                    break
                lo.append(edges[0])
                mid.append(edges[1])
                hi.append(edges[2])
            if lo and hi:
                trace.lo, trace.median, trace.hi = lo, mid, hi

    def _fit_arrays(
        self, fit_dir: Path, names: dict[int, str]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Problem]]:
        """Read every exported curve and profile, in export order."""
        curves: list[dict[str, Any]] = []
        profiles: list[dict[str, Any]] = []
        problems: list[Problem] = []
        if not fit_dir.is_dir():
            return (
                curves,
                profiles,
                [Problem(scope="fit", message=f"No fit output at {fit_dir.name}/")],
            )

        for index, refl_path in _numbered(fit_dir, "-refl.dat"):
            label = names.get(index) or f"model {index}"
            try:
                curve = read_refl_dat(refl_path, label=label, root=self.root)
            except (DataFormatError, OSError) as exc:
                problems.append(Problem(scope=f"model:{label}", message=str(exc)))
                continue
            payload = curve.as_dict()
            payload["index"] = index
            payload["named"] = index in names
            curves.append(payload)

        from nr_workbench.web.trajectory import read_slabs, substrate_offset

        slabs = dict(_numbered(fit_dir, "-slabs.dat"))
        for index, profile_path in _numbered(fit_dir, "-profile.dat"):
            label = names.get(index) or f"model {index}"
            try:
                profile = read_profile_dat(profile_path, label=label, root=self.root)
            except (DataFormatError, OSError) as exc:
                problems.append(Problem(scope=f"profile:{label}", message=str(exc)))
                continue
            payload = profile.as_dict()
            payload["index"] = index
            payload["named"] = index in names

            # refl1d puts z = 0 at the top of the stack, so profiles whose
            # total thickness differs are drawn offset from one another.
            # Referencing the substrate surface -- the one interface that
            # cannot move -- makes the buried layers line up.
            offset = 0.0
            slab_path = slabs.get(index)
            if slab_path is not None:
                try:
                    offset = substrate_offset(read_slabs(slab_path))
                except (OSError, ValueError):
                    offset = 0.0
            payload["substrate_offset"] = offset
            profiles.append(payload)

        if curves and not names:
            problems.append(
                Problem(
                    scope="fit",
                    message=(
                        "This fit recorded no model names, so curves are "
                        "labelled by export position only. Fits run with a "
                        "generated script carry their spec slot names; a "
                        "hand-written script can set them with "
                        "Experiment(..., name='...')."
                    ),
                )
            )
        return curves, profiles, problems

    def _parameters(self, fit_dir: Path) -> list[dict[str, Any]]:
        """Read the fitted parameter values, with uncertainties where present.

        The ``.par`` file is always written; ``-err.json`` only appears after a
        fitter that estimates uncertainty, so a value with no error bar means
        an optimiser was run, not that the uncertainty is zero.
        """
        rows: list[dict[str, Any]] = []
        par_file = next(iter(sorted(fit_dir.glob("*.par"))), None)
        if par_file is None:
            return rows

        errors = self._uncertainties(fit_dir)
        for line in par_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            name, _, value = line.rpartition(" ")
            name = name.strip()
            try:
                parsed = float(value)
            except ValueError:
                continue
            row: dict[str, Any] = {"name": name, "value": parsed}
            if name in errors:
                row.update(errors[name])
            rows.append(row)
        return rows

    def _uncertainties(self, fit_dir: Path) -> dict[str, dict[str, Any]]:
        """Read ``-err.json`` into a name-keyed mapping, if it exists."""
        err_file = next(iter(sorted(fit_dir.glob("*-err.json"))), None)
        if err_file is None:
            return {}
        payload = self._read_json(err_file)
        if not isinstance(payload, dict):
            return {}

        found: dict[str, dict[str, Any]] = {}
        for key, value in payload.items():
            if not isinstance(value, dict):
                continue
            entry: dict[str, Any] = {}
            for source, target in (
                ("mean", "mean"),
                ("median", "median"),
                ("std", "std"),
                ("p68", "p68"),
                ("p95", "p95"),
            ):
                if source in value:
                    entry[target] = value[source]
            if entry:
                found[key] = entry
        return found

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_fit(self, fit_id: str) -> dict[str, Any]:
        """Find a fit record by exact id or unique prefix."""
        exact = self.index.find(fit_id)
        if exact is not None:
            return exact
        matches = self.index.resolve(fit_id)
        if not matches:
            raise FileNotFoundError(f"No fit matching {fit_id!r}")
        if len(matches) > 1:
            ids = ", ".join(str(m.get("fit_id")) for m in matches[:5])
            raise ValueError(f"{fit_id!r} matches {len(matches)} fits: {ids}")
        return matches[0]

    def _fit_dir(self, fit_id: str, sample: Any) -> Path:
        """Locate a fit directory, searching all samples if needed."""
        from nr_workbench.provenance.lookup import plain_name

        # The index is committed: a name in it is refused unless it is one.
        if not plain_name(fit_id) or (sample and not plain_name(sample)):
            raise FileNotFoundError(f"{fit_id!r} does not name a fit directory here.")
        if sample:
            candidate = self.layout.sample(str(sample)) / "results" / fit_id
            if candidate.is_dir():
                return candidate
        for sample_id in self.layout.list_samples():
            candidate = self.layout.sample(sample_id) / "results" / fit_id
            if candidate.is_dir():
                return candidate
        # A script run from outside samples/ records its fit at the root.
        candidate = self.root / "results" / fit_id
        if candidate.is_dir():
            return candidate
        raise FileNotFoundError(
            f"Fit {fit_id} is recorded in the index but its result directory "
            "is gone. The index is append-only, so the record of the run "
            "survives even when the artifacts are deleted."
        )

    def _labels_for(self, fit_id: str) -> list[str]:
        """Return the labels a fit holds now: not one it was superseded in."""
        from nr_workbench.provenance.curation import NONE, curation_of

        return list(curation_of(self.index.entries()).get(fit_id, NONE).labels)

    def _series_dir(self, sample_id: str, name: str) -> Path:
        """Resolve a series directory."""
        directory = self.layout.sample(sample_id) / "data" / "tnr" / name
        if not directory.is_dir():
            raise FileNotFoundError(f"No series {name!r} for {sample_id}")
        return directory

    def _scan(self, sample_id: str) -> Any:
        """Scan a sample's data directories, or ``None`` if it has none."""
        try:
            return scan_sample(self.root, sample_id)
        except FileNotFoundError:
            return None

    def _sample_title(self, sample_id: str) -> str:
        """Read the sample title from the first heading of ``sample.md``."""
        text = self._read_text(self.layout.sample(sample_id) / "sample.md")
        for line in (text or "").splitlines():
            if line.startswith("# "):
                return line[2:].strip() or sample_id
        return sample_id

    @staticmethod
    def _read_text(path: Path) -> str | None:
        """Read a text file, returning ``None`` when it is absent."""
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        """Read a JSON file, returning an empty mapping on any failure."""
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}


def _find_suffix(directory: Path, suffix: str) -> Path | None:
    """Find the one file in ``directory`` ending in ``suffix``.

    Assessment outputs are label-prefixed (``r218389_assessment.json``), so an
    exact name is not knowable from the directory alone.
    """
    if not directory.is_dir():
        return None
    exact = directory / suffix
    if exact.is_file():
        return exact
    matches = sorted(directory.glob(f"*_{suffix}"))
    return matches[0] if matches else None


def _model_names(models: list[Any]) -> dict[int, str]:
    """Build the export-position to name mapping recorded at fit time."""
    names: dict[int, str] = {}
    for entry in models:
        if not isinstance(entry, dict):
            continue
        index = entry.get("index")
        name = entry.get("name")
        if isinstance(index, int) and isinstance(name, str) and name:
            names[index] = name
    return names


def _numbered(directory: Path, suffix: str) -> list[tuple[int, Path]]:
    """Find ``<basename>-<n><suffix>`` files, sorted by ``n``.

    Sorting on the parsed integer matters: lexical order puts model 10 between
    models 1 and 2, which would silently pair the wrong curve with the wrong
    label.
    """
    found: list[tuple[int, Path]] = []
    for path in directory.glob(f"*{suffix}"):
        stem = path.name[: -len(suffix)]
        _, _, tail = stem.rpartition("-")
        try:
            found.append((int(tail), path))
        except ValueError:
            continue
    return sorted(found)


def _truncate(curve: Curve, limit: int) -> Curve:
    """Return the first ``limit`` points of a curve."""
    return Curve(
        label=curve.label,
        q=curve.q[:limit],
        r=curve.r[:limit],
        dr=curve.dr[:limit],
        dq=curve.dq[:limit],
        theory=curve.theory[:limit],
        theta=curve.theta,
        time=curve.time,
        source=curve.source,
    )


def _or_none(value: Any) -> float | None:
    """Return a JSON-safe float, or ``None`` for anything non-finite."""
    import math

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


__all__ = ["ProjectData", "Problem", "Curve", "Profile"]
