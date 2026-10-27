"""The ground of the API of core plugins: the plugin's handle, the callers of its routes, transactions, the hub's
config, the plugin's own config and environment, its grants and audit rows, and what a clearance keeps out. Private to
``core_plugin_api``."""

from __future__ import annotations

import importlib.util
import os
import re
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from types import MappingProxyType

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables, telegram
from evo_agents.hub.config import HubConfig
from evo_agents.hub.plugin_calls import ApiContext, core_registry, effective_grant, enabled_in, enabled_query
from evo_agents.hub.server import audit as audit_log
from evo_agents.hub.server import member_channels
from evo_agents.hub.server.core_plugin_api._types import (
    Clearance,
    EgressSink,
    NotACorePlugin,
    Plugin,
    PluginConfig,
    ProjectConfig,
    ProjectRef,
    Refused,
    Session,
    issue_plugin,
    issue_session,
    own_clearance,
    own_plugin,
    own_session,
    session_principal,
)
from evo_agents.hub.server.plugin_routes import not_enabled
from evo_agents.hub.server.plugin_tokens import layer_config
from evo_agents.hub.server.sealing import Sealer
from evo_agents.hub.server.security import UNSAFE_METHODS, WEB, AdminUser, CurrentUser
from evo_agents.hub.server.terminal import origin_of

NOT_INSTALLED = "plugin {plugin} is not installed on this hub, or a hub admin turned it off"
NOT_FROM_THIS_HUB = (
    "a write made with a web session comes from a page of this hub: its Origin header must be {origin}, the origin of "
    "EVO_HUB_PUBLIC_URL"
)
NO_PUBLIC_URL = "this hub has no EVO_HUB_PUBLIC_URL, so it cannot tell its own pages' writes from another site's"
AUDIT_WORD = re.compile(r"[a-z][a-z0-9_]{0,63}")  # what follows the family of a plugin's audit action
MAX_AUDIT_TARGET = 1024
HUB_FAMILIES = audit_log.NAMED_FAMILIES | {"memory", "skill"}  # families whose target names a project or a memory

# The plugin


def _package_of(value: str) -> str:
    """The package of the module entry point ``value`` names: that module when it is a package, else its parent."""
    module = value.partition(":")[0]
    spec = importlib.util.find_spec(module)
    if spec is not None and spec.submodule_search_locations is not None:
        return module
    return module.rpartition(".")[0] or module


def plugin(module: str) -> Plugin:
    """The core plugin whose package holds ``module`` (a plugin passes ``__name__``): the package of the module its
    entry point names, or a module below it. NotACorePlugin when no core plugin of this process has it."""
    best = None
    for found in core_registry().plugins:
        package = _package_of(found.value)
        if (module == package or module.startswith(package + ".")) and (best is None or len(package) > len(best[0])):
            best = (package, found.manifest)
    if best is None:
        raise NotACorePlugin(f"module {module} is in the package of no core plugin of this process")
    manifest = best[1]
    egress = tuple(EgressSink(item["id"], item["kind"], item["host"]) for item in manifest.egress)
    return issue_plugin(manifest.id, tuple(item["name"] for item in manifest.contributions("channels")), egress)


# The callers of a plugin's routes


def _from_this_hub(request: Request) -> None:
    """403 unless a write carries the Origin of the hub's public URL."""
    if request.method not in UNSAFE_METHODS:
        return
    expected = origin_of(request.app.state.config.public_url)
    if expected is None:
        raise Refused(403, NO_PUBLIC_URL)
    if origin_of(request.headers.get("origin")) != expected:
        raise Refused(403, NOT_FROM_THIS_HUB.format(origin=expected))


def web_session(request: Request, user: CurrentUser) -> Session:
    """The caller's web session: 403 for any other credential, and for a write whose Origin is not the hub's."""
    if user.kind != WEB:
        raise Refused(403, member_channels.FROM_SESSION)
    _from_this_hub(request)
    return issue_session(user.user_id, user.login, user.token_id, True, principal=user)


def caller(user: CurrentUser) -> Session:
    """The caller, a member signed in with any credential the hub's own routes take: ``web`` for a web session, which
    alone links a channel. The hub's middleware has checked a web session's write for its CSRF header."""
    return issue_session(user.user_id, user.login, user.token_id, user.kind == WEB, principal=user)


def hub_admin(request: Request, user: AdminUser) -> Session:
    """The caller, a hub admin (403 otherwise, as the hub's admin routes answer), with a web session or a machine
    token; a write made with a web session carries the Origin of the hub's public URL."""
    if user.kind == WEB:
        _from_this_hub(request)
    return issue_session(user.user_id, user.login, user.token_id, user.kind == WEB, admin=True, principal=user)


def begin(request: Request) -> AbstractAsyncContextManager[AsyncConnection]:
    """A transaction on the hub's engine, for a route of the plugin."""
    return request.app.state.engine.begin()


def hub_config(request: Request) -> HubConfig:
    """The hub's config, which a plugin hands the API and never reads."""
    return request.app.state.config


def web_url(config: HubConfig, link: str | None) -> str | None:
    """The absolute URL of the hub's page a notification links to, when the hub knows its public URL."""
    return telegram.web_url(getattr(config, "public_url", None), link)


Context = ApiContext  # the hub hands plugins a CoreContext, a subclass whose fields past these are not the API's


def transaction(context: Context) -> AbstractAsyncContextManager[AsyncConnection]:
    """A transaction on the engine of the context the hub gave a job, an event, a tool or a source of the graph."""
    if not isinstance(context, ApiContext) or context.engine is None:
        raise ValueError("this context holds no engine of the hub")
    return context.engine.begin()


def plugin_environment(plugin: Plugin) -> Mapping[str, str]:
    """The variables of this process's environment named for the plugin, ``EVO_HUB_<ID>_*`` (its id in upper case, -
    as _), read now."""
    prefix = "EVO_HUB_" + own_plugin(plugin).id.upper().replace("-", "_") + "_"
    return MappingProxyType({name: value for name, value in os.environ.items() if name.startswith(prefix)})


# The plugin's config and grants


async def _installed(conn: AsyncConnection, name: str):
    p, v = tables.plugins, tables.plugin_versions
    query = (
        select(p.c.id, v.c.manifest)
        .join_from(p, v, (v.c.plugin_id == p.c.id) & (v.c.version == p.c.installed_version))
        .where(p.c.name == name, p.c.disabled_reason.is_(None))
    )
    return (await conn.execute(query)).one_or_none()


async def _project_id(conn: AsyncConnection, project: str) -> int | None:
    p = tables.projects
    return (await conn.execute(select(p.c.id).where(p.c.name == project))).scalar_one_or_none()


async def plugin_config(
    conn: AsyncConnection, plugin: Plugin, config: HubConfig, *, project: str | None = None
) -> PluginConfig:
    """The plugin's own effective config: the defaults of its schema, the hub's layer, then ``project``'s when given;
    its x-secret fields unsealed with the hub's key, those this process cannot open named in ``missing_secrets``. 404
    when the plugin is not installed and on, or ``project`` does not enable it."""
    own_plugin(plugin)
    installed = await _installed(conn, plugin.id)
    if installed is None:
        raise Refused(404, NOT_INSTALLED.format(plugin=plugin.id))
    project_id = None
    if project is not None:
        project_id = await _project_id(conn, project)
        if project_id is None or await enabled_in(conn, plugin.id, project_id) is None:
            failed = not_enabled(plugin.id, project)
            raise Refused(failed.status_code, failed.detail)
    sealer = Sealer.from_config(config) if config is not None else None
    schema = installed.manifest.get("config")
    values, missing = await layer_config(conn, sealer, installed.id, plugin.id, schema, project_id, project)
    return PluginConfig(MappingProxyType(dict(values)), tuple(missing))


async def project_configs(conn: AsyncConnection, plugin: Plugin, config: HubConfig) -> list[ProjectConfig]:
    """The plugin's effective config in each project that enables it, by project name: what a job that works across
    projects reads once. Empty when the plugin is not installed and on."""
    own_plugin(plugin)
    installed = await _installed(conn, plugin.id)
    if installed is None:
        return []
    sealer = Sealer.from_config(config) if config is not None else None
    schema = installed.manifest.get("config")
    query = enabled_query().where(tables.plugins.c.name == plugin.id).order_by(tables.projects.c.name)
    c = tables.plugin_configs.c
    versions = dict(
        (
            await conn.execute(
                select(c.project_id, func.max(c.version))
                .where(c.plugin_id == installed.id, c.layer == "project", c.profile_id.is_(None))
                .group_by(c.project_id)
            )
        ).all()
    )
    found = []
    for row in (await conn.execute(query)).all():
        values, missing = await layer_config(conn, sealer, installed.id, plugin.id, schema, row.project_id, row.project)
        found.append(
            ProjectConfig(row.project, MappingProxyType(dict(values)), versions.get(row.project_id), tuple(missing))
        )
    return found


async def granted(conn: AsyncConnection, plugin: Plugin, project: str, permission: str) -> bool:
    """Whether ``project`` enables the plugin and its grant there, granted and still asked for, holds
    ``permission``."""
    own_plugin(plugin)
    return await project_grants(conn, plugin.id, project, permission)


async def project_grants(conn: AsyncConnection, plugin_id: str, project: str, permission: str) -> bool:
    project_id = await _project_id(conn, project)
    row = None if project_id is None else await enabled_in(conn, plugin_id, project_id)
    return row is not None and permission in effective_grant(row)


async def restricted(conn: AsyncConnection, project: str | None, cleared: Clearance) -> bool:
    """Whether ``project`` is kept from a channel cleared for ``cleared``: none, no hub sink, a level or location the
    project's ladder lacks, or a hub sink above ``cleared`` in level or location (at or above, with ``below``), read as
    Telegram reads the ladder."""
    own_clearance(cleared)
    if project is None:
        return True
    rules = await member_channels.rules_of(conn, project)
    return member_channels.kept_from(rules, cleared.level, cleared.location, cleared.below)


# The audit log


async def audit(
    conn: AsyncConnection,
    plugin: Plugin,
    actor: Session,
    action: str,
    target: str,
    *,
    project: ProjectRef | None = None,
) -> None:
    """Record in the hub's audit log that ``actor`` (from ``WebSession``, ``Caller`` or ``HubAdmin``) did ``action``,
    ``<the plugin's id or one of its kinds>.<word>``, to ``target``, one line of at most 1 KiB that holds no secret:
    hub admins read it. With ``project`` (from ``project_access``) the row is of that project."""
    own_plugin(plugin)
    own_session(actor)
    family, _, word = action.partition(".") if isinstance(action, str) else ("", "", "")
    if family not in (plugin.id, *plugin.kinds) or family in HUB_FAMILIES or not AUDIT_WORD.fullmatch(word):
        raise ValueError(f"a plugin's audit action is {plugin.id}.<word> or <a kind of its channels>.<word>")
    if not isinstance(target, str) or len(target) > MAX_AUDIT_TARGET or "\n" in target or "\r" in target:
        raise ValueError(f"an audit target is one line of at most {MAX_AUDIT_TARGET} characters")
    principal = session_principal(actor)
    if project is not None and not isinstance(project, ProjectRef):
        raise ValueError("an audit's project comes from project_access")
    await audit_log.record(
        conn,
        actor_id=actor.user_id,
        token_id=principal.token_id,
        action=action,
        target=target,
        project_id=None if project is None else project.id,
    )
