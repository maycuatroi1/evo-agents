"""The agent packs of a run on this worker (``docs/workers.md``, Agent packs): built from the bundles the claim hands,
then given to the agent's runtime in its own form.

A run whose profile attaches agent packs (``evo_agents.plugins.agent_packs``) carries them in its claim
(``RunSpec.packs``): each pack at the version of its plugin the profile pins, with a presigned GET of that version's
bundle, its size and its SHA-256. Before the agent starts, the daemon builds them (``build``) into
``runs/<run>/packs/<plugin>/<pack>/``, emptied first: it downloads each bundle once, checks its size and SHA-256 against
the claim, reads it whole (``bundle.read_bundle``, which checks every pack it declares), checks that it is the plugin
and version the claim names and that it declares the pack, and that the owner of this worker accepted the plugin;
then it writes the pack's files with their modes, leaves out its ``.mcp.json``, and writes the
``.claude-plugin/plugin.json`` of a Claude Code plugin named ``<plugin>.<pack>`` (``Pack.claude_name``). Two packs
that name the same MCP server fail the build. Anything wrong fails the run with cause runtime before its agent starts.

What each runtime gets (``Adapter.pack_refusal`` refuses, before the agent starts, a pack holding what the runtime
cannot load, saying which):

- Claude Code takes every kind: each pack's directory as a local plugin (the ``plugins`` option of claude-agent-sdk,
  ``--plugin-dir``), which brings its skills (``<plugin>.<pack>:<skill>``), subagents and hooks; its MCP servers by
  their own names in ``mcp_servers`` (``claude_servers``), not as the plugin's, whose servers Claude Code names
  ``plugin:<plugin>:<server>`` where a profile's ``allowedMcpServers`` cannot name them.
- opencode takes skills, MCP servers and instructions: OPENCODE_CONFIG_CONTENT gains ``skills.paths`` with each pack's
  ``skills/`` and an ``mcp`` entry for each server, ``local`` or ``remote`` (``opencode_env``).
- Codex takes skills, stdio and http MCP servers, and instructions: the app-server's ``skills/extraRoots/set`` names
  each pack's ``skills/`` (``skill_roots``), and the thread's ``config`` an ``mcp_servers`` entry for each server
  (``codex_servers``).
- The runtimes of plugins (ACP) take none.

A pack's instructions follow the profile's in the system prompt of every runtime (``with_instructions``). On the skill
and tool axes of the run's profile a pack counts as named: an allowlist of the skill axis gains its skills, as the
runtime names them (``skill_names``), and one of the tool axis ``mcp:<server>`` for each of its servers
(``evo_agents.hub.profiles``; ``evo_agents.worker.runtimes.profiles.with_packs``). Checked against Claude Code 2.1.296,
opencode 1.18.34 and codex-cli 0.153.4, each listing the skills and connecting the MCP servers of a pack built so.

A run with a browser also gets the pack of the core plugin browser, built from this installation (``build(...,
browser=True)``; ``BROWSER_PACK``), whose Playwright server names the run's Chromium through ``${EVO_BROWSER_CDP}``
(``Pack.variables``); a runtime that loads no MCP server refuses it.

Standard library only.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents.hub import skill_sync
from evo_agents.hub.client import HubError
from evo_agents.plugins import agent_packs, bundle, core
from evo_agents.plugins.manifest import ManifestError, read_manifest

PACKS_DIR = "packs"  # under runs/<run>
KINDS = agent_packs.KINDS  # what a runtime that loads every component of a pack names as its ``pack_kinds``
TRANSPORTS = agent_packs.TRANSPORTS
CLAUDE_MANIFEST = ".claude-plugin/plugin.json"
LEFT_OUT = (agent_packs.MCP_FILE,)  # handed to the runtime through its own configuration
DIR_MODE = 0o700
CONFIG_CONTENT = "OPENCODE_CONFIG_CONTENT"
CLAUDE_CODE = "claude-code"
BROWSER_PLUGIN = (
    "browser"  # the core plugin whose pack every run with a browser gets (``evo_agents.core_plugins.browser``)
)
BROWSER_MODULE = "evo_agents.core_plugins.browser"
BROWSER_PACK = agent_packs.pack_name(BROWSER_PLUGIN, "browser")
NO_BROWSER_HINT = "dispatch the run without browser"
BROWSER_VARIABLE = "EVO_BROWSER_CDP"  # the DevTools endpoint of the run's browser, in the args of the pack's server
KIND_WORDS = {
    "skills": "skills",
    "agents": "subagents",
    "hooks": "hooks",
    "mcp_servers": "MCP servers",
    "instructions": "instructions",
}
INSTRUCTIONS_NOTE = "The instructions of agent pack {pack} (version {version}):\n\n{text}"


class PackFailed(RuntimeError):
    """A pack the claim names that this worker cannot build; the run fails with the message."""


@dataclass(frozen=True)
class Pack:
    """An agent pack built for a run, in ``root``."""

    pack: str  # <plugin>/<name>
    plugin: str
    name: str
    version: str
    root: Path
    components: agent_packs.Components
    # ``${NAME}`` in the pack's MCP servers that the run gives a value to: the DevTools endpoint of its browser
    # (``BROWSER_VARIABLE``). Set by the run before each agent starts; no part of the pack's identity.
    variables: dict[str, str] = field(default_factory=dict, compare=False, repr=False)

    @property
    def label(self) -> str:
        return f"{self.pack} {self.version}"

    @property
    def claude_name(self) -> str:
        """The name of the Claude Code plugin the pack is built as, which its skills and subagents go under: the
        plugin's id and the pack's name, joined by a dot, which neither holds."""
        return f"{self.plugin}.{self.name}"

    def servers(self) -> tuple[agent_packs.Server, ...]:
        """The pack's MCP servers, ``${CLAUDE_PLUGIN_ROOT}`` replaced by its directory and the run's ``variables``."""
        return tuple(server.rooted(str(self.root)).with_variables(self.variables) for server in self.components.servers)

    def skill_names(self, runtime: str) -> list[str]:
        """The pack's skills as ``runtime`` names them: ``<plugin>.<pack>:<skill>`` on Claude Code, the skill's own
        name elsewhere."""
        prefix = f"{self.claude_name}:" if runtime == CLAUDE_CODE else ""
        return [f"{prefix}{skill.name}" for skill in self.components.skills]


# Building


def build(
    tickets: Iterable[Mapping],
    directory: Path,
    *,
    accepted: Iterable[str] | None = None,
    fetch: Callable[[str, int], bytes] | None = None,
    browser: bool = False,
) -> tuple[Pack, ...]:
    """The packs of ``tickets`` (the claim's ``packs``), built in ``directory``, emptied first; ``accepted`` is the
    plugins the owner of this worker accepted, None to skip that check; ``fetch`` downloads a bundle
    (``skill_sync.get_bundle`` when None). With ``browser`` (the run has a browser), the pack of the core plugin browser
    comes too, built from this installation's own package: the run's profile need not attach it, and one that does
    gets it once. PackFailed saying what is wrong."""
    fetch = fetch or skill_sync.get_bundle
    tickets = list(tickets)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(mode=DIR_MODE, parents=True)
    allowed = None if accepted is None else set(accepted)
    held: dict[tuple[str, str], bundle.Contents] = {}
    found = []
    for item in tickets:
        ticket = _ticket(item)
        plugin, name, version = ticket["plugin"], ticket["name"], ticket["version"]
        if allowed is not None and plugin not in allowed:
            raise PackFailed(
                f"agent pack {ticket['pack']} is of plugin {plugin}, which the owner of this worker has not accepted: "
                "accept it in the worker's settings on the hub"
            )
        if (plugin, version) not in held:
            held[(plugin, version)] = _download(ticket, fetch)
        found.append(_place(ticket, held[(plugin, version)], directory / plugin / name))
    if browser and BROWSER_PACK not in {pack.pack for pack in found}:
        ticket, contents = _browser_bundle()
        found.append(_place(ticket, contents, directory / ticket["plugin"] / ticket["name"]))
    _check_servers(found)
    return tuple(found)


def _browser_bundle() -> tuple[dict, bundle.Contents]:
    """The ticket and the contents of the pack of the core plugin browser, from the package this worker runs."""
    # By name, as the entry point of the plugin names it: the worker holds no static import of a core plugin
    source = importlib.import_module(BROWSER_MODULE)
    where = "the agent pack of the core plugin browser, which every run with a browser gets,"
    try:
        manifest = read_manifest(source.MANIFEST)
        built = core.image_bundle(core.CorePlugin(BROWSER_PLUGIN, f"{BROWSER_MODULE}:MANIFEST", manifest))
        contents = bundle.read_bundle(built.data)
    except (bundle.BundleError, ManifestError) as exc:
        raise PackFailed(f"{where} cannot be built from this installation: {exc}") from None
    plugin, name = agent_packs.split_pack(BROWSER_PACK)
    ticket = {
        "pack": BROWSER_PACK,
        "plugin": plugin,
        "name": name,
        "version": manifest.version,
        "sha256": built.sha256,
        "size": len(built.data),
        "url": "",
    }
    return ticket, contents


def _ticket(item: object) -> dict:
    if not isinstance(item, Mapping):
        raise PackFailed("the claim names an agent pack this worker cannot read")
    pack = str(item.get("pack") or "")
    if not agent_packs.is_pack_name(pack):
        raise PackFailed(f"the claim names agent pack {pack!r}, which is not <plugin>/<pack>")
    plugin, name = agent_packs.split_pack(pack)
    if (item.get("plugin"), item.get("name")) != (plugin, name):
        raise PackFailed(f"the claim names agent pack {pack} with another plugin or name beside it")
    try:
        size = int(item["size"])
        return {
            "pack": pack,
            "plugin": plugin,
            "name": name,
            "version": str(item["version"]),
            "sha256": str(item["sha256"]),
            "size": size,
            "url": str(item["url"]),
        }
    except (KeyError, TypeError, ValueError):
        raise PackFailed(
            f"the claim names agent pack {pack} without its bundle's version, URL, size or SHA-256"
        ) from None


def _download(ticket: dict, fetch: Callable[[str, int], bytes]) -> bundle.Contents:
    where = f"the bundle of plugin {ticket['plugin']} {ticket['version']}, of agent pack {ticket['pack']},"
    try:
        data = fetch(ticket["url"], ticket["size"])
    except HubError as exc:
        raise PackFailed(f"{where} did not download: {exc}") from None
    if len(data) != ticket["size"] or hashlib.sha256(data).hexdigest() != ticket["sha256"]:
        raise PackFailed(f"{where} is not the one the hub recorded: another size or SHA-256")
    try:
        contents = bundle.read_bundle(data)
    except bundle.BundleError as exc:
        raise PackFailed(f"{where} is not a plugin bundle this worker reads: {exc}") from None
    manifest = contents.manifest
    if (manifest.id, manifest.version) != (ticket["plugin"], ticket["version"]):
        raise PackFailed(f"{where} holds plugin {manifest.id} {manifest.version}")
    return contents


def _place(ticket: dict, contents: bundle.Contents, root: Path) -> Pack:
    name = ticket["name"]
    declared = {item.get("name"): item for item in contents.manifest.contributions("agent_packs")}
    if name not in declared:
        raise PackFailed(f"plugin {ticket['plugin']} {ticket['version']} declares no agent pack {name}")
    top = agent_packs.directory(name)
    entries = [entry for entry in contents.files if entry.path.startswith(top)]
    try:
        found = agent_packs.components({entry.path[len(top) :]: entry.data for entry in entries})
    except agent_packs.PackError as exc:
        raise PackFailed(f"agent pack {ticket['pack']} {ticket['version']}: {exc}") from None
    root.mkdir(mode=DIR_MODE, parents=True)
    for entry in entries:
        path = entry.path[len(top) :]
        if path in LEFT_OUT:
            continue
        target = root.joinpath(*path.split("/"))
        target.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, entry.mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(entry.data)
    pack = Pack(ticket["pack"], ticket["plugin"], name, ticket["version"], root, found)
    manifest = {
        "name": pack.claude_name,
        "version": pack.version,
        "description": str(declared[name].get("description") or declared[name].get("title") or pack.pack),
    }
    target = root.joinpath(*CLAUDE_MANIFEST.split("/"))
    target.parent.mkdir(mode=DIR_MODE)
    target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return pack


def _check_servers(packs: Iterable[Pack]) -> None:
    seen: dict[str, str] = {}
    for pack in packs:
        for server in pack.components.servers:
            if server.name in seen:
                raise PackFailed(
                    f"agent packs {seen[server.name]} and {pack.pack} both name MCP server {server.name}, which a "
                    "session holds once: attach one of them"
                )
            seen[server.name] = pack.pack


# What the run's log and the runtimes get


def summary(packs: Iterable[Pack]) -> str:
    """The packs built, for the run's log."""
    parts = []
    for pack in packs:
        kinds = ", ".join(KIND_WORDS[kind] for kind in pack.components.kinds())
        parts.append(f"{pack.label} ({kinds})")
    return f"Agent packs built for the agent: {'; '.join(parts)}."


def refusal(packs: Iterable[Pack], runtime: str, *, kinds: Iterable[str], transports: Iterable[str]) -> str | None:
    """Why ``runtime``, which loads the components ``kinds`` and MCP servers over ``transports``, cannot load
    ``packs``: the first pack that holds something else, or two that name the same skill where the runtime does not
    tell them apart; None when it can."""
    kinds, transports = set(kinds), set(transports)
    seen: dict[str, str] = {}
    for pack in packs:
        left = [KIND_WORDS[kind] for kind in pack.components.kinds() if kind not in kinds]
        if left:
            return (
                f"agent pack {pack.label} holds {', '.join(left)}, which {runtime} does not load: run it on "
                "claude-code, or attach a pack without them"
            )
        for server in pack.components.servers:
            if server.transport not in transports:
                return (
                    f"agent pack {pack.label} has MCP server {server.name} over {server.transport}, which {runtime} "
                    f"does not connect to (it takes {', '.join(sorted(transports))}): run it on claude-code"
                )
        for skill in pack.skill_names(runtime):
            if skill in seen:
                return f"agent packs {seen[skill]} and {pack.label} both hold skill {skill}, which {runtime} lists once"
            seen[skill] = pack.label
    return None


def skill_names(packs: Iterable[Pack], runtime: str) -> list[str]:
    """The skills of ``packs`` as ``runtime`` names them."""
    return [name for pack in packs for name in pack.skill_names(runtime)]


def server_names(packs: Iterable[Pack]) -> list[str]:
    """The MCP servers of ``packs``, by name."""
    return [server.name for pack in packs for server in pack.components.servers]


def with_instructions(text: str, packs: Iterable[Pack]) -> str:
    """``text``, what the system prompt says, and the instructions of each pack that has some after it."""
    added = [
        INSTRUCTIONS_NOTE.format(pack=pack.pack, version=pack.version, text=pack.components.instructions)
        for pack in packs
        if pack.components.instructions
    ]
    return "\n\n".join([text, *added])


def skill_roots(packs: Iterable[Pack]) -> list[str]:
    """The ``skills/`` directory of each pack that has skills."""
    return [str(pack.root / agent_packs.SKILLS_DIR) for pack in packs if pack.components.skills]


def claude_servers(packs: Iterable[Pack]) -> dict:
    """The MCP servers of ``packs`` as claude-agent-sdk's ``mcp_servers`` takes them."""
    return {server.name: server.claude() for pack in packs for server in pack.servers()}


def claude_options(packs: Iterable[Pack]) -> dict:
    """What claude-agent-sdk's ``ClaudeAgentOptions`` gains for ``packs``: each one's directory as a local plugin
    (``plugins``, ``--plugin-dir``), and their MCP servers (``mcp_servers``); nothing for none."""
    packs = list(packs)
    if not packs:
        return {}
    found: dict = {"plugins": [{"type": "local", "path": str(pack.root)} for pack in packs]}
    servers = claude_servers(packs)
    if servers:
        found["mcp_servers"] = servers
    return found


def opencode_server(server: agent_packs.Server) -> dict:
    """An MCP server as opencode's ``mcp`` takes it."""
    if server.transport == "stdio":
        found = {"type": "local", "command": [server.command, *server.args]}
        if server.env:
            found["environment"] = dict(server.env)
        return found
    found = {"type": "remote", "url": server.url}
    if server.headers:
        found["headers"] = dict(server.headers)
    return found


def opencode_env(env: Mapping[str, str], packs: Iterable[Pack]) -> dict[str, str]:
    """The environment of the run's ``opencode serve`` with ``packs``: OPENCODE_CONFIG_CONTENT gains their skill
    directories in ``skills.paths`` and their servers in ``mcp``, over what it held; ``env`` as it is without packs."""
    packs = list(packs)
    if not packs:
        return dict(env)
    try:
        content = json.loads(env.get(CONFIG_CONTENT) or "{}")
    except ValueError:
        content = {}
    content = content if isinstance(content, dict) else {}
    roots = skill_roots(packs)
    if roots:
        skills = content.get("skills") if isinstance(content.get("skills"), dict) else {}
        paths = skills.get("paths") if isinstance(skills.get("paths"), list) else []
        content["skills"] = {**skills, "paths": [*paths, *roots]}
    servers = {server.name: opencode_server(server) for pack in packs for server in pack.servers()}
    if servers:
        own = content.get("mcp") if isinstance(content.get("mcp"), dict) else {}
        content["mcp"] = {**own, **servers}
    return {**env, CONFIG_CONTENT: json.dumps(content)}


def codex_servers(packs: Iterable[Pack]) -> dict:
    """The MCP servers of ``packs`` as the ``mcp_servers`` of a Codex thread's ``config`` takes them."""
    found = {}
    for pack in packs:
        for server in pack.servers():
            if server.transport == "stdio":
                found[server.name] = {"command": server.command, "args": list(server.args), "env": dict(server.env)}
            else:
                found[server.name] = {"url": server.url, "http_headers": dict(server.headers)}
    return found
