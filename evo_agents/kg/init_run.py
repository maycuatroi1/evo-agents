"""What a ``kg_init`` run is made of, on the hub and on a machine alike: when a project's profile changed enough to ask
the Curator again, the prompt the Curator reads, and the decision it gives back.

A run looks at the data profile of a project's graph (``evo_agents.kg.profile``), which is counts and shapes and so
reaches a prompt whatever the label of the items it describes, the coverage of the build, the strategies it may choose,
and the figures of the starter questions asked by words and, when the graph has vectors, by meaning. The Curator
decides and applies without waiting for a person. Its decision is one JSON object (``parse_decision``):

- ``strategy``: ``{"use": NAME}`` for a built-in or custom strategy, or ``{"create": {...}}`` for a strategy file of its
  own (``evo_agents.kg.strategy``: knowledge parts, settings, steps) that is written and applied;
- ``settings``: KG settings keys on top of the strategy's (``evo_agents.kg.settings``);
- ``sources``: sources to add to knowledge.yaml (an id, a connector and its keys; never a label looser than the
  project's);
- ``code_changes``: changes to the KG code, a dependency or the worker's runtime, each ``{"title", "why"}``, which go
  through the proposal flow and are never applied here;
- ``reasons``: one entry per decision, ``{"about", "why", "figures"}``, the figures being the numbers that justify it.

A project whose content lines are mostly prose (``mostly_prose``) starts with the entities step on (plan
kg-zip-to-answers, a3): its prompt says so, and a decision that leaves it with a strategy whose steps lack ``entities``
is refused unless a reason about the strategy or the settings cites ENTITY_FIGURES, the figure of the tune questions
with the step and without it, the one without it the higher.

A decision part without a reason that cites a figure is refused: the point of a run is that every choice says why and
by how much. Standard library only; the strategy and settings modules validate what they own.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path

SHARE = 0.10  # a language or data type is a major part of the project from this share of its content lines
PROSE_SHARE = 0.5  # a project is mostly prose from this share of its content lines
ENTITY_FIGURES = ("entities_on", "entities_off")  # the tune figure with the entities step and without it
HALF = 0.5  # the content lines changed by this share or more
NEW_PROFILE = "no profile of an earlier run"
PARTS = ("strategy", "settings", "sources", "code_changes")
KEYS = frozenset(PARTS) | {"reasons"}
MAX_SAMPLES = 40
MAX_SOURCES = 5


class DecisionError(ValueError):
    pass


# -- when to ask again --------------------------------------------------------------------------------------------


def major(profile: Mapping) -> set[str]:
    """What a profile says the project is made of at SHARE of its content lines: ``language:python``,
    ``text:vi`` (natural language, by characters: its lines are not counted) and ``class:prose``."""
    lines = int(profile.get("lines") or 0)
    found: set[str] = set()
    if lines:
        for name, v in (profile.get("languages") or {}).items():
            if int(v.get("lines") or 0) / lines >= SHARE:
                found.add(f"language:{name}")
        for name, v in (profile.get("classes") or {}).items():
            if int(v.get("lines") or 0) / lines >= SHARE:
                found.add(f"class:{name}")
    for name, share in ((profile.get("natural_language") or {}).get("share") or {}).items():
        if share >= SHARE:
            found.add(f"text:{name}")
    return found


def mostly_prose(profile: Mapping | None) -> bool:
    """Whether prose holds PROSE_SHARE or more of the profile's content lines."""
    lines = int((profile or {}).get("lines") or 0)
    prose = (((profile or {}).get("classes") or {}).get("prose") or {}).get("lines") or 0
    return bool(lines) and int(prose) / lines >= PROSE_SHARE


def changed(before: Mapping | None, after: Mapping | None) -> list[str]:
    """Why the profile ``after`` moved far enough from ``before``, the profile of the last run, to ask the Curator
    again: a source was added, something reached SHARE that had not, or the content lines changed by HALF or more. []
    when it did not, and when there is no ``after``; the whole profile counts as new without a ``before``."""
    if not after:
        return []
    if not before:
        return [NEW_PROFILE]
    why = []
    old = {s["id"] for s in before.get("sources") or []}
    added = sorted({s["id"] for s in after.get("sources") or []} - old)
    if added:
        why.append("sources added: " + ", ".join(added))
    reached = sorted(major(after) - major(before))
    if reached:
        why.append("now at least 10% of the content lines: " + ", ".join(reached))
    was, now = int(before.get("lines") or 0), int(after.get("lines") or 0)
    if was and abs(now - was) / was >= HALF:
        why.append(f"content lines went from {was} to {now}")
    elif not was and now:
        why.append(f"content lines went from 0 to {now}")
    return why


def suits(profile: Mapping, strategies: Mapping[str, object]) -> list[str]:
    """The names of the strategies whose ``suits`` classes are a major part of the profile, in the order given; every
    strategy that suits nothing in particular (empty ``suits``) is a candidate too."""
    classes = {k.split(":", 1)[1] for k in major(profile) if k.startswith("class:")}
    out = []
    for name, s in strategies.items():
        wanted = set(getattr(s, "suits", ()) or ())
        if not wanted or wanted & classes:
            out.append(name)
    return out


# -- the prompt ---------------------------------------------------------------------------------------------------


def cleared(samples: Iterable[tuple[str, object]], allows) -> list[str]:
    """The names of ``samples`` ((name, label) pairs) that ``allows(label)`` lets the run's sink see, at most
    MAX_SAMPLES: a name above the sink's clearance never reaches a prompt."""
    return [name for name, label in samples if allows(label)][:MAX_SAMPLES]


def _figure(found: Mapping | None) -> str:
    if not found:
        return "not measured"
    return (
        f"{found.get('n')} questions, hit@5 {found.get('hit@5')}%, tokens per hit {found.get('tokens_per_hit')}, "
        f"p95 {found.get('p95_ms')} ms"
    )


def prompt_lines(
    *,
    project: str,
    why: list[str],
    profile_text: str,
    coverage: Mapping | None,
    strategies: Iterable[Mapping],
    measures: Mapping | None,
    settings: Mapping,
    samples: Iterable[str] = (),
    tools: str = "",
    prose: bool = False,
) -> list[str]:
    """What the Curator reads of project ``project``. ``strategies`` are ``Strategy.to_json`` of the candidates;
    ``measures`` the figures {"current": {"words", "meaning", "candidates": {name: figures}}}; ``samples`` names of
    nodes the run's sink may see (the caller filtered them by label): nothing else of any item's text is here.
    ``prose``: the project is mostly prose (``mostly_prose``), so the entities step starts on."""
    lines = [
        f"# KG init of project {project}",
        "You are the Curator. Decide, for this project's knowledge graph, which strategy and settings fit the data, "
        "and apply them. Nobody reviews the decision first: each part needs the figures that justify it.",
        "Asked because: " + ("; ".join(why) or "a person asked for it") + ".",
        "",
        "## Data profile (counts and shapes, no content)",
        profile_text,
    ]
    cov = coverage or {}
    lines += [
        "",
        "## Coverage of the build",
        f"- errors {len(cov.get('errors') or [])}, warnings {len(cov.get('warnings') or [])}, dangling edges "
        f"{cov.get('dangling_edges', 0)}.",
    ]
    for w in (cov.get("warnings") or [])[:5]:
        lines.append(f"- warning: {str(w)[:200]}")
    lines += ["", "## Settings in effect", json.dumps(dict(settings), sort_keys=True), "", "## Strategies you may use"]
    for s in strategies:
        lines.append(
            f"- {s['name']} ({s.get('origin')}): {s.get('description')} suits {s.get('suits')}; settings {s.get('settings')}"
        )
    lines += ["", "## Figures of the starter questions"]
    m = measures or {}
    if not m:
        lines.append("- none: the project has no questions yet, so a choice rests on the profile alone.")
    else:
        lines.append(f"- current, by words: {_figure(m.get('words'))}")
        lines.append(
            f"- current, by meaning and words: {_figure(m.get('meaning')) if m.get('meaning') else 'no vectors'}"
        )
        for name, found in sorted((m.get("candidates") or {}).items()):
            lines.append(f"- candidate {name}: {_figure(found)}")
    if prose:
        lines += ["", ENTITIES_HELP]
    shown = list(samples)[:MAX_SAMPLES]
    if shown:
        lines += ["", "## Names of some nodes (only those your sink may see)"] + [f"- {s[:120]}" for s in shown]
    if tools:
        lines += ["", tools]
    lines += ["", DECISION_HELP]
    return lines


ENTITIES_HELP = (
    "## Entities and relations of prose\n"
    "The data is mostly prose: start with the entities step on, a strategy whose steps name entities: "
    "entity-graph-api on the hub, whose build calls the model itself, or entity-graph where Claude Code runs, which "
    "give a graph of entities of open types with descriptions and relations with keywords (as LightRAG), each with "
    "its source; multi-hop-api and multi-hop ask a closed set of kinds and relations instead. Turn it off "
    "only when the tune questions are answered worse with it: then cite, in the reason of the strategy or the "
    f"settings, the figures {ENTITY_FIGURES[0]} and {ENTITY_FIGURES[1]}, one measure of the tune questions with the "
    "step and without it. A decision that leaves the step off otherwise is refused."
)


DECISION_HELP = (
    "## Your decision\n"
    "Answer with one JSON object, in a ```json fence:\n"
    '{"strategy": {"use": "NAME"} or {"create": {strategy file as in docs/kg-strategies.md}}, '
    '"settings": {KG settings keys}, "sources": [{"id", "connector", ...}], '
    '"code_changes": [{"title", "why"}], '
    '"reasons": [{"about": "strategy|settings|sources|code_changes", "why": "...", "figures": {"name": number}}]}.\n'
    "Leave a part out to leave it as it is. Every part you give needs a reason whose figures are numbers from the "
    "profile or the measures above. A strategy of your own may add identifiers and ontology entries (knowledge) and "
    "turn on build steps; it never changes a source. A source you add must not be labelled looser than the project."
)


# -- the decision -------------------------------------------------------------------------------------------------


def extract_json(text: str) -> dict:
    """The JSON object in an agent's final message: the last fenced block, else the outermost braces."""
    marker = "```json"
    if marker in text:
        body = text.rsplit(marker, 1)[1].split("```", 1)[0]
    else:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            raise DecisionError("the answer holds no JSON object")
        body = text[start : end + 1]
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise DecisionError(f"the decision is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise DecisionError("the decision must be a JSON object")
    return data


def _numbers(figures) -> bool:
    return (
        isinstance(figures, dict)
        and bool(figures)
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in figures.values())
    )


def _strategy_problems(chosen) -> list[str]:
    from evo_agents.kg import strategy as kg_strategy

    if not isinstance(chosen, dict) or len(chosen) != 1 or next(iter(chosen)) not in ("use", "create"):
        return ['strategy must be {"use": NAME} or {"create": {...}}']
    if "use" in chosen and not isinstance(chosen["use"], str):
        return ["strategy.use must be a name"]
    if "create" in chosen:
        try:
            kg_strategy.parse(chosen["create"], origin="created by the Curator")
        except kg_strategy.StrategyError as exc:
            return [f"strategy.create: {exc}"]
    return []


def _settings_problems(settings) -> list[str]:
    from evo_agents.kg import settings as kg_settings

    problems = []
    try:
        kg_settings.of(settings)
    except kg_settings.SettingsError as exc:
        problems.append(f"settings: {exc}")
    if isinstance(settings, dict) and "strategy" in settings:
        problems.append("settings.strategy is set by the strategy part")
    return problems


def _source_problems(found) -> list[str]:
    if not isinstance(found, list) or len(found) > MAX_SOURCES:
        return [f"sources must be a list of at most {MAX_SOURCES} sources"]
    problems = []
    for i, s in enumerate(found):
        if not isinstance(s, dict) or not isinstance(s.get("id"), str) or not isinstance(s.get("connector"), str):
            problems.append(f"sources[{i}] needs an id and a connector")
        elif "label" in s:
            problems.append(f"sources[{i}] sets a label: a person does, a new source gets the strictest one")
    return problems


def _changes_ok(changes) -> bool:
    return isinstance(changes, list) and all(
        isinstance(c, dict) and isinstance(c.get("title"), str) and isinstance(c.get("why"), str) for c in changes
    )


def _reason_problems(out: dict) -> list[str]:
    """That each part given has a reason citing figures, and that every reason is well formed."""
    problems = []
    reasons = out.get("reasons")
    if reasons is not None and not isinstance(reasons, list):
        problems.append("reasons must be a list")
        reasons = None
    given = {p for p in PARTS if out.get(p)}
    covered = set()
    for i, r in enumerate(reasons or []):
        if not isinstance(r, dict) or not isinstance(r.get("why"), str) or not r["why"].strip():
            problems.append(f"reasons[{i}] needs a why")
        elif r.get("about") not in PARTS:
            problems.append(f"reasons[{i}].about must be one of {', '.join(PARTS)}")
        elif not _numbers(r.get("figures")):
            problems.append(f"reasons[{i}] must cite figures: numbers by name")
        else:
            covered.add(r["about"])
    return problems + [f"{p} has no reason that cites figures" for p in sorted(given - covered)]


def parse_decision(data, *, prose: bool = False, entities_now: bool = False, harness_root: Path | None = None) -> dict:
    """``data`` as a validated decision, or DecisionError naming every problem. Settings and strategies are checked by
    their own modules; here the shape, the sources and that each part has a reason with numbers. For a project that is
    mostly prose (``prose``), whose settings run the entities step or not (``entities_now``), a decision that leaves
    the step off needs the figures ENTITY_FIGURES (``entities_kept``); ``harness_root`` finds a custom strategy."""
    if isinstance(data, str):
        data = extract_json(data)
    if not isinstance(data, dict):
        raise DecisionError("the decision must be an object")
    problems = [f"unknown key {k!r}" for k in sorted(set(data) - KEYS)]
    out: dict = {k: data[k] for k in KEYS & set(data)}
    if out.get("strategy") is not None:
        problems += _strategy_problems(out["strategy"])
    if "settings" in out:
        problems += _settings_problems(out["settings"])
    if out.get("sources") is not None:
        problems += _source_problems(out["sources"])
    if out.get("code_changes") is not None and not _changes_ok(out["code_changes"]):
        problems.append("code_changes must be a list of {title, why}")
    problems += _reason_problems(out)
    if prose and not problems:
        problems += entities_kept(out, entities_now, harness_root)
    if problems:
        raise DecisionError("; ".join(problems))
    out.setdefault("reasons", [])
    return out


def entities_after(decision: Mapping, entities_now: bool, harness_root: Path | None = None) -> bool:
    """Whether the entities step runs once ``decision`` is applied to settings that run it or not (``entities_now``)."""
    from evo_agents.kg import strategy as kg_strategy

    on = entities_now
    chosen = decision.get("strategy")
    if isinstance(chosen, Mapping) and isinstance(chosen.get("use"), str):
        on = "entities" in kg_strategy.steps_of(chosen["use"], harness_root)
    elif isinstance(chosen, Mapping) and isinstance(chosen.get("create"), Mapping):
        on = "entities" in (chosen["create"].get("steps") or [])
    settings = decision.get("settings")
    if isinstance(settings, Mapping) and "entities" in settings and not settings["entities"]:
        on = False
    return on


def entities_kept(decision: Mapping, entities_now: bool, harness_root: Path | None = None) -> list[str]:
    """The problem of a decision for prose that leaves the entities step off without tune figures that show it worse
    (ENTITY_FIGURES, the one without the step the higher); none when the step stays on or the figures say so."""
    if entities_after(decision, entities_now, harness_root):
        return []
    on, off = ENTITY_FIGURES
    for r in decision.get("reasons") or []:
        found = r.get("figures") if isinstance(r, Mapping) and r.get("about") in ("strategy", "settings") else None
        if isinstance(found, Mapping) and all(_number(found.get(k)) for k in ENTITY_FIGURES):
            if found[off] > found[on]:
                return []
    return [
        "the data is mostly prose, so the entities step stays on (strategy entity-graph-api on the hub) unless the tune "
        f"questions are answered worse with it: cite {on} and {off} in a reason about the strategy or the settings, "
        f"{off} the higher"
    ]


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def figures(row: Mapping | None) -> dict | None:
    """The figures of the ``kg_search`` row of an evaluation (n, hit@5 in percent, tokens per hit, p95 in ms); None
    without a row that asked anything."""
    if not isinstance(row, Mapping) or not isinstance(row.get("n"), int) or row["n"] <= 0:
        return None

    def number(key: str, scale: float = 1.0):
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return round(float(value) * scale, 4)

    return {
        "n": row["n"],
        "hit@5": number("hit@5", 100.0),
        "tokens_per_hit": number("tokens_per_hit"),
        "p95_ms": number("p95_ms"),
    }


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def manifest(files: Mapping[str, Path]) -> list[dict]:
    """The input files of a run, each with its SHA-256: {"name", "path", "sha256"}."""
    return [{"name": name, "path": str(p), "sha256": digest(p)} for name, p in sorted(files.items())]
