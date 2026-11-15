"""An admin of a project acts on another member's run of it (``evo_agents.hub.server.runs.service.admin_acts``): answers
its decisions, a credential request among them, cancels, approves and reruns it, and lists the candidates to run it
again, which only the member who dispatched the run could do before. Each act writes an event naming the admin and the
dispatcher into the run's log and tells the dispatcher; a member who is no admin keeps the 403 of before.

A value an admin gives for another member's credential request is the admin's own secret, bound to no project, and a
share for that run alone lends it to the run and the run that resumes it (``share_leases.for_run``): never written as
the dispatcher's, never lent to another run of theirs."""

from datetime import timedelta

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)
pytest.importorskip("sqlalchemy", reason="the admin's acts need the hub-server extra, evo-ak[hub-server]")

from sqlalchemy import func, select, update

from evo_agents.hub import credential_requests, runs, tables
from evo_agents.hub.server.projects import ProjectAccess, ProjectRules
from evo_agents.hub.server.runs.service import admin_acts
from evo_agents.hub.server.security import MACHINE, RUN, Principal
from tests.hub import test_credentials_api as credentials_api
from tests.hub import test_run_access as run_access_tests
from tests.hub.live import bearer, sql, table_dump
from tests.hub.test_credentials_api import OWNER, PLAN, PROJECT, STEPS, by_name, leased, reap, sample, worker_of
from tests.hub.test_run_access import (
    ANSWER_URL,
    NEEDED,
    NEEDED_SECRET,
    QUESTION,
    audit_of,
    credential_run,
    view,
)
from tests.hub.test_runs import claim, moved, state_of

app_key, config, client = (credentials_api.app_key, credentials_api.config, credentials_api.client)
hub = run_access_tests.hub  # the members, and the project's default agent with an account pool

LEAD, LEAD_ID = "lead", 503  # an admin of the project, who dispatched none of the runs
ADMIN_GRANT = {"role": "admin", "max_level": "internal"}


@pytest.fixture
def lead(client, github, hub) -> dict:
    """The headers of LEAD, who holds the admin role on the project."""
    headers = bearer(live.sign_in(client, github, LEAD, LEAD_ID)["token"])
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{LEAD}", json=ADMIN_GRANT, headers=hub["admin"])
    assert granted.status_code == 200, granted.text
    return headers


def answer_of(client, headers, decision_id: int, **body):
    return client.post(ANSWER_URL.format(project=PROJECT, decision_id=decision_id), json=body, headers=headers)


def control(client, headers, run_id: int, action: str, **body):
    url = f"/v1/projects/{PROJECT}/runs/{run_id}/{action}"
    return client.post(url, json=body or None, headers=headers)


def admin_events(db, run_id: int) -> list[dict]:
    e = tables.run_events
    rows = sql(db, select(e.c.body).where(e.c.run_id == run_id, e.c.kind == "system").order_by(e.c.seq))
    return [body for (body,) in rows if "admin_action" in body]


def notices_of(db, login: str) -> list[tuple]:
    """(run, title, details) of each notice of kind ADMIN_NOTICE to ``login``, oldest first."""
    n, u = tables.notifications, tables.users
    query = (
        select(n.c.run_id, n.c.title, n.c.details)
        .join_from(n, u, u.c.id == n.c.user_id)
        .where(u.c.login == login, n.c.notice_kind == admin_acts.ADMIN_NOTICE)
        .order_by(n.c.id)
    )
    return [tuple(row) for row in sql(db, query)]


def secrets_of(client, headers) -> dict:
    listed = client.get("/v1/secrets", headers=headers)
    assert listed.status_code == 200, listed.text
    return {secret["name"]: secret for secret in listed.json()}


def park(client, db, run_id: int) -> None:
    """Run ``run_id``, waiting on a decision, parked by the reaper as if the wait had run out."""
    r = tables.runs
    waited = func.now() - timedelta(seconds=runs.DECISION_WAIT_SECONDS + 1)
    sql(db, update(r).values(waiting_since=waited).where(r.c.id == run_id))
    assert reap(client)["parked"] >= 1 and state_of(db, run_id) == "parked"


def asking_plan_run(client, hub, worker: dict) -> tuple[int, int]:
    """A plan run of OWNER's, running on ``worker``, that asked QUESTION and waits: (its id, the decision's id)."""
    dispatched = client.post(f"/v1/projects/{PROJECT}/plan-runs", json={"plan_id": PLAN}, headers=hub["owner"])
    assert dispatched.status_code == 201, dispatched.text
    run_id = claim(client, worker)["id"]
    moved(client, worker, run_id, "running", session_id="0199a3c1-0000-7000-8000-00000000000e")
    asked = client.post(f"/v1/worker/runs/{run_id}/decisions", json=QUESTION, headers=worker["headers"])
    assert asked.status_code == 201, asked.text
    moved(client, worker, run_id, "waiting")
    return run_id, asked.json()["id"]


def test_an_admin_answers_another_members_decision_and_the_run_and_its_dispatcher_say_so(client, hub, hub_db, lead):
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id, decision_id = asking_plan_run(client, hub, worker)

    # a member who is no admin keeps the refusal of before
    refused = answer_of(client, hub["other"], decision_id, option="sqlite")
    assert refused.status_code == 403
    assert f"only {OWNER}, who dispatched its run, or an admin of project {PROJECT}" in refused.json()["message"]

    answered = answer_of(client, lead, decision_id, option="postgres")
    assert answered.status_code == 200, answered.text
    decision = answered.json()
    assert (decision["state"], decision["answer_option"], decision["answered_by"]) == ("answered", "postgres", LEAD)
    (event,) = admin_events(hub_db, run_id)
    assert (event["admin_action"], event["by"], event["as"], event["dispatched_by"], event["decision_id"]) == (
        "answer",
        LEAD,
        admin_acts.PROJECT_ADMIN,
        OWNER,
        decision_id,
    )
    assert event["text"] == (
        f"{LEAD}, an admin of project {PROJECT}, answered decision #{decision_id} of it with postgres for {OWNER}, "
        "who dispatched it"
    )
    ((notice_run, title, details),) = notices_of(hub_db, OWNER)
    assert notice_run == run_id and title.startswith(f"Run #{run_id}: {LEAD}, an admin of project {PROJECT}")
    assert (details["action"], details["by"], details["decision_id"]) == ("answer", LEAD, decision_id)
    assert notices_of(hub_db, LEAD) == [], "the admin is not told what they did"
    target = audit_of(hub_db, "decision.answer")[-1][0]
    assert target.endswith(f"option=postgres for={OWNER} as=project_admin"), target


def test_the_dispatchers_own_answer_names_no_admin(client, hub, hub_db, lead):
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id, decision_id = asking_plan_run(client, hub, worker)
    assert answer_of(client, hub["owner"], decision_id, option="sqlite").status_code == 200
    assert admin_events(hub_db, run_id) == [] and notices_of(hub_db, OWNER) == []
    assert "for=" not in audit_of(hub_db, "decision.answer")[-1][0]


def test_an_admin_gives_a_credential_as_their_own_secret_lent_to_that_run_alone(client, hub, hub_db, lead):
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id, asked = credential_run(client, hub, worker, {"env_var": NEEDED, "hub_account": True})
    park(client, hub_db, run_id)
    owners_before = set(secrets_of(client, hub["owner"]))
    value = sample("deploy-key-")

    # the dispatcher's own account is theirs to lend, not the admin's
    for option, body in ((credential_requests.AS_ME, {}), (credential_requests.BOTH, {"secret": value})):
        refused = answer_of(client, lead, asked["id"], option=option, **body)
        assert refused.status_code == 403, (option, refused.text)
        assert f"only {OWNER}, who dispatched run {run_id}, lends it their own secret or account" in refused.text
        assert value not in refused.text
    assert state_of(hub_db, run_id) == "parked", "a refusal changes nothing"
    # nor may a member who is no admin give the value
    assert answer_of(client, hub["other"], asked["id"], secret=value).status_code == 403

    answered = answer_of(client, lead, asked["id"], secret=value)
    assert answered.status_code == 200, answered.text
    decision = answered.json()
    assert (decision["answer_option"], decision["answered_by"]) == ("value", LEAD)
    assert f"their own new secret {NEEDED_SECRET} of kind env" in decision["answer_text"]
    assert f"never as {OWNER}'s" in decision["answer_text"] and value not in answered.text
    # the admin's secret, bound to no project; nothing new under the dispatcher's name
    secret = secrets_of(client, lead)[NEEDED_SECRET]
    assert (secret["kind"], secret["env_var"], secret["projects"], secret["workers"]) == ("env", NEEDED, [], [])
    assert set(secrets_of(client, hub["owner"])) == owners_before
    cs = tables.credential_shares
    ((share_id, share_run, grantee_kind),) = sql(hub_db, select(cs.c.id, cs.c.run_id, cs.c.grantee_kind))
    assert (share_run, grantee_kind) == (run_id, "user")
    assert audit_of(hub_db, "share.create")[-1][0].endswith(f"projects={PROJECT} run={run_id}")

    # the run that resumes the parked one is lent the value under the share
    resumed = claim(client, worker)
    assert resumed["resume_of_run_id"] == run_id and decision["answer_run_id"] == resumed["id"]
    lease = by_name(leased(client, worker, resumed["id"]))[NEEDED_SECRET]
    assert (lease["env_var"], lease["value"]) == (NEEDED, value)
    assert view(client, hub["owner"], resumed["id"])["as_owner"] is False, "the admin let no run run as the owner"
    (event,) = admin_events(hub_db, run_id)
    assert (event["admin_action"], event["by"]) == ("answer", LEAD)
    assert [notice[0] for notice in notices_of(hub_db, OWNER)] == [run_id]

    # another run of the dispatcher's is lent nothing of it
    moved(client, worker, resumed["id"], "running", "verifying", "done")
    other_run = client.post(
        f"/v1/projects/{PROJECT}/runs", json={"plan_id": PLAN, "steps": [STEPS["notes"]]}, headers=hub["owner"]
    )
    assert other_run.status_code == 201, other_run.text
    next_id = claim(client, worker)["id"]
    assert NEEDED_SECRET not in by_name(leased(client, worker, next_id))
    assert share_id and value not in table_dump(hub_db)


def test_an_admin_who_keeps_a_secret_of_the_proposed_name_is_refused_and_nothing_is_written(client, hub, hub_db, lead):
    old = sample("in-use-")
    credentials_api.env_secret(client, lead, NEEDED_SECRET, NEEDED, old)  # the admin's own, which their runs lease
    before = secrets_of(client, lead)[NEEDED_SECRET]
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id, asked = credential_run(client, hub, worker, {"env_var": NEEDED})
    value = sample("deploy-key-")

    refused = answer_of(client, lead, asked["id"], secret=value)
    assert refused.status_code == 409, refused.text
    message = refused.json()["message"]
    assert f"you keep a secret named {NEEDED_SECRET} already" in message and "would be lost" in message
    assert f"`evo-agents hub run cancel {run_id} --project {PROJECT}`" in message
    assert f"`evo-agents hub decision answer {asked['id']} --option refuse --project {PROJECT}`" in message
    assert value not in refused.text and old not in refused.text
    # nothing was written: the secret, the decision, the shares, the run's log and the dispatcher's notices
    assert secrets_of(client, lead)[NEEDED_SECRET] == before
    shown = client.get(f"/v1/projects/{PROJECT}/decisions/{asked['id']}", headers=lead).json()
    assert (shown["state"], shown["answered_by"]) == ("open", None)
    assert sql(hub_db, select(tables.credential_shares.c.id)) == []
    assert admin_events(hub_db, run_id) == [] and notices_of(hub_db, OWNER) == []
    assert audit_of(hub_db, "secret.put")[-1][0] != f"decision:{asked['id']}"
    assert value not in table_dump(hub_db)

    # refusing stays open to the admin
    declined = answer_of(client, lead, asked["id"], option=credential_requests.REFUSE)
    assert declined.status_code == 200, declined.text
    assert declined.json()["answer_option"] == credential_requests.REFUSE
    assert [event["admin_action"] for event in admin_events(hub_db, run_id)] == ["answer"]


def test_an_admin_cancels_another_members_parked_plan_run_and_dispatches_the_plan_again(client, hub, hub_db, lead):
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id, _ = asking_plan_run(client, hub, worker)
    park(client, hub_db, run_id)

    busy = client.post(f"/v1/projects/{PROJECT}/plan-runs", json={"plan_id": PLAN}, headers=lead)
    assert busy.status_code == 409
    assert f"cancel it first with `evo-agents hub run cancel {run_id}`" in busy.json()["message"]
    other = control(client, hub["other"], run_id, "cancel")
    assert other.status_code == 403
    assert other.json()["message"] == (
        f"only {OWNER}, who dispatched run {run_id}, or an admin of project {PROJECT}, may cancel it"
    )

    cancelled = control(client, lead, run_id, "cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"
    e = tables.run_events
    states = sql(hub_db, select(e.c.body).where(e.c.run_id == run_id, e.c.kind == "state").order_by(e.c.seq))
    last = states[-1][0]
    assert (last["to"], last["actor"]) == ("cancelled", "owner")
    assert last["reason"] == f"{LEAD}, an admin of project {PROJECT}, cancelled it for {OWNER}, who dispatched it"
    (event,) = admin_events(hub_db, run_id)
    assert (event["admin_action"], event["as"], event["dispatched_by"]) == ("cancel", "project_admin", OWNER)
    assert [notice[0] for notice in notices_of(hub_db, OWNER)] == [run_id]
    assert audit_of(hub_db, "run.cancel")[-1][0].endswith(f"for={OWNER} as=project_admin")

    again = client.post(f"/v1/projects/{PROJECT}/plan-runs", json={"plan_id": PLAN}, headers=lead)
    assert again.status_code == 201, again.text
    assert again.json()["dispatched_by"] == LEAD


def failed_step_run(client, hub, worker: dict) -> int:
    body = {"plan_id": PLAN, "steps": [STEPS["notes"]]}
    dispatched = client.post(f"/v1/projects/{PROJECT}/runs", json=body, headers=hub["owner"])
    assert dispatched.status_code == 201, dispatched.text
    run_id = claim(client, worker)["id"]
    moved(client, worker, run_id, "running", "failed", error="it stopped", failure_cause="verify_failed")
    return run_id


def test_an_admin_lists_candidates_and_reruns_another_members_failed_run_as_their_own(client, hub, hub_db, lead):
    worker = worker_of(client, hub["owner"], "mac-mini")
    leads = worker_of(client, lead, "lead-box")
    run_id = failed_step_run(client, hub, worker)
    r = tables.runs
    sql(hub_db, update(r).values(as_owner=True, as_owner_since=func.now()).where(r.c.id == run_id))

    url = f"/v1/projects/{PROJECT}/runs/{run_id}/rerun-candidates"
    assert client.get(url, headers=hub["other"]).status_code == 403
    owners = client.get(url, headers=hub["owner"])
    assert owners.status_code == 200 and [w["worker"] for w in owners.json()["workers"]] == ["mac-mini"]
    theirs = client.get(url, headers=lead)
    assert theirs.status_code == 200, theirs.text
    assert [(w["worker"], w["shared"]) for w in theirs.json()["workers"]] == [("lead-box", False)], (
        "the admin's rerun runs on the admin's workers"
    )
    assert admin_events(hub_db, run_id) == [], "a read is no act"

    assert control(client, hub["other"], run_id, "rerun").status_code == 403
    rerun = control(client, lead, run_id, "rerun")
    assert rerun.status_code == 201, rerun.text
    new = rerun.json()
    assert (new["dispatched_by"], new["as_owner"], new["state"]) == (LEAD, False, "queued")
    (event,) = admin_events(hub_db, run_id)
    assert (event["admin_action"], event["rerun_id"]) == ("rerun", new["id"])
    assert event["text"].startswith(f"{LEAD}, an admin of project {PROJECT}, ran it again as run #{new['id']}")
    assert [notice[0] for notice in notices_of(hub_db, OWNER)] == [run_id]
    assert claim(client, leads)["id"] == new["id"], "the admin's worker takes it"


def test_an_admin_approves_and_a_hub_admin_cancels_another_members_run(client, hub, hub_db, lead):
    worker = worker_of(client, hub["owner"], "mac-mini")
    body = {"plan_id": PLAN, "steps": [STEPS["notes"]], "approval": "review"}
    dispatched = client.post(f"/v1/projects/{PROJECT}/runs", json=body, headers=hub["owner"])
    assert dispatched.status_code == 201, dispatched.text
    run_id = claim(client, worker)["id"]
    moved(client, worker, run_id, "running", "verifying", "review")
    assert control(client, hub["other"], run_id, "approve").status_code == 403
    approved = control(client, lead, run_id, "approve")
    assert approved.status_code == 200 and approved.json()["state"] == "done", approved.text
    assert [event["admin_action"] for event in admin_events(hub_db, run_id)] == ["approve"]

    queued = client.post(
        f"/v1/projects/{PROJECT}/runs", json={"plan_id": PLAN, "steps": [STEPS["evo-agents"]]}, headers=hub["owner"]
    )
    assert queued.status_code == 201, queued.text
    queued_id = queued.json()[0]["id"]
    # a hub admin with no grant on the project sees none of its runs, as before; with any grant they act as an admin
    unseen = control(client, hub["admin"], queued_id, "cancel")
    assert unseen.status_code == 403 and "needs a grant on it" in unseen.json()["message"]
    reader = {"role": "reader", "max_level": "internal"}
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{live.ADMIN}", json=reader, headers=hub["admin"])
    assert granted.status_code == 200, granted.text
    cancelled = control(client, hub["admin"], queued_id, "cancel")
    assert cancelled.status_code == 200 and cancelled.json()["state"] == "cancelled", cancelled.text
    (event,) = admin_events(hub_db, queued_id)
    assert (event["as"], event["text"]) == (
        admin_acts.HUB_ADMIN,
        f"{live.ADMIN}, a hub admin, cancelled it for {OWNER}, who dispatched it",
    )


def _access(role: str | None) -> ProjectAccess:
    return ProjectAccess(1, ProjectRules(PROJECT, [], [], [], None), role, None)


def test_only_an_admin_signed_in_as_themself_acts_on_another_members_run():
    member = Principal(user_id=7, login=LEAD, admin=False, token_id=1, kind=MACHINE, token_hash="h")
    assert admin_acts.standing(member, _access("admin")) == admin_acts.PROJECT_ADMIN
    assert admin_acts.standing(member, _access("writer")) is None
    hub_admin = Principal(user_id=8, login="octo", admin=True, token_id=2, kind=MACHINE, token_hash="h")
    assert admin_acts.standing(hub_admin, _access(None)) == admin_acts.HUB_ADMIN
    token = Principal(user_id=7, login=LEAD, admin=True, token_id=3, kind=RUN, token_hash="h", run_id=42)
    assert admin_acts.standing(token, _access("admin")) is None, "a run's token never acts as an admin"
    assert admin_acts.on_behalf(member, _access("writer"), 7, LEAD) is None, "one's own run"
    with pytest.raises(LookupError):
        admin_acts.on_behalf(member, _access("writer"), 9, OWNER)
