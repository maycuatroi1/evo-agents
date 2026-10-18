"""Triage reaches the agent of another project (criterion a6 of the plan run-failure-triage): when the error of a failed
run names another project, the candidates list its agents and the owner's role there, the triage agent invites one into
the thread (``evo-agents worker triage invite``) and hands it work by a mention; with no writer grant, or a label of the
chat someone in it does not clear, the hub says so in the thread and invites no one."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import insert, select

from evo_agents.hub import tables
from tests.hub.live import sql
from tests.hub.test_chat_runs import chat_worker
from tests.hub.test_chats import agent
from tests.hub.test_plans import registration
from tests.hub.test_runs import CHECKOUTS, PROJECT, client, hub  # noqa: F401
from tests.hub.test_triage import failed_step
from tests.hub.test_triage_actions import agent_of, audited, said

OTHER_PROJECT = "evo-lms"
OTHER_AGENT = "lms-bot"
ERROR = f"worker for {OTHER_PROJECT} refused the checkout: branch mismatch"
BOTH = {**CHECKOUTS, f"{OTHER_PROJECT}/{OTHER_PROJECT}": {"path": "/src/evo-lms", "branch": "main"}}


def other_project(client, hub, hub_db, role: str | None = "writer", level: str = "internal") -> None:
    """Project evo-lms with the agent lms-bot; the owner holds ``role`` there (no grant for None)."""
    assert client.put(f"/v1/projects/{OTHER_PROJECT}", json=registration(), headers=hub["admin"]).status_code == 200
    admin = {"role": "admin", "max_level": "internal"}
    assert client.put(f"/v1/admin/projects/{OTHER_PROJECT}/grants/owner", json=admin, headers=hub["admin"]).status_code
    agent(client, hub["owner"], OTHER_AGENT, project=OTHER_PROJECT)
    if role is None:
        assert (
            client.delete(f"/v1/admin/projects/{OTHER_PROJECT}/grants/owner", headers=hub["admin"]).status_code == 204
        )
    elif role != "admin":
        grant = {"role": role, "max_level": level}
        assert client.put(
            f"/v1/admin/projects/{OTHER_PROJECT}/grants/owner", json=grant, headers=hub["admin"]
        ).status_code


def serve(hub_db, worker: dict) -> None:
    (pid,) = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == OTHER_PROJECT))[0]
    sql(hub_db, insert(tables.worker_projects).values(worker_id=worker["id"], project_id=pid))


def invite(client, worker, agent_run: int, handle: str = OTHER_AGENT):
    body = {"handle": handle, "reason": "the error is the lms worker's"}
    return client.post(f"/v1/worker/runs/{agent_run}/triage/invite", json=body, headers=worker["headers"])


def failing(client, hub, hub_db, **kw):
    worker = chat_worker(client, hub["owner"], "mac-mini", checkouts=BOTH, slots=4)
    serve(hub_db, worker)
    run_id = failed_step(client, hub, worker, 2, cause="runtime", error=ERROR, **kw)
    return worker, run_id, agent_of(client, worker, run_id, hub_db)


def test_triage_project_named_in_the_error_is_listed_with_its_agents_and_the_owners_role(client, hub, hub_db):
    other_project(client, hub, hub_db)
    worker, run_id, _ = failing(client, hub, hub_db)
    found = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}/rerun-candidates", headers=hub["owner"]).json()
    assert [(o["project"], o["role"], o["fit"], o["named"]) for o in found["other_projects"]] == [
        (OTHER_PROJECT, "writer", True, OTHER_PROJECT)
    ]
    (bot,) = [a for a in found["agents"] if a["handle"] == OTHER_AGENT]
    assert bot["project"] == OTHER_PROJECT and bot["fit"] and bot["rank"] is not None
    assert any(f"error names {OTHER_PROJECT}" in why for why in bot["reasons"])
    assert {a["handle"] for a in found["agents"] if a["project"] == OTHER_PROJECT} == {OTHER_AGENT, OTHER_PROJECT}
    root = said(client, hub, hub_db, run_id)[0]
    assert f"The error names another project:\n- {OTHER_PROJECT}" in root and OTHER_AGENT in root
    assert "@" + OTHER_AGENT not in root  # naming it in the first message must not hand it work


def test_triage_project_not_named_in_the_error_is_no_one_to_invite(client, hub, hub_db):
    other_project(client, hub, hub_db)
    worker = chat_worker(client, hub["owner"], "mac-mini", checkouts=BOTH, slots=4)
    serve(hub_db, worker)
    run_id = failed_step(client, hub, worker, 2, cause="runtime", error="the runtime stopped")
    found = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}/rerun-candidates", headers=hub["owner"]).json()
    assert found["other_projects"] == [] and {a["project"] for a in found["agents"]} == {PROJECT}
    agent_run = agent_of(client, worker, run_id, hub_db)
    refused = invite(client, worker, agent_run)
    assert refused.status_code == 422 and "names" in refused.json()["message"]


def test_triage_invites_the_agent_of_the_other_project_and_hands_it_work_by_a_mention(client, hub, hub_db):
    other_project(client, hub, hub_db)
    worker, run_id, agent_run = failing(client, hub, hub_db)
    done = invite(client, worker, agent_run)
    assert done.status_code == 201, done.text
    assert done.json()["project"] == OTHER_PROJECT and done.json()["added"] is True
    assert said(client, hub, hub_db, run_id)[-1].startswith(f"Decision: invited agent {OTHER_AGENT} of project")
    (target,) = audited(hub_db, "run.triage_invite")
    assert f"run:{run_id}" in target and f"invited:@{OTHER_AGENT}" in target
    again = invite(client, worker, agent_run)
    assert again.status_code == 201 and again.json()["added"] is False
    handed = client.post(
        f"/v1/worker/runs/{agent_run}/chat/mentions",
        json={"handle": OTHER_AGENT, "body": "the lms worker refused the checkout: look at it"},
        headers=worker["headers"],
    )
    assert handed.status_code == 201, handed.text
    assert handed.json()["refusal"] is None and handed.json()["run_id"] is not None
    r = tables.runs
    (row,) = sql(hub_db, select(r.c.kind, r.c.hop, r.c.project_id).where(r.c.id == handed.json()["run_id"]))
    (lms,) = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == OTHER_PROJECT))
    assert row == ("chat", 2, lms[0])


def test_triage_project_without_the_writer_grant_is_said_in_the_thread_and_invites_no_one(client, hub, hub_db):
    other_project(client, hub, hub_db, role="reader")
    worker, run_id, agent_run = failing(client, hub, hub_db)
    root = said(client, hub, hub_db, run_id)[0]
    assert f"{OTHER_PROJECT} (the error names {OTHER_PROJECT}): no one is invited: the owner holds reader" in root
    refused = invite(client, worker, agent_run)
    assert refused.status_code == 409 and "needs the writer role" in refused.json()["message"], refused.text
    assert said(client, hub, hub_db, run_id)[-1].startswith(f"No one invited: agent {OTHER_AGENT}")
    assert "needs the writer role" in said(client, hub, hub_db, run_id)[-1]
    chat_id = sql(hub_db, select(tables.run_triage.c.chat_id).where(tables.run_triage.c.run_id == run_id))[0][0]
    members = client.get(f"/v1/chats/{chat_id}", headers=hub["owner"]).json()["agents"]
    assert [a["handle"] for a in members] == [PROJECT]


def test_triage_project_with_no_grant_at_all_is_named_without_its_agents(client, hub, hub_db):
    other_project(client, hub, hub_db, role=None)
    worker, run_id, agent_run = failing(client, hub, hub_db)
    found = client.get(f"/v1/projects/{PROJECT}/runs/{run_id}/rerun-candidates", headers=hub["owner"]).json()
    assert [(o["project"], o["role"], o["fit"]) for o in found["other_projects"]] == [(OTHER_PROJECT, None, False)]
    assert all(a["project"] == PROJECT for a in found["agents"])
    assert "holds no grant" in said(client, hub, hub_db, run_id)[0]
    assert invite(client, worker, agent_run).status_code == 422


def test_triage_label_of_the_chat_someone_does_not_clear_is_said_in_the_thread_and_invites_no_one(client, hub, hub_db):
    other_project(client, hub, hub_db)
    worker, run_id, agent_run = failing(client, hub, hub_db)
    chat_id = sql(hub_db, select(tables.run_triage.c.chat_id).where(tables.run_triage.c.run_id == run_id))[0][0]
    # someone-else joins the chat with a grant on evo-agents only: the label of evo-lms is past them
    assert client.put(f"/v1/chats/{chat_id}/people/someone-else", headers=hub["owner"]).status_code == 200
    refused = invite(client, worker, agent_run)
    assert refused.status_code == 409 and "someone-else" in refused.json()["message"], refused.text
    assert said(client, hub, hub_db, run_id)[-1].startswith("No one invited")
    members = client.get(f"/v1/chats/{chat_id}", headers=hub["owner"]).json()["agents"]
    assert [a["handle"] for a in members] == [PROJECT]
