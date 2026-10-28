"""The retrieval evaluation of a graph the hub built, scored as ``evo-agents kg eval`` scores one, for the measure of
the graph's health (``evo_agents.hub.server.kg_health``).

Two kinds of questions are asked. First the holdout part of the project's question set on the hub (kg_eval_sets,
``evo_agents.hub.server.kg_eval_sets``), its latest version, those of the project or of none: the part no ranking is
tuned on (docs/kg-eval.md). Then the questions ``evo-agents kg eval-questions`` makes of the done steps of the hub's
plans (``evo_agents.hub.kg_eval``), type ``plan-step``, whose ids the set does not hold: a step that is done and whose
evidence names commits or a pull request asks its title (``handoff.commits_of`` reads the hashes), and its answers are
the files its work changed, as ``repo:path``.

The hub has no checkout to ask git, so a commit's files are the files of the graph its ``changes`` edges lead to (the
paths of a commit item its source does not exclude, a merge's against its first parent; a file the branch deleted
since is no answer). A step whose commits were squashed names commits no branch of the graph holds; its answers then
come, in this order, from the pull requests of the graph that merged its branch (the ``on <branch>`` of its evidence)
or that its evidence cites (``#12``, ``PR 12``, ``pull/12``), through the commit each merged as; else from the paths
its evidence names that are files of the graph in its repo. A step is left out, with a reason (SKIP_REASONS), when it
names no repo, when none of that finds a file, or when it has no title. A step whose work changed many files stays:
capped Recall@5 and hit@5 score it fairly. At most MAX_QUESTIONS plan-step questions are asked, a sample fixed by the
hash of each question's id, so a measure asks the same ones as the one before plus those new steps add.

Each question goes to ``kg_search`` (METHODS) with the command's default limit, through a session that reads the graph
as the charter's owner does through the agent sink AGENT_SINK when the project's policy declares it (else its hub
sink); there is no grep baseline without a checkout. The score is the command's rows, over all the questions and per
type: hit@5, Recall@1 and @5, capped Recall@5, MRR, the share of empty answers, the answers per 1,000 tokens, the mean
tokens, the tokens per hit and per answer file, and the 95th percentile of the time of a call. Only reads the graph.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from evo_agents.hub.handoff import commits_of
from evo_agents.hub.plans import step_key
from evo_agents.kg.evaluate import (
    ALL_TYPES,
    DEFAULT_LIMIT,
    HOLDOUT,
    PLAN_STEP_TYPE,
    Question,
    QuestionSet,
    evaluate,
    id_file,
)

METHODS = ("kg_search",)
AGENT_SINK = "claude-code@anthropic"  # the sink the agents that read the graph go through
MAX_QUESTIONS = 300  # plan-step questions a measure asks
SHA_PREFIX = 7
MAX_EDGES = 500  # edges of a commit read: the answers of a commit of more files are its first MAX_EDGES
MAX_SKIPPED = 20  # reasons a measure keeps of the steps left out
MAX_EVIDENCE_PATHS = 50
SCORE_KEYS = (
    "n",
    "hit@5",
    "recall@1",
    "recall@5",
    "capped_recall@5",
    "mrr",
    "empty",
    "errors",
    "correct_per_1k_tokens",
    "mean_tokens",
    "tokens_per_hit",
    "tokens_per_file",
    "p95_ms",
)
# Why a done step with commits asks nothing, the keys of a score's skipped_by.
NO_REPO, NOT_IN_GRAPH, NO_FILE, NO_TITLE = "no_repo", "not_in_graph", "no_file", "no_title"
SKIP_REASONS = (NO_REPO, NOT_IN_GRAPH, NO_FILE, NO_TITLE)
# Where a plan-step question's answers came from, the keys of a score's answers_from.
FROM_COMMITS, FROM_PULLS, FROM_EVIDENCE = "commits", "pull_requests", "evidence_paths"

BRANCH = re.compile(r"\bon (?:branch )?`?([A-Za-z0-9][\w.\-/]*[\w\-])`?")
PULL = re.compile(r"(?:\bPR\s*#?|\bpull/|\bpulls/|(?<![\w&])#)(\d{1,7})\b", re.IGNORECASE)
PATH = re.compile(r"(?<![\w/.\-])(?:([A-Za-z0-9][\w.\-]*):)?((?:[\w.\-]+/)*[\w\-]+\.[A-Za-z0-9]{1,8})(?![\w/])")


@dataclass(frozen=True)
class Skipped:
    """A done step with commits that asks nothing: ``step`` (plan#step), ``reason`` (SKIP_REASONS) and why."""

    step: str
    reason: str
    why: str

    def __str__(self) -> str:
        return f"{self.step}: {self.why}"


class GraphCommits:
    """The commits a graph's git sources read, by the first SHA_PREFIX digits of their SHA: each with its node, its
    repo, its SHA and how many paths it changed; the files it changed are its ``changes`` edges. Also the graph's pull
    requests, with their branch and the commit each merged as, and whether a path is a file of the graph."""

    def __init__(self, store, b: int):
        self.store, self.b = store, b
        self.found: dict[str, list[dict]] = defaultdict(list)
        self.pulls: list[dict] = []
        alive = sorted(store.alive_ids("nodes", b))
        ids = [node for node in alive if ":commit:" in node and not node.startswith("commit:")]
        for node in store.nodes(ids, b).values():
            props = node.get("props") or {}
            sha, files = props.get("sha"), props.get("files")
            if node.get("kind") == "Commit" and isinstance(sha, str) and type(files) is int:
                entry = {"node": node["id"], "sha": sha, "repo": props.get("repo"), "files": files}
                self.found[sha[:SHA_PREFIX]].append(entry)
        for node in store.nodes([node for node in alive if ":pr:" in node], b).values():
            props = node.get("props") or {}
            if node.get("kind") == "PullRequest" and isinstance(props.get("merge_commit"), str):
                keep = ("repo", "number", "head", "merge_commit")
                self.pulls.append({key: props.get(key) for key in keep})

    def changed(self, sha: str, repo: str) -> tuple[list[str], int] | None:
        """(the files of the graph the one commit of ``repo`` whose SHA starts with ``sha`` changed, by path, and how
        many paths it changed, deleted ones included); None when the graph has no such commit, or two."""
        found = [e for e in self.found.get(sha[:SHA_PREFIX], []) if e["sha"].startswith(sha) and e["repo"] == repo]
        if len({e["sha"] for e in found}) != 1:
            return None
        edges = self.store.edges_of(found[0]["node"], self.b, "out", limit=MAX_EDGES)
        named = (id_file(edge["dst"]) for edge in edges if edge.get("rel") == "changes")
        return sorted({file[1] for file in named if file is not None}), found[0]["files"]

    def merged(self, repo: str, branch: str | None, numbers: Iterable[int]) -> list[str]:
        """The commits the merged pull requests of ``repo`` from ``branch``, or numbered ``numbers``, merged as."""
        wanted = set(numbers)
        return [
            p["merge_commit"]
            for p in sorted(self.pulls, key=lambda p: p.get("number") or 0)
            if p.get("repo") == repo and ((branch and p.get("head") == branch) or p.get("number") in wanted)
        ]

    def files(self, repo: str, paths: Iterable[str]) -> list[str]:
        """Those of ``paths`` that are files of the graph in ``repo``, in order."""
        wanted = list(dict.fromkeys(paths))
        held = self.store.nodes([f"{repo}:file:{path}" for path in wanted], self.b)
        return [path for path in wanted if f"{repo}:file:{path}" in held]


def _step_repo(body: dict, step: dict) -> str | None:
    if isinstance(step.get("repo"), str) and step["repo"]:
        return step["repo"]
    repos = [r.get("repo") for r in body.get("repos") or [] if isinstance(r, dict)]
    return repos[0] if len(repos) == 1 and isinstance(repos[0], str) else None


def evidence_branch(evidence: str) -> str | None:
    """The branch ``evidence`` says its commits are on (``repo@sha on feat/x``), if it names one."""
    found = BRANCH.search(evidence)
    return found.group(1) if found else None


def evidence_pulls(evidence: str) -> list[int]:
    return list(dict.fromkeys(int(n) for n in PULL.findall(evidence)))


def evidence_paths(evidence: str, repo: str) -> list[str]:
    """The paths ``evidence`` names, of ``repo`` or of no repo, at most MAX_EVIDENCE_PATHS."""
    found = [path.removeprefix("./") for named, path in PATH.findall(evidence) if named in ("", repo)]
    return list(dict.fromkeys(found))[:MAX_EVIDENCE_PATHS]


def _commit_files(commits: GraphCommits, shas: Iterable[str], repo: str) -> tuple[list[str], int]:
    files: list[str] = []
    named = 0
    for sha in shas:
        changed = commits.changed(sha, repo)
        if changed is not None:
            named += 1
            files += [path for path in changed[0] if path not in files]
    return files, named


def step_answers(commits: GraphCommits, evidence: str, repo: str) -> tuple[list[str], str | None, int]:
    """(the files a step's work changed, where they came from, how many of its commits the graph holds), by the
    order of the module: its commits, the pull requests that merged it, the paths its evidence names."""
    shas = commits_of(evidence)
    files, named = _commit_files(commits, shas, repo)
    if files:
        return files, FROM_COMMITS, named
    merges = [sha for sha in commits.merged(repo, evidence_branch(evidence), evidence_pulls(evidence)) if sha]
    found, _ = _commit_files(commits, merges, repo)
    if found:
        return found, FROM_PULLS, named
    found = commits.files(repo, evidence_paths(evidence, repo))
    return found, (FROM_EVIDENCE if found else None), named


def step_question(
    project: str, plan_id: str, body: dict, index: int, commits: GraphCommits
) -> tuple[Question | None, Skipped | str | None]:
    """The question step ``index`` of a plan gives and where its answers came from, or None and why it gives none
    (None, None: not done, or neither a commit nor a pull request in its evidence)."""
    step = body["steps"][index]
    if not isinstance(step, dict) or step.get("status") != "done":
        return None, None
    evidence = str(step.get("evidence") or "")
    shas = commits_of(evidence)
    if not shas and not evidence_pulls(evidence):
        return None, None
    name = f"{plan_id}#{step_key(step, index)}"
    repo = _step_repo(body, step)
    if repo is None:
        return None, Skipped(name, NO_REPO, "no repo named")
    files, source, named = step_answers(commits, evidence, repo)
    if not files:
        if not named:
            what = f"none of its {len(shas)} commit(s) is in the graph of {repo}" if shas else "no commit"
            why = f"{what}, and no pull request or path of its evidence gives a file"
            return None, Skipped(name, NOT_IN_GRAPH, why)
        return None, Skipped(name, NO_FILE, f"its commits change no file of the graph of {repo}")
    text = " ".join(str(step.get("title") or "").split())
    if not text:
        return None, Skipped(name, NO_TITLE, "no title")
    question = Question(name, text, PLAN_STEP_TYPE, project, repo, tuple((repo, path) for path in files))
    return question, source


def plan_questions(
    project: str, plans: Iterable[tuple[str, dict]], commits: GraphCommits, leave_out: Iterable[str] = ()
) -> tuple[list[Question], list[Skipped], int, Counter]:
    """The questions of ``plans`` ((plan id, body) each) but those whose id is in ``leave_out``, at most
    MAX_QUESTIONS of them by the hash of their id; why each step with commits gave none; how many questions there
    were before the cut; and how many of them each source of answers gave."""
    questions: list[Question] = []
    skipped: list[Skipped] = []
    sources: Counter = Counter()
    held = set(leave_out)
    for plan_id, body in plans:
        steps = body.get("steps") if isinstance(body, dict) and isinstance(body.get("steps"), list) else []
        for index in range(len(steps)):
            question, why = step_question(project, plan_id, body, index, commits)
            if question is not None and question.id not in held:
                questions.append(question)
                sources[why] += 1
            elif isinstance(why, Skipped):
                skipped.append(why)
    sample = sorted(questions, key=lambda q: hashlib.sha256(q.id.encode()).hexdigest())[:MAX_QUESTIONS]
    return sorted(sample, key=lambda q: q.id), skipped, len(questions), sources


def holdout_questions(question_set: QuestionSet | None, project: str) -> list[Question]:
    """The holdout questions of ``question_set`` asked of ``project``'s graph: those of the project or of none."""
    if question_set is None:
        return []
    return [q for q in question_set.part(HOLDOUT) if q.project in (None, project)]


def questions_digest(questions: list[Question]) -> str:
    """A digest of what ``questions`` ask and answer: the same set asked of the same graph scores the same."""
    data = [[q.id, q.text, q.type, q.repo, [list(a) for a in q.files], list(q.nodes)] for q in questions]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def score(
    questions: list[Question],
    skipped: list[Skipped],
    available: int,
    session,
    *,
    plan_steps: int | None = None,
    sources: Counter | None = None,
    question_set: dict | None = None,
    sink: str | None = None,
) -> dict:
    """The score of ``questions`` asked through ``session`` (a ``evo_agents.kg.serve.Session``): {types, questions,
    plan_steps, available (plan-step questions before the cut), skipped, skipped_by {reason: n}, dropped (the share of
    the plan steps with commits that ask nothing), reasons, answers_from, limit, sink, set, methods: {method: {n,
    hit@5, recall@1, recall@5, capped_recall@5, mrr, empty, errors, correct_per_1k_tokens, mean_tokens,
    tokens_per_hit, tokens_per_file, p95_ms}}, by_type: {type: {method: the same}}, digest}."""
    methods: dict = {}
    by_type: dict = {}
    if questions:
        report = evaluate(questions, session_for=lambda _: session, checkouts_for=lambda _: {}, methods=METHODS)
        for row in report.rows:
            numbers = {key: row.get(key) for key in SCORE_KEYS}
            if row["type"] == ALL_TYPES:
                methods[row["method"]] = numbers
            else:
                by_type.setdefault(row["type"], {})[row["method"]] = numbers
    steps = sum(q.type == PLAN_STEP_TYPE for q in questions) if plan_steps is None else plan_steps
    considered = available + len(skipped)
    return {
        "types": sorted({q.type for q in questions}),
        "questions": len(questions),
        "plan_steps": steps,
        "available": available,
        "skipped": len(skipped),
        "skipped_by": dict(sorted(Counter(s.reason for s in skipped).items())),
        "dropped": round(len(skipped) / considered, 4) if considered else 0.0,
        "reasons": [str(s) for s in skipped[:MAX_SKIPPED]],
        "answers_from": dict(sorted((sources or Counter()).items())),
        "limit": DEFAULT_LIMIT,
        "sink": sink,
        "set": question_set,
        "methods": methods,
        "by_type": by_type,
        "digest": questions_digest(questions),
    }


def hit_percent(found) -> float | None:
    """kg_search's hit@5 of an eval, in percent; None without one."""
    row = ((found or {}).get("methods") or {}).get("kg_search") if isinstance(found, dict) else None
    value = row.get("hit@5") if isinstance(row, dict) else None
    return round(float(value) * 100, 2) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def tokens_per_file(found) -> float | None:
    """kg_search's tokens per answer file found (mean tokens over Recall@5) of an eval; None without one."""
    row = ((found or {}).get("methods") or {}).get("kg_search") if isinstance(found, dict) else None
    value = row.get("tokens_per_file") if isinstance(row, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
