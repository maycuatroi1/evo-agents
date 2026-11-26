"""The signals of a project: who pushes a batch (an ingest key, or a member: ``push``), what a member reads of them
(``listed``, through ``readable``), and the registry of signal keys and the ingest keys, with who may read and change
each (the scopes below)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents.hub import signals
from evo_agents.hub.access import has_role
from evo_agents.hub.server import audit
from evo_agents.hub.server.base.errors import Conflict, NotFound, Unprocessable
from evo_agents.hub.server.base.scope import Noun, ProjectScope, Role, Scope
from evo_agents.hub.server.base.unit_of_work import UnitOfWork
from evo_agents.hub.server.projects import ProjectAccess, project_access
from evo_agents.hub.server.security import Principal
from evo_agents.hub.server.signals import repository
from evo_agents.hub.server.signals.models import (
    Signal,
    SignalIn,
    SignalIngestKey,
    SignalIngestKeyIn,
    SignalIngestKeyIssued,
    SignalKey,
    SignalKeyIn,
)

NOUN = Noun("a signal", "the signals")
READERS = ProjectScope("reader", NOUN)  # 404 without a grant (``project_access``), 403 for a hub admin without one


class KeysScope(ProjectScope):
    """Who may read or change the registry of keys or the ingest keys: a member who holds the role, the agent of a
    run too; the refusal names what they wanted (``what``) and the role they hold."""

    def __init__(self, role: Role, what: str):
        super().__init__(role, NOUN)
        self.what = what

    def refusal(self, user: Principal, access: ProjectAccess) -> str | None:
        if has_role(access.role, self.role):
            return None
        held = access.role or "no grant"
        return f"{self.what} of project {access.name} needs the {self.role} role on it; you hold {held}"


KEY_READERS = KeysScope("reader", "reading the signal keys")
KEY_ADDERS = KeysScope("admin", "adding a signal key")
KEY_ARCHIVERS = KeysScope("admin", "archiving a signal key")
INGEST_READERS = KeysScope("admin", "reading the ingest keys")
INGEST_CREATORS = KeysScope("admin", "creating an ingest key")
INGEST_REVOKERS = KeysScope("admin", "revoking an ingest key")


# Pushing


async def _member(engine: AsyncEngine, user: Principal, project: str) -> signals.Pusher:
    """The pusher of a batch a member sends: the writer role and a hub sink that takes the project's default label,
    as for any push."""
    async with UnitOfWork(engine) as conn:
        access = await project_access(conn, user, project)
        access.push_label(None)
    scope = user.scope
    via = "member" if scope is None else "run"
    run_id = None if scope is None else scope.run_id
    return signals.Pusher(access.project_id, access.rules, via, user_id=user.user_id, run_id=run_id)


async def push(
    engine: AsyncEngine, token: str | None, user: Principal | None, project: str, items: list[SignalIn], days: int
) -> signals.Pushed:
    """Store a batch of signals of the project, sent with ingest key ``token`` or by member ``user``, each item
    checked on its own; ``signals.SignalError`` for a refusal of the whole batch."""
    if token is not None:
        pusher = await signals.open_ingest(engine, token, project)
    else:
        assert user is not None
        pusher = await _member(engine, user, project)
    async with UnitOfWork(engine) as conn:
        return await signals.push(conn, pusher, items, days)


# Reading


def readable(access: ProjectAccess, sink: str | None) -> list[tuple[str, str]]:
    """The (level, location) pairs of the project's ladder that a label of this project with them may be read at,
    by the member, through ``sink`` or else the project's hub sink."""
    through = sink or (access.rules.hub_sink.sink if access.rules.hub_sink else None)
    if through is None:
        return []
    rules = access.rules
    return [
        (level, location)
        for level in rules.levels
        for location in rules.locations
        if access.visible({"level": level, "location": location, "integrity": "U", "projects": [rules.name]}, through)
    ]


async def listed(
    scope: Scope,
    sink: str | None,
    *,
    key: str | None,
    since: datetime | None,
    until: datetime | None,
    tenant: str | None,
    limit: int,
) -> list[Signal]:
    """The project's signals the member may read through ``sink`` (else the project's hub sink), newest first."""
    access = scope.access
    pairs = readable(access, sink)
    rows = await signals.signal_rows(
        scope.conn, access.project_id, pairs, key=key, since=since, until=until, tenant=tenant, limit=limit
    )
    through = sink or (access.rules.hub_sink.sink if access.rules.hub_sink else "")
    return [Signal(**row._mapping) for row in rows if access.visible(row.label, through)]


# The registry of keys


def _level(access: ProjectAccess, level: str | None, what: str) -> str | None:
    if level is not None and level not in access.rules.levels:
        levels = ", ".join(access.rules.levels)
        raise Unprocessable(f"{what} {level!r} is not a level of project {access.name} ({levels})")
    return level


async def keys(scope: Scope, archived: bool) -> list[SignalKey]:
    """The keys of the project's registry, by name; the archived ones too with ``archived``."""
    return [SignalKey(**row._mapping) for row in await repository.keys(scope.conn, scope.access.project_id, archived)]


async def add_key(scope: Scope, body: SignalKeyIn) -> SignalKey:
    """Add a key to the project's registry; 409 when the name is taken, archived or not."""
    conn, access = scope.conn, scope.access
    level = _level(access, body.level, "level")
    held = await repository.key_held(conn, access.project_id, body.name)
    if held is not None:
        archived = ", archived" if held.archived_at else ""
        raise Conflict(f"project {access.name} has a signal key {body.name} already{archived}")
    values = body.model_dump(exclude={"level"}) | {"level": level}
    await repository.add_key(conn, access.project_id, scope.user.user_id, values)
    await scope.auditor.record(audit.SIGNAL_KEY_ADD, f"{access.name} key={body.name}")
    row = await repository.key(conn, access.project_id, body.name)
    assert row is not None  # written above
    return SignalKey(**row._mapping)


async def archive_key(scope: Scope, name: str) -> SignalKey:
    """Archive key ``name``: it takes no new signal, and its signals stay; again, it changes nothing."""
    conn, access = scope.conn, scope.access
    if await repository.archive_key(conn, access.project_id, name, scope.user.user_id):
        await scope.auditor.record(audit.SIGNAL_KEY_ARCHIVE, f"{access.name} key={name}")
    row = await repository.key(conn, access.project_id, name)
    if row is None:
        raise NotFound(f"project {access.name} has no signal key {name}: see `evo-agents hub signal keys list`")
    return SignalKey(**row._mapping)


# Ingest keys


async def ingest_keys(scope: Scope, revoked: bool) -> list[SignalIngestKey]:
    """The project's ingest keys, oldest first, the revoked ones too with ``revoked``; never a token."""
    rows = await repository.ingest_keys(scope.conn, scope.access.project_id, revoked)
    return [SignalIngestKey(**row._mapping) for row in rows]


async def create_ingest_key(scope: Scope, body: SignalIngestKeyIn) -> SignalIngestKeyIssued:
    """A new ingest key for a service, with its token; 409 when the project has a live key of that name."""
    conn, access = scope.conn, scope.access
    max_level = _level(access, body.max_level, "max_level") or access.rules.default_label.get("level")
    if await repository.live_ingest_key(conn, access.project_id, body.name):
        raise Conflict(f"project {access.name} has a live ingest key {body.name}: revoke it or pick a name")
    token = signals.new_ingest_token()
    values = {"project_id": access.project_id, "name": body.name, "max_level": max_level}
    key_id = await repository.add_ingest_key(conn, scope.user.user_id, values, signals.hash_token(token))
    await scope.auditor.record(audit.SIGNAL_INGEST_CREATE, f"{access.name} ingest:{key_id} name={body.name}")
    row = await repository.ingest_key(conn, access.project_id, key_id)
    assert row is not None  # written above
    return SignalIngestKeyIssued(**row._mapping, token=token)


async def revoke_ingest_key(scope: Scope, key_id: int) -> SignalIngestKey:
    """Revoke ingest key ``key_id``: the next batch sent with it gets 401; again, it changes nothing."""
    conn, access = scope.conn, scope.access
    name = await repository.revoke_ingest_key(conn, access.project_id, key_id, scope.user.user_id)
    if name is not None:
        await scope.auditor.record(audit.SIGNAL_INGEST_REVOKE, f"{access.name} ingest:{key_id} name={name}")
    row = await repository.ingest_key(conn, access.project_id, key_id)
    if row is None:
        raise NotFound(f"project {access.name} has no ingest key {key_id}: see `evo-agents hub signal ingest list`")
    return SignalIngestKey(**row._mapping)
