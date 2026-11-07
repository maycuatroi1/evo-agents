"""The handoff between the agents of an agent system (criterion a13 of plan multi-agent-systems): an ask carries a
brief (goal, output, bounds, done-when, refs) that heads the prompt of the run asked and that its span records; an
ask off the asker's interacts edges is refused without a reason and runs off design with one; the budget of a trace
refuses the next ask; ``worker agent forward --run RUN`` posts the last answer of a run of the same trace verbatim,
written by its agent and forwarded by the run's. An agent of no system asks as before.
"""

from __future__ import annotations

import argparse

from evo_agents.worker import agent_ask_cli


def parsed(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="evo-agents worker")
    agent_ask_cli.register_agent(parser.add_subparsers(dest="command"))
    return parser.parse_args(["agent", *argv])


def test_the_ask_takes_a_brief_and_a_reason_and_sends_only_what_was_given():
    args = parsed(
        "ask",
        "fixer",
        "fix the retry loop",
        "--goal",
        "a green wait test",
        "--done-when",
        "the test passes ten times",
        "--ref",
        "evo-agents:tests/test_wait.py",
        "--ref",
        "run:41",
        "--reason",
        "the fixer owns the loop",
    )
    assert agent_ask_cli.ask_payload(args, "fixer", "fix the retry loop") == {
        "handle": "fixer",
        "body": "fix the retry loop",
        "goal": "a green wait test",
        "done_when": "the test passes ten times",
        "refs": ["evo-agents:tests/test_wait.py", "run:41"],
        "reason": "the fixer owns the loop",
    }
    bare = parsed("ask", "fixer", "fix the retry loop")
    assert agent_ask_cli.ask_payload(bare, "fixer", "fix the retry loop") == {
        "handle": "fixer",
        "body": "fix the retry loop",
    }, "with no flag the ask is as before"


def test_forward_takes_the_run_whose_answer_it_posts():
    args = parsed("forward", "--run", "42")
    assert args.run == 42 and args.func is agent_ask_cli.cmd_agent_forward


# On the hub

from tests.hub import pg  # noqa: E402

if pg.DSN:
    from sqlalchemy import select

    from evo_agents.hub import tables
    from tests.hub.live import sql
    from tests.hub.test_chat_runs import (  # noqa: F401
        app_key,
        chat_worker,
        claim,
        client,
        created,
        ended,
        hub,
        in_chats,
        moved,
        recover,
        reply,
        started,
        thread,
    )
    from tests.hub.test_system_traces import SPEC, put_system, spans_of

    def asked(client, worker: dict, run_id: int, handle: str, body: str, **brief) -> dict:
        response = in_chats(client, worker, run_id, "agent/asks", {"handle": handle, "body": body, **brief})
        assert response.status_code == 201, response.text
        return response.json()

    def forwarded(client, worker: dict, run_id: int, child: int) -> dict:
        response = in_chats(client, worker, run_id, "agent/forwards", {"run_id": child})
        assert response.status_code == 201, response.text
        return response.json()

    def system_desk(client, hub, spec: dict = SPEC) -> tuple:
        """The system review (in -> reviewer -> fixer), a worker for each agent, and the owner's run of @reviewer."""
        put_system(client, hub["owner"], spec)
        studio = chat_worker(client, hub["owner"], "studio", slots=3)
        fixer = chat_worker(client, hub["owner"], "fixer-node", slots=3, pool="fixer")
        chat = created(client, hub["owner"], agents=["reviewer", "fixer"])
        mention, spec_run = started(client, hub, studio, chat)
        return studio, fixer, chat, mention, spec_run["id"]

    def test_a_brief_heads_the_prompt_of_the_run_asked_and_its_span_says_which_fields_came(client, hub, hub_db):
        studio, fixer, _, _, parent = system_desk(client, hub)
        brief = {"goal": "a green wait test", "output": "the commit and the test run", "done_when": "it passes"}
        made = asked(client, studio, parent, "fixer", "fix the retry loop of the wait helper", **brief)
        assert made["refusal"] is None and made["run_id"] is not None
        spec = claim(client, fixer)
        assert spec["id"] == made["run_id"]
        head = spec["prompt"].split("\n\n# ")[0:3]
        assert spec["prompt"].index("# Your brief from @reviewer") < spec["prompt"].index("# Your agent system"), head
        for line in ("Goal: a green wait test", "Output: the commit and the test run", "Done when: it passes"):
            assert line in spec["prompt"]
        assert "Bounds:" not in spec["prompt"]
        (tool,) = [s for s in spans_of(hub_db) if s["tool"] == "agent_ask"]
        assert tool["attributes"]["evo.brief_fields"] == ["goal", "output", "done_when"]
        assert (tool["to_node"], tool["off_design"], tool["status"]) == ("fix", False, "ok")
        child = [s for s in spans_of(hub_db) if s["run_id"] == made["run_id"] and s["name"] == "invoke_agent"]
        assert [(s["node"], s["from_node"]) for s in child] == [("fix", "review")]

    def test_an_ask_off_the_edges_is_refused_without_a_reason_and_runs_off_design_with_one(client, hub, hub_db):
        studio, fixer, _, _, parent = system_desk(client, hub)
        child = asked(client, studio, parent, "fixer", "fix the retry loop")["run_id"]
        assert claim(client, fixer)["id"] == child
        moved(client, fixer, child, "running", session_id="sess-fix")
        refused = asked(client, fixer, child, "reviewer", "review my fix")
        assert refused["run_id"] is None
        assert "@reviewer is off the interacts edges of @fixer" in refused["refusal"]
        assert "lead to no agent" in refused["refusal"] and "--reason" in refused["refusal"]
        made = asked(client, fixer, child, "reviewer", "review my fix", reason="the fix touches the review rules")
        assert made["refusal"] is None and made["run_id"] is not None
        tools = [(s["status"], s["off_design"], s["reason"]) for s in spans_of(hub_db) if s["tool"] == "agent_ask"]
        assert tools[1:] == [
            ("refused", True, refused["refusal"]),
            ("ok", True, "the fix touches the review rules"),
        ]
        (span,) = [s for s in spans_of(hub_db) if s["run_id"] == made["run_id"] and s["name"] == "invoke_agent"]
        assert (span["from_node"], span["off_design"], span["reason"]) == (
            "fix",
            True,
            "the fix touches the review rules",
        )

    def test_an_agent_of_no_system_asks_as_before(client, hub, hub_db):
        studio = chat_worker(client, hub["owner"], "studio", slots=2)
        chat_worker(client, hub["owner"], "fixer-node", pool="fixer")
        chat = created(client, hub["owner"], agents=["reviewer", "fixer"])
        _, spec = started(client, hub, studio, chat)
        made = asked(client, studio, spec["id"], "fixer", "fix the retry loop")
        assert made["refusal"] is None and made["run_id"] is not None
        assert spans_of(hub_db) == [] and sql(hub_db, select(tables.agent_ask_briefs.c.run_id)) == []

    def test_the_budget_of_a_trace_refuses_the_next_ask(client, hub, hub_db):
        studio, _, _, _, parent = system_desk(client, hub, {**SPEC, "limits": {"trace_runs": 2, "trace_usd": 2}})
        assert asked(client, studio, parent, "fixer", "fix the retry loop")["refusal"] is None
        over = asked(client, studio, parent, "fixer", "and the flaky clock")
        assert over["run_id"] is None and "budget of 2 runs (limits.trace_runs" in over["refusal"]
        statuses = [s["status"] for s in spans_of(hub_db) if s["tool"] == "agent_ask"]
        assert statuses == ["ok", "refused"]

    def test_forward_posts_the_answer_of_a_run_of_the_trace_verbatim_as_its_agents(client, hub, hub_db):
        studio, fixer, chat, mention, parent = system_desk(client, hub)
        child = asked(client, studio, parent, "fixer", "fix the retry loop")["run_id"]
        early = forwarded(client, studio, parent, child)
        assert early["message_id"] is None and f"run #{child} has not ended" in early["refusal"]
        own = forwarded(client, studio, parent, parent)
        assert "not its own" in own["refusal"]
        moved(client, studio, parent, "waiting")
        recover(client)
        assert claim(client, fixer)["id"] == child
        moved(client, fixer, child, "running", session_id="sess-fix")
        answer = "Fixed in `wait.py`:\n\n- the loop backs off\n- the test passes ten times"
        reply(client, fixer, child, answer)
        ended(client, fixer, child)
        r = tables.runs
        (resumed,) = sql(hub_db, select(r.c.id).where(r.c.resume_of_run_id == parent))[0]
        assert claim(client, studio)["id"] == resumed
        moved(client, studio, resumed, "running", session_id="sess-1")
        done = forwarded(client, studio, resumed, child)
        assert done["refusal"] is None and done["message_id"] is not None
        (shown,) = [m for m in thread(client, hub["owner"], chat["id"], mention["id"]) if m["id"] == done["message_id"]]
        assert shown["body"] == answer
        assert (shown["author"]["kind"], shown["author"]["handle"], shown["forwarded_by"]) == (
            "agent",
            "fixer",
            "reviewer",
        )
        (span,) = [s for s in spans_of(hub_db) if s["tool"] == "agent_forward" and s["status"] == "ok"]
        assert span["attributes"]["evo.forwarded_run_id"] == child and span["message_id"] == done["message_id"]

        # a run of another trace is not forwarded
        other_chat = created(client, hub["owner"], title="Another talk", agents=["reviewer"])
        _, elsewhere = started(client, hub, studio, other_chat, "@reviewer read the release notes")
        moved(client, studio, elsewhere["id"], "verifying", "done")
        far = forwarded(client, studio, resumed, elsewhere["id"])
        assert far["message_id"] is None and "is not in the trace of this run" in far["refusal"]
