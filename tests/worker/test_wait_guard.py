"""The hook of waits of a plan run (``evo_agents.worker.wait_guard``): the waits run #1537 of evo-agents made in its
session (Monitor watches of a tag's CI, PyPI, builds of the knowledge graph and kg_init runs, loops that slept, a
``sleep 590``) are refused with words that name ``evo-agents worker wait``, the commands of work go through, only a
plan run gets the hook and the note beside the system prompt, and its prompt says how to wait for something elsewhere.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest

from evo_agents.harness import load_yaml
from evo_agents.hub import run_prompts
from evo_agents.worker import wait_guard
from evo_agents.worker.wait_guard import WaitGuard, waiting

WT = "/home/evo/.evo/worker-evo-agents/worktrees/evo-agents-1520/evo-agents"

# Commands of run #1537, shortened where their middle says nothing more.
REFUSED_1537 = [
    f"cd {WT}; sleep 590; for p in evo-agents taiwu; do evo-agents hub kg init --project $p --status --json; done",
    f"cd {WT}; until evo-agents hub kg builds --project evo-agents 2>/dev/null | sed -n 2p | grep -qv running; "
    "do sleep 60; done; evo-agents hub kg semantic --project evo-agents --json",
    f"cd {WT}; for i in $(seq 1 40); do out=$(evo-agents hub kg semantic --project evo-agents --json 2>/dev/null) "
    '&& { echo "$out"; break; }; sleep 45; done',
    f"cd {WT}; prev=; for i in $(seq 1 55); do cur=$(evo-agents hub kg init --project evo-agents --status --json "
    '| jq -c \'[.runs[]|{id:.id,state:.state}]\'); [ "$cur" != "$prev" ] && echo "$cur"; prev=$cur; '
    'echo "$cur" | grep -qE \'"(done|failed)"\' && break; sleep 30; done',
    "R=$(gh run list --branch v0.55.4 --workflow release -L1 --json databaseId -q '.[0].databaseId'); "
    'for i in $(seq 1 58); do v=$(gh run view $R --json status -q .status); [ "$v" = completed ] && break; '
    "sleep 30; done",
]
REFUSED = [
    *REFUSED_1537,
    "sleep 2m",
    "sleep 1m 90s && gh pr checks 12 --required",
    "gh run watch 123 --exit-status",
    "gh pr checks 12 --watch --required",
    "evo-agents hub run logs 1538 --project evo-agents --follow",
    "evo-agents hub run logs 1538 -f",
    "watch -n 30 evo-agents hub run show 1538",
    "timeout 3600 bash -c 'until curl -fsS https://pypi.org/pypi/evo-ak/0.55.4/json; do sleep 60; done'",
    "nohup sh -c 'while true; do gh pr checks 5; sleep 20; done' &",
]
ALLOWED = [
    "sleep 20; evo-agents worker plan > /tmp/plan.txt 2>&1; head -c 300 /tmp/plan.txt",
    "until curl -fsS localhost:8000/health; do sleep 1; done",
    "until grep -qE 'passed|failed' /tmp/tasks/b6senj8uv.output; do sleep 3; done; tail -15 /tmp/tasks/b6senj8uv.output",
    "uv run pytest -q tests/kg/test_profile.py -n 4",
    "for f in evo_agents/kg/*.py; do ruff check $f; done",
    "gh pr checks 12 --required",
    "evo-agents hub run logs 1538 --project evo-agents",
    'git commit -m "sleep 600 in the old loop"',
    "evo-agents worker wait --reason 'the CI of v0.55.4' --until +20m --step 4",
]


@pytest.mark.parametrize("command", REFUSED)
def test_a_wait_in_the_session_is_refused(command):
    assert waiting(command) is not None, command


@pytest.mark.parametrize("command", ALLOWED)
def test_work_and_short_pauses_go_through(command):
    assert waiting(command) is None, command


def test_the_refusal_names_worker_wait_and_is_recorded():
    recorded = []
    guard = WaitGuard(record=lambda text, body: recorded.append((text, body)), run_id=1537)
    why = guard.check("Bash", {"command": REFUSED_1537[0], "run_in_background": True})
    assert why is not None and "evo-agents worker wait" in why and "sleep 590" in why
    monitor = guard.check("Monitor", {"command": "until false; do sleep 60; done", "timeout_ms": 2400000})
    assert monitor is not None and "Monitor" in monitor
    assert [body["tool"] for _, body in recorded] == ["Bash", "Monitor"]
    assert all(body["verdict"] == "refused" and body["run"] == 1537 for _, body in recorded)
    assert guard.check("Read", {"file_path": "README.md"}) is None
    assert guard.check("Bash", {"command": "pytest -q tests/kg/test_profile.py"}) is None
    assert recorded[-1][1]["tool"] == "Monitor", "a call that goes through records nothing"


def test_only_a_plan_run_gets_the_hook():
    class Run:
        id = 1537
        notes: list = []

        def __init__(self, kind):
            self.spec = {"kind": kind}

        def note(self, text, **extra):
            self.notes.append((text, extra))

    for kind in ("chat", "author", "step", "release", "task"):
        assert wait_guard.for_run(Run(kind)) is None, kind
    run = Run("plan")
    guard = wait_guard.for_run(run)
    assert guard is not None and guard.check("Bash", {"command": "sleep 300"})
    assert run.notes[-1][1]["wait"]["what"] == "sleep 300"


def test_the_claude_hook_denies_with_the_reason():
    hook = wait_guard.claude_wait_hook(WaitGuard())
    denied = asyncio.run(hook({"tool_name": "Monitor", "tool_input": {"command": "sleep 900"}}, "toolu_1", {}))
    output = denied["hookSpecificOutput"]
    assert output["permissionDecision"] == "deny" and "evo-agents worker wait" in output["permissionDecisionReason"]
    assert asyncio.run(hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "toolu_2", {})) == {}
    assert wait_guard.claude_wait_hook(None) is None


def test_a_plan_runs_session_gets_the_hook_and_the_note(tmp_path):
    from evo_agents.worker.runtimes import claude_code
    from tests.worker.test_runtimes import _claude, _samples

    adapter = _claude(tmp_path, _samples, kind="plan")
    adapter.context = dataclasses.replace(adapter.context, waits=WaitGuard())
    adapter._session = "11111111-2222-3333-4444-555555555555"
    options = adapter.build_options("/opt/bin/claude")
    hooks = options.hooks["PreToolUse"][0].hooks
    assert len(hooks) == 1 and hooks[0].__name__ == "wait_guard"
    assert claude_code.PLAN_WAIT_NOTE in options.system_prompt["append"]

    chat = _claude(tmp_path, _samples, kind="chat")
    chat._session = adapter._session
    options = chat.build_options("/opt/bin/claude")
    assert options.hooks is None
    assert claude_code.PLAN_WAIT_NOTE not in options.system_prompt["append"]


def test_the_plan_runs_prompt_says_how_to_wait_for_something_elsewhere():
    from tests.hub.test_runs_model import FIXTURE

    plan = load_yaml(FIXTURE)
    prompt = run_prompts.build_plan_prompt(plan, plan["repos"])
    assert "A GitHub Actions run (the CI of a tag or of a commit pushed, a deploy)" in prompt
    assert "--on github.workflow_run --contains repo=OWNER/NAME --contains run_id=ID --until +90m --step KEY" in prompt
    assert "--until +15m --step KEY" in prompt and "no Monitor, no loop that sleeps" in prompt
