"""Shared workers narrowed to units of the org chart (``evo_agents.hub.server.worker_shares``), as criterion a5 of plan
credential-sharing names them: schema 0107 and going back; the owner and a hub admin narrow a worker to a unit and
take it back, each change audited worker.share or worker.unshare; a narrowed worker takes the runs of its owner and of
a member whose live primary seat is in one of its units or below one, and no other member's, at claim, at dispatch,
in the worker lists and in the candidates of a rerun; a shared worker with no unit keeps taking every writer's runs,
and PATCH {"shared": ...} still turns sharing on and off; ``hub org worker list|share|unshare``.

The parsing runs without Postgres; everything that needs the hub skips without EVO_HUB_TEST_DSN."""

import pytest

from evo_agents.hub.cli_org_workers import Unknown, find_worker, worker_line
from evo_agents.hub.contract import commands
from tests.hub import pg

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)

if pg.DSN:
    from sqlalchemy import select

    from evo_agents.hub import tables
    from evo_agents.hub.migrate import migrate
    from tests.hub.live import sql
    from tests.hub.test_migrate import ALL, INFO_TABLES, move_to
    from tests.hub.test_runs import (  # noqa: F401  (fixtures)
        ADMIN,
        OTHER,
        OWNER,
        PROJECT,
        add_worker,
        audit_rows,
        claim,
        client,
        dispatch,
        dispatched,
        hub,
        moved,
    )

KEYS = ["id", "worker", "owner", "shared", "units", "doctor_at", "doctor_high", "doctor_medium"]


# Without a hub


def test_worker_share_cli_commands_are_in_the_contract():
    printed = commands()
    for name in ("share", "unshare"):
        command = printed[f"hub org worker {name}"]
        assert {option["flags"][0] for option in command["options"]} >= {"--unit", "--json"}
        assert (command["json"]["kind"], command["json"]["schema"]) == ("object", "WorkerShares")
        assert command["json"]["keys"] == KEYS
    listed = printed["hub org worker list"]["json"]
    assert (listed["kind"], listed["schema"], listed["keys"]) == ("array", "WorkerShares", KEYS)


def test_worker_share_cli_names_a_worker_by_name_or_owner_and_name():
    rows = [
        {"id": 1, "worker": "lab", "owner": "ann", "shared": True, "units": ["lab"], "doctor_at": None},
        {"id": 2, "worker": "lab", "owner": "bob", "shared": False, "units": [], "doctor_at": None},
        {"id": 3, "worker": "gpu", "owner": "bob", "shared": True, "units": [], "doctor_at": None},
    ]
    assert find_worker(rows, "gpu")["id"] == 3
    assert find_worker(rows, "bob/lab")["id"] == 2
    with pytest.raises(Unknown, match="2 workers are named lab: name one as OWNER/NAME"):
        find_worker(rows, "lab")
    with pytest.raises(Unknown, match="you share no live worker named carl/lab"):
        find_worker(rows, "carl/lab")
    assert worker_line(rows[0]) == "ann/lab  shared with lab  (no doctor report)"
    assert worker_line(rows[1]) == "bob/lab  not shared  (no doctor report)"
    assert worker_line(rows[2]) == "bob/gpu  shared with every writer  (no doctor report)"


# Against a hub


def unit(client, hub, slug: str, parent: str = "root") -> None:
    body = {"slug": slug, "parent": parent, "kind": "team", "name": slug}
    response = client.post("/v1/org/units", json=body, headers=hub["admin"])
    assert response.status_code == 201, response.text


def seat(client, hub, slug: str, login: str = OTHER) -> None:
    body = {"login": login, "unit": slug, "role": "member", "primary": True}
    response = client.post("/v1/org/seats", json=body, headers=hub["admin"])
    assert response.status_code in (200, 201), response.text


def doctor(client, worker: dict, *findings: dict) -> dict:
    """The worker's daemon sends a doctor report with ``findings`` (none: a clean one)."""
    response = client.post("/v1/worker/doctor", json={"findings": list(findings)}, headers=worker["headers"])
    assert response.status_code == 200, response.text
    return response.json()


def narrow(client, headers, worker: dict, slug: str, status: int = 200) -> dict:
    """Narrow ``worker`` to unit ``slug``, after a clean doctor report of its daemon, which narrowing needs."""
    if status == 200:
        doctor(client, worker)
    response = client.put(f"/v1/org/workers/{worker['id']}/units/{slug}", headers=headers)
    assert response.status_code == status, response.text
    return response.json()


def widen(client, headers, worker: dict, slug: str, status: int = 200) -> dict:
    response = client.delete(f"/v1/org/workers/{worker['id']}/units/{slug}", headers=headers)
    assert response.status_code == status, response.text
    return response.json()


def patch_shared(client, headers, worker: dict, shared: bool) -> None:
    response = client.patch(f"/v1/workers/{worker['id']}", json={"shared": shared}, headers=headers)
    assert response.status_code == 200, response.text


def listed(client, headers, worker: dict) -> list[int]:
    response = client.get("/v1/workers", headers=headers)
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json() if row["id"] == worker["id"]]


@needs_pg
def test_worker_share_migration_adds_the_table_and_going_back_unshares_narrowed_workers(client, hub, hub_db):
    public = select(INFO_TABLES.c.table_name).where(INFO_TABLES.c.table_schema == "public")
    assert pg.WORKER_SHARE_TABLES <= {row[0] for row in sql(hub_db, public)}
    unit(client, hub, "lab")
    lab, open_one = add_worker(client, hub["owner"], "lab"), add_worker(client, hub["owner"], "open")
    narrow(client, hub["owner"], lab, "lab")
    patch_shared(client, hub["owner"], open_one, True)
    move_to(hub_db, "0106", down=True)
    assert not {row[0] for row in sql(hub_db, public)} & pg.WORKER_SHARE_TABLES
    w = tables.workers
    shared = dict(sql(hub_db, select(w.c.name, w.c.shared)))
    assert shared == {"lab": False, "open": True}  # narrowed: never shared with every writer once the rows are gone
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0107") :]
    assert pg.WORKER_SHARE_TABLES <= {row[0] for row in sql(hub_db, public)}


@needs_pg
def test_worker_share_access_owner_and_admin_narrow_and_take_back_each_audited(client, hub, hub_db):
    unit(client, hub, "lab")
    unit(client, hub, "ops")
    lab = add_worker(client, hub["owner"], "lab")
    target = f"worker:{lab['id']} name=lab owner={OWNER}"
    made = narrow(client, hub["owner"], lab, "lab")
    assert list(made) == KEYS
    assert (made["worker"], made["owner"], made["shared"], made["units"]) == ("lab", OWNER, True, ["lab"])
    assert made["doctor_at"] is not None and (made["doctor_high"], made["doctor_medium"]) == (0, 0)
    assert narrow(client, hub["owner"], lab, "lab")["units"] == ["lab"]  # already: nothing changes, nothing audited
    assert narrow(client, hub["admin"], lab, "ops")["units"] == ["lab", "ops"]
    for who, status in (("other", 404), ("reader", 404), ("stranger", 404)):
        narrow(client, hub[who], lab, "ops", status=status)
        widen(client, hub[who], lab, "ops", status=status)
    narrow(client, hub["owner"], lab, "no-such-unit", status=422)
    widen(client, hub["owner"], lab, "no-such-unit", status=404)
    assert widen(client, hub["owner"], lab, "lab")["units"] == ["ops"]
    last = widen(client, hub["admin"], lab, "ops")
    assert (last["shared"], last["units"]) == (False, [])  # with its last unit, sharing ends
    widen(client, hub["owner"], lab, "ops", status=404)
    rows = [row[:3] for row in audit_rows(hub_db, "worker") if row[0] in ("worker.share", "worker.unshare")]
    assert rows == [
        ("worker.share", f"{target} unit=lab by=owner shared=false->true", OWNER),
        ("worker.share", f"{target} unit=ops by=admin", ADMIN),
        ("worker.unshare", f"{target} unit=lab by=owner", OWNER),
        ("worker.unshare", f"{target} unit=ops by=admin shared=true->false", ADMIN),
    ]
    mine = client.get("/v1/org/workers", headers=hub["owner"])
    assert mine.status_code == 200 and [row["worker"] for row in mine.json()] == ["lab"]
    assert client.get("/v1/org/workers", headers=hub["other"]).json() == []
    assert [row["id"] for row in client.get("/v1/org/workers", headers=hub["admin"]).json()] == [lab["id"]]
    revoked = client.post(f"/v1/workers/{lab['id']}/revoke", headers=hub["owner"])
    assert revoked.status_code == 200, revoked.text
    narrow(client, hub["owner"], lab, "lab", status=409)
    assert client.get("/v1/org/workers", headers=hub["owner"]).json() == []


@needs_pg
def test_worker_share_claim_takes_only_runs_of_members_seated_in_its_units(client, hub):
    unit(client, hub, "lab")
    unit(client, hub, "bench", parent="lab")
    lab = add_worker(client, hub["owner"], "lab", slots=2)
    narrow(client, hub["owner"], lab, "lab")
    (theirs,) = dispatched(client, hub["other"], [4])  # someone-else is seated at the root only
    assert claim(client, lab) is None
    refused = dispatch(client, hub["other"], [5], worker_id=lab["id"])
    assert refused.status_code == 403 and "shared in project" in refused.text
    assert listed(client, hub["other"], lab) == []
    assert client.get(f"/v1/workers/{lab['id']}", headers=hub["other"]).status_code == 404
    (mine,) = dispatched(client, hub["owner"], [2])
    assert claim(client, lab)["id"] == mine["id"]  # its owner's runs, always
    moved(client, lab, mine["id"], "running", "failed", error="it stopped")
    seat(client, hub, "bench")  # a primary seat below the unit the worker is narrowed to
    assert listed(client, hub["other"], lab) == [lab["id"]]
    assert claim(client, lab)["id"] == theirs["id"]
    (pinned,) = dispatched(client, hub["other"], [5], worker_id=lab["id"])
    assert pinned["pinned_worker_id"] == lab["id"]


@needs_pg
def test_worker_share_rerun_candidates_follow_the_units(client, hub):
    unit(client, hub, "lab")
    unit(client, hub, "ops")
    seat(client, hub, "lab")
    lab = add_worker(client, hub["owner"], "lab")
    narrow(client, hub["owner"], lab, "lab")
    (run,) = dispatched(client, hub["other"], [4], worker_id=lab["id"])
    assert claim(client, lab)["id"] == run["id"]
    moved(client, lab, run["id"], "running", "failed", error="it stopped")
    path = f"/v1/projects/{PROJECT}/runs/{run['id']}/rerun-candidates"

    def candidates() -> list[str]:
        response = client.get(path, headers=hub["other"])
        assert response.status_code == 200, response.text
        return [candidate["worker"] for candidate in response.json()["workers"]]

    assert candidates() == ["lab"]
    narrow(client, hub["owner"], lab, "ops")
    widen(client, hub["owner"], lab, "lab")  # still shared, now with ops alone
    assert candidates() == []


@needs_pg
def test_shared_workers_without_a_unit_keep_every_writer_and_patch_shared_still_works(client, hub):
    unit(client, hub, "lab")
    lab = add_worker(client, hub["owner"], "lab", slots=2)
    patch_shared(client, hub["owner"], lab, True)  # no unit: every writer of a project it serves
    (first,) = dispatched(client, hub["other"], [4])
    assert claim(client, lab)["id"] == first["id"]
    moved(client, lab, first["id"], "running", "failed", error="it stopped")
    narrow(client, hub["owner"], lab, "lab")
    (second,) = dispatched(client, hub["other"], [5])
    assert claim(client, lab) is None  # narrowed to lab, where someone-else holds no seat
    patch_shared(client, hub["owner"], lab, False)
    rows = client.get("/v1/org/workers", headers=hub["owner"]).json()
    assert [(row["shared"], row["units"]) for row in rows] == [(False, ["lab"])]  # the units stay
    seat(client, hub, "lab")
    assert claim(client, lab) is None  # not shared: the units take nobody in
    patch_shared(client, hub["owner"], lab, True)
    assert claim(client, lab)["id"] == second["id"]
