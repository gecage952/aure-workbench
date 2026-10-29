"""Turning a scientist's notes into the physics half of a model spec.

``nrw model new`` fills in everything that is a *fact*: which runs exist, which
files belong to them, each segment's incident angle read from its header. It
cannot fill in the *stack* -- what the sample is made of, in what order, roughly
how thick -- because that is in the scientist's head and, if they wrote it down,
in ``sample.md``.

This module closes that gap two ways:

* :func:`build_prompt` is the request ``nrw model new --from-notes`` sends a
  configured endpoint, with the notes, the skills and the measured facts in it.
* :func:`agent_instructions` is what ``--print-prompt`` gives a coding assistant
  already in the repository. It names those files rather than inlining them,
  and the assistant runs the checks itself.

They are two texts, so a rule both must state is written once and used by
both. :data:`STACK_ORDER` is the one that drifted: the endpoint was told
"ambient first, substrate last" for every sample, which is back reflection,
while the assistant was told the last entry is the medium the beam enters.

**The division of labour is enforced, not requested.** A model may propose only
the :data:`PROPOSABLE` keys: what the sample is, and the choices its notes make.
Everything read from disk -- states, series, angles, file paths -- is filtered
out of whatever comes back, in :func:`merge_proposal`. A language model is being
asked what the sample is, never what was measured: the second is already known
exactly, and a plausible wrong answer there is unrecoverable.
"""

from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def placeholder_materials() -> dict[str, dict[str, float]]:
    """The materials `nrw model new` writes when nothing proposed any.

    A new copy each call, as is :func:`placeholder_stack`: a caller that changed
    one in place would otherwise change what :func:`is_placeholder` looks for.
    """
    return {"Ambient": {"rho": 0.0}, "Film": {"rho": 4.0}, "Si": {"rho": 2.07}}


def placeholder_stack() -> list[dict[str, Any]]:
    """The stack `nrw model new` writes when nothing proposed one.

    Air on a film on silicon: deliberately obvious as a stub, and valid, so the
    scaffold validates and generates as written.
    """
    return [
        {"name": "Ambient", "material": "Ambient", "thickness": 0, "roughness": 5},
        {"name": "Film", "material": "Film", "thickness": 100, "roughness": 5},
        {"name": "Si", "material": "Si"},
    ]


#: Which end of the stack the beam enters, said once for both requests.
#: refl1d takes the last entry as the incident medium, so the order *is* the
#: measurement geometry: `spec/geometry.py` reads it that way, and
#: `nrw model validate` checks it against the measured critical edge.
STACK_ORDER = (
    "Order the stack so the LAST entry is the medium the beam enters through. "
    "refl1d takes the last entry as the incident medium, so this order is the "
    "measurement geometry, not a presentation choice: a solid/liquid cell "
    "measured through the wafer ends with the substrate, and a film measured "
    "from the air side ends with the air. The last entry takes no thickness or "
    "roughness; the first, the backing medium, has thickness 0. Getting it "
    "backwards fits, converges, and reports a chi-squared in the hundreds with "
    "nothing naming the cause."
)

#: Keys a proposal may set. Anything else it returns is discarded.
PROPOSABLE = (
    "description",
    "materials",
    "stack",
    "parameters",
    "constraints",
    # Choices, not facts. `series_select` says which slices to model and
    # `probe.back_reflection` says which side the beam enters -- both are in
    # the scientist's notes and neither is derivable from the file listing.
    "series_select",
    "probe",
)

#: The only probe keys a proposal may set. `resolution` is fixed by the
#: schema and `dq_is_fwhm` is a property of the reduction, not something to
#: infer from prose.
PROPOSABLE_PROBE = ("back_reflection",)

#: Skills always sent: the schema, the physics, and the beamline conventions.
#: `sample-broadening` is here rather than in CONTEXTUAL_SKILLS because the
#: `per:` scope of the broadening term is a decision every multi-angle spec
#: makes, and getting it wrong is absorbed by a roughness rather than reported.
CORE_SKILLS = (
    "nrw-model-spec",
    "neutron-reflectometry",
    "refl-bl4b-instrument",
    "sample-broadening",
)

#: Skills sent when the notes mention them, keyed by the words that select each.
#: Three kinds of trigger, none of them a substring -- as substrings, "cu"
#: chose copper physics for "curve" and "thin" chose thin-layer degeneracy for
#: "nothing":
#:
#: * a word, matched whole and in any case, a plural allowed;
#: * a stem, ending in ``*``, that a word starts with (``deuterat*``);
#: * an element symbol, capitalised, matched in its own case with no lowercase
#:   letter after it -- ``Cu`` is in "CuO", "Cu2O" and "Cu/THF", not in "cues".
#:
#: "film" is not a polymer trigger. Every sample here is a film, so it chose
#: polymer physics for a copper one.
CONTEXTUAL_SKILLS: dict[str, tuple[str, ...]] = {
    "metal-oxide-interfaces": (
        "Cu",
        "copper",
        "oxide",
        "cuox",
        "titanium",
        "Ti",
        "chromium",
        "gold",
        "Au",
        "electrode",
        "metal*",
    ),
    "polymer-films": (
        "polymer*",
        "ionomer",
        "nafion",
        "pfsa",
        "brush",
        "peo",
        "pmma",
        "polystyrene",
        "swell*",
    ),
    "solvent-contrast-matching": (
        "d2o",
        "h2o",
        "thf",
        "dthf",
        "solvent",
        "deuterat*",
        "contrast",
        "toluene",
        "ethanol",
        "water",
    ),
    "thin-layer-degeneracy": ("oxide", "thin", "thinner", "adhesion", "interfacial"),
}


def _mentions(text: str, triggers: tuple[str, ...]) -> bool:
    """Whether the text uses any trigger, as :data:`CONTEXTUAL_SKILLS` reads them.

    Args:
        text: The notes, in their own case.
        triggers: One skill's triggers.

    Returns:
        True when one of them appears.
    """
    for trigger in triggers:
        if trigger[:1].isupper():
            found = re.search(rf"\b{re.escape(trigger)}(?![a-z])", text)
        elif trigger.endswith("*"):
            found = re.search(rf"\b{re.escape(trigger[:-1])}", text, re.IGNORECASE)
        else:
            # A plural only for a word long enough to have one: on "ti" it
            # matched "ties".
            plural = "(?:e?s)?" if trigger.isalpha() and len(trigger) >= 3 else ""
            found = re.search(rf"\b{re.escape(trigger)}{plural}\b", text, re.IGNORECASE)
        if found:
            return True
    return False


def _prose(notes: str) -> str:
    """The notes without the template's guidance, whose examples are not them."""
    from nr_workbench.notes import strip_comments

    return strip_comments(notes or "")


@dataclass
class Proposal:
    """What a model suggested, and what was done with it.

    Attributes:
        document: The accepted keys, ready to merge.
        rejected: Keys the model returned that it is not allowed to set.
        dropped_paths: Constrained paths removed for having no range.
        notes: Any commentary it offered, for the human to read.
        raw: The reply as received, for debugging.
    """

    document: dict[str, Any] = field(default_factory=dict)
    rejected: list[str] = field(default_factory=list)
    dropped_paths: list[str] = field(default_factory=list)
    notes: str = ""
    raw: str = ""


class AuthoringError(Exception):
    """Raised when a proposal cannot be obtained or understood."""


def relevant_skills(notes: str, available: dict[str, Path]) -> list[str]:
    """Choose which skills to send as context.

    Args:
        notes: The ``sample.md`` text.
        available: Skill name to its ``SKILL.md`` path.

    Returns:
        Skill names, core ones first, then any the notes select.
    """
    prose = _prose(notes)
    chosen = [name for name in CORE_SKILLS if name in available]
    for name, triggers in CONTEXTUAL_SKILLS.items():
        if name in available and name not in chosen and _mentions(prose, triggers):
            chosen.append(name)
    return chosen


def missing_relevant(notes: str, installed: dict[str, Path]) -> list[str]:
    """Name skills the notes call for that are not installed here.

    `nrw init` seeds the skills that apply to any sample; the material-specific
    ones are bundled but left to `nrw skills sync`. So the ones a particular
    sample most needs are exactly the ones likely to be absent, and silently
    proceeding without them wastes the context that makes the answer good.

    Args:
        notes: The ``sample.md`` text.
        installed: Skill name to path, as from :func:`find_skills`.

    Returns:
        Skill names worth installing before asking.
    """
    prose = _prose(notes)
    wanted = []
    for name, triggers in CONTEXTUAL_SKILLS.items():
        if name not in installed and _mentions(prose, triggers):
            wanted.append(name)
    return sorted(wanted)


def skill_sections(names: list[str], skills: dict[str, Path]) -> tuple[str, list[str]]:
    """The chosen skills as a request's reference sections, read safely.

    Args:
        names: The skills to include, in order.
        skills: Skill name to its ``SKILL.md``, as from :func:`find_skills`.

    Returns:
        ``(text, unreadable)``: one ``===== SKILL: name =====`` section per
        skill that could be read, and the names of any that could not -- a
        broken file costs that skill, not the whole request.
    """
    sections: list[str] = []
    unreadable: list[str] = []
    for name in names:
        try:
            body = skills[name].read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            unreadable.append(name)
            continue
        sections.append(f"===== SKILL: {name} =====\n{body}")
    return "\n\n".join(sections), unreadable


def find_skills(root: Path) -> dict[str, Path]:
    """Locate the project's installed skills.

    Args:
        root: Project root.

    Returns:
        Skill name to ``SKILL.md`` path.
    """
    found: dict[str, Path] = {}
    base = Path(root) / "skills"
    if not base.is_dir():
        return found
    for path in sorted(base.rglob("SKILL.md")):
        found[path.parent.name] = path
    return found


def build_prompt(
    *,
    skeleton: dict[str, Any],
    notes: str,
    skills: dict[str, Path],
    facts: str = "",
    measurements: str = "",
    experiment: str = "",
) -> tuple[str, str]:
    """Build the system and user halves of the request.

    Args:
        skeleton: The scaffolded spec, with its facts already filled in.
        notes: The ``sample.md`` text.
        skills: Skill name to path, as from :func:`find_skills`.
        facts: Extra measured context, e.g. `nrw data features` output.
        measurements: Each state's run, condition and notes, as the notes
            give them (see :func:`describe_measurements`).
        experiment: The experiment's own context, as
            :meth:`~nr_workbench.experiment.quoted.Quotable.text` gives it;
            nothing when none is written.

    Returns:
        ``(system, user)``.
    """
    import yaml

    skill_text, _ = skill_sections(relevant_skills(notes, skills), skills)

    system = (
        "You are helping a neutron reflectometry scientist at SNS REF_L (BL-4B) "
        "turn their notes into an `nrw-model/1` model specification.\n\n"
        "The reference material below is the project's own skills. Follow them; "
        "they carry this beamline's conventions and the schema.\n\n"
        f"{skill_text}\n\n"
        "===== YOUR TASK =====\n"
        "Propose the physics half of the spec, and only that.\n\n"
        "Return a single JSON object with these keys and no others:\n"
        '  "description" : one or two sentences describing the sample\n'
        '  "materials"   : {name: {"rho": float}} -- SLD in 1e-6/A2\n'
        '  "stack"       : [ {name, material, thickness, roughness}, ... ]\n'
        "                  in the order the stack rule below gives\n"
        '  "parameters"  : [ {path, range|value, per, in, ...}, ... ]\n'
        '  "constraints" : [ {series, form, from, to, paths} ] -- REQUIRED\n'
        "                  whenever the skeleton has a series\n"
        '  "series_select": {"<series name>": {"labels": ["*eis*"]}} --\n'
        "                  when the notes restrict which slices to model\n"
        '  "probe"       : {"back_reflection": true} -- ONLY if the notes\n'
        "                  say the beam enters through the substrate or\n"
        "the back of the sample\n"
        '  "notes"       : anything you are unsure about, for the human\n\n'
        "Rules:\n"
        f"- {STACK_ORDER}\n"
        "- Do NOT return `states`, `series`, `thetas`, `data_dir`, `run`, "
        "`reduced_dir`, `schema`, `name` or `sample`. Those are read from the "
        "files on disk and are already correct. Anything you return for them "
        "will be discarded.\n"
        "- Use the state and series names given in the skeleton when you write "
        "`in:` lists.\n"
        "- Prefer FEWER layers. Every layer under about 30 A is barely "
        "resolvable; do not add one without a reason from the notes.\n"
        "- Take the ambient -- what the sample sits in -- from the notes and "
        "from EACH MEASUREMENT below; never assume air. Its `rho` is a "
        "`parameters:` entry with a range around the solvent's SLD, unless it "
        "is air. When the measurements' conditions differ in the solvent or "
        "the contrast (D2O for one state, H2O for another), declare it once "
        "per state: {path: <ambient>.rho, range: [...], per: state, in: "
        "[<that state>]}, each range around that state's solvent. Whatever else "
        "the conditions say differs between the states is `per: state` too; "
        "what they say is shared is `per: model`.\n"
        "- If the skeleton has a `series`, RETURN a `constraints` block. Do "
        "not describe the change you would make in `notes` and omit the block "
        "-- an omitted constraint leaves every slice refitting the whole "
        "structure independently. Without one "
        "every slice refits the whole structure independently, which is almost "
        "never what is wanted. Choose the form from the evidence, in this "
        "order of preference:\n"
        "    linear_in_time    a(t) rises steadily; slices unevenly spaced.\n"
        "                      THE DEFAULT for a monotonic change.\n"
        "    linear_in_index   same, but only if slices are evenly spaced.\n"
        "    logistic          a(t) is sigmoidal -- induction, transition,\n"
        "                      plateau. Fits t_half and width.\n"
        "    exponential       a first-order relaxation to equilibrium.\n"
        "                      Fits tau.\n"
        "    piecewise_linear  structure no closed form captures. Costs K\n"
        "                      knots; use only if the simpler forms fail.\n"
        "    free              one parameter per slice. Last resort.\n"
        "    fixed             nothing changes across the series.\n"
        "  `from` and `to` are the steady states either side of the series, so "
        "the interpolating forms add no free parameters. Write `free` for "
        "either endpoint to fit it instead -- one extra parameter per path. "
        "Do that when the series has no bracketing state, or when the notes "
        "say something happened between the steady measurement and the run.\n"
        "  `paths` decides what is allowed to change across the series. The "
        "NOTES are authoritative here -- if they say which parameters change, "
        'list exactly those, and "the other parameters, even the Ti layer, '
        'will change" means include them all. Only when the notes are silent, '
        "fall back to the assessment: an oscillatory template in Q means a "
        "THICKNESS change, one-sign means an SLD contrast change.\n"
        "- Give every free parameter a physically sensible range, not a wide "
        "one.\n"
        "- If the notes do not say what a layer is made of, say so in `notes` "
        "rather than inventing a material.\n"
        "- SCOPE every parameter from the words the notes use. This mapping "
        "is the most common source of a wrong model, and the notes are usually "
        "explicit about it:\n"
        '    "each partial data file", "per segment", "each angle"\n'
        "      -> per: measurement\n"
        '    "common for all time slices", "one for the series"\n'
        "      -> per: state, in: [<the series>]\n"
        '    "common to all data", "the same everywhere", "shared"\n'
        "      -> per: model\n"
        '    "per state", "may differ between the two states"\n'
        "      -> per: state, in: [<the steady states>]\n"
        "  A structural parameter declared `per: state` with NO `in:` covers "
        "the series too and will collide with the constraint. Always scope "
        "structural parameters with `in:` when a series is present.\n"
        "- If the notes say the series should NOT be tied to the states either "
        'side -- "not tied", "there was a lag", "do not anchor", "the '
        'endpoints should be fitted" -- use `from: free` and `to: free` on the '
        "constraint instead of naming the states. Each free endpoint adds one "
        "parameter per path and is the right answer when the states do not "
        "continue smoothly into the run.\n"
        '- If the notes restrict which slices to model -- "only the eis '
        'data", "never the hold intervals", "the string to look for is X" -- '
        'set `series_select`, e.g. {"tnr218389": {"labels": ["*eis*"]}}.\n'
        "- Read the notes for *instrument* problems as well as sample "
        "composition, and add the matching nuisance parameter when one is "
        "described. These are easy to miss and each one, left out, pushes its "
        "error into a layer:\n"
        "    sample misaligned / angle uncertain / offset\n"
        "      -> {path: probe.theta_offset, range: [-0.02, 0.02], per: ...}\n"
        "    sample curved / bent / warped / mosaic / fringes damped, or any "
        "multi-angle fit at all\n"
        "      -> {path: probe.sample_broadening, range: [0.0, 0.15], "
        "per: angle}\n"
        "    high background / poor statistics at high Q\n"
        "      -> {path: probe.background, range: [0.0, 1.0e-5], per: state}\n"
        "  SCOPE theta_offset by asking whether the sample was physically "
        "MOVED, not whether it changed: it describes how the sample sits in the "
        "beam. An in-situ cell measured continuously -- an OCV, a tNR run, "
        "another OCV -- is never remounted, so use `per: model`: one alignment "
        "for the whole experiment. Use `per: state` only if the notes say the "
        "sample was remounted, moved, or realigned between measurements. "
        "Fitting one per state on a sample that never moved is several "
        "parameters describing one quantity, and they absorb the real "
        "differences between the states.\n"
        "  sample_broadening does NOT follow that rule -- scope it "
        "`per: angle`. On BL-4B its dominant cause is aperture-limited "
        "divergence at small incident angles, so it belongs to the ANGLE, not "
        "the mounting. refl1d adds it to the divergence, giving "
        "dQ/Q = (dtheta + omega)/tan(theta), which diverges as theta -> 0, so "
        "one shared value necessarily over-smears the lowest-angle segment or "
        "under-smears the rest. Expect the fitted values to be largest at the "
        "lowest angle and near zero at 3.5 deg.\n"
        "  `probe.intensity` is nearly always `per: state`: each reduction used "
        "its own direct beam. Add a `per: measurement` override with `in:` for "
        "any single segment the notes describe as mis-normalised.\n"
        "  theta_offset and sample_broadening only work on states measured per "
        "angle (`segments: auto`); scope them with `in:` if any state is "
        "`kind: combined`.\n"
        "- Output JSON only. No markdown fence, no commentary outside the JSON."
    )

    skeleton_yaml = yaml.safe_dump(skeleton, sort_keys=False)
    user_parts = [
        "===== THE SCIENTIST'S NOTES (sample.md) =====",
        _prose(notes).strip()
        or "(empty -- the scientist has not written anything yet)",
        "",
    ]
    if experiment:
        user_parts += [
            "===== THE EXPERIMENT THIS SAMPLE IS PART OF =====",
            "What the scientist wrote about the whole experiment, not this "
            "sample alone. Where it and the notes disagree, the notes are about "
            "this sample.",
            "",
            experiment,
            "",
        ]
    user_parts += [
        "===== WHAT IS ON DISK =====",
        "This part is already correct. Do not change it; use its names.",
        "",
        skeleton_yaml,
    ]
    if measurements:
        user_parts += [
            "===== EACH MEASUREMENT, AS THE NOTES DESCRIBE IT =====",
            "One line per state and series of the skeleton: its run, its "
            "condition in the Measurements table, and the notes on that run.",
            measurements,
        ]
    if facts:
        user_parts += ["===== MEASURED FROM THE DATA =====", facts]
    # Said in the user half as well: the claude_code provider decides whether a
    # reply should be JSON, and retries one that is not, from this half alone.
    user_parts += ["", "Reply with the JSON object the instructions describe."]
    return system, "\n".join(user_parts)


def is_placeholder(text: str) -> bool:
    """Whether a spec still has the placeholder stack `nrw model new` writes.

    The stack is compared, not the header comment announcing it: a person
    replacing the layers may well leave the comment where it is.

    Args:
        text: The spec's YAML.

    Returns:
        True when its materials and stack are still the stub's.
    """
    import yaml

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError:
        return False
    return (
        isinstance(document, dict)
        and document.get("materials") == placeholder_materials()
        and document.get("stack") == placeholder_stack()
    )


def describe_measurements(skeleton: dict[str, Any], notes: str) -> str:
    """Each state's and series' run, condition and notes, for the request.

    The notes are sent whole as well, but a model left to connect a table row
    to a state name by itself can miss it -- and then fits every state in air.

    Args:
        skeleton: The scaffolded spec, whose states and series name their runs.
        notes: The ``sample.md`` text.

    Returns:
        One line per state and series, and one for what they all share; ``""``
        when the notes say nothing about any measurement.
    """
    from nr_workbench.experiment.adopt import measurements_of

    try:
        measured = measurements_of(notes)
    except Exception:  # noqa: BLE001 - the notes still go whole
        return ""
    lines: list[str] = []
    said = False
    blocks = [
        *(("state", block) for block in skeleton.get("states") or []),
        *(("series", block) for block in skeleton.get("series") or []),
    ]
    for kind, block in blocks:
        run = block.get("run")
        parts = []
        # One line each, whatever was written: a note spanning lines must not
        # make a line of its own that reads as another state's.
        if measured.conditions.get(run):
            parts.append(f"condition: {_one_line(measured.conditions[run])}")
        if measured.notes.get(run):
            parts.append(f"notes: {_one_line(measured.notes[run])}")
        said = said or bool(parts)
        lines.append(
            f"- {kind} {block.get('name')} (run {run}): "
            + ("; ".join(parts) if parts else "nothing written about it")
        )
    if measured.shared:
        said = True
        lines.append(f"- every state: {_one_line(measured.shared)}")
    return "\n".join(lines) if said else ""


def _one_line(text: str) -> str:
    return " ".join(text.split())


def parse_proposal(reply: str) -> Proposal:
    """Read a model's reply into a proposal.

    Args:
        reply: The raw reply text.

    Returns:
        The parsed proposal, with disallowed keys recorded in ``rejected``.

    Raises:
        AuthoringError: If no JSON object can be found in the reply.
    """
    payload = extract_json(reply)
    if payload is None:
        raise AuthoringError(
            "The reply contained no JSON object. First 200 characters:\n"
            f"  {reply.strip()[:200]}"
        )

    proposal = Proposal(raw=reply, notes=str(payload.get("notes") or "").strip())
    for key, value in payload.items():
        if key == "notes":
            continue
        if key in PROPOSABLE:
            proposal.document[key] = value
        else:
            proposal.rejected.append(key)
    return proposal


def merge_proposal(skeleton: dict[str, Any], proposal: Proposal) -> dict[str, Any]:
    """Merge a proposal into the scaffolded skeleton.

    The skeleton wins for anything read from disk. This is the enforcement
    point for that division: only :data:`PROPOSABLE` keys are taken, whatever
    the model returned and whatever the prompt asked for.

    Args:
        skeleton: The scaffolded spec.
        proposal: What the model suggested.

    Returns:
        A new merged document.
    """
    merged = dict(skeleton)
    for key in PROPOSABLE:
        if key in ("series_select", "probe"):
            continue
        value = proposal.document.get(key)
        if value:
            merged[key] = value

    _apply_series_select(merged, proposal.document.get("series_select"))
    _apply_probe(merged, proposal.document.get("probe"))
    pinned = _drop_model_scoped_from_constraints(merged)
    _scope_constrained_parameters(merged)
    _repair_constraints(merged)
    proposal.dropped_paths = pinned + _drop_unrangeable_paths(merged)
    return merged


def _drop_unrangeable_paths(document: dict[str, Any]) -> list[str]:
    """Remove constrained paths that a free endpoint could not be bounded from.

    A `free` endpoint borrows its range from the path's `parameters`
    declaration. A path listed in `paths` with no declaration and no
    `endpoint_range` therefore has nothing to bound it, and resolution refuses
    -- correctly, since an unbounded endpoint drags the whole trajectory.

    Inventing a range would be the wrong repair: a plausible-looking interval
    on an SLD is exactly the kind of guess this package exists to avoid. So the
    path is dropped and named, and the human can declare it and regenerate.

    Args:
        document: The merged spec.

    Returns:
        The paths removed, for the caller to report.
    """
    from nr_workbench.spec.constraints import FREE_ENDPOINT

    declared = {
        str(parameter.get("path"))
        for parameter in document.get("parameters") or []
        if isinstance(parameter, dict) and parameter.get("range") is not None
    }

    dropped: list[str] = []
    kept_constraints = []
    for constraint in document.get("constraints") or []:
        if not isinstance(constraint, dict):
            continue
        free_ends = FREE_ENDPOINT in (constraint.get("from"), constraint.get("to"))
        if not free_ends or constraint.get("endpoint_range"):
            kept_constraints.append(constraint)
            continue
        paths = []
        for path in constraint.get("paths") or []:
            if str(path) in declared:
                paths.append(path)
            else:
                dropped.append(str(path))
        if paths:
            kept_constraints.append({**constraint, "paths": paths})

    if kept_constraints:
        document["constraints"] = kept_constraints
    elif "constraints" in document:
        document.pop("constraints")
    return dropped


def _scope_constrained_parameters(document: dict[str, Any]) -> None:
    """Scope `per: state` structural parameters away from the constrained series.

    `per: state` with no `in:` covers *every* group, series included, so a path
    that is also in a constraint's `paths` is assigned twice and the spec does
    not validate. The prompt asks for the `in:` and it is still omitted about
    half the time -- which is the signal that this belongs in code. A prompt is
    a request; producing a spec that validates is a correctness property.

    Only paths the constraint already owns are touched, and only when no `in:`
    was given, so a deliberate scoping is never overridden.
    """
    states = [
        str(state.get("name"))
        for state in document.get("states") or []
        if isinstance(state, dict) and state.get("name")
    ]
    if not states:
        return

    constrained: set[str] = set()
    for constraint in document.get("constraints") or []:
        if isinstance(constraint, dict):
            constrained.update(str(path) for path in constraint.get("paths") or [])
    if not constrained:
        return

    for parameter in document.get("parameters") or []:
        if not isinstance(parameter, dict):
            continue
        if parameter.get("per") != "state" or parameter.get("in"):
            continue
        if str(parameter.get("path")) in constrained:
            parameter["in"] = list(states)


def _drop_model_scoped_from_constraints(document: dict[str, Any]) -> list[str]:
    """Remove `per: model` paths from constraint paths.

    `per: model` is an explicit statement that a quantity is the same
    everywhere -- "use the steady states to pin down the copper SLD" -- and a
    constraint gives it a per-slice trajectory. The two contradict, and the
    spec does not validate.

    The declaration wins: it is the more specific statement of intent, and it
    is what the notes usually say in so many words.

    Returns:
        The paths removed, for the caller to report.
    """
    pinned = {
        str(parameter.get("path"))
        for parameter in document.get("parameters") or []
        if isinstance(parameter, dict) and parameter.get("per") == "model"
    }
    if not pinned:
        return []

    removed: list[str] = []
    kept = []
    for constraint in document.get("constraints") or []:
        if not isinstance(constraint, dict):
            continue
        paths = []
        for path in constraint.get("paths") or []:
            if str(path) in pinned:
                removed.append(str(path))
            else:
                paths.append(path)
        if paths:
            kept.append({**constraint, "paths": paths})

    if kept:
        document["constraints"] = kept
    elif "constraints" in document:
        document.pop("constraints")
    return removed


def _apply_series_select(document: dict[str, Any], selection: Any) -> None:
    """Set `select` on the named series, leaving every other field alone.

    Which slices to model is a choice -- "only the eis intervals, never the
    holds" -- while the run number, directory and angle are facts read off the
    disk. So the selection is merged in rather than the series being replaced.
    """
    if not isinstance(selection, dict):
        return
    for series in document.get("series") or []:
        if not isinstance(series, dict):
            continue
        chosen = selection.get(str(series.get("name")))
        if isinstance(chosen, dict) and chosen:
            series["select"] = chosen


def _apply_probe(document: dict[str, Any], probe: Any) -> None:
    """Merge the probe keys a proposal is allowed to set."""
    if not isinstance(probe, dict):
        return
    existing = dict(document.get("probe") or {})
    for key in PROPOSABLE_PROBE:
        if key in probe:
            existing[key] = probe[key]
    document["probe"] = existing


def _repair_constraints(document: dict[str, Any]) -> None:
    """Drop constraint paths naming layers the stack no longer has.

    The scaffold writes a `linear_in_time` constraint over `Film.thickness`,
    because `Film` is the placeholder stack's only layer. When a proposal
    replaces that stack, the constraint is left pointing at a layer that no
    longer exists and the spec does not validate -- an authored spec that fails
    its own `nrw model validate` is worse than the placeholder it replaced.

    Paths that survive are kept, so a proposal that renames the stack but keeps
    a thickness constraint still gets one. A constraint left with no paths is
    removed entirely.
    """
    layers = {
        str(entry.get("name"))
        for entry in document.get("stack", [])
        if isinstance(entry, dict) and entry.get("name")
    }
    if not layers:
        return

    kept = []
    for constraint in document.get("constraints", []) or []:
        if not isinstance(constraint, dict):
            continue
        paths = [
            path
            for path in constraint.get("paths", [])
            if not isinstance(path, str)
            or "." not in path
            or path.split(".", 1)[0] in layers
            or path.startswith("probe.")
        ]
        if paths:
            kept.append({**constraint, "paths": paths})

    if not kept:
        kept = _rebuild_constraints(document, layers)

    if kept:
        document["constraints"] = kept
    else:
        document.pop("constraints", None)


def _rebuild_constraints(
    document: dict[str, Any], layers: set[str]
) -> list[dict[str, Any]]:
    """Re-aim a constraint at the paths the new stack actually varies.

    A proposal often replaces the stack and describes the constraint change in
    prose instead of returning one -- a real reply said "the existing
    linear_in_time constraint should be moved from Film.thickness to
    CuOx.thickness" and then omitted the block. Dropping it leaves every slice
    refitting the whole structure independently, which is never what was
    wanted and is invisible in the spec.

    The right paths are derivable: a parameter declared `per: state` across the
    endpoint states is, by definition, a quantity the experiment changed
    between them, so interpolating it across the series is exactly what the
    form is for.

    Args:
        document: The merged spec.
        layers: Layer names present in the stack.

    Returns:
        A single rebuilt constraint, or an empty list if nothing qualifies.
    """
    series = document.get("series") or []
    states = document.get("states") or []
    if not series or len(states) < 2:
        return []

    endpoints = [str(state.get("name")) for state in states[:1] + states[-1:]]
    paths = []
    for parameter in document.get("parameters") or []:
        if not isinstance(parameter, dict):
            continue
        path = str(parameter.get("path", ""))
        if parameter.get("per") != "state" or path.startswith("probe."):
            continue
        if "." not in path or path.split(".", 1)[0] not in layers:
            continue
        targets = [str(t) for t in (parameter.get("in") or endpoints)]
        if all(name in targets for name in endpoints):
            paths.append(path)

    if not paths:
        return []
    return [
        {
            "series": str(series[0].get("name")),
            "form": "linear_in_time",
            "from": endpoints[0],
            "to": endpoints[-1],
            "paths": paths,
        }
    ]


def agent_instructions(
    *,
    spec_path: str,
    notes_path: str,
    skills: list[str],
    sample: str,
) -> str:
    """Write the instruction to hand a coding assistant.

    For the common case: VS Code with Claude Code or Copilot open on the
    project, no endpoint configured for nr-workbench itself. The assistant can
    already read every file named here, so the instruction points at them
    rather than inlining them.

    Args:
        spec_path: The scaffolded spec, relative to the project root.
        notes_path: The sample's ``sample.md``.
        skills: Skill names worth reading for this sample.
        sample: The sample identifier.

    Returns:
        A ready-to-paste instruction.
    """
    skill_lines = "\n".join(
        f"   - skills/reflectometry/{name}/SKILL.md" for name in skills
    )
    order = textwrap.fill(STACK_ORDER, width=76, subsequent_indent="   ")
    return f"""\
Fill in the model spec at {spec_path} for sample {sample}.

1. Read these skills first and follow them:
{skill_lines}

2. Read {notes_path} for what the sample is and what was done to it -- and
   for each measurement: its condition in the Measurements table, and its
   notes under Measurement conditions. Take the ambient from them, never
   assume air. What the conditions say differs between runs (a solvent
   contrast, D2O in one and H2O in another) is a parameter per state: the
   ambient's rho, `per: state`, one entry `in:` each state, each ranged
   around that state's solvent.

3. Edit ONLY these parts of the spec:
     description, materials, stack, parameters, constraints

   Leave `states`, `series`, `thetas`, `data_dir`, `run` and `reduced_dir`
   exactly as they are, and add no angles. Each incident angle is read from its
   data file's own header when the spec is resolved. A `null` angle is one no
   file records, for the person who measured it to give -- never fill it in.

4. {order}

   If the beam enters through the substrate, also set
   `probe: {{back_reflection: true}}`. It reorders nothing: `nrw model validate`
   checks the stack's order against it.

   Prefer fewer layers: below about 30 A a layer is barely resolvable, so do
   not add one without a reason from the notes.

   Read the notes for instrument problems too, and add the matching nuisance
   parameter -- left out, each pushes its error into a layer:

     misaligned / angle uncertain
       {{path: probe.theta_offset, range: [-0.02, 0.02], per: ...}}
     curved / bent / mosaic / fringes damped, or any multi-angle fit
       {{path: probe.sample_broadening, range: [0.0, 0.15], per: angle}}
     high background at high Q
       {{path: probe.background, range: [0.0, 1.0e-5], per: state}}

   Scope `theta_offset` by asking whether the sample was physically MOVED, not
   whether it changed -- it describes how the sample sits in the beam. An in-situ
   cell measured continuously is never remounted, so `per: model`: one alignment
   for the whole experiment. `per: state` only if the notes say it was remounted
   or realigned.

   `sample_broadening` is the exception: scope it `per: angle`. Its dominant
   cause here is aperture-limited divergence at small incident angles, so it
   belongs to the ANGLE rather than the mounting, and one shared value cannot
   represent it -- dQ/Q = (dtheta + omega)/tan(theta) diverges as theta -> 0.

   `probe.intensity` is nearly always `per: state`, since each reduction used its
   own direct beam.

   Both need per-angle data; scope with `in:` if a state is combined.

5. If the spec has a `series`, it needs a `constraints:` block -- without one,
   every slice refits the whole structure independently. Run
   `nrw model forms` for the list. Choose from the evidence:

     a(t) rises steadily        -> linear_in_time   (the usual answer)
     a(t) is sigmoidal          -> logistic         (fits t_half, width)
     first-order relaxation     -> exponential      (fits tau)
     none of the above fits     -> piecewise_linear (costs K knots)

   `from`/`to` name the states either side. Write `free` for either to fit
   that endpoint instead -- for a series with no bracketing state, or when
   where the sample finished is itself the measurement.

   `nrw tnr assess` names the right one in its verdict -- read
   `assessments/*/[label]_assessment.json` if it has been run. An oscillatory
   template means a THICKNESS change; one-sign means an SLD contrast change,
   and `paths` should say which.

6. Check your work:
     nrw data features <one of the data files>   # critical edge -> an SLD step
     nrw model validate {spec_path}
     nrw model preview {spec_path} --build       # initial chi-squared

   The step is the SLD of the medium that reflects the beam totally, less that
   of the medium the beam enters through: through the substrate, add the
   substrate's SLD before comparing it with the model.

7. If the notes do not say what a layer is made of, leave a TODO comment rather
   than inventing a material.
"""


def extract_json(reply: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a model's reply, fenced or bare.

    Used by ``nrw model new --from-notes`` and ``nrw assess``, so a reply that
    wraps its object in a fence or a sentence of preamble is read the same way
    by both.

    Args:
        reply: The reply text.

    Returns:
        The object, or ``None`` when the reply holds none.
    """
    text = reply.strip()

    # Fenced blocks by splitting on the fence, not by a lazy regex: a reply of
    # many unclosed fences made that take quadratic time.
    for block in text.split("```")[1::2]:
        body = block.strip()
        if body[:4].lower() == "json":
            body = body[4:].strip()
        if body.startswith("{"):
            text = body
            break

    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except ValueError:
        pass

    # Fall back to the outermost braces, which handles a reply with a sentence
    # of preamble in front of the object.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        value = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None
