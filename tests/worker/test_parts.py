"""The parts of the worker daemon that need no hub: the spool, the log, the checkouts, the runtimes, the backoff and
the cleanup of old worktrees."""

import asyncio
import json
import logging
import os
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from evo_agents.hub import runs
from evo_agents.worker import adapter as adapter_module
from evo_agents.worker import checkouts, logs
from evo_agents.worker.home import PidLock, WorkerConfig, WorkerHome
from evo_agents.worker.spool import Spool, SpoolBudget, leftover_runs

AT = datetime(2026, 10, 5, 9, 12, 3, tzinfo=timezone.utc)


# The spool


def test_the_spool_numbers_events_sends_them_in_batches_and_forgets_what_the_hub_acknowledged(tmp_path):
    budget = SpoolBudget()
    spool = Spool(tmp_path, 7, budget)
    for number in range(1, 8):
        assert spool.append("agent_message_chunk", {"text": f"chunk {number}"}, AT) == number
    assert spool.pending == 7 and budget.used > 0
    batch = spool.batch(max_events=3)
    assert [event["seq"] for event in batch] == [1, 2, 3]
    assert batch[0] == {
        "seq": 1,
        "at": "2026-10-05T09:12:03.000Z",
        "kind": "agent_message_chunk",
        "body": {"text": "chunk 1"},
    }
    assert spool.acknowledge(3) == 3
    assert [event["seq"] for event in spool.batch()] == [4, 5, 6, 7]
    assert spool.acknowledge(2) == 0, "an older ack changes nothing"
    spool.acknowledge(7)
    assert spool.pending == 0 and budget.used == 0
    assert spool.path.stat().st_size == 0, "an acknowledged spool is emptied"
    assert spool.append("system", {"text": "after"}, AT) == 8, "the seq goes on"
    assert stat.S_IMODE(spool.path.stat().st_mode) == 0o600


def test_a_daemon_that_restarts_sends_what_the_spool_kept_and_drops_a_line_cut_by_a_crash(tmp_path):
    spool = Spool(tmp_path, 9, SpoolBudget())
    for number in range(1, 6):
        spool.append("output", {"n": number}, AT)
    spool.acknowledge(2)
    with open(spool.path, "ab") as handle:
        handle.write(b'{"seq": 6, "at": "2026-10-05T09:')  # the crash came in the middle of a line
    again = Spool(tmp_path, 9, SpoolBudget())
    assert [event["seq"] for event in again.batch()] == [3, 4, 5]
    assert again.append("output", {"n": 6}, AT) == 6
    assert [event["body"]["n"] for event in again.batch()] == [3, 4, 5, 6]
    assert leftover_runs(tmp_path) == [9]
    again.remove()
    assert leftover_runs(tmp_path) == []


def test_a_full_spool_drops_events_without_a_gap_in_the_seq_and_says_so_once_there_is_room(tmp_path):
    line = len(json.dumps({"seq": 1, "at": "2026-10-05T09:12:03.000Z", "kind": "output", "body": {"n": 1}})) + 1
    budget = SpoolBudget(limit=line * 3 + 40)
    spool = Spool(tmp_path, 3, budget)
    assert [spool.append("output", {"n": n}, AT) for n in range(1, 6)] == [1, 2, 3, None, None]
    assert spool.dropped == 2
    spool.acknowledge(3)
    seq = spool.append("output", {"n": 6}, AT)
    events = spool.batch()
    assert [event["seq"] for event in events] == [4, 5] and seq == 5
    assert events[0]["kind"] == "system" and events[0]["body"]["dropped"] == 2
    assert "spool was full" in events[0]["body"]["text"]


def test_a_body_over_64_kib_is_cut_before_it_is_spooled_so_a_batch_fits(tmp_path):
    spool = Spool(tmp_path, 4, SpoolBudget())
    spool.append("tool_call_update", {"output": "x" * (200 * 1024)}, AT)
    event = spool.batch()[0]
    size = len(json.dumps(event["body"], ensure_ascii=False, separators=(",", ":")).encode())
    assert size <= runs.MAX_EVENT_BODY_BYTES, "measured as the hub measures it"
    assert event["body"]["output"].endswith("[cut by the hub: the event was over 65536 bytes]")


# The log


def test_the_log_is_private_json_lines_without_tokens(tmp_path):
    path = tmp_path / "worker.log"
    token = "evw_" + "A" * 43
    logs.configure(path, stderr=False, secrets=[token])
    try:
        logging.getLogger("evo_agents.worker").warning(
            "claim failed with %s", f"Bearer {token}", extra={"header": f"Authorization: Bearer {token}"}
        )
    finally:
        for handler in list(logging.getLogger().handlers):
            if isinstance(handler, logs._WorkerFile):
                logging.getLogger().removeHandler(handler)
                handler.close()
    text = path.read_text(encoding="utf-8")
    assert token not in text and "AAAAAAAA" not in text
    line = json.loads(text.splitlines()[-1])
    assert line["level"] == "warning" and line["logger"] == "evo_agents.worker"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# The state directory


def test_the_state_directory_is_private_and_holds_the_token_apart(tmp_path):
    home = WorkerHome(tmp_path / "worker")
    config = WorkerConfig(url="https://hub.example.org", worker_id=4, name="mac", projects=["demo"])
    home.save(config, "evw_" + "b" * 43)
    assert stat.S_IMODE(home.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(home.token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(home.config_path.stat().st_mode) == 0o600
    assert home.load_config() == config
    assert "evw_" not in home.config_path.read_text(encoding="utf-8")
    lock = PidLock(home.pid_path)
    assert lock.acquire()
    assert not PidLock(home.pid_path).acquire(), "a second daemon refuses to start"
    lock.release()
    assert home.read_pid() is None


# Checkouts


def _repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", "--initial-branch", branch, str(path)], check=True)
    return path


def test_checkouts_come_from_config_then_the_hubs_repos_then_the_registry(tmp_path):
    workspace = tmp_path / "ws"
    agents = _repo(workspace / "evo-agents")
    cli = _repo(workspace / "cli-checkout", branch="dev")
    _repo(tmp_path / "elsewhere" / "skills")
    (workspace / "not-a-repo").mkdir()
    manual = _repo(tmp_path / "manual")
    config = WorkerConfig(
        url="https://hub.example.org",
        worker_id=1,
        name="mac",
        projects=["demo", "other"],
        repos={"demo": {"workspace": str(tmp_path / "unused"), "repos": [{"name": "evo-cli", "path": "cli-checkout"}]}},
        checkouts={"other/tool": str(manual), "stranger/tool": str(manual)},
    )
    clusters = [
        {
            "name": "demo",
            "workspace": str(workspace),
            "repos": [str(agents), str(workspace / "not-a-repo")],
            "hub": {"url": "https://hub.example.org/", "project": "demo"},
        },
        {
            "name": "x",
            "repos": [str(tmp_path / "elsewhere" / "skills")],
            "hub": {"url": "https://another.example.org", "project": "demo"},
        },
    ]
    found = checkouts.discover(config, clusters)
    assert found == {
        "demo/evo-agents": {"path": str(agents), "branch": "main"},
        "demo/evo-cli": {"path": str(cli), "branch": "dev"},
        "other/tool": {"path": str(manual), "branch": "main"},
    }


def test_registry_files_are_read_in_order_and_a_broken_one_is_skipped(tmp_path):
    first, broken = tmp_path / "a.json", tmp_path / "b.json"
    first.write_text(json.dumps({"clusters": [{"name": "demo"}]}), encoding="utf-8")
    broken.write_text("{", encoding="utf-8")
    assert checkouts.registry_clusters([first, broken, tmp_path / "missing.json"]) == [{"name": "demo"}]


# Runtimes and adapters


class _Fake(adapter_module.Adapter):
    runtime = "codex"
    binary = "codex"

    @classmethod
    def detect(cls):
        return adapter_module.Detection(True, "0.153.4")


def test_a_runtime_without_an_adapter_is_unavailable_even_with_its_binary(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text("#!/bin/sh\necho '2.1.289 (Claude Code)'\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    found = adapter_module.detect_runtimes({"codex": _Fake})
    assert found["codex"] == {"available": True, "version": "0.153.4", "reason": None}
    assert found["claude-code"]["available"] is False and found["claude-code"]["version"] == "2.1.289"
    assert "no adapter for claude-code" in found["claude-code"]["reason"]
    assert found["opencode"] == {"available": False, "version": None, "reason": "opencode is not on PATH"}


def test_adapters_load_from_the_environment_and_a_bad_one_is_left_out(caplog):
    env = {adapter_module.ADAPTERS_VARIABLE: f"codex={__name__}:_Fake, opencode=no.such.module:X, nope=a:b"}
    with caplog.at_level(logging.WARNING):
        loaded = adapter_module.load_adapters(env)
    assert loaded["codex"] is _Fake, "the environment wins over the package's adapter"
    assert "opencode" not in loaded, "a spec that does not load leaves its runtime without an adapter"
    assert loaded["claude-code"].__name__ == "ClaudeCodeAdapter", "the package's own adapter"
    assert "adapter not loaded" in caplog.text


def test_an_adapter_event_must_be_a_worker_kind_with_a_zoned_time():
    with pytest.raises(ValueError):
        adapter_module.AgentEvent("state", {})
    with pytest.raises(ValueError):
        adapter_module.AgentEvent("system", {"text": "the daemon's own"})
    with pytest.raises(ValueError):
        adapter_module.AgentEvent("output", {}, datetime(2026, 10, 5))
    assert adapter_module.AgentEvent("plan", {"entries": []}).at.tzinfo is not None


# Backoff


def test_the_backoff_grows_from_1_to_60_seconds():
    pytest.importorskip("aiohttp")
    from evo_agents.worker.hubapi import Backoff

    backoff = Backoff(jitter=0)
    assert [backoff.next() for _ in range(8)] == [1, 2, 4, 8, 16, 32, 60, 60]
    backoff.reset()
    assert backoff.next() == 1
    jittered = Backoff()
    assert 0.8 <= jittered.next() <= 1.2 and 1.6 <= jittered.next() <= 2.4


# Cleanup


def test_worktrees_of_runs_that_ended_over_7_days_ago_are_removed(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp")
    from evo_agents.worker.daemon import Daemon

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@b",
        "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@b",
    }
    checkout = _repo(tmp_path / "checkout")
    (checkout / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True, env=env)
    subprocess.run(["git", "-C", str(checkout), "commit", "--quiet", "-m", "x"], check=True, env=env)
    home = WorkerHome(tmp_path / "worker")
    home.ensure()
    now = datetime.now(timezone.utc)
    for run_id, age in ((1, timedelta(days=8)), (2, timedelta(days=6))):
        path = home.worktree_path("demo", run_id)
        branch = f"evo-run/{run_id}"
        subprocess.run(["git", "-C", str(checkout), "worktree", "add", "--quiet", "-b", branch, str(path)], check=True)
        home.save_run(
            {
                "id": run_id,
                "checkout": str(checkout),
                "worktree": str(path),
                "local_branch": branch,
                "finished_at": (now - age).isoformat(),
            }
        )
    config = WorkerConfig(url="https://hub.example.org", worker_id=1, name="mac", projects=["demo"])
    daemon = Daemon(home, config, "evw_x", adapters={})
    assert asyncio.run(daemon.cleanup()) == [1]
    assert not home.worktree_path("demo", 1).exists() and home.worktree_path("demo", 2).exists()
    assert [record["id"] for record in home.load_runs()] == [2]
    branches = subprocess.run(
        ["git", "-C", str(checkout), "branch", "--list", "evo-run/*"], capture_output=True, text=True, check=True
    ).stdout
    assert "evo-run/1" not in branches and "evo-run/2" in branches


def test_a_plan_runs_directory_and_worktrees_go_7_days_after_it_ended_and_a_resumed_runs_stay(tmp_path):
    pytest.importorskip("aiohttp")
    from evo_agents.worker.daemon import Daemon
    from evo_agents.worker.gitops import Workspace

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@b",
        "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@b",
    }
    checkouts_of = {}
    for name in ("alpha", "beta"):
        checkout = _repo(tmp_path / name)
        (checkout / "README.md").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(checkout), "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", str(checkout), "commit", "--quiet", "-m", "x"], check=True, env=env)
        checkouts_of[name] = checkout
    home = WorkerHome(tmp_path / "worker")
    home.ensure()
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    for run_id in (5, 6):  # 5 ended; 6 was parked, and a run that resumed it took its worktrees over
        directory = home.worktree_path("demo", run_id)
        directory.mkdir()
        repos = []
        for name, checkout in checkouts_of.items():
            branch = f"evo-run/{run_id}/{name}"
            path = directory / name
            subprocess.run(
                ["git", "-C", str(checkout), "worktree", "add", "--quiet", "-b", branch, str(path)], check=True
            )
            base = subprocess.run(["git", "-C", str(checkout), "rev-parse", "HEAD"], capture_output=True, text=True)
            workspace = Workspace(name, "feat/x", "feat/x", checkout, path, branch, base.stdout.strip(), ("main",))
            assert Workspace.from_record(workspace.to_record()) == workspace
            repos.append(workspace.to_record())
        record = {"id": run_id, "kind": "plan", "dir": str(directory), "repos": repos, "finished_at": old}
        if run_id == 6:
            record.update({"dir": None, "repos": [], "resumed_by": 7, "moved_to": str(directory)})
        home.save_run(record)
    config = WorkerConfig(url="https://hub.example.org", worker_id=1, name="mac", projects=["demo"])
    assert asyncio.run(Daemon(home, config, "evw_x", adapters={}).cleanup()) == [5, 6]
    assert not home.worktree_path("demo", 5).exists()
    assert (home.worktree_path("demo", 6) / "alpha" / "README.md").exists(), "the resumed run's worktrees stay"
    for checkout in checkouts_of.values():
        branches = subprocess.run(
            ["git", "-C", str(checkout), "branch", "--list", "evo-run/*"], capture_output=True, text=True, check=True
        ).stdout
        assert "evo-run/5/" not in branches and "evo-run/6/" in branches


def test_a_plan_runs_worktree_folders_are_valid_branch_names_and_never_collide():
    from evo_agents.worker.gitops import folder_name, plan_branches

    assert folder_name("evo-agents") == "evo-agents"
    assert folder_name("team/web app") == "team-web-app"
    assert folder_name("..hidden") == "hidden" and folder_name("x.lock") == "x.lock-repo" and folder_name("/") == "repo"
    assert folder_name("evo-agents", {"evo-agents", "evo-agents-2"}) == "evo-agents-3"
    body = {"repos": [{"repo": "a", "branch": "main"}, {"repo": "b"}, "c", {"branch": "x"}]}
    assert plan_branches(body) == {"a": "main", "b": None} and plan_branches({}) == {}
