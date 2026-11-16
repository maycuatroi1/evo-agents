"""The priority of a run (``runs.priority``, ``evo_agents.hub.server.runs.service.priority``), as criterion a2 of the
plan orchestrator-keeps-plans-moving names it: an agent's ask sets it, a writer of the project changes it while the
run is queued, and a worker claims its queued runs by priority first, then by id.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select

from evo_agents.hub import tables
from tests.hub.live import sql
from tests.hub.test_agent_delegation import KB_AGENT, kb_node, kb_project
from tests.hub.test_chat_runs import (  # noqa: F401
    PROJECT,
    app_key,
    chat_worker,
    claim,
    client,
    created,
    hub,
    in_chats,
    moved,
    queued_lines,
    sent,
    started,
)


def asked(client, worker: dict, run_id: int, text: str, **body) -> int:
    response = in_chats(client, worker, run_id, "agent/asks", {"handle": KB_AGENT, "body": text, **body})
    assert response.status_code == 201, response.text
    assert response.json()["refusal"] is None, response.json()
    return response.json()["run_id"]


def test_a_queued_run_of_higher_priority_is_claimed_first(client, hub, hub_db):
    kb_project(client, hub, hub_db)
    studio = chat_worker(client, hub["owner"], "studio")
    node = kb_node(client, hub["owner"], hub_db)
    _, spec = started(client, hub, studio, created(client, hub["owner"]))
    low = asked(client, studio, spec["id"], "Which service signs the export links?", priority="low")
    medium = asked(client, studio, spec["id"], "Where is its key read?")
    urgent = asked(client, studio, spec["id"], "Is the key rotated?", priority="urgent")
    r = tables.runs
    rows = sql(hub_db, select(r.c.id, r.c.priority).where(r.c.id.in_([low, medium, urgent])).order_by(r.c.id))
    assert rows == [(low, "low"), (medium, "medium"), (urgent, "urgent")]
    # the queue of kb-node: medium waits behind none of lower priority, urgent behind none at all
    assert [line.split(": ")[-1] for line in queued_lines(hub_db)] == ["0 runs ahead of it", "0 runs ahead of it"]
    taken = []
    for _ in range(3):
        spec = claim(client, node)
        taken.append(spec["id"])
        moved(client, node, spec["id"], "running", session_id=f"sess-{spec['id']}")
        moved(client, node, spec["id"], "verifying", "done")
    assert taken == [urgent, medium, low]


def test_the_writer_sets_a_queued_runs_priority(client, hub, hub_db):
    chat_worker(client, hub["owner"], "studio")
    mention = sent(client, hub["owner"], created(client, hub["owner"])["id"], "@reviewer look at the diff")
    run_id = mention["mentions"][0]["run_id"]
    route = f"/v1/projects/{PROJECT}/runs/{run_id}/priority"
    assert client.post(route, json={"priority": "high"}, headers=hub["stranger"]).status_code == 404
    assert client.post(route, json={"priority": "soon"}, headers=hub["other"]).status_code == 422
    response = client.post(route, json={"priority": "high"}, headers=hub["other"])  # a writer of the project
    assert response.status_code == 200, response.text
    assert (response.json()["id"], response.json()["priority"]) == (run_id, "high")
    a = tables.audit
    targets = sql(hub_db, select(a.c.target).where(a.c.action == "run.priority"))
    assert targets == [(f"{PROJECT} run:{run_id} priority=medium->high",)]
    r = tables.runs
    sql(hub_db, r.update().values(state="cancelled", finished_at=func.now()).where(r.c.id == run_id))
    gone = client.post(route, json={"priority": "low"}, headers=hub["owner"])
    assert gone.status_code == 409, gone.text
