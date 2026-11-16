"""Starter questions for ``kg eval`` from the commit history a graph holds (the hub's starter set,
``evo_agents.hub.kg_eval_starter``).

A project with git history is measurable without a session or a plan: the subject and body of a commit asks, and the
files it changed that the graph holds answer. The commits are the Commit nodes of the graph's git sources (the files
of their ``changes`` edges and the message the node indexes), so the same questions come out on a machine and on the
hub, which has no checkout. The filter is ``evo_agents.kg.commit_labels`` (bots, short messages, sweeps, lock and
generated files), the one ``kg rank train`` takes its git labels with.

An answer is a file, never the commit's own node or the pull request that merged it: those carry the question's own
words, so a question that found them would find itself. A merge commit is no question (its paths are those of a pull
request, which the question does not name). An id is ``commit-<repo>-<12 characters of the sha>``, so a commit falls in
the same part (tune or holdout) whenever the set is made again.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from evo_agents.kg.commit_labels import MAX_FILES, MIN_WORDS, commit_label
from evo_agents.kg.evaluate import FILE_ID
from evo_agents.kg.snapshot import StoreRows

COMMIT_TYPE = "commit"
DEFAULT_LIMIT = 300  # questions a set keeps; the commits whose ids hash lowest, so the same ones on every call
ID_PREFIX = "commit-"
SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class Found:
    questions: list[dict] = field(default_factory=list)
    commits: int = 0
    left_out: dict[str, int] = field(default_factory=dict)

    def skip(self, why: str) -> None:
        self.left_out[why] = self.left_out.get(why, 0) + 1


def question_id(repo: str, sha: str) -> str:
    return f"{ID_PREFIX}{SAFE.sub('-', repo)}-{sha[:12]}"


def rows_questions(
    rows: StoreRows,
    *,
    min_words: int = MIN_WORDS,
    max_files: int = MAX_FILES,
    limit: int = DEFAULT_LIMIT,
    ceiling: tuple[int, int] | None = None,
) -> Found:
    """The questions of the rows of a build (``evo_agents.kg.snapshot.StoreRows``), read as a reader without an
    audience reads them. ``ceiling`` is the (level, location) of the most a reader of the set may see: a commit above
    it asks nothing."""
    public = sorted(n for n in rows.nodes if n[10] is None)
    commits = [(n[0], n[3], n[6], n[7]) for n in public if n[1] == "Commit"]
    held: dict[str, set[str]] = {}
    for n in public:
        found = FILE_ID.match(n[0]) if n[1] == "File" else None
        if found:
            held.setdefault(found["repo"], set()).add(found["path"])
    bodies = {key: row[4] or "" for key, row in rows.keyed if key == row[0] and len(row) == 5 and row[1] == "Commit"}

    def changed(ids: list[str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {node_id: [] for node_id in ids}
        for e in sorted(
            (e for e in rows.edges if e[2] == "changes" and e[1] in out and e[10] is None), key=lambda e: e[3]
        ):
            found = FILE_ID.match(e[3])
            if found:
                out[e[1]].append(found["path"])
        return out

    return _questions(
        commits,
        held,
        lambda ids: {i: bodies[i] for i in ids if i in bodies},
        changed,
        min_words=min_words,
        max_files=max_files,
        limit=limit,
        ceiling=ceiling,
    )


def _questions(
    rows,
    held: dict[str, set[str]],
    messages_of: Callable[[list[str]], dict[str, str]],
    changed_of: Callable[[list[str]], dict[str, list[str]]],
    *,
    min_words: int,
    max_files: int,
    limit: int,
    ceiling: tuple[int, int] | None,
) -> Found:
    """The questions of the Commit node rows ``rows`` (node id, props, level, location), by node id."""
    found = Found()
    commits = []
    for node_id, props, level, location in rows:
        data = json.loads(props or "{}")
        if data.get("item_kind") != "commit" or not data.get("sha") or not data.get("repo"):
            continue  # a commit only prose names (repo@sha) has no message and no paths
        found.commits += 1
        if data.get("merge"):
            found.skip("merge")
        elif ceiling is not None and (level > ceiling[0] or location > ceiling[1]):
            found.skip("above the label of the set")
        else:
            commits.append((node_id, data))
    ids = [node_id for node_id, _ in commits]
    messages, changed = messages_of(ids), changed_of(ids)
    made = []
    for node_id, data in commits:
        paths = changed[node_id]
        # the graph holds the files it reads: the others the commit changed still count towards a sweep
        others = [f"\0{n}" for n in range(max(0, int(data.get("files") or 0) - len(paths)))]
        commit = {
            "author": data.get("author_email", ""),
            "message": messages.get(node_id, ""),
            "files": [*paths, *others],
        }
        label = commit_label(commit, held.get(data["repo"], set()), min_words, max_files)
        if label is None:
            found.skip("bot, short, sweep or no file of the graph")
            continue
        text, files = label
        made.append(
            {
                "id": question_id(data["repo"], data["sha"]),
                "question": text,
                "type": COMMIT_TYPE,
                "repo": data["repo"],
                "files": sorted(f"{data['repo']}:{path}" for path in files),
            }
        )
    made.sort(key=lambda q: (_rank(q["id"]), q["id"]))
    if len(made) > limit:
        found.left_out["over the limit"] = len(made) - limit
    found.questions = made[:limit]
    return found


def _rank(question_id: str) -> str:
    return hashlib.sha256(question_id.encode()).hexdigest()
