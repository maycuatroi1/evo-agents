"""The hub's own settings that a hub admin changes (criterion a11 of plan hub-agent-plugins): the branding of the hub
(``evo_agents.hub.branding``) and which features are on (``evo_agents.hub.features``), for the hub and for a project.
Table hub_settings, schema 0037.

Each setting is a layer of fields, versioned as a plugin's config is (``evo_agents.hub.server.plugin_configs``): a
write names the fields it sets and those it unsets, the others stay as the current version has them, and each write
that changes something makes the layer's next version, never changed after; a rollback makes the next version a copy
of an older one, which names it (``rolled_back_to``); ``if_version`` other than the version now is 409, nothing
changed. Version 0 is a layer never written. Writes of one layer wait for each other (a transaction-level advisory
lock). Each new version adds an audit row, settings.branding or settings.features, naming the layer, the version and
the fields, or the version it rolled back to, never a value.

Two kinds: branding, the hub's alone, and features, the hub's own layer and a project's. A feature is as a project's
layer sets it, else as the hub's sets it, else as its default (on, or off for canvas, srs and tasks:
``features.enabled``).

``FeatureGate`` answers 404 to a request for the API of a feature that is off where the request asks (for the project
its path names, or for the whole hub). It runs after Authenticate, so a request without a credential still gets its
401 and a route outside every feature costs nothing. When a feature is off for a project the caller cannot see, the
404 is the one ``project_access`` gives, which tells nothing of the project. ``require_tool`` does the same for the
tools of the hub's /mcp, and ``feature_on`` (``evo_agents.hub.feature_rule``) is the same rule as a SQL expression, for the jobs that leave out the
projects a feature is off for (the Curator's night shift, its night figures and its morning brief).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import psycopg
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, exists, func, insert, or_, select
from sqlalchemy import exc as sa_exc
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.requests import Request

from evo_agents.hub import features, tables
from evo_agents.hub.feature_rule import feature_on  # noqa: F401 (where the jobs read it, as before)
from evo_agents.hub.server import audit
from evo_agents.hub.server.errors import error_response
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.security import Principal
from evo_agents.hub.tables_settings import KINDS

log = logging.getLogger(__name__)

BRANDING, FEATURES = KINDS
MAX_FIELDS = 50  # fields one write sets or unsets
ACTIONS = {BRANDING: audit.SETTINGS_BRANDING, FEATURES: audit.SETTINGS_FEATURES}

# What is wrong with a write: given the fields it sets and the values the layer would have after it, each problem.
Check = Callable[[dict, dict], list[str]]


class SettingsLayer(BaseModel):
    """One layer of a setting at one version."""

    kind: Literal[KINDS]
    project: str | None = Field(description="the project of a project's layer; null for the hub's own")
    version: int = Field(description="0 while it was never written")
    values: dict[str, Any] = Field(description="the fields it sets")
    rolled_back_to: int | None = Field(description="the older version this one copies, for a rollback")
    changed_by: str | None
    changed_at: datetime | None


class SettingsWrite(BaseModel):
    """The next version of a layer: what ``values`` sets and ``unset`` clears; every other field stays."""

    model_config = ConfigDict(extra="forbid")

    values: dict[str, Any] = Field(default_factory=dict, max_length=MAX_FIELDS, description="field -> value")
    unset: list[str] = Field(default_factory=list, max_length=MAX_FIELDS, description="fields the layer sets now")
    if_version: int | None = Field(None, ge=0, description="409 unless the layer is at this version now")


class SettingsRollback(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1, description="the older version of the layer to copy as its next one")
    if_version: int | None = Field(None, ge=0, description="409 unless the layer is at this version now")


@dataclass(frozen=True)
class Place:
    """Where a layer lives: its kind, and the project of a project's layer (None for the hub's own)."""

    kind: str
    project_id: int | None = None
    project: str | None = None

    def clause(self):
        s = tables.hub_settings.c
        return and_(s.kind == self.kind, s.project_id.is_not_distinct_from(self.project_id))

    def target(self, version: int) -> str:
        return f"{self.project}/{self.kind} v{version}" if self.project else f"{self.kind} v{version}"

    def title(self) -> str:
        if self.kind == BRANDING:
            return "the hub's branding"
        return f"the layer of features of project {self.project}" if self.project else "the hub's layer of features"


# Reading a layer


def _versions_query(place: Place):
    s, u = tables.hub_settings, tables.users
    return (
        select(
            s.c.version,
            s.c["values"].label("plain"),
            s.c.rolled_back_to,
            u.c.login.label("changed_by"),
            s.c.changed_at,
        )
        .join_from(s, u, u.c.id == s.c.changed_by)
        .where(place.clause())
        .order_by(s.c.version.desc())
    )


def layer_model(place: Place, row) -> SettingsLayer:
    common = {"kind": place.kind, "project": place.project}
    if row is None:
        return SettingsLayer(**common, version=0, values={}, rolled_back_to=None, changed_by=None, changed_at=None)
    return SettingsLayer(
        **common,
        version=row.version,
        values=row.plain,
        rolled_back_to=row.rolled_back_to,
        changed_by=row.changed_by,
        changed_at=row.changed_at,
    )


async def current(conn: AsyncConnection, place: Place):
    """The current version of the layer at ``place`` (version, plain, ...); None when it was never written."""
    return (await conn.execute(_versions_query(place).limit(1))).first()


async def read_layer(conn: AsyncConnection, place: Place) -> SettingsLayer:
    return layer_model(place, await current(conn, place))


async def layer_versions(conn: AsyncConnection, place: Place) -> list[SettingsLayer]:
    """Every version of the layer, the newest first."""
    return [layer_model(place, row) for row in await conn.execute(_versions_query(place))]


# Writing a layer


async def _lock(conn: AsyncConnection, place: Place) -> None:
    """Hold, until the transaction ends, the lock that serialises the writes of the layer at ``place``."""
    key = f"hub_settings:{place.kind}:{place.project_id or ''}"
    await conn.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))


def _check_version(place: Place, now: int, if_version: int | None) -> None:
    if if_version is not None and if_version != now:
        raise HTTPException(409, f"{place.title()} is at version {now}, not {if_version}; nothing changed")


async def _insert(conn: AsyncConnection, user: Principal, place: Place, version: int, values: dict, back=None):
    row = {
        "kind": place.kind,
        "project_id": place.project_id,
        "version": version,
        "values": values,
        "rolled_back_to": back,
        "changed_by": user.user_id,
    }
    await conn.execute(insert(tables.hub_settings).values(row))


async def _audit(conn: AsyncConnection, user: Principal, place: Place, target: str) -> None:
    await audit.record(
        conn,
        actor_id=user.user_id,
        token_id=user.token_id,
        action=ACTIONS[place.kind],
        target=target,
        project_id=place.project_id,
    )


def _unset_problems(held_values: dict, sets: dict, unset: list[str]) -> list[str]:
    problems = []
    both = sorted(set(sets) & set(unset))
    if both:
        problems.append(f"{', '.join(both)}: both set and unset")
    if len(set(unset)) != len(unset):
        problems.append("unset names a field more than once")
    for name in sorted(set(unset) - set(held_values) - set(both)):
        problems.append(f"{name}: the layer does not set it, so there is nothing to unset")
    return problems


async def apply(
    conn: AsyncConnection,
    user: Principal,
    place: Place,
    sets: dict,
    unset: list[str],
    if_version: int | None,
    check: Check,
) -> SettingsLayer:
    """Write the fields ``sets`` sets and those ``unset`` clears as the next version of the layer at ``place``, once
    ``check`` finds nothing wrong (422 naming each problem otherwise); the layer as it is now."""
    await _lock(conn, place)
    held = await current(conn, place)
    now = 0 if held is None else held.version
    _check_version(place, now, if_version)
    values = dict(held.plain) if held is not None else {}
    after = {name: value for name, value in values.items() if name not in unset} | sets
    problems = _unset_problems(values, sets, unset) or check(sets, after)
    if problems:
        raise HTTPException(422, f"{'; '.join(problems)}; nothing changed")
    changed = sorted(name for name in set(sets) | set(unset) if values.get(name) != after.get(name))
    if not changed:
        return layer_model(place, held)
    await _insert(conn, user, place, now + 1, after)
    await _audit(conn, user, place, f"{place.target(now + 1)} fields={','.join(changed)}")
    log.info("hub setting changed", extra={"kind": place.kind, "project": place.project, "version": now + 1})
    return layer_model(place, await current(conn, place))


async def write_layer(
    conn: AsyncConnection, user: Principal, place: Place, body: SettingsWrite, check: Check
) -> SettingsLayer:
    return await apply(conn, user, place, body.values, body.unset, body.if_version, check)


async def rollback_layer(conn: AsyncConnection, user: Principal, place: Place, body: SettingsRollback) -> SettingsLayer:
    """Copy version ``body.version`` of the layer at ``place`` as its next version; the layer as it is now."""
    await _lock(conn, place)
    held = await current(conn, place)
    now = 0 if held is None else held.version
    _check_version(place, now, body.if_version)
    if body.version >= now:
        raise HTTPException(409, f"{place.title()} is at version {now}: roll back to an older one")
    older = (await conn.execute(_versions_query(place).where(tables.hub_settings.c.version == body.version))).first()
    if older is None:
        raise HTTPException(404, f"{place.title()} has no version {body.version}")
    await _insert(conn, user, place, now + 1, older.plain, back=body.version)
    await _audit(conn, user, place, f"{place.target(now + 1)} rollback-to=v{body.version}")
    log.info("hub setting rolled back", extra={"kind": place.kind, "project": place.project, "version": body.version})
    return layer_model(place, await current(conn, place))


async def project_place(conn: AsyncConnection, kind: str, project: str) -> Place:
    """The place of ``project``'s layer of ``kind``; 404 when no project of that name is registered."""
    p = tables.projects
    project_id = (await conn.execute(select(p.c.id).where(p.c.name == project))).scalar_one_or_none()
    if project_id is None:
        raise HTTPException(404, f"no project {project} on this hub: see `evo-agents hub project list`")
    return Place(kind, project_id, project)


# Features


def _features_query(project: str | None):
    """The current values of the hub's own layer of features and, when ``project`` is given, of that project's."""
    s, p = tables.hub_settings.c, tables.projects.c
    scope = s.project_id.is_(None)
    if project is not None:
        scope = or_(scope, s.project_id == select(p.id).where(p.name == project).scalar_subquery())
    return (
        select(s.project_id, s["values"].label("plain"))
        .where(s.kind == FEATURES, scope)
        .ext(distinct_on(s.project_id))
        .order_by(s.project_id, s.version.desc())
    )


async def feature_values(conn: AsyncConnection, project: str | None = None) -> tuple[dict, dict | None]:
    """(what the hub's own layer of features sets, what ``project``'s sets or None without one)."""
    instance, own = {}, None
    for row in await conn.execute(_features_query(project)):
        if row.project_id is None:
            instance = row.plain
        else:
            own = row.plain
    return instance, own


def project_layers_query(user: Principal):
    """The project, and the values, of the current version of each project's layer of features, for the projects
    ``user`` holds a grant on (every one for a hub admin)."""
    s, p, g = tables.hub_settings, tables.projects, tables.grants
    query = (
        select(p.c.name, s.c["values"].label("plain"))
        .join_from(s, p, p.c.id == s.c.project_id)
        .where(s.c.kind == FEATURES)
        .ext(distinct_on(s.c.project_id))
        .order_by(s.c.project_id, s.c.version.desc())
    )
    if not user.admin:
        query = query.where(exists().where(g.c.project_id == s.c.project_id, g.c.user_id == user.user_id))
    return query


def off_message(feature: features.Feature, project: str | None) -> str:
    if project is not None and not feature.default:  # canvas, srs, tasks: a project enables it, as when it was a plugin
        return (
            f"{feature.id} is not enabled in project {project}: an admin of the project enables it on the project's "
            f"Plugins page (POST .../plugins/{feature.id}/enable), or a hub admin in the administration, Features"
        )
    where = f" for project {project}" if project is not None else ""
    return f"{feature.id} is turned off{where} on this hub; a hub admin turns it on in the administration, Features"


async def refusal(conn: AsyncConnection, user: Principal, feature: features.Feature, project: str | None) -> str | None:
    """The message of the 404 that a request of ``user`` for ``feature`` (in ``project``, None for the whole hub)
    gets; None while the feature is on there."""
    project = project if feature.per_project() else None
    instance, own = await feature_values(conn, project)
    if features.enabled(feature, instance, own):
        return None
    if project is not None:
        try:
            await project_access(conn, user, project)
        except HTTPException as exc:  # a project the caller cannot see: its own 404, which tells nothing
            return str(exc.detail)
    return off_message(feature, project)


async def require_tool(engine: AsyncEngine, user: Principal, tool: str, project: str | None) -> None:
    """404 when ``tool`` of the hub's /mcp belongs to a feature that is off in ``project`` (or for the hub)."""
    feature = features.tool_owner(tool)
    if feature is None:
        return
    project = project if isinstance(project, str) else None  # a project of another type: its schema refuses it
    async with engine.begin() as conn:
        message = await refusal(conn, user, feature, project)
    if message is not None:
        raise HTTPException(404, message)


class FeatureGate:
    """Pure ASGI middleware, after Authenticate: a request for a route of a feature that is off where it asks gets
    404, in the shape of every error of the API."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        found = features.route_owner(scope.get("path", "")) if scope["type"] == "http" else None
        user = scope.get("state", {}).get("principal") if found is not None else None
        if user is None:  # outside every feature, or a request Authenticate let through without a caller
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        try:
            async with request.app.state.engine.begin() as conn:
                message = await refusal(conn, user, *found)
        except (psycopg.OperationalError, sa_exc.OperationalError, OSError) as exc:
            log.warning("cannot check a feature: database unavailable", extra={"error": type(exc).__name__})
            message, status = "the hub database is unavailable; try again shortly", 503
        else:
            status = 404
        if message is None:
            await self.app(scope, receive, send)
            return
        await error_response(request, status, message)(scope, receive, send)
