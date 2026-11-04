"""The question set a project gets from its first graph (plan kg-data-profile-curator, criterion a2).

A project with git history has no plan and no session on the hub the day its first build succeeds, and a graph nobody
can measure cannot be configured from its measures. So that build writes version 1 of the project's set from the
commits the graph holds (``evo_agents.kg.eval_commits``; the hub has no checkout): set ``commits``, a question for each
commit that asks something, in the tune or holdout part by the hash of its id, like every set. The holdout answers stay
on the hub, as the routes of ``evo_agents.hub.server.kg_eval_sets`` keep them.

Only a project with no set yet gets one, and only from files the project's default label may read: a commit whose
label is above it asks nothing. A writer's push, or a later version, never waits on this one.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import ProjectRules

log = logging.getLogger("evo_agents.hub.kg_eval_starter")

SET_NAME = "commits"
QUESTION_KEYS = ("id", "question", "type", "project", "repo")  # what a question without its answers keeps
ANSWER_KEYS = ("files", "nodes")


def set_digest(entries: list[dict]) -> str:
    """A digest of what ``entries`` ask and answer, in their order."""
    data = [{key: entry.get(key) for key in QUESTION_KEYS + ANSWER_KEYS} for entry in entries]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def clean(entries: list[dict]) -> list[dict]:
    return [{key: entry[key] for key in QUESTION_KEYS + ANSWER_KEYS if entry.get(key) is not None} for entry in entries]


def questions_of(graph: Path, ceiling: tuple[int, int] | None) -> list[dict]:
    """The starter questions of the graph in the store at ``graph``; none when it has no ready build. Blocking."""
    from evo_agents.kg.eval_commits import commit_questions
    from evo_agents.kg.store import Store

    store = Store.open_file(graph)
    try:
        return commit_questions(store, ceiling=ceiling).questions
    finally:
        store.close()


async def start(conn: AsyncConnection, project_id: int, project: str, questions: list[dict]) -> int | None:
    """Put ``questions`` as version 1 of the project's set when it has none; its question count, or None when the
    project has a set already or there is no question."""
    sets = tables.kg_eval_sets
    if not questions:
        return None
    if (await conn.execute(select(sets.c.version).where(sets.c.project_id == project_id).limit(1))).first():
        return None
    entries = clean(questions)
    done = await conn.execute(
        pg_insert(sets)
        .values(
            project_id=project_id,
            version=1,
            name=SET_NAME,
            questions=entries,
            digest=set_digest(entries),
            created_by=None,
        )
        .on_conflict_do_nothing(index_elements=[sets.c.project_id, sets.c.version])
    )
    return len(entries) if done.rowcount else None


async def ceiling_of(conn: AsyncConnection, project_id: int, project: str) -> tuple[int, int] | None:
    """The (level, location) of the project's default label, the most a reader of its set may see by default."""
    p = tables.projects
    row = (await conn.execute(select(p.c.levels, p.c.locations, p.c.default_label).where(p.c.id == project_id))).first()
    if row is None:
        return None
    label = ProjectRules(project, row.levels, row.locations, [], row.default_label).stored(row.default_label)
    return (label.level, label.location) if label else None
