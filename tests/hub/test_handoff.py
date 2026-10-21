"""The handoff of a plan: ``evo_agents.hub.handoff``, GET /v1/projects/{project}/plans/{plan_id}/handoff, the command
``evo-agents hub plan handoff`` and the MCP tool plan_handoff (step 2 of the plan context-economy, criterion a2).

Checked without Postgres: the text has the plan's goal, each step done with its title, the commit hashes of its
evidence, its Decision lines verbatim and the rest of its evidence cut, and each step not done with its status and
depends_on; with a step, the evidence of the steps it depends on comes first; for every budget and the plans of the
fixtures, the text stays within its budget, never cuts a Decision line, and ends with a note naming what was left out
and plan_show; the same plan, revision, step and budget give the same text. With Postgres: the route reads as GET
.../plans/{plan_id} does (a grant, the plan's label, the session's sink), and the command and the MCP tool give the
route's text byte for byte.
"""

import argparse
import copy
import json

import pytest

from evo_agents.hub import plan_cli
from evo_agents.hub.contract import check_command, commands
from evo_agents.hub.handoff import (
    BYTES_PER_TOKEN,
    CUT_MARK,
    DEFAULT_BUDGET_TOKENS,
    MAX_BUDGET_TOKENS,
    MIN_BUDGET_TOKENS,
    build_handoff,
    commits_of,
    decisions_of,
    estimate_tokens,
)
from evo_agents.hub.mirror import read_plan
from evo_agents.hub.plans import PlanProblem, step_key
from evo_agents.kg.serve import DATA_META
from tests.hub import live
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_plans import COMPLETED, PROJECT, AppHub, counts, needs_pg, registration, reversed_keys

EVIDENCE = (
    "evo-agents@326c1e8 (commits 7b82e41, 326c1e8): pytest 12 passed on ports 3424/18424.\n"
    "Decision: From xl the run page is one window tall, so the composer stays in view.\n"
    "  Decision: Thụt lề, ký tự đặc biệt: <script>, `code`, ${x}, 100%.\n"
    "Merged at a7308e5. sha256:" + "ab" * 32 + " is no commit, nor 20261012, deadbeef or a uuid 1b4e28ba-2fa1-11d2."
)


def rollout() -> dict:
    return {
        "id": "rollout",
        "goal": "Ship the rollout to every team.",
        "steps": [
            {"id": 1, "title": "Write the docs", "status": "done", "evidence": EVIDENCE},
            {
                "id": 2,
                "title": "Build the API",
                "status": "done",
                "depends_on": [1],
                "evidence": "app@1234567: tests pass\nDecision: REST, not GraphQL, because the web reads it.",
            },
            {"id": 3, "title": "Ship it", "status": "in_progress", "depends_on": [1, 2]},
            {"id": "4b", "title": "Tell people", "status": "pending", "depends_on": ["3"]},
            {"id": 5, "title": "Wait for legal", "status": "blocked"},
        ],
    }


HEADER = (
    "# Handoff of plan rollout, revision 4, project evo-agents{step}\n"
    "Built by the hub from the plan it holds, in at most 4000 tokens. plan_show (MCP evo-hub) or "
    "`evo-agents hub plan show rollout --project evo-agents` has the whole plan.\n"
)
STEP_1 = (
    "### Step 1: Write the docs{status}\n"
    "Commits: 326c1e8, 7b82e41, a7308e5\n"
    "Decision: From xl the run page is one window tall, so the composer stays in view.\n"
    "Decision: Thụt lề, ký tự đặc biệt: <script>, `code`, ${{x}}, 100%.\n"
    "Evidence: evo-agents@326c1e8 (commits 7b82e41, 326c1e8): pytest 12 passed on ports 3424/18424.\n"
    "Merged at a7308e5. sha256:" + "ab" * 32 + " is no commit, nor 20261012, deadbeef or a uuid 1b4e28ba-2fa1-11d2.\n"
)
STEP_2 = (
    "### Step 2: Build the API{status}\n"
    "Commits: 1234567\n"
    "Decision: REST, not GraphQL, because the web reads it.\n"
    "Evidence: app@1234567: tests pass\n"
)
GOAL = "## Goal\nShip the rollout to every team.\n"
OPEN = (
    "## Steps not done: 3 of 5\n"
    "- 3 [in_progress] Ship it (after 1, 2)\n"
    "- 4b [pending] Tell people (after 3)\n"
    "- 5 [blocked] Wait for legal\n"
)


def handoff(body: dict, **options):
    return build_handoff(body, project=PROJECT, revision=4, **options)


# The text


def test_a_handoff_has_the_goal_the_steps_done_with_their_decisions_and_the_steps_left():
    found = handoff(rollout())
    expected = "\n".join(
        [
            HEADER.format(step=""),
            GOAL,
            "## Steps done\n" + STEP_1.format(status="") + STEP_2.format(status=""),
            OPEN,
        ]
    )
    assert found.text == expected
    assert (found.tokens, found.cut) == (estimate_tokens(expected), False)


def test_with_a_step_the_evidence_of_the_steps_it_depends_on_comes_first():
    for_3 = handoff(rollout(), step=3).text
    deps = "## The steps step 3 depends on\n" + STEP_1.format(status=" (done)") + STEP_2.format(status=" (done)")
    assert for_3 == "\n".join([HEADER.format(step="; for step 3"), deps, GOAL, OPEN])

    for_4b = handoff(rollout(), step="4b").text
    assert for_4b == "\n".join(
        [
            HEADER.format(step="; for step 4b"),
            "## The steps step 4b depends on\n### Step 3: Ship it (in_progress)\nNo evidence recorded.\n",
            GOAL,
            "## Other steps done\n" + STEP_1.format(status="") + STEP_2.format(status=""),
            OPEN,
        ]
    )
    missing = rollout()
    missing["steps"][2]["depends_on"] = [9, 1]
    assert "### Step 9: the plan has no such step\n### Step 1: Write the docs (done)" in handoff(missing, step=3).text


def test_a_step_the_plan_lacks_and_a_budget_out_of_range_are_refused():
    with pytest.raises(PlanProblem, match="no step '7'"):
        handoff(rollout(), step=7)
    for budget in (MIN_BUDGET_TOKENS - 1, MAX_BUDGET_TOKENS + 1, 0):
        with pytest.raises(ValueError, match=f"{MIN_BUDGET_TOKENS} to {MAX_BUDGET_TOKENS} tokens"):
            handoff(rollout(), budget_tokens=budget)


def test_commits_decision_lines_and_the_rest_of_an_evidence():
    assert commits_of(EVIDENCE) == ["326c1e8", "7b82e41", "a7308e5"]
    assert commits_of("app@1234567 and 7654321, abcdef0123456789abcdef0123456789abcdef01") == [
        "1234567",
        "abcdef0123456789abcdef0123456789abcdef01",
    ]
    assert commits_of("feat/x-abc1234 cafe0123-dirty ABC1234 abc123") == []
    assert decisions_of(EVIDENCE) == [
        "Decision: From xl the run page is one window tall, so the composer stays in view.",
        "Decision: Thụt lề, ký tự đặc biệt: <script>, `code`, ${x}, 100%.",
    ]
    assert decisions_of("no Decision: here\nDecision:") == ["Decision:"]


def test_the_same_plan_revision_step_and_budget_give_the_same_text():
    body = read_plan(COMPLETED[0]).body
    first = handoff(body, budget_tokens=1500)
    assert handoff(copy.deepcopy(body), budget_tokens=1500) == first
    assert handoff(reversed_keys(body), budget_tokens=1500) == first  # as JSONB hands keys back in its own order
    assert handoff(body, budget_tokens=1600).text != first.text
    assert build_handoff(body, project=PROJECT, revision=5, budget_tokens=1500).text != first.text


# The budget


def big_plan(steps: int = 40, decisions: int = 8) -> dict:
    """A plan whose handoff is far over any budget: long Decision lines and evidence in every step done."""
    body = {"id": "big", "goal": "Lớn. " * 2000, "steps": []}
    for n in range(1, steps + 1):
        lines = [f"evo-agents@{n:03d}abcd1: verify pass. " + "Kết quả đo được ghi lại. " * 120]
        lines += [f"Decision: lựa chọn {n}.{d} " + "vì lý do rõ ràng " * (5 + d) for d in range(decisions)]
        done = n <= steps * 3 // 4
        step = {"id": n, "title": f"Bước {n} " * 30, "status": "done" if done else "pending"}
        step |= {"depends_on": [n - 1, n - 2]} if n > 2 else {}
        step |= {"evidence": "\n".join(lines)} if done else {}
        body["steps"].append(step)
    return body


def plans_to_fit() -> list[dict]:
    return [read_plan(path).body for path in COMPLETED] + [big_plan(), big_plan(steps=200, decisions=2)]


@pytest.mark.parametrize("budget", [MIN_BUDGET_TOKENS, 2000, DEFAULT_BUDGET_TOKENS, MAX_BUDGET_TOKENS])
def test_the_text_stays_within_its_budget_and_never_cuts_a_decision_line(budget):
    for body in plans_to_fit():
        decisions = {line for step in body["steps"] for line in decisions_of(str(step.get("evidence") or ""))}
        keys = [None] + [step_key(step, i) for i, step in enumerate(body["steps"]) if step.get("depends_on")][:3]
        for key in keys:
            found = handoff(body, step=key, budget_tokens=budget)
            assert len(found.text.encode()) <= budget * BYTES_PER_TOKEN, (body["id"], key)
            assert found.tokens == estimate_tokens(found.text) <= budget
            shown = [line for line in found.text.splitlines() if line.startswith("Decision:")]
            assert set(shown) <= decisions, (body["id"], key)
            assert found.text.startswith(f"# Handoff of plan {body['id']}, revision 4, project {PROJECT}")
            if found.cut:
                assert "[Left out to stay within" in found.text or "bytes left out;" in found.text
            if key is not None:  # the steps it depends on come before the goal
                assert found.text.index(f"## The steps step {key} depends on") < found.text.find("## Goal") or (
                    "## Goal" not in found.text
                )


def test_a_cut_says_what_was_left_out_and_where_to_read_it():
    found = handoff(big_plan(), budget_tokens=2000)
    assert found.cut
    note = found.text.splitlines()[-1]
    assert note.startswith("[Left out to stay within 2000 tokens: ")
    assert "Decision lines of steps" in note and "the rest of the evidence of steps" in note
    assert note.endswith(
        "plan_show (MCP evo-hub, plan_id big, with step for one step) or "
        f"`evo-agents hub plan show big --project {PROJECT}` has them.]"
    )
    # the goal and the steps not done come before the Decision lines of the steps done
    assert "## Goal\n" in found.text and "## Steps not done: 10 of 40\n- 31 [pending]" in found.text
    for command in ("evo-agents hub plan show big --project evo-agents",):
        assert check_command(commands(), command) == []

    goal = found.text.split("## Goal\n", 1)[1].split("\n", 1)[0]
    total = len(big_plan()["goal"].strip().encode())
    assert goal.endswith(
        CUT_MARK.format(omitted=total - len(goal.split(" [cut: ")[0].encode()), total=total, where="plan_show")
    )

    whole = handoff(big_plan(steps=4, decisions=1), budget_tokens=MAX_BUDGET_TOKENS)
    evidence = next(line for line in whole.text.splitlines() if line.startswith("Evidence: evo-agents@001abcd1"))
    assert evidence.endswith("has it whole]") and "plan_show with step 1" in evidence


# The route, the command and the MCP tool


@pytest.fixture
def hub(hub_db, tmp_path, github):
    """The app behind https://hub.test, project evo-agents registered with a sink that clears public only (low), a
    plan with two steps done, and a client per login (setup_project of test_plans)."""
    from fastapi.testclient import TestClient

    from evo_agents.hub.server.app import create_app
    from tests.hub.test_plans import setup_project

    sinks = registration()["sinks"] + [{"id": "low", "kind": "agent-session", "clearance": {"level": "public"}}]
    config = live.hub_config(hub_db, tmp_path, github, public_url="https://hub.test")
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        hubs = setup_project(client, github, sinks=sinks)
        body = rollout()
        for step in body["steps"]:
            step |= {"repo": "evo-agents", "what": f"do {step['title'].lower()}"}
        answer = client.put(f"/v1/projects/{PROJECT}/plans/rollout", json={"body": body}, headers=hubs["alice"].headers)
        assert answer.status_code == 200, answer.text
        yield hubs


def get_handoff(member, **params):
    return member.client.get(f"/v1/projects/{PROJECT}/plans/rollout/handoff", params=params, headers=member.headers)


def mcp(member, arguments: dict, sink: str | None = None, data: bool = False) -> dict:
    headers = {
        **member.headers,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": "2025-11-25",
        "X-Evo-Project": PROJECT,
        **({"X-Evo-Sink": sink} if sink else {}),
    }
    message = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "plan_handoff", "arguments": arguments, **({"_meta": {DATA_META: True}} if data else {})},
    }
    response = member.client.post("/mcp", json=message, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["result"]


@needs_pg
def test_the_route_builds_the_handoff_of_the_plan_the_hub_holds(hub, hub_db):
    reader = hub["reader"]
    held = reader.client.get(f"/v1/projects/{PROJECT}/plans/rollout", headers=reader.headers).json()
    before = counts(hub_db)

    answer = get_handoff(reader, step="3", budget_tokens=1000)
    assert answer.status_code == 200, answer.text
    found = answer.json()
    assert_json_keys("hub plan handoff", found)
    built = build_handoff(held["body"], project=PROJECT, revision=held["revision"], step="3", budget_tokens=1000)
    assert found == {
        "project": PROJECT,
        "plan_id": "rollout",
        "revision": 1,
        "step": "3",
        "budget_tokens": 1000,
        "tokens": built.tokens,
        "cut": False,
        "text": built.text,
    }
    assert get_handoff(reader, step="3", budget_tokens=1000).json() == found  # same revision and budget, same text
    default = get_handoff(reader).json()
    assert default["budget_tokens"] == DEFAULT_BUDGET_TOKENS and default["step"] is None
    assert default["text"].startswith("# Handoff of plan rollout, revision 1, project evo-agents\n")
    assert counts(hub_db) == before  # a read writes nothing

    missing = get_handoff(reader, step="9")
    assert missing.status_code == 404 and "no step '9'" in missing.json()["message"]
    for budget in (MIN_BUDGET_TOKENS - 1, MAX_BUDGET_TOKENS + 1):
        assert get_handoff(reader, budget_tokens=budget).status_code == 422


@needs_pg
def test_reading_a_handoff_needs_what_reading_the_plan_needs(hub):
    refused = get_handoff(hub["admin"])  # a hub admin without a grant
    assert refused.status_code == 403
    assert refused.json()["message"] == f"reading the plans of project {PROJECT} needs a grant on it"
    assert get_handoff(hub["stranger"]).status_code == 404
    assert get_handoff(hub["public-reader"]).status_code == 404  # the plan is internal; the grant stops at public
    low = hub["reader"].client.get(
        f"/v1/projects/{PROJECT}/plans/rollout/handoff", headers={**hub["reader"].headers, "X-Evo-Sink": "low"}
    )
    assert low.status_code == 404

    below = mcp(hub["public-reader"], {"plan_id": "rollout"})
    assert below["isError"] and "no plan rollout" in below["content"][0]["text"]
    through_low = mcp(hub["reader"], {"plan_id": "rollout"}, sink="low")
    assert through_low["isError"] and "no plan rollout" in through_low["content"][0]["text"]
    assert mcp(hub["stranger"], {"plan_id": "rollout"})["isError"]


@needs_pg
def test_the_command_and_the_mcp_tool_give_the_route_s_text(hub, monkeypatch, capsys):
    reader = hub["reader"]
    for options, arguments, argv in (
        ({}, {}, []),
        (
            {"step": "4b", "budget_tokens": 1000},
            {"step": "4b", "budget_tokens": 1000},
            ["--step", "4b", "--budget-tokens", "1000"],
        ),
        ({"step": "3"}, {"step": 3}, ["--step", "3"]),
    ):
        text = get_handoff(reader, **options).json()["text"]

        tool = mcp(reader, {"plan_id": "rollout", **arguments})
        assert not tool.get("isError"), tool
        assert tool["content"] == [{"type": "text", "text": text}]  # what the agent's model reads: the text alone
        full = mcp(reader, {"plan_id": "rollout", **arguments}, data=True)
        assert full["content"] == tool["content"]
        assert "text" not in full["structuredContent"] and full["structuredContent"]["revision"] == 1

        monkeypatch.setattr(plan_cli, "_signed_in", lambda: (Through(reader), None))
        assert run_cli(["rollout", "--project", PROJECT, *argv]) == 0
        assert capsys.readouterr().out == text
        assert run_cli(["rollout", "--project", PROJECT, "--json", *argv]) == 0
        printed = json.loads(capsys.readouterr().out)
        assert_json_keys("hub plan handoff", printed)
        assert printed["text"] == text

    assert run_cli(["rollout", "--project", PROJECT, "--budget-tokens", "10"]) == 1
    assert f"--budget-tokens is {MIN_BUDGET_TOKENS} to {MAX_BUDGET_TOKENS}, not 10" in capsys.readouterr().err
    assert run_cli(["rollout", "--project", PROJECT, "--step", "9"]) == 1
    assert "no step '9'" in capsys.readouterr().err


class Through(AppHub):
    """AppHub of test_plans for a member's client of the ``hub`` fixture."""

    def __init__(self, member):
        self.client, self.headers, self.before = member.client, member.headers, None


def run_cli(argv: list[str]) -> int:
    from evo_agents.cli import build_parser

    args = build_parser().parse_args(["hub", "plan", "handoff", *argv])
    assert isinstance(args, argparse.Namespace)
    return args.func(args)
