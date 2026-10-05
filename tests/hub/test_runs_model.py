"""The run and worker model of evo_agents.hub.runs, without Postgres or a server.

The checks step 2 of the worker-fleet plan names: every move between run states outside TRANSITIONS is refused,
whoever asks; ready_steps is right on a fixture plan with a blocked step, steps whose dependencies are not done and
done steps; a prompt is at most 32 KiB, and a long context is cut with a mark. Around them: a worker's status from
its heartbeats, the event kinds, and what the prompt tells the agent."""

import itertools
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from evo_agents.harness import load_schema, load_yaml
from evo_agents.hub import runs
from evo_agents.hub.plans import step_key
from evo_agents.hub.runs import (
    ACTIVE_STATES,
    ACTORS,
    EVENT_KINDS,
    HELD_STATES,
    MAX_PROMPT_BYTES,
    RUN_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    TransitionRefused,
    build_prompt,
    can_transition,
    check_transition,
    clip,
    plan_repo,
    ready_steps,
    unready_reason,
    worker_status,
)
from evo_agents.schema import errors, validate

FIXTURE = Path(__file__).parent / "fixtures" / "runs" / "ready-steps.yaml"
COMPLETED = sorted((Path(__file__).parent / "fixtures" / "plans" / "completed").glob("*.yaml"))
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def plan():
    return load_yaml(FIXTURE)


def step(plan: dict, key) -> dict:
    return next(s for i, s in enumerate(plan["steps"]) if step_key(s, i) == str(key))


def size(text: str) -> int:
    return len(text.encode())


# Run states


def test_the_table_covers_every_state_and_only_known_actors():
    assert RUN_STATES == (
        "queued",
        "leased",
        "running",
        "interactive",
        "verifying",
        "review",
        "done",
        "failed",
        "lost",
        "cancelled",
    )
    assert ACTORS == ("worker", "owner", "reaper")
    assert set(TRANSITIONS) == set(RUN_STATES)
    assert set(HELD_STATES) | set(ACTIVE_STATES) | set(TERMINAL_STATES) == set(RUN_STATES)
    assert not set(ACTIVE_STATES) & set(TERMINAL_STATES)
    for old, moves in TRANSITIONS.items():
        for new, actors in moves.items():
            assert new in RUN_STATES and new != old
            assert actors and actors <= set(ACTORS)
    assert all(TRANSITIONS[state] == {} for state in TERMINAL_STATES)


def test_every_move_outside_the_table_is_refused():
    allowed = 0
    for old, new, actor in itertools.product(RUN_STATES, RUN_STATES, ACTORS):
        if actor in TRANSITIONS[old].get(new, ()):
            check_transition(old, new, actor)
            assert can_transition(old, new, actor)
            allowed += 1
        else:
            with pytest.raises(TransitionRefused):
                check_transition(old, new, actor)
            assert not can_transition(old, new, actor)
    assert allowed == sum(len(actors) for moves in TRANSITIONS.values() for actors in moves.values())


@pytest.mark.parametrize(
    ("old", "new", "actor"),
    [
        ("queued", "running", "worker"),  # a worker claims before it runs
        ("queued", "leased", "owner"),  # only a worker claims
        ("review", "done", "worker"),  # only the owner approves
        ("verifying", "done", "owner"),
        ("running", "lost", "worker"),  # only the reaper declares a run lost
        ("lost", "queued", "reaper"),  # a lost run stays lost; the next attempt is a new run
        ("done", "running", "owner"),
        ("running", "running", "worker"),
        ("bogus", "done", "worker"),
        ("running", "bogus", "worker"),
        ("running", "verifying", "admin"),
    ],
)
def test_refusals_name_the_reason(old, new, actor):
    with pytest.raises(TransitionRefused) as refused:
        check_transition(old, new, actor)
    assert str(refused.value)
    assert not can_transition(old, new, actor)


def test_a_refusal_names_who_may_make_the_move():
    with pytest.raises(TransitionRefused, match="only the owner can"):
        check_transition("review", "done", "worker")
    with pytest.raises(TransitionRefused, match="only the worker or the reaper can"):
        check_transition("running", "failed", "owner")
    with pytest.raises(TransitionRefused, match="final"):
        check_transition("cancelled", "queued", "owner")


def test_the_life_of_a_run_through_the_table():
    headless = [("queued", "leased", "worker"), ("leased", "running", "worker"), ("running", "verifying", "worker")]
    for path in (
        [*headless, ("verifying", "done", "worker")],
        [*headless, ("verifying", "review", "worker"), ("review", "done", "owner")],
        [*headless[:2], ("running", "interactive", "worker"), ("interactive", "running", "worker")],
        [("queued", "leased", "worker"), ("leased", "interactive", "worker"), ("interactive", "verifying", "worker")],
        [("queued", "cancelled", "owner")],
        [*headless[:2], ("running", "lost", "reaper")],
        [*headless[:2], ("running", "failed", "reaper")],  # the last attempt's lease expired
        [*headless[:2], ("running", "cancelled", "worker")],  # the worker stopped the agent on the owner's cancel
    ):
        for old, new, actor in path:
            check_transition(old, new, actor)


def test_every_state_is_reachable_and_every_run_can_end():
    reached, frontier = {"queued"}, ["queued"]
    while frontier:
        for new in TRANSITIONS[frontier.pop()]:
            if new not in reached:
                reached.add(new)
                frontier.append(new)
    assert reached == set(RUN_STATES)
    for state in RUN_STATES:
        seen, frontier = {state}, [state]
        while frontier:
            for new in TRANSITIONS[frontier.pop()]:
                if new not in seen:
                    seen.add(new)
                    frontier.append(new)
        assert seen & set(TERMINAL_STATES), state


# Workers


@pytest.mark.parametrize(
    ("ago", "drained", "revoked", "status"),
    [
        (0, False, False, "online"),
        (runs.HEARTBEAT_SECONDS, False, False, "online"),
        (runs.OFFLINE_AFTER_SECONDS, False, False, "online"),
        (runs.OFFLINE_AFTER_SECONDS + 1, False, False, "offline"),
        (None, False, False, "offline"),
        (10, True, False, "draining"),
        (runs.OFFLINE_AFTER_SECONDS + 1, True, False, "offline"),
        (10, False, True, "revoked"),
        (10, True, True, "revoked"),
        (None, False, True, "revoked"),
    ],
)
def test_worker_status_follows_heartbeats_drain_and_revoke(ago, drained, revoked, status):
    found = worker_status(
        last_heartbeat_at=None if ago is None else NOW - timedelta(seconds=ago),
        drained_at=NOW - timedelta(hours=1) if drained else None,
        revoked_at=NOW - timedelta(minutes=1) if revoked else None,
        now=NOW,
    )
    assert found == status
    assert found in runs.WORKER_STATUSES


def test_the_protocol_constants_the_plan_fixes():
    assert (runs.PROTOCOL_HEADER, runs.PROTOCOL_VERSION) == ("X-Evo-Worker-Protocol", "1")
    assert (runs.HEARTBEAT_SECONDS, runs.OFFLINE_AFTER_SECONDS, runs.LEASE_SECONDS) == (15, 300, 300)
    assert (runs.CLAIM_WAIT_SECONDS, runs.MAX_ATTEMPTS) == (25, 3)
    assert (runs.MAX_BATCH_EVENTS, runs.MAX_BATCH_BYTES, runs.MAX_EVENT_BODY_BYTES) == (500, 1 << 20, 64 << 10)
    assert (runs.MAX_RUN_EVENTS, runs.MAX_MESSAGE_BYTES, MAX_PROMPT_BYTES) == (20_000, 8 << 10, 32 << 10)


# Event kinds


def test_event_kinds_are_acps_session_updates_and_the_hubs_own():
    assert runs.ACP_EVENT_KINDS == (
        "agent_message_chunk",
        "agent_thought_chunk",
        "tool_call",
        "tool_call_update",
        "plan",
        "usage_update",
    )
    assert runs.HUB_EVENT_KINDS == ("user_message", "state", "system", "output")
    assert len(set(EVENT_KINDS)) == len(EVENT_KINDS) == 10
    assert set(runs.WORKER_EVENT_KINDS) == set(EVENT_KINDS) - {"user_message", "state"}


# Which steps are ready


def test_the_fixture_is_a_valid_plan(plan):
    assert errors(validate(plan, load_schema("plan"))) == []


def test_ready_steps_on_the_fixture(plan):
    assert [s["id"] for s in ready_steps(plan)] == [2, 6, 8, 9, 12]


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        (1, "its status is done, not pending"),
        (3, "it waits for step 2 (pending)"),
        (4, "its status is blocked, not pending"),
        (5, "it waits for step 4 (blocked)"),
        (7, "its status is in_progress, not pending"),
        (10, "it waits for step 42, which the plan does not have"),
        (11, "its status is done, not pending"),
    ],
)
def test_steps_that_are_not_ready_say_why(plan, key, reason):
    assert unready_reason(plan, step(plan, key)) == reason


def test_blocking_steps_that_are_not_done_hold_back_only_their_dependents(plan):
    assert step(plan, 2)["blocking"] and step(plan, 2)["status"] == "pending"
    assert step(plan, 6) in ready_steps(plan)
    assert step(plan, 3) not in ready_steps(plan)


def test_a_step_becomes_ready_when_its_dependency_is_done(plan):
    step(plan, 2)["status"] = "done"
    step(plan, 4)["status"] = "pending"
    assert [s["id"] for s in ready_steps(plan)] == [3, 4, 6, 8, 9, 12]


def test_odd_plans_have_no_ready_step_they_should_not():
    body = {
        "id": "odd",
        "steps": [
            "a bare string",
            {"id": "a", "what": "x", "status": "pending", "depends_on": "b"},
            {"id": "b", "what": "x", "status": "pending", "depends_on": ["b"]},
            {"id": "c", "what": "x", "status": "pending", "depends_on": None},
            {"id": "d", "what": "x", "status": "pending", "depends_on": []},
        ],
    }
    assert [s["id"] for s in ready_steps(body)] == ["c", "d"]
    assert unready_reason(body, body["steps"][0]) == "it is a bare string, not a mapping, so it has no status"
    assert unready_reason(body, body["steps"][1]) == "its depends_on is not a list of step ids"
    assert unready_reason(body, body["steps"][2]) == "it waits for step b (pending)"
    assert ready_steps({"id": "empty"}) == [] and ready_steps({"id": "x", "steps": {"1": {}}}) == []


@pytest.mark.parametrize("path", COMPLETED, ids=lambda p: p.stem)
def test_a_completed_plan_has_no_ready_step(path):
    assert ready_steps(load_yaml(path)) == []


# The prompt


def test_the_prompt_holds_the_step_its_plan_and_the_rules(plan):
    two = step(plan, 2)
    prompt = build_prompt(plan, two, plan_repo(plan, two))
    for text in (
        "step 2 of the plan fleet-demo",
        plan["goal"].strip(),
        plan["context"].strip(),
        "# Step 2: Model runs and workers",
        two["what"],
        two["verify"],
        two["note"],
        "## Step 1: Survey the runtimes",
        step(plan, 1)["evidence"],
        "Repository: evo-agents. Branch: feat/fleet-demo.",
        "Do this one step and nothing else",
        "plan_step",
        "`evo harness step`",
        "on the branch it is on (feat/fleet-demo)",
        ".evo-run/result.json",
        '"verify_commands"',
        '"summary"',
    ):
        assert text in prompt, text
    assert step(plan, 11)["evidence"] not in prompt  # not a dependency of step 2
    assert "cut by the hub" not in prompt
    assert size(prompt) <= MAX_PROMPT_BYTES


def test_the_prompt_names_every_dependency_even_without_evidence(plan):
    step(plan, 11).pop("evidence")
    prompt = build_prompt(plan, step(plan, 12), plan_repo(plan, step(plan, 12)))
    assert "## Step 1: Survey the runtimes" in prompt
    assert "## Step 11: Stack decisions\nNo evidence recorded." in prompt
    unknown = build_prompt(plan, step(plan, 10), None)
    assert "## Step 42\nThe plan has no such step." in unknown
    assert "Repository: not named by the plan. Branch: the branch checked out." in unknown


def test_plan_repo_finds_the_entry_of_the_steps_repo(plan):
    assert plan_repo(plan, step(plan, 1)) == plan["repos"][1]
    assert plan_repo(plan, step(plan, 2)) == plan["repos"][0]
    assert plan_repo(plan, {"repo": "elsewhere", "what": "x"}) == {"repo": "elsewhere"}
    assert plan_repo({"repos": [{"repo": "only", "branch": "b"}]}, {"what": "x"}) == {"repo": "only", "branch": "b"}
    assert plan_repo(plan, {"what": "x"}) is None


def test_a_long_context_is_cut_with_a_mark(plan):
    plan["context"] = "Bối cảnh rất dài của kế hoạch. " * 2000
    prompt = build_prompt(plan, step(plan, 2), plan_repo(plan, step(plan, 2)))
    assert size(prompt) <= MAX_PROMPT_BYTES
    assert "Bối cảnh rất dài của kế hoạch." in prompt
    assert prompt.count("cut by the hub") == 1
    total = size(plan["context"].strip())
    assert f"of {total} bytes left out" in prompt
    assert step(plan, 2)["what"] in prompt and "Rules for this run:" in prompt  # the rest survives
    head, _, _ = prompt.partition("\n[cut by the hub")
    assert size(head[head.index("# Context of the plan, shortened") :]) <= runs.CONTEXT_BYTES


def test_the_prompt_stays_within_32_kib_whatever_the_plan_holds(plan):
    huge = "Nội dung rất dài, có dấu tiếng Việt và ký tự 𝔘𝔫𝔦𝔠𝔬𝔡𝔢. " * 10_000
    deps = [{"id": n, "title": huge, "what": huge, "status": "done", "evidence": huge} for n in range(100, 400)]
    plan["goal"] = plan["context"] = plan["id"] = huge
    target = {
        "id": huge,
        "title": huge,
        "repo": huge,
        "what": huge,
        "verify": huge,
        "note": huge,
        "depends_on": [d["id"] for d in deps],
    }
    plan["steps"] = [*deps, target]
    prompt = build_prompt(plan, target, {"repo": huge, "branch": huge})
    assert size(prompt) <= MAX_PROMPT_BYTES
    assert "\ufffd" not in prompt  # cuts fall on character boundaries
    assert prompt.encode().decode("utf-8") == prompt
    assert "Rules for this run:" in prompt and ".evo-run/result.json" in prompt
    for heading in ("# Goal of the plan", "# Context of the plan, shortened", "## What to do", "## Note"):
        assert heading in prompt, heading
    assert "# Evidence from the steps this one depends on" in prompt


def test_clip_keeps_short_text_and_cuts_long_text_on_a_character_boundary():
    assert clip("short", 100) == "short"
    assert clip("x" * 100, 100) == "x" * 100
    text = "đ" * 1000  # two bytes each
    cut = clip(text, 300)
    assert size(cut) <= 300
    kept, _, mark = cut.partition("\n[cut by the hub: ")
    assert set(kept) == {"đ"}
    assert mark == f"{size(text) - size(kept)} of {size(text)} bytes left out; the plan on the hub has the full text]"
    assert size(clip(text, 10)) <= 10  # a budget smaller than the mark keeps only the start
