"""The Curator on a bench set (``kg bench curator``): a kg_init run that sees one set's data profile and its tune numbers.

A set is configured like a new project. ``decide`` writes the inputs of a run of the lens ``kg_init`` (the text of the
curator skill's kg_init lens, the set's merged data profile, the strategies, and the tune scores of the candidate
configs), asks an agent runtime (``claude --print`` with no tools, or ``EVO_KG_INIT_AGENT``) for the decision JSON, and
keeps the inputs next to the decision, each with its SHA-256. Nothing of the holdout or test part is among them: not a
question, not an answer, not a score. ``assemble`` joins the run summaries of the candidates (one per config, from
``kg bench run --settings``) and the decisions into ``bench/results/curator.json`` (format in ``bench.compare``).

A bench run asks graphs that are already built, so it applies only the *query* keys of a decision (the BM25 weights).
The build keys (passages, vectors, the entities step) and the semantic mode are recorded as ``unapplied``: this bench
does not rebuild the graphs for them. Standard library and PyYAML only.
"""

from __future__ import annotations

import copy
import json
import math
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from evo_agents.kg import init_local, init_run
from evo_agents.kg import profile as kg_profile
from evo_agents.kg import settings as kg_settings
from evo_agents.kg import strategy as kg_strategy
from evo_agents.kg.bench import compare, corpus, graphs, memory
from evo_agents.kg.bench.datasets import BenchError
from evo_agents.kg.bench.tasks import HOLDOUT, TEST, TUNE

SKILL_ENV = "EVO_CURATOR_SKILL"
SKILL_FILES = ("references/kg.md", "references/lenses.md")
FORMAT = "evo-kg-bench-curator/1"
DECIDE_TRIES = 2


# -- the skill ------------------------------------------------------------------------------------------------------


def skill_dir(given: str | None = None) -> Path:
    """The curator skill: ``given``, else ``$EVO_CURATOR_SKILL``, else ~/.claude/skills/curator, else the checkout of
    agent-skills beside this one."""
    here = Path(__file__).resolve()
    options = [given, os.environ.get(SKILL_ENV), Path.home() / ".claude/skills/curator"]
    options += [p / "agent-skills/skills/curator" for p in here.parents[:6]]
    for option in options:
        if option and (Path(option) / SKILL_FILES[0]).is_file():
            return Path(option)
    raise BenchError(f"no curator skill: pass --skill DIR or set {SKILL_ENV} (agent-skills, skills/curator)")


def _section(text: str, head: str) -> str:
    """The part of a markdown ``text`` from the heading line ``head`` to the next heading of the same level."""
    lines = text.splitlines()
    level = len(head) - len(head.lstrip("#"))
    start = next((i for i, line in enumerate(lines) if line.strip() == head), None)
    if start is None:
        raise BenchError(f"the skill has no section {head!r}")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("#") and len(lines[i]) - len(lines[i].lstrip("#")) <= level:
            end = i
            break
    return "\n".join(lines[start:end]).strip() + "\n"


def skill_texts(directory: Path) -> dict[str, str]:
    """The kg_init lens of the skill, in the two files that hold it."""
    kg = (directory / SKILL_FILES[0]).read_text(encoding="utf-8")
    lenses = (directory / SKILL_FILES[1]).read_text(encoding="utf-8")
    return {
        "skill-kg-init.md": _section(kg, "## Configuring a new graph (the kg_init lens)"),
        "skill-lenses-kg-init.md": _section(lenses, "## kg_init"),
    }


# -- the profile of a set -------------------------------------------------------------------------------------------


def _add(into: dict, other: dict) -> None:
    for k, v in other.items():
        into[k] = into.get(k, 0) + v


def merge_profiles(profiles: list[dict]) -> dict:
    """One profile from those of several graphs of a set: counts add up, shares and ratios are worked out again."""
    if not profiles:
        raise BenchError("no graph with a profile: build the set first (kg bench build)")
    total = {k: sum(p[k] for p in profiles) for k in ("items", "content_items", "lines", "chars", "edges")}
    nodes: dict = {}
    classes: dict = {}
    languages: dict = {}
    natural: dict = {}
    shapes: dict = {}
    sensitive = {k: {"strings": 0, "items": 0} for k in ("email", "phone", "national_id", "medical_code")}
    mentions = {"mentions": 0, "resolved": 0, "dangling": 0}
    sources: dict = {}
    binary = tiny = passages = with_passages = 0
    weights = {"p50": 0.0, "p90": 0.0, "p99": 0.0}
    longest = 0
    for p in profiles:
        _add(nodes, p["nodes"])
        for name, c in p["classes"].items():
            _add(classes.setdefault(name, {}), {k: c[k] for k in ("items", "lines", "chars")})
        for name, v in p["languages"].items():
            _add(languages.setdefault(name, {}), {k: v[k] for k in ("files", "lines", "calls", "inherits")})
        _add(natural, p["natural_language"]["chars"])
        for c in p["identifier_candidates"]:
            row = shapes.setdefault(c["shape"], {"shape": c["shape"], "pattern": c["pattern"], "count": 0, "items": 0})
            row["count"] += c["count"]
            row["items"] += c["items"]
        for k, v in p["sensitive"].items():
            _add(sensitive[k], v)
        _add(mentions, {k: p["mentions"][k] for k in mentions})
        binary += p["noise"]["binary"]
        tiny += p["noise"]["tiny"]
        n = p["length"]
        passages += n["passages"]
        with_passages += n["nodes_with_passages"]
        longest = max(longest, n["max"])
        for k in weights:
            weights[k] += n[k] * p["content_items"]
        for s in p["sources"]:
            row = sources.setdefault(s["id"], {"id": s["id"], "items": 0, "lines": 0, "chars": 0})
            _add(row, {k: s[k] for k in ("items", "lines", "chars")})
            if s.get("label"):
                row["label"] = s["label"]

    def ratio(a, b):
        return round(a / b, 4) if b else 0.0

    content = max(total["content_items"], 1)
    text_chars = sum(natural.values())
    commits = [p["history"] for p in profiles]
    history = {
        "commits": max(h.get("commits", 0) for h in commits),
        "pull_requests": max(h.get("pull_requests", 0) for h in commits),
    }
    if any(h.get("first") for h in commits):
        history["first"] = min(h["first"] for h in commits if h.get("first"))
        history["last"] = max(h["last"] for h in commits if h.get("last"))
    return {
        "version": profiles[0].get("version", kg_profile.PROFILE_VERSION),
        "graphs": len(profiles),
        **total,
        "nodes": dict(sorted(nodes.items())),
        "classes": {
            c: {**v, "share": ratio(v["chars"], total["chars"]), "item_share": ratio(v["items"], total["items"])}
            for c, v in sorted(classes.items(), key=lambda kv: -kv[1]["chars"])
        },
        "languages": {
            name: {
                **v,
                "calls_ratio": ratio(v["calls"], v["files"]),
                "inherits_ratio": ratio(v["inherits"], v["files"]),
            }
            for name, v in sorted(languages.items(), key=lambda kv: (-kv[1]["lines"], kv[0]))
        },
        "length": {
            **{k: int(v / content) for k, v in weights.items()},
            "max": longest,
            "passages": passages,
            "nodes_with_passages": with_passages,
        },
        "natural_language": {
            "chars": dict(sorted(natural.items())),
            "share": {k: ratio(v, text_chars) for k, v in sorted(natural.items())},
        },
        "identifier_candidates": sorted(shapes.values(), key=lambda c: (-c["count"], c["shape"]))[
            : kg_profile.TOP_SHAPES
        ],
        "sensitive": sensitive,
        "mentions": {**mentions, "resolved_ratio": ratio(mentions["resolved"], mentions["mentions"])},
        "noise": {"binary": binary, "tiny": tiny, "ratio": ratio(binary + tiny, total["items"])},
        "history": history,
        "sources": sorted(sources.values(), key=lambda s: s["id"]),
    }


def set_graphs(cache: Path, bench: str, ids: set[str] | None = None) -> list[Path]:
    """The graph directories of ``bench`` in the cache: of the tasks whose ids are in ``ids`` (all when None) for a
    code set, the one graph of a corpus set, the graph of each case of a memory set."""
    lay = graphs.layout(cache)
    if corpus.is_corpus(bench):
        docs, _ = corpus.load(cache, bench)
        return [lay.project_dir(corpus.stand_in(bench, docs))]
    if memory.is_memory(bench):
        cases, _ = memory.select(cache, bench, memory.Options(parts=(TUNE, HOLDOUT)))
        return [
            lay.project_dir(memory.stand_in(c))
            for c in cases
            if ids is None or c["id"] in ids or any(q.get("id") in ids for q in c.get("questions") or [])
        ]
    from evo_agents.kg.bench.tasks import load_tasks

    found, _ = load_tasks(cache, bench)
    return [lay.project_dir(t) for t in found if ids is None or t.id in ids]


def set_profile(cache: Path, bench: str, ids: set[str] | None = None) -> dict:
    """The merged data profile of the graphs of ``bench`` that exist (see ``set_graphs``)."""
    from evo_agents.kg.store import StaleSchema, Store

    found = []
    for project in dict.fromkeys(set_graphs(cache, bench, ids)):
        if not (project / "graph.sqlite").is_file():
            continue
        try:
            store = Store.open_file(project / "graph.sqlite")
        except (StaleSchema, OSError):
            continue
        try:
            one = kg_profile.of_coverage((store.summary() or {}).get("coverage"))
        finally:
            store.close()
        if one:
            found.append(one)
    return merge_profiles(found)


# -- the candidates and the tune scores -------------------------------------------------------------------------------

WEIGHT_VARIANTS = {
    "weights-body-heavy": {"bm25_body": 4.0},
    "weights-name-light": {"bm25_name": 2.0, "bm25_path": 1.0},
}


def candidate_plan(code: bool) -> list[dict]:
    """The configs every set is ranked on besides the Curator's: the release default, the query keys of each built-in
    strategy that has any, two weight variants and, on the code sets, plain BM25. Each is {name, kind, search,
    settings}; the settings are what ``kg bench run --settings`` takes."""
    out = [{"name": "default", "kind": "default", "search": "graph", "settings": {}}]
    seen = [{}]
    for name, s in sorted(kg_strategy.builtins().items()):
        query = {k: v for k, v in s.settings.items() if k not in kg_settings.BUILD_KEYS | kg_settings.SEMANTIC_KEYS}
        query = kg_settings.of(query).overrides() if query else {}
        if query in seen:
            continue
        seen.append(query)
        out.append({"name": f"strategy-{name}", "kind": "strategy", "search": "graph", "settings": query})
    for name, query in WEIGHT_VARIANTS.items():
        out.append({"name": name, "kind": "weights", "search": "graph", "settings": dict(query)})
    if code:
        out.append({"name": "plain-bm25", "kind": "bm25", "search": "plain", "settings": {}})
    return out


def _records(summary: dict, bench: str, part: str) -> list[dict]:
    method = summary["benches"][bench].get("method", "kg_search")
    return [
        r
        for r in summary.get("tasks", [])
        if r["bench"] == bench and r["part"] == part and r["method"] == method and r.get("skipped") is None
    ]


def part_scores(summary: dict, bench: str, part: str) -> dict:
    """{n, primary, se} of a bench's part in a run summary (se: of the mean, from the tasks)."""
    metric = summary["benches"][bench]["primary"]
    values = [r["metrics"][metric] for r in _records(summary, bench, part) if metric in r["metrics"]]
    n = len(values)
    if not n:
        return {"n": 0, "primary": None, "se": None}
    mean = sum(values) / n
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / max(n - 1, 1))
    return {"n": n, "primary": round(mean, 4), "se": round(sd / math.sqrt(n), 4)}


def tune_table(bench: str, candidates: list[dict]) -> dict:
    """The tune scores the Curator reads: {metric, candidates: [{name, kind, settings, n, primary, se}]}, from
    candidates that carry their run ``summary``. Only the tune part is read."""
    rows = []
    metric = None
    for c in candidates:
        metric = c["summary"]["benches"][bench]["primary"]
        rows.append({k: c[k] for k in ("name", "kind", "search", "settings")} | part_scores(c["summary"], bench, TUNE))
    return {"bench": bench, "metric": metric, "part": TUNE, "candidates": rows}


# -- the prompt ---------------------------------------------------------------------------------------------------


def _scores_lines(table: dict) -> list[str]:
    out = [
        f"Primary metric: {table['metric']} (higher is better), on the tune part only. se is the standard error of the "
        "mean over the questions; a difference under two standard errors is noise.",
        "",
    ]
    for r in table["candidates"]:
        score = "not measured" if r["primary"] is None else f"{r['primary']} (n {r['n']}, se {r['se']})"
        how = "plain BM25 search, not a setting you can choose" if r["search"] == "plain" else json.dumps(r["settings"])
        out.append(f"- {r['name']}: tune {score}; settings {how}")
    return out


def prompt_text(bench: str, profile: dict, table: dict, strategies: list[dict], texts: dict[str, str]) -> str:
    lines = [
        f"# KG init of bench set {bench}",
        "You are the Curator, running the lens kg_init. The set below is a new project: it has a built graph, a data "
        "profile and a tune part of questions, and nothing else. Nobody answers questions during the run; decide.",
        "",
        "## What you are asked in this run",
        "- Choose the strategy (one of the list, or a strategy of your own) and the KG settings for this project.",
        "- The graph is already built with the release defaults, and this run measures only the query keys "
        "(bm25_name, bm25_path, bm25_body, semantic_weight) of a decision. Build keys (passage_from, passage_chars, "
        "embed_chars, the entities step) and the semantic mode are recorded but not applied here, so give a reason "
        "from the figures for whichever you set. There are no vectors in this graph.",
        "- Use only the figures below. There is no holdout or test question, answer or score in this run, and you "
        "must not guess one. A choice the tune figures do not support is a choice to keep the default.",
        "- Cost: no model step, no entities step, no code change is wanted unless the figures show the need.",
        "",
        "## The skill text of the lens (agent-skills, skills/curator)",
        "",
    ]
    for name in sorted(texts):
        lines += [texts[name].rstrip(), ""]
    lines += [
        "## Data profile of the set (counts and shapes, no content)",
        kg_profile.render(profile),
        "",
        "## Strategies you may use",
    ]
    for s in strategies:
        lines.append(
            f"- {s['name']} ({s.get('origin')}): {s.get('description')} suits {s.get('suits')}; settings {s.get('settings')}"
        )
    lines += ["", "## Figures of the tune questions"]
    if all(r["primary"] is None for r in table["candidates"]):
        lines += ["- none: this set has no tune questions, so a choice rests on the profile alone."]
    else:
        lines += _scores_lines(table)
    lines += ["", init_run.DECISION_HELP]
    return "\n".join(lines) + "\n"


# -- the run ----------------------------------------------------------------------------------------------------


def ask_curator(prompt: str) -> tuple[str, float | None]:
    """(the Curator's answer, its cost in USD when the runtime says): the agent runtime of this machine."""
    argv = init_local.agent_argv()
    try:
        done = subprocess.run(
            argv, input=prompt, capture_output=True, text=True, timeout=init_local.AGENT_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BenchError(f"the agent runtime did not answer: {type(exc).__name__}: {exc}") from None
    if done.returncode != 0:
        raise BenchError(f"the agent runtime exited {done.returncode}: {(done.stderr or done.stdout).strip()[-300:]}")
    if argv[0] == "claude":
        try:
            data = json.loads(done.stdout)
        except ValueError:
            return done.stdout, None
        if isinstance(data, dict) and isinstance(data.get("result"), str):
            cost = data.get("total_cost_usd")
            return data["result"], float(cost) if isinstance(cost, (int, float)) else None
    return done.stdout, None


def resolve(decision: dict, pool: dict[str, kg_strategy.Strategy]) -> dict:
    """The config a decision stands for in a bench: the strategy (name, whether the Curator made it), the settings
    as decided, the query keys a bench run applies (the strategy's, then the decided ones over them) and the keys it
    cannot apply."""
    chosen = decision.get("strategy") or {}
    if "create" in chosen:
        used = kg_strategy.parse(chosen["create"], origin="created by the Curator")
        made = True
    elif "use" in chosen:
        if chosen["use"] not in pool:
            raise BenchError(f"the decision uses a strategy that does not exist: {chosen['use']!r}")
        used, made = pool[chosen["use"]], False
    else:
        used, made = None, False
    merged = dict(used.settings) if used else {}
    merged.update(decision.get("settings") or {})
    wanted = kg_settings.of(merged)
    fixed = kg_settings.BUILD_KEYS | kg_settings.SEMANTIC_KEYS
    unapplied = {k: v for k, v in wanted.overrides().items() if k in fixed and k != "strategy"}
    applied = {k: v for k, v in wanted.overrides().items() if k not in fixed}
    return {
        "strategy": {
            "name": used.name if used else None,
            "self_made": made,
            "origin": used.origin if used else None,
            "steps": list(used.steps) if used else [],
            **({"file": chosen["create"]} if made else {}),
        },
        "settings": merged,
        "applied": applied,
        "unapplied": unapplied,
    }


def write_inputs(out: Path, files: dict[str, str]) -> list[dict]:
    """Write the input files of a run under ``out`` and list them: {name, path (relative to out's parent), sha256,
    part}. None of them is of holdout or test."""
    out.mkdir(parents=True, exist_ok=True)
    listed = []
    for name, text in sorted(files.items()):
        path = out / name
        path.write_text(text, encoding="utf-8")
        listed.append(
            {
                "name": name,
                "path": f"{out.name}/{name}",
                "sha256": init_run.digest(path).removeprefix("sha256:"),
                "part": TUNE if name == "tune-scores.json" else "none",
            }
        )
    return listed


def decide(
    cache: Path,
    bench: str,
    candidates: list[dict],
    out: Path,
    *,
    skill: str | None = None,
    ask: Callable[[str], tuple[str, float | None]] | None = None,
    ids: set[str] | None = None,
) -> dict:
    """The Curator's decision for ``bench`` and the record of the run, written under ``out`` (``decision.json``).
    ``candidates`` carry the run summaries of the tune part; ``ids`` limits the graphs the profile reads."""
    for c in candidates:
        extra = {r["part"] for r in c["summary"].get("tasks", []) if r["bench"] == bench} - {TUNE}
        if extra:
            raise BenchError(f"{c['name']}: the tune scores hold {', '.join(sorted(extra))}: run them with --part tune")
    profile = set_profile(cache, bench, ids)
    table = tune_table(bench, candidates)
    pool = kg_strategy.builtins()
    strategies = [pool[n].to_json() for n in sorted(pool)]
    texts = skill_texts(skill_dir(skill))
    prompt = prompt_text(bench, profile, table, strategies, texts)
    files = {
        **texts,
        "profile.json": json.dumps(profile, sort_keys=True, indent=1) + "\n",
        "strategies.json": json.dumps(strategies, sort_keys=True, indent=1) + "\n",
        "tune-scores.json": json.dumps(table, sort_keys=True, indent=1) + "\n",
        "prompt.md": prompt,
    }
    inputs = write_inputs(out, files)
    record: dict = {
        "format": FORMAT,
        "bench": bench,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "inputs": inputs,
        "profile_graphs": profile["graphs"],
        "tune": table,
        "answers": [],
        "cost_usd": 0.0,
        "decision": None,
        "config": None,
        "error": None,
    }
    sent = prompt
    for _ in range(DECIDE_TRIES):
        answer, cost = (ask or ask_curator)(sent)
        record["answers"].append(answer)
        record["cost_usd"] = round(record["cost_usd"] + (cost or 0.0), 5)
        try:
            record["decision"] = init_run.parse_decision(answer)
            record["config"] = resolve(record["decision"], pool)
            record["error"] = None
            break
        except (init_run.DecisionError, kg_strategy.StrategyError, kg_settings.SettingsError, BenchError) as exc:
            record["error"] = str(exc)
            sent = prompt + f"\n## Your last answer was refused\n{exc}\nAnswer again with a valid decision.\n"
    (out / "decision.json").write_text(json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return record


# -- the file ---------------------------------------------------------------------------------------------------------


def _candidate_entry(c: dict, bench: str, split: str) -> dict:
    metric = c["summary"]["benches"][bench]["primary"]
    held = _records(c["summary"], bench, split)
    tasks = [
        {"id": r["id"], "v": r["metrics"][metric], "tokens": r.get("tokens", 0)} for r in held if metric in r["metrics"]
    ]
    return {
        "name": c["name"],
        "kind": c["kind"],
        "search": c["search"],
        "settings": c["settings"],
        "parts": {
            TUNE: part_scores(c["summary"], bench, TUNE),
            split: {**part_scores(c["summary"], bench, split), "tasks": tasks},
        },
    }


def assemble(
    plan: dict[str, dict],
    *,
    split: str = HOLDOUT,
    note: str = "",
    sampling: str = "",
) -> dict:
    """The Curator file. ``plan`` maps a bench to {"chosen": candidate name, "candidates": [{name, kind, search,
    settings, summary}], "record": the ``decide`` record or None (the release defaults, no Curator)}. The summary at
    the top is that of each bench's chosen candidate; the ``curator`` block lists every candidate."""
    if split in (TUNE, TEST):
        raise BenchError(f"the file ranks the configs on {HOLDOUT}, not {split}")
    benches: dict = {}
    tasks: list[dict] = []
    block: dict = {}
    first = None
    spent = 0.0
    calls = 0
    for bench, entry in plan.items():
        by_name = {c["name"]: c for c in entry["candidates"]}
        if entry["chosen"] not in by_name:
            raise BenchError(f"{bench}: chosen {entry['chosen']!r} is not among the candidates")
        chosen = by_name[entry["chosen"]]
        first = first or chosen["summary"]
        benches[bench] = copy.deepcopy(chosen["summary"]["benches"][bench])
        tasks += [r for r in chosen["summary"]["tasks"] if r["bench"] == bench and r["part"] == split]
        record = entry.get("record")
        spent += (record or {}).get("cost_usd", 0.0)
        calls += len((record or {}).get("answers", []))
        config = (record or {}).get("config") or {
            "strategy": {"name": None, "self_made": False, "origin": None, "steps": []},
            "settings": {},
            "applied": {},
            "unapplied": {},
        }
        block[bench] = {
            "metric": benches[bench]["primary"],
            "chosen": {"candidate": entry["chosen"], **config},
            "reasons": ((record or {}).get("decision") or {}).get("reasons", []),
            "inputs": (record or {}).get("inputs", []),
            "profile_graphs": (record or {}).get("profile_graphs"),
            "candidates": [_candidate_entry(c, bench, split) for c in entry["candidates"]],
        }
    assert first is not None
    out = {
        **{k: first.get(k) for k in ("format", "evo_ak", "extractor")},
        "created_at": first.get("created_at"),
        "settings": {k: v for k, v in (first.get("settings") or {}).items() if k != "kg_settings"}
        | {"sets": list(plan)},
        "cost": {"usd": round(spent, 4), "model_calls": calls},
        "benches": benches,
        "tasks": tasks,
        "curator": {"format": FORMAT, "split": split, "note": note, "sampling": sampling, "benches": block},
    }
    return out


def combine(summaries: list[dict]) -> dict:
    """One run summary from several of the same config's runs on other parts of the same sets."""
    out = copy.deepcopy(summaries[0])
    for other in summaries[1:]:
        for bench, numbers in other["benches"].items():
            out["benches"].setdefault(bench, numbers)["parts"].update(numbers["parts"])
        out["tasks"] += other["tasks"]
    return out


# -- a work directory ---------------------------------------------------------------------------------------------
#
#   WORK/SET/plan.json            the configs to rank: [{name, kind, search, settings}]
#   WORK/SET/settings/NAME.json   the settings file of each, for kg bench run --settings
#   WORK/SET/tune/NAME.json       the run summaries on the tune part (what the Curator reads)
#   WORK/SET/holdout/NAME.json    the run summaries on the holdout part (what the file ranks them on)
#   WORK/SET/curator/             the inputs of the Curator's run, its answers and decision.json


def write_plan(work: Path, bench: str, code: bool) -> list[dict]:
    plan = candidate_plan(code)
    root = work / bench
    (root / "settings").mkdir(parents=True, exist_ok=True)
    (root / "plan.json").write_text(json.dumps(plan, indent=1) + "\n", encoding="utf-8")
    for c in plan:
        (root / "settings" / f"{c['name']}.json").write_text(json.dumps(c["settings"]) + "\n", encoding="utf-8")
    return plan


def read_plan(work: Path, bench: str) -> list[dict]:
    try:
        return json.loads((work / bench / "plan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchError(f"no plan for {bench} in {work}: {exc}") from None


def read_runs(work: Path, bench: str, part: str, plan: list[dict]) -> list[dict]:
    """``plan`` with the summary of each config's run on ``part``; the configs with a summary only."""
    out = []
    for c in plan:
        path = work / bench / part / f"{c['name']}.json"
        if path.is_file():
            out.append({**c, "summary": compare.load_summary(path)})
    return out


def decide_from(work: Path, cache: Path, bench: str, *, skill: str | None = None, ask=None) -> dict:
    """``decide`` on the tune summaries in ``work``; the profile reads the graphs of the tune tasks (all the set's
    graphs when it has no tune task, as LocBench has none)."""
    found = read_runs(work, bench, TUNE, read_plan(work, bench))
    if not found:
        raise BenchError(f"no tune summary of {bench} in {work}/{bench}/tune")
    ids = {r["id"] for r in _records(found[0]["summary"], bench, TUNE)} or None
    return decide(cache, bench, found, work / bench / "curator", skill=skill, ask=ask, ids=ids)


def from_work(work: Path, benches: list[str], out_dir: Path, *, curator: bool, note: str, sampling: str) -> dict:
    """The Curator file of ``benches``: the Curator's config is the candidate ``curator`` (settings: what its
    decision applies), or with ``curator`` false the release default, which ranks the origin/main. The inputs of each
    run are copied to ``out_dir/curator-inputs/SET`` and listed with paths relative to ``out_dir``."""
    plan: dict = {}
    for bench in benches:
        configs = read_plan(work, bench)
        record = None
        if curator:
            record = json.loads((work / bench / "curator" / "decision.json").read_text(encoding="utf-8"))
            if not record.get("config"):
                raise BenchError(f"{bench}: the Curator gave no valid decision: {record.get('error')}")
            configs = configs + [
                {
                    "name": "curator",
                    "kind": "curator",
                    "search": "graph",
                    "settings": record["config"]["applied"],
                }
            ]
            target = out_dir / "curator-inputs" / bench
            target.mkdir(parents=True, exist_ok=True)
            for item in record["inputs"]:
                shutil.copy2(work / bench / "curator" / item["name"], target / item["name"])
                item["path"] = f"curator-inputs/{bench}/{item['name']}"
            (target / "decision.json").write_text(
                json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        candidates = []
        for c in configs:
            parts = [r for part in (TUNE, HOLDOUT) for r in read_runs(work, bench, part, [c])]
            if not any(HOLDOUT in {t["part"] for t in r["summary"]["tasks"]} for r in parts):
                raise BenchError(f"{bench}: no holdout run of {c['name']} in {work}/{bench}/holdout")
            candidates.append({**c, "summary": combine([r["summary"] for r in parts])})
        plan[bench] = {"chosen": "curator" if curator else "default", "candidates": candidates, "record": record}
    return assemble(plan, note=note, sampling=sampling)
