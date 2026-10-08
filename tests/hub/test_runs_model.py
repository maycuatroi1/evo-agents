"""The run and worker model of evo_agents.hub.runs, without Postgres or a server.

The checks step 2 of the worker-fleet plan names: every move between run states outside TRANSITIONS is refused,
whoever asks; ready_steps is right on a fixture plan with a blocked step, steps whose dependencies are not done and
done steps; a prompt is at most 32 KiB, and a long context is cut with a mark. Around them: a worker's status from
its heartbeats, the event kinds, and what the prompt tells the agent.

The checks step 1 of the plan-runs-and-decisions plan names: the moves into and out of ``waiting`` and ``parked``
and who may make each; time spent waiting for an answer does not count toward the timeout; the prompt of a plan run
of 60 steps stays under 32 KiB; and the prompt of a run of kind step is the one 0.3.0 gave, byte for byte
(``golden/step-prompts-0.3.0.json``)."""

import itertools
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evo_agents.harness import load_schema, load_yaml
from evo_agents.hub import runs
from evo_agents.hub.plans import step_key
from evo_agents.hub.runs import (
    ACTIVE_STATES,
    ACTORS,
    CLOCK_STATES,
    EVENT_KINDS,
    HELD_STATES,
    MAX_PROMPT_BYTES,
    RUN_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    TransitionRefused,
    build_plan_prompt,
    build_prompt,
    can_transition,
    check_transition,
    clip,
    past_timeout,
    plan_repo,
    ready_steps,
    run_seconds,
    unready_reason,
    worker_status,
)
from evo_agents.schema import errors, validate

FIXTURE = Path(__file__).parent / "fixtures" / "runs" / "ready-steps.yaml"
COMPLETED = sorted((Path(__file__).parent / "fixtures" / "plans" / "completed").glob("*.yaml"))
STEP_PROMPTS_030 = Path(__file__).parent / "golden" / "step-prompts-0.3.0.json"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


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
        "waiting",
        "review",
        "parked",
        "done",
        "failed",
        "lost",
        "cancelled",
    )
    assert runs.RUN_KINDS == ("step", "plan", "review")
    assert ACTORS == ("worker", "owner", "reaper")
    assert set(TRANSITIONS) == set(RUN_STATES)
    assert set(HELD_STATES) | set(ACTIVE_STATES) | set(TERMINAL_STATES) == set(RUN_STATES)
    assert not set(ACTIVE_STATES) & set(TERMINAL_STATES)
    # a waiting run keeps its worker, its slot and its lease; a parked one keeps none, yet holds its plan back
    assert "waiting" in HELD_STATES and "waiting" in runs.MESSAGE_STATES
    assert "parked" in ACTIVE_STATES and "parked" not in HELD_STATES and "parked" not in runs.MESSAGE_STATES
    assert set(CLOCK_STATES) == set(HELD_STATES) - {"waiting"}
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
        # a plan run: a decision answered in time, one answered after the run was parked, one nobody answered
        [*headless[:2], ("running", "waiting", "worker"), ("waiting", "running", "worker"), *headless[2:]],
        [*headless[:2], ("running", "waiting", "worker"), ("waiting", "parked", "reaper"), ("parked", "done", "owner")],
        [
            *headless[:2],
            ("running", "waiting", "worker"),
            ("waiting", "parked", "reaper"),
            ("parked", "cancelled", "reaper"),
        ],
        [*headless[:2], ("running", "waiting", "worker"), ("waiting", "cancelled", "worker")],
        [*headless[:2], ("running", "waiting", "worker"), ("waiting", "lost", "reaper")],
    ):
        for old, new, actor in path:
            check_transition(old, new, actor)


# Each move into or out of waiting and parked, with the one actor set that may make it; every other move from either
# state, and every move into either from elsewhere, is refused.
WAIT_MOVES = {
    ("running", "waiting"): {"worker"},  # the agent's turn ended with a decision open
    ("waiting", "running"): {"worker"},  # the answer reached the agent
    ("waiting", "parked"): {"reaper"},  # nobody answered within DECISION_WAIT_SECONDS
    ("waiting", "failed"): {"worker", "reaper"},
    ("waiting", "cancelled"): {"worker", "reaper"},  # the worker stopped the agent on the owner's cancel
    ("waiting", "lost"): {"reaper"},  # the worker stopped extending the lease
    ("parked", "done"): {"owner"},  # the owner's answer queued the run that resumes it
    ("parked", "cancelled"): {"owner", "reaper"},  # the owner gave up on it, or PARKED_DAYS passed
}


@pytest.mark.parametrize(
    ("old", "new", "actor"),
    [
        (old, new, actor)
        for old, new in itertools.product(RUN_STATES, RUN_STATES)
        if "waiting" in (old, new) or "parked" in (old, new)
        for actor in ACTORS
    ],
)
def test_waiting_and_parked_move_only_as_the_plan_says_and_only_by_their_actors(old, new, actor):
    allowed = actor in WAIT_MOVES.get((old, new), set())
    assert can_transition(old, new, actor) is allowed
    if allowed:
        check_transition(old, new, actor)
    else:
        with pytest.raises(TransitionRefused):
            check_transition(old, new, actor)


def test_refusals_around_waiting_and_parked_name_who_may_move():
    with pytest.raises(TransitionRefused, match="only the reaper can"):
        check_transition("waiting", "parked", "worker")
    with pytest.raises(TransitionRefused, match="only the worker can"):
        check_transition("waiting", "running", "owner")
    with pytest.raises(TransitionRefused, match="only the owner can"):
        check_transition("parked", "done", "reaper")
    with pytest.raises(TransitionRefused, match="only the owner or the reaper can"):
        check_transition("parked", "cancelled", "worker")
    with pytest.raises(TransitionRefused, match="cannot go from parked to running"):
        check_transition("parked", "running", "worker")  # a parked run goes on only as a new run
    with pytest.raises(TransitionRefused, match="cannot go from interactive to waiting"):
        check_transition("interactive", "waiting", "worker")


# Time toward the timeout


def test_only_the_time_the_agent_runs_counts_toward_the_timeout():
    """A plan run of 2 hours: an hour running, a day waiting for an answer, half an hour running, a day and a half
    waiting then parked, and 20 minutes in the run that resumes it. The hub settles the count at each move."""
    start = NOW
    timeline = [  # (state the run enters, hours later)
        ("leased", 0),
        ("running", 0.05),
        ("waiting", 1),
        ("running", 24),
        ("waiting", 24.5),
        ("parked", 48.5),
    ]
    spent, state, since = 0, "queued", start
    for new, hours in timeline:
        moved = start + timedelta(hours=hours)
        spent = run_seconds(spent, state, since, moved)
        state, since = new, moved
    assert spent == int(1.5 * 3600)
    for hours in (49, 24 * 6):
        later = start + timedelta(hours=hours)
        assert run_seconds(spent, "parked", since, later) == spent
        assert not past_timeout(spent, "parked", since, later, 2 * 3600)
    # the run that resumes it starts from what the parked run had used
    resumed = start + timedelta(days=3)
    assert run_seconds(spent, "running", resumed, resumed + timedelta(minutes=20)) == int(1.5 * 3600) + 1200
    assert not past_timeout(spent, "running", resumed, resumed + timedelta(minutes=30), 2 * 3600)
    assert past_timeout(spent, "running", resumed, resumed + timedelta(minutes=31), 2 * 3600)


@pytest.mark.parametrize("state", RUN_STATES)
def test_the_clock_runs_only_in_the_clock_states(state):
    since = NOW - timedelta(hours=3)
    counted = run_seconds(60, state, since, NOW)
    assert counted == (60 + 3 * 3600 if state in CLOCK_STATES else 60)
    assert past_timeout(60, state, since, NOW, 2 * 3600) is (state in CLOCK_STATES)


def test_the_clock_never_runs_backwards_or_without_a_start():
    assert run_seconds(90, "running", NOW + timedelta(minutes=5), NOW) == 90  # the hub's clock is behind
    assert run_seconds(90, "running", None, NOW) == 90
    assert not past_timeout(7200, "running", NOW, NOW, 7200)  # at the timeout exactly is not past it
    assert past_timeout(7200, "running", NOW - timedelta(seconds=1), NOW, 7200)


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
    assert (runs.DECISION_WAIT_SECONDS, runs.PARKED_DAYS, runs.PLAN_TIMEOUT_CHOICES) == (86400, 7, (2, 4, 8, 24))


def test_decisions_and_notices_the_plan_names():
    assert runs.DECISION_CATEGORIES == (
        "deploy",
        "delete_data",
        "live_migration",
        "external_send",
        "spend_money",
        "architecture",
        "scope",
    )
    assert set(runs.DECISION_CATEGORY_TEXT) == set(runs.DECISION_CATEGORIES)
    assert all(text and "\n" not in text for text in runs.DECISION_CATEGORY_TEXT.values())
    assert runs.WORKER_NOTICE_KINDS == ("push_default_branch", "merge_default_branch", "plan_finished", "run_failed")
    assert runs.NOTICE_KINDS == (*runs.WORKER_NOTICE_KINDS, "curator_brief")
    assert runs.NOTIFICATION_KINDS == ("decision", "notice", "proposal")
    assert runs.DECISION_STATES == ("open", "answered", "expired", "cancelled")
    assert runs.DELIVERY_STATES == ("pending", "delivered", "failed")
    assert (runs.DECISION_OPTIONS, runs.MAX_DECISION_CONTEXT_BYTES, runs.MAX_DELIVERY_ATTEMPTS) == ((2, 6), 16 << 10, 5)
    key = re.compile(runs.OPTION_KEY)
    assert all(key.match(name) for name in ("a", "B", "keep-sqlite", "use_pg", "x" * 32))
    assert not any(key.match(name) for name in ("", "-a", "two words", "x" * 33, "é"))


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


# The prompt of a run of kind step, as 0.3.0 gave it


def step_prompts() -> dict[str, str]:
    """The prompts ``golden/step-prompts-0.3.0.json`` holds, made with the 0.3.0 code: steps of the fixture with and
    without a repo, a context over its budget, and a plan over every budget."""
    prompts = {}
    plan = load_yaml(FIXTURE)
    for key in (2, 3, 7, 12):
        prompts[f"step-{key}"] = build_prompt(plan, step(plan, key), plan_repo(plan, step(plan, key)))
    prompts["no-repo"] = build_prompt(plan, step(plan, 10), None)
    long = load_yaml(FIXTURE)
    long["context"] = "Bối cảnh rất dài của kế hoạch. " * 2000
    prompts["long-context"] = build_prompt(long, step(long, 2), plan_repo(long, step(long, 2)))
    huge = load_yaml(FIXTURE)
    text = "Nội dung rất dài, có dấu tiếng Việt và ký tự 𝔘𝔫𝔦𝔠𝔬𝔡𝔢. " * 400
    deps = [{"id": n, "title": text, "what": text, "status": "done", "evidence": text} for n in range(100, 110)]
    huge["goal"] = huge["context"] = text
    target = {"id": 7, "title": text, "repo": text, "what": text, "verify": text, "note": text}
    target["depends_on"] = [d["id"] for d in deps]
    huge["steps"] = [*deps, target]
    prompts["over-budget"] = build_prompt(huge, target, {"repo": text, "branch": text})
    return prompts


def test_the_prompt_of_a_step_run_is_the_one_0_3_0_gave():
    golden = json.loads(STEP_PROMPTS_030.read_text(encoding="utf-8"))
    made = step_prompts()
    assert set(made) == set(golden)
    for name, prompt in golden.items():
        assert made[name] == prompt, name


# The prompt of a plan run

WORKTREES = "/Users/me/.evo/worker/worktrees/demo-12"


def sixty_steps() -> dict:
    """A plan of 60 steps over three repos in the shape of the harness plans, the first 10 done: long titles, a few
    KiB of what, verify and note each, and evidence on the done ones."""
    repos = [{"repo": name, "branch": "feat/kb-type-dashboard"} for name in ("m1-kb-service", "m1-portal")]
    repos.append({"repo": "meridai-harness", "branch": "main"})
    steps = []
    for number in range(1, 61):
        title = f"Bước {number}: thêm bảng điều khiển loại tài liệu cho kho tri thức, có kiểm thử đầu cuối"
        steps.append(
            {
                "id": number,
                "title": title * 3 if number == 30 else title,
                "repo": repos[number % 3]["repo"],
                "what": f"Làm phần {number} của bảng điều khiển. " * 80,
                "depends_on": list(range(max(1, number - 3), number)),
                "verify": "cd ~/github/m1-portal && pnpm lint && pnpm typecheck && pnpm test " * 10,
                "status": "done" if number <= 10 else "pending",
                "note": "Ghi chú dài cho bước này. " * 40,
                **({"evidence": "commit abc1234; pnpm test 0. " * 30} if number <= 10 else {}),
            }
        )
    return {
        "id": "kb-type-dashboard",
        "goal": "Người dùng xem được bảng điều khiển theo loại tài liệu trong kho tri thức. " * 10,
        "context": "Kho tri thức có ba dịch vụ và một cổng web. " * 120,
        "repos": repos,
        "steps": steps,
    }


def test_a_plan_run_prompt_of_60_steps_lists_every_open_step_and_stays_under_32_kib():
    plan = sixty_steps()
    worktrees = {entry["repo"]: f"{WORKTREES}/{entry['repo']}" for entry in plan["repos"]}
    prompt = build_plan_prompt(plan, plan["repos"], worktrees)
    assert size(prompt) <= MAX_PROMPT_BYTES
    assert "# Steps not done yet: 50 of 60" in prompt
    for number in range(1, 61):
        assert (f"\n- {number} [pending] Bước {number}:" in prompt) is (number > 10), number
    assert "more steps not done" not in prompt
    assert "- 11 [pending] Bước 11: thêm bảng điều khiển loại tài liệu cho kho tri thức" in prompt
    assert (
        "\n- 13 [pending] Bước 13: thêm bảng điều khiển loại tài liệu cho kho tri thức, có kiểm thử đầu cuối " in prompt
    )
    assert "(repo m1-portal; after 10, 11, 12)\n" in prompt
    # a title over its budget is cut on its own line
    (line,) = [line for line in prompt.splitlines() if line.startswith("- 30 [pending] ")]
    assert line.endswith("... (repo m1-kb-service; after 27, 28, 29)")
    assert size(line[len("- 30 [pending] ") : line.index(" (repo")]) <= runs.LIST_TITLE_BYTES
    assert plan["steps"][20]["what"][:200] not in prompt  # what, verify and note stay in the plan file
    assert plan["steps"][20]["note"][:100] not in prompt
    assert f"- meridai-harness: branch main, worktree {WORKTREES}/meridai-harness" in prompt
    # the context is cut to its budget, the rest is whole
    assert prompt.count("cut by the hub") == 1 and "# Context of the plan, shortened" in prompt


def test_the_plan_run_prompt_holds_the_rules_the_repos_and_the_open_steps(plan):
    prompt = build_plan_prompt(plan, plan["repos"], {"evo-agents": f"{WORKTREES}/evo-agents"})
    for text in (
        "You are running the plan fleet-demo from the evo-agents hub, as one plan run on a worker",
        "Rules for this plan run:",
        "execute-plan skill",
        "EVO_RUN_KIND=plan",
        ".evo-run/plan.yaml holds the whole plan",
        "`evo-agents worker plan`",
        "Checkpoint steps (a verification, a review, a deploy) are yours too",
        "`evo-agents worker step KEY in_progress --repo REPO`",
        "`evo-agents worker step KEY done --repo REPO --evidence TEXT --verify COMMAND`",
        "refuses done when one exits other than 0",
        "Do not call the hub's plan_step tool, `evo harness step` or `evo-agents hub plan`.",
        "`evo-agents worker ask --category CATEGORY",
        "with 2 to 6 options",
        "does not count toward the run's timeout",
        'line that starts with "Decision:"',
        "even when it is the repo's default branch",
        "never push or merge into a branch the plan does not name for that repo, never force-push",
        "`evo-agents worker notify --kind push_default_branch",
        "`--kind merge_default_branch`",
        ".evo-run/result.json",
        '"summary"',
        f"- evo-agents: branch feat/fleet-demo, worktree {WORKTREES}/evo-agents",
        "- evo-agents-harness: branch main, worktree evo-agents-harness",
        plan["goal"].strip(),
        plan["context"].strip(),
        "# Steps not done yet: 10 of 12",
        "- 2 [pending] Model runs and workers (repo evo-agents; after 1)",
        "- 4 [blocked] Sign dispatches (repo evo-agents)",
        "- 7 [in_progress] Pairing codes (repo evo-agents; after 1)",
        "- 10 [pending] Release (repo evo-agents; after 1, 42)",
    ):
        assert text in prompt, text
    for category, text in runs.DECISION_CATEGORY_TEXT.items():
        assert f"\n  - {category}: {text}\n" in prompt, category
    for done in (1, 11):
        assert f"\n- {done} [" not in prompt
    assert step(plan, 2)["what"] not in prompt and step(plan, 1)["evidence"] not in prompt
    # none of the rules of a run of one step
    assert "Do this one step and nothing else" not in prompt and "Do not switch branches" not in prompt
    assert "cut by the hub" not in prompt


def test_a_plan_run_prompt_stays_within_32_kib_whatever_the_plan_holds(plan):
    huge = "Nội dung rất dài, có dấu tiếng Việt và ký tự 𝔘𝔫𝔦𝔠𝔬𝔡𝔢. " * 10_000
    plan["goal"] = plan["context"] = plan["id"] = huge
    plan["steps"] = [
        {"id": f"{n}-{huge[:300]}", "title": huge, "repo": huge, "what": huge, "depends_on": [huge] * 50}
        for n in range(400)
    ]
    repos = [{"repo": f"{n}{huge[:500]}", "branch": huge} for n in range(100)]
    prompt = build_plan_prompt(plan, repos, {repos[0]["repo"]: huge})
    assert size(prompt) <= MAX_PROMPT_BYTES
    assert "�" not in prompt and prompt.encode().decode("utf-8") == prompt
    for text in ("Rules for this plan run:", "  - scope: ", ".evo-run/result.json", "# Goal of the plan"):
        assert text in prompt, text
    assert "# Steps not done yet: 400 of 400" in prompt
    assert "more steps not done, which .evo-run/plan.yaml lists" in prompt  # the list ends on a whole line
    assert "more repos, which .evo-run/plan.yaml lists" in prompt
    assert prompt.endswith("lists\n")
    listed = prompt[prompt.index("# Steps not done yet") :].splitlines()[2:]
    assert listed and all(line.startswith("- ") and "cut by the hub" not in line for line in listed)
    assert all(line.endswith("...)") for line in listed[:-1])  # each long value cut with "...", one line a step
    assert "cut by the hub" not in prompt[: prompt.index("# Goal of the plan")]  # the rules and repos are whole


def test_a_plan_run_prompt_of_an_odd_plan():
    body = {"steps": ["a bare string", {"what": "x"}, {"id": "d", "status": "done", "what": "x"}], "repos": "x"}
    prompt = build_plan_prompt(body, [], None)
    assert "You are running the plan without an id" in prompt
    assert "# Steps not done yet: 2 of 3" in prompt
    assert "\n- 0 [pending] a bare string\n" in prompt and "\n- 1 [pending] untitled\n" in prompt
    assert "- none named; the plan file says which repos the steps are in" in prompt
    named = build_plan_prompt(body, ["evo-agents", {"repo": "web", "branch": "feat/x"}], None)
    assert "- evo-agents: branch the branch checked out, worktree evo-agents" in named
    assert "- web: branch feat/x, worktree web" in named
    assert "# Steps not done yet: 0 of 0" in build_plan_prompt({"id": "empty"}, [], {})
