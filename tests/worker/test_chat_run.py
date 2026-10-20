"""Chat runs on the worker, without Postgres: the daemon in this process with the fake runtime, the in-memory hub, and
the two checkouts (alpha and beta) of ``tests.worker.test_plan_runs`` (``evo_agents.worker.runner.chat``).

The checks steps 6 of the plan agent-profiles-group-chat names on the worker (criteria a8 and a9): the daemon says it
runs chat runs; a chat run's agent works in EVO_RUN_KIND chat in a writable worktree of each repo of its project on the
branch chat/<run>; the last message of its turn goes to the hub as its reply in the thread, and a message that came
while it worked starts another turn in the same session; at its end the daemon commits what the agent left and pushes
the branch of each repo it changed, never a default branch, never forced, and tells the hub of each push for the draft
pull request; the next run of the session goes on in the worktrees and the session of the one before; Claude Code
gets the prompt verbatim and no question tool; and the agent's commands in chats (``evo-agents worker chat
post|react|create|mention``) work inside a chat run, a plan run too, and refuse outside one.

Criterion a2 of the chat-runs-start-fast plan: a push origin refuses at the end of a chat run fails the run, with git's
words in its error (the hub says it in the thread), cause push_declined for a hook that declined it (the hub then goes
on in the session at the next message, ``tests.hub.test_chat_runs``) and the usage of its turns, and leaves the commit
on chat/<first run> in the worktree, which the next run of the session goes on in and pushes. What that next run asks
of the origins before its agent starts is in ``tests.worker.test_preflight``.

Criterion a4: the daemon says it does ``runs.LAZY_CHAT_REPOS``; in a chat run that starts without the repo it needs,
the agent runs ``evo-agents worker repo open REPO``, and the daemon asks the fake hub to add it to the run, takes the
leases again, fetches the checkout and makes the worktree on chat/<first run>, notes it in the run's events and record,
and the command prints the path, the same path when the repo is open already. A repo the worker has no checkout of, one
the project does not list, and the command outside a chat run (a task run among them, which still prepares every repo
it names) are refused with one ``error:`` line and exit status 1. At the end only the repo opened is pushed and linked,
and the run that goes on in the session has its worktree alone, fetched, and opens another one in the same
directory. The socket's side, and that a guarded run's agent gets no credential from a repo opened, are in
``tests.worker.test_credentials``."""

from __future__ import annotations

import shlex

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.cli import main as cli_main
from evo_agents.hub import author, chat_runs, runs
from evo_agents.worker import gitops
from tests.worker.test_plan_runs import (
    PLAN,
    PROJECT,
    WAIT,
    Machine,
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


def test_a_push_origin_refuses_fails_the_chat_run_and_the_next_run_of_its_session_pushes_the_commit(machine):  # noqa: F811
    refuse = machine.tmp / "refuse-pushes"
    refuse.write_text("on\n", encoding="utf-8")
    hook = machine.origins["alpha"] / "hooks" / "pre-receive"
    hook.write_text(
        f"#!/bin/sh\nif [ -e {shlex.quote(str(refuse))} ]; then echo 'no write access to alpha' >&2; exit 1; fi\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    machine.scenarios(
        {
            "chat": [
                {"write": {"alpha/a.txt": "one\n"}},
                {"usage": {"total_cost_usd": 0.25}},
                {"say": "Wrote a.txt."},
            ],
            "chat/resume": [{"say": "Pushed a.txt this time."}],
        }
    )
    found = {}

    async def body(hub, daemon):
        first = hub.queue_chat_run(["alpha"])
        assert await hub.wait_state(first, "done", "failed", timeout=WAIT) == "failed", hub.texts(first)
        await wait_for(lambda: not daemon.runs, "the first run to let go of its slot")
        branch = f"chat/{first}"
        worktree = machine.directory(first) / "alpha"
        # the commit stays on the session's branch in the worktree, which origin has not got
        found.update(
            error=hub.runs[first]["error"],
            cause=hub.runs[first].get("failure_cause"),
            usage=hub.runs[first].get("usage"),
            on=git("symbolic-ref", "--short", "HEAD", cwd=worktree),
            subject=git("log", "--format=%s", "-1", cwd=worktree),
            commit=git("rev-parse", "HEAD", cwd=worktree),
            status=git("status", "--porcelain", cwd=worktree),
            pushed=machine.origin_rev("alpha", f"refs/heads/{branch}"),
        )
        refuse.unlink()
        session = machine.home.load_run(first)["session_id"]
        later = hub.queue_chat_run(["alpha"], resume_of=first, session_id=session, branch=branch)
        hub.thread_reply(later, "owner wrote in the thread (message #14):\npush it again, please")
        assert await hub.wait_state(later, "done", "failed", timeout=WAIT) == "done", hub.texts(later)
        found.update(hub=hub, first=first, later=later, branch=branch)

    with_daemon(machine, body)
    hub, first, later, branch = found["hub"], found["first"], found["later"], found["branch"]
    assert hub.moves(first) == ["leased", "running", "verifying", "failed"], hub.texts(first)
    assert chat_posts(hub, first) == ["Wrote a.txt."], "the reply went to the thread before the push"
    assert found["error"].startswith(f"git push of alpha to {branch} failed: "), found["error"]
    assert "no write access to alpha" in found["error"]
    assert found["error"].endswith("(origin declined it: pre-receive hook declined)"), found["error"]
    assert found["cause"] == "push_declined", "the hub goes on in the session at the next message (chat_runs.goes_on)"
    assert (found["usage"] or {}).get("total_cost_usd") == 0.25, "the failed run's turns count in the chat's budget"
    assert found["pushed"] is None and found["status"] == ""
    assert found["on"] == branch and found["subject"].startswith(f"run #{first}: ")
    head = machine.origin_rev("alpha", f"refs/heads/{branch}")
    assert head == found["commit"], "the next run of the session pushed the commit the first one left"
    assert machine.origin("alpha", "log", "--format=%s", branch).splitlines() == [found["subject"], "first commit"]
    assert pulls(hub) == [(later, "alpha", branch, head)]
    assert chat_posts(hub, later) == ["Pushed a.txt this time."]


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


def test_the_agent_of_a_chat_run_reads_a_message_or_its_thread_whole_with_the_read_command(machine):  # noqa: F811
    machine.scenarios(
        {
            "chat": [
                {"cli": ["chat", "read", "plugin.chats:message:12", "--thread"]},
                {"cli": ["chat", "read", "999"]},
                {"cli": ["chat", "read", "docs:doc:runbook"]},
                {"say": "Read it."},
            ]
        }
    )

    async def body(hub, daemon):
        run_id = hub.queue_chat_run(["alpha"])
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        whole, missing, bad = [e for e in machine.commands() if e["run"] == run_id]
        assert whole["exit"] == 0 and whole["stdout"].startswith("Message plugin.chats:message:12 of chat #7, data")
        assert whole["stdout"].endswith("the retry loop spins\n"), "the hub's text, printed as it is"
        assert missing["exit"] == 1 and "error: no chat message 999 that you can read" in missing["stderr"]
        assert bad["exit"] == 1 and "'docs:doc:runbook' names no message" in bad["stderr"]
        assert [c for c in hub.chat_calls if c["route"] == "chat/read"] == [
            {"run_id": run_id, "route": "chat/read", "id": "plugin.chats:message:12", "thread": True},
            {"run_id": run_id, "route": "chat/read", "id": "999", "thread": False},
        ]

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


def test_the_triage_commands_of_a_chat_run_rerun_and_edit_the_plan_inside_the_frame(machine):  # noqa: F811
    machine.scenarios(
        {
            "chat": [
                {"cli": ["triage", "rerun", "--reason", "git is installed now", "--confirm-cause"]},
                {"cli": ["triage", "rerun", "--reason", "try it", "--worker", "nowhere"]},
                {
                    "cli": [
                        "triage",
                        "plan",
                        "2",
                        "--if-revision",
                        "3",
                        "--verify",
                        "make check && make lint",
                        "--reason",
                        "the lint was missing",
                        "--rerun",
                    ]
                },
                {"say": "Done."},
            ],
            "review": [{"cli": ["triage", "rerun", "--reason", "x"]}, {"result": {"summary": "Reviewed."}}],
        }
    )

    async def body(hub, daemon):
        run_id = hub.queue_chat_run(["alpha"])
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        rerun, refused, edited = [e for e in machine.commands() if e["run"] == run_id]
        assert rerun["exit"] == 0 and "Ran the failed run again as run #" in rerun["stdout"]
        assert refused["exit"] == 1 and "the owner has no worker nowhere" in refused["stderr"]
        assert edited["exit"] == 0 and "Wrote revision 4 of the plan and ran the failed run again" in edited["stdout"]
        first, second, third = hub.chat_calls
        assert (first["route"], first["reason"], first["cause_confirmed"]) == (
            "triage/rerun",
            "git is installed now",
            True,
        )
        assert third["route"] == "triage/plan" and third["rerun"] is True and third["if_revision"] == 3
        assert (third["step"], third["verify"]) == ("2", "make check && make lint") and "what" not in third

        review = hub.queue_review_run(["alpha"])
        assert await hub.wait_state(review, "done", "failed", timeout=WAIT) == "done", hub.texts(review)
        (bad,) = [e for e in machine.commands() if e["run"] == review]
        assert bad["exit"] == 1 and "is a review run, whose agent does not talk in chats" in bad["stderr"]
        assert len(hub.chat_calls) == 3

    with_daemon(machine, body)


def opened_repos(hub) -> list[tuple[int, str]]:
    return [(c["run_id"], c["repo"]) for c in hub.chat_calls if c["route"] == "chat/repos"]


def setup_steps(hub, run_id: int) -> list[tuple]:
    """The steps of the setup of run ``run_id`` its notes name, with the repo of each, in order."""
    notes = [event["body"] for event in hub.runs[run_id]["events"] if event["kind"] == "system"]
    return [(note[chat_runs.SETUP_FIELD], note.get("repo")) for note in notes if chat_runs.SETUP_FIELD in note]


def test_a_chat_runs_agent_opens_the_repo_it_needs_and_the_session_goes_on_with_it_alone(machine, tmp_path):  # noqa: F811
    three = Machine(tmp_path / "three", ("alpha", "beta", "gamma"))  # gamma: a checkout the project does not list
    three.scenarios(
        {
            "chat": [
                {"sh": "ls"},
                {"cli": ["repo", "open", "beta"]},
                {"cli": ["repo", "open", "beta"]},
                {"cli": ["repo", "open", "delta"]},
                {"cli": ["repo", "open", "gamma"]},
                {"sh": "ls; git -C beta symbolic-ref --short HEAD"},
                {"write": {"beta/b.txt": "two\n"}},
                {"say": "Wrote b.txt in beta."},
            ],
            "chat/resume": [
                {"sh": "ls; cat beta/b.txt"},
                {"cli": ["repo", "open", "alpha"]},
                {"say": "Read alpha too."},
            ],
        }
    )
    found = {}

    async def body(hub, daemon):
        hub.project_repos = ["alpha", "beta", "delta"]  # delta: a repo of the project this worker has no checkout of
        first = hub.queue_chat_run([])
        hub.runs[first]["spec"]["repos"] = None  # a project without a harness, as the hub hands it (SQL NULL)
        assert await hub.wait_state(first, "done", "failed", timeout=WAIT) == "done", hub.texts(first)
        assert runs.LAZY_CHAT_REPOS in hub.last_heartbeat["features"]
        found.update(
            first_repos=list(hub.runs[first]["spec"]["repos"]),
            first_record=three.home.load_run(first),
            first_asks=hub.credential_calls.count(("ask", first)),
        )
        session = three.home.load_run(first)["session_id"]
        branch = f"chat/{first}"
        names = [entry["repo"] for entry in hub.runs[first]["spec"]["repos"]]
        later = hub.queue_chat_run(names, resume_of=first, session_id=session, branch=branch)
        hub.thread_reply(later, "owner wrote in the thread (message #21):\nand alpha?")
        assert await hub.wait_state(later, "done", "failed", timeout=WAIT) == "done", hub.texts(later)
        found.update(hub=hub, first=first, later=later, branch=branch)

    with_daemon(three, body)
    hub, first, later, branch = found["hub"], found["first"], found["later"], found["branch"]
    directory = three.directory(first)
    empty, beta, again, delta, gamma, looked, *_ = [e for e in three.commands() if e["run"] == first]
    assert empty["stdout"] == "", "the run started with no worktree"
    assert beta["exit"] == 0 and beta["stdout"] == f"{directory / 'beta'}\n", beta
    assert again["exit"] == 0 and again["stdout"] == beta["stdout"], "a repo open already: the same path"
    assert delta["exit"] == 1 and delta["stdout"] == ""
    no_checkout = f"worker mac has no checkout of delta of project {PROJECT}: the run cannot open it here"
    assert delta["stderr"] == f"error: {no_checkout}\n"
    assert gamma["exit"] == 1 and gamma["stdout"] == ""
    assert gamma["stderr"] == f"error: project {PROJECT} has no repo gamma: its repos are alpha, beta, delta\n"
    assert looked["stdout"].splitlines() == ["beta", branch], "the worktree of beta alone, on the session's branch"
    assert opened_repos(hub)[:2] == [(first, "beta"), (first, "gamma")], "delta never reached the hub"
    assert found["first_repos"] == [{"repo": "beta", "branch": branch}]
    assert [item["repo"] for item in found["first_record"]["repos"]] == ["beta"]
    assert found["first_asks"] == 2, "the leases were taken again once the hub added beta"
    texts = hub.texts(first)
    assert f"The agent opened beta: its worktree is {directory / 'beta'}, on {branch}." in texts
    assert any(text.startswith("The agent asked to open delta, which the run did not: worker mac") for text in texts)
    refused = f"The agent asked to open gamma, which the run did not: project {PROJECT} has no repo gamma"
    assert any(text.startswith(refused) for text in texts), texts
    # each step of the run's setup, and each repo the agent asked for, is a note that names it (chat_runs.SETUP_FIELD)
    assert setup_steps(hub, first) == [
        ("claimed", None),
        ("checking", None),
        ("starting", None),
        *[("opening", "beta"), ("opening", "beta"), ("working", "beta")],  # asked, fetched, opened
        *[("opening", "delta"), ("working", "delta"), ("opening", "gamma"), ("working", "gamma")],  # refused
    ]
    assert setup_steps(hub, later) == [
        ("claimed", None),
        ("checking", None),
        ("opening", "beta"),  # the fetch of the worktree it goes on in
        ("starting", None),
        *[("opening", "alpha"), ("opening", "alpha"), ("working", "alpha")],
    ]
    # only beta was pushed, on the session's branch, and linked in the thread
    head = three.origin_rev("beta", f"refs/heads/{branch}")
    assert head is not None and three.origin("beta", "log", "--format=%s", "-1", branch).startswith(f"run #{first}: ")
    assert three.origin_rev("alpha", f"refs/heads/{branch}") is None
    assert three.origin_rev("gamma", f"refs/heads/{branch}") is None
    assert [item for item in pulls(hub) if item[0] == first] == [(first, "beta", branch, head)]
    # the next turn of the session: the worktree of beta alone, fetched, then alpha opened in the same directory
    resumed, alpha = [e for e in three.commands() if e["run"] == later]
    assert resumed["stdout"].splitlines() == ["beta", "two"]
    assert alpha["exit"] == 0 and alpha["stdout"] == f"{directory / 'alpha'}\n", alpha
    later_texts = hub.texts(later)
    assert f"Goes on from run #{first} in {directory}, with its worktrees of beta." in later_texts
    assert any(text == f"Fetching origin in {three.checkouts['beta']}." for text in later_texts)
    assert any("1 left to their fetch" in text for text in later_texts), later_texts
    assert opened_repos(hub)[2:] == [(later, "alpha")]
    assert [item["repo"] for item in three.home.load_run(later)["repos"]] == ["beta", "alpha"]
    assert [item for item in pulls(hub) if item[0] == later] == [], "nothing changed in the next turn"
    assert git("symbolic-ref", "--short", "HEAD", cwd=directory / "alpha") == branch


def test_repo_open_refuses_outside_a_chat_run_and_a_task_run_still_prepares_every_repo(machine, monkeypatch, capsys):  # noqa: F811
    monkeypatch.delenv("EVO_RUN_ID", raising=False)
    assert cli_main(["worker", "repo", "open", "beta"]) == 1
    said = capsys.readouterr()
    assert said.out == "" and "error: `evo-agents worker repo open` works only inside a run of this worker" in said.err
    machine.scenarios({"task": [{"sh": "ls"}, {"cli": ["repo", "open", "beta"]}, {"say": "Looked."}]})

    async def body(hub, daemon):
        run_id = hub.queue_task_run(["alpha", "beta"])
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        listed, refused = [e for e in machine.commands() if e["run"] == run_id]
        assert listed["stdout"].splitlines() == ["alpha", "beta"], "a task run prepares every repo it names"
        assert refused["exit"] == 1 and refused["stdout"] == ""
        assert refused["stderr"].startswith(f"error: run {run_id} is a task run: only the agent of a chat run opens")
        assert opened_repos(hub) == []

    with_daemon(machine, body)
