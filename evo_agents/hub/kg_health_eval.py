"""The retrieval evaluation of a graph the hub built, scored as ``evo-agents kg eval`` scores one, for the measure of
the graph's health (``evo_agents.hub.server.kg_health``).

The questions are those ``evo-agents kg eval-questions`` makes of the done steps of the hub's plans
(``evo_agents.hub.kg_eval``), type ``plan-step``: a step that is done and whose evidence names commits asks its title
(``handoff.commits_of`` reads the hashes), and its answers are the files those commits changed, as ``repo:path``. The
hub has no checkout to ask git, so a commit's files are the files of the graph its ``changes`` edges lead to (the
paths of a commit item its source does not exclude, a merge's against its first parent; a file the branch deleted
since is no answer). A step is left out when it names no repo (its own, or the plan's only one), when the graph has
none of its commits in that repo, or when its commits change no file or more than DEFAULT_MAX_FILES paths. At most MAX_QUESTIONS are
asked, a sample fixed by the hash of each question's id, so a night asks the same ones as the night before plus those
new steps add.

Each question goes to ``kg_search`` (METHODS) with the command's default limit, through a session that reads the graph
as the charter's owner does through the project's hub sink; there is no grep baseline without a checkout. The score is
the command's row over all the questions: Recall@1 and @5, MRR, the share of empty answers, the answers per 1,000
tokens, the mean tokens and the 95th percentile of the time of a call. Only reads the graph.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable

from evo_agents.hub.handoff import commits_of
from evo_agents.hub.plans import step_key
from evo_agents.kg.evaluate import (
    ALL_TYPES,
    DEFAULT_LIMIT,
    DEFAULT_MAX_FILES,
    PLAN_STEP_TYPE,
    Question,
    evaluate,
    id_file,
)

METHODS = ("kg_search",)
MAX_QUESTIONS = 300
SHA_PREFIX = 7
MAX_EDGES = 500  # edges of a commit read: a commit of more than DEFAULT_MAX_FILES files is left out anyway
MAX_SKIPPED = 20  # reasons a measure keeps of the steps left out
SCORE_KEYS = ("n", "recall@1", "recall@5", "mrr", "empty", "errors", "correct_per_1k_tokens", "mean_tokens", "p95_ms")


class GraphCommits:
    """The commits a graph's git sources read, by the first SHA_PREFIX digits of their SHA: each with its node, its
    repo, its SHA and how many paths it changed; the files it changed are its ``changes`` edges."""

    def __init__(self, store, b: int):
        self.store, self.b = store, b
        self.found: dict[str, list[dict]] = defaultdict(list)
        ids = sorted(
            node for node in store.alive_ids("nodes", b) if ":commit:" in node and not node.startswith("commit:")
        )
        for node in store.nodes(ids, b).values():
            props = node.get("props") or {}
            sha, files = props.get("sha"), props.get("files")
            if node.get("kind") == "Commit" and isinstance(sha, str) and type(files) is int:
                entry = {"node": node["id"], "sha": sha, "repo": props.get("repo"), "files": files}
                self.found[sha[:SHA_PREFIX]].append(entry)

    def changed(self, sha: str, repo: str) -> tuple[list[str], int] | None:
        """(the files of the graph the one commit of ``repo`` whose SHA starts with ``sha`` changed, by path, and how
        many paths it changed, deleted ones included); None when the graph has no such commit, or two."""
        found = [e for e in self.found.get(sha[:SHA_PREFIX], []) if e["sha"].startswith(sha) and e["repo"] == repo]
        if len({e["sha"] for e in found}) != 1:
            return None
        edges = self.store.edges_of(found[0]["node"], self.b, "out", limit=MAX_EDGES)
        named = (id_file(edge["dst"]) for edge in edges if edge.get("rel") == "changes")
        return sorted({file[1] for file in named if file is not None}), found[0]["files"]


def _step_repo(body: dict, step: dict) -> str | None:
    if isinstance(step.get("repo"), str) and step["repo"]:
        return step["repo"]
    repos = [r.get("repo") for r in body.get("repos") or [] if isinstance(r, dict)]
    return repos[0] if len(repos) == 1 and isinstance(repos[0], str) else None


def step_question(
    project: str, plan_id: str, body: dict, index: int, commits: GraphCommits
) -> tuple[Question | None, str | None]:
    """The question step ``index`` of a plan gives, or None and why it gives none (None, None: not done, or no
    commit in its evidence)."""
    step = body["steps"][index]
    if not isinstance(step, dict) or step.get("status") != "done":
        return None, None
    shas = commits_of(str(step.get("evidence") or ""))
    if not shas:
        return None, None
    name = f"{plan_id}#{step_key(step, index)}"
    repo = _step_repo(body, step)
    if repo is None:
        return None, f"{name}: no repo named"
    files: list[str] = []
    named, most = 0, 0
    for sha in shas:
        changed = commits.changed(sha, repo)
        if changed is not None:
            named += 1
            most = max(most, changed[1])
            files += [path for path in changed[0] if path not in files]
    if not named:
        return None, f"{name}: none of its {len(shas)} commit(s) is in the graph of {repo}"
    count = max(len(files), most)
    if not files or count > DEFAULT_MAX_FILES:
        return None, f"{name}: its commits change {count} file(s), not 1 to {DEFAULT_MAX_FILES}"
    text = " ".join(str(step.get("title") or "").split())
    if not text:
        return None, f"{name}: no title"
    question = Question(name, text, PLAN_STEP_TYPE, project, repo, tuple((repo, path) for path in files))
    return question, None


def plan_questions(
    project: str, plans: Iterable[tuple[str, dict]], commits: GraphCommits
) -> tuple[list[Question], list[str], int]:
    """The questions of ``plans`` ((plan id, body) each), at most MAX_QUESTIONS of them by id, why each step with
    commits gave none, and how many questions there were before the cut."""
    questions: list[Question] = []
    skipped: list[str] = []
    for plan_id, body in plans:
        steps = body.get("steps") if isinstance(body, dict) and isinstance(body.get("steps"), list) else []
        for index in range(len(steps)):
            question, why = step_question(project, plan_id, body, index, commits)
            if question is not None:
                questions.append(question)
            elif why is not None:
                skipped.append(why)
    sample = sorted(questions, key=lambda q: hashlib.sha256(q.id.encode()).hexdigest())[:MAX_QUESTIONS]
    return sorted(sample, key=lambda q: q.id), skipped, len(questions)


def questions_digest(questions: list[Question]) -> str:
    """A digest of what ``questions`` ask and answer: the same set asked of the same graph scores the same."""
    data = [[q.id, q.text, q.repo, [list(answer) for answer in q.files]] for q in questions]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def score(questions: list[Question], skipped: list[str], available: int, session) -> dict:
    """The score of ``questions`` (``available`` before the cut) asked through ``session`` (a
    ``evo_agents.kg.serve.Session``): {type, questions, available, skipped, reasons, limit, methods: {method: {n,
    recall@1, recall@5, mrr, empty, errors, correct_per_1k_tokens, mean_tokens, p95_ms}}, digest}."""
    methods = {}
    if questions:
        report = evaluate(questions, session_for=lambda _: session, checkouts_for=lambda _: {}, methods=METHODS)
        for row in report.rows:
            if row["type"] == ALL_TYPES:
                methods[row["method"]] = {key: row.get(key) for key in SCORE_KEYS}
    return {
        "type": PLAN_STEP_TYPE,
        "questions": len(questions),
        "available": available,
        "skipped": len(skipped),
        "reasons": skipped[:MAX_SKIPPED],
        "limit": DEFAULT_LIMIT,
        "methods": methods,
        "digest": questions_digest(questions),
    }
