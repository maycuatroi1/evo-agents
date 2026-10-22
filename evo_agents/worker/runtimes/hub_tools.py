"""The hub's MCP server for the agent of a run of the Curator, whatever the worker's machine has configured (finding #65
of evo-agents, proposal #28): the prompt of a review run names the hub's tools, and a worker that has no evo-hub plugin
gave its agent none of them, so each night the agent wrote its own bridge to ``evo-agents hub mcp`` before it read
anything. A task run of a plugin's agent type is the same (plan loop-engine, the checkpoint of step 1, answer 7): its
task names the plugin's tools (``canvas__*``, ``srs__*``), which a run on Codex had no way to call.

A run whose claim names a Curator role in ROLES (``context.run["curator"]["role"]``: the Reviewer, the Builder, the
Judge; a replay run and a history run call none of the hub's tools, ``mcp_tools.RUN_TOOLS``), or a task run (kind
TASK_KIND, always of an agent type) or a question run (QUESTION_KIND, whose prompt names the hub's tools), and whose
agent runs on this machine (not in a container of a plugin's environment) gets SERVER, the stdio proxy ``evo-agents hub
mcp`` of this install (``COMMAND``: this interpreter, isolated, so a checkout of evo_agents in the worktree is never imported). Inside
a run the proxy speaks with the worker's token and X-Evo-Run (``evo_agents.hub.mcp_proxy``), so the machine needs no
``hub login``; the hub answers the run's project alone and shows its agent only what it may call. The server gets the
run's EVO_RUN_ID and EVO_WORKER_HOME in its environment, which Codex does not pass to a server by itself.

It is left out when the session would have the hub's tools already, so they are not offered twice: Claude Code with the
evo-hub plugin on and installed (``claude_plugins.plugin_on``), or a server named SERVER in the user's ``.claude.json``;
Codex with a server of that name in its config; and when the run's agent profile allows no such server.

The agent of a claude-code run with a config dir of its own (``claude_home``), of any kind, gets SERVER in that dir's
``.claude.json`` (``home_server``) unless it runs in a container or its profile allows no such server.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

from evo_agents.hub import claude_plugins
from evo_agents.hub.mcp_tools import SERVER_NAME
from evo_agents.worker.adapter import RunContext
from evo_agents.worker.environments import Container

SERVER = SERVER_NAME  # evo-hub, as the plugin names its server
PLUGIN = "evo-hub"
ROLES = ("reviewer", "builder", "judge")  # the Curator's runs whose agent calls the hub's tools
TASK_KIND = "task"  # runs.RUN_KINDS: a task run of an agent type, whose agent calls its plugin's tools
QUESTION_KIND = "question"  # and a question run of one, whose agent reads the hub as a reader
PASSED = ("EVO_RUN_ID", "EVO_WORKER_HOME", "EVO_RUN_PROJECT")  # what the proxy reads inside a run
COMMAND = (sys.executable or "python3", "-I", "-m", "evo_agents", "hub", "mcp")


def wanted(context: RunContext, profile=None) -> bool:
    """Whether the agent of ``context`` gets the hub's MCP server from its adapter (see the module), before what the
    runtime's own configuration holds is looked at."""
    curator = context.run.get("curator")
    role = curator.get("role") if isinstance(curator, Mapping) else None
    typed = context.run.get("kind") in (TASK_KIND, QUESTION_KIND)
    if (role not in ROLES and not typed) or isinstance(context.environment, Container):
        return False
    return profile is None or profile.allows_server(SERVER)


def _env(context: RunContext) -> dict[str, str]:
    return {name: str(context.env[name]) for name in PASSED if context.env.get(name)}


def _claude_configured(env: Mapping[str, str], cwd: Path) -> bool:
    """Whether Claude Code would give the session a hub server already: the evo-hub plugin, or one by hand."""
    if claude_plugins.plugin_on(PLUGIN, env, cwd):
        return True
    configured = env.get("CLAUDE_CONFIG_DIR")
    path = Path(configured) / ".claude.json" if configured else Path(env.get("HOME") or Path.home()) / ".claude.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return isinstance(servers, dict) and SERVER in servers


def claude_servers(context: RunContext, profile=None) -> dict:
    """What ``ClaudeAgentOptions.mcp_servers`` gains for the agent of ``context``: SERVER, or nothing."""
    if not wanted(context, profile) or _claude_configured(context.env, Path(context.worktree)):
        return {}
    command, *args = COMMAND
    return {SERVER: {"type": "stdio", "command": command, "args": args, "env": _env(context)}}


def home_server(context: RunContext, profile=None) -> dict | None:
    """SERVER as the ``.claude.json`` of a claude-code agent's config dir of its own names it (``claude_home``), for
    a run of any kind: None when the agent runs in a container, or the run's profile allows no such server."""
    if isinstance(context.environment, Container) or not (profile is None or profile.allows_server(SERVER)):
        return None
    command, *args = COMMAND
    return {"type": "stdio", "command": command, "args": args, "env": _env(context)}


def codex_servers(context: RunContext, configured: Iterable[str], profile=None) -> dict:
    """What the ``mcp_servers`` of a Codex thread's config gains for the agent of ``context``: SERVER, unless one of
    the servers the owner's or the worktree's codex config names (``configured``) is it already."""
    if not wanted(context, profile) or SERVER in set(configured):
        return {}
    command, *args = COMMAND
    return {SERVER: {"command": command, "args": args, "env": _env(context)}}
