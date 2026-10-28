"""The decision board of a project and the notices of the Loop's clock (``evo_agents.hub.server.loop``: ``board``,
``board_gates``, ``board_chain``, ``notices``), as criterion a8 of plan loop-engine names it, and schema 0096, on the
hub with Postgres.

GET /v1/projects/{p}/decision-board gathers what waits for a person: the three gates (the month's review of the goals
and a goal past its deadline; a baseline and a proposal of a claim of a canvas; a proposal of @builder of kind policy,
a repo of the merge queue and the Loop stopped by their breakers), then what the Loop's chain waits on (its decision
after a third failure, a run's decision, an author's question, a merge request held, a merge by hand, an audit sample,
an SRS version to review and its settle chat); each with since when, who may decide it and where, to anyone with a
grant, who sees only what they may read. The job hub.loop_notices sends goal_review on the first day of a month and
the Loop's brief at 07:00 for a project whose Curator sends none, once each, on the web and on Telegram, and nothing
for a project whose Loop is off. The chain is ``tests.hub.test_loop_tick``'s.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
from sqlalchemy import func, insert, select, update

from evo_agents.hub import tables
from evo_agents.hub.migrate import migrate
from evo_agents.hub.server.loop import notices
from evo_agents.hub.server.runs.service.dispatch import insert_run
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import sql
from tests.hub.test_ba_agent import granted, members, submitted  # noqa: F401 (members is a fixture)
from tests.hub.test_loop_admission import REPO, push_plan
from tests.hub.test_loop_tick import (  # noqa: F401 (client, hub and loop_core are fixtures)
    BASELINES,
    GOALS,
    METRIC,
    client,
    hub,
    loop_core,
    policy,
    project_id,
)
from tests.hub.test_migrate import ALL, move_to
from tests.hub.test_runs import OTHER, OWNER, PROJECT
from tests.hub.test_strategist_agent import CANVAS, CARD, CHI, READER, SLUG, edit
from tests.hub.test_value_check import NUMBER_KEY

app_key = credentials_api.app_key  # a fixture
BOARD = f"/v1/projects/{PROJECT}/decision-board"
DECIDE = f"/p/{PROJECT}/decide"


def board(client, headers) -> dict:
    found = client.get(BOARD, headers=headers)
    assert found.status_code == 200, found.text
    return found.json()


def of_kind(found: dict, kind: str) -> list[dict]:
    return [entry for entry in found["items"] if entry["kind"] == kind]


def sent(client, now: datetime) -> dict:
    return client.portal.call(partial(notices.send, client.app.state.engine, now=now))


def user_id(db, login: str) -> int:
    return sql(db, select(tables.users.c.id).where(tables.users.c.login == login))[0][0]


def db_now(db) -> datetime:
    return sql(db, select(func.now()))[0][0]


def a_run(client, db, owner: str = OWNER, plan_id: str | None = None) -> int:
    """An author run of ``owner``, as the Loop hands one out."""
    values = {
        "kind": "author",
        "project_id": project_id(db),
        "title": f"Loop {SLUG}",
        "dispatched_by": user_id(db, owner),
    }
    values |= {"requested_runtime": "claude-code", "runtime": "claude-code", "mode": "headless", "plan_id": plan_id}
    values["plan_revision"] = 1 if plan_id else None
    values |= {"approval": "auto", "timeout_s": 3900, "request": "Write the plan"}
    values["repos"] = [{"repo": "evo-agents-harness", "branch": None}]

    async def made() -> int:
        async with client.app.state.engine.begin() as conn:
            return await insert_run(conn, values)

    return client.portal.call(made)


def asked(db, run_id: int, category: str, question: str) -> int:
    d = tables.decisions
    values = {"run_id": run_id, "project_id": project_id(db), "category": category, "question": question}
    values["options"] = [{"key": "yes", "label": "Yes"}, {"key": "no", "label": "No"}]
    return sql(db, insert(d).values(values).returning(d.c.id))[0][0]


def waiting(db, kind: str, key: str, reason: str, **values) -> int:
    """A work item of goal G-1 waiting for a person for ``reason``, as the engine leaves one."""
    i = tables.loop_items
    goal = sql(db, select(tables.goals.c.id).where(tables.goals.c.number == 1))[0][0]
    cycle = "business" if kind in ("brainstorm", "use_case") else "development"
    row = {"project_id": project_id(db), "kind": kind, "cycle": cycle, "key": key, "goal_id": goal}
    row |= {"state": "waiting_human", "reason": reason, "engine": "rules/1"}
    return sql(db, insert(i).values(**row, **values).returning(i.c.id))[0][0]


def canvas_of(db) -> tuple[int, dict]:
    k = tables.canvas.canvases
    return sql(db, select(k.c.id, k.c.label).where(k.c.slug == SLUG))[0]


# The three gates


def test_decisions_page_gathers_the_three_gates_with_who_decides_each_and_where(client, hub, hub_db):
    policy(client, hub, {"enabled": True})
    pid, owner = project_id(hub_db), user_id(hub_db, OWNER)
    # gate 1: a goal past its deadline, and the month's review once the hub asked for it
    late = {"statement": "Corner shops reorder twice a week", "metric": METRIC, "deadline": "2026-01-31"}
    assert client.put(f"{GOALS}/G-2", json=late, headers=hub["owner"]).status_code == 200
    first = db_now(hub_db).astimezone(UTC).replace(day=1, hour=10, minute=0, second=0, microsecond=0)
    assert sent(client, first) == {"goal_review": 1}  # owner, the admin and the owner of both goals
    # gate 2: chi moves CL-1 to testing and opens its baseline; an agent proposes a new claim
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "status": "testing", "test_card": CARD})
    assert client.post(BASELINES, json={"claim": "CL-1"}, headers=hub["chi"]).status_code == 201
    canvas_id, label = canvas_of(hub_db)
    p = tables.canvas.proposals
    proposal = {"canvas_id": canvas_id, "kind": "create", "fields": {"cell": "solution", "text": "A reorder button"}}
    proposal |= {"reason": "Shops forget to reorder", "label": label, "actor": "agent", "user_id": owner}
    ((proposal_id,),) = sql(hub_db, insert(p).values(proposal).returning(p.c.id))
    # gate 3: @builder proposes a policy, a repo of the merge queue and the Loop stop on their breakers
    b = tables.builder_proposals
    change = {"kind": "policy", "change": {"set": {"loop.dispatches_per_hour": 3}}, "base_version": 1}
    change |= {"project_id": pid, "summary": "Hand out three runs an hour", "proposed_by": owner}
    ((builder_id,),) = sql(hub_db, insert(b).values(change).returning(b.c.id))
    mb = tables.merge_breakers
    tripped = {"state": "tripped", "cause": "revert", "reason": "two reverts in a row", "tripped_at": func.now()}
    sql(hub_db, insert(mb).values(project_id=pid, repo=REPO, **tripped))
    s = tables.loop_states
    stopped = {"breaker": "tripped", "breaker_reason": "31 work items in an hour", "tripped_at": func.now()}
    sql(hub_db, update(s).where(s.c.project_id == pid).values(stopped))

    found = board(client, hub["reader"])  # a reader reads it all, and decides none of it
    assert (found["project"], found["loop"]) == (PROJECT, True)
    assert found["counts"] == {"goal": 2, "canvas": 2, "policy": 3, "exception": 0, "srs": 0}
    assert [entry["group"] for entry in found["items"]] == ["goal"] * 2 + ["canvas"] * 2 + ["policy"] * 3
    (overdue,) = of_kind(found, "goal_overdue")
    assert overdue["title"].startswith("G-2 is past its deadline 2026-01-31: Corner shops reorder twice a week")
    assert (overdue["decided_by"], overdue["deciders"], overdue["link"]) == (
        "admin",
        [OWNER],
        f"/p/{PROJECT}/goals/G-2",
    )
    assert overdue["since"].startswith("2026-02-01T00:00:00") and overdue["refs"] == {"goal": "G-2"}
    (review,) = of_kind(found, "goal_review")
    assert review["refs"] == {"month": first.strftime("%Y-%m")} and review["deciders"] == [OWNER]
    (baseline,) = of_kind(found, "baseline")
    assert baseline["title"] == f"Baseline 1 of canvas {SLUG}: 1 item to decide"
    assert (baseline["decided_by"], baseline["deciders"]) == ("writer", sorted([OWNER, OTHER])), "chi changed CL-1"
    assert baseline["link"] == f"/p/{PROJECT}/x/canvas/canvases/{SLUG}/baselines/1"
    (claim,) = of_kind(found, "claim_proposal")
    assert claim["title"] == (
        f"Proposal #{proposal_id} on canvas {SLUG}: a new claim in solution, from an agent: A reorder button"
    )
    assert claim["deciders"] == sorted([CHI, OWNER, OTHER]) and claim["refs"] == {
        "proposal": proposal_id,
        "canvas": SLUG,
    }
    assert claim["link"] == f"/p/{PROJECT}/x/canvas/canvases/{SLUG}#proposal-{proposal_id}"
    assert claim["proposal"]["text"] == "A reorder button" and claim["proposal"]["goal"] is None
    (builder,) = of_kind(found, "policy_proposal")
    assert builder["title"] == f"@builder proposal #{builder_id} (policy): Hand out three runs an hour"
    assert (builder["decided_by"], builder["deciders"], builder["link"]) == ("admin", [OWNER], None)
    (merge,) = of_kind(found, "merge_breaker")
    assert merge["title"] == f"The merge queue of {REPO} stopped: two reverts in a row"
    assert merge["link"] == f"/p/{PROJECT}/merge-queue" and merge["refs"] == {"repo": REPO}
    (loop,) = of_kind(found, "loop_breaker")
    assert (
        loop["title"].startswith("The Loop stopped: 31 work items in an hour") and loop["link"] == f"/p/{PROJECT}/loop"
    )

    # a goal written since the review asked for it closes the review; the gates go as they are decided
    assert client.put(f"{GOALS}/G-2", json={**late, "deadline": "2027-01-31"}, headers=hub["owner"]).status_code == 200
    sql(hub_db, update(s).where(s.c.project_id == pid).values(breaker="armed"))
    after = board(client, hub["owner"])
    assert after["counts"]["goal"] == 0 and after["counts"]["policy"] == 2
    assert client.get(BOARD, headers=hub["admin"]).status_code == 403, "a hub admin with no grant"


def test_decisions_page_of_a_project_with_the_loop_off_still_has_its_gates_and_no_chain(client, hub, hub_db):
    found = board(client, hub["chi"])
    assert (found["loop"], found["items"]) == (False, [])
    assert found["counts"] == {"goal": 0, "canvas": 0, "policy": 0, "exception": 0, "srs": 0}
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "status": "testing", "test_card": CARD})
    assert client.post(BASELINES, json={"claim": "CL-1"}, headers=hub["chi"]).status_code == 201
    assert [entry["kind"] for entry in board(client, hub["chi"])["items"]] == ["baseline"]
    assert sent(client, db_now(hub_db).replace(day=1)) == {}, "the Loop's notices go to a project whose Loop is on"


def test_decisions_page_lists_the_proposer_of_a_baseline_among_its_deciders_once_self_approval_is_on(
    client, hub, hub_db
):
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "status": "testing", "test_card": CARD})
    assert client.post(BASELINES, json={"claim": "CL-1"}, headers=hub["chi"]).status_code == 201
    (baseline,) = of_kind(board(client, hub["chi"]), "baseline")
    assert baseline["deciders"] == sorted([OWNER, OTHER]), "off by default: chi changed CL-1 and opened its baseline"
    config = f"/v1/projects/{PROJECT}/plugins/canvas/config"
    written = client.post(config, json={"values": {"self_approval": True}}, headers=hub["owner"])
    assert written.status_code == 200, written.text
    (baseline,) = of_kind(board(client, hub["reader"]), "baseline")
    assert (baseline["decided_by"], baseline["deciders"]) == ("writer", sorted([CHI, OWNER, OTHER]))


# The proposals of a claim: what each proposes, the goal its brainstorm was for, rejected together, stale


PROPOSALS = f"{CANVAS}/proposals"
GOAL_1 = {"statement": "Corner shops reorder every week", "metric": METRIC, "canvas": SLUG}  # the hub fixture's G-1
WEEKLY = {"metric": NUMBER_KEY, "aggregate": "mean", "window_days": 30, "comparator": ">=", "threshold": 0.4}


def goal_id(db, number: int) -> int:
    return sql(db, select(tables.goals.c.id).where(tables.goals.c.number == number))[0][0]


def brainstormed(client, db, goal: int, texts: list[str], *, ledger: bool = True) -> tuple[int, list[int]]:
    """The run the Loop handed out to brainstorm goal G-``goal`` and the new claims of the Solution cell it proposed,
    as its agent puts them: (the run, the proposals). With ``ledger`` the work item is done and only its ledger names
    the run; otherwise the work item holds the run, still running."""
    run_id = a_run(client, db)
    i, e = tables.loop_items, tables.loop_item_events
    row = {"project_id": project_id(db), "kind": "brainstorm", "cycle": "business", "goal_id": goal_id(db, goal)}
    row |= {"key": f"brainstorm:G-{goal}:run{run_id}", "engine": "rules/1"}
    row |= {"state": "done", "finished_at": func.now()} if ledger else {"state": "running", "run_id": run_id}
    ((item_id,),) = sql(db, insert(i).values(row).returning(i.c.id))
    sql(db, insert(e).values(item_id=item_id, kind="dispatched", state="running", run_id=run_id))
    canvas_id, label = canvas_of(db)
    p = tables.canvas.proposals
    made = []
    for text in texts:
        fields = {"cell": "solution", "text": text, "confidence": "low", "test_card": {**CARD, **WEEKLY}}
        values = {"canvas_id": canvas_id, "kind": "create", "fields": fields, "reason": f"Moves the metric of G-{goal}"}
        values |= {"label": label, "actor": "agent", "user_id": user_id(db, OWNER), "run_id": run_id}
        made.append(sql(db, insert(p).values(values).returning(p.c.id))[0][0])
    return run_id, made


def statuses(db, ids: list[int]) -> dict[int, tuple]:
    p, u = tables.canvas.proposals, tables.users
    query = select(p.c.id, p.c.status, u.c.login, p.c.decided_reason).outerjoin(u, u.c.id == p.c.decided_by)
    return {row[0]: tuple(row[1:]) for row in sql(db, query.where(p.c.id.in_(ids)))}


def test_decisions_page_says_what_each_proposal_proposes_with_the_goal_and_run_it_came_of(client, hub, hub_db):
    second = {"statement": "Shops find a product in two taps", "metric": METRIC, "canvas": SLUG}
    assert client.put(f"{GOALS}/G-2", json=second, headers=hub["owner"]).status_code == 200
    first_run, (lists, reminders) = brainstormed(
        client, hub_db, 1, ["Shops reorder from a list of their last orders", "A reminder on the reorder day"]
    )
    second_run, (search,) = brainstormed(client, hub_db, 2, ["Search by the name on the shelf label"], ledger=False)
    canvas_id, label = canvas_of(hub_db)
    p, claims = tables.canvas.proposals, tables.canvas.claims
    ((claim_id,),) = sql(hub_db, select(claims.c.id).where(claims.c.number == 1))
    change = {"canvas_id": canvas_id, "kind": "change", "claim_id": claim_id, "base_rev": 2}
    change |= {"fields": {"confidence": "high"}, "reason": "Two shops asked for it", "label": label, "actor": "agent"}
    ((changed,),) = sql(hub_db, insert(p).values(**change, user_id=user_id(hub_db, OWNER)).returning(p.c.id))

    found = {entry["refs"]["proposal"]: entry for entry in of_kind(board(client, hub["reader"]), "claim_proposal")}
    assert sorted(found) == sorted([lists, reminders, search, changed])
    shown = found[lists]
    assert shown["title"] == (
        f"Proposal #{lists} on canvas {SLUG}: a new claim in solution, from run #{first_run}: Shops reorder from a "
        "list of their last orders"
    )
    assert shown["refs"] == {"proposal": lists, "canvas": SLUG, "run": first_run, "goal": "G-1"}
    assert shown["link"] == f"/p/{PROJECT}/x/canvas/canvases/{SLUG}#proposal-{lists}"
    assert shown["proposal"] == {
        "id": lists,
        "canvas": SLUG,
        "kind": "create",
        "claim": None,
        "cell": "solution",
        "text": "Shops reorder from a list of their last orders",
        "fields": ["cell", "confidence", "test_card", "text"],
        "status": None,
        "test": f"{NUMBER_KEY} mean over 30 days >= 0.4, by 2026-12-01",
        "reason": "Moves the metric of G-1",
        "synthetic": False,
        "run": first_run,
        "goal": {"code": "G-1", "statement": GOAL_1["statement"], "metric": f"{NUMBER_KEY} last over 90 days >= 0.5"},
    }
    assert found[search]["proposal"]["goal"]["code"] == "G-2", "the work item holds the run it handed out"
    assert found[search]["proposal"]["run"] == second_run
    other = found[changed]["proposal"]  # a change reads as its claim does now
    assert (other["kind"], other["claim"], other["cell"], other["fields"]) == (
        "change",
        "CL-1",
        "solution",
        ["confidence"],
    )
    assert other["text"] == "A reorder list built from the shop's last orders" and other["goal"] is None
    assert found[changed]["refs"] == {"proposal": changed, "canvas": SLUG}


def test_decisions_page_proposals_rejected_together_with_one_reason(client, hub, hub_db):
    _, made = brainstormed(client, hub_db, 1, ["A list of the last orders", "A weekly reminder", "A shared basket"])
    first, second, third = made
    p = tables.canvas.proposals
    sql(hub_db, update(p).where(p.c.id == third).values(status="stale"))
    body = {"proposals": made, "reason": "G-1 moved to search; these answer the old metric"}
    refused = client.post(f"{PROPOSALS}/reject", json=body, headers=hub["reader"])
    assert refused.status_code == 403, "a reader decides nothing"
    assert client.post(f"{PROPOSALS}/reject", json={**body, "reason": ""}, headers=hub["chi"]).status_code == 422
    foreign = client.post(f"{PROPOSALS}/reject", json={**body, "proposals": [first, 99_999]}, headers=hub["chi"])
    assert foreign.status_code == 404 and "has no proposal 99999" in foreign.text
    assert statuses(hub_db, [first])[first][0] == "open", "a 404 rejects nothing"

    done = client.post(f"{PROPOSALS}/reject", json={**body, "proposals": [*made, first]}, headers=hub["chi"])
    assert done.status_code == 200, done.text
    assert done.json() == {"rejected": [first, second], "left": [{"id": third, "status": "stale"}]}
    assert statuses(hub_db, made) == {
        first: ("rejected", CHI, body["reason"]),
        second: ("rejected", CHI, body["reason"]),
        third: ("stale", None, None),
    }
    assert of_kind(board(client, hub["chi"]), "claim_proposal") == []
    again = client.post(f"{PROPOSALS}/reject", json=body, headers=hub["chi"])
    assert again.json()["rejected"] == [] and len(again.json()["left"]) == 3


def test_a_goal_whose_statement_or_metric_changes_makes_the_open_proposals_of_its_brainstorms_stale(
    client, hub, hub_db
):
    second = {"statement": "Shops find a product in two taps", "metric": METRIC, "canvas": SLUG}
    assert client.put(f"{GOALS}/G-2", json=second, headers=hub["owner"]).status_code == 200
    _, (older, rejected) = brainstormed(client, hub_db, 1, ["A list of the last orders", "A weekly reminder"])
    _, (current,) = brainstormed(client, hub_db, 1, ["Reorder in one tap"], ledger=False)
    _, (other_goal,) = brainstormed(client, hub_db, 2, ["Search by the shelf label"])
    body = {"proposals": [rejected], "reason": "Not now"}
    assert client.post(f"{PROPOSALS}/reject", json=body, headers=hub["chi"]).status_code == 200
    ids = [older, rejected, current, other_goal]

    later = {**GOAL_1, "deadline": "2027-03-31"}  # a new deadline asks the same of the canvas
    assert client.put(f"{GOALS}/G-1", json=later, headers=hub["owner"]).json()["revision"] == 2
    assert [statuses(hub_db, ids)[number][0] for number in ids] == ["open", "rejected", "open", "open"]

    hit = {**later, "metric": {**METRIC, "aggregate": "mean", "target": 0.66}}
    assert client.put(f"{GOALS}/G-1", json=hit, headers=hub["owner"]).json()["revision"] == 3
    assert statuses(hub_db, ids) == {
        older: ("stale", None, None),
        rejected: ("rejected", CHI, "Not now"),
        current: ("stale", None, None),
        other_goal: ("open", None, None),
    }
    assert [entry["refs"]["proposal"] for entry in of_kind(board(client, hub["chi"]), "claim_proposal")] == [other_goal]
    accept = client.post(f"{PROPOSALS}/{current}/accept", headers=hub["chi"])
    assert accept.status_code == 409, "a stale proposal is accepted no more"

    _, (fresh,) = brainstormed(client, hub_db, 1, ["Reorder from the receipt"])  # the Loop brainstorms G-1 again
    worded = {**second, "statement": "Shops find a product in one tap"}
    assert client.put(f"{GOALS}/G-2", json=worded, headers=hub["owner"]).status_code == 200
    assert statuses(hub_db, [other_goal, fresh]) == {
        other_goal: ("stale", None, None),
        fresh: ("open", None, None),
    }


# What the Loop's chain waits on


def settle_chat(db, version_id: int, people: list[str]) -> int:
    """The settle chat of version ``version_id``, ``people`` in it."""
    t, s = tables, tables.srs
    profile = {"project_id": project_id(db), "handle": "ba-settle", "name": "BA", "color": "#336699"}
    profile |= {"permissions": {}, "created_by": user_id(db, OWNER)}
    ((profile_id,),) = sql(db, insert(t.agent_profiles).values(profile).returning(t.agent_profiles.c.id))
    chat = {"title": "Conflicts of demo-shop v1", "created_by": user_id(db, OWNER), "kind": "settle"}
    ((chat_id,),) = sql(db, insert(t.chats).values(**chat, direct_profile_id=profile_id).returning(t.chats.c.id))
    message = {"chat_id": chat_id, "seq": 1, "author_user_id": user_id(db, OWNER), "body": "Two limits", "label": []}
    ((root,),) = sql(db, insert(t.chat_messages).values(message).returning(t.chat_messages.c.id))
    sql(db, insert(s.settle_chats).values(version_id=version_id, chat_id=chat_id, root_message_id=root))
    members = [{"chat_id": chat_id, "user_id": user_id(db, login)} for login in people]
    sql(db, insert(t.chat_members).values(members))
    return chat_id


def test_decisions_page_lists_what_the_loops_chain_waits_on_and_who_answers_it(client, hub, hub_db):
    policy(client, hub, {"enabled": True})
    pid = project_id(hub_db)
    # the third failure of a brainstorm: a decision of category loop on its last run, asked of owner
    failed = a_run(client, hub_db)
    loop_decision = asked(hub_db, failed, "loop", "Work item failed 3 times: run it again, drop it, or leave it?")
    brainstorm = waiting(
        hub_db, "brainstorm", "brainstorm:G-1:r1", "loop_decision", detail={"decision": {"id": loop_decision}}
    )
    # a build whose author run asks a decision of other, and one whose author run asks in its chat
    deciding = a_run(client, hub_db, OTHER)
    run_decision = asked(hub_db, deciding, "architecture", "One table or two?")
    build = waiting(
        hub_db,
        "build",
        "build:srs:demo-shop:1:UC ORD-1",
        "run_decision",
        run_id=deciding,
        detail={"asked_in": deciding},
    )
    chatting = a_run(client, hub_db)
    question = waiting(
        hub_db, "build", "build:srs:demo-shop:1:UC ORD-2", "author_question", detail={"asked_in": chatting}
    )
    # a deliver whose merge request is blocked, one whose repo is merged by hand, and an audit sample not answered
    push_plan(client, hub, "reorder-button", (REPO,))
    q = tables.merge_queue
    request = {"project_id": pid, "repo": REPO, "iid": 7, "source": "plan", "target_branch": "main"}
    request |= {
        "source_branch": "evo/reorder-button",
        "plan_id": "reorder-button",
        "state": "blocked",
        "reason": "gate 3 (score hacking): the diff shows signs of score hacking (ci_changed), first at .gitlab-ci.yml",
    }
    ((merge_id,),) = sql(hub_db, insert(q).values(request).returning(q.c.id))
    held = waiting(
        hub_db,
        "deliver",
        "deliver:reorder-button",
        "merge_blocked",
        plan_id="reorder-button",
        detail={"merges": [merge_id]},
    )
    by_hand = waiting(
        hub_db, "deliver", "deliver:reorder-list", "merge_manual", plan_id="reorder-button", detail={"by_hand": [REPO]}
    )
    m = tables.merge_samples
    ((sample_id,),) = sql(hub_db, insert(m).values(project_id=pid, queue_id=merge_id).returning(m.c.id))
    # chi's version 1 of demo-shop under review, drafted for one use case and held by a settle chat for another
    submitted(client, hub["chi"], 1)
    v = tables.srs.versions
    ((version_id,),) = sql(hub_db, select(v.c.id).where(v.c.number == 1))
    srs = {"document": "demo-shop", "version": 1}
    review = waiting(
        hub_db, "use_case", "use_case:northwind-pantry:CL-1:b1", "srs_review", use_case="UC ORD-1", detail={"srs": srs}
    )
    chat_id = settle_chat(hub_db, version_id, [CHI, OTHER])
    settle = {"srs": {**srs, "findings": [41, 42], "chat": f"/chats/{chat_id}?message=1"}}
    finding = waiting(
        hub_db, "use_case", "use_case:northwind-pantry:CL-1:b2", "srs_finding", use_case="UC ORD-2", detail=settle
    )

    found = board(client, hub["reader"])
    assert found["counts"] == {"goal": 0, "canvas": 0, "policy": 0, "exception": 6, "srs": 2}
    (decision,) = of_kind(found, "loop_decision")
    assert decision["title"].startswith(f"Decision #{loop_decision} on run #{failed}: Work item failed 3 times")
    assert (decision["decided_by"], decision["deciders"]) == ("run_owner", [OWNER])
    assert (decision["link"], decision["work_item"]) == (f"/inbox?decision={loop_decision}", brainstorm)
    assert decision["refs"] == {"decision": loop_decision, "run": failed, "goal": "G-1"}
    (other,) = of_kind(found, "run_decision")
    assert (
        other["title"] == f"Decision #{run_decision} of run #{deciding}, work item #{build} (build): One table or two?"
    )
    assert (other["deciders"], other["work_item"]) == ([OTHER], build)
    (chat,) = of_kind(found, "author_question")
    assert chat["link"] == f"/p/{PROJECT}/runs/{chatting}?tab=chat" and chat["work_item"] == question
    (blocked,) = of_kind(found, "merge_held")
    assert blocked["title"] == (  # a hold for a person says how an admin passes it
        f"Merge request !7 of {REPO} blocked at gate 3 (score hacking): the diff shows signs of score hacking "
        f"(ci_changed), firs...; an admin passes it: `evo-agents hub merge pass {REPO} 7 --reason ...`"
    )
    assert blocked["refs"] == {"merge_request": merge_id, "repo": REPO, "plan": "reorder-button", "goal": "G-1"}
    assert (blocked["decided_by"], blocked["work_item"]) == ("admin", held)
    (manual,) = of_kind(found, "merge_manual")
    assert manual["title"].startswith(f"Plan reorder-button: merge {REPO} by hand") and manual["work_item"] == by_hand
    assert manual["link"] == f"/p/{PROJECT}/plans/reorder-button" and manual["decided_by"] == "writer"
    (sample,) = of_kind(found, "merge_sample")
    assert sample["title"] == f"Audit sample #{sample_id}: was merge request !7 of {REPO} right?"
    (in_review,) = of_kind(found, "srs_review")
    assert in_review["title"] == "SRS demo-shop v1 (UC ORD-1) waits for its review" and in_review["work_item"] == review
    assert (in_review["decided_by"], in_review["deciders"]) == ("srs_reviewer", sorted([OWNER, OTHER])), "chi wrote it"
    assert in_review["link"] == f"/p/{PROJECT}/x/srs/documents/demo-shop"
    (settling,) = of_kind(found, "srs_finding")
    assert settling["title"] == "SRS demo-shop v1 (UC ORD-2): 2 finding(s) of level decide to settle in its chat"
    assert (settling["decided_by"], settling["deciders"]) == ("chat_member", sorted([CHI, OTHER]))
    assert (settling["link"], settling["work_item"], settling["refs"]["chat"]) == (
        settle["srs"]["chat"],
        finding,
        chat_id,
    )

    # answered or moved on, they leave the board
    d = tables.decisions
    sql(hub_db, update(d).where(d.c.id == run_decision).values(state="cancelled"))
    i = tables.loop_items
    sql(hub_db, update(i).where(i.c.id.in_([held, by_hand])).values(state="watching", reason="merge_queue"))
    after = board(client, hub["reader"])
    assert after["counts"]["exception"] == 3 and not of_kind(after, "run_decision") and not of_kind(after, "merge_held")


def test_decisions_page_shows_a_decision_only_to_who_reads_its_run(client, hub, hub_db):
    policy(client, hub, {"enabled": True})
    push_plan(client, hub, "reorder-button", (REPO,))
    run_id = a_run(client, hub_db, plan_id="reorder-button")
    decision = asked(hub_db, run_id, "loop", "Run it again?")
    waiting(hub_db, "deliver", "deliver:reorder-button", "loop_decision", detail={"decision": {"id": decision}})
    granted(client, hub, READER, {"role": "reader", "max_level": "public"})  # below the plan's label, internal
    assert board(client, hub["reader"])["counts"]["exception"] == 0, "the reader reads no run of that plan"
    assert board(client, hub["owner"])["counts"]["exception"] == 1


# The notices of the Loop's clock


def notices_of(db, kind: str) -> list[tuple]:
    n, u = tables.notifications, tables.users
    query = (
        select(u.c.login, n.c.title, n.c.body, n.c.details, n.c.link)
        .join_from(n, u, u.c.id == n.c.user_id)
        .where(n.c.notice_kind == kind)
        .order_by(n.c.id)
    )
    return sql(db, query)


def telegram(db, login: str) -> int:
    ch = tables.notification_channels
    return sql(db, insert(ch).values(user_id=user_id(db, login), kind="telegram", config={}).returning(ch.c.id))[0][0]


def channels_of(db, kind: str) -> list[tuple]:
    n, nd = tables.notifications, tables.notification_deliveries
    query = select(nd.c.channel_id).join_from(nd, n, n.c.id == nd.c.notification_id).where(n.c.notice_kind == kind)
    return sql(db, query.order_by(nd.c.channel_id.nulls_first()))


def test_decisions_page_goal_review_goes_out_on_the_first_of_a_month_once_on_the_web_and_telegram(client, hub, hub_db):
    goal = {"statement": "Corner shops reorder every week", "metric": METRIC, "canvas": SLUG, "owner": CHI}
    assert client.put(f"{GOALS}/G-1", json=goal, headers=hub["owner"]).status_code == 200
    channel = telegram(hub_db, CHI)
    first = datetime(2026, 11, 1, 10, 30, tzinfo=UTC)
    assert sent(client, first) == {}, "the Loop is off"
    policy(client, hub, {"enabled": True})
    assert sent(client, first - timedelta(hours=11)) == {}, "the last day of October"
    assert sent(client, first) == {"goal_review": 2}  # owner, the admin, and chi, the goal's owner
    assert sent(client, first + timedelta(days=3)) == {}, "once a month"
    rows = notices_of(hub_db, "goal_review")
    assert [row[0] for row in rows] == [OWNER, CHI]
    login, title, body, details, link = rows[1]
    assert title == f"Review the goals of {PROJECT} for 2026-11: 1 active goal" and link == DECIDE
    assert details == {"month": "2026-11", "goals": 1, "past_deadline": 0}
    line = f"- G-1 (rank 1): Corner shops reorder every week; {NUMBER_KEY} last over 90 days >= 0.5, no value yet; "
    assert line + "deadline none" in body
    assert channels_of(hub_db, "goal_review") == [(None,), (None,), (channel,)], "the web twice, chi's Telegram"
    assert sent(client, datetime(2026, 12, 8, 10, 30, tzinfo=UTC)) == {}, "past the first week of December"
    assert sent(client, datetime(2026, 12, 7, 10, 30, tzinfo=UTC)) == {"goal_review": 2}, "a hub that was down"


def test_brief_of_the_loop_goes_to_goal_owners_at_seven_when_the_curator_sends_none(client, hub, hub_db):
    policy(client, hub, {"enabled": True, "timezone": "Asia/Ho_Chi_Minh"})
    seven = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)  # 07:00 in Ho Chi Minh City
    brainstorm = waiting(hub_db, "brainstorm", "brainstorm:G-1:r1", "loop_decision")
    review = waiting(hub_db, "use_case", "use_case:northwind-pantry:CL-1:b1", "srs_review")
    e = tables.loop_item_events
    ledger = [
        {"item_id": brainstorm, "kind": "dispatched", "state": "running", "at": seven - timedelta(hours=3)},
        {"item_id": review, "kind": "dispatched", "state": "running", "at": seven - timedelta(hours=30)},  # too old
        {"item_id": review, "kind": "done", "state": "done", "at": seven - timedelta(hours=1)},
    ]
    sql(hub_db, insert(e).values(ledger))
    assert sent(client, seven - timedelta(minutes=1)) == {}
    assert sent(client, seven) == {"loop_brief": 1}
    assert sent(client, seven + timedelta(hours=2)) == {}, "once a day"
    assert sent(client, seven + timedelta(hours=27)) == {}, "past 10:00 the next day: the grace of a brief"
    ((login, title, body, details, link),) = notices_of(hub_db, "loop_brief")
    assert (login, link) == (OWNER, DECIDE)
    assert title == f"Loop of {PROJECT}: 1 handed out, 1 done, 2 waiting for a person"
    assert details == {"day": "2026-10-20", "handed_out": 1, "done": 1, "waiting": 2}
    assert body.startswith(
        "**Loop**: 1 run handed out and 1 work item done in the last 24 hours; 2 waiting for a person "
        "(1 a decision after the third failure; 1 an SRS version to review)."
    )
    assert f"- work item #{brainstorm} (brainstorm): a decision after the third failure" in body
    assert body.endswith(f"Decide on the decision board: {DECIDE}.")
    policy(client, hub, {"enabled": False})
    assert sent(client, seven + timedelta(days=1)) == {}, "no brief of the Loop once it is off"


def test_decisions_page_schema_0094_takes_the_loops_notices_and_going_back_deletes_them(client, hub, hub_db):
    policy(client, hub, {"enabled": True})
    assert sent(client, datetime(2026, 11, 1, 10, 30, tzinfo=UTC)) == {"goal_review": 1}
    assert len(notices_of(hub_db, "goal_review")) == 1
    n = tables.notifications
    notice = {"user_id": user_id(hub_db, OWNER), "kind": "notice", "notice_kind": "loop_brief", "title": "Loop"}
    sql(hub_db, insert(n).values({**notice, "notice_kind": "pm_daily", "title": "Digest"}))
    move_to(hub_db, "0095", down=True)
    assert notices_of(hub_db, "goal_review") == []
    assert len(notices_of(hub_db, "pm_daily")) == 1, "going back keeps the notices of the plugin pm (0086)"
    with pytest.raises(psycopg.errors.CheckViolation, match="notifications_notice_kind_check"):
        sql(hub_db, insert(n).values(notice))
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0096") :]
    sql(hub_db, insert(n).values(notice))
