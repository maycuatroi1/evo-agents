"""The ground of the API of core plugins: the plugin's handle, transactions, the hub's config, the plugin's own config
and environment, its grants and audit rows, and what a clearance keeps out (``routes.callers`` holds the callers of its
routes)."""

from __future__ import annotations

import importlib.util
import os
import re
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from types import MappingProxyType

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import telegram
from evo_agents.hub.config import HubConfig
from evo_agents.hub.plugin_calls import ApiContext, core_registry, effective_grant, enabled_in
from evo_agents.hub.server import audit as audit_log
from evo_agents.hub.server.core_plugin_api.models.types import (
    Clearance,
    EgressSink,
    NotACorePlugin,
    Plugin,
    PluginConfig,
    ProjectConfig,
    ProjectRef,
    Refused,
    Session,
)
from evo_agents.hub.server.core_plugin_api.repository.ground import (
    config_versions,
    enabling_projects,
    project_id_of,
)
from evo_agents.hub.server.core_plugin_api.repository.ground import installed as installed_plugin
from evo_agents.hub.server.core_plugin_api.service.handles import (
    issue_plugin,
    own_clearance,
    own_plugin,
    own_session,
    session_principal,
)
from evo_agents.hub.server.credentials.models.sealing import Sealer
from evo_agents.hub.server.notifications.repository.channels import rules_of
from evo_agents.hub.server.notifications.service.member_channels import kept_from
from evo_agents.hub.server.plugins.service.extension import not_enabled
from evo_agents.hub.server.plugins.service.tokens import layer_config

NOT_INSTALLED = "plugin {plugin} is not installed on this hub, or a hub admin turned it off"
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


async def plugin_config(
    conn: AsyncConnection, plugin: Plugin, config: HubConfig, *, project: str | None = None
) -> PluginConfig:
    """The plugin's own effective config: the defaults of its schema, the hub's layer, then ``project``'s when given;
    its x-secret fields unsealed with the hub's key, those this process cannot open named in ``missing_secrets``. 404
    when the plugin is not installed and on, or ``project`` does not enable it."""
    own_plugin(plugin)
    installed = await installed_plugin(conn, plugin.id)
    if installed is None:
        raise Refused(404, NOT_INSTALLED.format(plugin=plugin.id))
    project_id = None
    if project is not None:
        project_id = await project_id_of(conn, project)
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
    installed = await installed_plugin(conn, plugin.id)
    if installed is None:
        return []
    sealer = Sealer.from_config(config) if config is not None else None
    schema = installed.manifest.get("config")
    versions = await config_versions(conn, installed.id)
    found = []
    for row in await enabling_projects(conn, plugin.id):
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
    project_id = await project_id_of(conn, project)
    row = None if project_id is None else await enabled_in(conn, plugin_id, project_id)
    return row is not None and permission in effective_grant(row)


async def restricted(conn: AsyncConnection, project: str | None, cleared: Clearance) -> bool:
    """Whether ``project`` is kept from a channel cleared for ``cleared``: none, no hub sink, a level or location the
    project's ladder lacks, or a hub sink above ``cleared`` in level or location (at or above, with ``below``), read as
    Telegram reads the ladder."""
    own_clearance(cleared)
    if project is None:
        return True
    rules = await rules_of(conn, project)
    return kept_from(rules, cleared.level, cleared.location, cleared.below)


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
