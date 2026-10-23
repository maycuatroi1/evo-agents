"""Agent packs on the worker (``evo_agents.worker.packs``): the daemon builds the packs a run's claim carries into the
run's directory, checked against the claim, and each runtime's adapter gives them to its agent in its own form; a pack
that does not build, or that holds what the runtime does not load, fails the run before its agent starts.

The runtimes are the fakes of ``tests.worker.test_runtimes``, the daemon runs in this process against the in-memory hub
of ``tests.worker.test_plan_runs``. Each test's name says "pack", so ``pytest tests/worker -k pack`` runs them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import os
import stat
from types import SimpleNamespace

import pytest

from evo_agents.hub.client import HubError
from evo_agents.plugins.bundle import Bundle, pack
from evo_agents.worker import packs
from evo_agents.worker.adapter import Adapter
from evo_agents.worker.runtimes import common, profiles
from evo_agents.worker.runtimes.claude_code import ClaudeCodeAdapter
from evo_agents.worker.runtimes.codex import CodexAdapter, thread_compaction
from evo_agents.worker.runtimes.opencode import OpencodeAdapter
from tests.plugins.samples import manifest, pack_files, write_plugin
from tests.worker.test_agent_profiles import DEFAULT, INSTRUCTIONS, _command, profile
from tests.worker.test_runtimes import (
    MODEL,
    _claude,
    _codex,
    _codex_samples,
    _collect,
    _opencode,
    _opencode_samples,
    _samples,
    needs,
)

PLUGIN = "acme-tools"
VERSION = "1.0.0"
SESSION = "11111111-2222-3333-4444-555555555555"
NOTES_FILES = {
    "packs/notes/skills/notes/SKILL.md": "---\nname: notes\ndescription: Take notes\n---\nWrite them down.\n",
    "packs/notes/INSTRUCTIONS.md": "Keep notes short.\n",
}


def lean_files(name: str = "reviewer") -> dict[str, str]:
    """The files of pack ``name`` with skills, MCP servers and instructions only, what every runtime loads."""
    top = f"packs/{name}/"
    return {
        path: text for path, text in pack_files(name).items() if not path.startswith((top + "agents/", top + "hooks/"))
    }


def bundle(
    tmp_path, files: dict[str, str] | None = None, *, names=("reviewer",), plugin=PLUGIN, version=VERSION
) -> Bundle:
    """The bundle of plugin ``plugin`` ``version`` declaring the packs ``names``, with ``files`` (``pack_files`` of
    each by default)."""
    declared = [{"name": name, "description": f"The {name} pack"} for name in names]
    data = manifest(id=plugin, version=version, contributes={"agent_packs": declared})
    if files is None:
        files = {path: text for name in names for path, text in pack_files(name).items()}
    directory = write_plugin(tmp_path / f"plugin-{plugin}-{version}", data, files)
    for name in names:
        lint = directory / "packs" / name / "bin" / "lint"
        if lint.exists():
            lint.chmod(0o755)
    return pack(directory)


def ticket(held: Bundle, name: str = "reviewer", **changes) -> dict:
    """The claim's ticket of pack ``name`` of ``held`` (``RunSpec.packs``)."""
    plugin, version = held.manifest.id, held.manifest.version
    return {
        "pack": f"{plugin}/{name}",
        "plugin": plugin,
        "name": name,
        "version": version,
        "sha256": held.sha256,
        "size": len(held.data),
        "url": f"https://blobs.example.test/{plugin}-{version}.tar.gz",
        "expires_at": "2026-10-18T12:00:00Z",
        **changes,
    }


class Store:
    """The blob store of a test: the bytes of each URL, and the downloads asked."""

    def __init__(self, *bundles: Bundle):
        self.held = {ticket(item)["url"]: item.data for item in bundles}
        self.asked: list[str] = []

    def __call__(self, url: str, size: int) -> bytes:
        self.asked.append(url)
        return self.held[url]


def built(tmp_path, files=None, *, names=("reviewer",)) -> tuple[packs.Pack, ...]:
    held = bundle(tmp_path, files, names=names)
    return packs.build([ticket(held, name) for name in names], tmp_path / "run" / "packs", fetch=Store(held))


def with_packs(adapter, found):
    """A new adapter of ``adapter``'s class for its run, with the packs ``found``."""
    return type(adapter)(dataclasses.replace(adapter.context, packs=tuple(found)))


# Building


def test_packs_are_built_in_the_run_directory_as_claude_code_plugins(tmp_path):
    held = bundle(tmp_path, {**pack_files(), **NOTES_FILES}, names=("reviewer", "notes"))
    store = Store(held)
    directory = tmp_path / "run" / "packs"
    directory.mkdir(parents=True)
    (directory / "stale").write_text("left by the run's last agent\n")
    found = packs.build([ticket(held), ticket(held, "notes")], directory, accepted=[PLUGIN], fetch=store)
    assert store.asked == [ticket(held)["url"]], "one download for the packs of one version of a plugin"
    assert not (directory / "stale").exists(), "the directory is emptied first"
    reviewer, notes = found
    assert reviewer.root == directory / PLUGIN / "reviewer" and reviewer.label == f"{PLUGIN}/reviewer {VERSION}"
    held_files = sorted(str(path.relative_to(reviewer.root)) for path in reviewer.root.rglob("*") if path.is_file())
    assert held_files == [
        ".claude-plugin/plugin.json",
        "INSTRUCTIONS.md",
        "agents/critic.md",
        "bin/lint",
        "hooks/hooks.json",
        "skills/review/SKILL.md",
        "skills/review/checklist.md",
    ], "every file of the pack but .mcp.json, whose servers go through the runtime's own configuration"
    assert json.loads((reviewer.root / ".claude-plugin" / "plugin.json").read_text()) == {
        "name": f"{PLUGIN}.reviewer",
        "version": VERSION,
        "description": "The reviewer pack",
    }
    assert stat.S_IMODE((reviewer.root / "bin" / "lint").stat().st_mode) & 0o100, "modes are kept"
    lint, docs = sorted(reviewer.servers(), key=lambda server: server.name, reverse=True)
    assert lint.command == f"{reviewer.root}/bin/lint" and lint.args == ("--fast",)
    assert docs.transport == "http" and docs.url == "https://docs.example.test/mcp"
    assert reviewer.skill_names("claude-code") == [f"{PLUGIN}.reviewer:review"]
    assert notes.skill_names("opencode") == ["notes"] and notes.components.kinds() == ("skills", "instructions")
    assert packs.summary(found) == (
        f"Agent packs built for the agent: {PLUGIN}/reviewer {VERSION} (skills, subagents, hooks, MCP servers, "
        f"instructions); {PLUGIN}/notes {VERSION} (skills, instructions)."
    )


def _failure(tmp_path, held: Bundle, item: dict, *, accepted=None, fetch=None) -> str:
    with pytest.raises(packs.PackFailed) as caught:
        packs.build([item], tmp_path / "run" / "packs", accepted=accepted, fetch=fetch or Store(held))
    return str(caught.value)


def test_a_pack_that_does_not_build_fails_with_what_is_wrong(tmp_path):
    held = bundle(tmp_path)
    of = f"the bundle of plugin {PLUGIN} {VERSION}, of agent pack {PLUGIN}/reviewer,"
    assert _failure(tmp_path, held, ticket(held), accepted=["other"]) == (
        f"agent pack {PLUGIN}/reviewer is of plugin {PLUGIN}, which the owner of this worker has not accepted: "
        "accept it in the worker's settings on the hub"
    )
    assert _failure(tmp_path, held, ticket(held, sha256="0" * 64)) == (
        f"{of} is not the one the hub recorded: another size or SHA-256"
    )
    assert "is not the one the hub recorded" in _failure(tmp_path, held, ticket(held, size=len(held.data) + 1))

    def cut(url, size):
        raise HubError("the download from the blob store was cut off (TimeoutError)")

    assert _failure(tmp_path, held, ticket(held), fetch=cut) == (
        f"{of} did not download: the download from the blob store was cut off (TimeoutError)"
    )
    other = bundle(tmp_path, version="2.0.0")
    swapped = ticket(other, version=VERSION, url=ticket(held)["url"])
    assert _failure(tmp_path, held, swapped, fetch=lambda url, size: other.data) == f"{of} holds plugin {PLUGIN} 2.0.0"
    assert _failure(tmp_path, held, ticket(held, "ghost")) == (
        f"plugin {PLUGIN} {VERSION} declares no agent pack ghost"
    )
    junk = b"not a bundle"
    junky = ticket(held, sha256=hashlib.sha256(junk).hexdigest(), size=len(junk))
    assert "is not a plugin bundle this worker reads" in _failure(tmp_path, held, junky, fetch=lambda url, size: junk)
    assert "is not <plugin>/<pack>" in _failure(tmp_path, held, ticket(held, pack="reviewer"))
    assert "with another plugin or name beside it" in _failure(tmp_path, held, {**ticket(held), "name": "notes"})
    assert "without its bundle's version, URL, size or SHA-256" in _failure(tmp_path, held, ticket(held, size=None))


def test_two_packs_that_name_one_mcp_server_fail_the_pack_build(tmp_path):
    held = bundle(tmp_path, names=("reviewer", "second"))
    with pytest.raises(packs.PackFailed) as caught:
        packs.build([ticket(held), ticket(held, "second")], tmp_path / "packs", fetch=Store(held))
    assert str(caught.value) == (
        f"agent packs {PLUGIN}/reviewer and {PLUGIN}/second both name MCP server docs, which a session holds once: "
        "attach one of them"
    )


# What each runtime loads


def test_pack_refusal_says_which_pack_holds_what_the_runtime_does_not_load(tmp_path):
    full = built(tmp_path / "full")
    lean = built(tmp_path / "lean", lean_files())
    for cls in (ClaudeCodeAdapter, OpencodeAdapter, CodexAdapter, Adapter):
        assert cls.pack_refusal(()) is None, "a run without packs is as before"
    assert ClaudeCodeAdapter.pack_refusal(full) is None
    for cls in (OpencodeAdapter, CodexAdapter):
        assert cls.pack_refusal(full) == (
            f"agent pack {PLUGIN}/reviewer {VERSION} holds subagents, hooks, which {cls.runtime} does not load: run it "
            "on claude-code, or attach a pack without them"
        )
        assert cls.pack_refusal(lean) is None
    sse = {
        **lean_files(),
        "packs/reviewer/.mcp.json": json.dumps({"mcpServers": {"feed": {"type": "sse", "url": "https://f.test/sse"}}}),
    }
    over_sse = built(tmp_path / "sse", sse)
    assert OpencodeAdapter.pack_refusal(over_sse) is None
    assert CodexAdapter.pack_refusal(over_sse) == (
        f"agent pack {PLUGIN}/reviewer {VERSION} has MCP server feed over sse, which codex does not connect to (it "
        "takes http, stdio): run it on claude-code"
    )

    class Plugin(Adapter):  # a runtime of a plugin (ACP), or any adapter that names no pack_kinds
        runtime = "acme/agent"

    assert Plugin.pack_refusal(lean) == (
        f"the acme/agent adapter of this worker loads no agent pack, and the run's agent attaches {PLUGIN}/reviewer "
        f"{VERSION}: run it on claude-code, opencode or codex"
    )


def test_two_packs_with_one_skill_name_are_refused_where_the_runtime_lists_skills_bare(tmp_path):
    first = built(
        tmp_path / "a",
        {"packs/notes/skills/notes/SKILL.md": NOTES_FILES["packs/notes/skills/notes/SKILL.md"]},
        names=("notes",),
    )
    files = {"packs/memo/skills/notes/SKILL.md": NOTES_FILES["packs/notes/skills/notes/SKILL.md"]}
    second = built(tmp_path / "b", files, names=("memo",))
    both = (*first, *second)
    assert ClaudeCodeAdapter.pack_refusal(both) is None, "Claude Code names them by their plugin"
    assert OpencodeAdapter.pack_refusal(both) == (
        f"agent packs {PLUGIN}/notes {VERSION} and {PLUGIN}/memo {VERSION} both hold skill notes, which opencode lists "
        "once"
    )


# Claude Code


def _options(adapter):
    adapter._session = SESSION
    return adapter.build_options("/opt/bin/claude")


def test_claude_code_loads_each_pack_as_a_plugin_with_its_mcp_servers_and_instructions(tmp_path):
    needs("claude_agent_sdk")
    found = built(tmp_path)
    root = found[0].root
    plain = _claude(tmp_path, _samples)
    before = _command(_options(plain))
    options = _options(with_packs(plain, found))
    command = _command(options)
    assert options.plugins == [{"type": "local", "path": str(root)}]
    assert command[command.index("--plugin-dir") + 1] == str(root)
    assert options.mcp_servers == {
        "docs": {"type": "http", "url": "https://docs.example.test/mcp", "headers": {"X-Team": "acme"}},
        "lint": {"type": "stdio", "command": f"{root}/bin/lint", "args": ["--fast"], "env": {"MODE": "strict"}},
    }
    assert json.loads(command[command.index("--mcp-config") + 1]) == {"mcpServers": options.mcp_servers}
    append = options.system_prompt["append"]
    assert append == _options(plain).system_prompt["append"] + (
        f"\n\nThe instructions of agent pack {PLUGIN}/reviewer (version {VERSION}):\n\nCite the line you review."
    )
    assert "--plugin-dir" not in before and "--mcp-config" not in before
    assert _command(_options(with_packs(plain, ()))) == before, "no pack: the launch of a run before packs"


def test_pack_skills_and_servers_count_as_named_on_an_allowlist_profile_on_claude_code(tmp_path):
    needs("claude_agent_sdk")
    found = built(tmp_path)
    limited = profile(tool=["read", "edit", "mcp:github"], skill=["dataviz"], model=["opus"], instructions=INSTRUCTIONS)
    adapter = with_packs(_claude(tmp_path, _samples, profile=limited, model="opus"), found)
    options = _options(adapter)
    command = _command(options)
    assert options.skills == ["dataviz", f"{PLUGIN}.reviewer:review"]
    settings = json.loads(command[command.index("--settings") + 1])
    assert settings == {
        "allowedMcpServers": [{"serverName": "github"}, {"serverName": "docs"}, {"serverName": "lint"}],
        "availableModels": ["opus"],
    }
    tools = command[command.index("--tools") + 1].split(",")
    assert "Bash" not in tools and "WebFetch" not in tools and "Agent" not in tools, "no other axis widens"
    append = options.system_prompt["append"]
    assert append.index(INSTRUCTIONS) < append.index("Cite the line you review."), "the pack's after the profile's"
    applied = profiles.profile_of(adapter.context)
    refusal = profiles.claude_call_refusal
    assert refusal(applied, "mcp__lint__check", {}) is None and refusal(applied, "mcp__docs__search", {}) is None
    assert refusal(applied, "Skill", {"skill": f"{PLUGIN}.reviewer:review"}) is None
    assert "does not allow the tools of MCP server slack" in refusal(applied, "mcp__slack__post", {})
    assert "does not allow skill deploy" in refusal(applied, "Skill", {"skill": "deploy"})
    assert applied.allowed("model") == ["opus"] and applied.allowed("credential") is None


def test_packs_widen_no_axis_of_mode_all_and_leave_a_profile_that_changes_nothing_alone(tmp_path):
    found = built(tmp_path)
    context = dataclasses.replace(_claude(tmp_path, _samples, profile=DEFAULT).context, packs=found)
    assert profiles.profile_of(context) is None, "an all-access profile with packs still changes nothing it would not"
    only_tools = profiles.Profile.of({"profile": profile(tool=["read"])})
    widened = profiles.with_packs(only_tools, ["review"], ["lint"])
    assert widened.allowed("tool") == ["read", "mcp:lint"] and widened.allowed("skill") is None
    assert profiles.with_packs(only_tools, [], []) == only_tools


# opencode


def test_opencode_gets_the_skills_mcp_servers_and_instructions_of_its_packs(tmp_path):
    found = built(tmp_path, lean_files())
    root = found[0].root
    limited = profile(tool=["read", "edit", "bash", "web", "agent"], skill=["dataviz"], model=[MODEL])
    adapter = with_packs(_opencode(tmp_path, _opencode_samples, model=MODEL, profile=limited), found)

    async def go():
        original = adapter.start_server

        async def start_server():
            server = await original()
            adapter.fake.mcp = {name: {"status": "connected"} for name in ("lint", "docs", "slack")}
            return server

        adapter.start_server = start_server
        return await _collect(adapter)

    events, outcome = asyncio.run(go())
    assert outcome.completed
    content = json.loads(adapter.server_env()["OPENCODE_CONFIG_CONTENT"])
    assert content["skills"] == {"paths": [str(root / "skills")]}
    assert content["mcp"] == {
        "docs": {"type": "remote", "url": "https://docs.example.test/mcp", "headers": {"X-Team": "acme"}},
        "lint": {"type": "local", "command": [f"{root}/bin/lint", "--fast"], "environment": {"MODE": "strict"}},
    }
    assert content["permission"]["skill"] == {"*": "deny", "dataviz": "allow", "review": "allow"}
    create = next(request for request in adapter.fake.requests if request[0] == "create")
    denied = [rule["permission"] for rule in create[2]["permission"] if rule["action"] == "deny"]
    assert "slack_*" in denied and "lint_*" not in denied and "docs_*" not in denied
    prompt = next(request for request in adapter.fake.requests if request[0] == "prompt")
    assert prompt[2]["system"] == (
        f"{common.HEADLESS_NOTE}\n\nThe instructions of agent pack {PLUGIN}/reviewer (version {VERSION}):\n\n"
        "Cite the line you review."
    )


def test_opencode_pack_config_merges_over_the_owners_config_content(tmp_path):
    found = built(tmp_path, lean_files())
    own = {"model": "a/b", "skills": {"paths": ["/mine"]}, "mcp": {"owner": {"type": "local", "command": ["o"]}}}
    env = {"OPENCODE_CONFIG_CONTENT": json.dumps(own), "PATH": "/bin"}
    content = json.loads(packs.opencode_env(env, found)["OPENCODE_CONFIG_CONTENT"])
    assert content["model"] == "a/b" and content["skills"]["paths"] == ["/mine", str(found[0].root / "skills")]
    assert sorted(content["mcp"]) == ["docs", "lint", "owner"]
    assert packs.opencode_env(env, ()) == env


# Codex


def test_codex_gets_the_skill_roots_mcp_servers_and_instructions_of_its_packs(tmp_path):
    needs("openai_codex")
    found = built(tmp_path, lean_files())
    root = found[0].root
    plain = _codex(tmp_path, _codex_samples, effort="low")

    class Recorded(type(plain)):
        async def set_skill_roots(self, roots):
            self.fake.log.append(("skills/extraRoots/set", roots))

    adapter = Recorded(dataclasses.replace(plain.context, packs=found))
    asyncio.run(_collect(adapter))
    assert adapter.fake.log[0] == ("skills/extraRoots/set", [str(root / "skills")]), "before the thread starts"
    method, options = adapter.fake.log[1]
    assert method == "thread/start"
    assert options["config"] == {
        **thread_compaction(adapter.context),  # what the owner's codex config leaves to the thread
        "model_reasoning_effort": "low",
        "mcp_servers": {
            "docs": {"url": "https://docs.example.test/mcp", "http_headers": {"X-Team": "acme"}},
            "lint": {"command": f"{root}/bin/lint", "args": ["--fast"], "env": {"MODE": "strict"}},
        },
    }
    assert options["developer_instructions"].endswith("Cite the line you review.")

    recorded = []

    class Client:
        async def request(self, method, params, *, response_model):
            recorded.append((method, params, response_model.__name__))

    adapter._codex = SimpleNamespace(_client=Client())
    asyncio.run(CodexAdapter.set_skill_roots(adapter, ["/a/skills"]))
    assert recorded == [("skills/extraRoots/set", {"extraRoots": ["/a/skills"]}, "SkillsExtraRootsSetResponse")]


def test_codex_pack_servers_stay_on_under_a_profile_that_turns_off_the_owners(tmp_path):
    needs("openai_codex")
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text('[mcp_servers.slack]\ncommand = "s"\n')
    found = built(tmp_path, lean_files())
    limited = profile(tool=["read", "edit"], model=["gpt-5.5"])
    plain = _codex(tmp_path, _codex_samples, profile=limited, model="gpt-5.5", env={"CODEX_HOME": str(home)})

    class Recorded(type(plain)):
        async def set_skill_roots(self, roots):
            self.fake.log.append(("skills/extraRoots/set", roots))

    adapter = Recorded(dataclasses.replace(plain.context, packs=found))
    asyncio.run(_collect(adapter))
    servers = adapter.fake.log[1][1]["config"]["mcp_servers"]
    assert servers["slack"] == {"enabled": False} and sorted(servers) == ["docs", "lint", "slack"]
    assert servers["lint"]["command"] == f"{found[0].root}/bin/lint"


# Where the agent runs


def test_a_container_mounts_each_pack_read_only_at_its_path(tmp_path):
    from tests.worker.test_environments import Engine, checkout_with_worktree, container, context_of

    found = built(tmp_path, lean_files())
    engine = Engine(tmp_path)
    _, worktree = checkout_with_worktree(tmp_path)
    box = container(tmp_path, {"PATH": os.defpath, **engine.env})
    directory = tmp_path / "adapter"
    directory.mkdir()
    context = dataclasses.replace(context_of(box, worktree, {"PATH": os.defpath}), packs=found)
    box.command("claude", ["--print"], context=context, directory=directory)
    before = json.loads((directory / "container.json").read_text(encoding="utf-8"))["before"]
    mounts = [before[i + 1] for i, item in enumerate(before) if item == "--mount"]
    root = found[0].root
    assert f"type=bind,src={root},dst={root},readonly" in mounts
    box.command("claude", ["--print"], context=dataclasses.replace(context, packs=()), directory=directory)
    before = json.loads((directory / "container.json").read_text(encoding="utf-8"))["before"]
    assert not any(str(root) in item for item in before), "no pack, no mount of one"


def test_a_run_with_packs_is_never_handed_to_a_terminal():
    from evo_agents.worker.runner.agent import Agent

    run = SimpleNamespace(
        spec={"packs": [{"pack": f"{PLUGIN}/reviewer"}]},
        runtime="claude-code",
        daemon=SimpleNamespace(adapters={"claude-code": ClaudeCodeAdapter}, tmux=SimpleNamespace(available=True)),
    )
    agent = Agent.__new__(Agent)
    agent.run, agent.terminal = run, SimpleNamespace(failed=None)
    assert (
        agent.interactive_unsupported() == "its agent profile attaches agent packs, which the terminal UI does not load"
    )
    run.spec = {"packs": []}
    assert agent.interactive_unsupported() is None


# The daemon


from tests.worker import fake_adapter  # noqa: E402
from tests.worker.test_plan_runs import PLAN, machine, wait_for, with_daemon  # noqa: E402, F401


def _queue_with_packs(hub, items: list[dict]) -> int:
    run_id = hub.queue_step_run(PLAN, "1", "alpha", "feat/alpha")
    hub.runs[run_id]["spec"]["packs"] = items
    return run_id


def _pack_daemon(machine, tmp_path, monkeypatch, *, accepted, steps=None) -> dict:  # noqa: F811
    held = bundle(tmp_path / "bundles")
    monkeypatch.setattr(packs.skill_sync, "get_bundle", Store(held))
    if steps is not None:
        machine.scenarios({"1": steps})
    found = {}

    async def body(hub, daemon):
        daemon.config.plugins = list(accepted)
        run_id = _queue_with_packs(hub, [ticket(held)])
        await hub.wait_state(run_id, "done", "failed", "review")
        await wait_for(lambda: not daemon.runs, "the run to let go of its slot")
        found.update(hub=hub, run=run_id, directory=machine.home.run_dir(run_id) / packs.PACKS_DIR)

    with_daemon(machine, body)
    return found


def test_a_pack_run_whose_pack_does_not_build_fails_before_its_agent_starts(machine, tmp_path, monkeypatch):  # noqa: F811
    found = _pack_daemon(machine, tmp_path, monkeypatch, accepted=[])
    hub, run_id = found["hub"], found["run"]
    run = hub.runs[run_id]
    why = (
        f"agent pack {PLUGIN}/reviewer is of plugin {PLUGIN}, which the owner of this worker has not accepted: accept "
        "it in the worker's settings on the hub"
    )
    assert run["state"] == "failed" and run["failure_cause"] == "runtime"
    assert run["error"] == f"agent packs: {why}"
    assert f"Agent packs: {why}; the agent did not start." in hub.texts(run_id)
    assert not machine.starts_path.exists(), "the agent never started"


def test_a_pack_run_on_a_runtime_that_loads_no_pack_fails_before_its_agent_starts(machine, tmp_path, monkeypatch):  # noqa: F811
    found = _pack_daemon(machine, tmp_path, monkeypatch, accepted=[PLUGIN])
    hub, run_id = found["hub"], found["run"]
    run = hub.runs[run_id]
    assert run["state"] == "failed" and run["failure_cause"] == "runtime"
    assert run["error"].startswith("agent packs: the claude-code adapter of this worker loads no agent pack")
    assert not machine.starts_path.exists(), "the agent never started"


def test_a_pack_run_goes_on_with_its_packs_built_for_each_agent(machine, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(fake_adapter.FakeAdapter, "pack_kinds", packs.KINDS)
    monkeypatch.setattr(fake_adapter.FakeAdapter, "pack_transports", packs.TRANSPORTS)
    seen = []
    original = fake_adapter.FakeAdapter.__init__

    def init(self, context):
        seen.append(context.packs)
        original(self, context)

    monkeypatch.setattr(fake_adapter.FakeAdapter, "__init__", init)
    steps = [{"write": {"a.txt": "a\n"}}, {"result": {"verify_commands": ["test -f a.txt"]}}]
    found = _pack_daemon(machine, tmp_path, monkeypatch, accepted=[PLUGIN], steps=steps)
    hub, run_id = found["hub"], found["run"]
    assert hub.runs[run_id]["state"] == "done", hub.texts(run_id)
    assert [start["run"] for start in machine.starts()][:1] == [run_id], "the agent started once the packs built"
    assert (
        f"Agent packs built for the agent: {PLUGIN}/reviewer {VERSION} (skills, subagents, hooks, MCP servers, "
        "instructions)."
    ) in hub.texts(run_id)
    assert seen and all(len(item) == 1 for item in seen)
    assert seen[0][0].root == found["directory"] / PLUGIN / "reviewer"


# The pack of the core plugin browser (plan agent-browser, criterion a8)

CDP = "http://127.0.0.1:41234"


def browser_built(tmp_path, tickets=()) -> tuple[packs.Pack, ...]:
    return packs.build(list(tickets), tmp_path / "run" / "packs", browser=True)


def test_browser_pack_is_built_for_a_run_with_a_browser_alone_and_names_its_chromium(tmp_path):
    from evo_agents.core_plugins import browser

    assert packs.build([], tmp_path / "none", browser=False) == ()
    (found,) = browser_built(tmp_path)
    assert (found.pack, found.plugin, found.name) == (packs.BROWSER_PACK, "browser", "browser")
    (server,) = found.components.servers
    assert server.name == "playwright" and server.args[:2] == ("-y", browser.PLAYWRIGHT_MCP)
    assert server.args[-2:] == ("--cdp-endpoint", "${" + packs.BROWSER_VARIABLE + "}")
    assert "evo-agents worker browser login" in (found.root / "INSTRUCTIONS.md").read_text(encoding="utf-8")

    plain = packs.claude_servers([found])["playwright"]
    assert "${" + packs.BROWSER_VARIABLE + "}" in plain["args"]  # no Chromium yet: the name stays
    found.variables[packs.BROWSER_VARIABLE] = CDP
    assert packs.claude_servers([found])["playwright"]["args"][-2:] == ["--cdp-endpoint", CDP]
    assert packs.codex_servers([found])["playwright"]["args"][-1] == CDP
    assert packs.opencode_server(found.servers()[0])["command"][-1] == CDP


def test_browser_pack_is_given_once_when_the_profile_attaches_it_too_and_beside_the_profiles_packs(tmp_path):
    held = bundle(tmp_path)
    store = Store(held)
    both = packs.build([ticket(held)], tmp_path / "run" / "packs", fetch=store, browser=True)
    assert [pack.pack for pack in both] == [f"{PLUGIN}/reviewer", packs.BROWSER_PACK]
    assert packs.build([ticket(held)], tmp_path / "again", fetch=store, browser=False)[0].pack == f"{PLUGIN}/reviewer"
    attached = bundle(tmp_path, names=("browser",), plugin="browser")  # a profile that attaches the pack by itself
    twice = packs.build([ticket(attached, "browser")], tmp_path / "twice", fetch=Store(attached), browser=True)
    assert [pack.pack for pack in twice] == [packs.BROWSER_PACK]


def test_browser_pack_is_refused_on_a_runtime_without_mcp_servers_before_the_agent_starts(tmp_path):
    (found,) = browser_built(tmp_path)
    why = packs.refusal([found], "bare", kinds=packs.KINDS, transports=("http",))
    assert why and "playwright" in why and "does not connect to" in why
    assert packs.refusal([found], "claude-code", kinds=packs.KINDS, transports=("stdio", "http")) is None
