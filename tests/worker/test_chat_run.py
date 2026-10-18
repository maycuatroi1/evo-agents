"""Chat runs on the worker, without Postgres: the daemon in this process with the fake runtime, the in-memory hub, and
the two checkouts (alpha and beta) of ``tests.worker.test_plan_runs`` (``evo_agents.worker.runner.chat``).

The checks steps 6 of the plan agent-profiles-group-chat names on the worker (criteria a8 and a9): the daemon says it
runs chat runs; a chat run's agent works in EVO_RUN_KIND chat in a writable worktree of each repo of its project on the
branch chat/<run>; the last message of its turn goes to the hub as its reply in the thread, and a message that came
while it worked starts another turn in the same session; at its end the daemon commits what the agent left and pushes
the branch of each repo it changed, never a default branch, never forced, and tells the hub of each push for the draft
pull request; the next run of the session goes on in the worktrees and the session of the one before; Claude Code
gets the prompt verbatim and no question tool; and the agent's commands in chats (``evo-agents worker chat
post|react|create|mention``) work inside a chat run, a plan run too, and refuse outside one."""

from __future__ import annotations

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.cli import main as cli_main
from evo_agents.hub import author, chat_runs, runs
from evo_agents.worker import gitops
from tests.worker.test_plan_runs import (
    PLAN,
    WAIT,
    git,
    machine,  # noqa: F401 (a fixture)
    plan_body,
    run_repos,
    wait_for,
    with_daemon,
)


def chat_posts(hub, run_id: int) -> list[str]:
    return list(hub.runs[run_id].get("chat") or [])


def pulls(hub) -> list[tuple]:
    return [(c["run_id"], c["repo"], c["branch"], c["head"]) for c in hub.chat_calls if c["route"] == "chat/pulls"]


def test_a_chat_run_answers_in_the_thread_and_pushes_the_branch_of_each_repo_it_changed(machine):  # noqa: F811
    machine.scenarios(
        {
            "chat": [
                {"sh": "echo $EVO_RUN_KIND; for r in alpha beta; do git -C $r symbolic-ref --short HEAD; done"},
                {"write": {"alpha/wait.py": "def wait_for():\n    return True\n"}},
                {"result": {"summary": "Replaced the sleep in alpha."}},
                {"say": "Replaced the sleep with wait_for in alpha; beta needed nothing."},
            ]
        }
    )

    async def body(hub, daemon):
        run_id = hub.queue_chat_run(["alpha", "beta"])
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        assert hub.moves(run_id) == ["leased", "running", "verifying", "done"]
        assert chat_posts(hub, run_id) == ["Replaced the sleep with wait_for in alpha; beta needed nothing."]
        assert hub.runs[run_id]["summary"] == "Replaced the sleep in alpha."
        assert "chat" in hub.last_heartbeat["run_kinds"]
        branch = f"chat/{run_id}"
        (look,) = [entry for entry in machine.commands() if entry["run"] == run_id]
        assert look["stdout"].splitlines() == ["chat", branch, branch]
        head = machine.origin_rev("alpha", f"refs/heads/{branch}")
        assert head is not None and machine.origin("alpha", "log", "--format=%s", "-1", branch).startswith(
            f"run #{run_id}: "
        )
        assert machine.origin_rev("beta", f"refs/heads/{branch}") is None, "a repo it did not change is not pushed"
        assert machine.origin_rev("alpha", "refs/heads/main") == machine.seeds["alpha"], "never a default branch"
        assert pulls(hub) == [(run_id, "alpha", branch, head)]
        assert any("The hub linked" in text for text in hub.texts(run_id))
        # the worktrees stay on the branch, for the next run of the session
        assert git("symbolic-ref", "--short", "HEAD", cwd=machine.directory(run_id) / "alpha") == branch

    with_daemon(machine, body)


def test_a_chat_run_takes_a_message_of_the_thread_in_a_second_turn_of_its_session(machine):  # noqa: F811
    go_on = machine.tmp / "go-on"
    machine.scenarios(
        {
            "chat": [{"no_input": True}, {"wait_for": str(go_on)}, {"say": "First answer."}],
            "chat/2": [{"say": "Second answer, with the retries too."}],
        }
    )

    async def body(hub, daemon):
        run_id = hub.queue_chat_run(["alpha"])
        await hub.wait_state(run_id, "running", timeout=WAIT)
        hub.thread_reply(run_id, "owner wrote in the thread (message #9):\nalso the retries")
        await wait_for(lambda: hub.last_heartbeat and run_id in hub.last_heartbeat["runs"], "a heartbeat of the run")
        go_on.write_text("go", encoding="utf-8")
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        assert chat_posts(hub, run_id) == ["First answer.", "Second answer, with the retries too."]
        first, second = machine.starts()
        assert second["resume"] == first["session"]
        assert second["prompt"].startswith(chat_runs.REPLY_PROMPT) and second["prompt"].endswith("also the retries")
        assert pulls(hub) == [], "nothing changed: nothing pushed"

    with_daemon(machine, body)


def test_the_next_chat_run_goes_on_in_the_worktrees_and_session_of_the_run_before(machine):  # noqa: F811
    machine.scenarios(
        {
            "chat": [{"write": {"alpha/a.txt": "one\n"}}, {"say": "Wrote a.txt."}],
            "chat/resume": [
                {"sh": "pwd -P; echo $EVO_RUN_ID; cat alpha/a.txt"},
                {"write": {"alpha/b.txt": "two\n"}},
                {"say": "Wrote b.txt too."},
            ],
        }
    )
    found = {}

    async def body(hub, daemon):
        first = hub.queue_chat_run(["alpha"])
        assert await hub.wait_state(first, "done", "failed", timeout=WAIT) == "done", hub.texts(first)
        session = machine.home.load_run(first)["session_id"]
        branch = f"chat/{first}"
        later = hub.queue_chat_run(["alpha"], resume_of=first, session_id=session, branch=branch)
        hub.thread_reply(later, "owner wrote in the thread (message #12):\nand b.txt, please")
        assert await hub.wait_state(later, "done", "failed", timeout=WAIT) == "done", hub.texts(later)
        found.update(first=first, later=later, session=session, branch=branch, hub=hub)

    with_daemon(machine, body)
    first, later, branch, hub = found["first"], found["later"], found["branch"], found["hub"]
    directory = machine.directory(first)
    one, two = machine.starts()
    assert two["resume"] == found["session"] and two["run"] == later
    assert two["prompt"].startswith(chat_runs.RESUME_PROMPT.format(id=later))
    assert two["prompt"].endswith("and b.txt, please")
    looked = [entry for entry in machine.commands() if entry["run"] == later][0]["stdout"].splitlines()
    assert looked == [str(directory.resolve()), str(later), "one"]
    assert machine.origin("alpha", "log", "--format=%s", branch).splitlines() == [
        f"run #{later}: {hub.runs[later]['spec']['title']}",
        f"run #{first}: {hub.runs[first]['spec']['title']}",
        "first commit",
    ]
    assert [item[0] for item in pulls(hub)] == [first, later]
    old = machine.home.load_run(first)
    assert old["resumed_by"] == later and old["dir"] is None, "the cleanup of the first run leaves them alone"
    assert any(f"Goes on from run #{first} in {directory}" in text for text in hub.texts(later))


def test_a_chat_run_pushes_only_its_chat_branch(machine):  # noqa: F811
    assert gitops.check_push("chat/41", ("main",), kind="chat", plan_branch=None) is False
    for branch, why in (
        ("main", "a default branch"),
        ("trunk", "a default branch"),
        ("feat/x", "not a branch of a chat"),
    ):
        with pytest.raises(gitops.PushRefused, match=why):
            gitops.check_push(branch, ("trunk",), kind="chat", plan_branch=branch)
    assert chat_runs.is_chat_branch("chat/41") and not chat_runs.is_chat_branch("chat/041")


def test_claude_code_gives_a_chat_run_its_prompt_verbatim_and_no_question_tool(tmp_path):
    from tests.worker.test_budget import claude, command_of
    from tests.worker.test_runtimes import _samples

    chat = claude(tmp_path, _samples, kind="chat")
    chat.options = chat.build_options("/opt/bin/claude")
    assert chat.options.verbatim_prompts is True
    assert chat.options.disallowed_tools == list(author.QUESTION_TOOLS)
    assert "--disallowedTools" in command_of(chat)
    for kind in ("step", "plan", "author"):
        other = claude(tmp_path, _samples, kind=kind)
        other.options = other.build_options("/opt/bin/claude")
        assert other.options.verbatim_prompts is False


def test_the_chat_commands_of_an_agent_in_a_chat_run_and_a_plan_run(machine):  # noqa: F811
    machine.scenarios(
        {
            "chat": [
                {"cli": ["chat", "post", "Working on it."]},
                {"cli": ["chat", "react", "12", "\U0001f440"]},
                {"cli": ["chat", "mention", "@fixer", "fix the retry loop"]},
                {"cli": ["chat", "mention", "reviewer", "me again"]},
                {"cli": ["chat", "create", "--title", "Retry design", "--person", "bob"]},
                {"cli": ["chat", "react", "12", "ok"]},
                {"say": "Done."},
            ],
            f"plan:{PLAN}": [
                {"cli": ["chat", "post", "Plan run says hi.", "--chat", "7", "--reply-to", "3"]},
                {"result": {"summary": "Said hi."}},
            ],
        }
    )

    async def body(hub, daemon):
        run_id = hub.queue_chat_run(["alpha"])
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        posted, reacted, handed, itself, made, bad = [e for e in machine.commands() if e["run"] == run_id]
        assert posted["exit"] == 0 and posted["stdout"].startswith("Posted message #1001 in chat 7")
        assert reacted["exit"] == 0 and reacted["stdout"] == "Put \U0001f440 on message #12.\n"
        assert (
            handed["exit"] == 0 and "Handed the work to @fixer in message #1003: its run is #503." in handed["stdout"]
        )
        assert itself["exit"] == 1 and "@reviewer took no work: an agent does not mention itself" in itself["stderr"]
        assert made["exit"] == 0 and "Created chat #75 'Retry design' with owner, bob and @reviewer" in made["stdout"]
        assert bad["exit"] == 1 and "not an emoji" in bad["stderr"]
        routes = ["chat/messages", "chat/reactions", "chat/mentions", "chat/mentions", "chats"]
        assert [c["route"] for c in hub.chat_calls] == routes
        assert hub.chat_calls[0]["body"] == "Working on it."
        assert [c["handle"] for c in hub.chat_calls[2:4]] == ["fixer", "reviewer"]
        assert hub.chat_calls[1] == {
            "run_id": run_id,
            "route": "chat/reactions",
            "message_id": 12,
            "emoji": "\U0001f440",
            "remove": False,
        }
        assert hub.chat_calls[4]["people"] == ["bob"] and hub.chat_calls[2]["body"] == "fix the retry loop"

        plan_id = hub.queue_plan_run(PLAN, run_repos(plan_body()))
        assert await hub.wait_state(plan_id, "done", "failed", timeout=WAIT) == "done", hub.texts(plan_id)
        (said,) = [e for e in machine.commands() if e["run"] == plan_id]
        assert said["exit"] == 0, said
        assert hub.chat_calls[-1] == {
            "run_id": plan_id,
            "route": "chat/messages",
            "body": "Plan run says hi.",
            "chat_id": 7,
            "reply_to": 3,
        }

    with_daemon(machine, body)


def test_the_chat_commands_refuse_outside_a_run_and_in_a_review_run(machine, monkeypatch, capsys):  # noqa: F811
    monkeypatch.delenv("EVO_RUN_ID", raising=False)
    assert cli_main(["worker", "chat", "post", "hello"]) == 1
    assert "works only inside a run of this worker: EVO_RUN_ID is not set" in capsys.readouterr().err
    machine.scenarios({"review": [{"cli": ["chat", "post", "hello"]}, {"result": {"summary": "Reviewed."}}]})

    async def body(hub, daemon):
        run_id = hub.queue_review_run(["alpha"])
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        (refused,) = [e for e in machine.commands() if e["run"] == run_id]
        assert refused["exit"] == 1 and "is a review run, whose agent does not talk in chats" in refused["stderr"]
        assert hub.chat_calls == []

    with_daemon(machine, body)
    assert runs.SESSION_KINDS == ("plan", "author", "chat")


def test_a_chat_runs_agent_asks_a_triage_decision_waits_for_the_answer_and_goes_on_in_its_session(machine):  # noqa: F811
    ask = {
        "cli": [
            "ask",
            "--category",
            runs.CHAT_DECISION_CATEGORY,
            "--question",
            "Retry the flaky test or skip it?",
            "--option",
            "retry=Retry",
            "--option",
            "skip=Skip",
            "--recommended",
            "retry",
        ]
    }
    machine.scenarios({"chat": [ask, {"say": "I asked you in decision #1."}], "chat/2": [{"say": "Retrying it."}]})

    async def body(hub, daemon):
        assert runs.CHAT_DECISIONS in runs.WORKER_FEATURES
        run_id = hub.queue_chat_run(["alpha"])
        assert await hub.wait_state(run_id, "waiting", "done", "failed", timeout=WAIT) == "waiting", hub.texts(run_id)
        assert runs.CHAT_DECISIONS in hub.last_heartbeat["features"]
        assert (hub.decisions[1]["run_id"], hub.decisions[1]["category"]) == (run_id, "triage")
        assert hub.answer(1, "retry") == run_id
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        assert chat_posts(hub, run_id) == ["I asked you in decision #1.", "Retrying it."]
        assert hub.moves(run_id) == ["leased", "running", "waiting", "running", "verifying", "done"]

    with_daemon(machine, body)
    first, second = machine.starts()
    assert second["resume"] == first["session"], "the answer goes on in the agent's session"
    assert "Chosen option: retry." in second["prompt"]
