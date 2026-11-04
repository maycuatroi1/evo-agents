"""The routes of the entities step's requests and answers on the hub (``evo_agents.hub.server.kg_entities``): a writer
reads the requests of the latest build, only the agent of a run posts answers, the hub keeps those to a request as a
blob it holds and queues a build."""

from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, select

from evo_agents.hub import kg_entity_relay as relay
from evo_agents.hub import tables
from evo_agents.hub.blobs import blob_key
from evo_agents.hub.server import security
from evo_agents.hub.server.security import RUN, Principal
from tests.hub.live import sql
from tests.hub.test_kg import PROJECT, jobs, open_hub

REQUEST = {
    "key": "k1",
    "item": "docs:intro",
    "text": "Intro to search.",
    "node_kinds": ["Concept"],
    "relations": ["depends_on"],
    "model": "claude-haiku-5-5",
}
ANSWER = {"nodes": [{"kind": "Concept", "name": "Search"}], "edges": [], "usage": {"input_tokens": 9}}


@pytest.fixture
def hub(hub_db, tmp_path, s3):
    client, found = open_hub(hub_db, tmp_path, s3)
    try:
        yield found
    finally:
        client.app.dependency_overrides.pop(security.principal, None)
        client.__exit__(None, None, None)


def deferred_build(hub, tmp_path) -> str:
    """A succeeded build of alpha that deferred REQUEST, its requests in the blob store; their hash."""
    path, sha, _, _ = relay.write_requests(tmp_path, [REQUEST])
    hub.app.state.blobs.put_file(sha, path)
    (project_id,) = sql(hub.db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0]
    coverage = {"entity_requests": {"sha256": sha, "count": 1}}
    sql(
        hub.db,
        insert(tables.kg_builds).values(
            project_id=project_id,
            status="succeeded",
            coverage=coverage,
            artifact_sha256="a" * 64,
            artifact_size=1,
            content_hash="sha256:" + "a" * 64,
            nodes=1,
            edges=0,
            started_at=func.now(),
            finished_at=func.now(),
        ),
    )
    return sha


def answers_path() -> str:
    return f"/v1/kg/{PROJECT}/entities/answers"


def test_a_writer_reads_the_requests_and_only_a_run_s_answers_are_kept(hub, tmp_path):
    deferred_build(hub, tmp_path)
    got = hub.client.get(f"/v1/kg/{PROJECT}/entities/requests", headers=hub.alice)
    assert got.status_code == 200, got.text
    assert got.json()["requests"] == [REQUEST]
    assert hub.client.get(f"/v1/kg/{PROJECT}/entities/requests", headers=hub.carol).status_code == 403

    body = {"answers": {"k1": ANSWER, "not-asked": ANSWER}}
    refused = hub.client.post(answers_path(), json=body, headers=hub.alice)
    assert refused.status_code == 403 and "run of the project's agent pool" in refused.text

    (alice,) = sql(hub.db, select(tables.users.c.id).where(tables.users.c.login == "alice"))[0]
    run = Principal(alice, "alice", False, 0, RUN, "", run_id=41)
    hub.client.app.dependency_overrides[security.principal] = lambda: run
    kept = hub.client.post(answers_path(), json=body, headers=hub.alice)
    assert kept.status_code == 200, kept.text
    assert kept.json() == {"kept": 1, "ignored": 1, "queued": True}

    b = tables.blobs
    ((sha, kind, by),) = sql(hub.db, select(b.c.sha256, b.c.kind, b.c.created_by).where(b.c.kind == relay.ANSWERS_KIND))
    assert by == alice and kind == relay.ANSWERS_KIND
    held = SimpleNamespace(fetch=hub.app.state.blobs.fetch)
    assert relay.read_answers(held, [sha]) == {"k1": ANSWER}
    assert hub.s3.get(blob_key(sha)) is not None
    assert any(job[2] == f"kg:{PROJECT}" for job in jobs(hub.db)), "a build is queued to read them"


def test_answers_to_no_request_keep_nothing_and_queue_nothing(hub, tmp_path):
    deferred_build(hub, tmp_path)
    (alice,) = sql(hub.db, select(tables.users.c.id).where(tables.users.c.login == "alice"))[0]
    hub.client.app.dependency_overrides[security.principal] = lambda: Principal(
        alice, "alice", False, 0, RUN, "", run_id=41
    )
    got = hub.client.post(answers_path(), json={"answers": {"other": ANSWER}}, headers=hub.alice)
    assert got.json() == {"kept": 0, "ignored": 1, "queued": False}
    malformed = hub.client.post(answers_path(), json={"answers": {"k1": []}}, headers=hub.alice)
    assert malformed.status_code == 422
    assert jobs(hub.db) == []
