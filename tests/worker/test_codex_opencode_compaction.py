"""The compaction settings of a Codex or opencode run (criterion a8 of the lean-cached-context plan).

- Codex: the thread's config gets ``model_auto_compact_token_limit`` and ``tool_output_token_limit``
  (``codex.compact_config``), 200,000 and 8,000 tokens unless EVO_WORKER_CODEX_AUTO_COMPACT_TOKEN_LIMIT or
  EVO_WORKER_CODEX_TOOL_OUTPUT_TOKEN_LIMIT sets another value or turns it off, on a new thread and on a resumed one, and
  the terminal UI gets them with ``-c``; a key the owner's codex config sets stays the owner's, and the profile's config
  is merged last.
- opencode: the run's server gets ``compaction.prune`` on in OPENCODE_CONFIG_CONTENT (``opencode.compaction_env``),
  merged into what the variable held, headless and for the terminal UI, unless EVO_WORKER_OPENCODE_COMPACTION_PRUNE
  turns it off or the owner's config sets it.

What Codex 0.153.4 itself does with the two keys was checked on the laptop against its app-server (docs/workers.md,
Runtime adapters).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from evo_agents.worker import interactive
from evo_agents.worker.adapter import RunContext
from evo_agents.worker.runtimes import codex, opencode, profiles
from evo_agents.worker.runtimes.codex import AUTO_COMPACT_LIMIT, TOOL_OUTPUT_LIMIT, compact_config, owner_compact_keys
from evo_agents.worker.runtimes.opencode import PRUNE_SETTING, compaction_env
from tests.worker.test_runtimes import THREAD, _codex, _codex_samples, _collect

AUTO = "EVO_WORKER_CODEX_AUTO_COMPACT_TOKEN_LIMIT"
TOOL = "EVO_WORKER_CODEX_TOOL_OUTPUT_TOKEN_LIMIT"
DEFAULTS = {AUTO_COMPACT_LIMIT: 200_000, TOOL_OUTPUT_LIMIT: 8_000}
PRUNED = {"compaction": {"prune": True}}


# Codex


def test_codex_compaction_is_200000_and_8000_tokens_unless_the_worker_says_otherwise():
    assert dict((key, default) for key, _, default in codex.COMPACT_SETTINGS) == DEFAULTS
    assert compact_config({}) == DEFAULTS
    assert compact_config({AUTO: "350000", TOOL: " 12000 "}) == {AUTO_COMPACT_LIMIT: 350_000, TOOL_OUTPUT_LIMIT: 12_000}
    for off in ("off", "OFF", "0"):
        assert compact_config({AUTO: off}) == {TOOL_OUTPUT_LIMIT: 8_000}, "codex's own compaction"
        assert compact_config({TOOL: off}) == {AUTO_COMPACT_LIMIT: 200_000}, "codex's own budget"
    for wrong in ("500k", "lots", "-1", "²"):
        assert compact_config({AUTO: wrong, TOOL: wrong}) == DEFAULTS
    assert compact_config({AUTO: "300000"}, {AUTO_COMPACT_LIMIT}) == {TOOL_OUTPUT_LIMIT: 8_000}, "the owner's key"
    assert compact_config({}, {AUTO_COMPACT_LIMIT, TOOL_OUTPUT_LIMIT}) == {}


def test_codex_owner_config_keeps_the_keys_it_sets(tmp_path):
    home, worktree = tmp_path / "codex-home", tmp_path / "worktree"
    (worktree / ".codex").mkdir(parents=True)
    home.mkdir()
    assert owner_compact_keys(home, worktree) == set(), "no config"
    (home / "config.toml").write_text('model = "gpt-5.5"\nmodel_auto_compact_token_limit = 800000\n')
    assert owner_compact_keys(home, worktree) == {AUTO_COMPACT_LIMIT}
    (home / "config.toml").write_text(
        'profile = "work"\n[profiles.work]\ntool_output_token_limit = 4000\n'
        "[profiles.other]\nmodel_auto_compact_token_limit = 1\n"
    )
    assert owner_compact_keys(home, worktree) == {TOOL_OUTPUT_LIMIT}, "the profile config.toml names, no other"
    (home / "config.toml").write_text("not toml = = =")
    (worktree / ".codex" / "config.toml").write_text("model_auto_compact_token_limit = 150000\n")
    assert owner_compact_keys(home, worktree) == {AUTO_COMPACT_LIMIT}, "the worktree's config"


def test_codex_thread_gets_the_compaction_on_start_and_on_resume(tmp_path):
    home = tmp_path / "codex-home"
    home.mkdir()
    env = {"CODEX_HOME": str(home), TOOL: "12000"}
    adapter = _codex(tmp_path, _codex_samples, env=env)
    asyncio.run(_collect(adapter))
    assert adapter.fake.log[0][0] == "thread/start"
    assert adapter.fake.log[0][1]["config"] == {AUTO_COMPACT_LIMIT: 200_000, TOOL_OUTPUT_LIMIT: 12_000}
    adapter = _codex(tmp_path, _codex_samples, env=env, resume=THREAD)
    asyncio.run(_collect(adapter))
    assert adapter.fake.log[0][:2] == ("thread/resume", THREAD)
    assert adapter.fake.log[0][2]["config"] == {AUTO_COMPACT_LIMIT: 200_000, TOOL_OUTPUT_LIMIT: 12_000}


def test_codex_thread_leaves_the_owners_keys_and_takes_the_profiles_config_last(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text("model_auto_compact_token_limit = 800000\n")
    adapter = _codex(tmp_path, _codex_samples, env={"CODEX_HOME": str(home)})
    asyncio.run(_collect(adapter))
    assert adapter.fake.log[0][1]["config"] == {TOOL_OUTPUT_LIMIT: 8_000}, "codex reads the owner's 800,000 itself"
    off = {"CODEX_HOME": str(home), TOOL: "off"}
    adapter = _codex(tmp_path, _codex_samples, env=off)
    asyncio.run(_collect(adapter))
    assert adapter.fake.log[0][1]["config"] is None, "nothing to send: codex's own config throughout"
    monkeypatch.setattr(profiles, "codex_config", lambda profile, servers: {TOOL_OUTPUT_LIMIT: 1_000})
    adapter = _codex(tmp_path, _codex_samples, env={"CODEX_HOME": str(home)})
    asyncio.run(_collect(adapter))
    assert adapter.fake.log[0][1]["config"] == {TOOL_OUTPUT_LIMIT: 1_000}


def _tui_context(tmp_path: Path, env: dict[str, str]) -> RunContext:
    """A run's context for a terminal UI, with a ``codex`` and an ``opencode`` on PATH that are never run."""
    worktree = tmp_path / "worktree"
    worktree.mkdir(exist_ok=True)
    (tmp_path / "bin").mkdir(exist_ok=True)
    for name in ("codex", "opencode"):
        (tmp_path / "bin" / name).write_text("#!/bin/sh\nexit 3\n")
        (tmp_path / "bin" / name).chmod(0o755)
    environment = {"HOME": str(tmp_path / "home"), "PATH": str(tmp_path / "bin"), **env}
    return RunContext(run={"id": 7, "title": "the step"}, worktree=worktree, prompt="Do the step.", env=environment)


def test_codex_terminal_ui_gets_the_same_compaction_with_c(tmp_path):
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text("tool_output_token_limit = 4000\n")
    tui = interactive.CodexTui(_tui_context(tmp_path, {"CODEX_HOME": str(home), AUTO: "250000"}), "thread-1")
    argv = tui.command("evo-run-7")
    assert argv[argv.index("-c") : argv.index("-C")] == ["-c", "model_auto_compact_token_limit=250000"]
    plain = interactive.CodexTui(_tui_context(tmp_path, {"CODEX_HOME": str(home), AUTO: "off"}), "thread-1")
    assert "-c" not in plain.command("evo-run-7")


# opencode


def _owner(tmp_path: Path, **env: str) -> dict[str, str]:
    """An environment whose opencode config folders are empty folders of the test's."""
    return {"HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "xdg"), **env}


def test_opencode_server_prunes_old_tool_outputs_merged_into_the_config_content(tmp_path):
    env = _owner(tmp_path)
    assert json.loads(compaction_env(env, tmp_path)[profiles.CONFIG_CONTENT]) == PRUNED
    own = json.dumps({"model": "a/b", "compaction": {"auto": False, "reserved": 5000}})
    merged = json.loads(compaction_env({**env, profiles.CONFIG_CONTENT: own}, tmp_path)[profiles.CONFIG_CONTENT])
    assert merged == {"model": "a/b", "compaction": {"auto": False, "reserved": 5000, "prune": True}}
    assert compaction_env(env, tmp_path)["HOME"] == env["HOME"], "the rest of the environment"


def test_opencode_prune_is_off_when_the_worker_says_so_or_the_content_is_no_object(tmp_path):
    for off in ("off", "FALSE", "0"):
        env = _owner(tmp_path, **{PRUNE_SETTING: off})
        assert compaction_env(env, tmp_path) == env
    for content in ("[1, 2]", "not json"):
        env = _owner(tmp_path, **{profiles.CONFIG_CONTENT: content})
        assert compaction_env(env, tmp_path) == env, "the owner's variable is left as it is"


@pytest.mark.parametrize(
    "where",
    ["content", "xdg", "home", "opencode_config", "worktree", "worktree_dot_opencode"],
)
def test_opencode_owner_config_that_sets_prune_keeps_it(tmp_path, where):
    worktree = tmp_path / "worktree"
    (worktree / ".opencode").mkdir(parents=True)
    owned = '{\n  // the owner\'s choice\n  "compaction": {"prune": false,},\n}\n'
    env = _owner(tmp_path)
    if where == "content":
        env[profiles.CONFIG_CONTENT] = json.dumps({"compaction": {"prune": False}})
    elif where == "xdg":
        (tmp_path / "xdg" / "opencode").mkdir(parents=True)
        (tmp_path / "xdg" / "opencode" / "opencode.jsonc").write_text(owned)
    elif where == "home":
        del env["XDG_CONFIG_HOME"]
        (tmp_path / "home" / ".config" / "opencode").mkdir(parents=True)
        (tmp_path / "home" / ".config" / "opencode" / "config.json").write_text(owned)
    elif where == "opencode_config":
        (tmp_path / "custom.json").write_text(owned)
        env["OPENCODE_CONFIG"] = str(tmp_path / "custom.json")
    elif where == "worktree":
        (worktree / "opencode.json").write_text(owned)
    else:
        (worktree / ".opencode" / "opencode.jsonc").write_text(owned)
    assert opencode.owner_prune(env, worktree)
    assert compaction_env(env, worktree) == env


def test_opencode_owner_config_without_prune_or_unreadable_leaves_it_to_the_run(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "opencode.json").write_text('{"compaction": {"auto": true}, "model": "a/b"}')
    (worktree / "opencode.jsonc").write_text("{ not json")
    env = _owner(tmp_path)
    assert not opencode.owner_prune(env, worktree)
    assert json.loads(compaction_env(env, worktree)[profiles.CONFIG_CONTENT]) == PRUNED


def test_opencode_jsonc_reads_comments_and_trailing_commas_outside_strings():
    text = '{\n  // a comment\n  "url": "http://x//y", /* block */ "list": [1, 2,],\n  "s": "a\\"/*b*/",\n}'
    assert opencode.jsonc(text) == {"url": "http://x//y", "list": [1, 2], "s": 'a"/*b*/'}
    assert opencode.jsonc("{ not json") is None


def test_opencode_terminal_ui_server_prunes_as_the_headless_one_does(tmp_path, monkeypatch):
    seen = {}

    async def start_server(binary, cwd, env):
        seen.update(env)
        return opencode.Server("http://127.0.0.1:4567", "the-password")

    monkeypatch.setattr(opencode, "start_server", start_server)
    tui = interactive.OpencodeTui(_tui_context(tmp_path, _owner(tmp_path)), "ses_1")
    asyncio.run(tui.start_server())
    assert json.loads(seen[profiles.CONFIG_CONTENT]) == PRUNED
