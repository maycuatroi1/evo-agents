"""The core plugin tasks on the hub (``evo_agents.core_plugins.tasks``), as criterion a3 of plan srs-tasks-plugins
names it: a project that enabled it keeps tasks. A writer creates, changes and assigns a task with a title, a
description, a status from the project's list, assignees and the BA, QA and Dev slots, a due date, a sprint and a
priority; links it to an SRS item by code (while the project enables srs), a plan step, a run and a pull request; a
plan step turning done shows on the task at once, the hub's plan.step event writes it into the task's history once,
and on_steps_done moves a task whose steps are all done. evo-tasks/1 JSON imports once, linking use cases by code.
Readers read, writers write, a task carries the project's label and reads follow it; people are members with a
grant. A project that did not enable the plugin answers 404.
"""

from __future__ import annotations

import json
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from evo_agents.core_plugins.tasks.events import steps_done
from evo_agents.hub import plugin_calls, plugin_events, tables
from evo_agents.hub.server.app import create_app
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from tests.hub import live
from tests.hub.live import ADMIN, bearer, sql
from tests.hub.test_chats import OPEN_SINKS
from tests.hub.test_plans import registration
from tests.hub.test_runs import PLAN, PROJECT, dispatched, plan_body

CORE = {
    "srs": EntryPoint("srs", "evo_agents.core_plugins.srs:MANIFEST", ENTRY_POINT_GROUP),
    "tasks": EntryPoint("tasks", "evo_agents.core_plugins.tasks:MANIFEST", ENTRY_POINT_GROUP),
}
SAMPLE_SRS = Path(__file__).parent / "fixtures" / "srs" / "demo-shop-srs.md"
SAMPLE_TASKS = Path(__file__).parent / "fixtures" / "tasks" / "demo-shop-tasks.json"
BASE = f"/v1/x/tasks/{PROJECT}"
MEMBERS = {  # login -> (github id, role, max level)
    "owner": (601, "admin", "internal"),
    "alice": (602, "writer", "internal"),
    "bao": (603, "writer", "internal"),
    "eve": (605, "reader", "internal"),
    "outsider": (606, "reader", "public"),
}


@pytest.fixture
def tasks_core(monkeypatch):
    """The entry points of the core plugins with srs's and tasks's, as the package installed declares them."""
    points = [point for point in core.entry_points(group=ENTRY_POINT_GROUP) if point.name not in CORE]
    points += list(CORE.values())
    monkeypatch.setattr(core, "entry_points", lambda *, group: list(points) if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    yield
    plugin_calls.core_registry.cache_clear()


@pytest.fixture
def client(hub_db, tmp_path, github, tasks_core):
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def members(client, github) -> dict:
    """The project with its members of MEMBERS, the hub admin and the plan rollout; the headers of each."""
    headers = {"admin": bearer(live.sign_in(client, github, ADMIN, 302)["token"])}
    registered = client.put(f"/v1/projects/{PROJECT}", json=registration(OPEN_SINKS), headers=headers["admin"])
    assert registered.status_code == 200, registered.text
    for login, (github_id, role, level) in MEMBERS.items():
        headers[login] = bearer(live.sign_in(client, github, login, github_id)["token"])
        grant = {"role": role, "max_level": level}
        granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=headers["admin"])
        assert granted.status_code == 200, granted.text
    pushed = client.put(f"/v1/projects/{PROJECT}/plans/{PLAN}", json={"body": plan_body()}, headers=headers["owner"])
    assert pushed.status_code == 200, pushed.text
    return headers


def enable(client, headers, plugin: str, permissions=()) -> None:
    body = {"permissions": list(permissions)}
    enabled = client.post(f"/v1/projects/{PROJECT}/plugins/{plugin}/enable", json=body, headers=headers)
    assert enabled.status_code == 200, enabled.text


def configure(client, headers, plugin: str, values: dict) -> None:
    written = client.post(f"/v1/projects/{PROJECT}/plugins/{plugin}/config", json={"values": values}, headers=headers)
    assert written.status_code == 200, written.text


@pytest.fixture
def hub(client, members) -> dict:
    """``members`` with plugins tasks (granted plans:read) and srs enabled, and the sample SRS imported."""
    enable(client, members["owner"], "tasks", ["plans:read"])
    enable(client, members["owner"], "srs")
    body = {"markdown": SAMPLE_SRS.read_text(encoding="utf-8"), "slug": "demo-shop"}
    imported = client.post(f"/v1/x/srs/{PROJECT}/import", json=body, headers=members["alice"])
    assert imported.status_code == 200, imported.text
    return members


def created(client, headers, expect: int = 201, **body) -> dict:
    answer = client.post(f"{BASE}/tasks", json=body, headers=headers)
    assert answer.status_code == expect, answer.text
    return answer.json()


def edited(client, headers, ref: str, expect: int = 200, **body) -> dict:
    answer = client.patch(f"{BASE}/tasks/{ref}", json=body, headers=headers)
    assert answer.status_code == expect, answer.text
    return answer.json()


def shown(client, headers, ref: str) -> dict:
    answer = client.get(f"{BASE}/tasks/{ref}", headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.json()


def sprint(client, headers, name: str, state: str = "planned", status: int = 201):
    answer = client.post(f"{BASE}/sprints", json={"name": name, "state": state}, headers=headers)
    assert answer.status_code == status, answer.text
    return answer.json()


def set_step(client, headers, step: int, status: str) -> None:
    revision = client.get(f"/v1/projects/{PROJECT}/plans/{PLAN}", headers=headers).json()["revision"]
    body = {"section": "steps", "step": step, "updates": {"status": status}, "if_revision": revision}
    patched = client.patch(f"/v1/projects/{PROJECT}/plans/{PLAN}", json=body, headers=headers)
    assert patched.status_code == 200, patched.text


def deliver(client) -> dict:
    return client.portal.call(lambda: plugin_events.deliver(client.app.state.engine))


def history(task: dict, name: str) -> list[tuple]:
    return [(e["actor"], e["from_value"], e["to_value"]) for e in task["events"] if e["field"] == name]


# Enabling and config


def test_tasks_routes_answer_404_where_the_project_has_not_enabled_the_plugin(client, members):
    assert client.get(f"{BASE}/tasks", headers=members["alice"]).status_code == 404
    enable(client, members["owner"], "tasks", ["plans:read"])
    assert client.get(f"{BASE}/tasks", headers=members["alice"]).json() == []
    disabled = client.post(f"/v1/projects/{PROJECT}/plugins/tasks/disable", headers=members["owner"])
    assert disabled.status_code == 200, disabled.text
    answer = client.post(f"{BASE}/tasks", json={"title": "x"}, headers=members["alice"])
    assert answer.status_code == 404


def test_tasks_settings_follow_the_project_config(client, hub):
    defaults = client.get(f"{BASE}/settings", headers=hub["eve"]).json()
    assert defaults["prefix"] == "T" and defaults["srs"] is True and defaults["types"] == ["task", "bug", "story"]
    assert [s["key"] for s in defaults["statuses"]] == [
        "backlog",
        "todo",
        "in_progress",
        "in_review",
        "done",
        "cancelled",
    ]
    statuses = [
        {"key": "open", "name": "Open", "group": "todo"},
        {"key": "shipped", "name": "Shipped", "group": "done"},
    ]
    configure(client, hub["owner"], "tasks", {"prefix": "SHOP", "statuses": statuses})
    task = created(client, hub["alice"], title="Cart totals")
    assert (task["ref"], task["status"], task["status_group"]) == ("SHOP-1", "open", "todo")
    refused = client.post(f"{BASE}/tasks", json={"title": "x", "status": "todo"}, headers=hub["alice"])
    assert refused.status_code == 422 and "its statuses are open, shipped" in refused.json()["message"]
    configure(client, hub["owner"], "tasks", {"on_steps_done": "nowhere"})
    broken = client.get(f"{BASE}/tasks", headers=hub["alice"])
    assert broken.status_code == 409 and "on_steps_done names nowhere" in broken.json()["message"]


# Writing and reading


def test_tasks_a_writer_creates_a_task_with_people_and_links_that_readers_read(client, hub):
    sprint(client, hub["alice"], "Sprint 7", "active")
    [run] = dispatched(client, hub["owner"], [2])
    body = {
        "title": "Cancel an order before it is packed",
        "description_md": "Customers cancel from the order page.",
        "type": "story",
        "status": "in_progress",
        "priority": "high",
        "due_on": "2026-10-24",
        "sprint": "Sprint 7",
        "assignees": ["alice"],
        "roles": {"ba": ["alice"], "qa": ["bao"]},
        "links": {
            "use_cases": ["uc-ord-001"],  # an alias of UC ORD-1, in another case
            "steps": [f"{PLAN}#2"],
            "runs": [run["id"]],
            "pulls": ["https://git.example.org/evo/shop/pull/88"],
        },
    }
    task = created(client, hub["alice"], **body)
    assert (task["ref"], task["type"], task["status_group"], task["sprint"]) == ("T-1", "story", "doing", "Sprint 7")
    assert task["assignees"] == ["alice"] and task["roles"] == {"ba": ["alice"], "qa": ["bao"], "dev": []}
    assert (task["steps_done"], task["steps_total"]) == (0, 1)
    assert task["label"] == {"level": "internal", "location": "any", "integrity": "U", "projects": [PROJECT]}
    links = {link["kind"]: link for link in shown(client, hub["eve"], "T-1")["links"]}
    assert (links["srs_item"]["code"], links["srs_item"]["item_kind"]) == ("UC ORD-1", "use_case")
    assert (links["srs_item"]["item_title"], links["srs_item"]["document"]) == ("Place an order", "demo-shop")
    step = links["plan_step"]
    assert (step["plan_id"], step["step"], step["step_title"], step["step_status"]) == (PLAN, "2", "Queue", "pending")
    assert step["runs"] == [{"id": run["id"], "state": "queued"}]
    assert (links["run"]["run_id"], links["run"]["run_kind"], links["run"]["run_state"]) == (
        run["id"],
        "step",
        "queued",
    )
    assert links["pull"]["url"] == "https://git.example.org/evo/shop/pull/88"
    assert history(task, "created") == [("alice", None, "T-1")]
    assert history(task, "assignee") == [("alice", None, "alice")]

    listed = client.get(f"{BASE}/tasks", params={"sprint": "Sprint 7"}, headers=hub["eve"]).json()
    assert [t["ref"] for t in listed] == ["T-1"]
    assert client.get(f"{BASE}/tasks", params={"backlog": "true"}, headers=hub["eve"]).json() == []
    assert [t["ref"] for t in client.get(f"{BASE}/tasks", params={"mine": "true"}, headers=hub["bao"]).json()] == [
        "T-1"
    ]
    assert client.get(f"{BASE}/tasks", params={"assignee": "bao"}, headers=hub["eve"]).json() == []
    assert shown(client, hub["eve"], "1")["ref"] == "T-1"
    assert client.get(f"{BASE}/tasks/T-2", headers=hub["eve"]).status_code == 404


def test_tasks_refuse_a_reader_people_without_a_grant_and_targets_not_there(client, hub):
    assert client.post(f"{BASE}/tasks", json={"title": "x"}, headers=hub["eve"]).status_code == 403
    stranger = client.post(f"{BASE}/tasks", json={"title": "x", "assignees": ["mallory"]}, headers=hub["alice"])
    assert stranger.status_code == 422 and "no grant there: mallory" in stranger.json()["message"]
    for links, said in (
        ({"use_cases": ["UC NOPE-9"]}, "no SRS item UC NOPE-9"),
        ({"steps": [f"{PLAN}#9"]}, "has no step 9"),
        ({"steps": ["elsewhere#1"]}, "no plan elsewhere"),
        ({"runs": [999]}, "no run 999"),
        ({"pulls": ["ftp://x/1"]}, "http(s) URL"),
    ):
        refused = client.post(f"{BASE}/tasks", json={"title": "x", "links": links}, headers=hub["alice"])
        assert refused.status_code == 422 and said in refused.json()["message"], refused.text
    assert client.get(f"{BASE}/tasks", headers=hub["eve"]).json() == []  # nothing was written


def test_tasks_edit_changes_fields_people_and_links_and_keeps_the_history(client, hub):
    sprint(client, hub["alice"], "Sprint 7", "active")
    parent = created(client, hub["alice"], title="Orders")
    task = created(client, hub["alice"], title="Cancel", links={"use_cases": ["UC ORD-1"], "steps": [f"{PLAN}#2"]})
    [use_case] = [link["id"] for link in task["links"] if link["kind"] == "srs_item"]
    changed = edited(
        client,
        hub["bao"],
        task["ref"],
        status="in_review",
        priority="urgent",
        due_on="2026-11-02",
        sprint="Sprint 7",
        parent=parent["ref"],
        assignees=["bao", "alice"],
        roles={"dev": ["bao"]},
        links={"use_cases": ["BR ORD1-1"]},
        unlink=[use_case],
    )
    assert (changed["status"], changed["priority"], changed["due_on"]) == ("in_review", "urgent", "2026-11-02")
    assert (changed["sprint"], changed["parent"], changed["assignees"]) == ("Sprint 7", "T-1", ["alice", "bao"])
    assert changed["roles"]["dev"] == ["bao"]
    assert [link["code"] for link in changed["links"] if link["kind"] == "srs_item"] == ["BR ORD1-1"]
    assert history(changed, "status") == [("bao", "backlog", "in_review")]
    assert history(changed, "sprint") == [("bao", None, "Sprint 7")]
    assert history(changed, "parent") == [("bao", None, "T-1")]
    cleared = edited(client, hub["alice"], task["ref"], due_on=None, sprint=None, assignees=[], roles={"dev": []})
    assert (cleared["due_on"], cleared["sprint"], cleared["assignees"], cleared["roles"]["dev"]) == (None, None, [], [])
    own_parent = edited(client, hub["alice"], parent["ref"], expect=422, parent=task["ref"])
    assert "never its own parent" in own_parent["message"]
    closed = client.patch(f"{BASE}/sprints/Sprint 7", json={"state": "closed"}, headers=hub["alice"])
    assert closed.status_code == 200, closed.text
    late = edited(client, hub["alice"], task["ref"], expect=422, sprint="Sprint 7")
    assert "is closed" in late["message"]
    comment = client.post(f"{BASE}/tasks/{task['ref']}/comments", json={"body": "Ready for QA"}, headers=hub["bao"])
    assert comment.status_code == 201 and comment.json()["author"] == "bao"
    assert [c["body"] for c in shown(client, hub["eve"], task["ref"])["comments"]] == ["Ready for QA"]


def test_tasks_sprints_run_one_at_a_time_and_closing_moves_the_open_tasks(client, hub):
    sprint(client, hub["alice"], "Sprint 7", "active")
    second = sprint(client, hub["alice"], "Sprint 8", "active", status=409)
    assert "close it first" in second["message"]
    sprint(client, hub["alice"], "Sprint 8")
    open_task = created(client, hub["alice"], title="Open", sprint="Sprint 7")
    done_task = created(client, hub["alice"], title="Done", sprint="Sprint 7", status="done")
    body = {"state": "closed", "move_to": "Sprint 8"}
    closed = client.patch(f"{BASE}/sprints/Sprint 7", json=body, headers=hub["alice"])
    assert closed.status_code == 200 and closed.json()["state"] == "closed" and closed.json()["tasks"] == 1
    assert shown(client, hub["eve"], open_task["ref"])["sprint"] == "Sprint 8"
    assert shown(client, hub["eve"], done_task["ref"])["sprint"] == "Sprint 7"
    started = client.patch(f"{BASE}/sprints/Sprint 8", json={"state": "active"}, headers=hub["alice"])
    assert started.status_code == 200 and started.json()["state"] == "active"
    names = [(s["name"], s["state"]) for s in client.get(f"{BASE}/sprints", headers=hub["eve"]).json()]
    assert sorted(names) == [("Sprint 7", "closed"), ("Sprint 8", "active")]


def test_tasks_a_label_above_the_readers_grant_hides_the_task(client, hub):
    created(client, hub["alice"], title="Internal")
    assert client.get(f"{BASE}/tasks", headers=hub["outsider"]).json() == []
    assert client.get(f"{BASE}/tasks/T-1", headers=hub["outsider"]).status_code == 404
    label = {"level": "internal", "location": "any"}
    refused = client.post(f"{BASE}/tasks", json={"title": "Own label", "label": label}, headers=hub["alice"])
    assert refused.status_code == 403 and "takes an admin" in refused.json()["message"]
    assert created(client, hub["owner"], title="Own label", label=label)["label"]["level"] == "internal"
    assert [t["ref"] for t in client.get(f"{BASE}/tasks", headers=hub["eve"]).json()] == ["T-1", "T-2"]


# Plan steps


def test_tasks_a_plan_step_turning_done_shows_on_the_task_and_writes_its_history_once(client, hub, hub_db):
    task = created(client, hub["alice"], title="Queue", links={"steps": [f"{PLAN}#2"]})
    other = created(client, hub["alice"], title="Docs", links={"steps": [f"{PLAN}#4"]})
    set_step(client, hub["owner"], 2, "done")
    now = shown(client, hub["eve"], task["ref"])  # read from the plan, before any event is delivered
    assert [link["step_status"] for link in now["links"]] == ["done"]
    assert (now["steps_done"], now["steps_total"], now["status"]) == (1, 1, "backlog")

    assert deliver(client)["delivered"] == 1
    after = shown(client, hub["eve"], task["ref"])
    assert history(after, "plan_step") == [(None, "pending", "done")]
    assert [e["cause"] for e in after["events"] if e["field"] == "plan_step"] == [f"plan {PLAN} step 2 is done"]
    assert after["status"] == "backlog"  # on_steps_done is not set: the task keeps its status
    assert history(shown(client, hub["eve"], other["ref"]), "plan_step") == []

    d = tables.plugin_deliveries
    [(delivery, payload)] = sql(hub_db, select(d.c.id, d.c.payload))
    again = client.portal.call(_steps_done, client.app.state.engine, payload, delivery)
    assert again == 0  # the same delivery tried again writes nothing
    assert history(shown(client, hub["eve"], task["ref"]), "plan_step") == [(None, "pending", "done")]


async def _steps_done(engine, payload: dict, delivery: int) -> int:
    async with engine.begin() as conn:
        project_id = await conn.scalar(select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))
        return await steps_done(conn, project_id, PROJECT, payload, delivery)


def test_tasks_on_steps_done_moves_a_task_once_every_step_it_links_is_done(client, hub):
    configure(client, hub["owner"], "tasks", {"on_steps_done": "in_review"})
    task = created(client, hub["alice"], title="Queue and daemon", links={"steps": [f"{PLAN}#2", f"{PLAN}#3"]})
    finished = created(client, hub["alice"], title="Cancelled", status="cancelled", links={"steps": [f"{PLAN}#3"]})
    set_step(client, hub["owner"], 2, "done")
    deliver(client)
    assert shown(client, hub["eve"], task["ref"])["status"] == "backlog"  # step 3 is not done yet
    set_step(client, hub["owner"], 3, "done")
    deliver(client)
    moved = shown(client, hub["eve"], task["ref"])
    assert moved["status"] == "in_review" and (moved["steps_done"], moved["steps_total"]) == (2, 2)
    [event] = [e for e in moved["events"] if e["field"] == "status"]
    assert (event["actor"], event["from_value"], event["to_value"]) == (None, "backlog", "in_review")
    assert event["cause"] == f"every plan step it links is done, plan {PLAN} step 3 last"
    assert shown(client, hub["eve"], finished["ref"])["status"] == "cancelled"


# SRS links


def test_tasks_link_srs_items_only_while_the_project_enables_srs(client, hub):
    task = created(
        client, hub["alice"], title="Cancel", links={"use_cases": ["UC ORD-1"], "pulls": ["https://x.example/1"]}
    )
    disabled = client.post(f"/v1/projects/{PROJECT}/plugins/srs/disable", headers=hub["owner"])
    assert disabled.status_code == 200, disabled.text
    assert [link["kind"] for link in shown(client, hub["eve"], task["ref"])["links"]] == ["pull"]
    refused = edited(client, hub["alice"], task["ref"], expect=422, links={"use_cases": ["UC ORD-2"]})
    assert "does not enable plugin srs" in refused["message"]
    assert client.get(f"{BASE}/settings", headers=hub["eve"]).json()["srs"] is False
    enable(client, hub["owner"], "srs")
    assert [link["code"] for link in shown(client, hub["eve"], task["ref"])["links"]] == ["UC ORD-1", None]


# Importing


def sample_tasks() -> dict:
    return json.loads(SAMPLE_TASKS.read_text(encoding="utf-8"))


def imported(client, headers, data: dict, *, dry_run: bool = False, status: int = 200) -> dict:
    answer = client.post(f"{BASE}/import", json={"data": data, "dry_run": dry_run}, headers=headers)
    assert answer.status_code == status, answer.text
    return answer.json()


def test_tasks_import_reads_evo_tasks_json_once_and_links_use_cases_by_code(client, hub):
    preview = imported(client, hub["alice"], sample_tasks(), dry_run=True)
    assert (preview["dry_run"], preview["tasks"], preview["sprints"], preview["created"]) == (True, 3, 1, [])
    problems = {(p["level"], p["task"], p["message"].split(":")[0]) for p in preview["problems"]}
    assert ("warning", None, "person p3 has no GitHub login") in problems
    assert ("warning", "n-002", "UC SHIP-1 is not linked") in problems
    assert not [p for p in preview["problems"] if p["level"] == "error"]
    assert client.get(f"{BASE}/tasks", headers=hub["eve"]).json() == []

    done = imported(client, hub["alice"], sample_tasks())
    assert done["created"] == ["T-1", "T-2", "T-3"] and done["links"] == preview["links"]
    first = shown(client, hub["eve"], "T-1")
    assert (first["type"], first["status"], first["sprint"], first["priority"]) == (
        "story",
        "in_progress",
        "Sprint 7",
        "high",
    )
    assert first["assignees"] == ["alice"] and first["roles"] == {"ba": ["alice"], "qa": ["bao"], "dev": []}
    kinds = {link["kind"]: link for link in first["links"]}
    assert kinds["srs_item"]["code"] == "UC ORD-1" and kinds["plan_step"]["step"] == "2"
    assert first["external_ref"] == {
        "system": "notion",
        "id": "0000aaaa",
        "url": "https://example.invalid/task/0000aaaa",
    }
    second = shown(client, hub["eve"], "T-2")
    assert second["external_ref"]["unmatched_use_cases"] == ["UC SHIP-1"] and second["parent"] == "T-1"
    assert [
        (e["field"], e["to_value"], e["at"][:10]) for e in second["events"] if e["cause"].startswith("done on")
    ] == [("status", "done", "2026-10-15")]

    again = imported(client, hub["alice"], sample_tasks())
    assert again["created"] == ["T-4"] and [s["task"] for s in again["skipped"]] == ["T-1", "T-2"]
    assert len(client.get(f"{BASE}/tasks", headers=hub["eve"]).json()) == 4  # the task without a source, again


def test_tasks_import_stops_at_a_status_the_project_does_not_have(client, hub):
    data = sample_tasks()
    data["status_map"].pop("Doing")
    preview = imported(client, hub["alice"], data, dry_run=True)
    [error] = [p for p in preview["problems"] if p["level"] == "error"]
    assert error["task"] == "n-001" and "status 'Doing' is no status of the project" in error["message"]
    refused = imported(client, hub["alice"], data, status=422)
    assert "nothing was imported" in refused["message"] and any(p["task"] == "n-001" for p in refused["detail"])
    assert client.get(f"{BASE}/tasks", headers=hub["eve"]).json() == []
    assert imported(client, hub["eve"], data, status=403)["message"].startswith("importing tasks")


# Tables


@pytest.mark.parametrize(
    "change",
    [
        lambda k: update(k.items).values(status="In Progress"),
        lambda k: update(k.items).values(priority="whenever"),
        lambda k: update(k.links).values(url="https://x.example/2"),  # a link to an SRS item holds no URL
        lambda k: update(k.people).values(role="owner"),
        lambda k: update(k.sprints).values(state="closed"),  # closed without closed_at
    ],
    ids=["status", "priority", "link-columns", "role", "closed-unstamped"],
)
def test_tasks_tables_refuse_rows_their_checks_forbid(client, hub, hub_db, change):
    sprint(client, hub["alice"], "Sprint 7")
    created(client, hub["alice"], title="Cancel", assignees=["alice"], links={"use_cases": ["UC ORD-1"]})
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(hub_db, change(tables.tasks))
