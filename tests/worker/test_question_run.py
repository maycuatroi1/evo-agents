"""Question runs on the worker, without Postgres: the daemon in this process with the fake runtime, the in-memory hub, and
the two checkouts (alpha and beta) of ``tests.worker.test_plan_runs`` (``evo_agents.worker.runner.question``).

What step 3 of the plan core-plugin-agent-questions names on the worker (criterion a4): the daemon says it runs
question runs; a question run's agent works in EVO_RUN_KIND question, one headless turn, in a worktree of each repo of
its project detached at origin's default branch; nothing is committed or pushed and no pull request is opened; the
answer the agent wrote in .evo-run/result.json goes to the hub with the report of done, cut to its limit, and a run
without one fails no_answer; Claude Code gets the question verbatim, no question tool, and the hub's MCP server."""

from __future__ import annotations

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.hub import author, question_runs
from evo_agents.worker import gitops
from evo_agents.worker.run import KINDS, QuestionRun
from evo_agents.worker.run_kinds import kind_refusal
from evo_agents.worker.runtimes import hub_tools
from tests.worker.test_plan_runs import (
    WAIT,
    git,
    machine,  # noqa: F401 (a fixture)
    with_daemon,
)

PROMPT = "You are @helper. <<<QUESTION\nWhat does @alpha/README.md say?\nQUESTION>>>"


def origin_heads(machine, name: str) -> list[str]:  # noqa: F811
    return machine.origin(name, "for-each-ref", "--format=%(refname)").splitlines()


def test_a_question_run_reads_detached_worktrees_and_sends_its_answer_with_done(machine):  # noqa: F811
    machine.scenarios(
        {
            "question": [
                {
                    "sh": "echo $EVO_RUN_KIND; for r in alpha beta; do git -C $r symbolic-ref -q HEAD || "
                    "git -C $r rev-parse HEAD; done"
                },
                {"write": {"alpha/notes.md": "left by the agent\n"}},
                {"result": {"answer": "  The README says # alpha.  ", "summary": "Read alpha's README."}},
                {"say": "Answered."},
            ]
        }
    )

    async def body(hub, daemon):
        run_id = hub.queue_question_run(["alpha", "beta"], PROMPT)
        hub.inbox[run_id].append({"id": 1, "text": "more", "decision_id": None, "delivered": False})
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        assert hub.moves(run_id) == ["leased", "running", "verifying", "done"]
        assert hub.runs[run_id]["answer"] == "The README says # alpha."
        assert hub.runs[run_id]["summary"] == "Read alpha's README."
        assert "question" in hub.last_heartbeat["run_kinds"]
        (look,) = [entry for entry in machine.commands() if entry["run"] == run_id]
        assert look["stdout"].splitlines() == ["question", machine.seeds["alpha"], machine.seeds["beta"]]
        for name in ("alpha", "beta"):
            assert origin_heads(machine, name) == ["refs/heads/main"], "nothing pushed"
            assert machine.origin_rev(name, "refs/heads/main") == machine.seeds[name]
            worktree = machine.directory(run_id) / name
            assert git("rev-parse", "HEAD", cwd=worktree) == machine.seeds[name], "nothing committed"
        assert git("status", "--porcelain", cwd=machine.directory(run_id) / "alpha") == "?? notes.md"
        assert [c for c in hub.chat_calls if c["run_id"] == run_id] == [], "no pull request, no chat"
        assert hub.runs[run_id].get("diffstat") is None
        assert len(machine.starts()) == 1, "one turn: the inbox starts none"
        (start,) = machine.starts()
        assert start["resume"] is None and start["prompt"] == PROMPT

    with_daemon(machine, body)


def test_a_question_run_cuts_a_long_answer_to_its_limit(machine):  # noqa: F811
    long = "a" * (question_runs.MAX_ANSWER_CHARS + 50)
    machine.scenarios({"question": [{"result": {"answer": long}}, {"say": "Answered."}]})

    async def body(hub, daemon):
        run_id = hub.queue_question_run(["alpha"])
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        assert hub.runs[run_id]["answer"] == "a" * question_runs.MAX_ANSWER_CHARS
        assert any("it is cut to" in text for text in hub.texts(run_id))

    with_daemon(machine, body)


@pytest.mark.parametrize(
    "actions",
    [
        [{"say": "I will not write the file."}],
        [{"result": {"summary": "no answer here"}}, {"say": "Done."}],
        [{"result": {"answer": "   "}}, {"say": "Done."}],
    ],
)
def test_a_question_run_without_an_answer_fails_no_answer(machine, actions):  # noqa: F811
    machine.scenarios({"question": actions})

    async def body(hub, daemon):
        run_id = hub.queue_question_run(["alpha"])
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "failed", hub.texts(run_id)
        assert hub.runs[run_id]["failure_cause"] == "no_answer"
        assert hub.runs[run_id].get("answer") is None
        assert origin_heads(machine, "alpha") == ["refs/heads/main"]

    with_daemon(machine, body)


def test_a_question_run_pushes_nothing(machine):  # noqa: F811
    for branch in ("main", "question/41", "task/41", "feat/x"):
        with pytest.raises(gitops.PushRefused, match="a question run reads and pushes nothing"):
            gitops.check_push(branch, ("main",), kind="question", plan_branch=branch)
    assert KINDS["question"] is QuestionRun
    refusal = kind_refusal(41, "question", ("plan",), "step")
    assert refusal.startswith("run 41 is a question run, which reads only") and "`evo-agents worker step`" in refusal


def test_claude_code_gives_a_question_run_its_question_verbatim_no_question_tool_and_the_hub(tmp_path):
    from tests.worker.test_budget import claude, command_of
    from tests.worker.test_hub_mcp_server import SERVER, isolated
    from tests.worker.test_runtimes import _context, _samples

    question = claude(tmp_path, _samples, kind="question")
    question.options = question.build_options("/opt/bin/claude")
    assert question.options.verbatim_prompts is True
    assert question.options.disallowed_tools == list(author.QUESTION_TOOLS)
    assert "--disallowedTools" in command_of(question)
    context = _context(tmp_path, "claude-code", env=isolated(tmp_path), kind="question")
    assert hub_tools.wanted(context) and hub_tools.claude_servers(context) == {"evo-hub": SERVER}
