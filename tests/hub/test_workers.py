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
and a machine token gets 403 there."""

import hashlib
import hmac
import logging
from datetime import UTC, datetime

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

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
    return sql(
        db, "SELECT a.target, u.login FROM audit a JOIN users u ON u.id = a.actor_id WHERE action = %s", (action,)
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
    assert sql(hub_db, "SELECT count(*) FROM workers") == [(1,)]

    # the token is a worker token of the owner, with the machine's hostname, kept as its hash only
    assert sql(hub_db, "SELECT kind, host, token_hash FROM tokens WHERE id = %s", (answer["token_id"],)) == [
        ("worker", HOST["hostname"], hash_token(token))
    ]
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
    sql(
        hub_db,
        "UPDATE worker_pairings SET created_at = created_at - interval '11 minutes', "
        "expires_at = expires_at - interval '11 minutes' WHERE id = %s",
        (pairing["id"],),
    )
    refused = join(client, pairing["code"])
    assert refused.status_code == 403 and "expired" in refused.json()["message"]
    assert client.get(f"/v1/workers/pairings/{pairing['id']}", headers=hub["owner"]).json()["status"] == "expired"
    assert sql(hub_db, "SELECT count(*) FROM workers") == [(0,)]
    assert sql(hub_db, "SELECT attempts FROM worker_pairings") == [(0,)]  # a dead code counts no tries


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
    assert sql(hub_db, "SELECT count(*) FROM workers") == [(0,)]
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
    sql(
        hub_db,
        "UPDATE worker_pairings SET created_at = created_at - interval '11 minutes', "
        "expires_at = expires_at - interval '11 minutes' WHERE id = %s",
        (pairings[0]["id"],),
    )
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
    assert sql(hub_db, "SELECT count(*) FROM workers") == [(1,)]


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
    assert sql(hub_db, "SELECT attempts FROM worker_pairings") == [(0,)]


def test_pairings_need_the_writer_role_on_every_project(client, hub, hub_db, github):
    reader_only = pair(client, hub["owner"], projects=["demo", "notes"])
    assert reader_only.status_code == 403 and "writer role" in reader_only.json()["message"]
    assert pair(client, hub["owner"], projects=["nowhere"]).status_code == 404
    # a hub admin without a grant manages a project but runs nothing in it
    assert pair(client, hub["admin"], projects=["demo"]).status_code == 403
    assert sql(hub_db, "SELECT count(*) FROM worker_pairings") == [(0,)]
    assert sql(hub_db, "SELECT count(*) FROM audit WHERE action ~ '^worker[.]'") == [(0,)]


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
    ((selector, stored),) = sql(hub_db, "SELECT code_selector, code_hash FROM worker_pairings")
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
        assert sql(hub_db, "SELECT count(*) FROM worker_pairings") == [(0,)]
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
    assert sql(hub_db, "SELECT count(*) FROM workers") == [(0,)]
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
    assert sql(hub_db, "SELECT count(*) FROM workers") == [(1,)]


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
    principal = client.portal.call(authenticate, client.app.state.pool, token, WORKER, client.app.state.config)
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
        client.app.state.pool,
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
    sql(hub_db, "UPDATE workers SET last_heartbeat_at = now() WHERE id = %s", (worker_id,))
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
    assert sql(hub_db, "SELECT dispatch_from FROM workers") == [("any",)]
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
    assert sql(hub_db, "SELECT dispatch_from FROM workers") == [("web",)]

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


def seed_plan(db, project: str = "demo") -> dict:
    """The plan worker-fleet at revision 1 in ``project``, as ``add_run`` expects it."""
    project_id = sql(db, "SELECT id FROM projects WHERE name = %s", (project,))[0][0]
    user_id = sql(db, "SELECT id FROM users WHERE login = %s", (OWNER,))[0][0]
    body = {"id": PLAN, "steps": [{"id": step, "status": "in_progress"} for step in range(7)]}
    values = (project_id, PLAN, Jsonb(body), "sha256:" + "f" * 64, user_id)
    sql(
        db,
        "INSERT INTO plans (project_id, plan_id, area, label, body, digest, updated_by) "
        "VALUES (%s, %s, 'active', '{}', %s, %s, %s)",
        values,
    )
    sql(
        db,
        "INSERT INTO plan_revisions (project_id, plan_id, revision, area, label, body, digest, actor_id) "
        "VALUES (%s, %s, 1, 'active', '{}', %s, %s, %s)",
        values,
    )
    return {"project": project_id, "user": user_id}


def test_a_hub_admin_revokes_a_worker_its_token_stops_and_its_runs_are_released(client, hub, hub_db):
    answer = joined(client, paired(client, hub["owner"])["code"])
    token, worker_id = answer["token"], answer["worker"]["id"]
    spare = joined(client, paired(client, hub["owner"], name="spare")["code"])["worker"]["id"]
    ids = seed_plan(hub_db) | {"worker": worker_id}
    with pg.admin(hub_db.admin_dsn) as conn:
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
    assert sql(hub_db, "SELECT revoked_at IS NOT NULL FROM tokens WHERE id = %s", (answer["token_id"],)) == [(True,)]
    again = client.post(f"/v1/workers/{worker_id}/revoke", headers=hub["owner"])
    assert again.status_code == 409 and "revoked already" in again.json()["message"]
    assert client.post(f"/v1/workers/{worker_id}/drain", headers=hub["owner"]).status_code == 409

    states = dict(sql(hub_db, "SELECT id, state FROM runs"))
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
    assert sql(
        hub_db,
        "SELECT step_key, attempt, state, worker_id, pinned_worker_id, requested_runtime, runtime, mode FROM runs "
        "WHERE parent_run_id = %s",
        (retried,),
    ) == [("1", 2, "queued", None, None, "any", "any", "headless")]
    errors = dict(sql(hub_db, "SELECT id, error FROM runs WHERE id = ANY(%s)", ([last, pinned_held, pinned_queued],)))
    assert all(error.startswith("its worker mac-mini was revoked") for error in errors.values())
    assert "attempt 3 of 3" in errors[last] and "pinned" in errors[pinned_held]
    # each move left the state event the hub writes, numbered after the run's own events
    events = sql(
        hub_db, "SELECT r.id, e.seq, e.body FROM run_events e JOIN runs r ON r.id = e.run_id WHERE e.kind = 'state'"
    )
    moved = {run: (seq, body["from"], body["to"], body["actor"]) for run, seq, body in events}
    assert moved == {
        retried: (1, "running", "lost", "reaper"),
        cancelled: (1, "verifying", "cancelled", "reaper"),
        last: (1, "interactive", "failed", "reaper"),
        pinned_held: (1, "leased", "failed", "reaper"),
        pinned_queued: (1, "queued", "failed", "reaper"),
    }
    assert sql(hub_db, "SELECT count(*) FROM runs WHERE event_seq = 1") == [(5,)]
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
    with pg.admin(hub_db.admin_dsn) as conn:
        held = add_run(conn, ids, "running", step="1")
    # another member's token is unknown to them, and their worker stays as it was
    assert client.delete(f"/v1/tokens/{token_id}", headers=hub["other"]).status_code == 404
    assert client.get(f"/v1/workers/{worker_id}", headers=hub["owner"]).json()["revoked_at"] is None

    assert client.delete(f"/v1/tokens/{token_id}", headers=hub["owner"]).status_code == 204
    shown = client.get(f"/v1/workers/{worker_id}", headers=hub["owner"]).json()
    assert (shown["status"], shown["held_runs"]) == ("revoked", 0)
    assert client.post("/v1/worker/no-such-route", headers=worker_headers(token)).status_code == 401
    assert sql(hub_db, "SELECT state FROM runs WHERE id = %s", (held,)) == [("lost",)]
    assert sql(hub_db, "SELECT state FROM runs WHERE parent_run_id = %s", (held,)) == [("queued",)]
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
    ((machine_id,),) = sql(
        hub_db,
        "SELECT t.id FROM tokens t JOIN users u ON u.id = t.user_id WHERE t.kind = 'machine' AND u.login = %s",
        (OTHER,),
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
