"""The agent profile of a run in its agent session: what each runtime's adapter makes of its tool, skill and model
axes and its instructions (``evo_agents.hub.profiles``; ``docs/workers.md``, Agent profiles in the session).

The hub enforces the credential, worker, project and runtime axes and the model a dispatch names; the claim carries
the profile at the version the run went under (``RunSpec.profile``), and the adapter applies the rest. A profile that
allows everything and has no instructions, a project's default, changes nothing: the adapter starts the agent with
the same settings as a run without a profile. What follows was checked against fakes of each runtime's model API that
recorded what the runtime offered the model, in the main session and in a subagent (2026-10-14).

Claude Code 2.1.296 through claude-agent-sdk 0.2.165:

- tools: ``tools`` lists the built-in tools of the words the profile allows (CLAUDE_TOOLS), with CLAUDE_ALWAYS; no
  other built-in is in the session, a subagent's included. ``allowedMcpServers`` in the session's flag settings
  (``--settings``) names the MCP servers the profile allows, so no other server is even configured. A plugin's server
  (``plugin:<plugin>:<server>``) matches there only by its command or URL, never by a name, so a profile with a tool
  allowlist runs without plugin servers. The owner's own settings may name more servers (Claude Code merges the
  arrays), so the adapter asks the CLI for its servers before it sends the prompt and fails the run on one the profile
  leaves out (``claude_extra_servers``). ``ClaudeSDKClient.toggle_mcp_server`` is not used: before the servers have
  connected it does not take, and it records the server as disabled for the worktree in the owner's
  ``~/.claude.json``.
- skills: ``skills`` lists the skills the profile allows, with ``setting_sources`` set to all three (the SDK would
  drop ``local``). The filter hides other skills from the main session only, so a profile that limits skills runs
  without the tools that start subagents (the ``agent`` word), as the approved design says.
- model: the run's model, which the hub took from the allowlist; ``availableModels`` keeps a subagent to the
  allowlist, unless the owner's settings widen it (merged as well).
- a PreToolUse hook, which Claude Code consults for subagents too and under bypassPermissions, refuses a call of a
  tool of an MCP server, of a skill or of a subagent on a model the profile leaves out (``claude_call_refusal``).
- instructions: after the adapter's own note in ``--append-system-prompt`` (``with_instructions``).

opencode 1.18.34:

- tools and skills: the server of the run starts with ``permission`` rules in OPENCODE_CONFIG_CONTENT, merged over
  the owner's config (``opencode_env``): ``deny`` for the permissions of each word the profile leaves out
  (OPENCODE_PERMISSIONS), and for ``skill`` ``deny`` then ``allow`` for each allowed skill. Rules of the config take
  tools out of what the model is offered and skills out of its listing, a subagent's too. The session gets the same
  rules and ``deny`` for ``<server>_*`` of each MCP server ``GET /mcp`` names that the profile leaves out
  (``opencode_rules``); a session the run resumes gets them again (``PATCH /session/:id``).
- model: the run's model (``providerID``/``modelID``), never the config's when the profile limits models.
- instructions: after the headless note in each prompt's ``system``.

Codex 0.153.4 as the app-server, through openai-codex 0.160.0:

- tools: the thread's ``config`` turns off the features of each word the profile leaves out (CODEX_FEATURES),
  ``web_search`` for ``web``, the MCP servers of the owner's and the worktree's ``config.toml`` the profile leaves out
  (``mcp_servers.<name>.enabled``), and CODEX_TOOL_FEATURES, which bring tools of their own. Codex cannot turn off
  ``apply_patch`` (``include_apply_patch_tool`` leaves it on), so a tool allowlist without ``edit`` fails the run
  before its agent starts; nor has it a switch that hides skills, so a skill allowlist fails it too
  (``codex_refusal``).
- model: the thread's ``model``. Instructions: after the adapter's note in ``developer_instructions``.

The agent packs the profile attaches (``context.packs``, ``evo_agents.worker.packs``) count as named on its skill and
tool axes: in the session an allowlist of the skill axis also names each pack's skills, as the runtime names them, and
one of the tool axis ``mcp:<server>`` for each pack's MCP server (``with_packs``); so every rule above lets them
through. An axis of mode all, and every other axis, stays as the profile has it.

Standard library only; ``claude_hooks`` imports claude-agent-sdk when it runs.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from evo_agents.hub import profiles as rules
from evo_agents.worker import packs
from evo_agents.worker.adapter import RunContext
from evo_agents.worker.runtimes.common import run_setting

WORDS = tuple(rules.TOOLS)  # read, edit, bash, web, agent
# Claude Code's built-in tools of each word, by every name they had: a name the CLI does not know is left out of the
# session without an error, so the old names stay (Task is Agent's, BashOutput and KillShell TaskStop's before it).
CLAUDE_TOOLS = {
    "read": ("Read", "Glob", "Grep", "LSP", "NotebookRead"),
    "edit": ("Edit", "MultiEdit", "Write", "NotebookEdit"),
    "bash": ("Bash", "BashOutput", "KillShell", "TaskStop", "Monitor", "EnterWorktree", "ExitWorktree"),
    "web": ("WebFetch", "WebSearch"),
    "agent": ("Agent", "Task", "SendMessage", "ListAgents", "Workflow"),
}
# Always in a session that limits tools, when the CLI has them: the agent's to-do list and the loader of deferred MCP
# tools (2.1.296 adds neither, named or not). TaskCreate and its kin stay out: 2.1.296 adds them when named, though a
# session without a profile has none.
CLAUDE_ALWAYS = ("TodoWrite", "ToolSearch")
CLAUDE_SKILL = "Skill"
SETTING_SOURCES = ["user", "project", "local"]  # Claude Code's own, which the SDK narrows when it sets skills
# opencode's permissions of each word: ``edit`` covers its write, patch and apply_patch tools too.
OPENCODE_PERMISSIONS = {
    "read": ("read", "glob", "grep", "list", "lsp"),
    "edit": ("edit",),
    "bash": ("bash",),
    "web": ("webfetch", "websearch", "codesearch"),
    "agent": ("task",),
}
CONFIG_CONTENT = "OPENCODE_CONFIG_CONTENT"
# Codex's features of each word; ``web`` is the ``web_search`` setting, and ``edit`` has none (``codex_refusal``).
CODEX_FEATURES = {
    "read": ("view_image",),
    "bash": ("shell_tool", "unified_exec"),
    "agent": ("multi_agent", "multi_agent_v2"),
}
# Features that bring tools of their own (connectors, plugins and their MCP servers, a browser, the screen, images),
# off whenever the profile limits tools.
CODEX_TOOL_FEATURES = ("apps", "plugins", "browser_use", "computer_use", "image_generation")
INSTRUCTIONS_NOTE = "You are {name} (@{handle}), an agent of this project. Its instructions:\n\n{instructions}"
NO_SUBAGENT_NOTE = (
    "Claude Code runs without its tools that start subagents (Agent, Workflow): agent profile {label} limits skills, "
    "and Claude Code's skill filter does not reach a subagent."
)
_KEY = re.compile(r"[^A-Za-z0-9_-]")


def server_key(name: str) -> str:
    """An MCP server's name as the runtimes write it in their tool names (``mcp__<key>__<tool>``, ``<key>_<tool>``):
    each character other than a letter, a digit, ``_`` and ``-`` as ``_`` (``plugin:a:b`` is ``plugin_a_b``)."""
    return _KEY.sub("_", name)


@dataclass(frozen=True)
class Profile:
    """The agent profile of a claimed run (``RunSpec.profile``)."""

    handle: str
    version: int
    name: str
    instructions: str
    permissions: dict

    @classmethod
    def of(cls, run: Mapping | None) -> Profile | None:
        """The profile of ``run`` as the claim answered it; None for a run without one."""
        body = run.get("profile") if isinstance(run, Mapping) else None
        if not isinstance(body, Mapping):
            return None
        permissions = body.get("permissions")
        return cls(
            handle=str(body.get("handle") or ""),
            version=int(body.get("version") or 0),
            name=str(body.get("name") or body.get("handle") or ""),
            instructions=str(body.get("instructions") or "").strip(),
            permissions=dict(permissions) if isinstance(permissions, Mapping) else {},
        )

    @property
    def label(self) -> str:
        return f"@{self.handle} (version {self.version})"

    @property
    def applies(self) -> bool:
        """Whether the agent session changes for the profile: it limits tool, skill or model, or has instructions
        (``rules.needs_worker``)."""
        return rules.needs_worker(self.permissions, self.instructions)

    def allowed(self, axis: str) -> list[str] | None:
        """The allowlist of ``axis``; None when it allows all."""
        return rules.allowed(self.permissions, axis)

    def words(self) -> set[str] | None:
        """The tool words the profile allows; None when its tool axis allows all."""
        tools = self.allowed("tool")
        return None if tools is None else {item for item in tools if item in WORDS}

    def servers(self) -> list[str] | None:
        """The MCP servers the profile allows, as ``mcp:<server>`` names them; None when its tool axis allows all."""
        tools = self.allowed("tool")
        if tools is None:
            return None
        return [item[len(rules.MCP_PREFIX) :] for item in tools if item.startswith(rules.MCP_PREFIX)]

    def allows_word(self, word: str) -> bool:
        words = self.words()
        return words is None or word in words

    def allows_server(self, name: str) -> bool:
        """Whether the profile allows the MCP server ``name``, compared as tool names write it (``server_key``)."""
        servers = self.servers()
        return servers is None or server_key(name) in {server_key(item) for item in servers}

    def allows_skill(self, name: str) -> bool:
        return rules.allows(self.permissions, "skill", name)

    def allows_model(self, name: str) -> bool:
        return rules.allows(self.permissions, "model", name)


def profile_of(context: RunContext) -> Profile | None:
    """The profile of the run of ``context``, when the session changes for it, with the run's agent packs counted as
    named on its skill and tool axes (``with_packs``); None otherwise."""
    profile = Profile.of(context.run)
    if profile is None or not profile.applies:
        return None
    if not context.packs:
        return profile
    runtime = str(context.run.get("runtime") or "")
    return with_packs(profile, packs.skill_names(context.packs, runtime), packs.server_names(context.packs))


def with_packs(profile: Profile, skills: Iterable[str], servers: Iterable[str]) -> Profile:
    """``profile`` with what the agent packs it attaches hold counted as named: an allowlist of the skill axis gains
    ``skills`` (the packs' skills, as the runtime names them), one of the tool axis ``mcp:<server>`` for each of
    ``servers``. Pinning a pack at a version is the admin's consent to what that version holds; an axis of mode all,
    and every other axis, stay as they are."""
    permissions = dict(profile.permissions)
    for axis, items in (("skill", list(skills)), ("tool", [rules.MCP_PREFIX + name for name in servers])):
        allow = profile.allowed(axis)
        if allow is None or not items:
            continue
        added = [item for item in items if item not in allow]
        permissions[axis] = {**rules.rule(profile.permissions, axis), "allow": [*allow, *added]}
    return replace(profile, permissions=permissions)


def summary(profile: Profile) -> str:
    """What the profile limits, for the run's log."""
    parts = []
    for axis, what in (("tool", "tools"), ("skill", "skills"), ("model", "models")):
        allow = profile.allowed(axis)
        if allow is not None:
            parts.append(f"{what} {', '.join(allow) or 'none'}")
    if profile.instructions:
        parts.append("its instructions added to the system prompt")
    return f"Agent profile {profile.label} applies in the agent session: {'; '.join(parts)}."


def model_refusal(profile: Profile | None, run: Mapping) -> str | None:
    """Why no runtime may start the agent of ``run`` under ``profile``'s model allowlist: it allows none, or the run
    names a model it leaves out; None otherwise. The hub refuses both at the dispatch; this keeps a run that got past
    it from running on another model."""
    allow = profile.allowed("model") if profile is not None else None
    if allow is None:
        return None
    if not allow:
        return f"agent profile {profile.label} allows no model"
    asked = run.get("model")
    if isinstance(asked, str) and asked.strip() and asked.strip() not in allow:
        return f"the run's model {asked.strip()} is not one agent profile {profile.label} allows ({', '.join(allow)})"
    return None


def session_model(context: RunContext, runtime: str) -> str | None:
    """The model of the agent session: the run's, else the daemon's setting (``run_setting``), as before; for a
    profile that limits models, the first of those that it allows, else the first of its allowlist."""
    chosen = run_setting(context, runtime, "model")
    profile = Profile.of(context.run)
    allow = profile.allowed("model") if profile is not None else None
    if allow is None or chosen in allow:
        return chosen
    return allow[0] if allow else None


def with_instructions(note: str, profile: Profile | None) -> str:
    """``note``, the adapter's own text for the system prompt, and the profile's instructions after it."""
    if profile is None or not profile.instructions:
        return note
    text = INSTRUCTIONS_NOTE.format(name=profile.name, handle=profile.handle, instructions=profile.instructions)
    return f"{note}\n\n{text}"


# Claude Code


def claude_tools(profile: Profile) -> list[str] | None:
    """The built-in tools of the session (``tools``); None for every one."""
    words = profile.words()
    skills = profile.allowed("skill")
    if words is None:
        return None
    if skills is not None:
        words = words - {"agent"}
    tools = [name for word in WORDS if word in words for name in CLAUDE_TOOLS[word]]
    tools += CLAUDE_ALWAYS
    if skills is None or skills:
        tools.append(CLAUDE_SKILL)
    return tools


def claude_settings(profile: Profile | None) -> dict:
    """What ``ClaudeAgentOptions`` gains for the profile, besides the hook (``claude_hooks``) and the instructions:
    nothing for a profile that limits no tool, skill or model."""
    if profile is None:
        return {}
    found: dict = {}
    tools = claude_tools(profile)
    skills = profile.allowed("skill")
    if tools is not None:
        found["tools"] = tools
    elif skills is not None:
        found["disallowed_tools"] = list(CLAUDE_TOOLS["agent"])
    if skills is not None:
        found["skills"] = list(skills)
        found["setting_sources"] = list(SETTING_SOURCES)
    settings: dict = {}
    servers = profile.servers()
    if servers is not None:
        settings["allowedMcpServers"] = [{"serverName": name} for name in servers]
    models = profile.allowed("model")
    if models is not None:
        settings["availableModels"] = list(models)
    if settings:
        found["settings"] = json.dumps(settings)
    return found


def claude_call_refusal(profile: Profile, name: str, tool_input) -> str | None:
    """Why the PreToolUse hook refuses a call of tool ``name`` with ``tool_input``: a tool of an MCP server, a skill,
    or a subagent on a model, that the profile leaves out; None when it lets the call through."""
    given = tool_input if isinstance(tool_input, Mapping) else {}
    if name.startswith("mcp__"):
        server = name[len("mcp__") :].split("__", 1)[0]
        if not profile.allows_server(server):
            return f"agent profile {profile.label} does not allow the tools of MCP server {server}"
    elif name == CLAUDE_SKILL:
        skill = str(given.get("skill") or "").strip().lstrip("/")
        if skill and not profile.allows_skill(skill):
            return f"agent profile {profile.label} does not allow skill {skill}"
    elif name in CLAUDE_TOOLS["agent"]:
        model = given.get("model")
        if isinstance(model, str) and model.strip() not in ("", "inherit") and not profile.allows_model(model.strip()):
            return f"agent profile {profile.label} does not allow model {model.strip()}"
    return None


def claude_hooks(profile: Profile | None) -> dict | None:
    """The ``hooks`` of the session: a PreToolUse hook that refuses what ``claude_call_refusal`` refuses; None when the
    profile limits no tool, skill or model."""
    if profile is None or not any(profile.allowed(axis) is not None for axis in ("tool", "skill", "model")):
        return None
    from claude_agent_sdk import HookMatcher

    async def gate(data, tool_use_id, context) -> dict:
        why = claude_call_refusal(profile, str(data.get("tool_name") or ""), data.get("tool_input"))
        if why is None:
            return {}
        decision = {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": why}
        return {"hookSpecificOutput": decision}

    return {"PreToolUse": [HookMatcher(matcher=None, hooks=[gate])]}


def claude_extra_servers(profile: Profile | None, status) -> list[str]:
    """The MCP servers Claude Code's status (``get_mcp_status``) lists that the profile leaves out, other than one the
    owner disabled."""
    if profile is None or profile.servers() is None or not isinstance(status, Mapping):
        return []
    extra = []
    for server in status.get("mcpServers") or []:
        if isinstance(server, Mapping) and server.get("status") != "disabled":
            name = str(server.get("name") or "")
            if not profile.allows_server(name):
                extra.append(name)
    return extra


def claude_notes(profile: Profile | None) -> list[str]:
    """What the run's log says of the session beyond ``summary``: the subagent tools left out for a skill
    allowlist."""
    if profile is None or profile.allowed("skill") is None:
        return []
    return [NO_SUBAGENT_NOTE.format(label=profile.label)]


# opencode


def opencode_denied(profile: Profile) -> list[str]:
    """opencode's permissions of the words the profile leaves out."""
    return [name for word in WORDS if not profile.allows_word(word) for name in OPENCODE_PERMISSIONS[word]]


def opencode_permission(profile: Profile | None) -> dict | None:
    """The ``permission`` of the server's config for the profile; None when it limits neither tool nor skill."""
    if profile is None:
        return None
    permission: dict = {name: "deny" for name in opencode_denied(profile)}
    skills = profile.allowed("skill")
    if skills is not None:
        permission["skill"] = {"*": "deny", **{name: "allow" for name in skills}} if skills else "deny"
    return permission or None


def opencode_env(env: Mapping[str, str], profile: Profile | None) -> dict[str, str]:
    """The environment of the run's ``opencode serve``: ``env``, and for a profile that limits tools or skills
    OPENCODE_CONFIG_CONTENT with its ``permission`` merged over what the variable already held."""
    permission = opencode_permission(profile)
    if permission is None:
        return dict(env)
    try:
        content = json.loads(env.get(CONFIG_CONTENT) or "{}")
    except ValueError:
        content = {}
    content = content if isinstance(content, dict) else {}
    own = content.get("permission")
    merged = {"*": own} if isinstance(own, str) else dict(own) if isinstance(own, dict) else {}
    merged.update(permission)
    return {**env, CONFIG_CONTENT: json.dumps({**content, "permission": merged})}


def opencode_rules(profile: Profile | None, servers: Iterable[str]) -> list[dict]:
    """The session's rules for the profile, after the run's own: ``deny`` for each permission of a word it leaves out,
    for the tools of each MCP server of ``servers`` it leaves out, and for each skill it does not allow."""
    if profile is None:
        return []
    found = [{"permission": name, "action": "deny", "pattern": "*"} for name in opencode_denied(profile)]
    for server in servers:
        if not profile.allows_server(server):
            found.append({"permission": f"{server_key(server)}_*", "action": "deny", "pattern": "*"})
    skills = profile.allowed("skill")
    if skills is not None:
        found.append({"permission": "skill", "action": "deny", "pattern": "*"})
        found += [{"permission": "skill", "action": "allow", "pattern": name} for name in skills]
    return found


# Codex


def codex_refusal(profile: Profile | None) -> str | None:
    """Why Codex may not start the agent under the profile: it leaves out ``edit`` or limits skills, which Codex
    cannot turn off; None otherwise."""
    if profile is None:
        return None
    if not profile.allows_word("edit"):
        return (
            f"agent profile {profile.label} leaves out the tool edit, and codex cannot turn off apply_patch, its tool "
            "that edits files: run it on claude-code or opencode, or allow edit"
        )
    if profile.allowed("skill") is not None:
        return (
            f"agent profile {profile.label} limits skills, and codex has no switch that hides skills from its "
            "session: run it on claude-code or opencode"
        )
    return None


def codex_servers(home: Path, worktree: Path) -> list[str]:
    """The MCP servers of the owner's codex ``config.toml`` (in ``home``) and of the worktree's
    ``.codex/config.toml``, each once."""
    found: list[str] = []
    for path in (home / "config.toml", worktree / ".codex" / "config.toml"):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        servers = data.get("mcp_servers")
        if isinstance(servers, dict):
            found += [name for name in servers if isinstance(name, str) and name not in found]
    return found


def codex_config(profile: Profile | None, servers: Iterable[str]) -> dict:
    """What the thread's ``config`` gains for the profile: the features and settings of the words it leaves out, and
    the MCP servers of ``servers`` it leaves out turned off; nothing when it limits no tool."""
    if profile is None or profile.words() is None:
        return {}
    features = {name: False for name in CODEX_TOOL_FEATURES}
    for word, names in CODEX_FEATURES.items():
        if not profile.allows_word(word):
            features.update(dict.fromkeys(names, False))
    found: dict = {"features": features}
    if not profile.allows_word("web"):
        found["web_search"] = "disabled"
    off = {server: {"enabled": False} for server in servers if not profile.allows_server(server)}
    if off:
        found["mcp_servers"] = off
    return found
