"""The hub's side of a run's browser identities (plan agent-browser, criterion a4): a dispatch names them, which gives
the run a browser and pins it to their worker; the claim hands it only to that worker while no other run holds one of
them, and the run holds them from its claim until it leaves the held states, the reaper's moves included. The worker
holding the run gets each profile (``browser_leases``), sends it back as the next version while the run holds the
identity, and says when its Chromium cannot open one; every answer is in the dispatcher's name in the audit log, and a
corrupt, too large or stale save keeps the version the identity had. A run without identities claims as before."""

import base64
import secrets
from pathlib import Path

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select, update

from evo_agents.hub import browser_archive, tables
from evo_agents.hub.server import browser_leases
from tests.hub import test_run_cli
from tests.hub.live import sql
from tests.hub.test_browser_claim import add_browser_worker
from tests.hub.test_browser_identity import (  # noqa: F401 (fixtures)
    ADMIN_GRANT,
    CHROMIUM,
    HIDDEN,
    PATH,
    audit_rows,
    created,
    opened,
    save,
    stored,
    user_id,
    versions,
)
from tests.hub.test_run_cli import as_json, runs_of
from tests.hub.test_runs import (
    OTHER,
    OWNER,
    PLAN,
    PROJECT,
    PROTOCOL,
    claim,
    control,
    dispatch,
    dispatched,
    expire,
    moved,
    of_run,
    recover,
    steady,
)

cli_hub = test_run_cli.hub  # the hub under uvicorn, with homes signed in to it
WORKER_CHROMIUM = "153.0.8010.12"


def profile_archive(tmp_path: Path, value: str) -> bytes:
    """The archive of a profile whose cookie file holds ``value``."""
    profile = tmp_path / secrets.token_hex(4)
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Cookies").write_text(value, encoding="utf-8")
    (profile / "Local State").write_text("{}", encoding="utf-8")
    return browser_archive.pack(profile)


def held_by(db, identity_id: int):
    b = tables.browser_identities
    return sql(db, select(b.c.held_by_run).where(b.c.id == identity_id))[0][0]


def set_identity(db, identity_id: int, **values) -> None:
    b = tables.browser_identities
    sql(db, update(b).values(**values).where(b.c.id == identity_id))


def lease(client, worker: dict, run_id: int, identity_id: int):
    path = f"/v1/worker/runs/{run_id}/browser-profile"
    return client.post(path, json={"identity_id": identity_id}, headers=worker["headers"])


def send(client, worker: dict, run_id: int, identity_id: int, archive: bytes, base_version: int):
    path = f"/v1/worker/runs/{run_id}/browser-profile/{identity_id}"
    params = {"base_version": base_version, "chromium_version": WORKER_CHROMIUM}
    headers = {**worker["headers"], "Content-Type": "application/octet-stream"}
    return client.put(path, params=params, content=archive, headers=headers)


def claimed_with(hub, worker: dict, identity: dict, steps=(4,)) -> int:
    """A run of ``steps`` dispatched with ``identity`` and claimed by ``worker``; its id."""
    (run,) = dispatched(hub.client, hub.headers["owner"], list(steps), identities=[identity["name"]])
    assert claim(hub.client, worker)["id"] == run["id"]
    return run["id"]


def test_browser_profile_dispatch_names_identities_which_give_a_browser_and_pin_the_worker(stored):
    client, headers, db = stored.client, stored.headers, stored.db
    identity = created(client, headers["owner"], "northwind-admin", domains=("northwind.example",))
    (run,) = dispatched(client, headers["owner"], [4], identities=["northwind-admin", "northwind-admin"])
    assert (run["browser"], run["browser_identities"]) == (True, [identity["id"]])
    assert of_run(db, run["id"], "browser", "browser_identities", "pinned_worker_id") == [
        (True, [identity["id"]], stored.box["id"])
    ]

    lab = add_browser_worker(client, headers["owner"], "lab")
    add_browser_worker(client, headers["other"], "lumen-box")
    created(client, headers["other"], "lumen-ops", worker="lumen-box")
    lumen_lab = created(client, headers["owner"], "lumen-lab", worker="lab")
    before = sql(db, select(tables.runs.c.id))
    refusals = (
        ({"identities": ["nowhere"]}, "no browser identity nowhere"),
        ({"identities": ["lumen-ops"]}, "no browser identity lumen-ops"),  # another member's
        ({"identities": ["northwind-admin", "lumen-lab"]}, "on several"),
        ({"identities": ["northwind-admin"], "worker_id": lab["id"]}, "pinned to worker lab"),
    )
    for extra, said in refusals:
        refused = dispatch(client, headers["owner"], [5], **extra)
        assert refused.status_code == 422 and said in refused.json()["message"], refused.text
    set_identity(db, identity["id"], status="disabled")
    refused = dispatch(client, headers["owner"], [5], identities=["northwind-admin"])
    assert refused.status_code == 422 and "disabled" in refused.json()["message"]
    assert sql(db, select(tables.runs.c.id)) == before

    assert control(client, headers["owner"], run["id"], "cancel").status_code == 200  # a plan run waits for it
    plan_run = client.post(
        f"/v1/projects/{PROJECT}/plan-runs",
        json={"plan_id": PLAN, "identities": ["lumen-lab"]},
        headers=headers["owner"],
    )
    assert plan_run.status_code == 201, plan_run.text
    assert (plan_run.json()["browser"], plan_run.json()["browser_identities"]) == (True, [lumen_lab["id"]])
    assert of_run(db, plan_run.json()["id"], "pinned_worker_id") == [(lab["id"],)]


def test_browser_profile_claim_goes_to_the_identity_worker_once_no_other_run_holds_it(stored):
    client, headers, db = stored.client, stored.headers, stored.db
    twin = add_browser_worker(client, headers["owner"], "twin", slots=3)
    lab = add_browser_worker(client, headers["owner"], "lab", slots=3)
    identity = created(client, headers["owner"], "northwind-admin", worker="twin")
    first, second = dispatched(client, headers["owner"], [4, 5], identities=["northwind-admin"])
    (plain,) = dispatched(client, headers["owner"], [2], browser=True)
    plain_spec = claim(client, lab)
    assert plain_spec["id"] == plain["id"] and "browser_identities" not in plain_spec  # a run without, as before
    assert claim(client, lab) is None  # never one of the identity's runs

    spec = claim(client, twin)
    assert spec["id"] == first["id"] and spec["browser_identities"] == [identity["id"]]
    assert held_by(db, identity["id"]) == first["id"]
    assert claim(client, twin) is None  # a slot and a browser free, but the first run holds the identity
    moved(client, twin, first["id"], "running", "failed", error="the agent stopped")
    assert held_by(db, identity["id"]) is None
    assert claim(client, twin)["id"] == second["id"]
    assert held_by(db, identity["id"]) == second["id"]


def test_browser_profile_reaper_releases_the_identity_of_a_lost_run(stored):
    client, headers, db, box = stored.client, stored.headers, stored.db, stored.box
    identity = created(client, headers["owner"], "northwind-admin")
    first = claimed_with(stored, box, identity, steps=(2,))
    moved(client, box, first, "running")
    expire(db, first)
    assert recover(client)["lost"] == 1
    assert held_by(db, identity["id"]) is None
    ((second, kept),) = sql(
        db, select(tables.runs.c.id, tables.runs.c.browser_identities).where(tables.runs.c.parent_run_id == first)
    )
    assert kept == [identity["id"]]
    steady(db, box)
    assert claim(client, box)["id"] == second
    assert held_by(db, identity["id"]) == second


def test_browser_profile_lease_hands_the_profile_only_to_the_worker_holding_the_run(stored, tmp_path):
    client, headers, db, box = stored.client, stored.headers, stored.db, stored.box
    identity = created(client, headers["owner"], "northwind-admin", domains=("northwind.example",))
    other = created(client, headers["owner"], "lumen-ops", domains=("lumen.example",))
    archive = profile_archive(tmp_path, "northwind_session=" + secrets.token_hex(8))
    assert save(stored, identity["id"], 0, archive) == 1
    run_id = claimed_with(stored, box, identity)

    answer = lease(client, box, run_id, identity["id"])
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert set(body) == set(browser_leases.BrowserProfileLease.model_fields) and not HIDDEN & set(body)
    assert (body["name"], body["domains"], body["status"], body["version"], body["chromium_version"]) == (
        "northwind-admin",
        ["northwind.example"],
        "active",
        1,
        CHROMIUM,
    )
    assert base64.b64decode(body["profile"]) == archive
    target = f"{PROJECT} browser-identity:{identity['id']} run:{run_id} version=1"
    assert audit_rows(db)[-1] == ("browser_identity.lease", target, OWNER, PROJECT)

    lab = add_browser_worker(client, headers["owner"], "lab")
    assert lease(client, lab, run_id, identity["id"]).status_code == 404  # not the worker holding the run
    assert lease(client, box, run_id, other["id"]).status_code == 404  # not one of the run's
    set_identity(db, identity["id"], held_by_run=None)
    assert lease(client, box, run_id, identity["id"]).status_code == 409
    set_identity(db, identity["id"], held_by_run=run_id, owner_id=user_id(db, OTHER))
    assert lease(client, box, run_id, identity["id"]).status_code == 403  # another member's
    set_identity(db, identity["id"], owner_id=user_id(db, OWNER), status="disabled")
    assert lease(client, box, run_id, identity["id"]).status_code == 403
    set_identity(db, identity["id"], status="active")
    reader = {"role": "reader", "max_level": "internal"}
    assert client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=reader, headers=headers["admin"]).is_success
    refused = lease(client, box, run_id, identity["id"])
    assert refused.status_code == 403 and "writer role" in refused.json()["message"]
    assert client.put(
        f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=headers["admin"]
    ).is_success
    assert [row[0] for row in audit_rows(db)].count("browser_identity.lease") == 1


def test_browser_profile_save_makes_the_next_version_while_the_run_holds_the_identity(stored, tmp_path, monkeypatch):
    client, headers, db, box = stored.client, stored.headers, stored.db, stored.box
    identity = created(client, headers["owner"], "northwind-admin")
    run_id = claimed_with(stored, box, identity)
    first = lease(client, box, run_id, identity["id"]).json()
    assert (first["version"], first["profile"]) == (0, None)

    archive = profile_archive(tmp_path, "northwind_session=" + secrets.token_hex(8))
    saved = send(client, box, run_id, identity["id"], archive, 0)
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"identity_id": identity["id"], "version": 1}
    assert opened(stored, identity["id"]) == (1, archive)
    b = tables.browser_identities
    query = select(b.c.chromium_version, b.c.last_saved_by).where(b.c.id == identity["id"])
    assert sql(db, query) == [(WORKER_CHROMIUM, user_id(db, OWNER))]
    assert audit_rows(db)[-1] == (
        "browser_identity.save",
        f"{PROJECT} browser-identity:{identity['id']} version=1",
        OWNER,
        PROJECT,
    )

    stale = send(client, box, run_id, identity["id"], profile_archive(tmp_path, "late"), 0)
    assert stale.status_code == 409 and "version 1" in stale.json()["message"]
    corrupt = send(client, box, run_id, identity["id"], b"not a profile", 1)
    assert corrupt.status_code == 422 and "gzipped tar" in corrupt.json()["message"]
    monkeypatch.setattr(browser_leases, "MAX_ARCHIVE_BYTES", 64)
    large = send(client, box, run_id, identity["id"], archive, 1)
    assert large.status_code == 413
    monkeypatch.undo()
    assert versions(db, identity["id"])[0] == 1 and opened(stored, identity["id"]) == (1, archive)

    moved(client, box, run_id, "running", "failed", error="the agent stopped")
    ended = send(client, box, run_id, identity["id"], archive, 1)
    assert ended.status_code == 404  # the run ended: it holds the identity no more
    assert versions(db, identity["id"])[0] == 1


def test_browser_profile_needs_login_from_the_worker_and_from_a_profile_the_hub_cannot_open(stored, tmp_path):
    client, headers, db, box = stored.client, stored.headers, stored.db, stored.box
    identity = created(client, headers["owner"], "northwind-admin")
    assert save(stored, identity["id"], 0, profile_archive(tmp_path, "northwind")) == 1
    run_id = claimed_with(stored, box, identity)
    path = f"/v1/worker/runs/{run_id}/browser-profile/{identity['id']}/needs-login"

    said = client.post(path, json={"cause": "newer_chromium"}, headers=box["headers"])
    assert said.status_code == 200 and said.json() == {"identity_id": identity["id"], "status": "needs_login"}
    assert audit_rows(db)[-1] == (
        "browser_identity.needs_login",
        f"{PROJECT} browser-identity:{identity['id']} run:{run_id} cause=newer_chromium",
        OWNER,
        PROJECT,
    )
    assert client.post(path, json={"cause": "lost"}, headers=box["headers"]).status_code == 422

    set_identity(db, identity["id"], status="active", key_nonce=secrets.token_bytes(12))  # the data key opens no more
    answer = lease(client, box, run_id, identity["id"]).json()
    assert (answer["profile"], answer["status"], answer["version"]) == (None, "needs_login", 1)
    assert audit_rows(db)[-1][0] == "browser_identity.needs_login"


def test_browser_profile_rerun_keeps_the_identities_while_they_are_usable(stored):
    client, headers, db = stored.client, stored.headers, stored.db
    identity = created(client, headers["owner"], "northwind-admin")
    (run,) = dispatched(client, headers["owner"], [4], identities=["northwind-admin"])
    assert control(client, headers["owner"], run["id"], "cancel").status_code == 200
    kept = control(client, headers["owner"], run["id"], "rerun")
    assert kept.status_code == 201, kept.text
    assert (kept.json()["browser"], kept.json()["browser_identities"]) == (True, [identity["id"]])
    assert of_run(db, kept.json()["id"], "pinned_worker_id") == [(stored.box["id"],)]
    assert control(client, headers["owner"], kept.json()["id"], "cancel").status_code == 200

    path = f"/v1/projects/{PROJECT}/runs/{kept.json()['id']}/rerun"
    dropped = client.post(path, json={"browser": False}, headers=headers["owner"])
    assert dropped.status_code == 201 and (dropped.json()["browser"], dropped.json()["browser_identities"]) == (
        False,
        [],
    )
    assert control(client, headers["owner"], dropped.json()["id"], "cancel").status_code == 200
    set_identity(db, identity["id"], status="disabled")
    refused = control(client, headers["owner"], kept.json()["id"], "rerun")
    assert refused.status_code == 409 and "browser false" in refused.json()["message"]


def test_browser_profile_cli_identity_dispatches_with_identities_and_a_browser(cli_hub, monkeypatch, capsys):
    client, headers = cli_hub.client, cli_hub.headers
    add_browser_worker(client, headers["owner"], "mac-mini")
    made = client.post(
        PATH,
        json={"name": "northwind-admin", "domains": ["northwind.example"], "worker": "mac-mini", "owner": "member"},
        headers=headers["owner"],
    )
    assert made.status_code == 201, made.text
    asked = as_json(
        runs_of(cli_hub, monkeypatch, capsys, "owner", "dispatch", PLAN, "4", "--identity", "northwind-admin", "--json")
    )
    assert [(run["browser"], run["browser_identities"]) for run in asked] == [(True, [made.json()["id"]])]
    as_json(runs_of(cli_hub, monkeypatch, capsys, "owner", "cancel", str(asked[0]["id"]), "--json"))
    planned = as_json(
        runs_of(cli_hub, monkeypatch, capsys, "owner", "plan", PLAN, "--identity", "northwind-admin", "--json")
    )
    assert (planned["kind"], planned["browser"], planned["browser_identities"]) == ("plan", True, [made.json()["id"]])


def test_browser_profile_route_of_another_kind_of_caller_is_refused(stored):
    client, headers, box = stored.client, stored.headers, stored.box
    identity = created(client, headers["owner"], "northwind-admin")
    run_id = claimed_with(stored, box, identity)
    member = lease(client, {"headers": {**headers["owner"], **PROTOCOL}}, run_id, identity["id"])
    assert member.status_code == 403  # a member's token is no worker's
