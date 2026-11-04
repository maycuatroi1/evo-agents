"""The run that writes a project's starter questions when its data has no commit history to ask from (plan
kg-upload-archive, criterion a6), without the database. ``evo_agents.hub.server.kg_questions`` holds the phase of the
kg_init run that queues it and the route its agent sends the questions to.

The Curator's kg_init run measures the starter questions before it decides (``evo_agents.hub.server.kg_init``). A
project with git history gets them from its commits on its first build (``evo_agents.hub.kg_eval_starter``); an
uploaded archive has no commit, so its project would be configured blind. When a kg_init run starts and the project has
no eval set, or only the commits' set with fewer than MIN_COMMIT_QUESTIONS, the hub queues a review run with lens
``kg_questions`` on the project's default agent pool, under the same cost cap as the kg_init run. Its prompt holds
counts and data classes only: the agent reads the items through the kg_* tools, within the clearance of the run's sink,
and writes at least TARGET questions (one per readable document when there are fewer), each answered by a file or a node
of the graph, covering every class of data the agent can read. The hub checks every answer against the graph as that
sink reads it (``check``) before it keeps the questions as a new version of the project's eval set; a question whose
answer is above the sink's clearance is refused like one whose answer is not in the graph. The kg_init run waits for the
questions run to end, or WAIT, then measures the tune part of the newest set; the holdout part stays for the hub's
nightly evaluation. When the sink reads no document of the graph, no questions run is queued and the kg_init run says
why in its notes.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta

from evo_agents.kg import profile as kg_profile
from evo_agents.kg.evaluate import file_matches, id_file, split_file

LENS = "kg_questions"
MIN_COMMIT_QUESTIONS = 30  # a project whose commits give fewer gets a questions run
TARGET = 100  # questions asked of the agent, or one per readable document when there are fewer
WAIT = timedelta(hours=2)  # the longest a kg_init run waits for its questions run before it measures
SET_NAME = "written"
QUESTION_TYPE = "written"
ID_PREFIX = "written-"
DOC_KINDS = ("File", "Document")  # the nodes of items: what a question can be answered by as a file
LEFT_OUT = frozenset({"binary", "history"})  # classes a question is not written about: assets, commits
MAX_QUESTION_CHARS = 1000
MAX_PROBLEMS = 20

# The states of a project's questions run, as the kg_init run's body["questions"] holds them
NOT_NEEDED, SKIPPED, QUEUED, RUNNING, DONE, FAILED, TIMED_OUT = (
    "not_needed",
    "skipped",
    "queued",
    "running",
    "done",
    "failed",
    "timed_out",
)
ENDED = (NOT_NEEDED, SKIPPED, DONE, FAILED, TIMED_OUT)


def needs_questions(name: str | None, count: int, by_hub: bool) -> str | None:
    """Why the project needs a questions run, from its newest eval set (``name``, ``count`` questions, ``by_hub`` when
    the hub wrote it); None when its set will do."""
    from evo_agents.hub.kg_eval_starter import SET_NAME as COMMITS

    if name is None:
        return "the project has no eval set"
    if by_hub and name == COMMITS and count < MIN_COMMIT_QUESTIONS:
        return f"its commits give {count} questions, under {MIN_COMMIT_QUESTIONS}"
    return None


def target(documents: int) -> int:
    return min(TARGET, documents)


@dataclass
class Readable:
    """What the run's sink reads of a graph: the documents within its clearance by class, and those above it."""

    documents: int = 0
    above: int = 0
    classes: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"documents": self.documents, "above": self.above, "classes": dict(sorted(self.classes.items()))}


@dataclass(frozen=True)
class _Node:
    node_id: str
    repo: str | None
    path: str | None
    cls: str
    readable: bool


def _class(props: Mapping) -> str:
    kind = props.get("item_kind")
    return kg_profile.class_of(kind) if isinstance(kind, str) else "other"


def _nodes(store, ceiling: tuple[int, int] | None, node_id: str | None = None):
    """The nodes of the latest ready build of ``store`` (DOC_KINDS, or the one ``node_id`` names), each with whether a
    reader up to ``ceiling`` (level, location) and the store's audience sees it."""
    from evo_agents.kg.labelled import labelled_nodes

    for node in labelled_nodes(store, DOC_KINDS, node_id):
        named = id_file(node.node_id)
        repo = node.props.get("repo") or (named[0] if named else None)
        yield _Node(node.node_id, repo, node.props.get("path"), _class(node.props), node.within(ceiling))


def readable(store, ceiling: tuple[int, int] | None) -> Readable:
    """The documents of the graph in ``store`` a reader up to ``ceiling`` sees, by class, and how many it does not.
    Assets and commits are left out: no question is written about them. Blocking."""
    found = Readable()
    classes: Counter[str] = Counter()
    for node in _nodes(store, ceiling):
        if node.cls in LEFT_OUT:
            continue
        if node.readable:
            found.documents += 1
            classes[node.cls] += 1
        else:
            found.above += 1
    found.classes = dict(classes)
    return found


def prompt(*, project: str, init_id: int, sink: str, found: Readable, why: str) -> str:
    """The text the questions run starts with: counts and classes, never the name or the text of an item."""
    wanted = target(found.documents)
    classes = ", ".join(f"{name} ({n} documents)" for name, n in sorted(found.classes.items()))
    return "\n".join(
        [
            f"## Starter questions of project {project} (kg_init run #{init_id})",
            "",
            f"The Curator's kg_init run of {project} waits for a set of retrieval questions to measure the graph with: "
            f"{why}. Through your sink ({sink}) the graph holds {found.documents} documents you may read, of these "
            f"classes: {classes or 'none'}.",
            "",
            "Read the documents with kg_search, kg_context and kg_node only: what they do not show you, you may not "
            f"use. Write at least {wanted} questions a person working with this data would ask, in the words of such a "
            'person rather than a copy of a heading, and answer each with the file (`files`: "repo:path" as kg_* '
            "names it) or the node (`nodes`: a node id) that holds the answer. Cover every class above with a "
            "question at least.",
            "",
            "## How to send them",
            'Write one JSON object a line to a file, each `{"question": ..., "files": [...]}` or with `nodes`, and '
            f"run `evo-agents hub kg init --project {project} --run {init_id} --questions FILE`. The hub checks that "
            "every answer is in the graph your sink reads and keeps the questions as a new version of the project's "
            "eval set; it refuses the whole file with the reasons otherwise: fix it and send it again. Record no "
            "finding and file no proposal.",
        ]
    )


@dataclass
class Checked:
    entries: list[dict] = field(default_factory=list)
    classes: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def _answers(entry: Mapping, key: str) -> list[str]:
    found = entry.get(key) or []
    return [a for a in found if isinstance(a, str) and a.strip()] if isinstance(found, list) else []


def _index(store, ceiling) -> tuple[dict[str, list[_Node]], dict[str, _Node]]:
    by_path: dict[str, list[_Node]] = {}
    by_id: dict[str, _Node] = {}
    for node in _nodes(store, ceiling):
        by_id[node.node_id] = node
        if node.path:
            by_path.setdefault(node.path, []).append(node)
    return by_path, by_id


def _node(store, ceiling, node_id: str, by_id: dict[str, _Node]) -> _Node | None:
    if node_id not in by_id:
        found = list(_nodes(store, ceiling, node_id))
        by_id[node_id] = found[0] if found else None
    return by_id[node_id]


def _question_problem(n: int, entry, by_path, by_id, store, ceiling) -> tuple[str | None, set[str]]:
    """(what is wrong with question ``n``, None when nothing; the classes its answers are of)."""
    if not isinstance(entry, Mapping):
        return f"question {n} is not an object", set()
    text = entry.get("question")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_QUESTION_CHARS:
        return f"question {n} has no question text of 1 to {MAX_QUESTION_CHARS} characters", set()
    files, nodes = _answers(entry, "files"), _answers(entry, "nodes")
    if not files and not nodes:
        return f"question {n} has no answer: give files or nodes", set()
    classes: set[str] = set()
    for answer in files:
        wanted = split_file(answer)
        held = [node for node in by_path.get(wanted[1], []) if file_matches(wanted, node.repo, node.path)]
        seen = [node for node in held if node.readable]
        if not seen:  # above the clearance reads as absent: the refusal says no more of it
            return f"question {n}: an answer file is not in the graph you read", set()
        classes |= {node.cls for node in seen}
    for answer in nodes:
        node = _node(store, ceiling, answer, by_id)
        if node is None or not node.readable:
            return f"question {n}: an answer node is not in the graph you read", set()
        classes.add(node.cls)
    return None, classes


def check(store, ceiling: tuple[int, int] | None, questions: list, found: Readable) -> Checked:
    """The questions as a set keeps them, when every answer is a file or a node of the graph in ``store`` a reader up to
    ``ceiling`` sees, there are ``target(found.documents)`` at least and they cover every class of ``found``; else the
    problems. A refusal never names an item. Blocking."""
    out = Checked()
    by_path, by_id = _index(store, ceiling)
    covered: Counter[str] = Counter()
    ids: set[str] = set()
    for n, entry in enumerate(questions, 1):
        problem, classes = _question_problem(n, entry, by_path, by_id, store, ceiling)
        if problem:
            out.problems.append(problem)
            continue
        made = str(entry.get("id") or ID_PREFIX + hashlib.sha256(entry["question"].strip().encode()).hexdigest()[:12])
        if made in ids:
            out.problems.append(f"question {n} repeats the id or the text of an earlier one")
            continue
        ids.add(made)
        covered.update(classes)
        kept = {"id": made, "question": entry["question"].strip(), "type": QUESTION_TYPE}
        for key in ("files", "nodes"):
            if _answers(entry, key):
                kept[key] = _answers(entry, key)
        out.entries.append(kept)
    wanted = target(found.documents)
    if len(out.entries) < wanted:
        out.problems.append(f"{len(out.entries)} questions are good, {wanted} are needed")
    missing = sorted(set(found.classes) - set(covered))
    if missing:
        out.problems.append(f"no question answered by a document of class {', '.join(missing)}")
    out.classes = dict(sorted(covered.items()))
    out.problems = out.problems[:MAX_PROBLEMS]
    return out
