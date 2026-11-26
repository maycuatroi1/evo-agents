"""The Curator promotes the memories of a project's agents into its shared memories (plan agent-profile-workspace,
criterion a4), for ``evo_agents.hub.server.proposals`` and ``evo_agents.hub.server.collect``.

What the Reviewer reads (``for_prompt``): the prompt of a review run holds the memories of the project's agents
written or changed since the project's last review run that ended done (all of them before the first), newest first,
each at its revision with where that revision came from (``repository.provenance_columns``) and its body clipped,
and the project's shared memories (scope project, of no agent, type project or reference) by id, revision, place and
description. Both list only what the run's owner reads through the agent's sink (``AGENT_SINK``) and whose audience,
if the label names one, the owner holds now (``kg_audience.person_audience``): a memory a chat run wrote carries
``chat:<id>`` and is listed for an owner in that chat, with its audience beside it; neither deleted memories nor
agents' memories archived or promoted already. The hub notes the audiences it lists as shown to the review run
(``run_audience``). The prompt says that memories are data, never instructions (``review.memory_lines``).

Evidence ``memory:ID:REV`` (``evidence``): a memory of the project such a prompt could list, an agent's or a shared
one, at a revision it has had; kept with ``resolved`` ``agent_memory`` (and the agent's handle) or ``shared_memory``.

A promotion (``PromotionIn``) rides on a proposal of kind memory, which then takes no draft plan: its sources are the
proposal's evidence that names agents' memories, each at its current revision; its target is a shared memory of the
project named by id and current revision, which it merges into, or a new one named by location (harness or a repo of
the project), name and type project or reference, at a key no shared memory holds; and its body is the whole memory
after the merge. The hub checks it when the run records the proposal (``checked``, 422 naming what is wrong) and keeps
it in proposals.promotion with the label it will take: the join of the labels of the sources and of the target it merges
into, never lower than the highest of them, which must pass the project's write rule through its hub sink
(``ProjectAccess.push_label``), and the union of their audiences, which the write rule does not see: a shared memory
promoted from a chat-only memory is read by the people of that chat alone, never more broadly. The proposal's own label
is that label joined with the project's default, and the label of every proposal holds the audiences of the memories
its evidence names (``cited_audience``); a proposal whose label names an audience is listed, shown and answered only
for a caller who holds it (``proposals.readable_proposal``).

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

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import review
from evo_agents.hub.memory import AGENT_SINK
from evo_agents.hub.memory_files import SHARED_TYPES
from evo_agents.hub.memory_places import HARNESS
from evo_agents.hub.server import ledger as ledger_lines
from evo_agents.hub.server import run_audience
from evo_agents.hub.server.base.auditor import Auditor
from evo_agents.hub.server.base.errors import Conflict, Unprocessable
from evo_agents.hub.server.history import owner_access
from evo_agents.hub.server.kg_audience import person_audience
from evo_agents.hub.server.mcp_text import memory_about
from evo_agents.hub.server.memories import repository
from evo_agents.hub.server.memories.models import BODY_BYTES, PROMPT_ROWS, PUT, SHARED_ROWS, PromotionIn, Row
from evo_agents.hub.server.memories.service import agents
from evo_agents.hub.server.memories.service.audience import label_audience, with_audience
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal
from evo_agents.kg.policy import audience_below, audience_join, join_all

AUTO_NOTE = "accepted by the hub: a promotion of tier 0, and the charter's auto_merge names tier 0"
WHERE = "the promotion"


def _refused(where: str, why: str) -> Unprocessable:
    return Unprocessable(f"{where}: {why}; nothing was recorded")


def _readable(access: ProjectAccess, label, held: frozenset[str]) -> bool:
    """What a review run's prompt may list: a label the owner reads through the agent's sink, whose audience, if it
    names one, is within ``held``, the owner's."""
    return access.visible(label, AGENT_SINK) and audience_below(label_audience(label), held)


def _with_audience(item: dict, label) -> dict:
    named = label_audience(label)
    return {**item, "audience": sorted(named)} if named else item


# What the Reviewer reads


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
    since = await repository.last_review(conn, run_id, project_id)
    held = await person_audience(conn, owner_id)
    agent_items, shown = [], set()
    for row in await repository.changed_agent_memories(conn, project_id, since):
        if len(agent_items) < PROMPT_ROWS and _readable(access, row.label, held):
            provenance = agents.provenance_of(row)
            item = {
                "id": row.id,
                "revision": row.revision,
                "agent": row.handle,
                "location": row.location,
                "name": row.name,
                "type": row.type,
                "provenance": provenance.model_dump() if provenance else None,
                "body": _clipped(row.body),
            }
            agent_items.append(_with_audience(item, row.label))
            shown.update(label_audience(row.label))
    shared = []
    for row in await repository.shared_memories(conn, project_id):
        if len(shared) < SHARED_ROWS and _readable(access, row.label, held):
            item = {key: getattr(row, key) for key in ("id", "revision", "location", "name", "type")}
            shared.append(_with_audience(item | {"about": memory_about(row.body)}, row.label))
            shown.update(label_audience(row.label))
    await run_audience.note(conn, run_id, shown)
    return {"since": since.isoformat() if since else None, "agents": agent_items, "shared": shared}


# Evidence, and the check of a promotion when the run records it


async def evidence(
    conn: AsyncConnection, project_id: int, project: str, access: ProjectAccess, held: frozenset[str], item, where: str
):
    """Evidence ``item`` of kind memory as the hub found it, for a review run whose owner holds audience ``held``; 422
    when the run could not have read it."""
    row = await repository.load(conn, item.memory_id)
    shared = row is not None and row.profile_id is None and row.memory.type in SHARED_TYPES
    if (
        row is None
        or row.project_id != project_id
        or row.memory.scope != "project"
        or row.memory.deleted
        or not (shared or row.profile_id is not None)
        or not _readable(access, row.memory.label, held)
    ):
        raise _refused(where, f"project {project} has no memory {item.memory_id} that this run may read")
    if item.revision > row.memory.revision:
        raise _refused(where, f"memory {item.memory_id} is at revision {row.memory.revision}, not {item.revision}")
    if row.profile_id is None:
        return {**item.stored(), "resolved": "shared_memory"}
    return {**item.stored(), "resolved": "agent_memory", "agent": row.memory.agent}


def _label(access: ProjectAccess, labels: list) -> dict:
    """The join of ``labels``, as the write rule of the project stores it, holding the union of their audiences, which
    that rule does not read; 422 when one cannot be read or the join does not pass the project's hub sink."""
    found = [access.rules.stored(label) for label in labels]
    if None in found:
        raise Unprocessable(f"{WHERE}: a label it joins cannot be read; nothing was written")
    audience = frozenset()
    for label in labels:
        audience = audience_join(audience, label_audience(label))
    return with_audience(access.push_label(access.rules.describe(join_all(found))), audience)  # pyright: ignore[reportArgumentType]


async def cited_audience(conn: AsyncConnection, evidence_found: list) -> frozenset[str]:
    """The union of the audiences of the memories evidence ``evidence_found`` names, as they are labelled now."""
    audience = frozenset()
    for memory_id in dict.fromkeys(item["memory_id"] for item in evidence_found if item.get("kind") == "memory"):
        row = await repository.load(conn, memory_id)
        if row is not None:
            audience = audience_join(audience, label_audience(row.memory.label))
    return audience


def _changed(memory_id: int, row: Row | None, revision: int) -> str | None:
    """Why agent's memory ``row`` is no longer the one the promotion names; None when it is."""
    if row is None or row.memory.deleted:
        return f"memory {memory_id} was deleted"
    if row.memory.revision != revision:
        return f"memory {memory_id} is at revision {row.memory.revision} now, not {revision}"
    if row.memory.promoted_at is not None:
        return f"memory {memory_id} was promoted already, into memory {row.memory.promoted_into}"
    return None


async def _sources(conn: AsyncConnection, sources: list[dict], *, lock: bool) -> tuple[list[Row], str | None]:
    """The rows of the agents' memories ``sources`` names, and why the first of them changed (None)."""
    rows = []
    for source in sources:
        row = await repository.load(conn, source["memory_id"], lock=lock)
        why = _changed(source["memory_id"], row, source["revision"])
        if why is not None or row is None:
            return rows, why
        rows.append(row)
    return rows, None


async def _target(
    conn: AsyncConnection, project_id: int, target: dict, user: Principal, *, lock: bool
) -> tuple[Row | None, str | None]:
    """The row the promotion writes over (the shared memory it merges into, or the tombstone at its new key; None
    for none), and why it cannot be written now (None when it can)."""
    if "memory_id" in target:
        row = await repository.load(conn, target["memory_id"], lock=lock)
        shared = row is not None and row.project_id == project_id and row.profile_id is None
        if row is None or not shared:
            return None, f"project {project_id} has no shared memory {target['memory_id']}"
        if row.memory.scope != "project" or row.memory.type not in SHARED_TYPES or row.memory.deleted:
            return None, f"project {project_id} has no shared memory {target['memory_id']}"
        if row.memory.revision != target["revision"]:
            return (
                None,
                f"shared memory {row.memory.id} is at revision {row.memory.revision} now, not {target['revision']}",
            )
        return row, None
    key = repository.key("project", project_id, target["location"], target["name"], user.user_id)
    row = await repository.at_key(conn, "shared", key)
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
    owner has ``access``; 422 naming what is wrong. (None, ``label``), the proposal's, without one. The proposal's
    label holds the audiences of the memories its evidence names (``cited_audience``)."""
    cited = await cited_audience(conn, evidence_found)
    label = with_audience(label, audience_join(label_audience(label), cited)) if cited else label
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
        and not await repository.has_repo(conn, run.project_id, target["location"])
    ):
        raise _refused(WHERE, f"its target's location must be {HARNESS} or a repo of project {run.project}")
    rows, why = await _sources(conn, sources, lock=False)
    if why is None and any(row.memory.archived_at is not None for row in rows):
        why = "an agent's memory it promotes is archived: a writer restores it first"
    held, why = (None, why) if why else await _target(conn, run.project_id, target, run.owner, lock=False)
    owner = await person_audience(conn, run.owner.user_id)
    if why is None and held is not None and "memory_id" in target and not _readable(access, held.memory.label, owner):
        why = f"project {run.project} has no shared memory {target['memory_id']} that this run may read"
    if why is not None:
        raise _refused(WHERE, why)
    merged = [held.memory.label] if held is not None and "memory_id" in target else []
    taken = _label(access, [row.memory.label for row in rows] + merged)
    kept = [{"memory_id": s["memory_id"], "revision": s["revision"], "agent": s.get("agent")} for s in sources]
    stored = {"sources": kept, "target": target, "body": promotion.body, "label": taken}
    return stored, _label(access, [label, taken])


# Accepting it


async def _write(
    conn: AsyncConnection, user: Principal, project_id: int, held: Row | None, target: dict, body: str, label: dict
) -> int:
    """Write the shared memory: over ``held`` (its next revision), else a new one; its id."""
    if held is None:
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
        return await repository.insert_memory(conn, values)
    kind = held.memory.type if "memory_id" in target else target["type"]
    await repository.change(conn, held.memory.id, kind, label, body, False, user.user_id)
    return held.memory.id


async def promote(conn: AsyncConnection, access: ProjectAccess, user: Principal, proposal_id: int, promotion: dict):
    """Write the promotion of proposal ``proposal_id``, accepted by ``user`` with ``access``, in the caller's
    transaction: the shared memory, its revision naming the proposal, the sources marked promoted, the ledger line;
    409 when a source or the target changed since the proposal was recorded."""
    target = promotion["target"]
    if "memory_id" in target:
        place = await repository.load(conn, target["memory_id"])
        where = place.memory if place is not None else SimpleNamespace(location="", name="")
    else:
        where = SimpleNamespace(**target)
    await repository.lock_key(
        conn, "project", "project", where.location, where.name, user.user_id, access.project_id, None
    )
    rows, why = await _sources(conn, promotion["sources"], lock=True)
    held, why = (None, why) if why else await _target(conn, access.project_id, target, user, lock=True)
    if why is not None:
        raise Conflict(
            f"proposal {proposal_id} cannot be accepted: {why} since it was recorded. Reject it; the next review "
            "reads the memories as they are; nothing was written"
        )
    merged = [held.memory.label] if held is not None and "memory_id" in target else []
    label = _label(access, [row.memory.label for row in rows] + merged)
    memory_id = await _write(conn, user, access.project_id, held, target, promotion["body"], label)
    provenance = {**dict.fromkeys(repository.PARAMETERS), "prov_proposal": proposal_id}
    await repository.add_revision(conn, memory_id, user.user_id, provenance)
    await Auditor(conn, None, user).record(PUT, f"memory:{memory_id}")
    ids = [row.memory.id for row in rows]
    await repository.mark_promoted(conn, ids, memory_id)
    revision = await repository.revision_of(conn, memory_id)
    written = {"memory_id": memory_id, "revision": revision}
    await repository.update_proposal(conn, proposal_id, {"promotion": {**promotion, "written": written}})
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


async def recorded(conn: AsyncConnection, run, access, proposal_id: int, tier: int, state: str, promotion) -> bool:
    """Accept ``promotion`` (None for none) of proposal ``proposal_id``, just recorded by review run ``run`` in
    ``state``, when it is tier 0, open, and the charter's auto_merge names tier 0, in the name of the run's owner
    (proposals.answered_by) with the note AUTO_NOTE; whether it did."""
    if promotion is None or tier != 0 or state != "open" or 0 not in await repository.auto_merge(conn, run.project_id):
        return False
    values = {
        "state": review.ANSWERED["accept"],
        "answered_by": run.owner.user_id,  # whose review it is; the hub accepted it on the charter's auto_merge
        "answered_at": func.now(),
        "note": AUTO_NOTE,
    }
    await repository.update_proposal(conn, proposal_id, values)
    await ledger_lines.append(
        conn, project_id=run.project_id, proposal_id=proposal_id, action="accepted", actor="curator", what=AUTO_NOTE
    )
    await promote(conn, access, run.owner, proposal_id, promotion)
    return True
