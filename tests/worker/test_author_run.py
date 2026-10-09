"""Author runs on the worker, without Postgres: the daemon in this process with the fake runtime, the in-memory hub,
and the two checkouts (alpha and beta) of ``tests.worker.test_plan_runs``, alpha standing for the project's harness.

The checks step 1 of the hub-plan-authoring plan names on the worker: an author run's worktrees are detached at
origin's default branch, of the harness and of each other repo the worker has a checkout of; the run commits and
pushes nothing, even what the agent committed; the agent has create-exec-plan as the claim names it, written under
.claude/skills of its directory, without any sync of skills on the machine; a claim without that skill, or a bundle
other than the one the hub recorded, fails the run before the agent starts; the agent runs in EVO_RUN_KIND author; the
daemon says it runs author runs; Claude Code's question tools are off for an author run; the commands of a plan run's
and of a review run's agent refuse inside one."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("aiohttp", reason="the daemon needs the worker extra, evo-ak[worker]")

from evo_agents.hub import author, skills
from tests.worker.test_plan_runs import (
    WAIT,
    git,
    machine,  # noqa: F401 (a fixture)
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
        assert await hub.wait_state(run_id, "done", "failed", timeout=WAIT) == "done", hub.texts(run_id)
        assert hub.moves(run_id) == ["leased", "running", "verifying", "done"]
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
