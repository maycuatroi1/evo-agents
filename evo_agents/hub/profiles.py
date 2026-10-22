"""Agent profiles: who an agent of a project is (handle, name, avatar, colour, description, instructions) and what its
runs may use, on eight axes (``docs/hub.md``, section "Agent profiles").

A profile belongs to a project, whose admins manage it; every member of the project reads it. Its handle is unique on
the hub and never equal to the name of a project, except for the project's default profile, whose handle is the
project's name (``@<project>``): every project has one, with every permission and no instructions, and a run whose
dispatch names no profile runs under it, as runs ran before profiles existed. A profile only narrows what the member
who dispatches a run has (their workers, their secrets, the projects they write to), with one exception: the accounts
of the agent itself (``evo_agents.hub.server.agent_accounts``), env secrets the profile owns, which its runs are leased
on the workers they are bound to in place of the dispatcher's secrets for the same variables, whatever the
credential axis says.

Each axis is ``{"mode": "all"}`` or ``{"mode": "allowlist", "allow": [...]}``; ``all`` is the default, and an
allowlist never grows by itself when a secret, a skill or a worker is added. What an allowlist names, by axis:

- ``credential``: secret names, and ``github-app`` for the token of the hub's GitHub App;
- ``tool``: the words of TOOLS (``read``, ``edit``, ``bash``, ``web``, ``agent``) and ``mcp:<server>``;
- ``skill``: skill names, global or of the project, compared ignoring case;
- ``worker``: worker names, or ``label:<label>`` for every worker carrying that label, compared ignoring case;
- ``project``: project names, the other projects whose runs the profile may take (its own always);
- ``model``: model names, as a runtime names them;
- ``runtime``: ``claude-code``, ``opencode``, ``codex``, and the runtimes of plugins, ``<plugin>/<name>``
  (``evo_agents.plugins.runtimes``);
- ``environment``: where the agent runs, in order of preference: ``host``, the worker's own machine, and the
  environments of plugins, ``<plugin>/<name>``, each a container (``evo_agents.plugins.run_environments``). An axis of
  mode all runs on the host, as every run ran before the axis; so does a profile version written before it, which
  names seven axes (``rule``).

The hub enforces credential, skill, worker, project, model, runtime and environment when it dispatches, hands out and
leases a run (a claim picks the first environment of the allowlist the claiming worker takes); the worker's adapter
enforces tool, skill and model in the agent session, from the profile the claim carries (``RunSpec.profile``), and
its container provider the environment (``evo_agents.worker.environments``). A worker whose daemon does not say it does (``runs.AGENT_PROFILES``) never claims a run whose
profile limits tool, skill or model, or adds instructions. Every edit of a profile raises its version; a run records
the profile and version it was dispatched under, and the hub applies that version's rules to it.

A profile may also attach agent packs of plugins (``evo_agents.plugins.agent_packs``), at most MAX_PACKS, each pinned at
a version of its plugin: ``{"plugin", "name", "version"}``. A pack it attaches counts as named on its skill and tool
axes: the skills of the pack pass the skill axis and its MCP servers the tool axis (``mcp:<server>``), since pinning
the pack at a version is the admin's consent to what that version holds; the axes still limit everything else, and a
pack widens no other axis. Only a worker whose daemon builds packs (``runs.AGENT_PACKS``) and whose owner accepted
each plugin takes such a run (``packs_unfit``).

Standard library only: the command line and the worker import this module on a core install.
"""

from __future__ import annotations

import hashlib
import re

from evo_agents.hub.runs import AGENT_PACKS, MAX_MODEL_CHARS, RUNTIMES
from evo_agents.plugins.run_environments import HOST, is_environment
from evo_agents.plugins.runtimes import is_plugin_runtime

AXES = ("credential", "tool", "skill", "worker", "project", "model", "runtime", "environment")
MODES = ("all", "allowlist")
ALL, ALLOWLIST = MODES

HANDLE_PATTERN = r"^[a-z0-9][a-z0-9-]{0,99}$"  # a project name's, since a default profile's handle is one
HANDLE = re.compile(HANDLE_PATTERN[1:-1])
MAX_NAME_CHARS = 80
MAX_DESCRIPTION_CHARS = 1000
MAX_INSTRUCTIONS_CHARS = 8000
MAX_ALLOW = 200  # items in one allowlist
MAX_PACKS = 16  # agent packs one profile attaches; the check agent_profiles_packs_check of schema 0041 holds it too
COLOR_PATTERN = r"^#[0-9a-f]{6}$"
DEFAULT_COLOR = "#475569"  # the default profile of every project
PALETTE = ("#2563eb", "#7c3aed", "#db2777", "#dc2626", "#d97706", "#16a34a", "#0891b2", "#4f46e5")

GITHUB_APP = "github-app"  # the credential allowlist's name for a token of the hub's GitHub App
MCP_PREFIX = "mcp:"
LABEL_PREFIX = "label:"

# The runtime-neutral words of the tool axis, and what each covers. ``mcp:<server>`` covers the tools of one MCP
# server. Tools that only keep the agent's own notes (a to-do list) are always there. Each runtime's adapter turns the
# words into its own tool names; a word it cannot turn off fails a run whose profile leaves the word out.
TOOLS = {
    "read": "read files and search them",
    "edit": "create and change files",
    "bash": "run shell commands",
    "web": "fetch web pages and search the web",
    "agent": "start subagents",
}
_SECRET = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")  # evo_agents.hub.credentials.SECRET_NAME
_SKILL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")  # evo_agents.hub.skills.NAME
_WORKER = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}")  # a worker's name, lowered
_LABEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")  # a worker's label, lowered
_MCP_SERVER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_LINE = re.compile(r"[^\x00-\x1f\x7f]+")


def all_access() -> dict:
    """The permissions of a default profile, and of a profile no axis of which was set: every axis ``all``."""
    return {axis: {"mode": ALL} for axis in AXES}


def default_color(handle: str) -> str:
    """The colour of a profile created without one: a colour of PALETTE picked by its handle."""
    return PALETTE[hashlib.sha256(handle.encode()).digest()[0] % len(PALETTE)]


def _tool_problem(item: str) -> str | None:
    if item in TOOLS:
        return None
    if item.startswith(MCP_PREFIX) and _MCP_SERVER.fullmatch(item[len(MCP_PREFIX) :]):
        return None
    return f"tool {item!r} is none of {', '.join(TOOLS)} or mcp:<server>"


def _worker_problem(item: str) -> str | None:
    if item.startswith(LABEL_PREFIX):
        return None if _LABEL.fullmatch(item[len(LABEL_PREFIX) :]) else f"{item!r} is not label:<a worker label>"
    return None if _WORKER.fullmatch(item) else f"{item!r} is not a worker's name"


def item_problem(axis: str, item: str) -> str | None:
    """Why ``item`` cannot be in the allowlist of ``axis``, as normalized (``normalized``); None when it can."""
    if axis == "credential":
        return None if _SECRET.fullmatch(item) else f"{item!r} is not a secret's name (or {GITHUB_APP})"
    if axis == "tool":
        return _tool_problem(item)
    if axis == "skill":
        return None if _SKILL.fullmatch(item) else f"{item!r} is not a skill's name"
    if axis == "worker":
        return _worker_problem(item)
    if axis == "project":
        return None if HANDLE.fullmatch(item) else f"{item!r} is not a project's name"
    if axis == "model":
        fits = len(item) <= MAX_MODEL_CHARS and _LINE.fullmatch(item)
        return None if fits else f"{item!r} is not a model's name (one line, at most {MAX_MODEL_CHARS} characters)"
    if axis == "runtime":
        if item in RUNTIMES or is_plugin_runtime(item):
            return None
        return f"runtime {item!r} is none of {', '.join(RUNTIMES)}, nor a plugin's <plugin>/<name>"
    if axis == "environment":
        return (
            None if is_environment(item) else f"environment {item!r} is neither {HOST} nor a plugin's <plugin>/<name>"
        )
    return f"{axis!r} is no axis: {', '.join(AXES)}"


def normalized(axis: str, item: str) -> str:
    """``item`` as an allowlist of ``axis`` keeps it: worker names and labels lowered, as the hub compares them."""
    item = item.strip()
    return item.lower() if axis == "worker" else item


def problems(permissions: dict) -> list[str]:
    """Every reason ``permissions`` (``{axis: {"mode", "allow"}}``, each axis present) is not a profile's; empty when
    it is one."""
    found = [f"{axis!r} is no axis: {', '.join(AXES)}" for axis in permissions if axis not in AXES]
    for axis in AXES:
        given = permissions.get(axis)
        if not isinstance(given, dict) or given.get("mode") not in MODES:
            found.append(f"{axis}: mode is all or allowlist")
            continue
        allow = given.get("allow", [])
        if given["mode"] == ALL and allow:
            found.append(f"{axis}: an axis of mode all takes no allow list")
        elif len(allow) > MAX_ALLOW:
            found.append(f"{axis}: at most {MAX_ALLOW} items")
        found += [f"{axis}: {why}" for why in filter(None, (item_problem(axis, item) for item in allow))]
    return found


def rule(permissions: dict | None, axis: str) -> dict:
    """The rule of ``axis`` in ``permissions``: ``{"mode": "all"}`` for an axis they do not name (a profile version
    written before the axis existed) or for a run without a profile."""
    found = (permissions or {}).get(axis)
    return found if isinstance(found, dict) else {"mode": ALL}


def limited(permissions: dict | None, axis: str) -> bool:
    """Whether ``axis`` of ``permissions`` is an allowlist; never for a run without a profile."""
    return rule(permissions, axis).get("mode") == ALLOWLIST


def allowed(permissions: dict | None, axis: str) -> list[str] | None:
    """The allowlist of ``axis``; None when the axis allows all (or there is no profile)."""
    return list(rule(permissions, axis).get("allow", [])) if limited(permissions, axis) else None


def allows(permissions: dict | None, axis: str, item: str) -> bool:
    """Whether ``axis`` of ``permissions`` lets ``item`` through: always for an axis of mode all; for an allowlist,
    when it names ``item`` (ignoring case for skills and workers)."""
    allow = allowed(permissions, axis)
    if allow is None:
        return True
    if axis in ("skill", "worker"):
        return item.lower() in {entry.lower() for entry in allow}
    return item in allow


def allows_worker(permissions: dict | None, name: str, labels) -> bool:
    """Whether the worker axis lets a worker named ``name`` with ``labels`` take a run."""
    allow = allowed(permissions, "worker")
    if allow is None:
        return True
    wanted = {name.lower(), *(LABEL_PREFIX + label.lower() for label in labels or ())}
    return bool(wanted & {entry.lower() for entry in allow})


def needs_worker(permissions: dict | None, instructions: str | None) -> bool:
    """Whether a run of the profile needs a worker whose adapter applies profiles (``runs.AGENT_PROFILES``): its
    profile limits tool, skill or model, or has instructions."""
    return any(limited(permissions, axis) for axis in ("tool", "skill", "model")) or bool(instructions)


def restricted_axes(permissions: dict | None) -> list[str]:
    """The axes of ``permissions`` that are allowlists, in AXES order."""
    return [axis for axis in AXES if limited(permissions, axis)]


def pack_plugins(packs: list | None) -> list[str]:
    """The plugins whose agent packs ``packs`` (a profile's, each ``{"plugin", "name", "version"}``) attach, sorted."""
    return sorted({pack["plugin"] for pack in packs or ()})


def packs_unfit(packs: list | None, *, worker_id: int, agent_version: str | None, features, plugins) -> str | None:
    """What keeps a worker (as its last heartbeat reported it, and its owner set it) from ever claiming a run whose
    profile attaches ``packs``; None when nothing does, and always for a profile that attaches none."""
    if not packs:
        return None
    if AGENT_PACKS not in set(features or ()):
        return (
            f"its daemon (evo-agents {agent_version or 'of an unknown version'}) does not say it builds agent packs, "
            "and the run's agent attaches some: upgrade it and restart its daemon"
        )
    missing = [name for name in pack_plugins(packs) if name not in set(plugins or ())]
    if missing:
        return (
            f"its owner has not let it take the agent packs of plugin {', '.join(missing)}: add it to the worker's "
            f"plugins, on its page or with PATCH /v1/workers/{worker_id}"
        )
    return None
