"""Review runs on the worker, without Postgres: the daemon in this process with the fake runtime, the in-memory hub,
and the two checkouts (alpha and beta) of ``tests.worker.test_plan_runs``. The fake agent runs the commands of a review
run's agent, ``evo-agents worker finding`` and ``evo-agents worker propose``, as subprocesses, as a real agent does.

The checks step 3 of the curator-agent plan names on the worker: a review run's worktrees are detached at origin's
default branch; the agent records a finding and a proposal with evidence, code evidence checked against the worktree and
sent with its commit; the run pushes nothing, even what the agent committed, and ends done without verify; ``worker
step`` refuses inside a review run and ``finding`` and ``propose`` outside one; the daemon says it runs review runs;
the worker counts the open items of reports and the learned skills waiting for review in the worktrees."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.worker import figures
from tests.worker.test_plan_runs import (
    ROOT,
    WAIT,
    git,
    machine,  # noqa: F401 (a fixture)
    plan_body,
    run_repos,
    wait_for,
    with_daemon,
)

DRAFT = """
id: curator-wait-helper
goal: Agents wait without a blocked command.
steps:
  - id: 1
    what: A wait helper.
    verify: python -m pytest -q
    acceptance:
      - no blocked sleep in a week of sessions
"""
REPORT = """# Report

## Kết quả

- shipped the queue

## Việc còn mở

- retry the claim on 503
- document the lease
  - a nested note, not an item

## Next

- [ ] wire the Telegram bot
- [x] done already
"""


def seed_report(machine) -> str:  # noqa: F811
    """A report with open items, and a learned skill waiting for review, committed to alpha's origin; the commit."""
    checkout = machine.checkouts["alpha"]
    (checkout / "reports").mkdir()
    (checkout / "reports" / "queue.md").write_text(REPORT, encoding="utf-8")
    pending = checkout / ".claude" / "skills" / "_pending" / "wait-helper"
    pending.mkdir(parents=True)
    (pending / "SKILL.md").write_text("---\nname: wait-helper\n---\nWait on a file.\n", encoding="utf-8")
    (checkout / "tool.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    git("add", "-A", cwd=checkout)
    git("commit", "--quiet", "-m", "a report", cwd=checkout)
    git("push", "--quiet", "origin", "main", cwd=checkout)
    return git("rev-parse", "HEAD", cwd=checkout)


def test_a_review_run_records_a_finding_and_a_proposal_and_pushes_nothing(machine):  # noqa: F811
    head = seed_report(machine)
    draft = machine.tmp / "draft.yaml"
    draft.write_text(DRAFT, encoding="utf-8")
    machine.scenarios(
        {
            "review": [
                {
                    "sh": "echo $EVO_RUN_KIND; git -C alpha symbolic-ref -q HEAD || echo detached; "
                    "git -C alpha rev-parse HEAD"
                },
                {"sh": "cat .evo-run/worktree-figures.json"},
                {"cli": ["step", "1", "in_progress"]},
                {
                    "cli": [
                        "finding",
                        "--lens",
                        "environment",
                        "--title",
                        "the claim retries on 503",
                        "--evidence",
                        "code:alpha:reports/queue.md:9",
                        "--evidence",
                        "run:41:7",
                        "--severity",
                        "high",
                    ]
                },
                {
                    "cli": [
                        "finding",
                        "--lens",
                        "cost",
                        "--title",
                        "a line too far",
                        "--evidence",
                        "code:alpha:tool.py:9",
                    ]
                },
                {"cli": ["finding", "--lens", "cost", "--title", "another repo", "--evidence", "code:gamma:a.py"]},
                {
                    "cli": [
                        "propose",
                        "--lens",
                        "environment",
                        "--kind",
                        "fix",
                        "--title",
                        "A wait helper",
                        "--path",
                        "alpha:tool.py",
                        "--finding",
                        "1",
                        "--plan-file",
                        str(draft),
                    ]
                },
                {"cli": ["propose", "--lens", "cost", "--kind", "fix", "--title", "x", "--plan-file", str(draft)]},
                {"write": {"alpha/tool.py": "changed\n"}},
                {"sh": "git -C alpha commit -qam 'a change the review should never push' && echo committed"},
                {"result": {"summary": "Looked at the environment: one finding, one proposal."}},
            ]
        }
    )

    async def body(hub, daemon):
        run_id = hub.queue_review_run(["alpha", "beta"])
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        assert hub.moves(run_id) == ["leased", "running", "verifying", "done"]
        assert hub.runs[run_id]["summary"] == "Looked at the environment: one finding, one proposal."
        assert hub.last_heartbeat["run_kinds"] == ["step", "plan", "review", "judge", "author"]

        commands = [entry for entry in machine.commands() if entry["run"] == run_id]
        kind, branch, at = commands[0]["stdout"].split()
        assert (kind, branch, at) == ("review", "detached", head)  # at origin's default branch, on no branch
        counted = json.loads(commands[1]["stdout"])
        assert [(item["path"], item["line"], item["text"]) for item in counted["reports"]] == [
            ("reports/queue.md", 9, "retry the claim on 503"),
            ("reports/queue.md", 10, "document the lease"),
            ("reports/queue.md", 15, "wire the Telegram bot"),
        ]
        assert counted["learned_skills"] == [
            {"repo": "alpha", "path": ".claude/skills/_pending/wait-helper/SKILL.md", "name": "wait-helper"}
        ]
        assert any("3 open items of reports, 1 learned skills" in text for text in hub.texts(run_id))
        step = commands[2]
        assert step["exit"] == 1 and "is a review run, which reads only" in step["stderr"]
        recorded = commands[3]
        assert recorded["exit"] == 0 and "Finding #1 recorded (environment, high)" in recorded["stdout"]
        assert commands[4]["exit"] == 1 and "the file has 3 lines" in commands[4]["stderr"]
        assert commands[5]["exit"] == 1 and "not in gamma" in commands[5]["stderr"]
        proposed = commands[6]
        assert proposed["exit"] == 0 and "Proposal #1 recorded: tier 1, open." in proposed["stdout"]
        assert commands[7]["exit"] == 1 and "--finding ID, --evidence SPEC, or both" in commands[7]["stderr"]

        (found,) = hub.findings
        assert found["evidence"] == [
            {"kind": "code", "repo": "alpha", "path": "reports/queue.md", "line": 9, "commit": head},
            {"kind": "run", "run_id": 41, "seq": 7},
        ]
        assert (found["lens"], found["severity"], found["run_id"]) == ("environment", "high", run_id)
        (sent,) = hub.proposals
        assert sent["paths"] == [{"repo": "alpha", "path": "tool.py"}] and sent["finding_ids"] == [1]
        assert sent["plan"]["steps"][0]["acceptance"] == ["no blocked sleep in a week of sessions"]

        # the agent committed in a worktree: nothing reached origin
        assert machine.origin_rev("alpha", "refs/heads/main") == head
        assert machine.origin("alpha", "for-each-ref", "--format=%(refname)", "refs/heads") == "refs/heads/main"

    with_daemon(machine, body, plan=plan_body())


def test_finding_and_propose_refuse_outside_a_review_run(machine):  # noqa: F811
    def agent_cli(*args: str, run: int | None = None) -> tuple[int, str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith("EVO_")}
        env["EVO_WORKER_HOME"] = str(machine.home.root)
        if run is not None:
            env["EVO_RUN_ID"] = str(run)
        done = subprocess.run(
            [sys.executable, "-m", "evo_agents", "worker", *args],
            capture_output=True,
            text=True,
            env={**env, "PYTHONPATH": str(ROOT)},
            timeout=60,
        )
        return done.returncode, done.stderr

    code, err = agent_cli("finding", "--lens", "cost", "--title", "x", "--evidence", "run:1:1")
    assert code == 1 and "works only inside a review run of this worker: EVO_RUN_ID is not set" in err

    machine.scenarios(
        {
            f"plan:{plan_body()['id']}": [
                {"cli": ["finding", "--lens", "cost", "--title", "x", "--evidence", "run:1:1"]},
                {"cli": ["propose", "--lens", "cost", "--kind", "fix", "--title", "x", "--plan-file", "x.yaml"]},
            ]
        }
    )

    async def body(hub, daemon):
        run_id = hub.queue_plan_run(plan_body()["id"], run_repos(plan_body()))
        await wait_for(lambda: len([c for c in machine.commands() if c["run"] == run_id]) == 2, "the commands")
        for entry in [c for c in machine.commands() if c["run"] == run_id]:
            assert entry["exit"] == 1 and "only the agent of a review run uses" in entry["stderr"], entry
        assert hub.findings == [] and hub.proposals == []
        await hub.wait_state(run_id, "done", "failed", timeout=WAIT)

    with_daemon(machine, body, plan=plan_body())


def test_the_open_items_of_a_report_are_its_unchecked_boxes_and_the_items_under_open_headings():
    assert figures.open_items(REPORT) == [
        (9, "retry the claim on 503"),
        (10, "document the lease"),
        (15, "wire the Telegram bot"),
    ]
    fenced = "## TODO\n\n```\n- not an item, code\n```\n- an item\n# Done\n- not open\n"
    assert figures.open_items(fenced) == [(6, "an item")]
