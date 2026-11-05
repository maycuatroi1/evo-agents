"""A project made from an upload (plan kg-upload-archive): ``evo-agents hub kg upload FILE --new-project P [--label
L]``, and the web's "New project from data", create project P with no harness and start the upload into it, in the
transaction of POST /v1/kg/{project}/uploads with ``new_project`` true (``evo_agents.hub.server.kg_uploads``).

The project gets what ``evo-agents harness init`` writes for a new harness, without its sources: the default ladder
(``evo_agents.kg.policy``), one hub sink HUB_SINK that clears L, one Claude Code sink AGENT_SINK that clears only
AGENT_CLEARANCE, so an agent reads what is labelled above it only once an admin raised that clearance on purpose,
one llm-backend sink LLM_SINK with that same clearance, through which the entities step of a strategy such as
multi-hop-api sends prose to a model's endpoint (an item labelled above it is not sent, ``evo_agents.kg.entities``),
the default label L for its memories, plans and uploads, and a minimal knowledge config naming that ladder and those
sinks with no source of its own, so the build the upload's extraction queues runs, and its first success queues a
kg_init run (``first_build``) as for any project with a config. L is DEFAULT_LEVEL without ``--label``.

Creating it takes what registering a project takes (``evo_agents.hub.server.projects.create_project``): a hub admin,
or a member who spends an unused right to register one. The creator becomes its admin at the top level. A name the hub
holds already is refused with 409 and nothing is changed: the upload goes into an existing project with ``--project``.
If anything later in the call fails, the transaction undoes the project and the right stays unused.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.access import HUB_KIND, ProjectRules, Refused
from evo_agents.hub.kg_ingest import config_digest
from evo_agents.hub.server import audit, invites
from evo_agents.hub.server.admin import upsert_grant
from evo_agents.hub.server.profile_store import ensure_default
from evo_agents.hub.server.projects import create_project
from evo_agents.hub.server.security import Principal
from evo_agents.kg.policy import DEFAULT_LEVELS, DEFAULT_LOCATIONS

DEFAULT_LEVEL = "internal"  # the label of the data without --label, as `harness init` labels a new harness's sources
HUB_SINK = "evo-hub"
AGENT_SINK = "claude-code@anthropic"
AGENT_CLEARANCE = "internal"
LLM_SINK = "llm"  # the sink the entities mapping of the built-in prose strategies names


def sinks(level: str) -> list[dict]:
    """The sinks of a project made from an upload labelled ``level``."""
    return [
        {"id": AGENT_SINK, "kind": "agent-session", "clearance": {"level": AGENT_CLEARANCE}},
        {"id": LLM_SINK, "kind": "llm-backend", "clearance": {"level": AGENT_CLEARANCE}},
        {"id": HUB_SINK, "kind": HUB_KIND, "clearance": {"level": level}},
    ]


def knowledge_config(project: str, level: str) -> dict:
    """The minimal knowledge config: the default ladder and the sinks, with no source; uploads add theirs."""
    policy = {"levels": list(DEFAULT_LEVELS), "locations": list(DEFAULT_LOCATIONS), "sinks": sinks(level)}
    return {"knowledge": {"version": 1, "project": project, "policy": policy, "sources": []}, "ontology": None}


def _level(project: str, label: str | None) -> str:
    level = label or DEFAULT_LEVEL
    if level not in DEFAULT_LEVELS:
        raise HTTPException(
            422, f"label {level!r} is not a level of a new project ({', '.join(DEFAULT_LEVELS)}); {project} not created"
        )
    return level


async def create(conn: AsyncConnection, user: Principal, project: str, label: str | None) -> int:
    """Create project ``project`` for data labelled ``label`` (a level name, DEFAULT_LEVEL without one) with
    ``user`` as its admin; its id. 403 without the right to register a project, 409 when the name is taken."""
    level = _level(project, label)
    if not user.admin and await invites.register_right(conn, user) is None:  # before the name: it tells nothing
        raise HTTPException(
            403, f"creating project {project} needs a hub admin, or an unused right to register a project (invite link)"
        )
    taken = await conn.execute(select(tables.projects.c.id).where(tables.projects.c.name == project))
    if taken.first() is not None:
        raise HTTPException(
            409, f"project {project} exists already; nothing was created: upload into it with --project {project}"
        )
    rules = ProjectRules(project, list(DEFAULT_LEVELS), list(DEFAULT_LOCATIONS), sinks(level))
    try:
        default_label = rules.describe(rules.given({"level": level}))
    except Refused as exc:
        raise HTTPException(exc.status, str(exc)) from None
    fields = {"levels": list(DEFAULT_LEVELS), "locations": list(DEFAULT_LOCATIONS), "default_label": default_label}
    top = DEFAULT_LEVELS[-1]
    project_id = await create_project(conn, user, project, fields, top)
    if project_id is None:  # another request created it a moment ago
        raise HTTPException(409, f"project {project} was created a moment ago by another request; nothing was created")
    if user.admin:  # a member spending a right got the admin grant with it
        await conn.execute(upsert_grant(user.user_id, project_id, "admin", top, user.user_id))
    await ensure_default(conn, project_id)
    rows = [
        {"project_id": project_id, "sink_id": s["id"], "kind": s["kind"], "clearance": s["clearance"]}
        for s in sinks(level)
    ]
    await conn.execute(insert(tables.project_sinks), rows)
    config = knowledge_config(project, level)
    await conn.execute(
        insert(tables.kg_configs).values(
            project_id=project_id,
            digest=config_digest(config),
            knowledge=config["knowledge"],
            updated_by=user.user_id,
        )
    )
    await audit.record(
        conn, actor_id=user.user_id, token_id=user.token_id, action=audit.PROJECT_REGISTER, target=project
    )
    return project_id
