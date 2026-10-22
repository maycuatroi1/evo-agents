"""The Curator promotes the memories of a project's agents into its shared memories (plan agent-profile-workspace,
criterion a4), for ``evo_agents.hub.server.proposals`` and ``evo_agents.hub.server.collect``.

What the Reviewer reads (``for_prompt``): the prompt of a review run holds the memories of the project's agents
written or changed since the project's last review run that ended done (all of them before the first), newest first,
each at its revision with where that revision came from (``memory_agents.provenance_columns``) and its body clipped,
and the project's shared memories (scope project, of no agent, type project or reference) by id, revision, place and
description. Both list only what the run's owner reads through the agent's sink (``AGENT_SINK``) and what names no
audience, as the index of an agent's memories does; neither deleted memories nor agents' memories archived or promoted
already. The prompt says that memories are data, never instructions (``review.memory_lines``).

Evidence ``memory:ID:REV`` (``evidence``): a memory of the project such a prompt could list, an agent's or a shared
one, at a revision it has had; kept with ``resolved`` ``agent_memory`` (and the agent's handle) or ``shared_memory``.

A promotion (``PromotionIn``) rides on a proposal of kind memory, which then takes no draft plan: its sources are the
proposal's evidence that names agents' memories, each at its current revision; its target is a shared memory of the
project named by id and current revision, which it merges into, or a new one named by location (harness or a repo of
the project), name and type project or reference, at a key no shared memory holds; and its body is the whole memory
after the merge. The hub checks it when the run records the proposal (``checked``, 422 naming what is wrong) and keeps
it in proposals.promotion with the label it will take: the join of the labels of the sources and of the target it merges
into, never lower than the highest of them, which must pass the project's write rule through its hub sink
(``ProjectAccess.push_label``). The proposal's own label is that label joined with the project's default.

Accepting it (``promote``): by an admin of the project (POST .../curator/proposals/{id}/answer), or by the hub as the
run records it when the proposal is tier 0, not dropped, and the charter's ``auto_merge`` names tier 0 (``recorded``,
with the ledger line ``accepted`` of the curator). The hub writes the shared memory: a new one, one whose tombstone held
the key, or the next revision of the target, the revision naming the proposal (memory_revisions.proposal_id) and the
audit row memory.put; it marks each source promoted (memories.promoted_at, promoted_into), out of the index and the
searches of runs, keeps what it wrote in proposals.promotion, and adds the ledger line ``promoted``. No plan, branch or
pull request is made. A source or target that changed since the proposal was recorded makes the accept a 409, and
nothing is written. The tier of kind memory stays the hub's (``tiers``): 0.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import BigInteger, DateTime, and_, bindparam, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import review, tables
from evo_agents.hub.memory import AGENT_SINK, HARNESS, MAX_BODY, SHARED_TYPES, body_problem, name_problem
from evo_agents.hub.server import ledger as ledger_lines
from evo_agents.hub.server import memories, memory_agents
from evo_agents.hub.server.audit import record
from evo_agents.hub.server.history import owner_access
from evo_agents.hub.server.mcp_text import memory_about
from evo_agents.hub.server.memory_audience import names_one
from evo_agents.hub.server.memory_paging import MAX_ID
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal
from evo_agents.kg.policy import join_all

PROMPT_ROWS = 40  # agents' memories in a review prompt, newest first
SHARED_ROWS = 100  # shared memories in a review prompt
BODY_BYTES = 1536  # each agent's memory's body in the prompt, at most
SCAN_ROWS = 400  # memories read for a prompt before the read rule filters them
AUTO_NOTE = "accepted by the hub: a promotion of tier 0, and the charter's auto_merge names tier 0"
WHERE = "the promotion"


class PromotionTarget(BaseModel):
    """The shared memory a promotion writes: one of the project by id and current revision, or a new one."""

    memory_id: int | None = Field(None, ge=1, le=MAX_ID, description="a shared memory of the project to merge into")
    revision: int | None = Field(None, ge=1, le=memories.MAX_REVISION, description="its current revision")
    location: str | None = Field(None, min_length=1, max_length=255, description="a new one: harness or a repo")
    name: str | None = Field(None, min_length=4, max_length=255, description="a new one: its file name, ending in .md")
    type: Literal[tuple(sorted(SHARED_TYPES))] | None = Field(None, description="a new one: project or reference")

    @model_validator(mode="after")
    def _one(self):
        merges = self.memory_id is not None or self.revision is not None
        new = any(value is not None for value in (self.location, self.name, self.type))
        if merges == new:
            raise ValueError("a target is {memory_id, revision} of a shared memory, or {location, name, type}")
        if merges and (self.memory_id is None or self.revision is None):
            raise ValueError("a shared memory to merge into is named by memory_id and revision together")
        if new and None in (self.location, self.name, self.type):
            raise ValueError("a new shared memory is named by location, name and type together")
        if new and not memories._printable(self.location):
            raise ValueError("a location is printable text without a slash or a backslash")
        problem = name_problem(self.name) if new else None
        if problem:
            raise ValueError(problem)
        return self


class PromotionIn(BaseModel):
    """What a proposal of kind memory promotes: its sources are its evidence naming agents' memories."""

    target: PromotionTarget
    body: str = Field(min_length=1, max_length=MAX_BODY, description="the whole memory after the merge")

    @model_validator(mode="after")
    def _body(self):
        problem = body_problem(self.body)
        if problem:
            raise ValueError(problem)
        return self


def _refused(where: str, why: str) -> HTTPException:
    return HTTPException(422, f"{where}: {why}; nothing was recorded")


def _readable(access: ProjectAccess, label) -> bool:
    """What a review run's prompt may list: a label naming no audience that the owner reads through the agent's sink."""
    return not names_one(label) and access.visible(label, AGENT_SINK)


# What the Reviewer reads


def _changed_memories():
    m, r = tables.memories, tables.memory_revisions
    namespace = tables.agent_profiles.alias("namespace")
    provenance, joined = memory_agents.provenance_columns()
    source = joined(
        m.join(r, and_(r.c.memory_id == m.c.id, r.c.revision == m.c.revision)).join(
            namespace, namespace.c.id == m.c.profile_id
        )
    )
    since = bindparam("since", type_=DateTime(timezone=True))
    return (
        select(m.c.id, m.c.revision, m.c.location, m.c.name, m.c.type, m.c.label, m.c.body, namespace.c.handle)
        .add_columns(*provenance)
        .select_from(source)
        .where(
            m.c.project_id == bindparam("project_id", type_=BigInteger),
            m.c.profile_id.is_not(None),
            ~m.c.deleted,
            m.c.archived_at.is_(None),
            m.c.promoted_at.is_(None),
            func.coalesce(m.c.updated_at > since, True),
        )
        .order_by(m.c.updated_at.desc(), m.c.id.desc())
        .limit(SCAN_ROWS)
    )


def _shared_memories():
    m = tables.memories
    return (
        select(m.c.id, m.c.revision, m.c.location, m.c.name, m.c.type, m.c.label, m.c.body)
        .where(
            m.c.project_id == bindparam("project_id", type_=BigInteger),
            m.c.scope == "project",
            m.c.profile_id.is_(None),
            m.c.type.in_(memories.SHARED),
            ~m.c.deleted,
        )
        .order_by(m.c.location, m.c.name)
        .limit(SCAN_ROWS)
    )


async def _last_review(conn: AsyncConnection, run_id: int, project_id: int):
    """When the project's last review run before ``run_id`` that ended done was claimed, which built its prompt; None
    before the first."""
    r = tables.runs
    query = select(func.max(r.c.leased_at)).where(
        r.c.project_id == project_id, r.c.kind == "review", r.c.state == "done", r.c.id < run_id
    )
    return (await conn.execute(query)).scalar_one_or_none()


def _clipped(body: str) -> str:
    data = body.encode()
    return body if len(data) <= BODY_BYTES else data[:BODY_BYTES].decode(errors="ignore") + "\n[...]"


async def for_prompt(conn: AsyncConnection, run_id: int, project_id: int, owner_id: int) -> dict | None:
    """The agents' memories new or changed since the last review and the shared memories, as review run ``run_id``
    of project ``project_id``, owned by ``owner_id``, may read them (``review.memory_lines``); None when the owner no
    longer reads the project."""
    access = await owner_access(conn, owner_id, project_id)
    if access is None:
        return None
    since = await _last_review(conn, run_id, project_id)
    agents = []
    for row in (await conn.execute(_changed_memories(), {"project_id": project_id, "since": since})).all():
        if len(agents) < PROMPT_ROWS and _readable(access, row.label):
            provenance = memory_agents.provenance_of(row)
            agents.append(
                {
                    "id": row.id,
                    "revision": row.revision,
                    "agent": row.handle,
                    "location": row.location,
                    "name": row.name,
                    "type": row.type,
                    "provenance": provenance.model_dump() if provenance else None,
                    "body": _clipped(row.body),
                }
            )
    shared = [
        {key: getattr(row, key) for key in ("id", "revision", "location", "name", "type")}
        | {"about": memory_about(row.body)}
        for row in (await conn.execute(_shared_memories(), {"project_id": project_id})).all()
        if _readable(access, row.label)
    ][:SHARED_ROWS]
    return {"since": since.isoformat() if since else None, "agents": agents, "shared": shared}


# Evidence, and the check of a promotion when the run records it


async def evidence(conn: AsyncConnection, project_id: int, project: str, access: ProjectAccess, item, where: str):
    """Evidence ``item`` of kind memory as the hub found it; 422 when the run could not have read it."""
    row = await memories._load(conn, item.memory_id)
    shared = row is not None and row.profile_id is None and row.memory.type in SHARED_TYPES
    if (
        row is None
        or row.project_id != project_id
        or row.memory.scope != "project"
        or row.memory.deleted
        or not (shared or row.profile_id is not None)
        or not _readable(access, row.memory.label)
    ):
        raise _refused(where, f"project {project} has no memory {item.memory_id} that this run may read")
    if item.revision > row.memory.revision:
        raise _refused(where, f"memory {item.memory_id} is at revision {row.memory.revision}, not {item.revision}")
    if row.profile_id is None:
        return {**item.stored(), "resolved": "shared_memory"}
    return {**item.stored(), "resolved": "agent_memory", "agent": row.memory.agent}


def _label(access: ProjectAccess, labels: list) -> dict:
    """The join of ``labels``, as the write rule of the project stores it; 422 when one cannot be read or the join
    does not pass the project's hub sink."""
    found = [access.rules.stored(label) for label in labels]
    if None in found:
        raise HTTPException(422, f"{WHERE}: a label it joins cannot be read; nothing was written")
    return access.push_label(access.rules.describe(join_all(found)))


def _changed(memory_id: int, row, revision: int) -> str | None:
    """Why agent's memory ``row`` (a ``memories.Row``) is no longer the one the promotion names; None when it is."""
    if row is None or row.memory.deleted:
        return f"memory {memory_id} was deleted"
    if row.memory.revision != revision:
        return f"memory {memory_id} is at revision {row.memory.revision} now, not {revision}"
    if row.memory.promoted_at is not None:
        return f"memory {memory_id} was promoted already, into memory {row.memory.promoted_into}"
    return None


async def _sources(conn: AsyncConnection, sources: list[dict], *, lock: bool) -> tuple[list, str | None]:
    """The rows of the agents' memories ``sources`` names, and why the first of them changed (None)."""
    rows = []
    for source in sources:
        row = await memories._load(conn, source["memory_id"], lock=lock)
        why = _changed(source["memory_id"], row, source["revision"])
        if why is not None:
            return rows, why
        rows.append(row)
    return rows, None


def _key(project_id: int, target: dict, user: Principal) -> dict:
    """The parameters of ``memories._at_key("shared")`` for a new target."""
    return {
        "profile_id": None,
        "scope": "project",
        "project_id": project_id,
        "location": target["location"],
        "name": target["name"],
        "user_id": user.user_id,
    }


async def _target(conn: AsyncConnection, project_id: int, target: dict, user: Principal, *, lock: bool):
    """The row the promotion writes over (the shared memory it merges into, or the tombstone at its new key; None
    for none), and why it cannot be written now (None when it can)."""
    if "memory_id" in target:
        row = await memories._load(conn, target["memory_id"], lock=lock)
        shared = row is not None and row.project_id == project_id and row.profile_id is None
        if not shared or row.memory.scope != "project" or row.memory.type not in SHARED_TYPES or row.memory.deleted:
            return None, f"project {project_id} has no shared memory {target['memory_id']}"
        if row.memory.revision != target["revision"]:
            return (
                None,
                f"shared memory {row.memory.id} is at revision {row.memory.revision} now, not {target['revision']}",
            )
        return row, None
    found = (await conn.execute(memories._at_key("shared"), _key(project_id, target, user))).one_or_none()
    row = memories._row(found) if found else None
    if row is not None and not row.memory.deleted:
        return None, (
            f"shared memory {row.memory.id} (revision {row.memory.revision}) holds {target['name']} at "
            f"{target['location']}: name it as the target, with memory_id and revision, to merge into it"
        )
    return row, None


async def checked(
    conn: AsyncConnection, run, access: ProjectAccess, promotion: PromotionIn | None, evidence_found: list, label: dict
) -> tuple[dict | None, dict]:
    """The promotion as proposals.promotion keeps it and the label of its proposal, for review run ``run`` whose
    owner has ``access``; 422 naming what is wrong. (None, ``label``), the proposal's, without one."""
    if promotion is None:
        return None, label
    sources = list(
        {item["memory_id"]: item for item in evidence_found if item.get("resolved") == "agent_memory"}.values()
    )
    if not sources:
        raise _refused(WHERE, "name each agent's memory it promotes as evidence memory:ID:REV")
    target = promotion.target.model_dump(exclude_none=True)
    if (
        "location" in target
        and target["location"] != HARNESS
        and not await memories._has_repo(conn, run.project_id, target["location"])
    ):
        raise _refused(WHERE, f"its target's location must be {HARNESS} or a repo of project {run.project}")
    rows, why = await _sources(conn, sources, lock=False)
    if why is None and any(row.memory.archived_at is not None for row in rows):
        why = "an agent's memory it promotes is archived: a writer restores it first"
    held, why = (None, why) if why else await _target(conn, run.project_id, target, run.owner, lock=False)
    if why is None and held is not None and "memory_id" in target and not _readable(access, held.memory.label):
        why = f"project {run.project} has no shared memory {target['memory_id']} that this run may read"
    if why is not None:
        raise _refused(WHERE, why)
    merged = [held.memory.label] if held is not None and "memory_id" in target else []
    taken = _label(access, [row.memory.label for row in rows] + merged)
    kept = [{"memory_id": s["memory_id"], "revision": s["revision"], "agent": s.get("agent")} for s in sources]
    stored = {"sources": kept, "target": target, "body": promotion.body, "label": taken}
    return stored, _label(access, [label, taken])


# Accepting it


async def _write(conn: AsyncConnection, user: Principal, project_id: int, held, target: dict, body: str, label: dict):
    """Write the shared memory: over ``held`` (its next revision), else a new one; its id."""
    if held is None:
        m = tables.memories
        values = {
            "scope": "project",
            "project_id": project_id,
            "location": target["location"],
            "name": target["name"],
            "type": target["type"],
            "owner_id": user.user_id,
            "label": label,
            "body": body,
            "updated_by": user.user_id,
        }
        return (await conn.execute(insert(m).values(**values).returning(m.c.id))).scalar_one()
    kind = held.memory.type if "memory_id" in target else target["type"]
    await conn.execute(memories._CHANGED, memories._changed(held.memory.id, kind, label, body, False, user.user_id))
    return held.memory.id


async def promote(conn: AsyncConnection, access: ProjectAccess, user: Principal, proposal_id: int, promotion: dict):
    """Write the promotion of proposal ``proposal_id``, accepted by ``user`` with ``access``, in the caller's
    transaction: the shared memory, its revision naming the proposal, the sources marked promoted, the ledger line;
    409 when a source or the target changed since the proposal was recorded."""
    target = promotion["target"]
    if "memory_id" in target:
        place = await memories._load(conn, target["memory_id"])
        where = place.memory if place is not None else SimpleNamespace(location="", name="")
    else:
        where = SimpleNamespace(**target)
    key = SimpleNamespace(scope="project", type="project", location=where.location, name=where.name)
    await conn.execute(memories._LOCK, {"lock_key": memories._lock_key(key, user, access.project_id, None)})
    rows, why = await _sources(conn, promotion["sources"], lock=True)
    held, why = (None, why) if why else await _target(conn, access.project_id, target, user, lock=True)
    if why is not None:
        raise HTTPException(
            409,
            f"proposal {proposal_id} cannot be accepted: {why} since it was recorded. Reject it; the next review "
            "reads the memories as they are; nothing was written",
        )
    merged = [held.memory.label] if held is not None and "memory_id" in target else []
    label = _label(access, [row.memory.label for row in rows] + merged)
    memory_id = await _write(conn, user, access.project_id, held, target, promotion["body"], label)
    provenance = {**dict.fromkeys(memory_agents.PARAMETERS), "prov_proposal": proposal_id}
    await conn.execute(memories._REVISION, {"written_id": memory_id, "actor": user.user_id, **provenance})
    await record(conn, actor_id=user.user_id, token_id=user.token_id, action=memories.PUT, target=f"memory:{memory_id}")
    m, p = tables.memories, tables.proposals
    ids = [row.memory.id for row in rows]
    await conn.execute(update(m).values(promoted_at=func.now(), promoted_into=memory_id).where(m.c.id.in_(ids)))
    revision = (await conn.execute(select(m.c.revision).where(m.c.id == memory_id))).scalar_one()
    written = {"memory_id": memory_id, "revision": revision}
    await conn.execute(update(p).values(promotion={**promotion, "written": written}).where(p.c.id == proposal_id))
    sources = ", ".join(f"memory:{row.memory.id}:{row.memory.revision}" for row in rows)
    await ledger_lines.append(
        conn,
        project_id=access.project_id,
        proposal_id=proposal_id,
        action="promoted",
        actor="curator",
        what=f"wrote shared memory {memory_id} at revision {revision} ({where.location}/{where.name}) from {sources}",
        details={"written": written, "sources": [row.memory.id for row in rows]},
    )
    return written


async def _auto_merge(conn: AsyncConnection, project_id: int) -> list:
    c = tables.charters
    query = select(c.c.body).where(c.c.project_id == project_id).order_by(c.c.revision.desc()).limit(1)
    body = (await conn.execute(query)).scalar_one_or_none() or {}
    return list(body.get("auto_merge") or [])


async def recorded(conn: AsyncConnection, run, access, proposal_id: int, tier: int, state: str, promotion) -> bool:
    """Accept ``promotion`` (None for none) of proposal ``proposal_id``, just recorded by review run ``run`` in
    ``state``, when it is tier 0, open, and the charter's auto_merge names tier 0, in the name of the run's owner
    (proposals.answered_by) with the note AUTO_NOTE; whether it did."""
    if promotion is None or tier != 0 or state != "open" or 0 not in await _auto_merge(conn, run.project_id):
        return False
    p = tables.proposals
    values = {
        "state": review.ANSWERED["accept"],
        "answered_by": run.owner.user_id,  # whose review it is; the hub accepted it on the charter's auto_merge
        "answered_at": func.now(),
        "note": AUTO_NOTE,
    }
    await conn.execute(update(p).values(**values).where(p.c.id == proposal_id))
    await ledger_lines.append(
        conn, project_id=run.project_id, proposal_id=proposal_id, action="accepted", actor="curator", what=AUTO_NOTE
    )
    await promote(conn, access, run.owner, proposal_id, promotion)
    return True
