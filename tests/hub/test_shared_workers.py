"""Shared workers (docs/workers.md, Shared workers): a worker its owner shares takes the runs of every member who holds
writer on a project it serves, one member's runs at a time; only its owner shares it, the members it is shared with see
it but change nothing, and the run stays its dispatcher's: their grant, their candidates, their worker names."""

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from evo_agents.hub import tables
from evo_agents.hub.client import HubError
from evo_agents.hub.run_cli_workers import worker_id as _worker_id
from tests.hub.test_plans import registration
from tests.hub.test_runs import (  # noqa: F401  (fixtures)
    OTHER,
    OWNER,
    PROJECT,
    READ_ONLY,
    WRITER,
    add_worker,
    audit_rows,
    claim,
    client,
    dispatch,
    dispatched,
    hub,
    moved,
    of_run,
)

BETA = "beta"


def share(client, headers, worker: dict, shared: bool = True):
    return client.patch(f"/v1/workers/{worker['id']}", json={"shared": shared}, headers=headers)


def shared_worker(client, hub, name: str = "lab", slots: int = 2) -> dict:
    worker = add_worker(client, hub["owner"], name, slots=slots)
    response = share(client, hub["owner"], worker)
    assert response.status_code == 200 and response.json()["shared"] is True, response.text
    return worker


def grant(client, hub, login: str, body: dict, project: str = PROJECT) -> None:
    response = client.put(f"/v1/admin/projects/{project}/grants/{login}", json=body, headers=hub["admin"])
    assert response.status_code == 200, response.text


def test_a_member_dispatches_to_a_worker_its_owner_shares_and_it_claims_their_run(client, hub, hub_db):
    lab = add_worker(client, hub["owner"], "lab", slots=2)
    refused = dispatch(client, hub["other"], [4], worker_id=lab["id"])
    assert refused.status_code == 403 and "worker of the member who dispatches it or to one shared" in refused.text
    assert share(client, hub["owner"], lab).status_code == 200
    assert audit_rows(hub_db, "worker")[-1][:3] == (
        "worker.update",
        f"worker:{lab['id']} name=lab owner={OWNER} shared=false->true",
        OWNER,
    )
    (pinned,) = dispatched(client, hub["other"], [4], worker_id=lab["id"])
    assert pinned["dispatched_by"] == OTHER and pinned["pinned_worker_id"] == lab["id"]
    assert claim(client, lab)["id"] == pinned["id"]
    (loose,) = dispatched(client, hub["other"], [5])  # no pin: a worker shared in the project may take it too
    assert claim(client, lab)["id"] == loose["id"]
    assert of_run(hub_db, loose["id"], "worker_id") == [(lab["id"],)]


def test_a_shared_worker_runs_one_member_s_runs_at_a_time(client, hub, hub_db):
    lab = shared_worker(client, hub, slots=2)
    (theirs,) = dispatched(client, hub["other"], [4])
    (mine,) = dispatched(client, hub["owner"], [5])
    assert claim(client, lab)["id"] == theirs["id"]  # the oldest run first
    assert claim(client, lab) is None  # a free slot, but the run it holds is another member's
    moved(client, lab, theirs["id"], "running", "failed", error="it stopped")
    assert claim(client, lab)["id"] == mine["id"]
    (again,) = dispatched(client, hub["owner"], [2])
    assert claim(client, lab)["id"] == again["id"]  # the owner's two runs side by side


def test_a_shared_worker_takes_no_run_of_a_member_without_writer_nor_any_once_its_owner_lost_writer(client, hub):
    lab = shared_worker(client, hub)
    (theirs,) = dispatched(client, hub["other"], [4])
    grant(client, hub, OTHER, READ_ONLY)
    assert claim(client, lab) is None
    grant(client, hub, OTHER, WRITER)
    grant(client, hub, OWNER, READ_ONLY)
    assert claim(client, lab) is None  # its owner no longer writes to the project: it serves it no more
    pinned = dispatch(client, hub["other"], [5], worker_id=lab["id"])
    assert pinned.status_code == 403
    grant(client, hub, OWNER, WRITER)
    assert claim(client, lab)["id"] == theirs["id"]


def test_a_worker_no_longer_shared_takes_no_more_runs_of_other_members(client, hub):
    lab = shared_worker(client, hub)
    (theirs,) = dispatched(client, hub["other"], [4], worker_id=lab["id"])
    assert share(client, hub["owner"], lab, False).status_code == 200
    assert claim(client, lab) is None
    assert dispatch(client, hub["other"], [5], worker_id=lab["id"]).status_code == 403
    assert share(client, hub["owner"], lab).status_code == 200
    assert claim(client, lab)["id"] == theirs["id"]


def test_only_its_owner_shares_a_worker_and_the_members_it_is_shared_with_only_see_it(client, hub):
    beta = {**registration(), "harness": {"name": BETA, "workspace": "~/ws", "path": "beta-harness"}}
    assert client.put(f"/v1/projects/{BETA}", json=beta, headers=hub["admin"]).status_code == 200
    grant(client, hub, OWNER, WRITER, BETA)
    body = {"name": "lab", "projects": [PROJECT, BETA], "slots": 2, "hostname": "lab", "os": "linux", "arch": "x64"}
    created = client.post("/v1/workers", json={**body, "agent_version": "0.4.0"}, headers=hub["owner"])
    assert created.status_code == 201, created.text
    lab = created.json()["worker"]
    path = f"/v1/workers/{lab['id']}"

    def listed(member: str) -> list[dict]:
        response = client.get("/v1/workers", headers=hub[member])
        assert response.status_code == 200, response.text
        return [worker for worker in response.json() if worker["id"] == lab["id"]]

    assert listed("other") == [] and client.get(path, headers=hub["other"]).status_code == 404
    assert client.patch(path, json={"shared": True}, headers=hub["other"]).status_code == 404
    assert client.patch(path, json={"shared": True}, headers=hub["admin"]).status_code == 403
    assert client.patch(path, json={"shared": True}, headers=hub["owner"]).status_code == 200
    (seen,) = listed("other")
    assert (seen["owner"], seen["shared"], seen["projects"], seen["kg_sync"]) == (OWNER, True, [PROJECT], None)
    assert client.get(path, headers=hub["other"]).json()["projects"] == [PROJECT]
    assert client.get(path, headers=hub["owner"]).json()["projects"] == sorted([PROJECT, BETA])
    assert listed("reader") == [] and listed("stranger") == []  # no writer on a project it serves
    assert client.get(path, headers=hub["reader"]).status_code == 404
    for action in ("drain", "revoke"):
        assert client.post(f"{path}/{action}", headers=hub["other"]).status_code == 404
    assert client.patch(path, json={"slots": 1}, headers=hub["other"]).status_code == 404
    assert client.get("/v1/workers/999999", headers=hub["admin"]).status_code == 404
    assert client.get(path, headers=hub["admin"]).json()["projects"] == sorted([PROJECT, BETA])


def test_the_candidates_of_a_member_s_failed_run_list_the_worker_shared_with_them(client, hub):
    lab = shared_worker(client, hub)
    (run,) = dispatched(client, hub["other"], [4], worker_id=lab["id"])
    assert claim(client, lab)["id"] == run["id"]
    moved(client, lab, run["id"], "running", "failed", error="it stopped")
    response = client.get(f"/v1/projects/{PROJECT}/runs/{run['id']}/rerun-candidates", headers=hub["other"])
    assert response.status_code == 200, response.text
    (candidate,) = response.json()["workers"]
    assert (candidate["worker"], candidate["owner"], candidate["shared"], candidate["fit"]) == (
        "lab",
        OWNER,
        True,
        True,
    )


def test_the_command_line_names_a_shared_worker_by_its_name_or_owner_and_name():
    rows = [
        {"id": 1, "name": "lab", "owner": "me", "shared": False},
        {"id": 2, "name": "lab", "owner": "ann", "shared": True},
        {"id": 3, "name": "gpu", "owner": "ann", "shared": True},
        {"id": 4, "name": "gpu", "owner": "bob", "shared": True},
        {"id": 5, "name": "box", "owner": "bob", "shared": True},
    ]

    class Hub:
        def call(self, method, path):
            assert (method, path) == ("GET", "/v1/workers")
            return rows

    assert _worker_id(Hub(), "me", "lab") == 1  # one's own first
    assert _worker_id(Hub(), "me", "ann/lab") == 2
    assert _worker_id(Hub(), "me", "box") == 5
    assert _worker_id(Hub(), "me", "7") == 7
    with pytest.raises(HubError, match="several members share a worker named 'gpu'"):
        _worker_id(Hub(), "me", "gpu")
    with pytest.raises(HubError, match="no member shares one by that name"):
        _worker_id(Hub(), "me", "nope")


def test_the_shared_column_starts_false_for_every_worker(client, hub, hub_db):
    lab = add_worker(client, hub["owner"], "lab")
    assert live.sql(hub_db, tables.workers.select().with_only_columns(tables.workers.c.shared)) == [(False,)]
    assert client.get(f"/v1/workers/{lab['id']}", headers=hub["owner"]).json()["shared"] is False
