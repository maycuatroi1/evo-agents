"""Plan steps handed through an agent system: an ask whose brief names a plan step (``--ref plan:<id>/step:<step>``,
``system_traces.ref_steps``) goes to a thread, and so a session, of that step in the work chat (``work_chats``), so
independent steps run at once on the free slots of the agent's pool while asks of the same step keep its session; and
the Token strategy rates a chat run that works on plan steps at least as hard as the hardest of them
(``token_strategy``, ``model_router.with_steps``): the coordinator of a plan handed to a system, and an ask of a step,
whatever the words of its prompt say."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import model_router, tables
from evo_agents.hub.model_router import Rating
from evo_agents.hub.server.systems.service.traces import ref_plans, ref_steps
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import sql
from tests.hub.test_chat_runs import (  # noqa: F401
    PROJECT,
    app_key,
    chat_worker,
    claim,
    client,
    hub,
    in_chats,
    moved,
)
from tests.hub.test_model_router import put_policy
from tests.hub.test_system_traces import push_plan, put_system, system_dispatch
from tests.hub.test_work_chats import asked, claimed, desk, opened, run_row

PLAN = credentials_api.PLAN
LADDER = {"easy": "haiku", "medium": "sonnet", "hard": "opus"}


def step_ref(step: int | str, plan: str = PLAN) -> str:
    return f"plan:{plan}/step:{step}"


def resumed_from(db, run_id: int) -> int | None:
    r = tables.runs
    return sql(db, select(r.c.resume_of_run_id).where(r.c.id == run_id))[0][0]


def routing_of(db, run_id: int) -> dict:
    r = tables.runs
    return sql(db, select(r.c.routing).where(r.c.id == run_id))[0][0]


def test_refs_name_plan_steps_with_the_step_or_beside_one_plan():
    brief = {"refs": [step_ref(2), " plan:northwind-ops ", "repo:docs/a.md", step_ref("a3", "lumen-kb")]}
    assert ref_steps(brief) == {(PLAN, "2"), ("lumen-kb", "a3")}
    assert ref_plans(brief) == [PLAN, "northwind-ops", "lumen-kb"], "a step ref still gives the trace its plan"
    assert ref_steps({"refs": [f"plan:{PLAN}", "step:4"]}) == {(PLAN, "4")}
    assert ref_steps({"refs": [f"plan:{PLAN}", "plan:northwind-ops", "step:4"]}) == set(), "which plan is unclear"
    assert ref_steps({"refs": ["step:4"]}) == set() and ref_steps(None) == set()


def test_an_ask_of_another_plan_step_opens_a_session_of_its_own_and_one_of_the_same_step_goes_back_to_it(
    client, hub, hub_db
):
    studio, fixer, _, _, parent = desk(client, hub)  # the fixer's pool: one node with three slots
    first = asked(client, studio, parent, "fixer", "do step 1", refs=[step_ref(1)])["run_id"]
    _, work = opened(hub_db)
    assert claimed(client, fixer) == first
    moved(client, fixer, first, "running", session_id="sess-step-1")
    one_thread = run_row(hub_db, first)[1]

    second = asked(client, studio, parent, "fixer", "do step 2", refs=[step_ref(2)])["run_id"]
    assert second not in (None, first)
    assert run_row(hub_db, second)[:2] != (work, one_thread) and run_row(hub_db, second)[0] == work
    assert resumed_from(hub_db, second) is None, "a session of its own, not the one of step 1"
    assert claimed(client, fixer) == second, "it takes a free slot while step 1 runs"
    moved(client, fixer, second, "running", session_id="sess-step-2")
    two_thread = run_row(hub_db, second)[1]

    again = asked(client, studio, parent, "fixer", "and keep step 1 small", refs=[step_ref(1)])
    assert again["run_id"] == first, "the same step: the inbox of its run"
    beside = asked(client, studio, parent, "fixer", "step 2 also needs a test", refs=[f"plan:{PLAN}", "step:2"])
    assert beside["run_id"] == second

    moved(client, fixer, first, "verifying", "done")
    later = asked(client, studio, parent, "fixer", "one more fix of step 1", refs=[step_ref(1)])["run_id"]
    assert later not in (first, second)
    assert run_row(hub_db, later)[:2] == (work, one_thread) and resumed_from(hub_db, later) == first
    assert run_row(hub_db, second)[1] == two_thread

    plain = asked(client, studio, parent, "fixer", "a question of no step")["run_id"]
    assert plain == later, "an ask that names no step goes to the agent's latest thread, as before"


def test_with_steps_rates_at_least_the_hardest_step():
    easy = Rating("easy", "rule", "the prompt speaks of documents, wording or an export alone")
    steps = [("1", Rating("easy", "plan", "the step says difficulty easy")),
             ("3", Rating("hard", "plan", "the step says difficulty hard"))]  # fmt: skip
    raised = model_router.with_steps(easy, steps)
    assert (raised.difficulty, raised.source) == ("hard", "plan") and "step 3" in raised.reason
    hard = Rating("hard", "rule", "the prompt speaks of a migration")
    assert model_router.with_steps(hard, steps[:1]) is hard
    assert model_router.with_steps(Rating(None, "scorer", "nothing"), steps[:1]).difficulty == "easy"
    assert model_router.with_steps(easy, []) is easy


def stepped_plan() -> dict:
    """The plan of ``test_credentials_api``, each step with its difficulty: step 1 exports documents (easy), step 2
    is medium, step 3 is hard and done, step 4 easy."""
    body = credentials_api.plan_body()
    levels = {"1": "easy", "2": "medium", "3": "hard", "4": "easy"}
    for step in body["steps"]:
        step["difficulty"] = levels[str(step["id"])]
    body["steps"][0]["title"] = "Export the docs of evo-agents"
    body["steps"][2]["status"] = "done"
    return body


def test_a_plan_handed_to_a_system_and_an_ask_of_its_step_are_rated_by_the_hardest_step(client, hub, hub_db):
    put_policy(client, hub, {"routing": {"enabled": True, "scorer": "off", "ladder": LADDER}})
    put_system(client, hub["owner"])
    studio = chat_worker(client, hub["owner"], "studio", slots=2)
    fixer = chat_worker(client, hub["owner"], "fixer-node", slots=2, pool="fixer")
    push_plan(client, hub["owner"], stepped_plan())
    found = system_dispatch(client, hub["owner"]).json()
    assert model_router.rate_prompt("chat", "Export the docs").difficulty == "easy", "its words alone read easy"

    lead = claim(client, studio)
    assert lead["id"] == found["run_id"]
    routing = routing_of(hub_db, lead["id"])
    assert (routing["difficulty"], routing["source"]) == ("medium", "plan"), routing  # step 3 is done
    assert "step 2" in routing["reason"]
    moved(client, studio, lead["id"], "running", session_id="sess-lead")

    hard = asked(client, studio, lead["id"], "fixer", "export the docs", refs=[step_ref(1), step_ref(3)])["run_id"]
    assert claimed(client, fixer) == hard
    routing = routing_of(hub_db, hard)
    assert (routing["difficulty"], routing["source"]) == ("hard", "plan") and "step 3" in routing["reason"], routing
    easy = asked(client, studio, lead["id"], "fixer", "export the docs", refs=[step_ref(4)])["run_id"]
    assert easy != hard and claimed(client, fixer) == easy
    assert routing_of(hub_db, easy)["difficulty"] == "easy"
