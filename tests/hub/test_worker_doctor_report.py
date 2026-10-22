"""The doctor reports of workers (``evo_agents.hub.server.worker_doctor``), as criterion a6 of plan credential-sharing
names them: schema 0108 and going back; POST /v1/worker/doctor keeps the newest report of the worker whose token sent
it, redacted, and the list of workers shows it; narrowing a worker to a unit answers 409 with the reason without a
report, with one older than 48 hours or with a high finding; a report with a high finding stops a narrowed worker
taking the runs of its units' members, never its owner's, and tells the owner once with the notice worker_doctor.

Everything here needs the hub and skips without EVO_HUB_TEST_DSN."""

import pytest

from evo_agents.hub import runs
from tests.hub import pg

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)

if pg.DSN:
    import psycopg
    from sqlalchemy import func, insert, select, update

    from evo_agents.hub import tables
    from evo_agents.hub.migrate import migrate
    from tests.hub.live import sql
    from tests.hub.test_migrate import ALL, columns, move_to
    from tests.hub.test_runs import (  # noqa: F401  (fixtures)
        OTHER,
        OWNER,
        PROTOCOL,
        add_worker,
        claim,
        client,
        dispatched,
        hub,
        moved,
    )
    from tests.hub.test_worker_share import doctor, seat, unit

HIGH = {"code": "other-admin", "severity": "high", "detail": "carol administers this machine (group sudo)."}
MEDIUM = {"code": "machine-token-on-worker", "severity": "medium", "detail": "~/.evo/hub/token is a machine token."}
SECRET = "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
LEAKY = {"code": "gh-token-file", "severity": "high", "detail": "hosts.yml holds ghp_" + SECRET}
DOCTOR_COLUMNS = {"doctor_at", "doctor_high", "doctor_medium", "doctor_findings"}


def put_unit(client, headers, worker: dict, slug: str):
    return client.put(f"/v1/org/workers/{worker['id']}/units/{slug}", headers=headers)


def org_row(client, headers, worker: dict) -> dict:
    response = client.get("/v1/org/workers", headers=headers)
    assert response.status_code == 200, response.text
    return next(row for row in response.json() if row["id"] == worker["id"])


def notices(db) -> list[tuple]:
    n, u = tables.notifications, tables.users
    query = (
        select(u.c.login, n.c.title, n.c.details, n.c.link)
        .join_from(n, u, u.c.id == n.c.user_id)
        .where(n.c.notice_kind == "worker_doctor")
        .order_by(n.c.id)
    )
    return sql(db, query)


def test_doctor_report_is_a_feature_of_the_daemon():
    assert runs.DOCTOR_REPORT == "doctor_report" and runs.DOCTOR_REPORT in runs.WORKER_FEATURES
    assert runs.DOCTOR_REPORT in runs.HUB_FEATURES and "worker_doctor" in runs.NOTICE_KINDS
    assert (runs.DOCTOR_EVERY_SECONDS, runs.DOCTOR_FRESH_SECONDS) == (24 * 3600, 48 * 3600)


@needs_pg
def test_doctor_report_migration_adds_the_columns_and_the_notice_and_going_back_removes_them(client, hub, hub_db):
    assert DOCTOR_COLUMNS <= columns(hub_db, "workers")
    lab = add_worker(client, hub["owner"], "lab")
    doctor(client, lab, HIGH)
    users = tables.users
    owner_id = sql(hub_db, select(users.c.id).where(users.c.login == OWNER))[0][0]
    notice = {"user_id": owner_id, "kind": "notice", "notice_kind": "worker_doctor", "title": "Worker lab"}
    sql(hub_db, insert(tables.notifications).values(**notice))
    move_to(hub_db, "0107", down=True)
    assert not DOCTOR_COLUMNS & columns(hub_db, "workers")
    n = tables.notifications
    assert sql(hub_db, select(func.count()).select_from(n).where(n.c.notice_kind == "worker_doctor")) == [(0,)]
    with pytest.raises(psycopg.errors.CheckViolation, match="notifications_notice_kind_check"):
        sql(hub_db, insert(n).values(**notice))
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0108") :]
    assert DOCTOR_COLUMNS <= columns(hub_db, "workers")


@needs_pg
def test_doctor_report_is_kept_redacted_listed_and_replaced_by_the_next(client, hub, hub_db):
    lab = add_worker(client, hub["owner"], "lab")
    assert org_row(client, hub["owner"], lab)["doctor_at"] is None
    answer = doctor(client, lab, LEAKY, MEDIUM, {**MEDIUM, "code": "path-not-owned"})
    assert (answer["worker_id"], answer["doctor_high"], answer["doctor_medium"]) == (lab["id"], 1, 2)
    row = org_row(client, hub["owner"], lab)
    assert (row["doctor_at"], row["doctor_high"], row["doctor_medium"]) == (answer["doctor_at"], 1, 2)
    w = tables.workers
    ((kept,),) = sql(hub_db, select(w.c.doctor_findings).where(w.c.id == lab["id"]))
    assert [finding["code"] for finding in kept] == ["gh-token-file", "machine-token-on-worker", "path-not-owned"]
    assert set(kept[0]) == {"code", "severity", "detail"}
    assert SECRET not in kept[0]["detail"] and "***" in kept[0]["detail"]  # a credential never reaches the hub's row
    later = doctor(client, lab)
    row = org_row(client, hub["owner"], lab)
    assert (row["doctor_high"], row["doctor_medium"]) == (0, 0) and row["doctor_at"] >= answer["doctor_at"]
    assert later["doctor_high"] == 0


@needs_pg
def test_doctor_report_takes_only_a_live_workers_token_and_well_formed_findings(client, hub):
    lab = add_worker(client, hub["owner"], "lab")
    sent = client.post("/v1/worker/doctor", json={"findings": []}, headers={**hub["owner"], **PROTOCOL})
    assert sent.status_code == 403, sent.text  # a member's token is no worker's
    for bad in ({**HIGH, "severity": "urgent"}, {**HIGH, "code": "Not A Code"}, {**HIGH, "detail": "x" * 2001}):
        refused = client.post("/v1/worker/doctor", json={"findings": [bad]}, headers=lab["headers"])
        assert refused.status_code == 422, refused.text
    many = client.post("/v1/worker/doctor", json={"findings": [MEDIUM] * 101}, headers=lab["headers"])
    assert many.status_code == 422, many.text


@needs_pg
def test_doctor_report_missing_stale_or_high_refuses_sharing_with_a_unit(client, hub, hub_db):
    unit(client, hub, "lab")
    lab = add_worker(client, hub["owner"], "lab")
    refused = put_unit(client, hub["owner"], lab, "lab")
    assert refused.status_code == 409 and "sent no doctor report" in refused.text  # an older daemon sends none
    doctor(client, lab, HIGH, MEDIUM)
    refused = put_unit(client, hub["owner"], lab, "lab")
    assert refused.status_code == 409 and "1 high finding" in refused.text
    doctor(client, lab, MEDIUM)  # medium alone does not stop sharing
    w = tables.workers
    sql(hub_db, update(w).where(w.c.id == lab["id"]).values(doctor_at=func.now() - func.make_interval(0, 0, 0, 0, 49)))
    refused = put_unit(client, hub["admin"], lab, "lab")
    assert refused.status_code == 409 and "more than 48 hours ago" in refused.text
    doctor(client, lab, MEDIUM)
    shared = put_unit(client, hub["owner"], lab, "lab")
    assert shared.status_code == 200, shared.text
    assert (shared.json()["units"], shared.json()["doctor_medium"]) == (["lab"], 1)


@needs_pg
def test_doctor_report_with_high_stops_unit_runs_keeps_the_owners_and_notifies_the_owner_once(client, hub, hub_db):
    unit(client, hub, "lab")
    seat(client, hub, "lab")
    lab = add_worker(client, hub["owner"], "lab")
    doctor(client, lab)
    assert put_unit(client, hub["owner"], lab, "lab").status_code == 200
    (first,) = dispatched(client, hub["other"], [4])
    assert claim(client, lab)["id"] == first["id"]
    moved(client, lab, first["id"], "running", "failed", error="it stopped")
    doctor(client, lab, HIGH)
    (theirs,) = dispatched(client, hub["other"], [5])
    assert claim(client, lab) is None  # the member's run waits
    (mine,) = dispatched(client, hub["owner"], [2])
    assert claim(client, lab)["id"] == mine["id"]  # its owner's runs go on
    moved(client, lab, mine["id"], "running", "failed", error="it stopped")
    doctor(client, lab, HIGH, LEAKY)  # still high: no second notice
    ((login, title, details, link),) = notices(hub_db)
    assert (login, link) == (OWNER, f"/workers/{lab['id']}")
    assert details == {"worker_id": lab["id"], "worker": "lab", "codes": ["other-admin"]}
    assert "stopped taking shared runs" in title
    assert claim(client, lab) is None
    doctor(client, lab, MEDIUM)  # fixed: the member's runs come back
    assert claim(client, lab)["id"] == theirs["id"]
    assert len(notices(hub_db)) == 1


@needs_pg
def test_doctor_report_with_high_on_a_worker_not_narrowed_changes_nothing_and_notifies_nobody(client, hub, hub_db):
    lab = add_worker(client, hub["owner"], "lab")
    patched = client.patch(f"/v1/workers/{lab['id']}", json={"shared": True}, headers=hub["owner"])
    assert patched.status_code == 200, patched.text
    doctor(client, lab, HIGH)
    (theirs,) = dispatched(client, hub["other"], [4])
    assert claim(client, lab)["id"] == theirs["id"]  # shared with every writer: the doctor does not narrow it
    assert notices(hub_db) == []
    assert OTHER != OWNER
