# Contributing

Thanks for contributing! This project follows the **assess → plan → implement →
test → review** workflow described in
[.github/copilot-instructions.md](.github/copilot-instructions.md) (also imported
by [CLAUDE.md](CLAUDE.md), so Claude Code follows the same rules).

## Setup

```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -e ".[dev]"
pre-commit install        # enable the lint / format / secret-scan hooks
```

## Before you open a pull request

- `pre-commit run --all-files` — runs the exact checks CI runs (ruff lint + format, secret scan, ...).
- `pytest` — all tests must pass and stay above the coverage floor in `pyproject.toml`.
  It runs in parallel by default (`-n auto --dist loadscope`), which takes the
  suite from ~176s to ~43s. **To debug one test, turn that off:**
  `pytest -n0 -s tests/test_x.py::test_y` — under xdist there is no stdout to
  attach to, so `-s` and `pdb` need `-n0`. A single small file is also faster
  serially (0.85s against 3.9s), since 20 workers each import the world.
- **The default run fits nothing.** A test that needs a fit's record gets the
  optimizer's answer from a recording in `tests/data/fits/`, and nrw's own runner
  still writes the record from it (`tests/fit_replay.py`). `pytest -m fit` runs
  the 33 tests that fit for real: run them when you change fitting code. A test
  whose fit no recording answers fails, with the command that records it:
  `pytest --record-fits <that test>`. A new test that must run the optimizer
  itself is marked `@pytest.mark.fit`.
- Add or update tests for any behavior change. Favor real, integration-style tests
  over heavy mocking — see the standard in [skills/review-test/SKILL.md](skills/review-test/SKILL.md).
- Record non-obvious decisions, API quirks, or constraints in
  [docs/ground_truths.md](docs/ground_truths.md) so they persist across sessions.
- Changed `nr_workbench/spec/models.py`? Run `python tools/regen_schema_asset.py`
  and commit the result. The bundled JSON Schema is generated from those models,
  and a stale copy does not merely go out of date — every object in it is closed,
  so a key it has not heard of is *rejected*. `pytest` fails if you forget.

## Reviews

For significant changes, run the reviewer subagents (design / security), and
review the tests yourself against `skills/review-test`. The subagents apply the
shared standards in [skills/](skills/); to change a standard, edit its
`SKILL.md`, not the dispatcher agent.

## Code style

Type hints and Google-style docstrings on public functions, specific exceptions
with clear messages, and comments that explain *why*. The
[copilot-instructions](.github/copilot-instructions.md) file has full examples.
