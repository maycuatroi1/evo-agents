"""The MCP tools plugins contribute, on the hub's /mcp beside its own (``evo_agents.hub.server.mcp``,
``docs/plugins.md``, section Extension points).

A plugin's tool is named ``<plugin>__<tool>`` (``plugin_calls.NAMESPACE``): the namespace keeps it apart from the
hub's tools and from another plugin's. It is listed, and answers, in a session whose project enabled the plugin, for
a caller who holds a grant there, while the plugin is installed and turned on. For the agent of a run, the run's agent
profile decides too: a tool axis of mode ``all`` lets the plugins' tools through, an allowlist only those of the
plugins it names as ``mcp:<plugin>``; the agent of a judge run gets no plugin's tool at all (``mcp.JUDGE_TOOLS``).
Anywhere else, the tool is not there: tools/list leaves it out and a call answers a tool error saying so.

Its description and input schema are those its installed version's manifest gives, which the plugin's publisher
signed; a backend cannot change them at run time, only a new version, signed and upgraded to by a hub admin, can.
Arguments are checked against the schema before the plugin sees them; the plugin gets the tool's name, the arguments
and the caller (login, project, role there) as plugin/1's ``tool`` params, and its result, checked against plugin/1,
becomes the tool's result.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from evo_agents.hub.plugin_calls import (
    NAMESPACE,
    CoreContext,
    PluginCallError,
    PluginHostUnavailable,
    call,
    enabled_plugins,
    manifest_of,
)
from evo_agents.hub.profiles import MCP_PREFIX
from evo_agents.hub.server.projects import project_access
from evo_agents.hub.server.runs.service.profiles import run_profile
from evo_agents.hub.server.security import Principal
from evo_agents.schema import errors, validate

log = logging.getLogger(__name__)

TOOL_TIMEOUT = 30.0
OBJECT = {"type": "object"}
SHOWN_PROBLEMS = 5


def tool_name(plugin: str, tool: str) -> str:
    return f"{plugin}{NAMESPACE}{tool}"


def split(name: str) -> tuple[str, str] | None:
    """(plugin, tool) of a plugin's tool name, or None for a name of the hub's own."""
    plugin, found, tool = name.partition(NAMESPACE)
    return (plugin, tool) if found and plugin and tool else None


def profile_allows(permissions: dict | None, plugin: str) -> bool:
    """Whether an agent profile's tool axis lets the tools of ``plugin`` through: mode all, or an allowlist naming
    ``mcp:<plugin>``; None (no profile) lets them through."""
    rule = (permissions or {}).get("tool") or {"mode": "all"}
    return rule.get("mode") != "allowlist" or f"{MCP_PREFIX}{plugin}" in (rule.get("allow") or [])


def described(plugin: str, item: dict) -> dict:
    """The tool as tools/list gives it, from the installed version's manifest."""
    return {
        "name": tool_name(plugin, item["name"]),
        "description": item.get("description") or item.get("title") or item["name"],
        "inputSchema": item.get("input_schema") or OBJECT,
    }


async def visible(state, user: Principal, project: str | None) -> tuple[str | None, list[tuple[object, dict]]]:
    """The caller's role in ``project`` and the plugins' tools it may see there, as (row of ``enabled_query``,
    contribution); none without a project or a grant, or for the agent of a judge run."""
    scope = user.scope
    if project is None or (scope is not None and scope.kind == "judge"):
        return None, []
    async with state.engine.begin() as conn:
        try:
            access = await project_access(conn, user, project)
        except HTTPException:
            return None, []
        if access.role is None:
            return None, []
        rows = await enabled_plugins(conn, access.project_id)
        profile = await run_profile(conn, scope.run_id) if scope is not None else None
    permissions = profile.permissions if profile is not None else None
    found = [
        (row, item)
        for row in rows
        if profile_allows(permissions, row.name)
        for item in manifest_of(row).contributions("mcp_tools")
    ]
    return access.role, found


async def listed(state, user: Principal, project: str | None) -> list[dict]:
    """The plugins' tools tools/list gives the caller in ``project``."""
    _, found = await visible(state, user, project)
    return [described(row.name, item) for row, item in found]


def _error(message: str) -> dict:
    return {"content": [{"type": "text", "text": f"error: {message}"}], "isError": True}


async def answer(state, user: Principal, project: str | None, name: str, arguments: dict) -> dict:
    """The result of a call to the plugin's tool ``name``, as MCP's tools/call gives it."""
    plugin, tool = split(name)
    role, found = await visible(state, user, project)
    hit = next(((row, item) for row, item in found if row.name == plugin and item["name"] == tool), None)
    if hit is None:
        where = f"project {project}" if project else "this session, which has no project"
        return _error(
            f"no tool {name} in {where}: the project does not enable plugin {plugin}, the plugin has no tool {tool}, "
            "or the run's agent profile leaves it out"
        )
    row, item = hit
    try:
        problems = errors(validate(arguments, item.get("input_schema") or OBJECT))
    except ValueError as exc:  # a schema of the plugin's that this validator does not read
        problems = []
        log.info("plugin tool schema not checked", extra={"contribution": f"{plugin}:{tool}", "error": str(exc)})
    if problems:
        shown = "; ".join(f"{i.path or 'arguments'}: {i.message}" for i in problems[:SHOWN_PROBLEMS])
        return _error(f"bad arguments for {name}: {shown}")
    params = {"tool": tool, "arguments": arguments, "caller": {"login": user.login, "project": project, "role": role}}
    context = CoreContext(plugin, state.engine, state.config, project, row.project_id)
    try:
        result = await call(state.plugin_host, plugin, row.tier, "mcp_tools", item, params, context, TOOL_TIMEOUT)
    except PluginHostUnavailable as exc:
        return _error(f"plugin {plugin} is unavailable now: {exc}")
    except PluginCallError as exc:
        return _error(f"plugin {plugin} failed: {exc}")
    return {"content": result["content"], "isError": bool(result.get("is_error"))}
