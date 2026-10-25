"""The routes of northwind-ledger, under /v1/x/northwind-ledger/<project>/: its own tables in SQLAlchemy Core, the
hub's data through the API alone.

- ``entries``: the project's entries (GET, a reader), one more (POST, a writer), which records the event
  ``entry_added`` on the project's stream and the audit line ``northwind-ledger.add``;
- ``remind``: a notice of kind ``northwind_ledger_overdue`` to each member of the project, when an entry is overdue;
- ``overview``: what the hub holds of the project, read through the API (members, plans, tasks, goals, runs);
- ``signal``, ``invite``, ``kg`` and ``hand``: a value of a signal, a join link, a build of the knowledge graph and a
  task for the agent type bookkeeper, each as the API makes them;
- ``configs``: the plugin's config in every project that enables it, for a hub admin.

A project that did not enable the plugin answers 404 before a route runs; ``hub.project_access`` checks the caller's
role in the project as the hub's own routes do.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, insert, select, update

from evo_agents.hub.server import core_plugin_api as hub
from northwind_ledger import OVERDUE, tables

router = APIRouter(include_in_schema=False)
BOOKKEEPER = "bookkeeper"


def plugin() -> hub.Plugin:
    return hub.plugin(__name__)


def _entry(row) -> dict:
    return {
        "id": row.id,
        "memo": row.memo,
        "amount_cents": row.amount_cents,
        "due_on": row.due_on.isoformat() if row.due_on else None,
        "paid": row.paid,
    }


class EntryIn(BaseModel):
    memo: str = Field(min_length=1, max_length=500)
    amount_cents: int
    due_on: date | None = None


@router.get("/{project}/entries")
async def list_entries(project: str, request: Request, caller: hub.Caller, open: bool = False) -> dict:
    e = tables.entries
    async with hub.begin(request) as conn:
        found = await hub.project_access(conn, caller, project)
        query = select(e).where(e.c.project_id == found.id).order_by(e.c.id)
        if open:
            query = query.where(e.c.paid.is_(False))
        rows = (await conn.execute(query)).all()
    return {"entries": [_entry(row) for row in rows]}


@router.post("/{project}/entries", status_code=201)
async def add_entry(project: str, body: EntryIn, request: Request, caller: hub.Caller) -> dict:
    e = tables.entries
    async with hub.begin(request) as conn:
        found = await hub.project_access(conn, caller, project, role="writer")
        made = await conn.scalar(
            insert(e)
            .values(
                project_id=found.id,
                memo=body.memo,
                amount_cents=body.amount_cents,
                due_on=body.due_on,
                created_by=caller.user_id,
            )
            .returning(e.c.id)
        )
        event = await hub.emit_event(conn, plugin(), project, "entry_added", {"entry": made})
        await hub.audit(conn, plugin(), caller, "northwind-ledger.add", f"entry:{made} project={project}")
    return {"id": made, "event": event}


@router.post("/{project}/entries/{entry}/paid")
async def mark_paid(project: str, entry: int, request: Request, caller: hub.Caller) -> dict:
    e = tables.entries
    async with hub.begin(request) as conn:
        found = await hub.project_access(conn, caller, project, role="writer")
        done = await conn.execute(
            update(e).values(paid=True).where(e.c.id == entry, e.c.project_id == found.id).returning(e.c.id)
        )
        if done.scalar_one_or_none() is None:
            raise hub.Refused(404, f"no entry {entry} in the ledger of {project}")
    return {"id": entry, "paid": True}


@router.post("/{project}/remind")
async def remind(project: str, request: Request, caller: hub.Caller) -> dict:
    e = tables.entries
    async with hub.begin(request) as conn:
        found = await hub.project_access(conn, caller, project, role="writer")
        overdue = await conn.scalar(
            select(func.count())
            .select_from(e)
            .where(e.c.project_id == found.id, e.c.paid.is_(False), e.c.due_on < func.current_date())
        )
        if not overdue:
            return {"overdue": 0, "sent": 0}
        members = [grant.user_id for grant in await hub.read_grants(conn, plugin(), project)]
        sent = await hub.notify(
            conn,
            plugin(),
            project,
            OVERDUE,
            users=members,
            title=f"{overdue} entries of the ledger of {project} are overdue",
            link=f"/p/{project}/x/northwind-ledger/ledger",
        )
    return {"overdue": overdue, "sent": len(sent)}


@router.get("/{project}/overview")
async def overview(project: str, request: Request, caller: hub.Caller) -> dict:
    async with hub.begin(request) as conn:
        found = await hub.project_access(conn, caller, project)
        me = plugin()
        members = await hub.read_grants(conn, me, project)
        plans = await hub.read_plans(conn, me, project)
        tasks = await hub.read_tasks(conn, me, project)
        goals = await hub.read_goals(conn, me, project)
        runs = await hub.read_runs(conn, me, project, limit=50)
        users = await hub.read_users(conn, me, ids=[caller.user_id])
    return {
        "project": found.name,
        "role": found.role,
        "you": users[0].login if users else None,
        "members": [grant.login for grant in members],
        "plans": [plan.plan_id for plan in plans],
        "tasks": [task.number for task in tasks],
        "goals": [goal.code for goal in goals],
        "runs": len(runs),
    }


class SignalIn(BaseModel):
    key: str
    value: float
    external_id: str | None = None


@router.post("/{project}/signal")
async def signal(project: str, body: SignalIn, request: Request, caller: hub.Caller) -> dict:
    async with hub.begin(request) as conn:
        await hub.project_access(conn, caller, project, role="writer")
        done = await hub.push_signal(conn, plugin(), project, body.key, body.value, external_id=body.external_id)
    return {"stored": done.stored, "duplicate": done.duplicate, "refused": done.refused}


class InviteIn(BaseModel):
    for_login: str | None = None


@router.post("/{project}/invite")
async def invite(project: str, body: InviteIn, request: Request, caller: hub.Caller) -> dict:
    async with hub.begin(request) as conn:
        await hub.project_access(conn, caller, project, role="admin")
        made = await hub.create_invite(
            conn, plugin(), hub.hub_config(request), project, created_by=caller.user_id, for_login=body.for_login
        )
    return {"id": made.id, "token": made.token, "url": made.url}


@router.post("/{project}/kg", status_code=202)
async def kg(project: str, request: Request, caller: hub.Caller) -> dict:
    async with hub.begin(request) as conn:
        await hub.project_access(conn, caller, project, role="writer")
        await hub.queue_kg_build(conn, plugin(), project)
    return {"queued": True}


class HandIn(BaseModel):
    task: str = Field(min_length=1, max_length=2000)


@router.post("/{project}/hand")
async def hand(project: str, body: HandIn, request: Request, caller: hub.Caller) -> dict:
    async with hub.begin(request) as conn:
        await hub.project_access(conn, caller, project, role="writer")
        kind = await hub.enabled_agent_type(conn, plugin(), project, BOOKKEEPER)
        done = await hub.hand_task(conn, plugin(), project, BOOKKEEPER, body.task, for_user=caller.user_id)
    return {"enabled": kind is not None, "run_id": done.run_id, "refused": done.refused}


@router.get("/{project}/configs")
async def configs(project: str, request: Request, admin: hub.HubAdmin) -> dict:
    async with hub.begin(request) as conn:
        found = await hub.project_configs(conn, plugin(), hub.hub_config(request))
    return {"projects": [item.project for item in found]}
