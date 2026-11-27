"""A project's retrieval question sets (``evo_agents.hub.server.kg_eval_sets``): checking a set, the transaction that
keeps it as the next version, what a reader may read of it, and the latest set with its holdout answers, which only
the hub's evaluation reads."""

from __future__ import annotations

import json

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from evo_agents.hub.kg_eval_starter import ANSWER_KEYS, QUESTION_KEYS, clean, set_digest
from evo_agents.hub.server import audit, kg_run_access
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.kg.models.eval_sets import EvalSetBody, EvalSetPut, EvalSetView
from evo_agents.hub.server.kg.repository import eval_sets as repository
from evo_agents.hub.server.plans import service as plan_service
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import holds
from evo_agents.kg.evaluate import ALL_TYPES, HOLDOUT, QuestionError, QuestionSet, parse_set, split_of


def parsed(name: str, entries: list[dict]) -> QuestionSet:
    """The set ``name`` of ``entries`` as ``kg eval`` reads it; 422 naming the bad question."""
    lines = [json.dumps({"set": name})] + [json.dumps(entry, ensure_ascii=False) for entry in entries]
    try:
        return parse_set(lines)
    except QuestionError as exc:  # line 1 is the header: line n + 1 is question n
        raise HTTPException(422, f"{exc} (line n + 1 is question n)") from None


def shown(entry: dict, answers: bool) -> dict:
    """A question as a machine reads it: with its split, and without its answers in holdout or on asking."""
    split = split_of(str(entry["id"]))
    keep = QUESTION_KEYS + (ANSWER_KEYS if answers and split != HOLDOUT else ())
    return {**{key: entry[key] for key in keep if key in entry}, "split": split}


async def latest_set(conn: AsyncConnection, project_id: int) -> tuple[QuestionSet, str] | None:
    """The latest version of the project's set, holdout answers included, and its digest; None without one. For the
    hub's evaluation only: what a machine reads goes through GET, which strips the holdout answers."""
    row = await repository.latest_row(conn, project_id)
    if row is None:
        return None
    return QuestionSet(parsed(row.name, row.questions).questions, row.name, row.version), row.digest


async def holdout_questions(conn: AsyncConnection, project_id: int) -> list[dict]:
    """The holdout questions of the project's latest set, without their answers (for building their labels)."""
    row = await repository.latest_row(conn, project_id)
    return [shown(e, False) for e in row.questions if split_of(str(e["id"])) == HOLDOUT] if row else []


async def put_set(engine: AsyncEngine, user, project: str, body: EvalSetBody) -> EvalSetPut:
    """Keep ``body`` as the next version of the project's set, or say the latest is it already, for a writer."""
    missing = [str(n) for n, entry in enumerate(body.questions, 1) if entry.get("id") is None]
    if missing:
        raise HTTPException(
            422, f"every question of a set on the hub has an id; question(s) {', '.join(missing[:20])} have none"
        )
    found = parsed(body.set, body.questions)
    entries = clean(body.questions)
    for entry, question in zip(entries, found.questions, strict=True):
        entry["id"] = question.id  # a number id is a string, as kg eval reads it
    digest = set_digest(entries)
    holdout = sum(q.split == HOLDOUT for q in found.questions)
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        if not holds(access.role, "writer"):
            raise HTTPException(403, f"putting the question set of project {access.name} needs the writer role on it")
        await repository.lock_project(conn, access.project_id)
        latest = await repository.latest_row(conn, access.project_id)
        if latest is not None and latest.digest == digest and latest.name == body.set:
            return EvalSetPut(
                set=body.set,
                version=latest.version,
                digest=digest,
                questions=len(entries),
                holdout=holdout,
                created=False,
            )
        version = (latest.version if latest else 0) + 1
        await repository.add_set(conn, access.project_id, version, body.set, entries, digest, user.user_id)
        await audit.record(
            conn,
            actor_id=user.user_id,
            token_id=user.token_id,
            action=audit.KG_EVAL_SET,
            target=f"{project} set={body.set} version={version} questions={len(entries)}",
            project_id=access.project_id,
        )
    return EvalSetPut(
        set=body.set, version=version, digest=digest, questions=len(entries), holdout=holdout, created=True
    )


async def read_set(
    engine: AsyncEngine, user, project: str, version: int | None, split: str, answers: bool, sink: str | None
) -> EvalSetView:
    """The project's set, or its ``split`` part, for a reader whose grant reaches its default label through ``sink``."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        if access.role is None:
            raise HTTPException(403, f"reading the question set of project {access.name} needs a grant on it")
        if not access.visible(
            access.rules.default_label, plan_service.read_sink(access, kg_run_access.sink(user, sink)) or ""
        ):
            raise HTTPException(404, f"project {project} has no question set you can read")
        row = await repository.latest_row(conn, access.project_id, version)
    if row is None:
        what = f"no version {version} of its question set" if version else "no question set yet"
        raise HTTPException(404, f"project {project} has {what}: put one with `evo-agents hub kg eval-set push`")
    questions = [shown(e, answers) for e in row.questions]
    if split != ALL_TYPES:
        questions = [q for q in questions if q["split"] == split]
    return EvalSetView(
        set=row.name,
        version=row.version,
        digest=row.digest,
        created_at=row.created_at,
        split=split,  # pyright: ignore[reportArgumentType]
        answers=answers,
        questions=questions,
    )
