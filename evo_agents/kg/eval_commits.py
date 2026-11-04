"""Starter questions for ``kg eval`` from the commit history a graph holds (``kg eval-questions --from-commits``).

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
from dataclasses import dataclass, field

from evo_agents.kg.commit_labels import MAX_FILES, MIN_WORDS, commit_label
from evo_agents.kg.evaluate import FILE_ID
from evo_agents.kg.store import LIVE, SEEN, Store

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


def _files(store: Store, b: int) -> dict[str, set[str]]:
    """The paths of each repo's File nodes the graph holds."""
    held: dict[str, set[str]] = {}
    rows = store.db.execute(f"SELECT node_id FROM nodes WHERE kind = 'File' AND {LIVE} AND {SEEN}", {"b": b})
    for (node_id,) in rows:
        found = FILE_ID.match(node_id)
        if found:
            held.setdefault(found["repo"], set()).add(found["path"])
    return held


def _changed(store: Store, b: int, ids: list[str]) -> dict[str, list[str]]:
    """The paths of the File nodes each commit node changed (its ``changes`` edges), by repo and path."""
    out: dict[str, list[str]] = {node_id: [] for node_id in ids}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ", ".join(f":i{n}" for n in range(len(chunk)))
        rows = store.db.execute(
            f"SELECT src, dst FROM edges WHERE rel = 'changes' AND src IN ({marks}) AND {LIVE} AND {SEEN} ORDER BY dst",
            {"b": b, **{f"i{n}": node_id for n, node_id in enumerate(chunk)}},
        )
        for src, dst in rows:
            found = FILE_ID.match(dst)
            if found:
                out[src].append(found["path"])
    return out


def _messages(store: Store, ids: list[str]) -> dict[str, str]:
    """The text each commit node indexes (its message), for the nodes whose row has no audience restriction."""
    out: dict[str, str] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ", ".join("?" for _ in chunk)
        for node_id, body in store.db.execute(
            f"SELECT node_id, body FROM node_fts WHERE kind = 'Commit' AND node_id IN ({marks})", chunk
        ):
            out[node_id] = body or ""
    return out


def question_id(repo: str, sha: str) -> str:
    return f"{ID_PREFIX}{SAFE.sub('-', repo)}-{sha[:12]}"


def commit_questions(
    store: Store,
    b: int | None = None,
    *,
    min_words: int = MIN_WORDS,
    max_files: int = MAX_FILES,
    limit: int = DEFAULT_LIMIT,
    ceiling: tuple[int, int] | None = None,
) -> Found:
    """The questions of build ``b`` (the latest ready one) of the graph in ``store``. ``ceiling`` is the (level,
    location) of the most a reader of the set may see: a commit above it asks nothing."""
    found = Found()
    b = b if b is not None else store.latest_ready()
    if b is None:
        return found
    held = _files(store, b)
    rows = store.db.execute(
        "SELECT node_id, props, label_lvl, label_loc FROM nodes WHERE kind = 'Commit'"
        f" AND {LIVE} AND {SEEN} ORDER BY node_id",
        {"b": b},
    ).fetchall()
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
    messages, changed = _messages(store, ids), _changed(store, b, ids)
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
