"""The retrieval question sets of a project's knowledge graph, versioned on the hub (kg_eval_sets, migration 0178).

A set is what ``evo-agents kg eval`` reads (``evo_agents.kg.evaluate``, docs/kg-eval.md): a question a JSON object,
each in the ``tune`` or the ``holdout`` part by the hash of its id. The hub keeps it so that every machine and the
hub's own evaluation of its graph (``evo_agents.hub.server.kg_health``) ask the same questions.

- PUT /v1/kg/{project}/eval-set {set, questions}: a writer puts the set; it is the next version, or the latest one
  again when its questions are the same (the same digest). 422 names the bad questions.
- GET /v1/kg/{project}/eval-set?version=&split=&answers=: the latest version (or the one named) for a reader whose
  grant reaches the project's default label through the sink (X-Evo-Sink, the hub sink by default), all of it or one
  part. A holdout question never carries its answers here: tuning reads the tune part, and the list of holdout
  questions, without answers, is all a machine gets of the other part. ``answers=false`` strips the tune answers too.

Only the hub's evaluation reads holdout answers, through ``latest_set``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server import plans as plan_routes
from evo_agents.hub.server.admin import ProjectName
from evo_agents.hub.server.errors import ErrorBody
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser
from evo_agents.kg.evaluate import ALL_TYPES, HOLDOUT, QuestionError, QuestionSet, parse_set, split_of

MAX_QUESTIONS = 5000
MAX_NAME_CHARS = 100
QUESTION_KEYS = ("id", "question", "type", "project", "repo")  # what a question without its answers keeps
ANSWER_KEYS = ("files", "nodes")

router = APIRouter(
    prefix="/v1/kg",
    tags=["kg"],
    responses={code: {"model": ErrorBody} for code in (401, 403, 404, 422)},
)


class EvalSetBody(BaseModel):
    set: str = Field(min_length=1, max_length=MAX_NAME_CHARS, description="the set's name, e.g. evo-agents")
    questions: list[dict] = Field(
        min_length=1,
        max_length=MAX_QUESTIONS,
        description="the questions, as the lines of a set file: id, question, type, project, repo, files, nodes",
    )


class EvalSetPut(BaseModel):
    set: str
    version: int
    digest: str = Field(description="SHA-256 of what the questions ask and answer")
    questions: int
    holdout: int = Field(description="the questions in the holdout part")
    created: bool = Field(description="false when the set was the latest version already")


class EvalSetView(BaseModel):
    set: str
    version: int
    digest: str
    created_at: datetime
    split: Literal["all", "tune", "holdout"]
    answers: bool = Field(description="whether the tune questions carry their answers; holdout ones never do")
    questions: list[dict] = Field(description="the questions of the part asked, in the set's order, each with split")


def _parsed(name: str, entries: list[dict]) -> QuestionSet:
    lines = [json.dumps({"set": name})] + [json.dumps(entry, ensure_ascii=False) for entry in entries]
    try:
        return parse_set(lines)
    except QuestionError as exc:  # line 1 is the header: line n + 1 is question n
        raise HTTPException(422, f"{exc} (line n + 1 is question n)") from None


def set_digest(entries: list[dict]) -> str:
    """A digest of what ``entries`` ask and answer, in their order."""
    data = [{key: entry.get(key) for key in QUESTION_KEYS + ANSWER_KEYS} for entry in entries]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _clean(entries: list[dict]) -> list[dict]:
    return [{key: entry[key] for key in QUESTION_KEYS + ANSWER_KEYS if entry.get(key) is not None} for entry in entries]


def shown(entry: dict, answers: bool) -> dict:
    """A question as a machine reads it: with its split, and without its answers in holdout or on asking."""
    split = split_of(str(entry["id"]))
    keep = QUESTION_KEYS + (ANSWER_KEYS if answers and split != HOLDOUT else ())
    return {**{key: entry[key] for key in keep if key in entry}, "split": split}


async def latest_row(conn: AsyncConnection, project_id: int, version: int | None = None):
    s = tables.kg_eval_sets
    query = select(s.c.name, s.c.version, s.c.digest, s.c.questions, s.c.created_at).where(s.c.project_id == project_id)
    if version is not None:
        query = query.where(s.c.version == version)
    return (await conn.execute(query.order_by(s.c.version.desc()).limit(1))).one_or_none()


async def latest_set(conn: AsyncConnection, project_id: int) -> tuple[QuestionSet, str] | None:
    """The latest version of the project's set, holdout answers included, and its digest; None without one. For the
    hub's evaluation only: what a machine reads goes through GET, which strips the holdout answers."""
    row = await latest_row(conn, project_id)
    if row is None:
        return None
    return QuestionSet(_parsed(row.name, row.questions).questions, row.name, row.version), row.digest


async def holdout_questions(conn: AsyncConnection, project_id: int) -> list[dict]:
    """The holdout questions of the project's latest set, without their answers (for building their labels)."""
    row = await latest_row(conn, project_id)
    return [shown(e, False) for e in row.questions if split_of(str(e["id"])) == HOLDOUT] if row else []


@router.put("/{project}/eval-set", response_model=EvalSetPut)
async def put_eval_set(request: Request, project: ProjectName, body: EvalSetBody, user: CurrentUser) -> EvalSetPut:
    """Put the project's retrieval question set: the next version, or the latest again when it asks the same."""
    missing = [str(n) for n, entry in enumerate(body.questions, 1) if entry.get("id") is None]
    if missing:
        raise HTTPException(
            422, f"every question of a set on the hub has an id; question(s) {', '.join(missing[:20])} have none"
        )
    parsed = _parsed(body.set, body.questions)
    entries = _clean(body.questions)
    for entry, question in zip(entries, parsed.questions, strict=True):
        entry["id"] = question.id  # a number id is a string, as kg eval reads it
    digest = set_digest(entries)
    holdout = sum(q.split == HOLDOUT for q in parsed.questions)
    s = tables.kg_eval_sets
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        if not has_role(access.role, "writer"):
            raise HTTPException(403, f"putting the question set of project {access.name} needs the writer role on it")
        await conn.execute(
            select(tables.projects.c.id).where(tables.projects.c.id == access.project_id).with_for_update()
        )
        latest = await latest_row(conn, access.project_id)
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
        await conn.execute(
            s.insert().values(
                project_id=access.project_id,
                version=version,
                name=body.set,
                questions=entries,
                digest=digest,
                created_by=user.user_id,
            )
        )
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


@router.get("/{project}/eval-set", response_model=EvalSetView)
async def get_eval_set(
    request: Request,
    project: ProjectName,
    user: CurrentUser,
    version: Annotated[int | None, Query(ge=1, description="this version, not the latest")] = None,
    split: Annotated[Literal["all", "tune", "holdout"], Query(description="only this part of the set")] = ALL_TYPES,
    answers: Annotated[bool, Query(description="false: no question carries its answers")] = True,
    sink: Annotated[str | None, Header(alias=plan_routes.SINK_HEADER)] = None,
) -> EvalSetView:
    """The project's retrieval question set, or one part of it; holdout questions come without their answers."""
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        if access.role is None:
            raise HTTPException(403, f"reading the question set of project {access.name} needs a grant on it")
        if not access.visible(access.rules.default_label, plan_routes._sink(access, sink)):
            raise HTTPException(404, f"project {project} has no question set you can read")
        row = await latest_row(conn, access.project_id, version)
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
        split=split,
        answers=answers,
        questions=questions,
    )
