"""Member task runs (plan evo-creative-workspace, step 2, criterion a2; ``evo_agents.hub.server.member_tasks``): a
writer of a project hands an agent a task with a JSON input and external asset URIs, with no Git, and reads its state
and output.

The checks the step names: POST /v1/projects/{p}/task-runs queues a run of kind task with no agent type and no repo,
under the caller's own principal (a token, and an app session ``eva_``); a reader is refused; the worker that claims it
gets a prompt with the input and the assets; GET .../task-runs/{task} gives its state, then its output once done, and
follows a lost run's next attempt; its end stores task.result for the project's webhooks."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import autonomy_card, tables
from evo_agents.hub.server import member_tasks
from tests.hub.live import bearer, sql
from tests.hub.test_app_session_auth import MEMBER, client, hub, session  # noqa: F401 (client, hub: fixtures)
from tests.hub.test_chat_runs import chat_worker
from tests.hub.test_runs import (
    OWNER,
    PROJECT,
    READER,
    WRITER,
    audit_rows,
    claim,
    expire,
    moved,
    of_run,
    recover,
    state_of,
)

TASK = "Draft three taglines for the spring campaign of lumen-studio."
INPUT = {"brand": "lumen", "tone": ["warm", "short"], "limits": {"words": 8}}
ADMIN_GRANT = {"role": "admin", "max_level": "internal"}
ASSETS = ["https://assets.lumenops.example/brief.pdf", "s3://lumen-assets/moodboard/spring.png"]


def handed(client, headers, **body):
    body = {"task": TASK, "input": INPUT, "assets": ASSETS} | body
    return client.post(f"/v1/projects/{PROJECT}/task-runs", json=body, headers=headers)


def task_view(client, headers, task_id: int):
    return client.get(f"/v1/projects/{PROJECT}/task-runs/{task_id}", headers=headers)


def worker_of(client, headers, name: str = "studio") -> dict:
    return chat_worker(client, headers, name, pool=PROJECT)


def test_a_writer_hands_a_task_with_no_git_under_their_own_principal(client, hub, hub_db):
    answer = handed(client, hub["owner"])
    assert answer.status_code == 201, answer.text
    task = answer.json()
    run_id = task["task_id"]
    assert (task["run_id"], task["attempt"], task["state"], task["dispatched_by"]) == (run_id, 1, "queued", OWNER)
    assert (task["input"], task["assets"], task["output"]) == (INPUT, ASSETS, None)
    assert task["title"] == TASK
    (row,) = of_run(hub_db, run_id, "kind", "agent_type_id", "repos", "branch", "dispatched_via", "request")
    assert row == ("task", None, None, f"task/{run_id}", "machine", TASK)
    users = tables.users
    ((owner_id,),) = sql(hub_db, select(users.c.id).where(users.c.login == OWNER))
    ((principal,),) = of_run(hub_db, run_id, "principal_id")
    assert principal == owner_id  # the caller's own principal, so their autonomy ceiling and accounts
    dispatches = [row for row in audit_rows(hub_db, "run") if row[0] == "run.dispatch_task"]
    assert dispatches == [("run.dispatch_task", f"{PROJECT}/task run:{run_id} assets=2", OWNER, PROJECT)]
    # the audit row names no task, input or URI
    assert "lumenops" not in str(audit_rows(hub_db, "run"))
    # a reader reads it, as any run of the project
    shown = task_view(client, hub["reader"], run_id)
    assert shown.status_code == 200 and shown.json()["state"] == "queued"


def test_a_reader_or_a_stranger_is_refused_and_a_bad_body_is_422(client, hub, hub_db):
    refused = handed(client, hub["reader"])
    assert refused.status_code == 403 and "writer role" in refused.json()["message"]
    assert handed(client, hub["stranger"]).status_code == 404
    for body in (
        {"assets": ["file:///etc/passwd"]},
        {"assets": ["https://"]},
        {"assets": ["https://a.example/x y"]},
        {"assets": [f"https://a.example/{n}" for n in range(member_tasks.MAX_ASSETS + 1)]},
        {"input": ["not", "an", "object"]},
        {"input": {"text": "x" * member_tasks.MAX_INPUT_BYTES}},
        {"task": ""},
        {"repos": ["evo-agents"]},
    ):
        assert handed(client, hub["owner"], **body).status_code == 422, body
    assert sql(hub_db, select(tables.member_tasks.c.task_id)) == []
    assert task_view(client, hub["owner"], 999).status_code == 404
    assert task_view(client, hub["stranger"], 999).status_code == 404


def test_an_app_session_hands_a_task_as_its_member(client, hub, hub_db):
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{MEMBER}", json=WRITER, headers=hub["admin"])
    assert granted.status_code == 200, granted.text
    member = bearer(session(client, hub)["token"])
    answer = handed(client, member)
    assert answer.status_code == 201, answer.text
    task = answer.json()
    assert task["dispatched_by"] == MEMBER
    assert of_run(hub_db, task["task_id"], "dispatched_via") == [("web",)]
    assert task_view(client, member, task["task_id"]).json()["state"] == "queued"


def test_its_worker_gets_the_input_and_assets_and_the_member_reads_the_output(client, hub, hub_db):
    task_id = handed(client, hub["owner"]).json()["task_id"]
    studio = worker_of(client, hub["owner"])
    spec = claim(client, studio)
    assert (spec["id"], spec["kind"], spec["branch"]) == (task_id, "task", f"task/{task_id}")
    assert not spec.get("repos")
    prompt = autonomy_card.without_card(spec["prompt"])
    assert f"{OWNER} hands you the task below" in prompt
    assert "This run works in no repository" in prompt
    assert all(f"- {uri}" in prompt for uri in ASSETS)
    assert f"# The task\n{TASK}\n" in prompt and '"brand": "lumen"' in prompt
    moved(client, studio, task_id, "running")
    assert task_view(client, hub["owner"], task_id).json()["state"] == "running"
    moved(client, studio, task_id, "verifying", "done", summary="Three taglines, in taglines.md of the brief's folder.")
    shown = task_view(client, hub["owner"], task_id).json()
    assert shown["state"] == "done" and shown["finished_at"] is not None
    assert "Three taglines, in taglines.md" in shown["output"]


def test_a_lost_run_goes_on_in_its_next_attempt_with_its_input(client, hub, hub_db):
    task_id = handed(client, hub["owner"]).json()["task_id"]
    studio = worker_of(client, hub["owner"])
    assert claim(client, studio)["id"] == task_id
    moved(client, studio, task_id, "running")
    expire(hub_db, task_id)
    assert recover(client)["lost"] == 1 and state_of(hub_db, task_id) == "lost"
    shown = task_view(client, hub["owner"], task_id).json()
    assert (shown["task_id"], shown["attempt"], shown["state"]) == (task_id, 2, "queued")
    assert shown["run_id"] != task_id and shown["input"] == INPUT
    spec = claim(client, worker_of(client, hub["other"], "desk"))
    assert spec["id"] == shown["run_id"] and ASSETS[0] in spec["prompt"] and '"brand": "lumen"' in spec["prompt"]


def test_its_end_stores_task_result_for_the_projects_webhooks(client, hub, hub_db):
    made = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=hub["admin"])
    assert made.status_code == 200, made.text
    made = client.post(
        f"/v1/projects/{PROJECT}/webhooks",
        json={"url": "https://hooks.lumenops.example/evo", "events": ["task.result"]},
        headers=hub["owner"],
    )
    assert made.status_code == 201, made.text
    task_id = handed(client, hub["owner"]).json()["task_id"]
    studio = worker_of(client, hub["owner"])
    claim(client, studio)
    d = tables.webhook_deliveries
    assert sql(hub_db, select(d.c.event).where(d.c.event == "task.result")) == []
    moved(client, studio, task_id, "running", "verifying", "done", summary="Done.")
    ((payload,),) = sql(hub_db, select(d.c.payload).where(d.c.event == "task.result"))
    assert payload == {
        "task_id": task_id,
        "run_id": task_id,
        "attempt": 1,
        "project": PROJECT,
        "state": "done",
        "failure_cause": None,
    }
    assert READER not in str(payload)
