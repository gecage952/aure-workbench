"""A sample's models, for the Experiment page: its specs, a new one, and fits.

A spec is written by ``nrw model new --from-notes``, and a fit runs ``nrw
model generate`` and ``nrw fit run`` -- the commands a person types, run as
jobs, in child processes in the project (:mod:`nr_workbench.web.jobs`), so the
page and the terminal cannot disagree about what a spec holds or how a fit is
recorded. The model code is
never imported into the server, which would load it, and whatever it imports,
into the process serving every page.

Which commands make up a fit is decided here, with the request they answer.

Nothing here imports Flask; :mod:`nr_workbench.web.experiment_api` maps the
errors to status codes.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

from nr_workbench.aure_setup import OUTPUT_DIR, SETUP_FILE, setup_dir
from nr_workbench.experiment.model import CatalogValidationError, validate_sample_id
from nr_workbench.fitters import FITTERS, refuse
from nr_workbench.fitting.settings import (
    BUMPS_DEFAULTS,
    FIT_LIMITS,
    METHOD_SETTINGS,
    FitDefaults,
    FitSettingsError,
    read_fit_defaults,
)
from nr_workbench.project.layout import ProjectLayout
from nr_workbench.project.samples import validate_model_name
from nr_workbench.web.experiment import RequestError, require_writable
from nr_workbench.web.jobs import CommandRefused, Job, JobNotFound, JobRunner

#: Where a sample's specs live, under ``samples/<id>/``, as ``nrw model new``
#: and ``nrw aure import`` write them.
MODELS_DIR = "models"

#: The fitter settings the Fit form offers; the rest come from nrw.toml.
FORM_SETTINGS = ("steps", "samples", "burn")

#: A fit's note, one line: it is stored in the record, and heads its notes.
MAX_FIT_NOTE = 500


class ModelRefused(CommandRefused):
    """The request cannot be carried out yet; the message says why."""


class ModelNotFound(LookupError):
    """There is no such spec."""


class ModelsData:
    """A project's models, as the Experiment page lists, writes and fits them.

    Args:
        root: Project root.
        writable: Whether writes are allowed at all.
        why_read_only: What a refused write says.
    """

    def __init__(
        self, root: Path, *, writable: bool = True, why_read_only: str = ""
    ) -> None:
        self.root = Path(root)
        self.layout = ProjectLayout(root=self.root)
        self.writable = writable
        self.why_read_only = why_read_only
        self.jobs = JobRunner(self.root, on_finished=self._find_recorded_fit)

    def models(self, sample_id: str) -> dict[str, Any]:
        """One sample's specs, and what a new one could be built from.

        Args:
            sample_id: The sample.

        Returns:
            Its specs, whether the sample exists and has data, the steady runs
            AuRE may be asked to fit (as ``nrw aure new`` counts them), and
            whether AuRE is installed.

        Raises:
            RequestError: If the sample id is not usable.
        """
        from nr_workbench.aure_adapter import is_available

        directory = self._sample_dir(sample_id)
        models = [
            {
                "name": spec.stem,
                "spec": spec.relative_to(self.root).as_posix(),
                "script": spec.with_suffix(".py").is_file(),
                # A proposal of AuRE's nobody edited: a new quick fit may
                # replace it, so the page offers one.
                "proposed": (proposed := _is_unedited_proposal(spec)),
                # The run AuRE fitted for it: Quick fit again fits that one.
                "run": _proposed_run(spec) if proposed else None,
                # nrw's stub -- air on a film on Si -- not the sample: a fit of
                # it means nothing, and the page says so before running one.
                "placeholder": _is_placeholder(spec),
            }
            for spec in sorted((directory / MODELS_DIR).glob("*.yaml"))
        ]
        found = self._measured(sample_id)
        defaults, problem = self._fit_defaults()
        return {
            "sample": sample_id,
            "exists": directory.is_dir(),
            "has_data": bool(found and (found.steady or found.series)),
            "runs": sorted(found.steady) if found else [],
            "models": models,
            "writable": self.writable,
            # Whether it is installed, found without importing it: AuRE brings
            # the whole language-model stack, seconds of it, into the server.
            "aure": is_available(),
            # AuRE reads sample.md, which the page's edits reach on Apply.
            "sample_md_pending": _sample_md_pending(self.root, sample_id),
            # What the Fit form starts from: the project's own nrw.toml.
            "fit": {
                "method": defaults.method,
                "settings": {m: defaults.settings_for(m) for m in FITTERS},
                # The rest of what each box starts at, and which boxes a
                # fitter takes: the same tables `nrw fit run` resolves with.
                "bumps": BUMPS_DEFAULTS,
                "takes": {
                    m: [key for key in METHOD_SETTINGS[m] if key in FORM_SETTINGS]
                    for m in FITTERS
                },
                "problem": problem,
            },
        }

    def create(self, sample_id: str, name: Any) -> dict[str, Any]:
        """Write a new spec from the sample's notes and data, as a job.

        ``nrw model new --from-notes``: the states come from the data on disk,
        and the stack from the notes -- the sample's description and each run's
        condition and notes -- by the configured language model. Without one,
        it is nrw's placeholder stack, and the job's output says so. A job, not
        a request: the model's answer can take a minute.

        Args:
            sample_id: The sample.
            name: The model's name: the spec is ``models/<name>.yaml``.

        Returns:
            ``{"job": ...}``, the job started.

        Raises:
            WritesDisabledError: The server was started read-only.
            RequestError: The sample id or the name is not usable.
            ModelRefused: The sample has no directory or no data yet, or the
                spec exists.
            JobBusy: A job is running already.
        """
        require_writable(self.writable, self.why_read_only)
        directory = self._sample_dir(sample_id)
        name = _model_name(name)
        if not directory.is_dir():
            raise ModelRefused(
                f"samples/{sample_id}/ does not exist yet. Apply creates it, "
                "with the data a spec is built from."
            )
        found = self._measured(sample_id)
        if not (found and (found.steady or found.series)):
            # Said now, as `nrw model new` would a moment later: the states are
            # the data's, and there is none to build them from.
            raise ModelRefused(
                f"No data found for {sample_id!r}: samples/{sample_id}/data/ "
                "holds none yet. Apply the runs assigned to this sample first."
            )
        spec = directory / MODELS_DIR / f"{name}.yaml"
        if spec.exists():
            # Checked here for the message; `nrw model new` refuses too, without
            # --force, so two requests racing for one name cannot overwrite.
            raise ModelRefused(
                f"{self._shown(spec)} already exists. Choose another name, or edit "
                "that spec."
            )
        # `--` ends the options: the sample id is never read as one.
        job = self.jobs.start(
            label=f"new model {name}, from the notes",
            sample=sample_id,
            model=name,
            steps=[["model", "new", "--from-notes", f"--name={name}", "--", sample_id]],
        )
        return {"job": job.as_dict()}

    def fit(self, sample_id: str, name: Any, request: dict[str, Any]) -> dict[str, Any]:
        """Start fitting a spec: ``nrw model generate``, then ``nrw fit run``.

        The script is generated from the spec every time, and generate refuses
        a script edited by hand -- so what runs is the spec's, never code
        written into ``models/`` some other way.

        Args:
            sample_id: The sample.
            name: The model, ``models/<name>.yaml``.
            request: ``method`` (amoeba, de or dream; the project's default
                from nrw.toml when absent), and optionally ``steps``, DREAM's
                ``samples`` and ``burn``, a one-line ``note``, and ``force``
                to run again what already ran. A setting not given is left to
                ``nrw fit run``, which takes it from nrw.toml, else bumps.

        Returns:
            ``{"job": ...}``, the job started.

        Raises:
            WritesDisabledError: The server was started read-only.
            RequestError: A setting is not usable.
            ModelRefused: The project's nrw.toml [fit] cannot be read.
            ModelNotFound: There is no such spec.
            JobBusy: A job is running already.
        """
        require_writable(self.writable, self.why_read_only)
        directory = self._sample_dir(sample_id)
        name = _model_name(name)
        spec = directory / MODELS_DIR / f"{name}.yaml"
        if not spec.is_file():
            raise ModelNotFound(f"{self._shown(spec)} does not exist.")
        defaults, problem = self._fit_defaults()
        if problem:
            # Said now, not by the job a moment later: `nrw fit run` reads it too.
            raise ModelRefused(f"nrw.toml cannot be used to fit: {problem}")
        method = request.get("method") or defaults.method
        if not isinstance(method, str):
            raise RequestError(f"method must be text, not {method!r}.")
        if method not in FITTERS:
            raise RequestError(refuse(method))
        # `--option=value`, one argument each: a value is never read as an option.
        options: list[str] = []
        for key in FORM_SETTINGS:
            value = request.get(key)
            if value is None or value == "":
                continue
            if key not in METHOD_SETTINGS[method]:
                raise RequestError(
                    f"{key} is not a {method} setting: {method} takes "
                    f"{', '.join(METHOD_SETTINGS[method])}."
                )
            low, high = FIT_LIMITS[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not low <= value <= high
            ):
                raise RequestError(
                    f"{key} must be a whole number from {low} to {high}."
                )
            options.append(f"--{key}={value}")
        note = _fit_note(request.get("note", ""))
        if note:
            options.append(f"--note={note}")
        if request.get("force") is True:
            options.append("--force")
        job = self.jobs.start(
            label=f"{method} fit of {name}",
            sample=sample_id,
            model=name,
            steps=_fit_steps(self._shown(spec), method, options),
        )
        return {"job": job.as_dict()}

    def quick_fit(self, sample_id: str, name: Any, run: Any = None) -> dict[str, Any]:
        """Start a quick fit with AuRE: a stack proposed from sample.md, then fitted.

        AuRE's own run is reconnaissance (see :mod:`nr_workbench.commands.
        aure_cmd`); the fit that counts is the one ``nrw fit run`` records. So
        the job goes on from AuRE's quick budget to import the spec it proposes,
        generate its script, and fit that from AuRE's values -- and that fit is
        the one in Fits. Its fitter is the project's, as for every fit: the
        ``method`` in ``nrw.toml``'s ``[fit]``, else DREAM.

        A model can always be quick-fitted again -- after the notes changed,
        say. Each run of AuRE gets a folder of its own, and a spec that is an
        unedited proposal of AuRE's is replaced by the new one. A spec someone
        edited never is: that is checked again as the spec is written, so an
        edit made while AuRE ran is kept too.

        Args:
            sample_id: The sample.
            name: The model to write, ``models/<name>.yaml``; AuRE's run is kept
                in ``aure/<name>/``.
            run: Which steady run AuRE fits; needed when the sample has several.

        Returns:
            ``{"job": ...}``, the job started.

        Raises:
            WritesDisabledError: The server was started read-only.
            RequestError: The name or the run is not usable.
            ModelRefused: The model's spec was edited since AuRE proposed it, or
                AuRE is not installed.
            JobBusy: A job is running already.
        """
        from nr_workbench.aure_adapter import is_available

        require_writable(self.writable, self.why_read_only)
        directory = self._sample_dir(sample_id)
        name = _model_name(name)
        if run is not None and (
            isinstance(run, bool) or not isinstance(run, int) or run <= 0
        ):
            raise RequestError(f"run must be a run number, not {run!r}.")
        spec = directory / MODELS_DIR / f"{name}.yaml"
        again = spec.exists()
        if again and not _is_unedited_proposal(spec):
            raise ModelRefused(
                f"{self._shown(spec)} has been edited since AuRE proposed it, or was "
                "not proposed by AuRE, so a quick fit would replace your version. "
                "Quick-fit it under another name, or fit it as it is with Fit…."
            )
        if not is_available():
            raise ModelRefused(
                "AuRE is not installed where nrw serve runs, so there is nothing "
                "to run a quick fit with. `nrw doctor` lists what is installed."
            )
        workdir = self._free_run_dir(sample_id, name)
        chosen = [f"--run={run}"] if run is not None else []
        job = self.jobs.start(
            label=f"quick fit of {name} with AuRE" + (" again" if again else ""),
            sample=sample_id,
            model=name,
            steps=[
                ["aure", "new", f"--name={workdir.name}", *chosen, "--", sample_id],
                # Needs a language-model endpoint; without one it says how to
                # set one, and the job stops here.
                ["aure", "run", self._shown(workdir / SETUP_FILE), "--budget=quick"],
                # No --run: import reads the run from the setup AuRE was given,
                # and checks it belongs to this sample.
                [
                    "aure",
                    "import",
                    self._shown(workdir / OUTPUT_DIR),
                    f"--sample={sample_id}",
                    f"--name={name}",
                    *(["--replace-unedited"] if again else []),
                ],
                # No --method: `nrw fit run` takes nrw.toml's [fit], else dream.
                *_fit_steps(
                    self._shown(spec),
                    None,
                    ["--note=From AuRE's quick fit, refined from its values."],
                ),
            ],
        )
        return {"job": job.as_dict()}

    def job_status(self, offset: int | None = None) -> dict[str, Any]:
        """The job running, or the last one, and what it printed.

        Args:
            offset: The byte of its output to read from; ``None`` for the last
                chunk, as a page opening onto the job wants.

        Returns:
            ``{"job", "log", "offset"}``; ``job`` is ``None`` when there has
            been none.
        """
        job = self.jobs.current()
        if job is None:
            return {"job": None, "log": "", "offset": 0}
        try:
            text, offset = self.jobs.log(job.id, offset)
        except JobNotFound:
            text, offset = "", 0
        return {"job": job.as_dict(), "log": text, "offset": offset}

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Stop the running job.

        Args:
            job_id: The job, as the page names it.

        Returns:
            ``{"job": ...}``.

        Raises:
            WritesDisabledError: The server was started read-only.
            JobNotFound: *job_id* is not the current job.
        """
        require_writable(self.writable, self.why_read_only)
        return {"job": self.jobs.cancel(job_id).as_dict()}

    def _find_recorded_fit(self, job: Job) -> dict[str, str | None]:
        """What a job's own ``nrw fit run`` recorded, from what it printed.

        Read from the job's output, not guessed from the index: the terminal
        fits the same models while a page job runs, and the newest fit of a
        model since the job started need not be the job's.

        Returns:
            ``fit_id``: the fit it recorded -- none when the fit step never ran,
            or was cancelled, which leaves an interrupted run the index does not
            have. ``same_as``: the fit it was refused as identical to, which the
            page links, offering to run again anyway.
        """
        from nr_workbench.commands.fit import IDENTICAL_RE, RUNNING_RE
        from nr_workbench.provenance.index import FitIndex

        found: dict[str, str | None] = {"fit_id": None, "same_as": None}
        if not job.steps or job.steps[-1][:2] != ["fit", "run"]:
            return found
        if job.step != len(job.steps):
            return found
        started = f"$ nrw {' '.join(job.steps[-1])}\n"
        _, _, fitted = self.jobs.output(job.id).rpartition(started)
        index = FitIndex(self.layout.index_file)
        running = list(RUNNING_RE.finditer(fitted))
        if running:
            fit_id = PurePosixPath(running[-1].group("directory")).name
            found["fit_id"] = fit_id if index.find(fit_id) else None
        identical = IDENTICAL_RE.search(fitted)
        if identical and index.find(identical.group("fit_id")):
            found["same_as"] = identical.group("fit_id")
        return found

    def _free_run_dir(self, sample_id: str, name: str) -> Path:
        """A folder for a new run of AuRE for *name*: ``aure/<name>/``, else
        ``<name>-2``, ``<name>-3`` -- an earlier run, its checkpoints and its
        record of what the language model was asked, is kept."""
        first = setup_dir(self.root, sample_id, name)
        if not first.exists():
            return first
        n = 2
        while (candidate := first.with_name(f"{name}-{n}")).exists():
            n += 1
        return candidate

    def _fit_defaults(self) -> tuple[FitDefaults, str | None]:
        """What nrw.toml says about fitting; and why not, when it cannot say."""
        from nr_workbench.project.config import ProjectConfigError, load_config

        try:
            return read_fit_defaults(load_config(self.root).raw), None
        except (ProjectConfigError, FitSettingsError) as exc:
            return FitDefaults(), str(exc)

    def _measured(self, sample_id: str) -> Any:
        """The sample's measurements as the commands see them: the register, or
        the disk. ``None`` when there is no such sample."""
        from nr_workbench.project.scan import load_register, scan_sample

        try:
            return load_register(self.root, sample_id) or scan_sample(
                self.root, sample_id
            )
        except FileNotFoundError:
            return None

    def _sample_dir(self, sample_id: str) -> Path:
        try:
            validate_sample_id(sample_id)
        except CatalogValidationError as exc:
            raise RequestError(str(exc)) from exc
        return self.layout.sample(sample_id)

    def _shown(self, path: Path) -> str:
        """*path* as the project names it, and as the commands take it."""
        return path.relative_to(self.root).as_posix()


def _fit_steps(spec: str, method: str | None, options: list[str]) -> list[list[str]]:
    """Generate a spec's script, then fit it: the tail of every fit job.

    Args:
        spec: The spec, relative to the project.
        method: The fitter, or ``None`` for the project's default.
        options: More ``--option=value`` arguments for ``nrw fit run``.

    Returns:
        The two steps.
    """
    script = PurePosixPath(spec).with_suffix(".py").as_posix()
    return [
        ["model", "generate", spec],
        # --verbose: the fitter's progress is what the page shows.
        [
            "fit",
            "run",
            script,
            *([f"--method={method}"] if method else []),
            "--verbose",
            *options,
        ],
    ]


def _model_name(name: Any) -> str:
    try:
        return validate_model_name(name)
    except ValueError as exc:
        raise RequestError(str(exc)) from exc


def _fit_note(value: Any) -> str:
    """A fit's note: one line of text, or nothing.

    Raises:
        RequestError: It is not text, is too long, or is not one line.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise RequestError(f"note must be text, not {value!r}.")
    text = value.strip()
    if len(text) > MAX_FIT_NOTE:
        raise RequestError(
            f"note is {len(text)} characters; the limit is {MAX_FIT_NOTE}."
        )
    if "\n" in text or "\r" in text:
        raise RequestError("note must be one line of text.")
    return text


def _is_unedited_proposal(spec: Path) -> bool:
    from nr_workbench.aure_import import is_unedited_proposal

    try:
        return is_unedited_proposal(spec.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return False


def _proposed_run(spec: Path) -> int | None:
    """The run an AuRE proposal was fitted to: its one state's."""
    import yaml

    try:
        document = yaml.safe_load(spec.read_text(encoding="utf-8"))
        run = document["states"][0]["run"]
    except (
        OSError,
        UnicodeDecodeError,
        yaml.YAMLError,
        KeyError,
        IndexError,
        TypeError,
    ):
        return None
    return run if isinstance(run, int) and not isinstance(run, bool) else None


def _sample_md_pending(root: Path, sample_id: str) -> bool:
    from nr_workbench.experiment.render import sample_md_pending

    return sample_md_pending(root, sample_id)


def _is_placeholder(spec: Path) -> bool:
    from nr_workbench.spec.authoring import is_placeholder

    try:
        return is_placeholder(spec.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return False
