"""The tasks of an approved version (``evo_agents.core_plugins.srs.auto_tasks``), as criterion a8 of plan
srs-conflicts-and-tasks names it.

When a version is approved in a project that enables the plugin tasks, the hub compares it with the version approved
before: each use case new in it gets a frame task linked to it (its external_key the document, the version and the
code), the open tasks of a use case that changed (itself or a business rule under it) get a comment of the approver,
and the open tasks of a use case it dropped move to the first status of group cancelled, their history naming the hub
and why. Then @ba gets a task run that splits each open frame task with no child into child tasks, made with
tasks__create for the approver: the tool takes ``parent``, answers that run as it answers a chat run, refuses a child
whose title the frame task has already, and still refuses any other task run. A second call of ``approved`` for the
same version, as a replayed event would make, adds no task, comment or run.

The stack is ``tests.hub.test_ba_agent``'s: owner runs @ba on the worker studio, chi writes the document, someone-else
approves it.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.core_plugins.srs import auto_tasks
from evo_agents.hub import tables
from tests.hub.live import sql
from tests.hub.test_ba_agent import (  # noqa: F401 (app_key, client, hub, members and srs_core are fixtures)
    CHI,
    OTHER,
    OWNER,
    READ_ONLY,
    SRS,
    agent_of,
    app_key,
    client,
    enable,
    enable_ba,
    granted,
    hub,
    imported,
    item,
    members,
    owners_worker,
    review,
    running,
    runs_of,
    saved,
    srs_core,
    submitted,
    tool,
)
from tests.hub.test_runs import PROJECT, moved

TASKS = f"/v1/x/tasks/{PROJECT}"
APPROVALS = tables.srs.approval_tasks
SPLIT_V1 = "@ba splits the tasks of demo-shop v1"


def tasks(client, headers) -> list[dict]:
    listed = client.get(f"{TASKS}/tasks", headers=headers)
    assert listed.status_code == 200, listed.text
    return listed.json()


def task(client, headers, ref: str) -> dict:
    found = client.get(f"{TASKS}/tasks/{ref}", headers=headers)
    assert found.status_code == 200, found.text
    return found.json()


def reviewed(client, studio: dict) -> None:
    """The worker claims @ba's review of a version, which finds nothing."""
    spec = running(client, studio, "task")
    assert "\nReview version " in spec["prompt"], spec["prompt"]
    moved(client, studio, spec["id"], "verifying", "done", summary="Nothing found.")


def approved(client, hub, studio: dict, number: int, submitter: str = "chi") -> None:
    submitted(client, hub[submitter], number)
    reviewed(client, studio)
    answer = review(client, hub["other"], number)
    assert answer.status_code == 200 and answer.json()["state"] == "approved", answer.text


def called_again(client, hub_db, number: int, previous: int | None) -> auto_tasks.Outcome:
    """``auto_tasks.approved`` once more for version ``number`` of demo-shop, approved by someone-else, as a replay of
    the event srs.version.approved would call it."""
    d, u = tables.srs.documents, tables.users
    ((document_id,),) = sql(hub_db, select(d.c.id).where(d.c.slug == "demo-shop"))
    ((approver_id,),) = sql(hub_db, select(u.c.id).where(u.c.login == OTHER))

    async def work():
        async with client.app.state.engine.begin() as conn:
            return await auto_tasks.approved(conn, document_id, number, previous, approver_id)

    return client.portal.call(work)


def counts(hub_db) -> tuple[int, int, int, int]:
    """(tasks, comments, history rows, task runs)."""
    t, r = tables.tasks, tables.runs
    return (
        len(sql(hub_db, select(t.items.c.id))),
        len(sql(hub_db, select(t.comments.c.id))),
        len(sql(hub_db, select(t.events.c.id))),
        len(sql(hub_db, select(r.c.id).where(r.c.kind == "task"))),
    )


def approval_rows(hub_db) -> list[tuple]:
    a, v, u = APPROVALS, tables.srs.versions, tables.users
    query = (
        select(v.c.number, a.c.previous_version, u.c.login, a.c.split_run_id, a.c.outcome)
        .join_from(a, v, v.c.id == a.c.version_id)
        .join(u, u.c.id == a.c.approved_by)
        .order_by(a.c.id)
    )
    return sql(hub_db, query)


def first_approval(client, hub) -> dict:
    """tasks and @ba on, the document imported by chi, version 1 reviewed and approved by someone-else; the worker
    studio, and the split run it claimed next."""
    enable(client, hub["owner"], "tasks", ["plans:read"])
    enable_ba(client, hub)
    studio = owners_worker(client, hub)
    imported(client, hub["chi"])
    approved(client, hub, studio, 1)
    return {"studio": studio, "split": running(client, studio, "task")}


def test_srs_auto_tasks_first_approval_makes_a_frame_task_of_each_use_case_and_hands_ba_the_split(client, hub, hub_db):
    done = first_approval(client, hub)
    made = tasks(client, hub["reader"])
    assert [(t["ref"], t["title"], t["type"], t["status"], t["codes"], t["parent"], t["created_by"]) for t in made] == [
        ("T-1", "UC ORD-1: Place an order", "story", "backlog", ["UC ORD-1"], None, OTHER),
        ("T-2", "UC ORD-2: Pay for an order", "story", "backlog", ["UC ORD-2"], None, OTHER),
        ("T-3", "UC RET-1: Return a book", "story", "backlog", ["UC RET-1"], None, OTHER),
    ]
    t = tables.tasks.items
    keys = sql(hub_db, select(t.c.external_key).order_by(t.c.number))
    assert keys == [("srs:demo-shop:1:UC ORD-1",), ("srs:demo-shop:1:UC ORD-2",), ("srs:demo-shop:1:UC RET-1",)]
    frame = task(client, hub["reader"], "T-1")
    assert frame["external_ref"] == {"source": "srs", "document": "demo-shop", "version": 1, "use_case": "UC ORD-1"}
    assert f"/p/{PROJECT}/x/srs/documents/demo-shop/items/UC%20ORD-1" in frame["description_md"]
    cause = f"use case UC ORD-1 is new in version 1 of demo-shop, which {OTHER} approved"
    assert [(e["actor"], e["field"], e["to_value"], e["cause"]) for e in frame["events"]] == [
        (None, "created", "T-1", cause),
        (None, "link", "srs_item UC ORD-1", cause),
    ]
    # the split run: @ba on owner's worker, the frame tasks listed, the skill named
    split = done["split"]
    assert [row[1:] for row in runs_of(hub_db, "task")] == [
        ("ba", OWNER, "machine", "@ba reviews demo-shop v1"),
        ("ba", OWNER, "machine", SPLIT_V1),
    ]
    for said in (
        f"Version 1 of it was approved by {OTHER}",
        "- T-1: UC ORD-1: Place an order (use case UC ORD-1)",
        "- T-3: UC RET-1: Return a book (use case UC RET-1)",
        "Follow the skill srs-tasks of your pack",
        "parent: the frame task's number (such as T-1)",
    ):
        assert said in split["prompt"], said
    outcome = {"created": [1, 2, 3], "commented": [], "cancelled": [], "no_split": None}
    assert approval_rows(hub_db) == [(1, None, OTHER, split["id"], outcome)]
    # called again, as a replayed event would: no task, comment, history row or run more
    before = counts(hub_db)
    again = called_again(client, hub_db, 1, None)
    assert (again.again, again.created, again.split_run_id) == (True, [1, 2, 3], split["id"])
    assert counts(hub_db) == before == (3, 0, 6, 2)
    assert approval_rows(hub_db) == [(1, None, OTHER, split["id"], outcome)]


def test_srs_auto_tasks_split_run_makes_child_tasks_for_the_approver_and_no_twin(client, hub, hub_db):
    done = first_approval(client, hub)
    agent = agent_of(done["studio"], done["split"]["id"])
    child = {"title": "Cart: refuse more than 50 lines", "description_md": "Covers BR ORD1-1.", "parent": "T-1"}
    text, error = tool(client, agent, "tasks__create", **child, links=["BR ORD1-1"])
    assert not error, text
    assert text.startswith(f"Task T-4 created for {OTHER}, linked to BR ORD1-1, UC ORD-1, a child of T-1: Cart"), text
    made = task(client, hub["reader"], "T-4")
    assert (made["parent"], made["created_by"], made["codes"]) == ("T-1", OTHER, ["BR ORD1-1", "UC ORD-1"])
    # a twin title under the same frame task, a call with no parent, a parent that is no frame task: refused
    twin = {**child, "title": "  cart: REFUSE more than   50 lines"}
    text, error = tool(client, agent, "tasks__create", **twin)
    assert error and "T-1 has a child task T-4 titled" in text and "nothing was written" in text, text
    text, error = tool(client, agent, "tasks__create", title="Stock: hold for 15 minutes")
    assert error and "name the frame task in parent" in text, text
    text, error = tool(client, agent, "tasks__create", title="Stock: hold for 15 minutes", parent="T-4")
    assert error and "T-4 is no frame task of an approved SRS version" in text, text
    text, error = tool(client, agent, "tasks__create", title="Stock: hold for 15 minutes", parent="T-1")
    assert not error and "Task T-5 created" in text and "a child of T-1" in text, text
    # the run acts for the approver while they hold writer, and any other task run is refused as before
    granted(client, hub, OTHER, READ_ONLY)
    text, error = tool(client, agent, "tasks__create", title="Checkout screen", parent="T-1")
    assert error and "needs the writer role" in text, text
    saved(client, hub["chi"], 1, {"key": item(client, hub["chi"], "BR ORD1-1")["key"], "body": "- At most 40 lines."})
    submitted(client, hub["chi"], 2)
    moved(client, done["studio"], done["split"]["id"], "verifying", "done", summary="Two child tasks.")
    review_run = running(client, done["studio"], "task")
    assert "\nReview version 2 of" in review_run["prompt"], review_run["prompt"]
    text, error = tool(client, agent_of(done["studio"], review_run["id"]), "tasks__create", title="x", parent="T-1")
    assert error and "the agent of a task run creates no task" in text, text
    assert [t["ref"] for t in tasks(client, hub["reader"])] == ["T-1", "T-2", "T-3", "T-4", "T-5"]


def test_srs_auto_tasks_next_approval_frames_new_comments_changed_and_cancels_dropped(client, hub, hub_db):
    done = first_approval(client, hub)
    studio, split = done["studio"], done["split"]
    child = {"title": "Cart: refuse more than 50 lines", "parent": "T-1"}
    assert not tool(client, agent_of(studio, split["id"]), "tasks__create", **child)[1]  # T-4, a child of T-1
    moved(client, studio, split["id"], "verifying", "done", summary="One child task.")
    finished = client.patch(f"{TASKS}/tasks/T-2", json={"status": "done"}, headers=hub["chi"])
    assert finished.status_code == 200, finished.text
    # version 2: a business rule of UC ORD-1 and one of UC ORD-2 change, UC ORD-3 is new; version 3 drops UC RET-1
    order, cart, retries = (item(client, hub["chi"], code) for code in ("UC ORD-1", "BR ORD1-1", "BR ORD2-1"))
    body = "| Item | Content |\n|---|---|\n| Actor | Customer |\n| Trigger | The customer presses Cancel |"
    new = {"kind": "use_case", "code": "UC ORD-3", "title": "Cancel an order", "parent": order["parent"], "body": body}
    saved(
        client,
        hub["chi"],
        1,
        {"key": cart["key"], "body": "- A cart holds at most 40 lines."},
        {"key": retries["key"], "body": "- A declined payment is tried 2 times."},
        {"new": new},
    )
    retired = client.post(f"{SRS}/documents/demo-shop/retire", json={"codes": ["UC RET-1"]}, headers=hub["owner"])
    assert retired.status_code == 200 and retired.json()["version"] == 3, retired.text
    approved(client, hub, studio, 3)
    listed = {t["ref"]: t for t in tasks(client, hub["reader"])}
    assert (listed["T-5"]["title"], listed["T-5"]["codes"], listed["T-5"]["status"]) == (
        "UC ORD-3: Cancel an order",
        ["UC ORD-3"],
        "backlog",
    )
    t = tables.tasks.items
    assert sql(hub_db, select(t.c.external_key).where(t.c.number == 5)) == [("srs:demo-shop:3:UC ORD-3",)]
    # the open tasks of UC ORD-1 get the approver's comment; T-2 is done, so UC ORD-2's change leaves it be
    said = f"Use case UC ORD-1 changed in version 3 of demo-shop, which {OTHER} approved"
    compare = f"/p/{PROJECT}/x/srs/documents/demo-shop/compare?from=1&to=3"
    for ref in ("T-1", "T-4"):
        ((comment,),) = [task(client, hub["reader"], ref)["comments"]]
        assert comment["author"] == OTHER and comment["body"].startswith(said) and compare in comment["body"]
    assert task(client, hub["reader"], "T-2")["comments"] == []
    # the open task of UC RET-1, dropped, is cancelled; its history names the hub and why
    dropped = task(client, hub["reader"], "T-3")
    assert (dropped["status"], dropped["status_group"], dropped["comments"]) == ("cancelled", "cancelled", [])
    cause = f"use case UC RET-1 was dropped in version 3 of demo-shop, which {OTHER} approved"
    assert (None, "status", "backlog", "cancelled", cause) in [
        (e["actor"], e["field"], e["from_value"], e["to_value"], e["cause"]) for e in dropped["events"]
    ]
    # the split run lists only the frame task that waits: T-1 has a child, T-2 is done, T-3 cancelled
    ((split_v3, *_),) = [row for row in runs_of(hub_db, "task") if row[4] == "@ba splits the tasks of demo-shop v3"]
    prompt = running(client, studio, "task")
    assert prompt["id"] == split_v3 and "- T-5: UC ORD-3: Cancel an order (use case UC ORD-3)" in prompt["prompt"]
    assert "- T-1:" not in prompt["prompt"] and "- T-2:" not in prompt["prompt"] and "- T-3:" not in prompt["prompt"]
    outcome = {"created": [5], "commented": [1, 4], "cancelled": [3], "no_split": None}
    assert approval_rows(hub_db)[1] == (3, 1, OTHER, split_v3, outcome)
    # a replay of version 3 does nothing more
    before = counts(hub_db)
    again = called_again(client, hub_db, 3, 1)
    assert (again.again, again.created, again.commented, again.cancelled) == (True, [5], [1, 4], [3])
    assert counts(hub_db) == before


def test_srs_auto_tasks_need_the_plugin_tasks_and_make_frames_without_ba(client, hub, hub_db):
    imported(client, hub["chi"])
    assert submitted(client, hub["chi"], 1)["state"] == "in_review"  # @ba is off: no review run holds approval
    assert review(client, hub["other"], 1).json()["state"] == "approved"
    off = client.get(f"{TASKS}/tasks", headers=hub["reader"])
    assert off.status_code == 404, "the project does not enable tasks: nothing is made"
    assert approval_rows(hub_db) == [] and runs_of(hub_db, "task") == []
    assert called_again(client, hub_db, 1, None) is None
    # with tasks on and @ba off, the next approval makes the frame of its new use case, and no split run
    enable(client, hub["owner"], "tasks", ["plans:read"])
    order = item(client, hub["chi"], "UC ORD-1")
    body = "| Item | Content |\n|---|---|\n| Actor | Customer |"
    new = {"kind": "use_case", "code": "UC ORD-3", "title": "Cancel an order", "parent": order["parent"], "body": body}
    saved(client, hub["chi"], 1, {"new": new})
    submitted(client, hub["chi"], 2)
    assert review(client, hub["other"], 2).json()["state"] == "approved"
    assert [(t["ref"], t["title"]) for t in tasks(client, hub["reader"])] == [("T-1", "UC ORD-3: Cancel an order")]
    ((number, previous, login, run_id, outcome),) = approval_rows(hub_db)
    assert (number, previous, login, run_id, outcome["created"]) == (2, 1, OTHER, None, [1])
    assert "agent type ba of plugin srs is not enabled" in outcome["no_split"], outcome
