"""Agent packs: what a plugin contributes to an agent profile (``contributes.agent_packs``), a directory
``packs/<name>/`` of its bundle laid out as a Claude Code plugin.

A pack holds up to five kinds of component (``KINDS``), each where a Claude Code plugin keeps it:

- skills: ``skills/<skill>/SKILL.md``, whose front matter names the skill as its directory does and describes it;
- subagents: ``agents/<agent>.md``;
- hooks: ``hooks/hooks.json``, an object whose ``hooks`` is an object;
- MCP servers: ``.mcp.json``, whose ``mcpServers`` maps a name to a stdio server (``command``, ``args``, ``env``) or
  a remote one (``type`` http or sse, ``url``, ``headers``); ``${CLAUDE_PLUGIN_ROOT}`` in them stands for the
  directory the worker builds the pack in (``Server.rooted``);
- instructions: ``INSTRUCTIONS.md``, text the agent's system prompt gains.

Any other file is a resource of the pack, a script a hook or a skill runs. What a Claude Code plugin may hold and the
worker does not hand a runtime is refused (``REFUSED``): commands (skills replace them), output styles, LSP servers,
settings, and a ``.claude-plugin/`` directory, whose plugin.json could move the components elsewhere: the worker writes
the plugin manifest itself. ``components`` checks one pack's files and says what it holds; ``bundle.read_bundle`` runs
it on each pack a manifest declares, so the hub refuses a bad pack when it is published, before any profile attaches
it. The hub registers a pack as ``<plugin>/<name>`` (``pack_name``) in the projects that enabled the plugin.

Standard library only: the hub, the worker and the command line share this module.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

from evo_agents.plugins.runtimes import PLUGIN_RUNTIME, SEPARATOR

PACKS_DIR = "packs"  # the directory of a bundle that holds its packs, one directory each
SKILLS_DIR = "skills"
SKILL_FILE = "SKILL.md"
AGENTS_DIR = "agents"
HOOKS_FILE = "hooks/hooks.json"
MCP_FILE = ".mcp.json"
INSTRUCTIONS_FILE = "INSTRUCTIONS.md"
KINDS = ("skills", "agents", "hooks", "mcp_servers", "instructions")
REFUSED = ("commands", "output-styles", ".claude-plugin", ".lsp.json", "settings.json")
ROOT_VARIABLE = "${CLAUDE_PLUGIN_ROOT}"  # the pack's directory, in .mcp.json and hooks.json
TRANSPORTS = ("stdio", "http", "sse")
MAX_SKILLS = 64
MAX_AGENTS = 64
MAX_SERVERS = 20
MAX_INSTRUCTIONS = 32 * 1024  # bytes of INSTRUCTIONS.md
MAX_DESCRIPTION = 1024  # characters of a skill's description, as Claude Code bounds it
SKILL_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
AGENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
SERVER_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
URL = re.compile(r"https?://[^\s]{1,2000}")
_KEY = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*):[ \t]*(.*)")
_BLOCK = (">", "|", ">-", "|-", ">+", "|+")


class PackError(ValueError):
    """A pack that is not one: the message says what is wrong, with the file."""


def pack_name(plugin: str, name: str) -> str:
    """The name the hub registers pack ``name`` of plugin ``plugin`` under: ``sandbox/reviewer``."""
    return f"{plugin}{SEPARATOR}{name}"


def is_pack_name(name: object) -> bool:
    """Whether ``name`` reads as ``<plugin>/<pack>``."""
    return isinstance(name, str) and PLUGIN_RUNTIME.fullmatch(name) is not None


def split_pack(name: str) -> tuple[str, str]:
    """The plugin and the pack of ``<plugin>/<pack>``; ValueError for anything else."""
    if not is_pack_name(name):
        raise ValueError(f"{name!r} is not <plugin>/<pack>")
    plugin, _, pack = name.partition(SEPARATOR)
    return plugin, pack


def directory(name: str) -> str:
    """The directory of pack ``name`` in its bundle: ``packs/<name>/``."""
    return f"{PACKS_DIR}/{name}/"


def files_of(entries: Iterable[tuple[str, bytes]], name: str) -> dict[str, bytes]:
    """The files of pack ``name`` among a bundle's ``(path, data)`` entries, by their paths inside the pack."""
    top = directory(name)
    return {path[len(top) :]: data for path, data in entries if path.startswith(top)}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: str  # SKILL.md, relative to the pack's directory


@dataclass(frozen=True)
class Server:
    """An MCP server of a pack, as its .mcp.json declares it."""

    name: str
    transport: str  # one of TRANSPORTS
    command: str = ""
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    url: str = ""
    headers: tuple[tuple[str, str], ...] = ()

    def rooted(self, root: str) -> Server:
        """The server with ``${CLAUDE_PLUGIN_ROOT}`` replaced by ``root``, the pack's directory on the worker."""

        def put(text: str) -> str:
            return text.replace(ROOT_VARIABLE, root)

        return replace(
            self,
            command=put(self.command),
            args=tuple(put(arg) for arg in self.args),
            env=tuple((key, put(value)) for key, value in self.env),
            url=put(self.url),
            headers=tuple((key, put(value)) for key, value in self.headers),
        )

    def claude(self) -> dict:
        """The server as Claude Code's mcpServers and the Agent SDK's mcp_servers take it."""
        if self.transport == "stdio":
            return {"type": "stdio", "command": self.command, "args": list(self.args), "env": dict(self.env)}
        return {"type": self.transport, "url": self.url, "headers": dict(self.headers)}


@dataclass(frozen=True)
class Components:
    """What one pack holds."""

    skills: tuple[Skill, ...] = ()
    agents: tuple[str, ...] = ()
    hooks: bool = False
    servers: tuple[Server, ...] = ()
    instructions: str = ""
    resources: tuple[str, ...] = field(default=(), compare=False)

    def kinds(self) -> tuple[str, ...]:
        """The kinds of component the pack holds, in KINDS' order."""
        held = {
            "skills": bool(self.skills),
            "agents": bool(self.agents),
            "hooks": self.hooks,
            "mcp_servers": bool(self.servers),
            "instructions": bool(self.instructions),
        }
        return tuple(kind for kind in KINDS if held[kind])


def components(files: Mapping[str, bytes]) -> Components:
    """What the pack of ``files`` (path inside the pack -> bytes) holds, checked; PackError saying what is wrong."""
    for path in files:
        top = path.split("/", 1)[0]
        if top in REFUSED:
            raise PackError(
                f"{path}: a pack holds skills, agents, hooks, MCP servers and instructions; {top} is not one of them"
            )
    found = Components(
        skills=_skills(files),
        agents=_agents(files),
        hooks=_hooks(files),
        servers=_servers(files),
        instructions=_text(files, INSTRUCTIONS_FILE, MAX_INSTRUCTIONS).strip(),
    )
    if not found.kinds():
        raise PackError(
            f"the pack holds no {SKILLS_DIR}/, {AGENTS_DIR}/, {HOOKS_FILE}, {MCP_FILE} nor {INSTRUCTIONS_FILE}"
        )
    taken = {skill.path for skill in found.skills} | {f"{AGENTS_DIR}/{agent}.md" for agent in found.agents}
    taken |= {HOOKS_FILE, MCP_FILE, INSTRUCTIONS_FILE}
    return replace(found, resources=tuple(sorted(path for path in files if path not in taken)))


def _text(files: Mapping[str, bytes], path: str, bound: int) -> str:
    data = files.get(path)
    if data is None:
        return ""
    if len(data) > bound:
        raise PackError(f"{path} is {len(data)} bytes, over {bound}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise PackError(f"{path} is not UTF-8 text") from None


def _json(files: Mapping[str, bytes], path: str) -> dict | None:
    text = _text(files, path, MAX_INSTRUCTIONS)
    if path not in files:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PackError(f"{path} is not JSON: {exc}") from None
    if not isinstance(data, dict):
        raise PackError(f"{path} holds {type(data).__name__}, not an object")
    return data


def _skills(files: Mapping[str, bytes]) -> tuple[Skill, ...]:
    names = sorted({path.split("/")[1] for path in files if path.startswith(f"{SKILLS_DIR}/") and path.count("/") > 1})
    stray = sorted(path for path in files if path.startswith(f"{SKILLS_DIR}/") and path.count("/") == 1)
    if stray:
        raise PackError(f"{stray[0]}: a skill is a directory {SKILLS_DIR}/<skill>/ holding {SKILL_FILE}")
    if len(names) > MAX_SKILLS:
        raise PackError(f"{SKILLS_DIR}/: at most {MAX_SKILLS} skills")
    return tuple(_skill(files, name) for name in names)


def _skill(files: Mapping[str, bytes], name: str) -> Skill:
    path = f"{SKILLS_DIR}/{name}/{SKILL_FILE}"
    if not SKILL_NAME.fullmatch(name):
        raise PackError(f"{SKILLS_DIR}/{name}/: a skill's name is lowercase letters, digits and hyphens, at most 64")
    if path not in files:
        raise PackError(f"{SKILLS_DIR}/{name}/ holds no {SKILL_FILE}")
    front = front_matter(_text(files, path, 256 * 1024), path)
    if front.get("name") != name:
        raise PackError(f"{path}: its front matter names the skill {name}, as its directory does")
    description = front.get("description", "")
    if not description or len(description) > MAX_DESCRIPTION:
        raise PackError(f"{path}: its front matter describes the skill in 1 to {MAX_DESCRIPTION} characters")
    return Skill(name, description, path)


def front_matter(text: str, path: str = SKILL_FILE) -> dict[str, str]:
    """The top-level keys of the YAML front matter that opens ``text``, each as one line of text: a plain or quoted
    scalar, or a folded or literal block joined by spaces. Enough to read a skill's name and description without a
    YAML parser; PackError when the front matter opens and never closes."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    found: dict[str, str] = {}
    key = None
    for line in lines[1:]:
        if line.strip() == "---":
            return found
        match = _KEY.fullmatch(line)
        if match:
            key, value = match[1], match[2].strip()
            found[key] = "" if value in _BLOCK else _unquoted(value)
        elif key and line[:1] in (" ", "\t") and line.strip():
            found[key] = f"{found[key]} {line.strip()}".strip()
    raise PackError(f"{path}: its front matter opens with --- and never closes")


def _unquoted(value: str) -> str:
    if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _agents(files: Mapping[str, bytes]) -> tuple[str, ...]:
    found = []
    for path in sorted(files):
        if not path.startswith(f"{AGENTS_DIR}/"):
            continue
        stem = path[len(AGENTS_DIR) + 1 :]
        if not stem.endswith(".md") or not AGENT_NAME.fullmatch(stem[:-3]):
            raise PackError(f"{path}: a subagent is a file {AGENTS_DIR}/<agent>.md, letters, digits, _ and -")
        _text(files, path, 256 * 1024)
        found.append(stem[:-3])
    if len(found) > MAX_AGENTS:
        raise PackError(f"{AGENTS_DIR}/: at most {MAX_AGENTS} subagents")
    return tuple(found)


def _hooks(files: Mapping[str, bytes]) -> bool:
    data = _json(files, HOOKS_FILE)
    if data is None:
        return False
    if not isinstance(data.get("hooks"), dict) or not data["hooks"]:
        raise PackError(f"{HOOKS_FILE}: its hooks is an object of events, as Claude Code reads it")
    return True


def _servers(files: Mapping[str, bytes]) -> tuple[Server, ...]:
    data = _json(files, MCP_FILE)
    if data is None:
        return ()
    servers = data.get("mcpServers")
    if set(data) != {"mcpServers"} or not isinstance(servers, dict) or not servers:
        raise PackError(f"{MCP_FILE}: an object with only mcpServers, which names at least one server")
    if len(servers) > MAX_SERVERS:
        raise PackError(f"{MCP_FILE}: at most {MAX_SERVERS} servers")
    return tuple(_server(name, servers[name]) for name in sorted(servers))


def _server(name: str, item: object) -> Server:
    where = f"{MCP_FILE}: server {name!r}"
    if not SERVER_NAME.fullmatch(name):
        raise PackError(f"{where}: a server's name is letters, digits, _ and -, at most 64")
    if not isinstance(item, dict):
        raise PackError(f"{where} is not an object")
    transport = item.get("type", "stdio")
    if transport not in TRANSPORTS:
        raise PackError(f"{where}: type is one of {', '.join(TRANSPORTS)}")
    keys = ("type", "command", "args", "env") if transport == "stdio" else ("type", "url", "headers")
    extra = sorted(set(item) - set(keys))
    if extra:
        raise PackError(f"{where}: a {transport} server takes {', '.join(keys)}, not {', '.join(extra)}")
    if transport == "stdio":
        command, args = item.get("command"), item.get("args", [])
        if not isinstance(command, str) or not command.strip():
            raise PackError(f"{where}: a stdio server names its command")
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise PackError(f"{where}: args is a list of strings")
        return Server(name, transport, command=command, args=tuple(args), env=_pairs(where, "env", item))
    url = item.get("url")
    if not isinstance(url, str) or not URL.fullmatch(url.replace(ROOT_VARIABLE, "x")):
        raise PackError(f"{where}: a {transport} server names its http or https url")
    return Server(name, transport, url=url, headers=_pairs(where, "headers", item))


def _pairs(where: str, key: str, item: dict) -> tuple[tuple[str, str], ...]:
    value = item.get(key, {})
    if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise PackError(f"{where}: {key} maps names to strings")
    return tuple(sorted(value.items()))
