"""Workers on the hub: pairing codes, joining, direct registration, and the owner's drain, undrain, dispatch_from and
revoke.

The checks step 4 of the worker-fleet plan names: an expired code, a code used a second time and a code after 5
wrong tries are all refused; a worker token gets 403 on /v1/projects and a machine token 403 on /v1/worker/claim; a
pairing for a project where the caller is only a reader is 403; another member asking for one's worker gets 404; a
hub admin can revoke any worker; and no log record holds a code or a token. Around them: a web session and a machine
token keep working on their own routes as before, the worker routes need the protocol header, a revoked worker's
token is 401 at once and the runs it held are released as the reaper would, every change leaves its audit row, and
the database keeps neither a code nor a token in clear.

And what a security review of the first version asked for: revoking a worker's token revokes the worker, refused
joins are limited per client address, a code is kept as an HMAC under the session secret, only the owner undrains a
worker, and a locked code counts against the member's five until it expires.

And step 10 of the worker-credentials plan: only the owner, from a web session, sets who may dispatch to a worker,
and a machine token gets 403 there.

And step 1 of the worker-settings-web plan: only the owner, from a web session or with their machine token, changes
a worker's slots (up to 16), labels and name with PATCH /v1/workers/{id}; each change is audited, the values it has
change nothing, and the next heartbeat answer carries them.

And step 1 of the auto-update-nightly plan: the owner locks a worker's version the same way, the heartbeat answer
carries the lock, and the hub keeps and shows the commit a daemon runs and what it found of updating itself, which a
daemon that sends neither leaves empty.

And the projects a worker serves, changed in place with PATCH /v1/workers/{id} by its owner alone: a project added
needs the writer role, one served already stays on a lesser role, the change is audited and reaches the heartbeat
answer, and dropping a project is refused while a run of it is active on the worker or its night shift is on duty
there.

And step 1 of the nightly-within-a-minute plan: the heartbeat answer carries where nightly points once the hub read it;
the owner or a hub admin asks for and cancels an Update now as they drain, audited once and refused with why when the
worker is locked, its daemon too old or already current; the answer carries the open request, and the hub ends it on
the heartbeat that shows it ended."""

import hashlib
import hmac
import logging
from datetime import UTC, datetime, timedelta

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, insert, select, update

from evo_agents.hub import tables
from evo_agents.hub.log import scrub
from evo_agents.hub.server import audit
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.security import MACHINE, WORKER, authenticate, hash_token
from evo_agents.hub.server.workers import CROCKFORD, RefusalLimit, normal_code
from tests.hub.fake_github import Account
from tests.hub.live import ADMIN, add_project, bearer, sql, table_dump
from tests.hub.test_run_tables import PLAN, add_run
from tests.hub.test_web_auth import cookie, csrf_for, web_sign_in

ADMIN_ID = 302
OWNER, OWNER_ID = "owner", 501
OTHER, OTHER_ID = "someone-else", 502
PROTOCOL = {"X-Evo-Worker-Protocol": "1"}
HOST = {"hostname": "mac-mini.local", "os": "darwin", "arch": "arm64", "agent_version": "0.3.0"}
WRITER = {"role": "writer", "max_level": "internal"}
READER = {"role": "reader", "max_level": "internal"}


@pytest.fixture
def config(hub_db, tmp_path, github):
    return live.hub_config(hub_db, tmp_path, github, **live.web_changes(github))


@pytest.fixture
def client(config):
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def admin(client, github) -> dict:
    return bearer(live.sign_in(client, github, ADMIN, ADMIN_ID)["token"])


@pytest.fixture
def hub(client, github, hub_db, admin):
    """Projects demo and docs, where the owner is a writer, and notes, where the owner is a reader; the owner's and
    another member's machine tokens."""
    for name in ("demo", "docs", "notes"):
        add_project(hub_db, name)
    owner = live.sign_in(client, github, OWNER, OWNER_ID)
    other = live.sign_in(client, github, OTHER, OTHER_ID)
    for project, login, grant in (
        ("demo", OWNER, WRITER),
        ("docs", OWNER, WRITER),
        ("notes", OWNER, READER),
        ("demo", OTHER, WRITER),
    ):
        response = client.put(f"/v1/admin/projects/{project}/grants/{login}", json=grant, headers=admin)
        assert response.status_code == 200, response.text
    return {"owner": bearer(owner["token"]), "other": bearer(other["token"]), "admin": admin, "raw": owner}


def pair(client, headers, name="mac-mini", projects=("demo",), **extra):
    body = {"name": name, "projects": list(projects), **extra}
    return client.post("/v1/workers/pairings", json=body, headers=headers)


def paired(client, headers, **extra) -> dict:
    response = pair(client, headers, **extra)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def join(client, code: str, **host):
    return client.post("/v1/worker/join", json={"code": code, **HOST, **host}, headers=PROTOCOL)


def joined(client, code: str, **host) -> dict:
    response = join(client, code, **host)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def wrong_rest(code: str, tries: int) -> list[str]:
    """``tries`` codes that share the selector (the first four characters) of ``code`` and differ in the rest."""
    head, rest = code[:4], code.replace("-", "")[4:]
    others = [c * 4 for c in CROCKFORD if c * 4 != rest]
    return [f"{head}-{other}" for other in others[:tries]]


def worker_headers(token: str) -> dict:
    return {**bearer(token), **PROTOCOL}


def audit_rows(db, action: str) -> list[tuple]:
    audit_table, users = tables.audit, tables.users
    return sql(
        db,
        select(audit_table.c.target, users.c.login)
        .join_from(audit_table, users, users.c.id == audit_table.c.actor_id)
        .where(audit_table.c.action == action),
    )


def count(db, table, *where) -> list[tuple]:
    return sql(db, select(func.count()).select_from(table).where(*where))


def expire(db, pairing_id: int) -> None:
    """Move pairing ``pairing_id`` 11 minutes back, past its expiry."""
    pairings, back = tables.worker_pairings, timedelta(minutes=11)
    sql(
        db,
        update(pairings)
        .values(created_at=pairings.c.created_at - back, expires_at=pairings.c.expires_at - back)
        .where(pairings.c.id == pairing_id),
    )


# Codes


def test_codes_are_crockford_base32_read_the_way_crockford_reads_them():
    assert normal_code("abcd-efgh") == "ABCDEFGH"
    assert normal_code(" ab cd-ef gh ") == "ABCDEFGH"
    assert normal_code("O0IL-1234") == "0011" + "1234"  # O as 0, I and L as 1
    for wrong in ("ABCD-EFG", "ABCD-EFGHJ", "ABCD-EFGU", "ABCD_EFGH", ""):
        assert normal_code(wrong) is None, wrong


def test_a_pairing_code_joins_a_machine_once_and_the_hub_keeps_no_code_or_token(client, hub, hub_db):
    pairing = paired(client, hub["owner"], projects=["demo", "docs", "demo"], slots=2, labels=["gpu", "gpu"])
    code = pairing["code"]
    assert len(code) == 9 and code[4] == "-" and all(c in CROCKFORD for c in code.replace("-", ""))
    assert (pairing["name"], pairing["projects"], pairing["slots"], pairing["labels"]) == (
        "mac-mini",
        ["demo", "docs"],
        2,
        ["gpu"],
    )
    expires = datetime.fromisoformat(pairing["expires_at"]) - datetime.now(UTC)
    assert 9 * 60 < expires.total_seconds() <= 10 * 60
    state = client.get(f"/v1/workers/pairings/{pairing['id']}", headers=hub["owner"]).json()
    assert (state["status"], state["tries_left"], state["worker_id"]) == ("waiting", 5, None)

    # the code in any case, without its hyphen, still names the pairing
    answer = joined(client, code.lower().replace("-", ""))
    token, worker = answer["token"], answer["worker"]
    assert token.startswith("evw_") and len(token) == 4 + 43
    assert worker["owner"] == OWNER and worker["projects"] == ["demo", "docs"]
    assert (worker["name"], worker["slots"], worker["labels"], worker["allow_web_terminal"]) == (
        "mac-mini",
        2,
        ["gpu"],
        False,
    )
    assert (worker["hostname"], worker["os"], worker["arch"], worker["agent_version"]) == tuple(HOST.values())
    assert worker["status"] == "offline" and worker["held_runs"] == 0  # no heartbeat yet
    state = client.get(f"/v1/workers/pairings/{pairing['id']}", headers=hub["owner"]).json()
    assert (state["status"], state["worker_id"]) == ("joined", worker["id"])

    # used once: the same code is refused, and the worker stays alone
    second = join(client, code)
    assert second.status_code == 403 and second.json()["error"] == "forbidden"
    assert count(hub_db, tables.workers) == [(1,)]

    # the token is a worker token of the owner, with the machine's hostname, kept as its hash only
    tokens = tables.tokens
    stored = select(tokens.c.kind, tokens.c.host, tokens.c.token_hash).where(tokens.c.id == answer["token_id"])
    assert sql(hub_db, stored) == [("worker", HOST["hostname"], hash_token(token))]
    dump = table_dump(hub_db)
    for secret in (token, code, code.replace("-", "")):
        assert secret not in dump
    listed = {row["kind"] for row in client.get("/v1/tokens", headers=hub["owner"]).json()}
    assert listed == {"machine", "worker"}
    by_kind = client.get("/v1/admin/tokens", params={"kind": "worker"}, headers=hub["admin"]).json()["items"]
    assert [(row["login"], row["host"]) for row in by_kind] == [(OWNER, HOST["hostname"])]

    assert [target for target, _ in audit_rows(hub_db, "worker.pair")] == [f"pairing:{pairing['id']} name=mac-mini"]
    assert audit_rows(hub_db, "worker.join") == [
        (f"worker:{worker['id']} name=mac-mini pairing:{pairing['id']}", OWNER)
    ]


def test_an_expired_code_is_refused(client, hub, hub_db):
    pairing = paired(client, hub["owner"])
    expire(hub_db, pairing["id"])
    refused = join(client, pairing["code"])
    assert refused.status_code == 403 and "expired" in refused.json()["message"]
    assert client.get(f"/v1/workers/pairings/{pairing['id']}", headers=hub["owner"]).json()["status"] == "expired"
    assert count(hub_db, tables.workers) == [(0,)]
    assert sql(hub_db, select(tables.worker_pairings.c.attempts)) == [(0,)]  # a dead code counts no tries


def test_five_wrong_tries_lock_the_code_and_the_right_one_is_refused_after_them(client, hub, hub_db):
    pairing = paired(client, hub["owner"])
    unrelated = paired(client, hub["owner"], name="desk")
    wrong = wrong_rest(pairing["code"], 5)
    for number, attempt in enumerate(wrong, start=1):
        refused = join(client, attempt)
        assert refused.status_code == 403, refused.text
        state = client.get(f"/v1/workers/pairings/{pairing['id']}", headers=hub["owner"]).json()
        assert state["tries_left"] == 5 - number
    assert state["status"] == "locked"
    assert join(client, pairing["code"]).status_code == 403  # the right code, too late
    assert count(hub_db, tables.workers) == [(0,)]
    # the wrong tries counted against that pairing only, and every refusal reads the same
    assert client.get(f"/v1/workers/pairings/{unrelated['id']}", headers=hub["owner"]).json()["tries_left"] == 5
    messages = {join(client, code).json()["message"] for code in (pairing["code"], "ZZZZ-ZZZZ")}
    assert len(messages) == 1
    assert joined(client, unrelated["code"])["worker"]["name"] == "desk"


def test_a_locked_code_counts_against_the_five_until_it_expires(client, hub, hub_db):
    pairings = [paired(client, hub["owner"], name=f"box-{number}") for number in range(5)]
    for attempt in wrong_rest(pairings[0]["code"], 5):
        assert join(client, attempt).status_code == 403
    assert client.get(f"/v1/workers/pairings/{pairings[0]['id']}", headers=hub["owner"]).json()["status"] == "locked"
    # locking a code by hand frees no place, so a member cannot hold more selectors that way
    refused = pair(client, hub["owner"], name="box-5")
    assert refused.status_code == 409 and "waiting or locked" in refused.json()["message"]
    expire(hub_db, pairings[0]["id"])
    assert pair(client, hub["owner"], name="box-5").status_code == 201


def unknown_code(pairing: dict) -> str:
    """A well-formed code whose selector is not the one of ``pairing``."""
    return "YYYY-YYYY" if pairing["code"].startswith("ZZZZ") else "ZZZZ-ZZZZ"


def test_refused_codes_from_one_address_are_limited(client, hub, hub_db):
    now = [1000.0]
    client.app.state.join_refusals.clock = lambda: now[0]
    pairing = paired(client, hub["owner"])
    for _ in range(3):
        assert join(client, "ABCD-EFG").status_code == 422  # a malformed code is refused before it counts
    for _ in range(10):
        assert join(client, unknown_code(pairing)).status_code == 403
        now[0] += 30  # ten refusals in four and a half minutes
    limited = join(client, pairing["code"])  # the right code waits too
    assert limited.status_code == 429, limited.text
    assert limited.json()["error"] == "too_many_requests"
    assert limited.headers["retry-after"] == "300"  # the first refusal, at 1000, leaves the window at 1600
    assert "300 seconds" in limited.json()["message"]
    state = client.get(f"/v1/workers/pairings/{pairing['id']}", headers=hub["owner"]).json()
    assert (state["status"], state["tries_left"]) == ("waiting", 5)  # a 429 never reaches the pairing
    now[0] = 1599.0
    assert join(client, pairing["code"]).headers["retry-after"] == "1"
    now[0] = 1600.0
    assert joined(client, pairing["code"])["worker"]["name"] == "mac-mini"
    assert count(hub_db, tables.workers) == [(1,)]


def test_the_refusal_limit_counts_each_address_apart_and_forgets_the_least_recent():
    now = [0.0]
    limit = RefusalLimit(limit=2, window=60, max_clients=2, clock=lambda: now[0])
    limit.refuse("10.0.0.1")
    limit.refuse("10.0.0.1")
    assert limit.retry_after("10.0.0.1") == 60 and limit.retry_after("10.0.0.2") is None
    now[0] = 10.0
    limit.refuse("10.0.0.2")
    assert limit.retry_after("10.0.0.1") == 50 and limit.retry_after("10.0.0.2") is None
    limit.refuse("10.0.0.3")  # a third address: 10.0.0.1, refused least recently, is forgotten
    assert limit.retry_after("10.0.0.1") is None
    limit.refuse("10.0.0.2")
    assert limit.retry_after("10.0.0.2") == 60  # still counted: its first refusal was kept
    now[0] = 71.0
    assert limit.retry_after("10.0.0.2") is None  # the first refusal left the window


def test_a_malformed_code_is_422_and_counts_against_nothing(client, hub, hub_db):
    paired(client, hub["owner"])
    for code in ("ABCD-EFG", "ABCD-EFGU", "not a pairing code"):
        response = join(client, code)
        assert response.status_code == 422 and "Crockford" in response.json()["message"], code
    assert sql(hub_db, select(tables.worker_pairings.c.attempts)) == [(0,)]


def test_pairings_need_the_writer_role_on_every_project(client, hub, hub_db, github):
    reader_only = pair(client, hub["owner"], projects=["demo", "notes"])
    assert reader_only.status_code == 403 and "writer role" in reader_only.json()["message"]
    assert pair(client, hub["owner"], projects=["nowhere"]).status_code == 404
    # a hub admin without a grant manages a project but runs nothing in it
    assert pair(client, hub["admin"], projects=["demo"]).status_code == 403
    assert count(hub_db, tables.worker_pairings) == [(0,)]
    assert count(hub_db, tables.audit, tables.audit.c.action.regexp_match("^worker[.]")) == [(0,)]


def test_a_web_session_pairs_only_with_its_csrf_header(client, hub, github, hub_db):
    session = web_sign_in(client, github, Account(OWNER, OWNER_ID))
    body = {"name": "laptop", "projects": ["demo"]}
    refused = client.post("/v1/workers/pairings", json=body, headers=cookie(session))
    assert refused.status_code == 403 and "X-Evo-CSRF" in refused.json()["message"]
    csrf = {"X-Evo-CSRF": csrf_for(client, session)}
    created = client.post("/v1/workers/pairings", json=body, headers={**cookie(session), **csrf})
    assert created.status_code == 201, created.text
    state = client.get(f"/v1/workers/pairings/{created.json()['id']}", headers=cookie(session))
    assert state.status_code == 200 and state.json()["status"] == "waiting"


def test_a_member_has_at_most_five_waiting_codes(client, hub, hub_db):
    pairings = [paired(client, hub["owner"], name=f"box-{number}") for number in range(5)]
    sixth = pair(client, hub["owner"], name="box-5")
    assert sixth.status_code == 409 and "5 pairing codes" in sixth.json()["message"]
    assert pair(client, hub["other"], name="box-5").status_code == 201  # per member
    joined(client, pairings[0]["code"])  # a used code is no longer waiting
    assert pair(client, hub["owner"], name="box-5").status_code == 201
    assert pair(client, hub["owner"], name="box-6").status_code == 409


def test_the_hub_keeps_an_hmac_of_the_code_under_the_session_secret(client, hub, hub_db, config):
    code = paired(client, hub["owner"])["code"].replace("-", "")
    pairings = tables.worker_pairings
    ((selector, stored),) = sql(hub_db, select(pairings.c.code_selector, pairings.c.code_hash))
    assert selector == code[:4]
    assert stored != hashlib.sha256(code.encode()).hexdigest()  # a plain hash would let the rest be tried offline
    keyed = hmac.new(config.session_secret.encode(), b"pairing-code\0" + code.encode(), hashlib.sha256).hexdigest()
    assert stored == keyed


def test_without_the_session_secret_pairing_and_joining_answer_503(hub_db, tmp_path, github):
    config = live.hub_config(hub_db, tmp_path, github)  # device flow only: no session secret
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        add_project(hub_db, "demo")
        admin = bearer(live.sign_in(client, github, ADMIN, ADMIN_ID)["token"])
        owner = bearer(live.sign_in(client, github, OWNER, OWNER_ID)["token"])
        assert client.put(f"/v1/admin/projects/demo/grants/{OWNER}", json=WRITER, headers=admin).status_code == 200
        refused = pair(client, owner)
        assert refused.status_code == 503 and "EVO_HUB_SESSION_SECRET" in refused.json()["message"]
        assert join(client, "ABCD-EFGH").status_code == 503
        assert count(hub_db, tables.worker_pairings) == [(0,)]
        # a machine token registers its machine directly all the same
        body = {"name": "box", "projects": ["demo"], **HOST}
        assert client.post("/v1/workers", json=body, headers=owner).status_code == 201


def test_another_members_pairing_is_404(client, hub):
    pairing = paired(client, hub["owner"])
    assert client.get(f"/v1/workers/pairings/{pairing['id']}", headers=hub["other"]).status_code == 404
    assert client.get(f"/v1/workers/pairings/{pairing['id']}", headers=hub["admin"]).status_code == 404


def test_names_are_unique_among_the_owners_live_workers(client, hub, hub_db):
    first = paired(client, hub["owner"])
    second = paired(client, hub["owner"])  # two codes for one name: the first machine to join takes it
    joined(client, first["code"])
    taken = join(client, second["code"])
    assert taken.status_code == 409 and "mac-mini" in taken.json()["message"]
    assert pair(client, hub["owner"], name="MAC-MINI").status_code == 409  # names compare in any case
    assert pair(client, hub["other"], name="mac-mini").status_code == 201  # another owner's worker may share it


def test_a_join_after_the_owner_lost_the_writer_role_is_refused(client, hub, hub_db, admin):
    pairing = paired(client, hub["owner"], projects=["demo", "docs"])
    assert client.put(f"/v1/admin/projects/docs/grants/{OWNER}", json=READER, headers=admin).status_code == 200
    refused = join(client, pairing["code"])
    assert refused.status_code == 409 and "writer role" in refused.json()["message"]
    assert "docs" not in refused.json()["message"]  # the caller holds a code, not a view of the owner's projects
    assert count(hub_db, tables.workers) == [(0,)]
    assert client.get(f"/v1/workers/pairings/{pairing['id']}", headers=hub["owner"]).json()["status"] == "waiting"


# Registering with a machine token


def test_a_machine_token_registers_its_machine_directly(client, hub, hub_db, github):
    body = {"name": "build-box", "projects": ["demo"], "slots": 4, "labels": ["linux"], **HOST, "os": "linux"}
    response = client.post("/v1/workers", json=body, headers=hub["owner"])
    assert response.status_code == 201, response.text
    answer = response.json()
    assert answer["token"].startswith("evw_") and answer["worker"]["os"] == "linux"
    assert answer["worker"]["slots"] == 4 and answer["worker"]["projects"] == ["demo"]
    assert audit_rows(hub_db, "worker.register") == [(f"worker:{answer['worker']['id']} name=build-box", OWNER)]

    reader_only = client.post(
        "/v1/workers", json={**body, "name": "other", "projects": ["notes"]}, headers=hub["owner"]
    )
    assert reader_only.status_code == 403
    session = web_sign_in(client, github, Account(OWNER, OWNER_ID))
    csrf = {"X-Evo-CSRF": csrf_for(client, session)}
    by_web = client.post("/v1/workers", json={**body, "name": "web"}, headers={**cookie(session), **csrf})
    assert by_web.status_code == 403 and "pairing code" in by_web.json()["message"]
    assert count(hub_db, tables.workers) == [(1,)]


# Which credential works where


def test_a_worker_token_works_only_on_the_worker_routes(client, hub, hub_db):
    token = joined(client, paired(client, hub["owner"])["code"])["token"]
    for path in ("/v1/projects", "/v1/auth/whoami", "/v1/workers", "/v1/tokens"):
        response = client.get(path, headers=worker_headers(token))
        assert response.status_code == 403, (path, response.text)
        assert "only on /v1/worker/*" in response.json()["message"]
    assert (
        client.post("/v1/workers/pairings", json={"name": "x", "projects": ["demo"]}, headers=bearer(token)).status_code
        == 403
    )
    # on its own routes it gets past the credential check: this path has no route behind it
    assert client.post("/v1/worker/no-such-route", headers=worker_headers(token)).status_code == 404
    # a worker token is a Bearer token: as the session cookie it is no session
    assert client.get("/v1/projects", headers=cookie(token)).status_code == 401
    # the owner of a worker may be a hub admin; the worker is not
    principal = client.portal.call(authenticate, client.app.state.engine, token, WORKER, client.app.state.config)
    assert principal.kind == WORKER and principal.login == OWNER and principal.admin is False


def test_machine_tokens_and_web_sessions_get_403_on_the_worker_routes(client, hub, github):
    session = web_sign_in(client, github, Account(OWNER, OWNER_ID))
    csrf = {"X-Evo-CSRF": csrf_for(client, session)}
    for headers in (hub["owner"], hub["admin"], {**cookie(session), **csrf}):
        response = client.post("/v1/worker/claim", headers={**headers, **PROTOCOL})
        assert response.status_code == 403, response.text
        assert "worker token" in response.json()["message"]
    # nor does a forged worker token, nor no credential at all
    assert client.post("/v1/worker/claim", headers=worker_headers("evw_" + "A" * 43)).status_code == 401
    missing = client.post("/v1/worker/claim", headers=PROTOCOL)
    assert missing.status_code == 401 and "evo-agents worker join" in missing.json()["message"]


def test_the_existing_credentials_keep_their_routes(client, hub, github):
    # a machine token and a web session still work where they did, and a token of the other kind still is no session
    assert client.get("/v1/projects", headers=hub["owner"]).status_code == 200
    session = web_sign_in(client, github, Account(OWNER, OWNER_ID))
    assert client.get("/v1/projects", headers=cookie(session)).status_code == 200
    assert client.get("/v1/projects", headers=bearer(session)).status_code == 401
    assert client.get("/v1/projects", headers=cookie(hub["raw"]["token"])).status_code == 401
    assert client.get("/v1/projects", headers=bearer("evh_" + "A" * 43)).status_code == 401
    principal = client.portal.call(
        authenticate,
        client.app.state.engine,
        live.sign_in(client, github, ADMIN, ADMIN_ID)["token"],
        MACHINE,
        client.app.state.config,
    )
    assert principal.admin is True  # an admin's machine token is an admin's, as before
    assert client.get("/v1/health/live").status_code == 200  # public paths stay public


def test_the_worker_routes_need_the_protocol_header(client, hub):
    pairing = paired(client, hub["owner"])
    body = {"code": pairing["code"], **HOST}
    for headers in ({}, {"X-Evo-Worker-Protocol": "2"}, {"X-Evo-Worker-Protocol": ""}):
        response = client.post("/v1/worker/join", json=body, headers=headers)
        assert response.status_code == 426, response.text
        assert response.json()["error"] == "upgrade_required" and "X-Evo-Worker-Protocol" in response.json()["message"]
    assert client.post("/v1/worker/claim", headers=hub["owner"]).status_code == 426  # before any credential
    assert client.get(f"/v1/workers/pairings/{pairing['id']}", headers=hub["owner"]).json()["tries_left"] == 5
    assert joined(client, pairing["code"])["worker"]["name"] == "mac-mini"
    # the members' routes do not use it
    assert client.get("/v1/workers", headers=hub["owner"]).status_code == 200


# Listing, drain, undrain, revoke


def test_a_member_sees_only_their_own_workers_and_a_hub_admin_sees_all(client, hub):
    mine = joined(client, paired(client, hub["owner"])["code"])["worker"]
    theirs = joined(client, paired(client, hub["other"], name="their-box")["code"])["worker"]
    assert [w["id"] for w in client.get("/v1/workers", headers=hub["owner"]).json()] == [mine["id"]]
    assert [w["id"] for w in client.get("/v1/workers", headers=hub["other"]).json()] == [theirs["id"]]
    assert {w["id"] for w in client.get("/v1/workers", headers=hub["admin"]).json()} == {mine["id"], theirs["id"]}

    assert client.get(f"/v1/workers/{mine['id']}", headers=hub["owner"]).json() == mine
    for path in (f"/v1/workers/{mine['id']}", "/v1/workers/999999"):
        response = client.get(path, headers=hub["other"])
        assert response.status_code == 404 and response.json()["error"] == "not_found"
    for action in ("drain", "undrain", "revoke"):
        assert client.post(f"/v1/workers/{mine['id']}/{action}", headers=hub["other"]).status_code == 404
    assert client.get(f"/v1/workers/{mine['id']}", headers=hub["admin"]).status_code == 200


def test_drain_and_undrain_change_the_status_once_and_only_the_owner_undrains(client, hub, hub_db):
    worker = joined(client, paired(client, hub["owner"])["code"])
    worker_id = worker["worker"]["id"]
    target = f"worker:{worker_id} name=mac-mini owner={OWNER}"
    workers = tables.workers
    sql(hub_db, update(workers).values(last_heartbeat_at=func.now()).where(workers.c.id == worker_id))
    assert client.get(f"/v1/workers/{worker_id}", headers=hub["owner"]).json()["status"] == "online"
    for _ in range(2):  # the second drain changes nothing and writes no audit row
        drained = client.post(f"/v1/workers/{worker_id}/drain", headers=hub["owner"])
        assert drained.status_code == 200 and drained.json()["status"] == "draining"
    assert len(audit_rows(hub_db, "worker.drain")) == 1
    # a hub admin may stop another member's worker, never set it going again
    refused = client.post(f"/v1/workers/{worker_id}/undrain", headers=hub["admin"])
    assert refused.status_code == 403 and f"only {OWNER}" in refused.json()["message"]
    assert client.get(f"/v1/workers/{worker_id}", headers=hub["owner"]).json()["status"] == "draining"
    assert audit_rows(hub_db, "worker.undrain") == []
    undrained = client.post(f"/v1/workers/{worker_id}/undrain", headers=hub["owner"])
    assert undrained.status_code == 200 and undrained.json()["status"] == "online"
    assert audit_rows(hub_db, "worker.undrain") == [(target, OWNER)]
    drained = client.post(f"/v1/workers/{worker_id}/drain", headers=hub["admin"])
    assert drained.status_code == 200 and drained.json()["status"] == "draining"
    assert sorted(login for _, login in audit_rows(hub_db, "worker.drain")) == sorted([OWNER, ADMIN])


def web_writes(client, github, login: str, github_id: int) -> dict:
    """The headers of a write made with a web session of ``login``: its cookie and its CSRF header."""
    session = web_sign_in(client, github, Account(login, github_id))
    return {**cookie(session), "X-Evo-CSRF": csrf_for(client, session)}


def test_dispatch_from_is_set_by_the_owner_from_a_web_session_only_and_audited(client, hub, hub_db, github):
    answer = joined(client, paired(client, hub["owner"])["code"])
    worker_id = answer["worker"]["id"]
    path = f"/v1/workers/{worker_id}/dispatch-from"
    assert answer["worker"]["dispatch_from"] == "any"  # as every worker before schema 0011
    owner_web = web_writes(client, github, OWNER, OWNER_ID)

    # a token never sets it, so one that leaked cannot open the worker again: the owner's machine token and a hub
    # admin's get 403 before any worker is looked at, and the worker's own token is refused off its routes
    for headers in (hub["owner"], hub["admin"]):
        for some_id in (worker_id, 999999):
            refused = client.post(f"/v1/workers/{some_id}/dispatch-from", json={"value": "web"}, headers=headers)
            assert refused.status_code == 403 and "web session only" in refused.json()["message"], refused.text
    assert client.post(path, json={"value": "web"}, headers=worker_headers(answer["token"])).status_code == 403
    session_only = {"Cookie": owner_web["Cookie"]}  # the cookie without its CSRF header
    assert client.post(path, json={"value": "web"}, headers=session_only).status_code == 403
    assert sql(hub_db, select(tables.workers.c.dispatch_from)) == [("any",)]
    assert audit_rows(hub_db, "worker.dispatch_from") == []

    for _ in range(2):  # the second changes nothing and writes no audit row
        set_web = client.post(path, json={"value": "web"}, headers=owner_web)
        assert set_web.status_code == 200, set_web.text
        assert set_web.json()["dispatch_from"] == "web"
    assert client.get(f"/v1/workers/{worker_id}", headers=hub["owner"]).json()["dispatch_from"] == "web"
    assert [w["dispatch_from"] for w in client.get("/v1/workers", headers=hub["owner"]).json()] == ["web"]
    target = f"worker:{worker_id} name=mac-mini owner={OWNER}"
    assert audit_rows(hub_db, "worker.dispatch_from") == [(f"{target} dispatch_from=web", OWNER)]
    for body in ({"value": "machine"}, {"value": None}, {}):
        assert client.post(path, json=body, headers=owner_web).status_code == 422, body

    # a hub admin signed in on the web sees the worker and may drain it, never open it again; another member gets the
    # 404 of a worker they cannot see
    refused = client.post(path, json={"value": "any"}, headers=web_writes(client, github, ADMIN, ADMIN_ID))
    assert refused.status_code == 403 and f"only {OWNER}, who owns worker {worker_id}" in refused.json()["message"]
    other_web = web_writes(client, github, OTHER, OTHER_ID)
    assert client.post(path, json={"value": "any"}, headers=other_web).status_code == 404
    assert sql(hub_db, select(tables.workers.c.dispatch_from)) == [("web",)]

    back = client.post(path, json={"value": "any"}, headers=owner_web)
    assert back.status_code == 200 and back.json()["dispatch_from"] == "any"
    assert sorted(audit_rows(hub_db, "worker.dispatch_from")) == [
        (f"{target} dispatch_from=any", OWNER),
        (f"{target} dispatch_from=web", OWNER),
    ]

    # a revoked worker takes no runs from anywhere
    assert client.post(f"/v1/workers/{worker_id}/revoke", headers=hub["owner"]).status_code == 200
    gone = client.post(path, json={"value": "web"}, headers=owner_web)
    assert gone.status_code == 409 and "revoked" in gone.json()["message"]


# Settings: slots, labels and name


def settings_of(db, worker_id: int) -> tuple:
    workers = tables.workers
    return sql(db, select(workers.c.slots, workers.c.labels, workers.c.name).where(workers.c.id == worker_id))[0]


def test_the_owner_patches_the_worker_settings_from_the_web_or_a_machine_token_and_each_change_is_audited(
    client, hub, hub_db, github
):
    answer = joined(client, paired(client, hub["owner"], labels=["mac"])["code"])
    worker_id = answer["worker"]["id"]
    path = f"/v1/workers/{worker_id}"
    owner_web = web_writes(client, github, OWNER, OWNER_ID)

    changed = client.patch(path, json={"slots": 16, "labels": ["gpu", "mac", "gpu"]}, headers=owner_web)
    assert changed.status_code == 200, changed.text
    assert (changed.json()["slots"], changed.json()["labels"], changed.json()["name"]) == (
        16,
        ["gpu", "mac"],
        "mac-mini",
    )
    assert settings_of(hub_db, worker_id) == (16, ["gpu", "mac"], "mac-mini")
    target = f"worker:{worker_id} name=mac-mini owner={OWNER}"
    assert audit_rows(hub_db, "worker.update") == [(f"{target} slots=1->16 labels=[mac]->[gpu,mac]", OWNER)]

    # the values it has already change nothing and write no audit row, from either credential
    for headers in (owner_web, hub["owner"]):
        same = client.patch(path, json={"slots": 16, "labels": ["gpu", "mac"], "name": "mac-mini"}, headers=headers)
        assert same.status_code == 200 and same.json()["slots"] == 16, same.text
    assert len(audit_rows(hub_db, "worker.update")) == 1

    # the owner's machine token changes them too; a name differing only in case is a change of the worker's own name
    renamed = client.patch(path, json={"name": "Mac-Mini", "slots": 3, "labels": []}, headers=hub["owner"])
    assert renamed.status_code == 200, renamed.text
    assert (renamed.json()["name"], renamed.json()["slots"], renamed.json()["labels"]) == ("Mac-Mini", 3, [])
    assert (f"{target} slots=16->3 labels=[gpu,mac]->[] name=mac-mini->Mac-Mini", OWNER) in audit_rows(
        hub_db, "worker.update"
    )
    assert client.get(path, headers=hub["owner"]).json()["name"] == "Mac-Mini"
    assert [w["slots"] for w in client.get("/v1/workers", headers=hub["owner"]).json()] == [3]
    assert len(audit_rows(hub_db, "worker.update")) == 2


def test_a_patch_of_the_worker_settings_is_refused_to_all_but_its_owner_and_for_bad_values(client, hub, hub_db, github):
    answer = joined(client, paired(client, hub["owner"])["code"])
    worker_id = answer["worker"]["id"]
    path = f"/v1/workers/{worker_id}"
    owner_web = web_writes(client, github, OWNER, OWNER_ID)
    joined(client, paired(client, hub["owner"], name="build-box")["code"])

    # a hub admin sees the worker and may drain it, never change it: 403, by token or on the web; anyone else 404
    for headers in (hub["admin"], web_writes(client, github, ADMIN, ADMIN_ID)):
        refused = client.patch(path, json={"slots": 4}, headers=headers)
        assert refused.status_code == 403 and f"only {OWNER}, who owns worker {worker_id}" in refused.json()["message"]
    for headers in (hub["other"], web_writes(client, github, OTHER, OTHER_ID)):
        assert client.patch(path, json={"slots": 4}, headers=headers).status_code == 404
    assert client.patch("/v1/workers/999999", json={"slots": 4}, headers=hub["owner"]).status_code == 404
    # a web session writes only with its CSRF header; the worker's own token works on its routes only
    assert client.patch(path, json={"slots": 4}, headers={"Cookie": owner_web["Cookie"]}).status_code == 403
    assert client.patch(path, json={"slots": 4}, headers=worker_headers(answer["token"])).status_code == 403

    # a name another live worker of the owner holds, in any case, is 409
    taken = client.patch(path, json={"name": "BUILD-BOX", "slots": 4}, headers=owner_web)
    assert taken.status_code == 409 and "BUILD-BOX" in taken.json()["message"]

    # values out of range, or none at all, are 422
    for body in (
        {"slots": 0},
        {"slots": 17},
        {"slots": "many"},
        {"labels": ["no spaces"]},
        {"labels": [f"l{n}" for n in range(17)]},
        {"name": "-starts-with-a-dash"},
        {"name": "x" * 101},
        {},
        {"slots": None},
    ):
        refused = client.patch(path, json=body, headers=owner_web)
        assert refused.status_code == 422, (body, refused.text)
    assert settings_of(hub_db, worker_id) == (1, [], "mac-mini")
    assert audit_rows(hub_db, "worker.update") == []

    # a revoked worker is 409, and its name is free for another worker of the owner
    assert client.post(f"/v1/workers/{worker_id}/revoke", headers=hub["owner"]).status_code == 200
    gone = client.patch(path, json={"slots": 4}, headers=owner_web)
    assert gone.status_code == 409 and "revoked" in gone.json()["message"]
    box = [w for w in client.get("/v1/workers", headers=hub["owner"]).json() if w["name"] == "build-box"][0]
    reused = client.patch(f"/v1/workers/{box['id']}", json={"name": "mac-mini"}, headers=owner_web)
    assert reused.status_code == 200 and reused.json()["name"] == "mac-mini"


def test_pairings_registrations_and_heartbeats_take_up_to_sixteen_slots(client, hub, hub_db):
    assert pair(client, hub["owner"], slots=17).status_code == 422
    answer = joined(client, paired(client, hub["owner"], slots=16)["code"])
    assert answer["worker"]["slots"] == 16
    body = {"name": "build-box", "projects": ["demo"], **HOST}
    assert client.post("/v1/workers", json={**body, "slots": 17}, headers=hub["owner"]).status_code == 422
    registered = client.post("/v1/workers", json={**body, "slots": 16}, headers=hub["owner"])
    assert registered.status_code == 201 and registered.json()["worker"]["slots"] == 16
    beat = {"free_slots": 16}
    assert client.post("/v1/worker/heartbeat", json=beat, headers=worker_headers(answer["token"])).status_code == 200
    assert client.get(f"/v1/workers/{answer['worker']['id']}", headers=hub["owner"]).json()["free_slots"] == 16
    too_many = client.post("/v1/worker/heartbeat", json={"free_slots": 17}, headers=worker_headers(answer["token"]))
    assert too_many.status_code == 422


def test_a_heartbeat_answer_carries_the_settings_the_owner_patched(client, hub, hub_db):
    answer = joined(client, paired(client, hub["owner"], slots=2, labels=["mac"])["code"])
    worker_id, headers = answer["worker"]["id"], worker_headers(answer["token"])

    def beat(free_slots: int) -> dict:
        response = client.post("/v1/worker/heartbeat", json={"free_slots": free_slots}, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    settings = {"plugins": [], "version_locked": False, "projects": ["demo"]}
    assert beat(2)["settings"] == {"slots": 2, "name": "mac-mini", "labels": ["mac"], **settings}
    patched = client.patch(f"/v1/workers/{worker_id}", json={"slots": 6, "name": "big-mac"}, headers=hub["owner"])
    assert patched.status_code == 200, patched.text
    first = beat(2)  # what the daemon held before it read the answer
    assert first["settings"] == {"slots": 6, "name": "big-mac", "labels": ["mac"], **settings}
    assert first["drain"] is False and first["runs"] == []
    assert client.get(f"/v1/workers/{worker_id}", headers=hub["owner"]).json()["free_slots"] == 2
    beat(6)
    assert client.get(f"/v1/workers/{worker_id}", headers=hub["owner"]).json()["free_slots"] == 6
    # fewer slots than the daemon reported free: the hub keeps free_slots within the slots it holds
    client.patch(f"/v1/workers/{worker_id}", json={"slots": 1}, headers=hub["owner"])
    assert beat(6)["settings"]["slots"] == 1
    assert client.get(f"/v1/workers/{worker_id}", headers=hub["owner"]).json()["free_slots"] == 1


# The version lock, and what a daemon says of its install and of updating itself

COMMIT = "ca3e9af" + "0" * 33
NIGHTLY = "f" * 40


def locked_of(db, worker_id: int) -> bool:
    workers = tables.workers
    return sql(db, select(workers.c.version_locked).where(workers.c.id == worker_id))[0][0]


def test_the_owner_locks_the_worker_version_with_a_patch_audited_once_and_the_heartbeat_answer_carries_the_lock(
    client, hub, hub_db, github
):
    answer = joined(client, paired(client, hub["owner"])["code"])
    worker_id, headers = answer["worker"]["id"], worker_headers(answer["token"])
    path = f"/v1/workers/{worker_id}"
    owner_web = web_writes(client, github, OWNER, OWNER_ID)
    # every worker starts unlocked: one that joined, and one its owner registered with a machine token
    assert answer["worker"]["version_locked"] is False
    body = {"name": "build-box", "projects": ["demo"], **HOST}
    registered = client.post("/v1/workers", json=body, headers=hub["owner"])
    assert registered.status_code == 201 and registered.json()["worker"]["version_locked"] is False

    def beat() -> dict:
        response = client.post("/v1/worker/heartbeat", json={"free_slots": 1}, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()["settings"]

    assert beat()["version_locked"] is False

    locked = client.patch(path, json={"version_locked": True}, headers=owner_web)
    assert locked.status_code == 200, locked.text
    assert locked.json()["version_locked"] is True and locked_of(hub_db, worker_id) is True
    target = f"worker:{worker_id} name=mac-mini owner={OWNER}"
    assert audit_rows(hub_db, "worker.update") == [(f"{target} version_locked=false->true", OWNER)]
    assert beat()["version_locked"] is True
    assert client.get(path, headers=hub["owner"]).json()["version_locked"] is True
    shown = {w["id"]: w["version_locked"] for w in client.get("/v1/workers", headers=hub["owner"]).json()}
    assert shown == {worker_id: True, registered.json()["worker"]["id"]: False}

    # the lock it has already changes nothing and writes no audit row, from either credential
    for credential in (owner_web, hub["owner"]):
        same = client.patch(path, json={"version_locked": True, "slots": 1}, headers=credential)
        assert same.status_code == 200 and same.json()["version_locked"] is True, same.text
    assert len(audit_rows(hub_db, "worker.update")) == 1

    # the owner's machine token unlocks it, with another setting in the same change
    unlocked = client.patch(path, json={"version_locked": False, "slots": 2}, headers=hub["owner"])
    assert unlocked.status_code == 200, unlocked.text
    assert (unlocked.json()["version_locked"], unlocked.json()["slots"]) == (False, 2)
    assert (f"{target} slots=1->2 version_locked=true->false", OWNER) in audit_rows(hub_db, "worker.update")
    unlocked_settings = {"slots": 2, "name": "mac-mini", "labels": [], "plugins": [], "version_locked": False}
    assert beat() == {**unlocked_settings, "projects": ["demo"]}


def test_a_patch_of_the_version_lock_is_refused_to_all_but_its_owner_and_for_bad_values(client, hub, hub_db, github):
    answer = joined(client, paired(client, hub["owner"])["code"])
    worker_id = answer["worker"]["id"]
    path = f"/v1/workers/{worker_id}"
    owner_web = web_writes(client, github, OWNER, OWNER_ID)
    lock = {"version_locked": True}

    for headers in (hub["admin"], web_writes(client, github, ADMIN, ADMIN_ID)):
        refused = client.patch(path, json=lock, headers=headers)
        assert refused.status_code == 403 and f"only {OWNER}, who owns worker {worker_id}" in refused.json()["message"]
    for headers in (hub["other"], web_writes(client, github, OTHER, OTHER_ID)):
        assert client.patch(path, json=lock, headers=headers).status_code == 404
    assert client.patch(path, json=lock, headers=worker_headers(answer["token"])).status_code == 403
    for body in ({"version_locked": "maybe"}, {"version_locked": None}, {"version_locked": [True]}):
        refused = client.patch(path, json=body, headers=owner_web)
        assert refused.status_code == 422, (body, refused.text)
    assert locked_of(hub_db, worker_id) is False
    assert audit_rows(hub_db, "worker.update") == []

    assert client.post(f"{path}/revoke", headers=hub["owner"]).status_code == 200
    gone = client.patch(path, json=lock, headers=owner_web)
    assert gone.status_code == 409 and "revoked" in gone.json()["message"]
    assert locked_of(hub_db, worker_id) is False


def test_a_heartbeat_keeps_the_commit_and_the_self_update_state_and_the_worker_views_show_them(client, hub, hub_db):
    answer = joined(client, paired(client, hub["owner"])["code"])
    worker_id, headers = answer["worker"]["id"], worker_headers(answer["token"])
    path, w = f"/v1/workers/{worker_id}", tables.workers
    assert (answer["worker"]["agent_commit"], answer["worker"]["self_update"]) == (None, None)

    def beat(**body) -> None:
        response = client.post("/v1/worker/heartbeat", json={"free_slots": 1, **body}, headers=headers)
        assert response.status_code == 200, response.text

    def shown() -> dict:
        one = client.get(path, headers=hub["owner"]).json()
        listed = [x for x in client.get("/v1/workers", headers=hub["owner"]).json() if x["id"] == worker_id]
        assert listed == [one]
        return one

    checked = "2026-10-19T10:00:00+00:00"
    waiting = {"state": "waiting", "nightly_commit": NIGHTLY, "checked_at": checked}
    beat(agent_commit=COMMIT, self_update=waiting)
    worker = shown()
    assert worker["agent_commit"] == COMMIT
    report = worker["self_update"]
    since = report.pop("since")
    assert report == {
        **waiting,
        "checked_at": "2026-10-19T10:00:00Z",
        "reason": None,
        "error": None,
        "error_at": None,
        "update_now": None,
    }
    assert since is not None

    # the same state keeps since, however many heartbeats say it; another state moves it to now
    hour_ago = datetime.now(UTC) - timedelta(hours=1)
    sql(hub_db, update(w).values(self_update_since=hour_ago).where(w.c.id == worker_id))
    failed = {**waiting, "error": "uv tool install failed (2): no network", "error_at": "2026-10-19T10:05:00+00:00"}
    beat(agent_commit=COMMIT, self_update=failed)
    report = shown()["self_update"]
    assert datetime.fromisoformat(report["since"]) == hour_ago
    assert (report["error"], report["error_at"]) == (failed["error"], "2026-10-19T10:05:00Z")
    beat(agent_commit=COMMIT, self_update={"state": "unsupported", "reason": "an editable install of evo-ak"})
    report = shown()["self_update"]
    assert (report["state"], report["reason"], report["error"]) == (
        "unsupported",
        "an editable install of evo-ak",
        None,
    )
    assert datetime.fromisoformat(report["since"]) > hour_ago

    # a state this hub does not know is kept as named; a commit of a SHA-256 repository is taken
    beat(agent_commit="a" * 64, self_update={"state": "paused_by_owner"})
    worker = shown()
    assert (worker["agent_commit"], worker["self_update"]["state"]) == ("a" * 64, "paused_by_owner")

    # a heartbeat that says neither, as an older daemon's, clears both
    beat()
    worker = shown()
    assert (worker["agent_commit"], worker["self_update"]) == (None, None)
    kept = sql(hub_db, select(w.c.agent_commit, w.c.self_update, w.c.self_update_since).where(w.c.id == worker_id))
    assert kept == [(None, None, None)]


def test_a_heartbeat_with_a_malformed_commit_or_self_update_is_422_and_changes_nothing(client, hub, hub_db):
    answer = joined(client, paired(client, hub["owner"])["code"])
    worker_id, headers = answer["worker"]["id"], worker_headers(answer["token"])
    good = {"free_slots": 1, "agent_commit": COMMIT, "self_update": {"state": "current", "nightly_commit": COMMIT}}
    assert client.post("/v1/worker/heartbeat", json=good, headers=headers).status_code == 200
    for bad in (
        {"agent_commit": "ca3e9af"},
        {"agent_commit": COMMIT.upper()},
        {"self_update": {}},
        {"self_update": {"state": "Not A State"}},
        {"self_update": {"state": "unsupported", "reason": ""}},
        {"self_update": {"state": "unsupported", "reason": "r" * 501}},
        {"self_update": {"state": "waiting", "error": "e" * 2001}},
        {"self_update": {"state": "waiting", "nightly_commit": "nightly"}},
        {"self_update": {"state": "waiting", "error_at": "yesterday"}},
    ):
        refused = client.post("/v1/worker/heartbeat", json={"free_slots": 1, **bad}, headers=headers)
        assert refused.status_code == 422, (bad, refused.text)
    worker = client.get(f"/v1/workers/{worker_id}", headers=hub["owner"]).json()
    assert (worker["agent_commit"], worker["self_update"]["state"]) == (COMMIT, "current")


# The hub's read of nightly, and the owner's Update now


def read_nightly(db, commit: str = NIGHTLY) -> None:
    """What the job hub.read_nightly keeps when nightly points to ``commit``."""
    nightly = tables.hub_nightly
    sql(db, delete(nightly))
    sql(db, insert(nightly).values(commit_sha=commit, read_at=func.now()))


def test_the_heartbeat_answer_carries_nightly_once_the_hub_read_it_and_the_worker_views_show_it(client, hub, hub_db):
    answer = joined(client, paired(client, hub["owner"])["code"])
    worker_id, headers = answer["worker"]["id"], worker_headers(answer["token"])
    beat = {"free_slots": 1}
    before = client.post("/v1/worker/heartbeat", json=beat, headers=headers)
    assert before.status_code == 200 and "nightly" not in before.json() and "update_now" not in before.json()
    worker = client.get(f"/v1/workers/{worker_id}", headers=hub["owner"]).json()
    assert (worker["nightly"], worker["update_now"]) == (None, None)
    read_nightly(hub_db)
    nightly = client.post("/v1/worker/heartbeat", json=beat, headers=headers).json()["nightly"]
    assert nightly["commit"] == NIGHTLY and datetime.fromisoformat(nightly["read_at"])
    worker = client.get(f"/v1/workers/{worker_id}", headers=hub["owner"]).json()
    assert worker["nightly"] == nightly
    assert [x["nightly"] for x in client.get("/v1/workers", headers=hub["owner"]).json()] == [nightly]


def update_now_of(db, worker_id: int) -> tuple:
    w = tables.workers
    shown = select(w.c.update_requested_at, w.c.update_ended_at, w.c.update_outcome, w.c.update_reason)
    return sql(db, shown.where(w.c.id == worker_id))[0]


def test_an_update_now_is_asked_and_cancelled_with_the_rights_of_drain_and_audited_once(client, hub, hub_db):
    answer = joined(client, paired(client, hub["owner"])["code"])
    worker_id, headers = answer["worker"]["id"], worker_headers(answer["token"])
    path, target = f"/v1/workers/{worker_id}/update-now", f"worker:{worker_id} name=mac-mini owner={OWNER}"
    beat = {"free_slots": 1, "agent_commit": COMMIT, "features": ["update_now"]}
    assert client.post("/v1/worker/heartbeat", json=beat, headers=headers).status_code == 200
    read_nightly(hub_db)
    for credential in (hub["other"], worker_headers(answer["token"])):
        assert client.post(path, headers=credential).status_code in (403, 404)
        assert client.post(f"{path}/cancel", headers=credential).status_code in (403, 404)
    assert client.post("/v1/workers/999999/update-now", headers=hub["owner"]).status_code == 404
    assert update_now_of(hub_db, worker_id) == (None, None, None, None)

    for _ in range(2):  # the second request finds one open: nothing changes, no audit row
        asked = client.post(path, headers=hub["owner"])
        assert asked.status_code == 200, asked.text
        shown = asked.json()["update_now"]
        assert (shown["ended_at"], shown["outcome"], shown["reason"]) == (None, None, None)
    assert audit_rows(hub_db, "worker.update_now") == [(target, OWNER)]
    requested_at = update_now_of(hub_db, worker_id)[0]
    assert datetime.fromisoformat(shown["requested_at"]) == requested_at

    for _ in range(2):  # cancelling when none is open changes nothing either
        cancelled = client.post(f"{path}/cancel", headers=hub["owner"])
        assert cancelled.status_code == 200 and cancelled.json()["update_now"]["outcome"] == "cancelled"
    assert audit_rows(hub_db, "worker.update_now_cancel") == [(target, OWNER)]
    assert update_now_of(hub_db, worker_id)[2:] == ("cancelled", None)

    # a hub admin may ask for and cancel another member's Update now, as they may drain
    assert client.post(path, headers=hub["admin"]).json()["update_now"]["outcome"] is None
    assert client.post(f"{path}/cancel", headers=hub["admin"]).json()["update_now"]["outcome"] == "cancelled"
    assert [login for _, login in audit_rows(hub_db, "worker.update_now")] == [OWNER, ADMIN]
    assert [login for _, login in audit_rows(hub_db, "worker.update_now_cancel")] == [OWNER, ADMIN]

    assert client.post(f"/v1/workers/{worker_id}/revoke", headers=hub["owner"]).status_code == 200
    for route in (path, f"{path}/cancel"):
        gone = client.post(route, headers=hub["owner"])
        assert gone.status_code == 409 and "revoked" in gone.json()["message"]
    assert len(audit_rows(hub_db, "worker.update_now")) == 2


def test_an_update_now_is_refused_with_why_when_locked_too_old_or_current(client, hub, hub_db):
    answer = joined(client, paired(client, hub["owner"])["code"])
    worker_id, headers = answer["worker"]["id"], worker_headers(answer["token"])
    path = f"/v1/workers/{worker_id}/update-now"

    def refused(why: str) -> None:
        response = client.post(path, headers=hub["owner"])
        assert response.status_code == 409 and why in response.json()["message"], response.text

    # a daemon that does not say update_now, as an older one: it still updates itself once idle
    client.post("/v1/worker/heartbeat", json={"free_slots": 1, "agent_commit": COMMIT}, headers=headers)
    refused("does not say update_now")
    beat = {"free_slots": 1, "agent_commit": COMMIT, "features": ["update_now"]}
    client.post("/v1/worker/heartbeat", json=beat, headers=headers)
    assert client.patch(f"/v1/workers/{worker_id}", json={"version_locked": True}, headers=hub["owner"]).is_success
    refused("locked")
    client.patch(f"/v1/workers/{worker_id}", json={"version_locked": False}, headers=hub["owner"])
    read_nightly(hub_db, COMMIT)
    refused("already")
    assert update_now_of(hub_db, worker_id) == (None, None, None, None)
    assert audit_rows(hub_db, "worker.update_now") == []
    # before the hub has read nightly once, it cannot say the worker is current, and takes the request
    sql(hub_db, delete(tables.hub_nightly))
    assert client.post(path, headers=hub["owner"]).status_code == 200


def test_the_heartbeat_answer_carries_an_open_update_now_until_a_heartbeat_shows_it_ended(client, hub, hub_db):
    answer = joined(client, paired(client, hub["owner"])["code"])
    worker_id, headers = answer["worker"]["id"], worker_headers(answer["token"])
    path = f"/v1/workers/{worker_id}"
    says = {"free_slots": 1, "agent_commit": COMMIT, "features": ["update_now"]}
    read_nightly(hub_db)

    def beat(**body) -> dict:
        response = client.post("/v1/worker/heartbeat", json={**says, **body}, headers=headers)
        assert response.status_code == 200, response.text
        return response.json()

    def ask() -> str:
        asked = client.post(f"{path}/update-now", headers=hub["owner"])
        assert asked.status_code == 200, asked.text
        return asked.json()["update_now"]["requested_at"]

    def ended() -> tuple:
        shown = client.get(path, headers=hub["owner"]).json()["update_now"]
        listed = [x["update_now"] for x in client.get("/v1/workers", headers=hub["owner"]).json()]
        assert listed == [shown]
        return shown["outcome"], shown["reason"]

    beat()
    requested_at = ask()
    for _ in range(2):  # a daemon that carries it out says so in self_update; the hub keeps it open
        answer = beat(self_update={"state": "waiting", "update_now": {"requested_at": requested_at}})
        assert datetime.fromisoformat(answer["update_now"]["requested_at"]) == datetime.fromisoformat(requested_at)
    assert ended() == (None, None)
    # current: the daemon runs the commit of nightly the hub read
    assert "update_now" not in beat(agent_commit=NIGHTLY, self_update={"state": "current"})
    assert ended() == ("current", None)
    assert "update_now" not in beat()  # an ended request is never carried again

    # failed and impossible as the daemon reports them for this request, with why
    read_nightly(hub_db, "e" * 40)
    for outcome, report in (
        ("failed", {"state": "waiting", "error": "uv tool install failed (2): no network"}),
        ("impossible", {"state": "waiting", "reason": "a process of the install would not stop"}),
    ):
        requested_at = ask()
        # an end it reports for an earlier request is not this one's
        stale = {**report, "update_now": {"requested_at": "2026-01-01T00:00:00+00:00", "ended": outcome}}
        assert "update_now" in beat(self_update=stale)
        mine = {**report, "update_now": {"requested_at": requested_at, "ended": outcome}}
        assert "update_now" not in beat(self_update=mine)
        assert ended() == (outcome, report.get("error") or report["reason"])

    # impossible, as the hub sees it: a daemon that says locked or unsupported, or drops update_now
    for report, why in (
        ({"state": "unsupported", "reason": "an editable install of evo-agents"}, "an editable install of evo-agents"),
        ({"state": "locked"}, "its daemon says locked"),
    ):
        ask()
        assert "update_now" not in beat(self_update=report)
        assert ended() == ("impossible", why)
    ask()
    assert "update_now" not in beat(features=[])
    assert ended() == ("impossible", "its daemon does not say update_now in its features")


def seed_plan(db, project: str = "demo") -> dict:
    """The plan worker-fleet at revision 1 in ``project``, as ``add_run`` expects it."""
    projects, users = tables.projects, tables.users
    project_id = sql(db, select(projects.c.id).where(projects.c.name == project))[0][0]
    user_id = sql(db, select(users.c.id).where(users.c.login == OWNER))[0][0]
    body = {"id": PLAN, "steps": [{"id": step, "status": "in_progress"} for step in range(7)]}
    values = {
        "project_id": project_id,
        "plan_id": PLAN,
        "area": "active",
        "label": {},
        "body": body,
        "digest": "sha256:" + "f" * 64,
    }
    sql(db, insert(tables.plans).values(**values, updated_by=user_id))
    sql(db, insert(tables.plan_revisions).values(**values, revision=1, actor_id=user_id))
    return {"project": project_id, "user": user_id}


def test_a_hub_admin_revokes_a_worker_its_token_stops_and_its_runs_are_released(client, hub, hub_db):
    answer = joined(client, paired(client, hub["owner"])["code"])
    token, worker_id = answer["token"], answer["worker"]["id"]
    spare = joined(client, paired(client, hub["owner"], name="spare")["code"])["worker"]["id"]
    ids = seed_plan(hub_db) | {"worker": worker_id}
    with live.connect(hub_db) as conn:
        history = add_run(conn, ids, "done", step="0")  # finished before: left alone
        retried = add_run(conn, ids, "running", step="1")
        cancelled = add_run(conn, ids, "verifying", step="2", cancel_requested_at=datetime.now(UTC))
        last = add_run(conn, ids, "interactive", step="3", attempt=3, parent_run_id=history)
        pinned_held = add_run(conn, ids, "leased", step="4", pinned_worker_id=worker_id)
        pinned_queued = add_run(conn, ids, "queued", step="5", pinned_worker_id=worker_id)
        elsewhere = add_run(conn, ids | {"worker": spare}, "running", step="6")
    assert client.post("/v1/worker/no-such-route", headers=worker_headers(token)).status_code == 404
    assert client.get(f"/v1/workers/{worker_id}", headers=hub["owner"]).json()["held_runs"] == 4

    revoked = client.post(f"/v1/workers/{worker_id}/revoke", headers=hub["admin"])
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked" and revoked.json()["revoked_at"] is not None
    assert revoked.json()["held_runs"] == 0
    refused = client.post("/v1/worker/no-such-route", headers=worker_headers(token))
    assert refused.status_code == 401 and "worker token is revoked" in refused.json()["message"]
    tokens = tables.tokens
    revoked_token = select(tokens.c.revoked_at.is_not(None)).where(tokens.c.id == answer["token_id"])
    assert sql(hub_db, revoked_token) == [(True,)]
    again = client.post(f"/v1/workers/{worker_id}/revoke", headers=hub["owner"])
    assert again.status_code == 409 and "revoked already" in again.json()["message"]
    assert client.post(f"/v1/workers/{worker_id}/drain", headers=hub["owner"]).status_code == 409

    runs, run_events = tables.runs, tables.run_events
    states = dict(sql(hub_db, select(runs.c.id, runs.c.state)))
    assert {run: states[run] for run in (history, retried, cancelled, last, pinned_held, pinned_queued, elsewhere)} == {
        history: "done",
        retried: "lost",
        cancelled: "cancelled",
        last: "failed",
        pinned_held: "failed",
        pinned_queued: "failed",
        elsewhere: "running",  # another worker's run is left alone
    }
    # the lost run's step is queued again as the next attempt, unpinned, claimed by nobody, and asking for the
    # runtime its dispatch asked for (any), not the one the lost run's worker picked
    next_attempt = select(
        runs.c.step_key,
        runs.c.attempt,
        runs.c.state,
        runs.c.worker_id,
        runs.c.pinned_worker_id,
        runs.c.requested_runtime,
        runs.c.runtime,
        runs.c.mode,
    ).where(runs.c.parent_run_id == retried)
    assert sql(hub_db, next_attempt) == [("1", 2, "queued", None, None, "any", "any", "headless")]
    ended = select(runs.c.id, runs.c.error).where(runs.c.id.in_([last, pinned_held, pinned_queued]))
    errors = dict(sql(hub_db, ended))
    assert all(error.startswith("its worker mac-mini was revoked") for error in errors.values())
    assert "attempt 3 of 3" in errors[last] and "pinned" in errors[pinned_held]
    # each move left the state event the hub writes, numbered after the run's own events
    events = sql(
        hub_db,
        select(runs.c.id, run_events.c.seq, run_events.c.body)
        .join_from(run_events, runs, runs.c.id == run_events.c.run_id)
        .where(run_events.c.kind == "state"),
    )
    moved = {run: (seq, body["from"], body["to"], body["actor"]) for run, seq, body in events}
    assert moved == {
        retried: (1, "running", "lost", "reaper"),
        cancelled: (1, "verifying", "cancelled", "reaper"),
        last: (1, "interactive", "failed", "reaper"),
        pinned_held: (1, "leased", "failed", "reaper"),
        pinned_queued: (1, "queued", "failed", "reaper"),
    }
    # a run that failed got a second event, the system line that says its cause was inferred (plan run-failure-triage)
    assert count(hub_db, runs, runs.c.event_seq == 1) == [(2,)]
    assert count(hub_db, runs, runs.c.event_seq == 2) == [(3,)]
    assert audit_rows(hub_db, "worker.revoke") == [(f"worker:{worker_id} name=mac-mini owner={OWNER}", ADMIN)]

    # a revoked worker leaves the list unless asked for, and its name is free again
    assert [w["id"] for w in client.get("/v1/workers", headers=hub["owner"]).json()] == [spare]
    listed = client.get("/v1/workers", params={"revoked": "true"}, headers=hub["owner"]).json()
    assert {w["id"] for w in listed} == {worker_id, spare}
    assert pair(client, hub["owner"], name="mac-mini").status_code == 201


def test_the_owner_revokes_their_own_worker(client, hub, hub_db):
    answer = joined(client, paired(client, hub["owner"])["code"])
    assert client.post(f"/v1/workers/{answer['worker']['id']}/revoke", headers=hub["owner"]).status_code == 200
    assert client.post("/v1/worker/no-such-route", headers=worker_headers(answer["token"])).status_code == 401
    assert audit_rows(hub_db, "worker.revoke")[0][1] == OWNER


def test_revoking_a_worker_token_revokes_its_worker(client, hub, hub_db):
    answer = joined(client, paired(client, hub["owner"])["code"])
    token, token_id, worker_id = answer["token"], answer["token_id"], answer["worker"]["id"]
    ids = seed_plan(hub_db) | {"worker": worker_id}
    with live.connect(hub_db) as conn:
        held = add_run(conn, ids, "running", step="1")
    # another member's token is unknown to them, and their worker stays as it was
    assert client.delete(f"/v1/tokens/{token_id}", headers=hub["other"]).status_code == 404
    assert client.get(f"/v1/workers/{worker_id}", headers=hub["owner"]).json()["revoked_at"] is None

    assert client.delete(f"/v1/tokens/{token_id}", headers=hub["owner"]).status_code == 204
    shown = client.get(f"/v1/workers/{worker_id}", headers=hub["owner"]).json()
    assert (shown["status"], shown["held_runs"]) == ("revoked", 0)
    assert client.post("/v1/worker/no-such-route", headers=worker_headers(token)).status_code == 401
    runs = tables.runs
    assert sql(hub_db, select(runs.c.state).where(runs.c.id == held)) == [("lost",)]
    assert sql(hub_db, select(runs.c.state).where(runs.c.parent_run_id == held)) == [("queued",)]
    assert audit_rows(hub_db, "worker.revoke") == [(f"worker:{worker_id} name=mac-mini owner={OWNER}", OWNER)]
    assert audit_rows(hub_db, "token.revoke") == [(audit.token_target(token_id), OWNER)]
    again = client.post(f"/v1/workers/{worker_id}/revoke", headers=hub["owner"])
    assert again.status_code == 409 and "revoked already" in again.json()["message"]
    assert pair(client, hub["owner"], name="mac-mini").status_code == 201  # the name is free again

    # a hub admin revoking a worker token from the admin console ends that worker the same way
    desk = joined(client, paired(client, hub["owner"], name="desk")["code"])
    assert client.delete(f"/v1/admin/tokens/{desk['token_id']}", headers=hub["admin"]).status_code == 204
    assert client.get(f"/v1/workers/{desk['worker']['id']}", headers=hub["owner"]).json()["status"] == "revoked"
    assert (f"worker:{desk['worker']['id']} name=desk owner={OWNER}", ADMIN) in audit_rows(hub_db, "worker.revoke")
    # a token no worker holds revokes no worker
    tokens, users = tables.tokens, tables.users
    ((machine_id,),) = sql(
        hub_db,
        select(tokens.c.id)
        .join_from(tokens, users, users.c.id == tokens.c.user_id)
        .where(tokens.c.kind == "machine", users.c.login == OTHER),
    )
    assert client.delete(f"/v1/admin/tokens/{machine_id}", headers=hub["admin"]).status_code == 204
    assert len(audit_rows(hub_db, "worker.revoke")) == 2


# Logs


def test_no_log_record_holds_a_code_or_a_token(client, hub, hub_db, caplog):
    caplog.set_level(logging.DEBUG)
    pairing = paired(client, hub["owner"])
    other = paired(client, hub["owner"], name="desk")
    wrong = wrong_rest(other["code"], 2)
    for code in wrong:
        assert join(client, code).status_code == 403
    answer = joined(client, pairing["code"])
    assert join(client, pairing["code"]).status_code == 403
    registered = client.post("/v1/workers", json={"name": "box", "projects": ["demo"], **HOST}, headers=hub["owner"])
    assert registered.status_code == 201
    for token in (answer["token"], registered.json()["token"]):
        assert client.post("/v1/worker/no-such-route", headers=worker_headers(token)).status_code == 404
        assert client.get("/v1/projects", headers=bearer(token)).status_code == 403
    assert client.post(f"/v1/workers/{answer['worker']['id']}/revoke", headers=hub["owner"]).status_code == 200
    assert client.post("/v1/worker/no-such-route", headers=worker_headers(answer["token"])).status_code == 401

    assert {"worker pairing created", "worker joined", "worker registered", "wrong pairing code"} <= {
        record.getMessage() for record in caplog.records
    }
    raw = caplog.text + "\n".join(
        f"{record.getMessage()} {record.args!r} {vars(record)!r}" for record in caplog.records
    )
    secrets = {answer["token"], registered.json()["token"], hub["raw"]["token"]}
    for code in (pairing["code"], other["code"], *wrong):
        secrets |= {code, code.replace("-", "")}
    for secret in secrets:
        assert secret not in raw, "a code or a token reached a log record"


def test_a_worker_token_in_any_log_line_is_masked():
    token = "evw_" + "Wk-_9" * 8 + "abc"
    assert scrub(f"worker said {token} at join") == "worker said evw_*** at join"


def test_a_pairing_code_in_any_log_line_is_masked():
    assert scrub("joined with 7K2M-Q9XZ now") == "joined with *** now"
    assert scrub('{"code": "ABCD-EF12"}') == '{"code": "***"}'
    for kept in ("123E4567-E89B-12D3-A456-426614174000", "fast-path", "ABCD-EFGHJ", "XABCD-EFGH", "ILOU-1234"):
        assert scrub(kept) == kept


# The projects a worker serves


def projects_of(db, worker_id: int) -> list[str]:
    wp, projects = tables.worker_projects, tables.projects
    names = (
        select(projects.c.name)
        .join_from(wp, projects, projects.c.id == wp.c.project_id)
        .where(wp.c.worker_id == worker_id)
        .order_by(projects.c.name)
    )
    return [name for (name,) in sql(db, names)]


def test_the_owner_changes_the_projects_of_a_worker_in_place_audited_and_sent_to_its_daemon(
    client, hub, hub_db, github
):
    answer = joined(client, paired(client, hub["owner"])["code"])
    worker_id, token = answer["worker"]["id"], answer["token"]
    path = f"/v1/workers/{worker_id}"

    added = client.patch(path, json={"projects": ["docs", "demo", "docs"]}, headers=hub["owner"])
    assert added.status_code == 200, added.text
    assert added.json()["projects"] == ["demo", "docs"] and added.json()["id"] == worker_id
    assert projects_of(hub_db, worker_id) == ["demo", "docs"]
    target = f"worker:{worker_id} name=mac-mini owner={OWNER}"
    assert audit_rows(hub_db, "worker.update") == [(f"{target} projects=[demo]->[demo,docs]", OWNER)]
    beat = client.post("/v1/worker/heartbeat", json={"free_slots": 1}, headers=worker_headers(token))
    assert beat.status_code == 200 and beat.json()["settings"]["projects"] == ["demo", "docs"], beat.text

    # the same projects in another order change nothing, from the web too; the token and the other settings stay
    owner_web = web_writes(client, github, OWNER, OWNER_ID)
    same = client.patch(path, json={"projects": ["docs", "demo"]}, headers=owner_web)
    assert same.status_code == 200 and same.json()["projects"] == ["demo", "docs"], same.text
    assert len(audit_rows(hub_db, "worker.update")) == 1
    both = client.patch(path, json={"projects": ["docs"], "slots": 2}, headers=owner_web)
    assert both.status_code == 200 and (both.json()["projects"], both.json()["slots"]) == (["docs"], 2)
    assert (f"{target} slots=1->2 projects=[demo,docs]->[docs]", OWNER) in audit_rows(hub_db, "worker.update")
    assert client.post("/v1/worker/heartbeat", json={"free_slots": 1}, headers=worker_headers(token)).status_code == 200


def test_a_project_added_needs_the_writer_role_and_one_served_already_stays_on_a_lesser_role(
    client, hub, hub_db, admin
):
    granted = client.put(f"/v1/admin/projects/notes/grants/{OWNER}", json=WRITER, headers=admin)
    assert granted.status_code == 200, granted.text
    worker_id = joined(client, paired(client, hub["owner"], projects=["demo", "notes"])["code"])["worker"]["id"]
    path = f"/v1/workers/{worker_id}"
    # the owner holds reader on notes now, as the owner of a worker that serves another member's project does on the hub
    lowered = client.put(f"/v1/admin/projects/notes/grants/{OWNER}", json=READER, headers=admin)
    assert lowered.status_code == 200, lowered.text

    kept = client.patch(path, json={"projects": ["demo", "notes", "docs"]}, headers=hub["owner"])
    assert kept.status_code == 200, kept.text
    assert kept.json()["projects"] == projects_of(hub_db, worker_id) == ["demo", "docs", "notes"]

    # dropped, notes is a project the owner only reads: adding it again is 403
    assert client.patch(path, json={"projects": ["demo", "docs"]}, headers=hub["owner"]).status_code == 200
    refused = client.patch(path, json={"projects": ["demo", "docs", "notes"]}, headers=hub["owner"])
    assert refused.status_code == 403, refused.text
    assert "writer role" in refused.json()["message"] and "reader" in refused.json()["message"]
    # a project the owner holds no grant on, and one the hub does not have, are 404 alike
    add_project(hub_db, "hidden")
    for name in ("hidden", "nowhere"):
        unseen = client.patch(path, json={"projects": ["demo", name]}, headers=hub["owner"])
        assert unseen.status_code == 404, unseen.text
    assert projects_of(hub_db, worker_id) == ["demo", "docs"]
    assert len(audit_rows(hub_db, "worker.update")) == 2

    # an empty list, a name the hub would not take, or too many are 422
    for projects in ([], ["Not A Name"], [f"p{n}" for n in range(101)]):
        assert client.patch(path, json={"projects": projects}, headers=hub["owner"]).status_code == 422, projects


def test_the_projects_of_a_worker_are_changed_by_its_owner_alone(client, hub, hub_db, github):
    worker_id = joined(client, paired(client, hub["owner"])["code"])["worker"]["id"]
    path, body = f"/v1/workers/{worker_id}", {"projects": ["demo", "docs"]}
    for headers in (hub["admin"], web_writes(client, github, ADMIN, ADMIN_ID)):
        refused = client.patch(path, json=body, headers=headers)
        assert refused.status_code == 403 and f"only {OWNER}, who owns worker {worker_id}" in refused.json()["message"]
    # another member who writes to demo, even with the worker shared with them, sees it and changes nothing
    assert client.patch(path, json={"shared": True}, headers=hub["owner"]).status_code == 200
    assert client.get(path, headers=hub["other"]).status_code == 200
    assert client.patch(path, json=body, headers=hub["other"]).status_code == 404
    assert projects_of(hub_db, worker_id) == ["demo"]
    assert client.post(f"/v1/workers/{worker_id}/revoke", headers=hub["owner"]).status_code == 200
    gone = client.patch(path, json=body, headers=hub["owner"])
    assert gone.status_code == 409 and "revoked" in gone.json()["message"]


def test_a_project_is_not_dropped_while_a_run_of_it_is_active_on_the_worker_or_its_night_shift_is_on_duty_there(
    client, hub, hub_db
):
    answer = joined(client, paired(client, hub["owner"], projects=["demo", "docs"])["code"])
    worker_id = answer["worker"]["id"]
    path = f"/v1/workers/{worker_id}"
    ids = seed_plan(hub_db, "docs") | {"worker": worker_id}
    with live.connect(hub_db) as conn:
        held = add_run(conn, ids, "running", step="1")
        pinned = add_run(conn, ids, "queued", step="2", pinned_worker_id=worker_id)
        add_run(conn, ids, "done", step="3")  # ended: it needs the worker no more
    projects = tables.projects
    demo = sql(hub_db, select(projects.c.id).where(projects.c.name == "demo"))[0][0]
    duty = {"project_id": demo, "kind": "night_shift", "owner_id": ids["user"], "worker_id": worker_id}
    sql(hub_db, insert(tables.schedules).values(**duty))

    busy = client.patch(path, json={"projects": ["demo"]}, headers=hub["owner"])
    assert busy.status_code == 409, busy.text
    said = busy.json()["message"]
    assert "cannot stop serving docs" in said and f"({held}, {pinned})" in said and "nothing was changed" in said
    on_duty = client.patch(path, json={"projects": ["docs"]}, headers=hub["owner"])
    assert on_duty.status_code == 409, on_duty.text
    assert "cannot stop serving demo" in on_duty.json()["message"]
    assert "evo-agents hub curator charter" in on_duty.json()["message"]
    assert projects_of(hub_db, worker_id) == ["demo", "docs"] and audit_rows(hub_db, "worker.update") == []

    # once the runs ended, docs goes; the night shift of demo keeps demo, and the worker's id and token stay
    runs_table = tables.runs
    ended = runs_table.c.id.in_([held, pinned])
    sql(hub_db, update(runs_table).values(state="cancelled", finished_at=func.now()).where(ended))
    dropped = client.patch(path, json={"projects": ["demo"]}, headers=hub["owner"])
    assert dropped.status_code == 200 and dropped.json()["projects"] == ["demo"], dropped.text
    beat = client.post("/v1/worker/heartbeat", json={"free_slots": 1}, headers=worker_headers(answer["token"]))
    assert beat.status_code == 200 and beat.json()["settings"]["projects"] == ["demo"]
