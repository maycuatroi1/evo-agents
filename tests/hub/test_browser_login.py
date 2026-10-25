"""Login sessions of browser identities on the hub (plan agent-browser, criterion a7): a run of kind browser with no
agent and no repo, queued on the worker that holds the identity (or, for a Move, on another one), claimed only by a
daemon that says it runs the kind, holding the identity from its claim. Save (the finish of the run) is what seals the
profile and raises the version; the profile of a Move goes to the new worker; cancelling saves nothing. An identity
that turns needs_login sends its owner a notice with a link to log it in, and disable and enable switch it off and on."""

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import browser_login, runs, tables
from tests.hub.live import sql
from tests.hub.test_browser_claim import REPORT, add_browser_worker, browser_beat
from tests.hub.test_browser_identity import PATH, audit_rows, created, opened, stored, user_id  # noqa: F401 (fixtures)
from tests.hub.test_browser_profile import (
    held_by,
    lease,
    profile_archive,
    save,
    send,
    set_identity,
)
from tests.hub.test_runs import OTHER, OWNER, PLAN, PROJECT, claim, control, dispatched, moved, of_run

KINDS = ("step", "browser")


def login_worker(client, headers, name: str, *, kinds=KINDS) -> dict:
    """A worker with the feature browser whose daemon says it runs the kinds ``kinds``."""
    worker = add_browser_worker(client, headers, name)
    assert browser_beat(client, worker, REPORT, run_kinds=kinds).status_code == 200
    return worker


def login(client, headers, identity_id: int, **body):
    return client.post(f"{PATH}/{identity_id}/login", json=body, headers=headers)


def queued(client, headers, identity_id: int, **body) -> dict:
    response = login(client, headers, identity_id, **body)
    assert response.status_code == 201, response.text
    return response.json()


def identity_row(db, identity_id: int):
    b = tables.browser_identities
    query = select(b.c.version, b.c.status, b.c.worker_id, b.c.held_by_run).where(b.c.id == identity_id)
    return sql(db, query)[0]


def notices(db, notice_kind: str) -> list[tuple]:
    n = tables.notifications
    query = select(n.c.user_id, n.c.title, n.c.link, n.c.project_id).where(n.c.notice_kind == notice_kind)
    return sql(db, query.order_by(n.c.id))


def test_browser_login_session_is_claimed_by_the_identity_worker_and_save_seals_the_profile(stored, tmp_path):
    client, headers, db = stored.client, stored.headers, stored.db
    box = login_worker(client, headers["owner"], "forge")
    identity = created(client, headers["owner"], "northwind-admin", worker="forge", domains=("northwind.example",))
    run = queued(client, headers["owner"], identity["id"])
    assert (run["kind"], run["state"], run["browser"], run["browser_identities"]) == (
        "browser",
        "queued",
        True,
        [identity["id"]],
    )
    assert run["title"] == "Log in browser identity northwind-admin"
    assert of_run(db, run["id"], "pinned_worker_id", "timeout_s", "max_attempts", "repos") == [
        (box["id"], browser_login.TIMEOUT_MINUTES * 60, 1, None)
    ]
    assert login(client, headers["owner"], identity["id"]).status_code == 409  # a session waits already

    spec = claim(client, box)
    assert spec["id"] == run["id"] and spec["kind"] == "browser" and spec["browser_identities"] == [identity["id"]]
    assert spec["timeout_min"] == browser_login.TIMEOUT_MINUTES
    assert held_by(db, identity["id"]) == run["id"]
    leased = lease(client, box, run["id"], identity["id"]).json()
    assert (leased["version"], leased["profile"], leased["login_url"]) == (0, None, "https://northwind.example/")

    moved(client, box, run["id"], "interactive")
    finish = control(client, headers["owner"], run["id"], "finish")
    assert finish.status_code == 200, finish.text
    archive = profile_archive(tmp_path, "northwind_session")
    assert send(client, box, run["id"], identity["id"], archive, 0).json()["version"] == 1
    moved(client, box, run["id"], "verifying", "done")
    assert identity_row(db, identity["id"]) == (1, "active", box["id"], None)
    assert opened(stored, identity["id"]) == (1, archive)
    actions = [row[0] for row in audit_rows(db)]
    assert actions.count("browser_identity.login") == 1 and "browser_identity.save" in actions


def test_browser_login_session_cancel_and_timeout_save_nothing(stored, tmp_path):
    client, headers, db = stored.client, stored.headers, stored.db
    box = login_worker(client, headers["owner"], "forge")
    body = {"name": "northwind-admin", "domains": ["x.test"], "worker": "forge", "login_url": "https://x.test/login"}
    identity = client.post(PATH, json=body, headers=headers["owner"]).json()
    assert identity["login_url"] == "https://x.test/login"
    bad = client.post(PATH, json={**body, "name": "bad", "login_url": "ftp://x.test/"}, headers=headers["owner"])
    assert bad.status_code == 422
    assert save(stored, identity["id"], 0, profile_archive(tmp_path, "before")) == 1
    run = queued(client, headers["owner"], identity["id"])
    assert claim(client, box)["id"] == run["id"]
    leased = lease(client, box, run["id"], identity["id"]).json()
    assert (leased["version"], leased["login_url"]) == (1, "https://x.test/login")
    assert leased["profile"] is not None
    moved(client, box, run["id"], "interactive")
    assert control(client, headers["owner"], run["id"], "cancel").status_code == 200
    moved(client, box, run["id"], "cancelled")
    assert identity_row(db, identity["id"]) == (1, "active", box["id"], None)

    again = queued(client, headers["owner"], identity["id"])
    assert claim(client, box)["id"] == again["id"]
    moved(client, box, again["id"], "interactive", "failed", error="it ran past its timeout", failure_cause="timeout")
    assert identity_row(db, identity["id"]) == (1, "active", box["id"], None)
    assert opened(stored, identity["id"])[0] == 1


def test_browser_login_session_move_hands_no_profile_and_the_identity_moves_after_save(stored, tmp_path):
    client, headers, db = stored.client, stored.headers, stored.db
    box = login_worker(client, headers["owner"], "forge")
    lab = login_worker(client, headers["owner"], "lab")
    identity = created(client, headers["owner"], "northwind-admin", worker="forge")
    assert save(stored, identity["id"], 0, profile_archive(tmp_path, "before")) == 1
    refused = login(client, headers["owner"], identity["id"], worker="nowhere")
    assert refused.status_code == 422

    run = queued(client, headers["owner"], identity["id"], worker="lab")
    assert run["title"] == "Move browser identity northwind-admin on lab"
    assert claim(client, box) is None  # the session is lab's
    assert claim(client, lab)["id"] == run["id"]
    leased = lease(client, lab, run["id"], identity["id"]).json()
    assert (leased["version"], leased["profile"]) == (1, None)  # a profile does not leave its worker
    assert control(client, headers["owner"], run["id"], "finish").status_code == 200
    moved(client, lab, run["id"], "interactive")
    saved = send(client, lab, run["id"], identity["id"], profile_archive(tmp_path, "after"), 1)
    assert saved.status_code == 200, saved.text
    moved(client, lab, run["id"], "verifying", "done")
    assert identity_row(db, identity["id"]) == (2, "active", lab["id"], None)


def test_browser_login_session_is_refused_for_what_cannot_take_it(stored):
    client, headers, db = stored.client, stored.headers, stored.db
    box = login_worker(client, headers["owner"], "forge")
    old = login_worker(client, headers["owner"], "old", kinds=("step",))
    identity = created(client, headers["owner"], "northwind-admin", worker="forge")
    elder = created(client, headers["owner"], "lumen-ops", worker="old")
    refused = login(client, headers["owner"], elder["id"])
    assert refused.status_code == 409 and "does not say it runs browser" in refused.json()["message"]
    assert claim(client, old) is None
    assert login(client, headers["other"], identity["id"]).status_code == 404  # another member's
    assert login(client, headers["owner"], 99999).status_code == 404
    set_identity(db, identity["id"], status="disabled")
    disabled = login(client, headers["owner"], identity["id"])
    assert disabled.status_code == 409 and "disabled" in disabled.json()["message"]
    set_identity(db, identity["id"], status="active", held_by_run=None)
    run = queued(client, headers["owner"], identity["id"])
    assert claim(client, box)["id"] == run["id"]
    assert login(client, headers["owner"], identity["id"]).status_code == 409  # held by the session
    assert control(client, headers["owner"], run["id"], "cancel").status_code == 200

    assert runs.RUN_KINDS[-1] == "browser"


def test_browser_login_session_finish_is_for_author_and_browser_runs_only(stored):
    client, headers = stored.client, stored.headers
    box = login_worker(client, headers["owner"], "forge")
    identity = created(client, headers["owner"], "northwind-admin", worker="forge")
    run = queued(client, headers["owner"], identity["id"])
    early = control(client, headers["owner"], run["id"], "finish")
    assert early.status_code == 409  # queued: nothing runs yet
    assert claim(client, box)["id"] == run["id"]
    moved(client, box, run["id"], "interactive")
    asked = control(client, headers["owner"], run["id"], "finish")
    assert asked.status_code == 200 and asked.json()["id"] == run["id"]
    assert control(client, headers["other"], run["id"], "finish").status_code == 403
    assert of_run(stored.db, run["id"], "finish_requested_at")[0][0] is not None
    plan = client.post(f"/v1/projects/{PROJECT}/runs/{run['id']}/finish", headers=headers["owner"])
    assert plan.status_code == 200  # asked again: nothing changes


def test_needs_login_sends_the_owner_a_notice_with_a_log_in_link(stored, tmp_path):
    client, headers, db = stored.client, stored.headers, stored.db
    box = login_worker(client, headers["owner"], "forge")
    identity = created(client, headers["owner"], "northwind-admin", worker="forge")
    assert save(stored, identity["id"], 0, profile_archive(tmp_path, "northwind")) == 1
    (run,) = dispatched(client, headers["owner"], [4], identities=["northwind-admin"])
    assert claim(client, box)["id"] == run["id"]
    path = f"/v1/worker/runs/{run['id']}/browser-profile/{identity['id']}/needs-login"
    assert client.post(path, json={"cause": "unopenable"}, headers=box["headers"]).status_code == 200
    found = notices(db, "browser_needs_login")
    assert found == [
        (
            user_id(db, OWNER),
            "Browser identity northwind-admin needs a new login",
            f"/p/{PROJECT}/browser?login={identity['id']}",
            found[0][3],
        )
    ]
    assert user_id(db, OTHER) not in [row[0] for row in found]


def test_needs_login_identity_is_switched_off_and_on_by_its_manager(stored, tmp_path):
    client, headers, db = stored.client, stored.headers, stored.db
    identity = created(client, headers["owner"], "northwind-admin")
    off = client.post(f"{PATH}/{identity['id']}/disable", headers=headers["owner"])
    assert off.status_code == 200 and off.json()["status"] == "disabled"
    assert client.post(f"{PATH}/{identity['id']}/disable", headers=headers["other"]).status_code == 404
    on = client.post(f"{PATH}/{identity['id']}/enable", headers=headers["owner"])
    assert on.json()["status"] == "needs_login"  # never saved: it waits for its first login
    assert client.post(f"{PATH}/{identity['id']}/enable", headers=headers["owner"]).status_code == 409
    assert save(stored, identity["id"], 0, profile_archive(tmp_path, "northwind")) == 1
    client.post(f"{PATH}/{identity['id']}/disable", headers=headers["owner"])
    assert client.post(f"{PATH}/{identity['id']}/enable", headers=headers["owner"]).json()["status"] == "active"
    actions = [row[0] for row in audit_rows(db)]
    assert actions.count("browser_identity.disable") == 2 and actions.count("browser_identity.enable") == 2


def browser_login_ask(stored, box: dict, run_id: int, identity: str, **extra):
    body = {"identity": identity, "url": "https://northwind.example/login", "reason": "the session expired", **extra}
    return stored.client.post(f"/v1/worker/runs/{run_id}/browser-login", json=body, headers=box["headers"])


def test_login_decision_of_a_plan_run_is_open_once_with_done_and_skip_and_the_live_view(stored):
    client, headers, db, box = stored.client, stored.headers, stored.db, stored.box
    identity = created(client, headers["owner"], "northwind-admin", domains=("northwind.example",))
    run = client.post(
        f"/v1/projects/{PROJECT}/plan-runs",
        json={"plan_id": PLAN, "identities": ["northwind-admin"]},
        headers=headers["owner"],
    )
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]
    assert claim(client, box)["id"] == run_id
    moved(client, box, run_id, "running")

    asked = browser_login_ask(stored, box, run_id, "northwind-admin")
    assert asked.status_code == 201, asked.text
    first = asked.json()
    assert first["outcome"] == "asked" and first["identity_id"] == identity["id"] and first["decision_id"]
    again = browser_login_ask(stored, box, run_id, str(identity["id"]))
    assert again.json()["decision_id"] == first["decision_id"]  # one open login decision at a time

    d = tables.decisions
    row = sql(db, select(d.c.category, d.c.state, d.c.options, d.c.context).where(d.c.run_id == run_id))
    assert len(row) == 1
    category, state, options, context = row[0]
    reason = sql(db, select(d.c.recommended_reason).where(d.c.run_id == run_id))
    assert reason == [(browser_login.LOGIN_REASON,)], "the hub recommends signing in, and says why"
    assert [o["key"] for o in options if o.get("recommended")] == [browser_login.LOGIN_DONE]
    assert (category, state) == (browser_login.CATEGORY, "open")
    assert [o["key"] for o in options] == [browser_login.LOGIN_DONE, browser_login.LOGIN_SKIP]
    assert "northwind.example/login" in context and "the session expired" in context and f"/runs/{run_id}" in context

    unknown = browser_login_ask(stored, box, run_id, "nowhere")
    assert unknown.status_code == 422 and "no browser identity" in unknown.json()["message"]
    bad_url = browser_login_ask(stored, box, run_id, "northwind-admin", url="file:///etc/passwd")
    assert bad_url.status_code == 422


def test_browser_login_by_a_step_run_marks_the_identity_needs_login_and_fails_the_run(stored, tmp_path):
    client, headers, db, box = stored.client, stored.headers, stored.db, stored.box
    identity = created(client, headers["owner"], "northwind-admin", domains=("northwind.example",))
    assert save(stored, identity["id"], 0, profile_archive(tmp_path, "northwind")) == 1
    (run,) = dispatched(client, headers["owner"], [4], identities=["northwind-admin"])
    assert claim(client, box)["id"] == run["id"]
    moved(client, box, run["id"], "running")

    failed = browser_login_ask(stored, box, run["id"], "northwind-admin")
    assert failed.status_code == 201, failed.text
    assert failed.json()["outcome"] == "failed" and failed.json()["decision_id"] is None
    assert "log" in failed.json()["message"].lower()
    r, b = tables.runs, tables.browser_identities
    assert sql(db, select(r.c.state).where(r.c.id == run["id"])) == [("failed",)]
    assert sql(db, select(b.c.status).where(b.c.id == identity["id"])) == [("needs_login",)]
    assert sql(db, select(tables.decisions.c.id).where(tables.decisions.c.run_id == run["id"])) == []
