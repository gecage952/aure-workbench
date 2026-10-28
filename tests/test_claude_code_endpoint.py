"""Offering Claude Code as AuRE's endpoint, from nr-workbench's side.

nr-workbench does not implement the provider — AuRE does. What it owns is the
*advice*: every "no endpoint configured" message should offer
`LLM_PROVIDER=claude_code` when the installed AuRE has that provider, and must
not when it does not. AuRE is pinned by SHA and tracked on `main`, so an
installed copy can easily predate it, and advice naming a provider the user
cannot select is worse than no advice at all.

The detection is a file check rather than an import because `aure.llm.providers`
pulls in langchain, and this runs from error paths and from `nrw doctor`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from nr_workbench import aure_adapter


def _pretend_aure(monkeypatch, tmp_path: Path, *, with_provider: bool) -> None:
    """Point the on-disk check at a fabricated aure package."""
    pkg = tmp_path / "aure"
    (pkg / "llm" / "providers").mkdir(parents=True, exist_ok=True)
    if with_provider:
        (pkg / "llm" / "providers" / "claude_code.py").write_text("")

    real = importlib.util.find_spec

    def fake(name, *args, **kwargs):
        if name == "aure":
            return SimpleNamespace(submodule_search_locations=[str(pkg)])
        return real(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake)


# ── Detection ───────────────────────────────────────────────────────────


def test_provider_is_detected_when_the_module_is_there(monkeypatch, tmp_path) -> None:
    _pretend_aure(monkeypatch, tmp_path, with_provider=True)
    assert aure_adapter.claude_code_supported()


def test_an_older_aure_is_reported_as_not_supporting_it(monkeypatch, tmp_path) -> None:
    """The pin can point at a commit from before the provider existed."""
    _pretend_aure(monkeypatch, tmp_path, with_provider=False)
    assert not aure_adapter.claude_code_supported()


def test_detection_survives_aure_being_absent(monkeypatch) -> None:
    monkeypatch.setattr(aure_adapter, "is_available", lambda: False)
    monkeypatch.setattr(importlib.util, "find_spec", lambda *a, **k: None)
    assert not aure_adapter.claude_code_supported()


def test_detection_does_not_import_aure_or_langchain(monkeypatch, tmp_path) -> None:
    """A file check, not an import: this runs from `nrw doctor`, and
    `aure.llm.providers` costs seconds because it pulls in langchain."""
    _pretend_aure(monkeypatch, tmp_path, with_provider=True)

    real_import = (
        __builtins__["__import__"]
        if isinstance(__builtins__, dict)
        else __builtins__.__import__
    )

    def watched(name, *args, **kwargs):
        if name.split(".")[0] in {"aure", "langchain", "langchain_core"}:
            raise AssertionError(f"imported {name} to answer a capability question")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", watched)
    assert aure_adapter.claude_code_supported()


# ── The advice ──────────────────────────────────────────────────────────


def test_hint_offers_claude_code_when_it_is_available(monkeypatch) -> None:
    monkeypatch.setattr(aure_adapter, "claude_code_supported", lambda: True)
    hint = aure_adapter.endpoint_hint()
    assert "LLM_PROVIDER=claude_code" in hint
    assert "no key" in hint


def test_hint_stays_quiet_about_a_provider_that_is_not_there(monkeypatch) -> None:
    monkeypatch.setattr(aure_adapter, "claude_code_supported", lambda: False)
    hint = aure_adapter.endpoint_hint()
    assert "claude_code" not in hint
    assert "LLM_PROVIDER" in hint


# ── The gate that does not move ─────────────────────────────────────────


def test_an_endpoint_that_is_the_same_model_still_stands_down_under_an_agent() -> None:
    """The 2026-08-10 argument has two halves and claude_code answers one.

    The harness holds this conversation; a fresh `claude -p` does not, and its
    verdict still lands back in the harness's context as evidence. So the
    NRW_AGENT gate is unchanged, and the grep test that guards every call site
    stays the authority on that.
    """
    root = Path(__file__).resolve().parents[1] / "src" / "nr_workbench"
    gated = (root / "commands" / "assess.py").read_text()
    assert "agent_is_driving" in gated
