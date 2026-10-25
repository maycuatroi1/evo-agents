"""A probe run on the worker (``evo_agents.worker.runner.probe``), without Postgres: the daemon in this process with
the fake runtime, the in-memory hub, and the checkouts of ``tests.worker.test_plan_runs``.

The checks step 2 of the correction-recurrence plan names on the worker: k sessions of each arm, each in a new
repository at the probe's base commit; arm on with the harness files of the default branch's tip and arm off with that
tip less the change's diff (on claude-code the repo's .claude/settings.json and .claude/hooks/ too, under .claude/ of
the session's directory); the agent given the task alone; a trace of the commands the agent ran and the files it
changed written after its turn, read-only, and named to the verify, which runs as code the worker does not trust; each
session posted with the run's key, the runtime and the model it ran on; 1/2k of the run's caps for each session; no
event that carries the verify; nothing pushed; and a change whose diff no longer comes off the tip stale, without an
agent."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from tests.worker.test_plan_runs import (
    git,
    machine,  # noqa: F401 (a fixture)
    with_daemon,
)

BRANCH = "curator/1-rule"
TASK = "Leave done.txt in the repository when the work is over."
MARKER = "probe-answer-key"
BUDGET = {"max_usd": 1.0, "max_turns": 30, "max_seconds": 1200, "spent_usd": 0.0, "spent_seconds": 0.0}
RULE = "Always write done.txt.\n"


def commit(checkout, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        target = checkout / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git("add", "-A", cwd=checkout)
    git("commit", "--quiet", "-m", message, cwd=checkout)
    return git("rev-parse", "HEAD", cwd=checkout)


def history(machine, *, rewrite: bool = False) -> dict:  # noqa: F811
    """On alpha's main: the base (AGENTS.md v1, the repo's settings), the change merged (a rule in AGENTS.md, a hook
    and the settings that run it), then a later commit (another file; with ``rewrite``, AGENTS.md rewritten where the
    rule was)."""
    alpha = machine.checkouts["alpha"]
    found = {}
    base_files = {"AGENTS.md": "# Rules\nrules v1\n", ".claude/settings.json": '{"hooks": {}}\n'}
    found["base"] = commit(alpha, base_files, "the harness at v1")
    git("checkout", "--quiet", "-b", BRANCH, cwd=alpha)
    rule = {"AGENTS.md": f"# Rules\nrules v1\n{RULE}", ".claude/hooks/guard.sh": "#!/bin/sh\nexit 0\n"}
    found["head"] = commit(alpha, {**rule, ".claude/settings.json": '{"hooks": {"guard": 1}}\n'}, "the change")
    git("checkout", "--quiet", "main", cwd=alpha)
    git("merge", "--quiet", "--no-ff", "-m", "Merge the change", BRANCH, cwd=alpha)
    found["merge"] = git("rev-parse", "HEAD", cwd=alpha)
    later = {"AGENTS.md": "# Rules\nrules v2, all new\n"} if rewrite else {"later.txt": "later\n"}
    found["tip"] = commit(alpha, later, "after the change")
    git("push", "--quiet", "origin", "main", BRANCH, cwd=alpha)
    return found


def curator(found: dict, *, k: int = 2) -> dict:
    verify = (
        f'cp "$EVO_PROBE_TRACE" {{out}}/ && test ! -w "$EVO_PROBE_TRACE" && env > {{out}}/env-$(basename '
        f'"$EVO_PROBE_TRACE") && test -f done.txt # {MARKER}'
    )
    probe = {
        "probe_run_id": 9,
        "probe_id": 5,
        "repo": "alpha",
        "base_sha": found["base"],
        "made_at": "2026-10-20T00:00:00+00:00",
        "task": TASK,
        "verify": verify,
        "k": k,
        "week": "2026-10-19",
        "change_repo": "alpha",
        "merge_sha": found["merge"],
    }
    return {
        "change_id": 1,
        "branch": BRANCH,
        "forge": "github",
        "base_branch": "main",
        "head_sha": found["head"],
        "protected_paths": [],
        "probe": probe,
    }


def look(machine, name: str) -> dict:  # noqa: F811
    """An action that writes what the session's agent sees to files of the test, then a Bash command that writes
    done.txt only when the rule is in AGENTS.md."""
    out = machine.tmp / f"seen-{name}"
    return {
        "sh": (
            f"mkdir -p {out}; cat alpha/AGENTS.md > {out}/agents; git -C alpha log --format=%H > {out}/log; "
            f"cat .claude/settings.json > {out}/settings; ls .claude/hooks > {out}/hooks 2>/dev/null; pwd > {out}/cwd"
        )
    }


ACT = {"bash": "grep -q 'Always write done.txt' alpha/AGENTS.md && echo done > alpha/done.txt; true"}


def seen(machine, name: str, what: str) -> str:  # noqa: F811
    path = machine.tmp / f"seen-{name}" / what
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_probe_run_runs_k_sessions_of_each_arm_and_posts_each_with_its_key(machine):  # noqa: F811
    found_commits = history(machine)
    out = machine.tmp / "traces"
    out.mkdir()
    names = ["on-1", "off-1", "on-2", "off-2"]
    keys = ["probe", "probe/2", "probe/3", "probe/4"]
    machine.scenarios(
        {
            key: [{"model": "claude-opus-test"}, look(machine, name), ACT, {"say": "Done."}]
            for key, name in zip(keys, names, strict=True)
        }
    )
    spec = curator(found_commits)
    spec["probe"]["verify"] = spec["probe"]["verify"].format(out=out)
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_probe_run([{"repo": "alpha", "branch": None}], spec, budget=BUDGET)
        assert await hub.wait_state(run_id, "done", "failed", timeout=90) == "done", hub.texts(run_id)
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    run_id, hub = found["run"], found["hub"]
    assert hub.moves(run_id) == ["leased", "running", "verifying", "done"]
    sent = hub.probes
    assert [(item["session"]["arm"], item["session"]["index"]) for item in sent] == [
        ("on", 1),
        ("off", 1),
        ("on", 2),
        ("off", 2),
    ]
    assert all(
        item["base_sha"] == found_commits["base"] and item["harness_sha"] == found_commits["tip"] for item in sent
    )
    assert [item["session"]["passed"] for item in sent] == [True, False, True, False]
    for item in sent:
        session = item["session"]
        assert session["runtime"] == "claude-code" and session["model"] == "claude-opus-test"
        assert session["duration_ms"] >= 0 and session["session_id"] and session["agent_error"] is None
    # arm on has the rule, the hook and the settings of the tip; arm off that tip less the change's diff
    for name in names:
        rule_in = RULE in seen(machine, name, "agents")
        assert rule_in == name.startswith("on"), (name, seen(machine, name, "agents"))
        assert ("guard.sh" in seen(machine, name, "hooks")) == name.startswith("on")
        assert ("guard" in seen(machine, name, "settings")) == name.startswith("on")
        log = seen(machine, name, "log").split()
        assert found_commits["base"] in log and found_commits["merge"] not in log and found_commits["tip"] not in log
        assert seen(machine, name, "cwd").strip().endswith(f"/{name}")
    # the trace: the commands the agent ran and the files it changed, read-only, outside the session's directory
    trace = json.loads((out / "on-1.json").read_text(encoding="utf-8"))
    assert trace["commands"] == [ACT["bash"]]
    assert "done.txt" in trace["files"]
    assert json.loads((out / "off-1.json").read_text(encoding="utf-8"))["files"] == []
    env = (out / "env-on-1.json").read_text(encoding="utf-8")
    assert "EVO_RUN_ID" not in env and "EVO_WORKER_HOME" not in env  # the verify ran as untrusted code
    # 1/2k of the run's caps for each session, the task alone in its prompt
    starts = [item for item in machine.starts() if item["run"] == run_id]
    assert [start["cwd"].rsplit("/", 1)[-1] for start in starts] == names
    assert [start["budget"]["max_usd"] for start in starts] == [0.25] * 4
    assert starts[0]["budget"]["max_seconds"] == 300
    for start in starts:
        assert TASK in start["prompt"] and MARKER not in start["prompt"]
    # no event carries the verify, the probe's answer key
    assert MARKER not in json.dumps(hub.runs[run_id]["events"])
    assert machine.origin_rev("alpha", "refs/heads/main") == found_commits["tip"]  # nothing pushed
    assert hub.judge_refused == []


def test_probe_run_of_a_change_whose_diff_no_longer_comes_off_is_stale_without_an_agent(machine):  # noqa: F811
    found_commits = history(machine, rewrite=True)
    machine.scenarios({"probe": [{"say": "never"}]})
    spec = curator(found_commits)
    spec["probe"]["verify"] = spec["probe"]["verify"].format(out=machine.tmp)
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_probe_run([{"repo": "alpha", "branch": None}], spec, budget=BUDGET)
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "done", hub.texts(run_id)
        found.update(hub=hub, run=run_id)

    with_daemon(machine, body)
    hub, run_id = found["hub"], found["run"]
    (sent,) = hub.probes
    assert set(sent) == {"run_id", "stale"}
    assert "the change's diff no longer comes off main" in sent["stale"]
    starts = machine.starts() if machine.starts_path.exists() else []
    assert [item for item in starts if item["run"] == run_id] == []
    assert hub.runs[run_id]["summary"].startswith("Nothing probed: the probe is stale this week")


def test_probe_runs_agent_command_of_a_plan_says_what_a_probe_run_does_instead(machine):  # noqa: F811
    found_commits = history(machine)
    machine.scenarios({"probe": [{"cli": ["step", "3", "in_progress"]}]})
    spec = curator(found_commits, k=1)
    spec["probe"]["verify"] = spec["probe"]["verify"].format(out=machine.tmp)
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_probe_run([{"repo": "alpha", "branch": None}], spec, budget=BUDGET)
        assert await hub.wait_state(run_id, "done", "failed", timeout=60) == "done", hub.texts(run_id)
        found.update(run=run_id)

    with_daemon(machine, body)
    commands = [item for item in machine.commands() if item["run"] == found["run"]]
    assert len(commands) == 2  # a session of each arm
    assert all(item["exit"] != 0 and "is a probe run, which does one task" in item["stderr"] for item in commands)


def test_probe_run_of_a_plugins_rule_places_the_files_of_each_arm_at_the_base_commit(machine):  # noqa: F811
    """A core plugin's probe (core plugin API 1.4) has no change: both arms start at the base commit, arm on with the
    files the plugin rendered with its rule and arm off with those rendered without it, a path an arm lacks absent."""
    found_commits = history(machine)
    names = ["on-1", "off-1"]
    machine.scenarios(
        {
            key: [look(machine, name), ACT, {"say": "Done."}]
            for key, name in zip(["probe", "probe/2"], names, strict=True)
        }
    )
    spec = curator(found_commits, k=1)
    for key in ("change_id", "branch", "head_sha"):
        spec.pop(key)
    hook = {".claude/hooks/rule.sh": "#!/bin/sh\nexit 0\n"}
    spec["probe"].update(
        verify=f"test -f done.txt # {MARKER}",
        merge_sha=None,
        plugin="northwind-editorial",
        ref="post.voice",
        arms={"on": {"AGENTS.md": f"# Rules\n{RULE}", **hook}, "off": {"AGENTS.md": "# Rules\n"}},
    )
    found = {}

    async def body(hub, daemon):
        run_id = hub.queue_probe_run([{"repo": "alpha", "branch": None}], spec, budget=BUDGET)
        assert await hub.wait_state(run_id, "done", "failed", timeout=90) == "done", hub.texts(run_id)
        found.update(run=run_id, hub=hub)

    with_daemon(machine, body)
    run_id, hub = found["run"], found["hub"]
    sent = hub.probes
    assert [(item["session"]["arm"], item["session"]["passed"]) for item in sent] == [("on", True), ("off", False)]
    assert all(item["base_sha"] == item["harness_sha"] == found_commits["base"] for item in sent)
    assert seen(machine, "on-1", "agents") == f"# Rules\n{RULE}"
    assert seen(machine, "off-1", "agents") == "# Rules\n"
    assert "rule.sh" in seen(machine, "on-1", "hooks") and "rule.sh" not in seen(machine, "off-1", "hooks")
    assert "guard" not in seen(machine, "on-1", "hooks")  # the change's hook is no part of a plugin's probe
    for name in names:
        log = seen(machine, name, "log").split()
        assert found_commits["base"] in log and found_commits["merge"] not in log
    assert any("probe #5 of plugin northwind-editorial rule post.voice" in text for text in hub.texts(run_id))
    assert MARKER not in json.dumps(hub.runs[run_id]["events"])
    starts = [item for item in machine.starts() if item["run"] == run_id]
    assert all(MARKER not in start["prompt"] for start in starts)
