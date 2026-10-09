"""Author runs on the worker, without Postgres: the daemon in this process with the fake runtime, the in-memory hub,
and the two checkouts (alpha and beta) of ``tests.worker.test_plan_runs``, alpha standing for the project's harness.

The checks step 1 of the hub-plan-authoring plan names on the worker: an author run's worktrees are detached at
origin's default branch, of the harness and of each other repo the worker has a checkout of; the run commits and
pushes nothing, even what the agent committed; the agent has create-exec-plan as the claim names it, written under
.claude/skills of its directory, without any sync of skills on the machine; a claim without that skill, or a bundle
other than the one the hub recorded, fails the run before the agent starts; the agent runs in EVO_RUN_KIND author; the
daemon says it runs author runs; Claude Code's question tools are off for an author run; the commands of a plan run's
and of a review run's agent refuse inside one.

And those step 3 names: the last message of each turn of the agent goes to the run's chat, and the run then waits for
its owner's reply, which starts the next turn in the same session; a run its owner ended the chat of ends done once the
turn is over, without waiting; a parked author run keeps its directory, worktrees and skills, and the reply resumes it
in them and in its session."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.hub import author, skills
from tests.worker.test_plan_runs import (
    WAIT,
    git,
    machine,  # noqa: F401 (a fixture)
    wait_for,
    with_daemon,
)

SKILL_TEXT = (
    "---\nname: create-exec-plan\ndescription: Use when writing an execution plan\n---\nAuthor mode: ask in chat.\n"
)


def skill_bundle(tmp_path: Path, text: str = SKILL_TEXT) -> bytes:
    directory = tmp_path / "bundle" / author.AUTHOR_SKILL
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(text, encoding="utf-8")
    (directory / "references").mkdir()
    (directory / "references" / "author.md").write_text("Questions go in the chat.\n", encoding="utf-8")
    return skills.pack(directory).data


async def ended_by_owner(hub, run_id: int) -> str:
    """Wait until the author run waits for its owner's reply, end its chat as its owner, and wait for its end."""
    assert await hub.wait_state(run_id, "waiting", "done", "failed", timeout=WAIT) == "waiting", hub.texts(run_id)
    hub.finish(run_id)
    return await hub.wait_state(run_id, "done", "failed", timeout=WAIT)


def test_an_author_run_reads_detached_worktrees_with_the_hubs_skill_and_pushes_nothing(machine):  # noqa: F811
    alpha_head = machine.seeds["alpha"]
    machine.scenarios(
        {
            "author": [
                {
                    "sh": "echo $EVO_RUN_KIND; git -C alpha symbolic-ref -q HEAD || echo detached; "
                    "git -C alpha rev-parse HEAD; ls; cat .claude/skills/create-exec-plan/SKILL.md"
                },
                {"cli": ["step", "1", "in_progress"]},
                {"cli": ["finding", "--lens", "cost", "--title", "x", "--evidence", "run:1:1"]},
                {"write": {"alpha/plans/new.yaml": "id: new\n", ".evo-run/authored-plan.yaml": "id: wait-helper\n"}},
                {"sh": "git -C alpha add -A && git -C alpha commit -qm 'a plan the run must never push' && echo ok"},
                {"result": {"summary": "Wrote plan wait-helper."}},
            ]
        }
    )

    async def body(hub, daemon):
        ticket = hub.add_skill(author.AUTHOR_SKILL, 3, skill_bundle(machine.tmp))
        run_id = hub.queue_author_run(["alpha", "beta", "gamma"], [ticket], prompt="Write the plan of a wait helper.")
        assert await ended_by_owner(hub, run_id) == "done", hub.texts(run_id)
        assert hub.moves(run_id) == ["leased", "running", "waiting", "running", "verifying", "done"]
        assert hub.runs[run_id]["summary"] == "Wrote plan wait-helper."
        assert "author" in hub.last_heartbeat["run_kinds"]

        commands = [entry for entry in machine.commands() if entry["run"] == run_id]
        lines = commands[0]["stdout"].splitlines()
        assert lines[:3] == ["author", "detached", alpha_head]  # at origin's default branch, on no branch
        assert "alpha" in lines and "beta" in lines and "gamma" not in lines
        assert "Author mode: ask in chat." in commands[0]["stdout"]
        assert commands[1]["exit"] == 1 and "is an author run, which writes a plan" in commands[1]["stderr"]
        assert commands[2]["exit"] == 1 and "is an author run: only the agent of a review run" in commands[2]["stderr"]
        texts = hub.texts(run_id)
        assert any("Skills from the hub for the agent: create-exec-plan version 3." in text for text in texts)
        assert any("No checkout of gamma on this worker" in text for text in texts)
        assert any("detached at" in text and "read-only" in text for text in texts)

        directory = machine.directory(run_id)
        written = directory / ".claude" / "skills" / author.AUTHOR_SKILL
        assert (written / "references" / "author.md").read_text(encoding="utf-8") == "Questions go in the chat.\n"
        assert (directory / ".evo-run" / "authored-plan.yaml").exists()
        (start,) = [entry for entry in machine.starts() if entry["run"] == run_id]
        assert start["cwd"] == str(directory) and start["prompt"].startswith("Write the plan of a wait helper.")
        assert "Repos of the project this worker has no checkout of: gamma." in start["prompt"]

        # the agent committed in the harness's worktree: nothing reached origin
        assert machine.origin_rev("alpha", "refs/heads/main") == alpha_head
        assert machine.origin("alpha", "for-each-ref", "--format=%(refname)", "refs/heads") == "refs/heads/main"
        assert git("rev-parse", "HEAD", cwd=machine.checkouts["alpha"]) == alpha_head

    with_daemon(machine, body)


def test_an_author_run_without_create_exec_plan_fails_before_its_agent_starts(machine):  # noqa: F811
    machine.scenarios({"author": [{"touch": str(machine.tmp / "started")}]})

    async def body(hub, daemon):
        run_id = hub.queue_author_run(["alpha"], [])
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "failed"
        error = hub.runs[run_id]["error"]
        assert "no skill create-exec-plan" in error and "evo-agents hub skills publish" in error
        assert not (machine.tmp / "started").exists()

    with_daemon(machine, body)


def test_an_author_run_whose_skill_bundle_is_not_the_recorded_one_fails_and_writes_no_skill(machine):  # noqa: F811
    machine.scenarios({"author": [{"touch": str(machine.tmp / "started")}]})

    async def body(hub, daemon):
        ticket = hub.add_skill(author.AUTHOR_SKILL, 3, skill_bundle(machine.tmp), sha256="0" * 64)
        run_id = hub.queue_author_run(["alpha"], [ticket])
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "failed"
        assert "is not the one the hub recorded" in hub.runs[run_id]["error"]
        assert not (machine.tmp / "started").exists()
        assert not (machine.directory(run_id) / ".claude").exists()

    with_daemon(machine, body)


def test_an_author_run_needs_the_harness_checked_out_and_claude_code(machine):  # noqa: F811
    machine.scenarios({"author": [{"touch": str(machine.tmp / "started")}]})

    async def body(hub, daemon):
        ticket = hub.add_skill(author.AUTHOR_SKILL, 1, skill_bundle(machine.tmp))
        harness_missing = hub.queue_author_run(["gamma", "alpha"], [ticket])
        assert await hub.wait_state(harness_missing, "done", "failed", timeout=WAIT) == "failed"
        assert "no checkout of demo/gamma, the project's harness" in hub.runs[harness_missing]["error"]
        other_runtime = hub.queue_author_run(["alpha"], [ticket], runtime="opencode")
        assert await hub.wait_state(other_runtime, "done", "failed", timeout=WAIT) == "failed"
        assert "runs on claude-code only, not opencode" in hub.runs[other_runtime]["error"]
        assert not (machine.tmp / "started").exists()

    with_daemon(machine, body)


def test_claude_code_turns_off_the_question_tools_for_an_author_run_only(tmp_path):
    from tests.worker.test_budget import claude, command_of
    from tests.worker.test_runtimes import _samples

    authored = claude(tmp_path, _samples, kind="author")
    authored.options = authored.build_options("/opt/bin/claude")
    assert authored.options.disallowed_tools == list(author.QUESTION_TOOLS) == ["AskUserQuestion"]
    command = command_of(authored)
    assert command[command.index("--disallowedTools") + 1] == "AskUserQuestion"

    for kind in ("step", "plan", "review"):
        other = claude(tmp_path, _samples, kind=kind)
        other.options = other.build_options("/opt/bin/claude")
        assert other.options.disallowed_tools == [] and "--disallowedTools" not in command_of(other)


def test_the_author_skill_ticket_round_trips_through_the_bundle_checks(tmp_path):
    data = skill_bundle(tmp_path)
    contents = skills.read_bundle(data, author.AUTHOR_SKILL)
    assert contents.name == author.AUTHOR_SKILL
    target = tmp_path / "out" / author.AUTHOR_SKILL
    target.parent.mkdir()
    target.mkdir()
    skills.write_tree(contents, target)
    assert skills.tree_digest(target) == contents.tree
    assert [entry.path for entry in contents.files] == ["SKILL.md", "references/author.md"]


PLAN_YAML = """id: wait-helper
title: A wait helper
goal: Tests wait for a condition instead of sleeping.
steps:
  - id: 1
    title: Add the helper
    status: pending
"""


def test_an_author_runs_agent_puts_its_plan_reads_it_back_and_revises_it_with_its_revision(machine):  # noqa: F811
    revised = PLAN_YAML.replace("A wait helper", "A wait helper, revised")
    machine.scenarios(
        {
            "author": [
                {"cli": ["plan"]},
                {"write": {".evo-run/plan.yaml": PLAN_YAML, ".evo-run/revised.yaml": revised}},
                {"cli": ["put", ".evo-run/plan.yaml"]},
                {"cli": ["put", ".evo-run/revised.yaml"]},
                {"cli": ["plan", "--json"]},
                {"cli": ["put", ".evo-run/revised.yaml", "--if-revision", "1"]},
                {"result": {"summary": "Wrote plan wait-helper."}},
            ]
        }
    )

    async def body(hub, daemon):
        ticket = hub.add_skill(author.AUTHOR_SKILL, 3, skill_bundle(machine.tmp))
        run_id = hub.queue_author_run(["alpha"], [ticket])
        assert await ended_by_owner(hub, run_id) == "done", hub.texts(run_id)
        first, created, refused, read, revision = [e for e in machine.commands() if e["run"] == run_id]
        assert first["exit"] == 1 and f"author run {run_id} has put no plan on the hub yet" in first["stderr"]
        assert created["exit"] == 0, created
        assert "Created plan wait-helper of project demo on the hub at revision 1." in created["stdout"]
        assert "Pass --if-revision 1 to put it again." in created["stdout"]
        assert refused["exit"] == 1 and "pass if_revision" in refused["stderr"], "never replaced without a revision"
        assert read["exit"] == 0 and '"revision": 1' in read["stdout"] and '"plan_id": "wait-helper"' in read["stdout"]
        assert (
            revision["exit"] == 0
            and "Revised plan wait-helper of project demo on the hub: revision 2." in (revision["stdout"])
        )
        assert [(item["status"], item.get("if_revision")) for item in hub.plan_puts] == [
            (200, None),
            (409, None),
            (200, 1),
        ]
        assert hub.plans["wait-helper"]["body"]["title"] == "A wait helper, revised"
        assert hub.plan_puts[0]["body"]["steps"][0]["id"] == 1

    with_daemon(machine, body)


def test_an_author_run_of_a_plan_puts_that_plan_only_and_put_refuses_outside_an_author_run(machine):  # noqa: F811
    machine.scenarios(
        {
            "author": [
                {"write": {".evo-run/other.yaml": PLAN_YAML.replace("wait-helper", "other-plan")}},
                {"cli": ["put", ".evo-run/other.yaml", "--if-revision", "1"]},
                {"result": {"summary": "Revised nothing."}},
            ],
            "review": [
                {"write": {".evo-run/plan.yaml": PLAN_YAML}},
                {"cli": ["put", ".evo-run/plan.yaml"]},
                {"result": {"summary": "Reviewed."}},
            ],
        }
    )

    async def body(hub, daemon):
        import yaml

        hub.put_plan(yaml.safe_load(PLAN_YAML))
        ticket = hub.add_skill(author.AUTHOR_SKILL, 3, skill_bundle(machine.tmp))
        run_id = hub.queue_author_run(["alpha"], [ticket], plan_id="wait-helper")
        assert await ended_by_owner(hub, run_id) == "done", hub.texts(run_id)
        (other,) = [e for e in machine.commands() if e["run"] == run_id]
        assert other["exit"] == 1 and f"author run {run_id} writes plan wait-helper" in other["stderr"]
        assert hub.plans["wait-helper"]["revision"] == 1 and "other-plan" not in hub.plans

        review_id = hub.queue_review_run(["alpha"])
        assert await hub.wait_state(review_id, "done", "failed", timeout=WAIT) == "done", hub.texts(review_id)
        (put,) = [e for e in machine.commands() if e["run"] == review_id]
        assert put["exit"] == 1 and "only the agent of an author run" in put["stderr"], put
        assert [item["run_id"] for item in hub.plan_puts] == [run_id]

    with_daemon(machine, body)


# The chat (step 3)


def test_an_author_run_posts_each_turn_to_its_chat_and_the_reply_starts_the_next_turn_in_its_session(
    machine,  # noqa: F811
):
    machine.scenarios(
        {
            "author": [{"say": "Which repos should the wait helper cover?"}],
            "author/2": [
                {"result": {"summary": "Wrote plan wait-helper for alpha."}},
                {"say": "Put plan wait-helper at revision 1. I decided the order of the steps myself."},
            ],
        }
    )

    async def body(hub, daemon):
        ticket = hub.add_skill(author.AUTHOR_SKILL, 3, skill_bundle(machine.tmp))
        run_id = hub.queue_author_run(["alpha"], [ticket])
        assert await hub.wait_state(run_id, "waiting", "done", "failed", timeout=WAIT) == "waiting", hub.texts(run_id)
        assert hub.runs[run_id]["chat"] == ["Which repos should the wait helper cover?"]
        waits = "the run waits for its owner's reply in the chat"
        await wait_for(lambda: any(waits in text for text in hub.texts(run_id)), "the note that the run waits")
        assert len(machine.starts()) == 1, "no turn starts before the reply"

        assert hub.reply(run_id, "Only alpha, please.") == run_id
        await wait_for(lambda: len(hub.runs[run_id].get("chat", [])) == 2, "the second message of the agent")
        assert await ended_by_owner(hub, run_id) == "done", hub.texts(run_id)
        assert hub.runs[run_id]["chat"][1].startswith("Put plan wait-helper at revision 1.")
        assert hub.moves(run_id) == [
            "leased",
            "running",
            "waiting",
            "running",
            "waiting",
            "running",
            "verifying",
            "done",
        ]
        assert hub.runs[run_id]["summary"] == "Wrote plan wait-helper for alpha."
        assert all(message["delivered"] for message in hub.inbox[run_id])

        first, second = machine.starts()
        assert second["resume"] == first["session"], "the reply goes on in the agent's session"
        assert second["prompt"].startswith(author.REPLY_PROMPT) and second["prompt"].endswith("Only alpha, please.")

    with_daemon(machine, body)


def test_an_author_run_whose_owner_ended_the_chat_during_a_turn_ends_done_without_waiting_for_a_reply(
    machine,  # noqa: F811
):
    go_on = machine.tmp / "go-on"
    machine.scenarios({"author": [{"wait_for": str(go_on)}, {"say": "Put plan wait-helper."}]})

    async def body(hub, daemon):
        ticket = hub.add_skill(author.AUTHOR_SKILL, 3, skill_bundle(machine.tmp))
        run_id = hub.queue_author_run(["alpha"], [ticket])
        assert await hub.wait_state(run_id, "running", "failed", timeout=WAIT) == "running", hub.texts(run_id)
        hub.finish(run_id)
        await wait_for(lambda: daemon.runs[run_id].finish_asked.is_set(), "the heartbeat to say finish")
        go_on.write_text("go", encoding="utf-8")
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        assert hub.moves(run_id) == ["leased", "running", "verifying", "done"]
        assert hub.runs[run_id]["chat"] == ["Put plan wait-helper."], "the last message still reaches the chat"
        assert hub.runs[run_id]["summary"] == "Put plan wait-helper.", "without a result, the last message"
        assert any("the owner ended the chat: the run ends done" in text for text in hub.texts(run_id))

    with_daemon(machine, body)


def test_a_parked_author_run_keeps_its_directory_and_the_reply_resumes_it_there_in_its_session(machine):  # noqa: F811
    machine.scenarios(
        {
            "author": [
                {"write": {".evo-run/draft.yaml": "id: wait-helper\n"}},
                {"say": "Should the helper poll or subscribe?"},
            ],
            "author/resume": [
                {"sh": "pwd -P; echo $EVO_RUN_ID; cat .evo-run/draft.yaml; ls .claude/skills"},
                {"say": "Polling it is: plan wait-helper is on the hub."},
            ],
        }
    )
    found = {}

    async def body(hub, daemon):
        ticket = hub.add_skill(author.AUTHOR_SKILL, 3, skill_bundle(machine.tmp))
        parked = hub.queue_author_run(["alpha", "beta"], [ticket])
        assert await hub.wait_state(parked, "waiting", "done", "failed", timeout=WAIT) == "waiting", hub.texts(parked)
        hub.park(parked)  # nobody replied within a day
        await wait_for(lambda: parked not in daemon.runs, "the parked run to free its slot")
        record = machine.home.load_run(parked)
        assert record["state"] == "parked" and record["session_id"] and record["parked_at"]
        assert any("The hub parked the run" in text for text in hub.texts(parked))
        assert (machine.directory(parked) / ".claude" / "skills" / author.AUTHOR_SKILL / "SKILL.md").exists()

        resumed = hub.reply(parked, "Poll every second.")  # the reply to a parked run queues the run that resumes it
        assert resumed != parked and hub.runs[resumed]["spec"]["resume_of_run_id"] == parked
        assert await ended_by_owner(hub, resumed) == "done", hub.texts(resumed)
        found.update(parked=parked, resumed=resumed, hub=hub, session=record["session_id"])

    with_daemon(machine, body)
    parked, resumed, hub = found["parked"], found["resumed"], found["hub"]
    directory = machine.directory(parked)
    first, again = machine.starts()
    assert first["session"] == found["session"] and again["resume"] == found["session"], "the same session"
    assert again["run"] == resumed and Path(again["cwd"]) == directory, "the same directory and worktrees"
    assert again["prompt"].startswith(author.RESUME_PROMPT.format(id=resumed))
    assert again["prompt"].endswith("Poll every second.")
    (looked,) = [entry for entry in machine.commands() if entry["run"] == resumed]
    assert looked["stdout"].splitlines() == [
        str(directory.resolve()),
        str(resumed),
        "id: wait-helper",
        "create-exec-plan",
    ]
    assert hub.runs[parked]["chat"] == ["Should the helper poll or subscribe?"]
    assert hub.runs[resumed]["chat"] == ["Polling it is: plan wait-helper is on the hub."]
    assert hub.moves(resumed) == ["leased", "running", "waiting", "running", "verifying", "done"]
    assert hub.runs[parked]["state"] == "done" and hub.runs[parked]["resumed_by"] == resumed
    old, new = machine.home.load_run(parked), machine.home.load_run(resumed)
    assert old["resumed_by"] == resumed and old["dir"] is None and old["repos"] == [], "the cleanup leaves them alone"
    assert new["dir"] == str(directory) and [item["repo"] for item in new["repos"]] == ["alpha", "beta"]
    texts = hub.texts(resumed)
    assert any(f"Goes on from parked run #{parked}" in text for text in texts)
    assert not any("Skills from the hub" in text for text in texts), "the parked run's skills stay; none is fetched"
