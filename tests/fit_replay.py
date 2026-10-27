"""Fits recorded once, and replayed, for tests that only need a fit's record.

Most tests that fit want what comes after a fit -- its record, its page, its
bundle, its export -- and not the fit itself. Running the optimizer for each
of them made the suite fit about a hundred times. Here, the optimizer's answer
for each distinct fit is recorded once, in ``tests/data/fits/``, and replayed:
nrw's own runner still writes every file of the record, from the problem left
at the answer, so a change in how nrw records a fit is seen by these tests as
it would be by a real one. Only the optimizer is not run.

A test marked ``fit`` runs the optimizer for real (``pytest -m fit``); those
are the tests of fitting itself. ``pytest --record-fits`` runs real fits for
the others too, and records their answers. A fit no recording matches -- its
model, data or settings changed -- fails, naming the command that records it,
rather than running the optimizer unasked.
"""

from __future__ import annotations

import hashlib
import importlib.abc
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: Where the answers are kept, one file per distinct fit.
RECORDINGS = Path(__file__).parent / "data" / "fits"

#: Fit settings that change how a fit runs, not what it finds.
_HOW_NOT_WHAT = frozenset({"verbose", "parallel"})


class NoRecordedFit(AssertionError):
    """A test would run the optimizer, and no recording answers for it."""


def fingerprint(problem: Any, method: str, options: dict[str, Any]) -> str:
    """What makes two fits the same fit: their problem, and how they were asked.

    The parameters, their starting values and bounds, the misfit at the
    starting point (which the data and the model decide), the fitter and its
    settings. Numbers are rounded well above any platform's rounding noise, so
    a recording made on one machine is found on another.
    """
    low, high = problem.bounds()
    described = {
        "method": method,
        "options": {k: options[k] for k in sorted(options) if k not in _HOW_NOT_WHAT},
        "labels": list(problem.labels()),
        "start": [f"{value:.12g}" for value in problem.getp()],
        "bounds": [[f"{a:.12g}", f"{b:.12g}"] for a, b in zip(low, high, strict=True)],
        "nllf": f"{float(problem.nllf()):.9g}",
    }
    text = json.dumps(described, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def recorder(real_fit: Callable[..., Any]) -> Callable[..., Any]:
    """bumps' ``fit``, run for real, its answer recorded for later replay."""

    def fit(problem: Any, method: str = "amoeba", **options: Any) -> Any:
        key = fingerprint(problem, method, options)
        result = real_fit(problem, method=method, **options)
        if getattr(getattr(result, "state", None), "show", None) is not None:
            raise NoRecordedFit(
                f"a {method} fit leaves a sampler's state, which is not replayed: "
                "mark this test `fit`, so it runs the optimizer for real"
            )
        RECORDINGS.mkdir(parents=True, exist_ok=True)
        (RECORDINGS / f"{key}.json").write_text(
            json.dumps(
                {
                    "method": method,
                    "labels": list(problem.labels()),
                    "x": [float(v) for v in result.x],
                    "fun": float(result.fun),
                    "dx": None
                    if result.get("dx") is None
                    else list(map(float, result.dx)),
                },
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )
        return result

    return fit


def replayer(test: str) -> Callable[..., Any]:
    """bumps' ``fit``, answered from a recording without running the optimizer."""

    def fit(problem: Any, method: str = "amoeba", **options: Any) -> Any:
        from scipy.optimize import OptimizeResult

        key = fingerprint(problem, method, options)
        path = RECORDINGS / f"{key}.json"
        if not path.is_file():
            raise NoRecordedFit(
                f"no recorded fit answers this {method} fit (fingerprint {key}): "
                "its model, data or settings differ from every recording in "
                f"tests/data/fits/. Record it with: pytest --record-fits {test}"
            )
        recorded = json.loads(path.read_text(encoding="utf-8"))
        problem.setp(recorded["x"])
        return OptimizeResult(
            x=recorded["x"],
            fun=recorded["fun"],
            dx=recorded["dx"],
            success=True,
            status=0,
            message="replayed from tests/data/fits",
            nit=0,
            state=None,
            method=method,
            options=options,
        )

    return fit


class _PatchOnImport(importlib.abc.MetaPathFinder):
    """Patch ``bumps.fitters`` the moment a test first imports it."""

    def __init__(self, patch: Callable[[Any], None]) -> None:
        self._patch = patch

    def find_spec(self, name: str, path: Any, target: Any = None) -> Any:
        if name != "bumps.fitters":
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(name, path, target)
            if spec is not None and spec.loader is not None:
                break
        else:
            return None
        exec_module = spec.loader.exec_module
        patch = self._patch

        def patched(module: Any) -> None:
            exec_module(module)
            patch(module)

        spec.loader.exec_module = patched  # type: ignore[method-assign]
        return spec


def install(monkeypatch: Any, *, record: bool, test: str) -> None:
    """Replay (or record) every bumps fit for the rest of one test.

    Lazily: bumps costs half a second to import, and most tests never fit.
    """

    def patch(module: Any) -> None:
        real = module.fit
        monkeypatch.setattr(module, "fit", recorder(real) if record else replayer(test))

    loaded = sys.modules.get("bumps.fitters")
    if loaded is not None:
        patch(loaded)
    else:
        monkeypatch.setattr(sys, "meta_path", [_PatchOnImport(patch), *sys.meta_path])
