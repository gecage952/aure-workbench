"""Execute a refl1d script and capture its output.

The load-bearing detail is the bumps 1.0.x export workaround documented in
:func:`run_fit` -- getting that wrong silently discards every uncertainty
output, which is exactly the kind of failure this package exists to prevent.

refl1d and bumps are imported inside the functions that use them: importing
them at module scope would add seconds to ``nrw --help``.
"""

from __future__ import annotations

import runpy
import sys
import threading
import warnings
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nr_workbench.tnr.notify import notify

#: Settings that identify a fit run. Anything absent falls back to the fitter's
#: own default, which is recorded as ``None`` rather than guessed at.
FIT_SETTING_KEYS = (
    "method",
    "steps",
    "samples",
    "burn",
    "pop",
    "seed",
    "alpha",
    "parallel",
)


class FitError(Exception):
    """Raised when a script cannot be loaded or a fit cannot be run."""


# --------------------------------------------------------------------------
# Observing which files a script actually reads
# --------------------------------------------------------------------------
#
# A hand-written script does not declare its inputs, and guessing them from
# string literals fails on the pattern every real script here uses:
#
#     os.path.join(os.path.dirname(__file__), "..", "data", "steady", fname)
#
# So we observe instead of guess. An audit hook sees every genuine file open,
# however the path was assembled -- through numpy.loadtxt, refl1d's loaders, or
# a path built at runtime from a run number. That is exact rather than
# heuristic, and it cannot be fooled by clever path construction.

#: Collector for the currently-tracked execution, or None. Guarded by the lock
#: because an audit hook can fire from any thread.
_tracked: set[str] | None = None
_tracked_lock = threading.Lock()
_hook_installed = False


def _audit_hook(event: str, args: tuple[Any, ...]) -> None:
    """Record file opens while tracking is active.

    Must stay cheap and must never raise: this runs on every open in the
    process, including the interpreter's own.
    """
    if event != "open" or _tracked is None:
        return
    path = args[0] if args else None
    if not isinstance(path, str | bytes):
        return
    try:
        text = path.decode() if isinstance(path, bytes) else path
    except UnicodeDecodeError:
        return
    with _tracked_lock:
        if _tracked is not None:
            _tracked.add(text)


@contextmanager
def track_opened_files() -> Iterator[set[str]]:
    """Collect every file path opened inside the block.

    Audit hooks cannot be removed once added, so one hook is installed lazily
    and gated on a module-level collector that this manager sets and clears.

    Yields:
        The set of opened paths, populated as the block runs.
    """
    global _tracked, _hook_installed

    if not _hook_installed:
        sys.addaudithook(_audit_hook)
        _hook_installed = True

    collected: set[str] = set()
    with _tracked_lock:
        previous, _tracked = _tracked, collected
    try:
        yield collected
    finally:
        with _tracked_lock:
            _tracked = previous


@dataclass
class FitOutcome:
    """What a fit produced.

    Attributes:
        chisq: Reduced chi-squared, if obtainable.
        n_free: Number of free parameters.
        n_points: Number of data points.
        artifacts: Named output files relative to the fit directory.
        models: Export position to model name; see :func:`describe_models`.
        export_ok: Whether the bumps export completed.
        export_error: Why the export failed, if it did.
        converged: Whether the sampler reported convergence. ``None`` when the
            fitter does not report it at all -- an optimiser has no opinion,
            and that is different from converging.
    """

    chisq: float | None = None
    n_free: int | None = None
    n_points: int | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    models: list[dict[str, Any]] = field(default_factory=list)
    export_ok: bool = True
    export_error: str | None = None
    converged: bool | None = None


@dataclass
class LoadedProblem:
    """A problem loaded from a script, plus the files loading it read.

    Attributes:
        problem: The bumps ``FitProblem``.
        opened: Absolute paths the script opened while building it.
    """

    problem: Any
    opened: list[Path] = field(default_factory=list)


def load_problem(script: Path, *, root: Path | None = None) -> LoadedProblem:
    """Execute a script and return its ``problem`` plus the files it read.

    Args:
        script: Path to a refl1d script defining ``problem = FitProblem(...)``.
        root: Project root. When given, only files inside it are reported as
            inputs -- otherwise every stdlib and site-packages file the
            interpreter touched would be listed.

    Returns:
        The problem and the data files it consumed.

    Raises:
        FitError: If the script fails to execute or defines no ``problem``.
    """
    script = Path(script).resolve()
    if not script.is_file():
        raise FitError(f"Script not found: {script}")

    with track_opened_files() as opened:
        try:
            namespace = runpy.run_path(str(script), run_name="__nrw_fit_run__")
        except Exception as exc:
            raise FitError(f"Script {script} raised while loading: {exc}") from exc

    if "problem" not in namespace:
        raise FitError(
            f"Script {script} defines no module-level `problem`. "
            "A fit script must end with `problem = FitProblem(...)`."
        )

    return LoadedProblem(
        problem=namespace["problem"],
        opened=_filter_inputs(opened, script=script, root=root),
    )


def problem_path(fit_dir: Path) -> Path | None:
    """Bumps' file of a fit's problem, ``fit/<model>.json``: the model, its
    parameters at the fit's values, and its data. ``None`` when the fit left
    none."""
    for path in sorted((Path(fit_dir) / "fit").glob("*.json")):
        if not path.name.endswith(("-err.json", "-expt.json")):
            return path
    return None


#: The classes a problem file may name: those of refl1d's and bumps' own
#: models that fits use. Bumps builds each class a problem file names, with
#: the file's arguments, and unpickles each function in it, so a file naming
#: any other class, or holding a function, could run code as it loads.
PROBLEM_CLASSES = frozenset(
    {
        "bumps.fitproblem.FitProblem",
        "bumps.parameter.Calculation",
        "bumps.parameter.FreeVariables",
        "bumps.parameter.Parameter",
        "bumps.parameter.Uniform",
        "bumps.parameter.Variable",
        "bumps.util.NumpyArray",
        "refl1d.experiment.Experiment",
        "refl1d.probe.probe.NeutronProbe",
        "refl1d.probe.probe.QProbe",
        "refl1d.sample.layers.Slab",
        "refl1d.sample.layers.Stack",
        "refl1d.sample.material.SLD",
    }
)

#: What bumps calls a node that stands for an object defined once elsewhere
#: in the file, and a pickled function.
_REFERENCE, _FUNCTION = "Reference", "Callable"


def load_problem_file(path: Path) -> LoadedProblem:
    """Load a problem file as bumps' export wrote it (:func:`problem_path`).

    It reads no other file: the data are in it. It is refused before bumps
    builds anything from it when it holds a pickled function, or names a
    class outside :data:`PROBLEM_CLASSES`. The check reads the file as bumps
    does, once bumps' migrations have brought it to the current format.

    Raises:
        FitError: It is refused, or bumps cannot load it.
    """
    import json

    from bumps import plugin
    from bumps.serialize import deserialize, migrate

    try:
        _, document = migrate(json.loads(Path(path).read_text(encoding="utf-8")))
        document = plugin.migrate_serialized(document)
    except Exception as exc:  # noqa: BLE001 - whatever bumps raises, it did not load
        raise FitError(f"{path} could not be loaded: {exc}") from exc
    refused = _code_in(document)
    if refused:
        raise FitError(
            f"{path} holds {refused}, so nrw does not load it: bumps would run "
            "it as the file loads. A problem file of refl1d's and bumps' own "
            "models loads; this fit's script, in its sample's models/, runs it "
            "as code you can read."
        )
    try:
        problem = deserialize(document, migration=False)
    except Exception as exc:  # noqa: BLE001 - whatever bumps raises, it did not load
        raise FitError(f"{path} could not be loaded: {exc}") from exc
    return LoadedProblem(problem=problem, opened=[])


def _code_in(document: Any) -> str:
    """What in a problem file bumps would run as it loads: a pickled function,
    or a class outside :data:`PROBLEM_CLASSES`; ``""`` when nothing is."""
    pending = [document]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if "__class__" in node:
                kind = node["__class__"]
                if kind == _FUNCTION:
                    return "a pickled function"
                if kind != _REFERENCE and (
                    not isinstance(kind, str) or kind not in PROBLEM_CLASSES
                ):
                    return f"the class {kind!r}"
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return ""


def _filter_inputs(opened: set[str], *, script: Path, root: Path | None) -> list[Path]:
    """Reduce observed opens to the data files that count as fit inputs.

    Drops the script itself (recorded separately), anything outside the project,
    and the interpreter's own reads -- imported modules, caches, and the
    site-packages tree.
    """
    root_resolved = Path(root).resolve() if root else None
    interpreter_prefixes = tuple(Path(p).resolve() for p in sys.path if p)

    keep: list[Path] = []
    for raw in sorted(opened):
        try:
            path = Path(raw).resolve()
        except (OSError, ValueError):
            continue
        if not path.is_file() or path == script:
            continue
        if path.suffix in {".py", ".pyc", ".pyi", ".so", ".pth", ".dylib"}:
            continue
        if root_resolved is not None:
            try:
                path.relative_to(root_resolved)
            except ValueError:
                continue
        elif any(_is_within(path, prefix) for prefix in interpreter_prefixes):
            continue
        keep.append(path)
    return keep


def _is_within(path: Path, parent: Path) -> bool:
    """Return whether ``path`` sits under ``parent``."""
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def describe_problem(problem: Any) -> tuple[int | None, int | None]:
    """Report a problem's free-parameter and data-point counts.

    Both are best-effort: the bumps API for these has moved between versions,
    and a missing count is not worth failing a fit over.

    Args:
        problem: A bumps ``FitProblem``.

    Returns:
        ``(n_free, n_points)``, either of which may be ``None``.
    """
    n_free: int | None = None
    n_points: int | None = None

    with suppress(Exception):
        n_free = len(problem.getp())

    for attr in ("dof", "numpoints"):
        try:
            value = getattr(problem, attr)
            n_points = int(value() if callable(value) else value)
            break
        except Exception:
            continue

    return n_free, n_points


def describe_models(problem: Any) -> list[dict[str, Any]]:
    """Map each model's export position to its name and size.

    bumps writes its per-model exports as ``<basename>-<i+1>-refl.dat``,
    numbered by position in ``problem.models`` and nothing else -- the name is
    not used in the filename. Anything reading those files afterwards therefore
    has to know the ordering, and reconstructing it by re-parsing the script is
    guesswork that goes wrong quietly: a plot labelled ``ocv2`` showing
    ``ocv1``'s data looks perfectly reasonable.

    So the ordering is recorded here, once, from the object that was actually
    fitted. Generated scripts name their Experiments after the spec slot
    (``ocv1#0``); a hand-written script may not name them at all, in which case
    only the position is recorded and consumers can say so honestly rather than
    invent a label.

    Args:
        problem: A bumps ``FitProblem``.

    Returns:
        One entry per model, in export order, each with its 1-based ``index``
        (matching the export filename), ``name`` if the script set one, and
        ``n_points`` where obtainable. Empty if the problem exposes no models.
    """
    try:
        models = list(problem.models)
    except Exception:
        return []

    described: list[dict[str, Any]] = []
    for position, model in enumerate(models, start=1):
        entry: dict[str, Any] = {"index": position}
        name = getattr(model, "name", None)
        if name:
            entry["name"] = str(name)
        with suppress(Exception):
            entry["n_points"] = int(model.numpoints())
        described.append(entry)
    return described


def run_fit(
    problem: Any,
    output_dir: Path,
    *,
    method: str = "amoeba",
    steps: int | None = None,
    samples: int | None = None,
    burn: int | None = None,
    pop: int | None = None,
    seed: int | None = None,
    alpha: float | None = None,
    parallel: int = 0,
    plots: bool = False,
    quiet: bool = False,
) -> FitOutcome:
    """Run a fit and write bumps' full export into ``output_dir``.

    Takes an already-loaded problem rather than a script path, so the caller
    can observe the script's inputs (see :func:`load_problem`) without
    executing it twice.

    Args:
        problem: A bumps ``FitProblem``.
        output_dir: Directory to write the bumps export into.
        method: Which fitter: ``amoeba`` to explore locally, ``de`` to explore
            globally when the starting point is what amoeba is failing on,
            ``dream`` to quote. Those are the only three; see
            :mod:`nr_workbench.fitters`.
        steps: Maximum optimizer steps.
        samples: DREAM sample count.
        burn: DREAM burn-in.
        pop: Population size.
        seed: Random seed, for a reproducible run.
        alpha: Bumps convergence parameter.
        plots: Let bumps render its PNGs. Off by default; see :func:`_export`.
        parallel: CPUs to use. ``0`` means all of them, ``1`` forces serial.
            dream and de both evaluate a whole population each generation
            and scale well; amoeba is sequential and gains nothing.
        quiet: Suppress the fitter's own progress output.

    Returns:
        What the fit produced.

    Raises:
        FitError: If the fit itself fails, or the fitter is not on the menu.
    """
    from bumps.fitters import fit as bumps_fit

    from nr_workbench.fitters import FITTERS, refuse

    # Last gate before bumps. The CLI and the spec both check, but this function
    # is also the library entry point, and a fitter that reaches bumps directly
    # would land in the provenance record as a legitimate result.
    if method not in FITTERS:
        raise FitError(refuse(method))

    # bumps names its export files after ``problem.name``; unset, that becomes
    # the literal string "None" in every filename.
    if not getattr(problem, "name", None):
        problem.name = "problem"

    kwargs: dict[str, Any] = {
        "method": method,
        "verbose": 0 if quiet else 1,
        "parallel": parallel,
    }
    for key, value in (
        ("steps", steps),
        ("samples", samples),
        ("burn", burn),
        ("pop", pop),
        ("seed", seed),
        ("alpha", alpha),
    ):
        if value is not None:
            kwargs[key] = value

    # DREAM reports non-convergence by warning and carrying on, so the only
    # record of it is a line on stderr that nothing reads. A fit that did not
    # converge can have the best chi-squared of the set -- on the real Cu/THF
    # corpus it did -- so losing this turns the most important caveat about a
    # result into the one fact nobody has.
    converged: bool | None = None
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = bumps_fit(problem, **kwargs)
        converged = _read_convergence(caught, method)
    except Exception as exc:
        if parallel == 1 or not _is_multiprocessing_failure(exc):
            raise FitError(f"Fit failed: {exc}") from exc
        # Losing an hour of fitting to a multiprocessing problem is far worse
        # than running it slowly. bumps starts a Manager and spawns workers
        # that re-import __main__, which some environments cannot do.
        notify(
            f"Parallel fitting failed ({type(exc).__name__}: {exc}); "
            "falling back to a single CPU."
        )
        kwargs["parallel"] = 1
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                result = bumps_fit(problem, **kwargs)
            converged = _read_convergence(caught, method)
        except Exception as serial_exc:
            raise FitError(f"Fit failed: {serial_exc}") from serial_exc

    outcome = FitOutcome(converged=converged)
    with suppress(Exception):
        outcome.chisq = float(problem.chisq())
    outcome.n_free, outcome.n_points = describe_problem(problem)
    outcome.models = describe_models(problem)

    _export(problem, result, Path(output_dir), outcome, plots=plots)
    return outcome


#: What bumps says when a sampler has not converged. Matched loosely because
#: it is a human-facing warning, not an API, and a wording change should read
#: as "unknown" rather than silently as "converged".
_NOT_CONVERGED = "did not converge"

#: Fitters that test convergence and warn when it fails. An optimiser has no
#: opinion, and "no opinion" must not be recorded as "converged".
_CONVERGENCE_TESTING = frozenset({"dream"})


def _read_convergence(caught: list[Any], method: str) -> bool | None:
    """Decide convergence from the warnings a fit emitted.

    Args:
        caught: Warnings recorded during the fit.
        method: The fitter that ran.

    Returns:
        ``False`` if the sampler said it did not converge, ``True`` if a
        convergence-testing fitter ran and said nothing, and ``None`` when the
        fitter does not test convergence at all --- which is not the same as
        converging, and must not be reported as if it were.
    """
    for entry in caught:
        if _NOT_CONVERGED in str(entry.message).lower():
            return False
    return True if method.lower() in _CONVERGENCE_TESTING else None


def _stats_without_plots(
    state: Any, portion: float | None = None, figfile: str | None = None
) -> None:
    """Write DREAM's parameter statistics without rendering its figures.

    ``bumps.dream.views.plot_all`` computes the variable statistics and writes
    ``-err.json`` *before* importing matplotlib, then draws five figures. Only
    the first half is wanted: ``-err.json`` is the parameter uncertainty table
    that the fit page and the trajectory band both read, while the figures are
    the part that is slow and that fails on a many-model problem.
    """
    from bumps.dream.stats import save_vars, var_stats

    draw = state.draw(portion=portion)
    stats = var_stats(draw)
    if figfile is not None:
        save_vars(stats, str(figfile) + "-err.json")


@contextmanager
def _plots_disabled(problem: Any, *, enabled: bool) -> Iterator[None]:
    """Stop bumps rendering PNGs during an export.

    Patched rather than configured because ``export_fit`` takes no flag for it.
    Both hooks are restored on the way out, including on failure.
    """
    if enabled:
        yield
        return

    from bumps import errplot
    from bumps.dream.state import MCMCDraw

    def skip(*_args: Any, **_kwargs: Any) -> None:
        """Stand in for a plotting call."""
        return None

    def skip_errors(*_args: Any, **_kwargs: Any) -> None:
        """Stand in for calc_errors.

        Returning None short-circuits the ``if res is not None`` guard around
        ``show_errors``, so the expensive per-sample profile recomputation is
        skipped too rather than being done and thrown away.
        """
        return None

    original_show = errplot.show_errors
    original_calc = errplot.calc_errors
    original_state_show = MCMCDraw.show
    try:
        problem.plot = skip
        errplot.show_errors = skip
        errplot.calc_errors = skip_errors
        # `fit_state.show()` runs before `fit_state.save()` too, so the same
        # failure mode applies one level down: the DREAM correlation, trace and
        # variable plots can take the chain with them. It cannot simply be
        # skipped, though -- `plot_all` writes `-err.json`, the parameter
        # statistics table, before it touches matplotlib. So it is replaced by
        # exactly that half.
        MCMCDraw.show = _stats_without_plots
        yield
    finally:
        errplot.show_errors = original_show
        errplot.calc_errors = original_calc
        MCMCDraw.show = original_state_show
        with suppress(AttributeError):
            del problem.plot


#: Exceptions that mean "the worker pool could not start", rather than "the
#: fit itself failed". Kept broad on purpose: the failure modes vary by
#: platform and start method, and the fallback is always safe.
_MP_FAILURE_MARKERS = (
    "multiprocessing",
    "spawn",
    "EOFError",
    "BrokenPipe",
    "Manager",
    "pickle",
    "daemonic",
)


def _is_multiprocessing_failure(exc: BaseException) -> bool:
    """Whether a failure looks like the worker pool rather than the model."""
    text = f"{type(exc).__name__}: {exc}"
    trace = ""
    with suppress(Exception):
        import traceback

        trace = "".join(traceback.format_exception(exc))
    haystack = (text + trace).lower()
    return any(marker.lower() in haystack for marker in _MP_FAILURE_MARKERS)


def _export(
    problem: Any,
    result: Any,
    output_dir: Path,
    outcome: FitOutcome,
    *,
    plots: bool = False,
) -> None:
    """Write bumps' export files, recording failure rather than raising.

    **Plotting is off by default, and that is a data-safety decision.**
    ``export_fit`` calls ``problem.plot()`` *before* ``fit_state.save()``, so a
    failure while rendering takes the chain, ``-err.json`` and every
    uncertainty output down with it -- the whole point of having run DREAM for
    an hour. A 21-model co-refinement renders 21 model PNGs plus the
    correlation and trace plots, which is where it falls over.

    The images are also the least useful thing in the directory: `nrw serve`
    draws the same curves from the data files, interactively and against the
    right axes. So they are skipped, `problem.plot` and ``errplot.show_errors``
    replaced with no-ops for the duration, and everything else is written
    exactly as before. Pass ``plots=True`` to restore them.

    **Do not pass ``export=`` to ``bumps.fitters.fit``.** In bumps 1.0.x that
    path calls ``export_fit(export, problem, result.state, ...)``, forwarding a
    bare ``MCMCDraw`` as the ``fit`` argument. ``export_fit`` then does
    ``getattr(fit, "fit_state", getattr(fit, "state", None))``, which an
    ``MCMCDraw`` does not satisfy, so it resolves to ``None`` and the whole
    uncertainty block is **silently skipped**: no ``*-err.json`` parameter
    uncertainties, no ``*-chain.mc.gz``, no DREAM diagnostics. Calling
    ``export_fit`` ourselves with the ``OptimizeResult`` -- the documented
    usage -- lets it recover ``result.state`` and write everything.

    A failed export is recorded and does not fail the fit: the parameters are
    already in hand, and losing them because a plot could not be written would
    be worse than an incomplete record that says so.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        from bumps.webview.server.api import export_fit

        with _plots_disabled(problem, enabled=plots):
            export_fit(str(output_dir), problem, result, basename=problem.name)
    except Exception as exc:
        outcome.export_ok = False
        outcome.export_error = f"{type(exc).__name__}: {exc}"

    outcome.artifacts = collect_artifacts(output_dir)


#: Suffix to artifact name. Order matters: the first match wins, so the more
#: specific patterns come first.
_ARTIFACT_PATTERNS: tuple[tuple[str, str], ...] = (
    ("-err.json", "uncertainties"),
    (".par", "parameters"),
    ("-chain.mc.gz", "chain"),
    ("-refl.dat", "reflectivity"),
    ("-profile.dat", "profile"),
    ("-expt.json", "experiment"),
    ("problem.json", "problem_json"),
)


def collect_artifacts(output_dir: Path) -> dict[str, str]:
    """Name the interesting files bumps wrote.

    Args:
        output_dir: The directory bumps exported into.

    Returns:
        Mapping of artifact name to path relative to ``output_dir``'s parent,
        so the paths read naturally inside a fit record.
    """
    artifacts: dict[str, str] = {}
    if not output_dir.is_dir():
        return artifacts

    parent = output_dir.parent
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        name = path.name
        for suffix, label in _ARTIFACT_PATTERNS:
            if name.endswith(suffix) and label not in artifacts:
                artifacts[label] = path.relative_to(parent).as_posix()
                break
    return artifacts
