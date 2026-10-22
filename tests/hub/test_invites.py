"""Invite links (``evo_agents.hub.server.invites``, plan autonomous-oss-growth, criterion a1): a hub admin makes a
one-time link to join a project or to register one, lists and revokes it, and nobody else may; a signed-in member
accepts it once, the second of two accepts at once finding it accepted; a link accepted, revoked, expired, unknown or
bound to another login gives nothing; a register link's right makes its holder the admin of the one project they
register; the token is shown once and stored nowhere; each of create, accept and revoke writes one audit row without
it; the public preview of a link's page names who made it and, only to someone it is for, what it gives, never the
login it is bound to; a holder sees nothing of another project; the web's sign-in comes back to a relative path of the hub alone;
migration 0104 goes up and down; ``evo-agents hub admin invite create|list|revoke`` drives it all; and (criterion a2)
the holder of a register link who runs ``evo-agents harness init`` and then ``evo-agents hub project register`` in a
directory of repos becomes the admin of that one project.

The parsing and the return path run without Postgres; everything that needs the hub skips without EVO_HUB_TEST_DSN."""

from __future__ import annotations

import json
import re
import secrets
import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest

from evo_agents.hub.contract import commands
from evo_agents.hub.server.web_auth import return_path
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys
from tests.hub.test_run_cli import as_json, ok, write_credentials
from tests.hub.test_secrets_cli import run_cli

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)

if pg.DSN:
    import httpx
    import psycopg
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select, update

    from evo_agents.hub import tables
    from evo_agents.hub.server.app import create_app
    from evo_agents.hub.server.sealing import KEY_BYTES
    from evo_agents.hub.server.security import SESSION_COOKIE, hash_token
    from tests.hub.fake_github import Account
    from tests.hub.live import ADMIN, LEVELS, add_project, bearer, sql
    from tests.hub.test_migrate import ALL, migrate, move_to
    from tests.hub.test_migrate import tables as public_tables
    from tests.hub.test_projects import body as registration
    from tests.hub.test_run_stream import serving
    from tests.hub.test_web_auth import CALLBACK, authorize, cookie, csrf_for, set_cookie, web_sign_in

TOKEN = re.compile(r"evn_[A-Za-z0-9_-]{43}")  # 32 random bytes in base64url: 256 bits
ADMIN_ID = 302
WRITER = {"kind": "join", "project": "demo", "role": "writer", "max_level": "internal"}
REGISTER = {"kind": "register"}
ACCEPT = "/v1/invites/accept"
PREVIEW = "/v1/invites/preview"
INVITES = "/v1/admin/invites"


# Without the hub


def test_hub_admin_invite_commands_are_in_the_contract():
    printed = commands()
    schemas = {verb: printed[f"hub admin invite {verb}"]["json"]["schema"] for verb in ("create", "list", "revoke")}
    assert schemas == {"create": "InviteCreated", "list": "InviteRow", "revoke": "InviteRow"}
    listed = set(printed["hub admin invite list"]["json"]["keys"])
    assert {"id", "kind", "project", "role", "for_login", "created_by", "expires_at", "status", "accepted_by"} <= listed
    assert "token" not in listed and {"token", "url"} <= set(printed["hub admin invite create"]["json"]["keys"])
    # the commands of hub admin that were there before the invite links are still there
    assert {"hub admin grant", "hub admin revoke", "hub admin users", "hub admin stats"} <= set(printed)


def test_hub_admin_invite_create_checks_its_kind_before_asking_the_hub(monkeypatch, capsys, tmp_path):
    home = write_credentials(tmp_path / "home", "https://hub.example.org", "octo-admin", "evh_unused")
    create = ("hub", "admin", "invite", "create")
    both = run_cli(monkeypatch, capsys, home, *create, "--register", "--project", "demo")
    assert both.code == 2 and "--register takes no --project" in both.err
    half = run_cli(monkeypatch, capsys, home, *create, "--project", "demo", "--role", "reader")
    assert half.code == 2 and "needs --project, --role and --max-level" in half.err
    for days in ("0", "31"):
        refused = run_cli(monkeypatch, capsys, home, *create, "--register", "--days", days)
        assert refused.code == 2 and f"1 to 30 days, not {days}" in refused.err
    with pytest.raises(SystemExit) as not_a_number:
        run_cli(monkeypatch, capsys, home, *create, "--register", "--days", "seven")
    assert not_a_number.value.code == 2 and "invalid int value" in capsys.readouterr().err


@pytest.mark.parametrize(
    "value, taken",
    [
        ("/", True),
        ("/invite/evn_abc", True),
        ("/projects/demo?tab=plans#steps", True),
        ("/a:b//c", True),
        ("//evil.example", False),
        ("///evil.example", False),
        ("/\\evil.example", False),
        ("\\\\evil.example", False),
        ("https://evil.example/invite/x", False),
        ("javascript:alert(1)", False),
        ("invite/evn_abc", False),
        ("", False),
        ("/with space", False),
        ("/tab\there", False),
        ("/new\nline", False),
        ("/café", False),
        ("/" + "a" * 2047, True),
        ("/" + "a" * 2048, False),
        (None, False),
        (["/"], False),
    ],
)
def test_the_invite_return_path_is_one_relative_path_of_the_hub(value, taken):
    assert return_path(value) == (value if taken else None)


# The hub


@pytest.fixture
def config(hub_db, tmp_path, github):
    return live.hub_config(
        hub_db, tmp_path, github, secrets_key=secrets.token_bytes(KEY_BYTES), **live.web_changes(github)
    )


@pytest.fixture
def client(config):
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def admin(client, github) -> dict:
    return bearer(live.sign_in(client, github, ADMIN, ADMIN_ID)["token"])


def member(client, github, login: str, github_id: int) -> dict:
    return bearer(live.sign_in(client, github, login, github_id)["token"])


def created(client, admin: dict, **body) -> dict:
    response = client.post(INVITES, json=body, headers=admin)
    assert response.status_code == 201, response.text
    return response.json()


def accepted(client, headers: dict, token: str) -> dict:
    response = client.post(ACCEPT, json={"token": token}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def grants_of(client, headers: dict) -> list[tuple]:
    grants = client.get("/v1/auth/whoami", headers=headers).json()["grants"]
    return sorted((g["project"], g["role"], g["max_level"]) for g in grants)


def invite_audit(db) -> list[tuple]:
    audit = tables.audit
    rows = select(audit.c.action, audit.c.target, audit.c.project_id).where(audit.c.action.like("invite.%"))
    return sql(db, rows.order_by(audit.c.id))


def project_id(db, name: str) -> int:
    return sql(db, select(tables.projects.c.id).where(tables.projects.c.name == name))[0][0]


@needs_pg
def test_an_admin_makes_lists_and_revokes_invite_links_shown_once(client, github, hub_db, admin):
    add_project(hub_db, "demo")
    response = client.post(INVITES, json=WRITER, headers=admin)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    invite = response.json()
    assert_json_keys("hub admin invite create", invite)
    assert TOKEN.fullmatch(invite["token"]) and invite["url"] == "https://hub.test/invite/" + invite["token"]
    assert (invite["kind"], invite["project"], invite["role"], invite["max_level"]) == (
        "join",
        "demo",
        "writer",
        "internal",
    )
    assert (invite["status"], invite["created_by"], invite["for_login"], invite["accepted_by"]) == (
        "open",
        ADMIN,
        None,
        None,
    )
    lasts = sql(hub_db, select(tables.invites.c.expires_at - tables.invites.c.created_at))[0][0]
    assert lasts == timedelta(days=7)  # the default
    register = created(client, admin, **REGISTER, for_login="Newcomer", days=30)
    assert (register["kind"], register["project"], register["role"], register["for_login"]) == (
        "register",
        None,
        None,
        "Newcomer",
    )
    assert register["token"] != invite["token"]
    # the hub keeps the hash of each token, never a token
    stored = sql(hub_db, select(tables.invites.c.token_hash).order_by(tables.invites.c.id))
    assert stored == [(hash_token(invite["token"]),), (hash_token(register["token"]),)]
    dump = live.table_dump(hub_db)
    assert invite["token"] not in dump and register["token"] not in dump

    listed = client.get(INVITES, headers=admin)
    assert listed.status_code == 200, listed.text
    assert_json_keys("hub admin invite list", listed.json())
    assert [(i["id"], i["status"]) for i in listed.json()] == [(register["id"], "open"), (invite["id"], "open")]
    assert invite["token"] not in listed.text and register["token"] not in listed.text

    revoked = client.post(f"{INVITES}/{invite['id']}/revoke", headers=admin)
    assert revoked.status_code == 200, revoked.text
    assert_json_keys("hub admin invite revoke", revoked.json())
    assert revoked.json()["status"] == "revoked"
    again = client.post(f"{INVITES}/{invite['id']}/revoke", headers=admin)
    assert again.status_code == 409 and "is revoked" in again.json()["message"]
    assert client.post(f"{INVITES}/999999/revoke", headers=admin).status_code == 404

    demo = project_id(hub_db, "demo")
    assert invite_audit(hub_db) == [
        ("invite.create", f"invite:{invite['id']} kind=join project=demo role=writer max_level=internal", demo),
        ("invite.create", f"invite:{register['id']} kind=register for=Newcomer", None),
        ("invite.revoke", f"invite:{invite['id']} kind=join project=demo role=writer max_level=internal", demo),
    ]


@needs_pg
def test_an_invite_link_is_made_only_as_its_kind_and_the_ladder_allow(client, hub_db, admin):
    add_project(hub_db, "demo")
    refusals = [
        ({**WRITER, "days": 31}, 422),
        ({**WRITER, "days": 0}, 422),
        ({"kind": "join", "project": "demo", "role": "writer"}, 422),  # no max level
        ({**REGISTER, "project": "demo"}, 422),
        ({**REGISTER, "role": "reader"}, 422),
        ({**WRITER, "role": "owner"}, 422),
        ({**WRITER, "max_level": "top-secret"}, 422),  # not a level of demo's ladder
        ({**WRITER, "for_login": "not a login"}, 422),
        ({"kind": "lurk"}, 422),
        ({**WRITER, "project": "elsewhere"}, 404),
    ]
    for body, status in refusals:
        response = client.post(INVITES, json=body, headers=admin)
        assert response.status_code == status, (body, response.text)
    assert "not top-secret" in client.post(INVITES, json={**WRITER, "max_level": "top-secret"}, headers=admin).text
    assert sql(hub_db, select(func.count()).select_from(tables.invites)) == [(0,)]
    assert invite_audit(hub_db) == []
    longest = created(client, admin, **WRITER, days=30)
    assert sql(hub_db, select(tables.invites.c.expires_at - tables.invites.c.created_at)) == [(timedelta(days=30),)]
    assert longest["status"] == "open"


@needs_pg
def test_only_a_hub_admin_makes_lists_or_revokes_an_invite_link(client, github, hub_db, admin):
    add_project(hub_db, "demo")
    invite = created(client, admin, **WRITER)
    member_headers = member(client, github, "member", 501)
    granted = {"role": "admin", "max_level": "secret"}  # even the admin role on the project is not enough
    assert client.put("/v1/admin/projects/demo/grants/member", json=granted, headers=admin).status_code == 200
    session = web_sign_in(client, github, Account("member", 501))
    web = {**cookie(session), "X-Evo-CSRF": csrf_for(client, session)}
    for headers in (member_headers, web):
        for method, path, body in (
            ("POST", INVITES, WRITER),
            ("GET", INVITES, None),
            ("POST", f"{INVITES}/{invite['id']}/revoke", None),
        ):
            response = client.request(method, path, json=body, headers=headers)
            assert response.status_code == 403, (method, path, response.text)
            assert response.json()["error"] == "forbidden"
    assert client.get(INVITES, headers={}).status_code == 401
    assert sql(hub_db, select(tables.invites.c.id, tables.invites.c.revoked_at)) == [(invite["id"], None)]
    assert [row[0] for row in invite_audit(hub_db)] == ["invite.create"]


@needs_pg
def test_accepting_a_join_invite_on_the_web_grants_as_hub_admin_grant_does(client, github, hub_db, admin):
    add_project(hub_db, "demo")
    invite = created(client, admin, **WRITER, for_login="NewComer")
    session = web_sign_in(client, github, Account("newcomer", 701))  # the login of the link, in another case
    web = {**cookie(session), "X-Evo-CSRF": csrf_for(client, session)}
    assert client.post(ACCEPT, json={"token": invite["token"]}, headers=cookie(session)).status_code == 403  # no CSRF
    answer = accepted(client, web, invite["token"])
    assert (answer["id"], answer["status"], answer["accepted_by"]) == (invite["id"], "accepted", "newcomer")
    assert "token" not in answer
    assert grants_of(client, cookie(session)) == [("demo", "writer", "internal")]
    grants = tables.grants
    admin_id = sql(hub_db, select(tables.users.c.id).where(tables.users.c.login == ADMIN))[0][0]
    assert sql(hub_db, select(grants.c.role, grants.c.max_level, grants.c.granted_by)) == [
        ("writer", "internal", admin_id)
    ]
    # a link works once: accepting it again gives nothing more
    again = client.post(ACCEPT, json={"token": invite["token"]}, headers=web)
    assert again.status_code == 410 and again.json()["error"] == "gone"
    assert "accepted already" in again.json()["message"] and "nothing was granted" in again.json()["message"]
    demo = project_id(hub_db, "demo")
    target = f"invite:{invite['id']} kind=join project=demo role=writer max_level=internal for=NewComer"
    assert invite_audit(hub_db) == [("invite.create", target, demo), ("invite.accept", target, demo)]
    assert sql(hub_db, select(func.count()).select_from(tables.audit).where(tables.audit.c.action == "grant.put")) == [
        (0,)
    ]
    assert invite["token"] not in live.table_dump(hub_db)


@needs_pg
def test_a_join_invite_never_lowers_the_grant_its_member_holds(client, github, hub_db, admin):
    add_project(hub_db, "demo")
    senior = member(client, github, "senior", 502)
    junior = member(client, github, "junior", 503)
    for login, grant in (("senior", ("admin", "secret")), ("junior", ("reader", "public"))):
        body = {"role": grant[0], "max_level": grant[1]}
        assert client.put(f"/v1/admin/projects/demo/grants/{login}", json=body, headers=admin).status_code == 200
    accepted(client, senior, created(client, admin, **WRITER)["token"])
    accepted(client, junior, created(client, admin, **WRITER)["token"])
    assert grants_of(client, senior) == [("demo", "admin", "secret")]
    assert grants_of(client, junior) == [("demo", "writer", "internal")]
    mixed = created(client, admin, **{**WRITER, "role": "reader", "max_level": "customer"})
    accepted(client, junior, mixed["token"])
    assert grants_of(client, junior) == [("demo", "writer", "customer")]  # the higher role and the higher level


@needs_pg
def test_an_invite_accepted_revoked_expired_unknown_or_for_another_login_gives_nothing(client, github, hub_db, admin):
    add_project(hub_db, "demo")
    newcomer = member(client, github, "newcomer", 701)
    revoked = created(client, admin, **WRITER)
    assert client.post(f"{INVITES}/{revoked['id']}/revoke", headers=admin).status_code == 200
    expired = created(client, admin, **WRITER)
    invites = tables.invites
    sql(
        hub_db,
        update(invites)
        .where(invites.c.id == expired["id"])
        .values(created_at=func.now() - timedelta(days=8), expires_at=func.now() - timedelta(seconds=1)),
    )
    foreign = created(client, admin, **WRITER, for_login="someone-else")
    used = created(client, admin, **WRITER)
    accepted(client, member(client, github, "first", 702), used["token"])
    listed = {i["id"]: i["status"] for i in client.get(INVITES, headers=admin).json()}
    assert [listed[i["id"]] for i in (revoked, expired, foreign, used)] == ["revoked", "expired", "open", "accepted"]

    cases = [
        (revoked["token"], 410, "was revoked"),
        (expired["token"], 410, "expired at"),
        (used["token"], 410, "accepted already"),
        (foreign["token"], 403, "for another GitHub login"),
        ("evn_" + "A" * 43, 404, "not known to the hub"),
        ("not-even-a-token", 404, "not known to the hub"),
    ]
    before = sql(hub_db, select(func.count()).select_from(tables.audit))
    for token, status, said in cases:
        response = client.post(ACCEPT, json={"token": token}, headers=newcomer)
        assert response.status_code == status, (said, response.text)
        assert said in response.json()["message"] and "nothing was granted" in response.json()["message"]
        assert "someone-else" not in response.json()["message"]  # the login a link is bound to stays the admin's
    assert grants_of(client, newcomer) == []
    assert sql(hub_db, select(func.count()).select_from(tables.audit)) == before
    assert sql(hub_db, select(invites.c.id).where(invites.c.accepted_by.is_not(None))) == [(used["id"],)]
    assert client.post(f"{INVITES}/{used['id']}/revoke", headers=admin).status_code == 409  # a grant is taken back
    assert client.post(ACCEPT, json={"token": revoked["token"]}).status_code == 401


def previewed(client, token: str, headers: dict | None = None) -> dict:
    response = client.post(PREVIEW, json={"token": token}, headers=headers or {})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def gives(preview: dict) -> tuple:
    return preview["project"], preview["role"], preview["max_level"]


@needs_pg
def test_an_invite_page_shows_what_a_link_gives_only_to_whom_it_is_for(client, github, hub_db, admin):
    add_project(hub_db, "demo")
    unbound = created(client, admin, **WRITER)
    bound = created(client, admin, **WRITER, for_login="NewComer")
    register = created(client, admin, **REGISTER, for_login="newcomer")
    before = invite_audit(hub_db)

    # a visitor who has not signed in: who invites, and what an unbound link gives; a bound link says no more than that
    # it is bound, never to whom
    seen = previewed(client, unbound["token"])
    assert {key: seen[key] for key in ("kind", "invited_by", "status", "bound", "viewer", "for_viewer")} == {
        "kind": "join",
        "invited_by": ADMIN,
        "status": "open",
        "bound": False,
        "viewer": None,
        "for_viewer": False,
    }
    assert gives(seen) == ("demo", "writer", "internal") and seen["accepted_by_viewer"] is False
    hidden = previewed(client, bound["token"])
    assert (hidden["bound"], hidden["invited_by"], gives(hidden)) == (True, ADMIN, (None, None, None))
    assert "newcomer" not in json.dumps(hidden).lower()
    stranger = previewed(client, register["token"])
    assert (stranger["kind"], stranger["bound"], gives(stranger)) == ("register", True, (None, None, None))

    # signed in as another login: still nothing of a bound link, and it is not theirs to accept
    other = cookie(web_sign_in(client, github, Account("someone", 702)))
    theirs_not = previewed(client, bound["token"], other)
    assert (theirs_not["viewer"], theirs_not["for_viewer"], gives(theirs_not)) == ("someone", False, (None, None, None))
    assert previewed(client, unbound["token"], other)["for_viewer"] is True

    # the login of the link, in another case: its project and role, and the right to accept it
    session = web_sign_in(client, github, Account("newcomer", 701))
    mine = previewed(client, bound["token"], cookie(session))
    assert (mine["viewer"], mine["for_viewer"], gives(mine)) == ("newcomer", True, ("demo", "writer", "internal"))
    assert previewed(client, register["token"], cookie(session))["for_viewer"] is True
    assert invite_audit(hub_db) == before  # reading a link writes nothing
    assert grants_of(client, cookie(session)) == []

    # once accepted, the member who accepted it still sees what it gave; anyone else sees an accepted link, nothing more
    accepted(client, {**cookie(session), "X-Evo-CSRF": csrf_for(client, session)}, unbound["token"])
    after = previewed(client, unbound["token"], cookie(session))
    assert (after["status"], after["accepted_by_viewer"], gives(after)) == (
        "accepted",
        True,
        ("demo", "writer", "internal"),
    )
    for headers in (other, None):
        closed = previewed(client, unbound["token"], headers)
        assert (closed["status"], closed["accepted_by_viewer"], gives(closed)) == (
            "accepted",
            False,
            (None, None, None),
        )

    # a revoked link shows nothing it gave; an unknown token is 404, and a stale session reads as nobody signed in
    assert client.post(f"{INVITES}/{bound['id']}/revoke", headers=admin).status_code == 200
    revoked = previewed(client, bound["token"], cookie(session))
    assert (revoked["status"], gives(revoked)) == ("revoked", (None, None, None))
    for token in ("evn_" + "A" * 43, "not-even-a-token"):
        unknown = client.post(PREVIEW, json={"token": token}, headers=cookie(session))
        assert unknown.status_code == 404 and "not known to the hub" in unknown.json()["message"]
    assert previewed(client, register["token"], cookie("evs_" + "x" * 43))["viewer"] is None


@needs_pg
def test_two_accepts_of_one_invite_at_once_give_it_once(client, github, hub_db, admin):
    add_project(hub_db, "demo")
    invite = created(client, admin, **WRITER)
    headers = {login: member(client, github, login, number) for number, login in enumerate(("ann", "bob"), 801)}
    barrier = threading.Barrier(2)
    answers = {}

    def take(login: str) -> None:
        barrier.wait()
        answers[login] = client.post(ACCEPT, json={"token": invite["token"]}, headers=headers[login])

    threads = [threading.Thread(target=take, args=(login,)) for login in headers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert sorted(response.status_code for response in answers.values()) == [200, 410]
    (winner,) = [login for login, response in answers.items() if response.status_code == 200]
    assert answers[winner].json()["accepted_by"] == winner
    grants = tables.grants
    assert sql(hub_db, select(func.count()).select_from(grants)) == [(1,)]
    assert grants_of(client, headers[winner]) == [("demo", "writer", "internal")]
    assert [row[0] for row in invite_audit(hub_db)] == ["invite.create", "invite.accept"]


@needs_pg
def test_a_register_invite_lets_its_holder_register_one_project_as_its_admin(client, github, hub_db, admin):
    founder = member(client, github, "founder", 901)
    refused = client.put("/v1/projects/fresh", json=registration(), headers=founder)
    assert refused.status_code == 403 and "needs a hub admin" in refused.json()["message"]
    right = created(client, admin, **REGISTER, for_login="founder")
    answer = accepted(client, founder, right["token"])
    assert (answer["status"], answer["project"], answer["used_at"]) == ("accepted", None, None)
    assert grants_of(client, founder) == []  # the right alone, no grant yet

    registered = client.put("/v1/projects/fresh", json=registration(), headers=founder)
    assert registered.status_code == 200, registered.text
    assert registered.json()["created"] is True
    assert (registered.json()["role"], registered.json()["max_level"]) == ("admin", LEVELS[-1])
    assert grants_of(client, founder) == [("fresh", "admin", LEVELS[-1])]
    (row,) = [i for i in client.get(INVITES, headers=admin).json() if i["id"] == right["id"]]
    assert (row["status"], row["project"], row["used_at"] is not None) == ("accepted", "fresh", True)
    # the right is spent: a second new project is refused, and the founder updates their own as its admin
    another = client.put("/v1/projects/another", json=registration(), headers=founder)
    assert another.status_code == 403
    updated = client.put("/v1/projects/fresh", json=registration(repos=[]), headers=founder)
    assert updated.status_code == 200 and updated.json()["changed"] is True
    assert client.post(f"{INVITES}/{right['id']}/revoke", headers=admin).status_code == 409
    projects = sql(hub_db, select(tables.projects.c.name).order_by(tables.projects.c.name))
    assert projects == [("fresh",)]
    audit = tables.audit
    registrations = select(audit.c.action, audit.c.target).where(audit.c.action.like("project.%"))
    assert sql(hub_db, registrations.order_by(audit.c.id)) == [
        ("project.register", "fresh"),
        ("project.update", "fresh"),
    ]
    fresh = project_id(hub_db, "fresh")
    assert invite_audit(hub_db) == [
        ("invite.create", f"invite:{right['id']} kind=register for=founder", None),
        ("invite.accept", f"invite:{right['id']} kind=register for=founder", None),
    ]
    assert sql(hub_db, select(tables.invites.c.project_id)) == [(fresh,)]


@needs_pg
def test_a_revoked_register_invite_right_registers_nothing(client, github, hub_db, admin):
    holder = member(client, github, "holder", 902)
    right = created(client, admin, **REGISTER)
    accepted(client, holder, right["token"])
    revoked = client.post(f"{INVITES}/{right['id']}/revoke", headers=admin)
    assert revoked.status_code == 200 and revoked.json()["status"] == "revoked"
    refused = client.put("/v1/projects/fresh", json=registration(), headers=holder)
    assert refused.status_code == 403
    assert sql(hub_db, select(func.count()).select_from(tables.projects)) == [(0,)]
    # a hub admin who registers the name first leaves the holder's next right unused
    second = created(client, admin, **REGISTER)
    accepted(client, holder, second["token"])
    assert client.put("/v1/projects/taken", json=registration(), headers=admin).status_code == 200
    assert client.put("/v1/projects/taken", json=registration(repos=[]), headers=holder).status_code == 403
    assert sql(hub_db, select(tables.invites.c.used_at).where(tables.invites.c.id == second["id"])) == [(None,)]
    assert client.put("/v1/projects/mine", json=registration(), headers=holder).json()["created"] is True


@needs_pg
def test_project_isolation_an_invite_holder_sees_nothing_of_another_project(client, github, hub_db, admin):
    from tests.hub.test_plans import registration as plans_registration
    from tests.hub.test_runs import PLAN, PROJECT, plan_body

    owner = member(client, github, "owner", 601)
    # Another project with something of each kind: a plan, a run, a memory, a secret its owner leases as an account
    assert client.put(f"/v1/projects/{PROJECT}", json=plans_registration(), headers=admin).status_code == 200
    grant = {"role": "writer", "max_level": "internal"}
    assert client.put(f"/v1/admin/projects/{PROJECT}/grants/owner", json=grant, headers=admin).status_code == 200
    pushed = client.put(f"/v1/projects/{PROJECT}/plans/{PLAN}", json={"body": plan_body()}, headers=owner)
    assert pushed.status_code == 200, pushed.text
    run = client.post(f"/v1/projects/{PROJECT}/runs", json={"plan_id": PLAN, "steps": [2]}, headers=owner)
    assert run.status_code == 201, run.text
    run_id = run.json()[0]["id"]
    note = {
        "scope": "project",
        "project": PROJECT,
        "location": "harness",
        "name": "roadmap.md",
        "type": "project",
        "body": "---\nname: Roadmap\n---\nthe roadmap\n",
    }
    memory = client.put("/v1/memories", params={"sink": "hub"}, json=note, headers=owner)
    assert memory.status_code == 200, memory.text
    secret = {"projects": [PROJECT], "kind": "env", "env_var": "CLAUDE_CODE_OAUTH_TOKEN", "value": "sk-" + "x" * 40}
    assert client.put("/v1/secrets/claude-main", json=secret, headers=owner).status_code == 200

    # The holder joins one project with a link and registers another with a right
    add_project(hub_db, "side")
    holder = member(client, github, "holder", 602)
    accepted(client, holder, created(client, admin, **{**WRITER, "project": "side", "max_level": "secret"})["token"])
    accepted(client, holder, created(client, admin, **REGISTER)["token"])
    assert client.put("/v1/projects/fresh", json=registration(), headers=holder).status_code == 200

    shown = client.get("/v1/projects", headers=holder)
    assert sorted(p["name"] for p in shown.json()) == ["fresh", "side"]
    hidden = [
        f"/v1/projects/{PROJECT}",
        f"/v1/projects/{PROJECT}/plans",
        f"/v1/projects/{PROJECT}/plans/{PLAN}",
        f"/v1/projects/{PROJECT}/runs",
        f"/v1/projects/{PROJECT}/runs/{run_id}",
        f"/v1/memories/{memory.json()['id']}",
        f"/v1/kg/{PROJECT}/runs",
        f"/v1/kg/{PROJECT}/builds",
        f"/v1/kg/{PROJECT}/graph",
        "/v1/accounts/claude-main",
    ]
    for path in hidden:
        assert client.get(path, headers=owner).status_code == 200, path  # each is there, for the project's own member
        response = client.get(path, headers=holder)
        assert response.status_code == 404, (path, response.text)
    memories = client.get("/v1/memories", params={"project": PROJECT}, headers=holder)
    assert memories.status_code in (200, 404), memories.text
    assert "roadmap" not in memories.text
    everything = client.get("/v1/memories", headers=holder)
    assert everything.status_code == 200 and everything.json()["items"] == []
    assert client.get("/v1/memories/search", params={"q": "roadmap"}, headers=holder).json()["items"] == []
    assert [secret["name"] for secret in client.get("/v1/secrets", headers=owner).json()] == ["claude-main"]
    assert client.get("/v1/secrets", headers=holder).json() == []
    assert [account["name"] for account in client.get("/v1/accounts", headers=owner).json()] == ["claude-main"]
    assert client.get("/v1/accounts", headers=holder).json() == []
    # and the owner, who sees their own project, sees none of the holder's
    assert sorted(p["name"] for p in client.get("/v1/projects", headers=owner).json()) == [PROJECT]


@needs_pg
def test_the_web_sign_in_of_an_invite_comes_back_to_its_page_and_nowhere_else(client, github, hub_db):
    github.browser_account = Account("newcomer", 701)
    for asked, back in (
        ("/invite/evn_abc", "/invite/evn_abc"),
        ("//evil.example/invite", "/"),
        ("https://evil.example/", "/"),
        ("/\\evil.example", "/"),
        (None, "/"),
    ):
        params = {} if asked is None else {"next": asked}
        login = client.get("/v1/auth/web/login", params=params, follow_redirects=False)
        assert login.status_code == 302, login.text
        query = dict(httpx.URL(login.headers["location"]).params)
        response = client.get(CALLBACK, params=authorize(github, query), follow_redirects=False)
        assert response.status_code == 303, response.text
        assert response.headers["location"] == back, asked
        assert set_cookie(response, SESSION_COOKIE) is not None
        client.cookies.clear()


@needs_pg
@pytest.mark.empty_db
def test_migration_0104_adds_the_invite_links_and_takes_them_back(hub_db):
    move_to(hub_db, "0104")
    assert "invites" in public_tables(hub_db)
    users, invites = tables.users, tables.invites
    (user_id,) = sql(hub_db, users.insert().values(login="octo").returning(users.c.id))[0]
    good = {"kind": "register", "token_hash": "a" * 64, "created_by": user_id, "expires_at": func.now() + timedelta(1)}
    sql(hub_db, invites.insert().values(good))
    for bad, check in (
        ({**good, "token_hash": "b" * 64, "kind": "lurk"}, "invites_check"),
        ({**good, "token_hash": "c" * 64, "role": "reader"}, "invites_check"),  # a register link names no role
        ({**good, "token_hash": "d" * 64, "kind": "join"}, "invites_check"),  # a join link names its project
        ({**good, "token_hash": "e" * 64, "for_login": "not a login"}, "invites_check"),
        ({**good, "token_hash": "the token itself"}, "invites_check"),
        ({**good, "expires_at": func.now() - timedelta(1), "token_hash": "f" * 64}, "invites_check"),
        (good, "invites_token_hash_key"),
    ):
        with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.UniqueViolation), match=check):
            sql(hub_db, invites.insert().values(bad))
    move_to(hub_db, "0103", down=True)
    assert "invites" not in public_tables(hub_db)
    assert migrate(hub_db.dsn).applied == tuple(ALL[ALL.index("0104") :])
    assert "invites" in public_tables(hub_db)


# The command line


@pytest.fixture
def hub(hub_db, tmp_path, github):
    """The hub under uvicorn with project demo, and a home signed in for the hub admin and for a member."""
    app = create_app(live.hub_config(hub_db, tmp_path, github, **live.web_changes(github)))
    with serving(app) as url, httpx.Client(base_url=url, timeout=10) as client:
        add_project(hub_db, "demo")
        homes = {}
        for login, github_id in ((ADMIN, ADMIN_ID), ("member", 501)):
            token = live.sign_in(client, github, login, github_id)["token"]
            homes[login] = write_credentials(tmp_path / login, url, login, token)
        yield SimpleNamespace(url=url, homes=homes, client=client)


@needs_pg
def test_hub_admin_invite_create_list_and_revoke_from_the_command_line(hub, monkeypatch, capsys, hub_db):
    def run(who: str, *args: str):
        return run_cli(monkeypatch, capsys, hub.homes[who], "hub", "admin", "invite", *args)

    made = ok(run(ADMIN, "create", "--project", "demo", "--role", "writer", "--max-level", "internal", "--for", "ann"))
    token = TOKEN.search(made.out)[0]
    assert "Invite link 1: writer on demo, up to level internal, for ann alone, open until" in made.out
    assert "Shown this once" in made.out and f"https://hub.test/invite/{token}" in made.out
    issued = as_json(run(ADMIN, "create", "--register", "--days", "3", "--json"))
    assert_json_keys("hub admin invite create", issued)
    assert issued["kind"] == "register" and TOKEN.fullmatch(issued["token"])

    listed = as_json(run(ADMIN, "list", "--json"))
    assert_json_keys("hub admin invite list", listed)
    assert [(i["id"], i["kind"], i["for_login"], i["status"]) for i in listed] == [
        (2, "register", None, "open"),
        (1, "join", "ann", "open"),
    ]
    table = ok(run(ADMIN, "list")).out
    assert table.splitlines()[0].split()[:3] == ["ID", "STATUS", "GIVES"]
    assert "the right to register one new project" in table and token not in table
    assert issued["token"] not in json.dumps(listed)

    revoked = as_json(run(ADMIN, "revoke", "1", "--json"))
    assert_json_keys("hub admin invite revoke", revoked)
    assert revoked["status"] == "revoked"
    again = run(ADMIN, "revoke", "1")
    assert again.code == 1 and "invite link 1 is revoked" in again.err
    for args in (("list",), ("revoke", "2"), ("create", "--register")):
        refused = run("member", *args)
        assert refused.code == 1 and "EVO_HUB_ADMINS" in refused.err
    assert sql(hub_db, select(tables.invites.c.revoked_at.is_(None)).order_by(tables.invites.c.id)) == [
        (False,),
        (True,),
    ]


@needs_pg
def test_harness_init_then_project_register_makes_a_register_invite_holder_its_admin(
    hub, github, monkeypatch, capsys, tmp_path, hub_db
):
    """The path of a newcomer, end to end: `harness init` in a directory of repos, a register link accepted, and
    `hub project register` of what init wrote makes them the admin of that one project and of no other."""
    from evo_agents.hub.registration import registration as harness_registration
    from tests.harness.test_harness_init import git_repo

    signed_in = live.sign_in(hub.client, github, "founder", 911)["token"]
    founder = write_credentials(tmp_path / "founder", hub.url, "founder", signed_in)

    def run(home, *args: str):
        return run_cli(monkeypatch, capsys, home, *args)

    def harness(name: str, *project: str):
        workspace = tmp_path / name
        git_repo(workspace / "api", "main", f"https://github.com/acme/{name}-api.git", origin_head="main")
        git_repo(workspace / "web", "trunk")
        ok(run(founder, "harness", "init", str(workspace), *project))
        ok(run(founder, "harness", "validate", str(workspace)))
        return workspace

    workspace = harness("acme-tools")
    before = run(founder, "hub", "project", "register", str(workspace))
    assert before.code == 1 and "needs a hub admin" in before.err  # no right yet

    right = as_json(
        run(hub.homes[ADMIN], "hub", "admin", "invite", "create", "--register", "--for", "founder", "--json")
    )
    assert hub.client.post(ACCEPT, json={"token": right["token"]}, headers=bearer(signed_in)).status_code == 200
    # a name the hub has already is still refused, and leaves the right unused
    named_demo = harness("acme-demo", "--project", "demo")
    refused = run(founder, "hub", "project", "register", str(named_demo))
    assert refused.code == 1 and "admin role on project demo" in refused.err

    registered = ok(run(founder, "hub", "project", "register", str(workspace)))
    assert "Registered project acme-tools" in registered.out and "note:" not in registered.out
    listed = as_json(run(founder, "hub", "project", "list", "--json"))
    assert [(p["name"], p["role"], p["max_level"]) for p in listed] == [("acme-tools", "admin", LEVELS[-1])]
    assert [(r["name"], r["origin"], r["default_branch"]) for r in listed[0]["repos"]] == [
        ("api", "https://github.com/acme/acme-tools-api.git", "main"),
        ("web", None, "trunk"),
    ]
    assert listed[0]["harness"]["name"] == "acme-tools"

    # the right is spent: a second new project is refused, by the command line and by the API alike
    other = harness("acme-other")
    again = run(founder, "hub", "project", "register", str(other))
    assert again.code == 1 and "needs a hub admin" in again.err
    body = harness_registration(other).body
    assert hub.client.put("/v1/projects/acme-other", json=body, headers=bearer(signed_in)).status_code == 403
    assert sql(hub_db, select(tables.projects.c.name).order_by(tables.projects.c.name)) == [
        ("acme-tools",),
        ("demo",),
    ]
    # and the founder sees nothing of the project they were not given
    assert hub.client.get("/v1/projects/demo", headers=bearer(signed_in)).status_code == 404
