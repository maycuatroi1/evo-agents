"""notes, a core plugin of the tests (criterion a22 of hub-agent-plugins): a table of its own, notes_entries, which a
revision appended to the hub's chain of migrations creates (``core_notes_migrations``); routes under
/v1/x/notes/<project>/ that read and write it; an MCP tool that counts its rows; a subscription to the steps of plans;
a native page, entries, which the web's end-to-end build carries (web/src/components/plugin-notes). Beside it,
``HUB_WIDE`` (beacon) is a core plugin with nothing per project, as Telegram: a channel of members and a route of its
own at the path it names (the empty prefix), which checks its caller itself.

The tests install it as an entry point of group evo_agents.plugins (``installed``) and append its revision to the
hub's migrations (``migrations``), so the hub they start migrates the table and records the plugin in its store.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Column, DateTime, Identity, MetaData, Table, Text, func, insert, select

from evo_agents.hub.server.notifications import Channel, Outgoing
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import CurrentUser

MIGRATIONS = Path(__file__).resolve().parent / "core_notes_migrations"
HERE = "tests.hub.core_notes"

metadata = MetaData()  # the plugin's own: the hub's schema has the table, its metadata does not
entries = Table(
    "notes_entries",
    metadata,
    Column("id", BigInteger, Identity(always=True), primary_key=True),
    Column("project_id", BigInteger, nullable=False),
    Column("body", Text, nullable=False),
    Column("author", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

MANIFEST = {
    "id": "notes",
    "version": "1.0.0",
    "publisher": "evo-agents",
    "tier": "core",
    "name": "Notes",
    "engines": {"evo-agents": ">=0.16"},
    "plugin_api": 1,
    "permissions": ["plans:read"],
    "contributes": {
        "routes": [{"name": "entries", "python": f"{HERE}:router"}],
        "mcp_tools": [
            {
                "name": "count",
                "description": "How many notes the project holds",
                "python": f"{HERE}:count",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
        "events": [{"name": "steps", "on": ["plan.step"], "python": f"{HERE}:on_step"}],
        "tables": [{"name": "notes_entries", "title": "The notes of each project"}],
        "pages": [{"name": "entries", "title": "Notes", "icon": "notebook-pen"}],
    },
}
MISSING_TABLE = {
    **MANIFEST,
    "id": "ghost",
    "contributes": {**MANIFEST["contributes"], "tables": [{"name": "ghost_entries"}], "routes": []},
}
# A core plugin with nothing per project, as Telegram: a channel of members and a route of its own.
HUB_WIDE = {
    "id": "beacon",
    "version": "1.0.0",
    "publisher": "evo-agents",
    "tier": "core",
    "name": "Beacon",
    "engines": {"evo-agents": ">=0.16"},
    "plugin_api": 1,
    "contributes": {
        "channels": [{"name": "beacon", "python": f"{HERE}:BeaconChannel"}],
        "routes": [{"name": "status", "python": f"{HERE}:beacon_router", "prefix": ""}],
    },
}
# The same, owning a table no migration creates.
HUB_WIDE_MISSING_TABLE = {
    "id": "phantom",
    "version": "1.0.0",
    "publisher": "evo-agents",
    "tier": "core",
    "name": "Phantom",
    "engines": {"evo-agents": ">=0.16"},
    "plugin_api": 1,
    "contributes": {"tables": [{"name": "phantom_links"}]},
}

router = APIRouter()
STEPS: list[dict] = []  # the plan.step events delivered, in order


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=200)


@router.get("/{project}/entries")
async def list_entries(request: Request, project: str, user: CurrentUser) -> list[dict]:
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        query = select(entries.c.body, entries.c.author).where(entries.c.project_id == access.project_id)
        rows = (await conn.execute(query.order_by(entries.c.id))).all()
    return [{"body": row.body, "author": row.author} for row in rows]


@router.post("/{project}/entries", status_code=201)
async def add_entry(request: Request, project: str, body: NoteIn, user: CurrentUser) -> dict:
    async with request.app.state.engine.begin() as conn:
        access = await project_access(conn, user, project)
        await conn.execute(insert(entries).values(project_id=access.project_id, body=body.body, author=user.login))
    return {"body": body.body, "author": user.login}


async def count(params: dict, context) -> dict:
    async with context.engine.begin() as conn:
        query = select(func.count()).select_from(entries).where(entries.c.project_id == context.project_id)
        found = (await conn.execute(query)).scalar_one()
    return {"content": [{"type": "text", "text": f"{found} notes in {params['caller']['project']}"}]}


async def on_step(params: dict, context) -> dict:
    STEPS.append(params)
    return {}


beacon_router = APIRouter(include_in_schema=False)


@beacon_router.get("/v1/me/beacon")
async def beacon_status(user: CurrentUser) -> dict:
    """The caller's beacon, for any member signed in: the route checks its caller, no project enables it."""
    return {"login": user.login}


class BeaconChannel(Channel):
    async def send(self, notification: Outgoing, config: dict) -> str | None:
        return None
