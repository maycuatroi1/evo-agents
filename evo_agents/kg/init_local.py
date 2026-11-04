"""``evo-agents kg init``: the kg_init run of a project on this machine, without the hub.

It does what the hub's run does (``evo_agents.hub.kg_init``): reads the data profile of the project's graph, measures
the strategies that suit it and the current settings on the starter questions, gives the Curator the profile, the
coverage, the strategies and the figures, and applies what the Curator decides to the local settings, knowledge.yaml, the
strategy files and the ontology, then builds the graph again.

- The questions are the tune part of ``--questions FILE``, else the commit questions of the graph
  (``evo_agents.kg.eval_commits``). The holdout part of a file is never read into the run: the input files the run
  keeps are the tune questions, the profile, the coverage, the strategies and the prompt.
- The Curator is an agent runtime on this machine: Claude Code (``claude --print``) or Codex (``codex exec``), or the
  command argv in ``EVO_KG_INIT_AGENT`` (it reads the prompt on stdin and writes its answer on stdout). It runs with no
  tools: everything it needs is in the prompt, and the prompt holds counts and shapes, never an item's text.
- ``kg-init.json`` in the project's directory records the run: why it was asked, the input files with their SHA-256,
  the figures, the decision with its reasons, what was applied and the build that followed.

A decision that does not parse, or a part that cannot be applied, changes nothing it has not already changed and is
recorded as it is. Measuring a strategy asks the questions of the graph already built with the strategy's query keys;
its build keys (the passages, the vectors, the steps) take effect with the build the run ends with.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import yaml

from evo_agents.kg import init_run
from evo_agents.kg import profile as kg_profile
from evo_agents.kg import settings as kg_settings
from evo_agents.kg import strategy as kg_strategy

FILE = "kg-init.json"
INPUT_DIR = "kg-init"
AGENT_ENV = "EVO_KG_INIT_AGENT"
AGENT_TIMEOUT = 900
MAX_QUESTIONS = 300
SINK = "cli"


class InitError(Exception):
    pass


def agent_argv() -> list[str]:
    """The command of the agent runtime on this machine that gives the Curator's answer."""
    custom = os.environ.get(AGENT_ENV)
    if custom:
        try:
            argv = json.loads(custom)
        except ValueError:
            argv = None
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise InitError(f"{AGENT_ENV} must be a JSON list of strings")
        return argv
    if shutil.which("claude"):
        return [
            "claude", "--print", "--output-format", "json", "--max-turns", "1", "--tools", "", "--strict-mcp-config",
            "--disable-slash-commands", "--no-session-persistence",
        ]  # fmt: skip
    if shutil.which("codex"):
        return ["codex", "exec", "--skip-git-repo-check", "-"]
    raise InitError(f"no agent runtime on this machine (claude or codex), and {AGENT_ENV} is not set")


def ask_agent(prompt: str) -> str:
    """The text the runtime answers ``prompt`` with."""
    argv = agent_argv()
    try:
        done = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=AGENT_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise InitError(f"the agent runtime did not answer: {type(exc).__name__}: {exc}") from None
    if done.returncode != 0:
        raise InitError(f"the agent runtime exited {done.returncode}: {(done.stderr or done.stdout).strip()[-300:]}")
    out = done.stdout
    if argv[0] == "claude":  # --output-format json wraps the answer
        try:
            data = json.loads(out)
        except ValueError:
            return out
        if isinstance(data, dict) and isinstance(data.get("result"), str):
            return data["result"]
    return out


# -- what the run reads -------------------------------------------------------------------------------------------


def _latest(project):
    from evo_agents.kg.store import Store

    store = Store.open_readonly(project)
    if store is None or store.latest_ready() is None:
        if store is not None:
            store.close()
        return None, None
    return store, store.latest_ready()


def _tune_questions(project, store, path: Path | None) -> tuple[list, str]:
    """(the tune questions, where they came from): the tune part of ``path``, else the commit questions."""
    from evo_agents.kg import eval_commits
    from evo_agents.kg.evaluate import TUNE, QuestionError, load_set, parse_questions

    if path is not None:
        try:
            found = load_set(path)
        except (QuestionError, OSError) as exc:
            raise InitError(f"{path}: {exc}") from None
        return found.part(TUNE)[:MAX_QUESTIONS], f"the tune part of {path}"
    made = eval_commits.commit_questions(store, limit=MAX_QUESTIONS).questions
    lines = [json.dumps(q, ensure_ascii=False) for q in made]
    if not lines:
        return [], "no commit questions: the graph has too few commits"
    return [q for q in parse_questions(lines) if q.split == TUNE], "the commit questions of the graph (tune part)"


def _dump(question) -> dict:
    out = {"id": question.id, "question": question.text, "type": question.type}
    if question.repo:
        out["repo"] = question.repo
    if question.files:
        out["files"] = [f"{r}:{p}" if r else p for r, p in question.files]
    if question.nodes:
        out["nodes"] = list(question.nodes)
    return out


def _measure(project, questions: list, overrides: dict | None, *, meaning: bool) -> dict | None:
    from evo_agents.kg.evaluate import evaluate, project_checkouts
    from evo_agents.kg.serve import make_session

    if not questions:
        return None

    def session_for(_project):
        session = make_session(project.name, SINK)
        session.semantic = meaning
        return session

    with kg_settings.overriding(overrides):
        report = evaluate(
            questions,
            session_for=session_for,
            checkouts_for=lambda p: project_checkouts(project.name),
            methods=["kg_search"],
        )
    row = next((r for r in report.rows if r["method"] == "kg_search" and r["type"] == "all"), None)
    return init_run.figures(row)


def _vectors(store, b) -> bool:
    try:
        return store.vector_info() is not None
    except Exception:  # a store without vectors may lack the table
        return False


def measures(project, store, b, questions: list, candidates: dict[str, kg_strategy.Strategy]) -> dict:
    """The figures of the current settings, by words and by meaning, and of each candidate strategy's query keys."""
    if not questions:
        return {}
    current = kg_settings.read(project.root)
    out: dict = {"questions": len(questions), "words": _measure(project, questions, None, meaning=False)}
    out["meaning"] = _measure(project, questions, None, meaning=True) if _vectors(store, b) else None
    found: dict[str, dict | None] = {}
    needs: dict[str, list[str]] = {}
    for name, s in candidates.items():
        try:
            wanted = kg_settings.of(dict(s.settings), current)
        except kg_settings.SettingsError:
            continue
        needs[name] = kg_settings.rebuild_keys(current, wanted)
        query_keys = {k: getattr(wanted, k) for k in kg_settings.KEYS if k not in kg_settings.BUILD_KEYS}
        found[name] = _measure(project, questions, query_keys, meaning=False)
    out["candidates"] = found
    out["needs_rebuild"] = needs
    return out


# -- what the run applies -----------------------------------------------------------------------------------------


def _figures_of(decision: dict, about: str) -> dict | None:
    for r in decision.get("reasons") or []:
        if r.get("about") == about:
            return r["figures"]
    return None


def _why_of(decision: dict, about: str) -> str:
    return "; ".join(r["why"] for r in decision.get("reasons") or [] if r.get("about") == about)


def apply_decision(project, decision: dict) -> dict:
    """Apply a validated decision to the project's files; what each part did, by part."""
    applied: dict = {}
    harness = project.harness.root
    chosen = decision.get("strategy")
    if chosen:
        if "create" in chosen:
            named = kg_strategy.parse(chosen["create"], origin="created by the Curator")
            path = Path(harness) / kg_strategy.CUSTOM_DIR / f"{named.name}.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(chosen["create"], sort_keys=False, allow_unicode=True), encoding="utf-8")
            use = kg_strategy.load_file(path)
        else:
            use = kg_strategy.find(chosen["use"], harness)
        found = kg_strategy.apply(use, project, reason=f"kg init: {_why_of(decision, 'strategy')}"[:500])
        applied["strategy"] = {
            "name": use.name,
            "origin": use.origin,
            "changed": found.changed,
            "needs_rebuild": found.needs_rebuild,
        }
    if decision.get("settings"):
        before = kg_settings.read(project.root)
        after = kg_settings.of(decision["settings"], before)
        kg_settings.write(
            project.root,
            after,
            reason=f"kg init: {_why_of(decision, 'settings')}"[:500],
            figures=_figures_of(decision, "settings"),
        )
        applied["settings"] = kg_settings.diff(before, after)
    if decision.get("sources"):
        applied["sources"] = _add_sources(project, decision["sources"])
    if decision.get("code_changes"):
        applied["code_changes"] = {
            "applied": False,
            "why": "a change to the KG code, a dependency or the runtime goes through a proposal on the hub",
            "items": decision["code_changes"],
        }
    return applied


def _add_sources(project, sources: list[dict]) -> dict:
    """Append the sources whose id the project lacks to knowledge.yaml (the file is copied to ``.before-init``); the
    file is put back when the project no longer loads."""
    from evo_agents.kg.project import ProjectError, load_project_at

    path = project.knowledge_path
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}
    have = {s.get("id") for s in data.get("sources") or []}
    new = [s for s in sources if s["id"] not in have]
    skipped = [s["id"] for s in sources if s["id"] in have]
    if new:
        shutil.copy2(path, path.with_name(path.name + ".before-init"))
        data["sources"] = (data.get("sources") or []) + new
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        try:
            load_project_at(project.harness.root, project.home)
        except ProjectError as exc:
            path.write_text(text, encoding="utf-8")
            raise InitError(f"the sources break knowledge.yaml and were not added: {exc}") from None
    return {"added": [s["id"] for s in new], "skipped_existing": skipped}


# -- the run ------------------------------------------------------------------------------------------------------


def _coverage_of(store, b) -> dict:
    return store.build_row(b)["coverage"]


def run(
    project,
    *,
    questions: Path | None = None,
    ask: Callable[[str], str] | None = None,
    why: list[str] | None = None,
    rebuild: bool = True,
) -> dict:
    """Run kg_init on ``project``; the record written to ``kg-init.json``. ``ask`` gives the Curator's answer to the
    prompt (the agent runtime of this machine by default)."""
    from evo_agents.kg.build import build_project
    from evo_agents.kg.project import load_project_at

    store, b = _latest(project)
    if store is None:
        report = build_project(project)
        if not report.ok:
            raise InitError("the project has no graph and its build failed: " + "; ".join(report.errors[:3]))
        store, b = _latest(project)
        if store is None:
            raise InitError("the build left no ready graph")
    try:
        coverage = _coverage_of(store, b)
        profile = kg_profile.of_coverage(coverage)
        if profile is None:
            raise InitError("the graph was built before data profiles: run evo-agents kg build --force first")
        tune, source = _tune_questions(project, store, questions)
        pool = kg_strategy.available(project.harness.root)
        names = init_run.suits(profile, pool)
        candidates = {n: pool[n] for n in names if pool[n].settings}
        found = measures(project, store, b, tune, candidates)
    finally:
        store.close()
    tuning = kg_settings.read(project.root)
    strategies = [pool[n].to_json() for n in pool]
    lines = init_run.prompt_lines(
        project=project.name,
        why=why or ["asked on this machine"],
        profile_text=kg_profile.render(profile),
        coverage=coverage,
        strategies=strategies,
        measures=found,
        settings=tuning.overrides(),
    )
    prompt = "\n".join(lines)
    inputs_dir = project.root / INPUT_DIR
    inputs_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "profile": inputs_dir / "profile.json",
        "coverage": inputs_dir / "coverage.json",
        "strategies": inputs_dir / "strategies.json",
        "questions": inputs_dir / "questions.tune.jsonl",
        "prompt": inputs_dir / "prompt.md",
    }
    files["profile"].write_text(json.dumps(profile, sort_keys=True, indent=1), encoding="utf-8")
    files["coverage"].write_text(json.dumps(coverage, sort_keys=True, indent=1, default=str), encoding="utf-8")
    files["strategies"].write_text(json.dumps(strategies, sort_keys=True, indent=1), encoding="utf-8")
    files["questions"].write_text(
        "".join(json.dumps(_dump(q), ensure_ascii=False) + "\n" for q in tune), encoding="utf-8"
    )
    files["prompt"].write_text(prompt, encoding="utf-8")
    record: dict = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "project": project.name,
        "why": why or ["asked on this machine"],
        "questions": {"source": source, "count": len(tune)},
        "inputs": init_run.manifest(files),
        "profile_summary": {
            "items": profile["items"],
            "lines": profile["lines"],
            "major": sorted(init_run.major(profile)),
        },
        "measures": found,
        "decision": None,
        "applied": None,
        "error": None,
        "build": None,
    }
    try:
        answer = (ask or ask_agent)(prompt)
        decision = init_run.parse_decision(answer)
        record["decision"] = decision
        record["applied"] = apply_decision(project, decision)
    except (InitError, init_run.DecisionError, kg_strategy.StrategyError, kg_settings.SettingsError) as exc:
        record["error"] = str(exc)
    if rebuild and record["error"] is None:
        after = load_project_at(project.harness.root, project.home)
        report = build_project(after)
        record["build"] = {
            "ok": report.ok,
            "build_id": report.build_id,
            "content_hash": report.content_hash,
            "errors": report.errors[:5],
        }
        if report.ok:
            record["applied_profile_digest"] = init_run.digest(files["profile"])
    (project.root / FILE).write_text(json.dumps(record, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return record
