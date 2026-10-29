"""Tests for skill discovery, structure, and installation.

Skills are loaded as authoritative context by both assistants, so a malformed
or missing one degrades behaviour silently. Everything here is aimed at making
that loud.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nr_workbench.harness import HARNESSES, resolve
from nr_workbench.skills_install import (
    SkillError,
    bundled_skills_root,
    discover_skills,
    dispatcher_markdown,
    parse_skill,
    plan_skill_files,
)

#: Every harness that reads subagent stubs, so a new one joins these tests by
#: being registered rather than by being named here.
DISPATCHER_HARNESSES = [h for h in HARNESSES if h.agents_dir is not None]

CLAUDE = resolve(["claude"])[0]
COPILOT = resolve(["copilot"])[0]

#: The v2 anatomy from neutron-skills. Order is part of the contract: an agent
#: reading top-to-bottom should get context before process before verification.
V2_SECTIONS = [
    "## Overview",
    "## When to Use",
    "## Process",
    "## Rationalizations",
    "## Red Flags",
    "## Verification",
]

#: Skills `nrw init` installs by default.
SEED_SKILLS = {"nr-workbench-project", "neutron-reflectometry", "tnr-change-assessment"}


def write_skill(
    root: Path, domain: str, name: str, *, frontmatter: str, body: str = "x"
) -> Path:
    """Write a minimal skill on disk and return its SKILL.md path."""
    directory = root / domain / name
    directory.mkdir(parents=True, exist_ok=True)
    skill_md = directory / "SKILL.md"
    skill_md.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
    return skill_md


# --------------------------------------------------------------------------
# Bundled skills
# --------------------------------------------------------------------------


def test_bundled_skills_root_exists() -> None:
    assert bundled_skills_root().is_dir()


def test_seed_skills_are_bundled() -> None:
    names = {skill.name for skill in discover_skills()}

    assert names >= SEED_SKILLS, f"missing seed skills: {SEED_SKILLS - names}"


@pytest.mark.parametrize("skill", discover_skills(), ids=lambda s: s.name)
def test_bundled_skill_follows_v2_anatomy(skill) -> None:
    """Every shipped skill must carry the six v2 sections, in order."""
    text = (skill.directory / "SKILL.md").read_text(encoding="utf-8")

    positions = []
    for heading in V2_SECTIONS:
        index = text.find(f"\n{heading}\n")
        assert index != -1, f"{skill.name}: missing section '{heading}'"
        positions.append(index)

    assert positions == sorted(positions), f"{skill.name}: v2 sections are out of order"


@pytest.mark.parametrize("skill", discover_skills(), ids=lambda s: s.name)
def test_bundled_skill_frontmatter_is_complete(skill) -> None:
    """Frontmatter must carry the fields the retriever and dispatchers need."""
    text = (skill.directory / "SKILL.md").read_text(encoding="utf-8")
    meta = yaml.safe_load(text.split("---")[1])

    assert meta["name"] == skill.directory.name
    assert meta.get("version") == 2, "shipped skills use the v2 anatomy"
    metadata = meta.get("metadata") or {}
    assert metadata.get("tags"), "metadata.tags drives retrieval scoring"
    assert metadata.get("instruments"), "metadata.instruments drives retrieval scoring"
    # The house description style; also what the dispatcher shows the agent.
    assert "USE FOR:" in meta["description"]
    assert "DO NOT USE FOR:" in meta["description"]


@pytest.mark.parametrize("skill", discover_skills(), ids=lambda s: s.name)
def test_bundled_skill_body_stays_readable(skill) -> None:
    """Under ~500 lines; longer material belongs in references/."""
    lines = (skill.directory / "SKILL.md").read_text(encoding="utf-8").splitlines()

    assert len(lines) <= 500, (
        f"{skill.name}: {len(lines)} lines; move detail to references/"
    )


@pytest.mark.parametrize("skill", discover_skills(), ids=lambda s: s.name)
def test_no_bundled_skill_reads_the_critical_edge_as_the_top_layers_sld(
    skill,
) -> None:
    """A critical edge measures a step from the medium the beam enters through:
    d8-THF at 6.35, through silicon, is a step of 4.28. Three skills read the
    step as the top layer's SLD, and the one sent with every `nrw model new
    --from-notes` request called a model at 6.2 wrong against an edge of 4.3."""
    text = " ".join((skill.directory / "SKILL.md").read_text(encoding="utf-8").split())

    for claim in ("topmost SLD", "top-layer SLD", "your model says 6.2"):
        assert claim not in text, f"{skill.name}: {claim!r}"


@pytest.mark.parametrize(
    ("name", "reading"),
    [
        ("refl-bl4b-instrument", "Through the substrate, add the substrate's SLD"),
        ("metal-oxide-interfaces", "the solvent is the step plus the substrate's SLD"),
        ("solvent-contrast-matching", "The implied SLD is the step from the silicon"),
        ("structural-hypothesis-ranking", "once read as the step it is"),
    ],
)
def test_the_critical_edge_checks_add_the_substrate_in_back_reflection(
    name: str, reading: str
) -> None:
    (skill,) = [skill for skill in discover_skills() if skill.name == name]
    text = " ".join((skill.directory / "SKILL.md").read_text(encoding="utf-8").split())

    assert reading in text


# --------------------------------------------------------------------------
# parse_skill
# --------------------------------------------------------------------------


def test_parse_skill_reads_name_and_description(tmp_path: Path) -> None:
    skill_md = write_skill(
        tmp_path,
        "reflectometry",
        "demo-skill",
        frontmatter="name: demo-skill\ndescription: Does a thing.",
    )

    skill = parse_skill(skill_md, domain="reflectometry")

    assert skill.name == "demo-skill"
    assert skill.description == "Does a thing."
    assert skill.relative_skill_md == "skills/reflectometry/demo-skill/SKILL.md"


def test_parse_skill_rejects_name_directory_mismatch(tmp_path: Path) -> None:
    """A mismatch breaks the dispatcher's path, so it must fail at build time."""
    skill_md = write_skill(
        tmp_path, "d", "on-disk-name", frontmatter="name: other-name\ndescription: x"
    )

    with pytest.raises(SkillError, match="does not match its directory"):
        parse_skill(skill_md, domain="d")


def test_parse_skill_rejects_missing_frontmatter(tmp_path: Path) -> None:
    directory = tmp_path / "d" / "s"
    directory.mkdir(parents=True)
    skill_md = directory / "SKILL.md"
    skill_md.write_text("# no frontmatter\n", encoding="utf-8")

    with pytest.raises(SkillError, match="no YAML frontmatter"):
        parse_skill(skill_md, domain="d")


def test_parse_skill_rejects_missing_description(tmp_path: Path) -> None:
    skill_md = write_skill(tmp_path, "d", "s", frontmatter="name: s")

    with pytest.raises(SkillError, match="missing `description`"):
        parse_skill(skill_md, domain="d")


def test_parse_skill_rejects_uppercase_name(tmp_path: Path) -> None:
    skill_md = write_skill(
        tmp_path, "d", "Bad_Name", frontmatter="name: Bad_Name\ndescription: x"
    )

    with pytest.raises(SkillError, match="lowercase-hyphen"):
        parse_skill(skill_md, domain="d")


def test_discover_skills_raises_rather_than_skipping_a_broken_skill(
    tmp_path: Path,
) -> None:
    """A malformed skill is a packaging bug; skipping it would hide the bug."""
    write_skill(tmp_path, "d", "good", frontmatter="name: good\ndescription: x")
    write_skill(tmp_path, "d", "bad", frontmatter="description: no name here")

    with pytest.raises(SkillError):
        discover_skills(tmp_path)


# --------------------------------------------------------------------------
# Dispatchers
# --------------------------------------------------------------------------


def test_dispatchers_differ_only_by_their_declared_frontmatter() -> None:
    """Strip each harness's own frontmatter and every dispatcher must match.

    Anything else means the assistants are being told different things --
    exactly the drift the shared skills/ folder exists to prevent. Stated over
    the registry rather than over a named pair, so a harness cannot be added
    with a quietly different body.
    """
    skill = next(s for s in discover_skills() if s.name == "nr-workbench-project")

    bodies = {}
    for harness in DISPATCHER_HARNESSES:
        lines = dispatcher_markdown(skill, harness).splitlines()
        for declared in harness.dispatcher_frontmatter:
            assert declared in lines, f"{harness.name} lost its own frontmatter"
        bodies[harness.name] = [
            line for line in lines if line not in harness.dispatcher_frontmatter
        ]

    distinct = {tuple(body) for body in bodies.values()}
    assert len(distinct) == 1, f"dispatcher bodies diverged: {sorted(bodies)}"


def test_copilot_dispatchers_carry_no_tool_names() -> None:
    """Copilot's agent schema flags Claude's tool names as unknown.

    The read-only guardrail is real and Claude-specific; emitting it for
    Copilot produces a stub its own validator rejects.
    """
    skill = next(s for s in discover_skills() if s.name == "nr-workbench-project")

    assert "tools:" in dispatcher_markdown(skill, CLAUDE)
    assert "tools:" not in dispatcher_markdown(skill, COPILOT)


def test_dispatcher_points_at_the_installed_skill_path() -> None:
    skill = next(s for s in discover_skills() if s.name == "neutron-reflectometry")

    stub = dispatcher_markdown(skill, CLAUDE)

    assert "skills/reflectometry/neutron-reflectometry/SKILL.md" in stub


def test_dispatcher_frontmatter_survives_a_multiline_description(
    tmp_path: Path,
) -> None:
    """Descriptions contain colons and newlines; the block scalar must hold."""
    skill_md = write_skill(
        tmp_path,
        "d",
        "tricky",
        frontmatter='name: tricky\ndescription: "Line one: with a colon.\\nLine two."',
    )
    skill = parse_skill(skill_md, domain="d")

    stub = dispatcher_markdown(skill, COPILOT)
    meta = yaml.safe_load(stub.split("---")[1])

    assert meta["name"] == "tricky"
    assert "colon" in meta["description"]


# --------------------------------------------------------------------------
# Installation planning
# --------------------------------------------------------------------------


def test_plan_skill_files_includes_body_references_and_every_dispatcher() -> None:
    skill = next(s for s in discover_skills() if s.name == "neutron-reflectometry")

    paths = {
        relpath
        for relpath, _ in plan_skill_files(
            skill, harnesses=[h.name for h in DISPATCHER_HARNESSES]
        )
    }

    assert "skills/reflectometry/neutron-reflectometry/SKILL.md" in paths
    assert (
        "skills/reflectometry/neutron-reflectometry/references/refinement-strategy.md"
        in paths
    ), "references/ material must be installed alongside the skill"
    for harness in DISPATCHER_HARNESSES:
        assert f"{harness.agents_dir}/neutron-reflectometry.md" in paths


def test_plan_skill_files_writes_dispatchers_only_for_selected_harnesses() -> None:
    """A project that does not use an assistant must not carry its stubs.

    Before harnesses were selectable every project got a `.github/agents/` tree
    whether or not anyone read it.
    """
    skill = next(s for s in discover_skills() if s.name == "neutron-reflectometry")

    paths = {relpath for relpath, _ in plan_skill_files(skill, harnesses=["claude"])}

    assert ".claude/agents/neutron-reflectometry.md" in paths
    assert ".github/agents/neutron-reflectometry.md" not in paths
    assert "skills/reflectometry/neutron-reflectometry/SKILL.md" in paths, (
        "the skill itself is tool-neutral and installs regardless"
    )
