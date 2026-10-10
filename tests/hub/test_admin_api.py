"""The admin API the web's admin area uses: every route under /v1/admin answers 403 to a member, whether by
machine token or web session, and a cookie write without X-Evo-CSRF is 403 and changes nothing. The audit trail
filters by actor, action, project and time range and pages by cursor without overlap or gaps; each row carries
the project its action happened in, from schema 0007, which also fills the rows written before it. The token list
shows every user's tokens, and revoking one makes it 401 at once, answers 404 and 409 for unknown and revoked ids,
and leaves an audit row naming the token and its owner. The worker's daily hub.prune_audit deletes the rows older than
EVO_HUB_AUDIT_DAYS (365 by default, 30 to 3650, a bad value stops the hub) and logs how many, and the trail, its
actions and the overview read on."""

import asyncio
import dataclasses
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import column, func, insert, select, table, update
from sqlalchemy.ext.asyncio import create_async_engine

from evo_agents.hub import jobs, tables
from evo_agents.hub import migrate as hub_migrate
from evo_agents.hub.config import ConfigError, load_config
from evo_agents.hub.migrate import alembic_config, migrate
from evo_agents.hub.openapi import document
from evo_agents.hub.server import audit
from evo_agents.hub.server.admin_console import decode_cursor, encode_cursor
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.security import SESSION_COOKIE
from evo_agents.hub.worker import queue
from tests.hub.fake_github import Account
from tests.hub.live import ADMIN, add_project, bearer, sql
from tests.hub.test_web_auth import cookie, csrf_for, set_cookie, web_sign_in

ADMIN_ID = 302
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


def count(db, table_, *conditions) -> int:
    return sql(db, select(func.count()).select_from(table_).where(*conditions))[0][0]


def grant_path(project: str, login: str) -> str:
    return f"/v1/admin/projects/{project}/grants/{login}"


def admin_routes() -> list[tuple[str, str]]:
    """Every operation under /v1/admin in the API contract, with its path parameters filled in."""
    filled = {"{project}": "demo", "{login}": "someone", "{token_id}": "1"}
    routes = []
    for path, operations in document()["paths"].items():
        if path.startswith("/v1/admin/"):
            for name, value in filled.items():
                path = path.replace(name, value)
            routes += [(method.upper(), path) for method in operations]
    return sorted(routes)


def test_every_admin_route_is_403_for_a_member_by_token_and_by_web_session(client, github, hub_db, admin):
    add_project(hub_db)
    member = live.sign_in(client, github, "member", 501)
    assert client.put(grant_path("demo", "member"), json=READER, headers=admin).status_code == 200
    session = web_sign_in(client, github, Account("member", 501))
    csrf = {"X-Evo-CSRF": csrf_for(client, session)}
    routes = admin_routes()
    assert {
        ("GET", "/v1/admin/audit"),
        ("GET", "/v1/admin/audit/actions"),
        ("GET", "/v1/admin/tokens"),
        ("DELETE", "/v1/admin/tokens/1"),
        ("GET", "/v1/admin/users"),
        ("PUT", "/v1/admin/projects/demo/grants/someone"),
        ("DELETE", "/v1/admin/projects/demo/grants/someone"),
    } <= set(routes)
    before = count(hub_db, tables.audit)
    for method, path in routes:
        body = READER if method == "PUT" else None
        for headers in (bearer(member["token"]), {**cookie(session), **csrf}):
            response = client.request(method, path, json=body, headers=headers)
            assert response.status_code == 403, (method, path, response.text)
            assert response.json()["error"] == "forbidden" and "EVO_HUB_ADMINS" in response.json()["message"]
    assert count(hub_db, tables.audit) == before  # a refusal writes nothing
    assert sql(hub_db, select(tables.tokens.c.revoked_at).where(tables.tokens.c.id == 1)) == [(None,)]


def test_an_admin_web_session_grants_only_with_the_csrf_header(client, github, hub_db):
    add_project(hub_db)
    session = web_sign_in(client, github, Account(ADMIN, ADMIN_ID))
    for headers in ({}, {"X-Evo-CSRF": "forged"}):
        refused = client.put(grant_path("demo", "newbie"), json=READER, headers={**cookie(session), **headers})
        assert refused.status_code == 403 and "X-Evo-CSRF" in refused.json()["message"]
    assert count(hub_db, tables.grants) == 0
    assert count(hub_db, tables.audit, tables.audit.c.action.regexp_match("^grant[.]")) == 0

    csrf = {"X-Evo-CSRF": csrf_for(client, session)}
    granted = client.put(grant_path("demo", "newbie"), json=READER, headers={**cookie(session), **csrf})
    assert granted.status_code == 200, granted.text
    revoked = client.delete(grant_path("demo", "newbie"), headers=cookie(session))
    assert revoked.status_code == 403
    assert client.delete(grant_path("demo", "newbie"), headers={**cookie(session), **csrf}).status_code == 204


def test_a_grant_shows_the_project_to_the_member_and_revoking_hides_it(client, github, hub_db, admin):
    add_project(hub_db)
    member = bearer(live.sign_in(client, github, "member", 501)["token"])
    assert client.get("/v1/projects", headers=member).json() == []
    assert client.put(grant_path("demo", "member"), json=READER, headers=admin).status_code == 200
    assert [p["name"] for p in client.get("/v1/projects", headers=member).json()] == ["demo"]

    users = {u["login"]: u for u in client.get("/v1/admin/users", headers=admin).json()}
    (grant,) = users["member"]["grants"]
    assert (grant["project"], grant["role"], grant["max_level"], grant["granted_by"]) == (
        "demo",
        "reader",
        "internal",
        ADMIN,
    )
    assert datetime.fromisoformat(grant["granted_at"]) > datetime.now(UTC) - timedelta(minutes=1)

    assert client.delete(grant_path("demo", "member"), headers=admin).status_code == 204
    assert client.get("/v1/projects", headers=member).json() == []
    assert client.get("/v1/projects/demo", headers=member).status_code == 404
    assert client.delete(grant_path("demo", "member"), headers=admin).status_code == 404


def test_users_come_by_login_whatever_its_case_and_grants_by_project(client, github, hub_db, admin):
    for name in ("zeta", "alpha", "mid"):
        add_project(hub_db, name)
    member = bearer(live.sign_in(client, github, "member", 501)["token"])
    live.sign_in(client, github, "Zed", 503)
    live.sign_in(client, github, "amy", 504)
    for project in ("zeta", "alpha", "mid"):
        assert client.put(grant_path(project, "member"), json=READER, headers=admin).status_code == 200

    users = client.get("/v1/admin/users", headers=admin).json()
    assert [u["login"] for u in users] == ["alpha-owner", "amy", "member", "mid-owner", ADMIN, "Zed", "zeta-owner"]
    (grants,) = [u["grants"] for u in users if u["login"] == "member"]
    assert [g["project"] for g in grants] == ["alpha", "mid", "zeta"]
    me = client.get("/v1/auth/whoami", headers=member).json()
    assert [g["project"] for g in me["grants"]] == ["alpha", "mid", "zeta"]
    overview = client.get("/v1/admin/overview", headers=admin).json()
    assert [g["project"] for g in overview["grants"]] == ["alpha", "mid", "zeta"]


# The audit trail


def audit_page(client, admin, **params) -> dict:
    response = client.get("/v1/admin/audit", params=params, headers=admin)
    assert response.status_code == 200, response.text
    return response.json()


def all_pages(client, admin, **params) -> list[list[dict]]:
    pages, cursor = [], None
    while True:
        page = audit_page(client, admin, **params, **({"cursor": cursor} if cursor else {}))
        pages.append(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return pages


def test_the_trail_filters_by_actor_and_pages_without_overlap_or_gaps(client, github, hub_db, admin):
    add_project(hub_db)
    for _ in range(6):  # each sign-in is one auth.login row of its actor
        live.sign_in(client, github, "member", 501)
    live.sign_in(client, github, "other", 502)
    assert client.put(grant_path("demo", "member"), json=READER, headers=admin).status_code == 200

    everything = audit_page(client, admin, actor="member", limit=200)
    assert everything["next_cursor"] is None
    assert [row["action"] for row in everything["items"]] == ["auth.login"] * 6
    assert {row["actor"] for row in everything["items"]} == {"member"}
    assert audit_page(client, admin, actor="MEMBER", limit=200) == everything  # logins are case-insensitive

    pages = all_pages(client, admin, actor="member", limit=4)
    assert [len(page) for page in pages] == [4, 2]
    assert [row["id"] for page in pages for row in page] == [row["id"] for row in everything["items"]]
    keys = [(row["at"], row["id"]) for page in pages for row in page]
    assert keys == sorted(keys, reverse=True)  # newest first, and each page goes on where the last one stopped

    # A row written between two pages lands on the first page, not inside the next one.
    first = audit_page(client, admin, actor="member", limit=4)
    live.sign_in(client, github, "member", 501)
    second = audit_page(client, admin, actor="member", limit=4, cursor=first["next_cursor"])
    assert [row["id"] for row in first["items"] + second["items"]] == [row["id"] for row in everything["items"]]

    assert audit_page(client, admin, actor="nobody-here")["items"] == []


def test_the_trail_filters_by_action_project_and_time(client, github, hub_db, admin):
    add_project(hub_db, "demo")
    add_project(hub_db, "other")
    live.sign_in(client, github, "member", 501)
    for project in ("demo", "other", "demo"):
        assert client.put(grant_path(project, "member"), json=READER, headers=admin).status_code == 200

    grants = audit_page(client, admin, action="grant.put")["items"]
    assert [(row["project"], row["target"]) for row in grants] == [
        ("demo", "demo/member role=reader max_level=internal"),
        ("other", "other/member role=reader max_level=internal"),
        ("demo", "demo/member role=reader max_level=internal"),
    ]
    assert all(row["actor"] == ADMIN and row["token_id"] for row in grants)
    demo = audit_page(client, admin, project="demo")["items"]
    assert [row["action"] for row in demo] == ["grant.put", "grant.put"]
    logins = audit_page(client, admin, action="auth.login")["items"]
    assert {row["project"] for row in logins} == {None}

    middle = grants[1]["at"]
    since = audit_page(client, admin, action="grant.put", since=middle)["items"]
    until = audit_page(client, admin, action="grant.put", until=middle)["items"]
    assert [row["id"] for row in since] == [grants[0]["id"], grants[1]["id"]]  # since is inclusive
    assert [row["id"] for row in until] == [grants[2]["id"]]  # until is exclusive

    actions = client.get("/v1/admin/audit/actions", headers=admin).json()
    assert actions == ["auth.login", "grant.put"]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 201},
        {"since": "2026-10-04T00:00:00"},  # no time zone
        {"since": "2026-10-05T00:00:00Z", "until": "2026-10-04T00:00:00Z"},
        {"since": "2026-10-04T00:00:00Z", "until": "2026-10-04T00:00:00Z"},
        {"cursor": "not-a-cursor"},
        {"cursor": encode_cursor(datetime(2026, 10, 4), 1)},  # no time zone
        {"actor": "-bad"},
        {"action": "Grant.Put"},
        {"project": "Demo"},
    ],
)
def test_the_trail_refuses_bad_filters_with_422(client, admin, params):
    response = client.get("/v1/admin/audit", params=params, headers=admin)
    assert response.status_code == 422, response.text
    assert response.json()["error"] == "invalid_request"


def test_a_cursor_round_trips_its_sort_key():
    at = datetime(2026, 10, 4, 7, 30, 1, 123456, tzinfo=UTC)
    assert decode_cursor(encode_cursor(at, 42)) == (at, 42)


@pytest.mark.parametrize(
    ("action", "target", "expected"),
    [
        ("grant.put", "demo/member role=reader max_level=internal", ("demo", None)),
        ("grant.delete", "demo/member", ("demo", None)),
        ("project.register", "demo", ("demo", None)),
        ("plan.put", "demo/agent-hub@3", ("demo", None)),
        ("kg.build", "demo", ("demo", None)),
        ("blob.commit", "demo", ("demo", None)),
        ("skill.publish", "skill:project/demo/stop-slop", ("demo", None)),
        ("skill.publish", "skill:global/stop-slop", (None, None)),
        ("memory.put", "memory:17", (None, 17)),
        ("auth.login", "web", (None, None)),
        ("auth.login", "machine:laptop", (None, None)),
        ("token.revoke", "token:4 login=demo", (None, None)),
    ],
)
def test_the_project_of_an_action_comes_from_its_target(action, target, expected):
    assert audit.subject(action, target) == expected


def creator(db, project_id: int) -> int:
    projects = tables.projects
    return sql(db, select(projects.c.created_by).where(projects.c.id == project_id))[0][0]


def add_note(db, project_id: int, user_id: int) -> int:
    """A memory note.md of the project, written by ``user_id``; its id."""
    memories = tables.memories
    note = insert(memories).values(
        scope="project",
        project_id=project_id,
        location="harness",
        name="note.md",
        type="project",
        owner_id=user_id,
        label={"level": "public"},
        body="body",
        updated_by=user_id,
    )
    return sql(db, note.returning(memories.c.id))[0][0]


def test_a_memory_action_is_filed_under_the_memory_s_project(client, hub_db):
    project_id = add_project(hub_db, "demo")
    user_id = creator(hub_db, project_id)
    memory_id = add_note(hub_db, project_id, user_id)

    async def write():
        engine = create_async_engine(hub_db.dsn.replace("postgresql://", "postgresql+psycopg://", 1))
        try:
            async with engine.begin() as conn:
                await audit.record(
                    conn, actor_id=user_id, token_id=None, action="memory.put", target=f"memory:{memory_id}"
                )
                await audit.record(conn, actor_id=user_id, token_id=None, action="auth.logout", target="token:1")
                projects = tables.projects
                other = (await conn.execute(select(projects.c.id).where(projects.c.name == "demo"))).scalar_one()
                await audit.record(
                    conn, actor_id=user_id, token_id=None, action="custom.thing", target="x", project_id=other
                )
        finally:
            await engine.dispose()

    asyncio.run(write())
    trail = tables.audit
    rows = sql(hub_db, select(trail.c.action, trail.c.project_id).order_by(trail.c.id))
    assert rows == [("memory.put", project_id), ("auth.logout", None), ("custom.thing", project_id)]


def move_to(db, revision: str, *, down: bool = False) -> None:
    engine = hub_migrate._engine(db.dsn)
    try:
        with engine.begin() as conn:
            config = alembic_config()
            config.attributes["connection"] = conn
            (command.downgrade if down else command.upgrade)(config, revision)
    finally:
        engine.dispose()


@pytest.mark.empty_db
def test_0007_files_the_rows_written_before_it_under_their_project(hub_db):
    move_to(hub_db, "0006")
    project_id = add_project(hub_db, "demo")
    add_project(hub_db, "demo-two")
    user_id = creator(hub_db, project_id)
    memory_id = add_note(hub_db, project_id, user_id)
    rows = [
        ("grant.put", "demo/member role=reader max_level=public"),
        ("plan.put", "demo/agent-hub@2"),
        ("project.register", "demo"),
        ("skill.publish", "skill:project/demo/stop-slop"),
        ("skill.publish", "skill:global/stop-slop"),
        ("memory.put", f"memory:{memory_id}"),
        ("auth.login", "web"),
        ("grant.put", "gone/member role=reader max_level=public"),  # a project no longer registered
        ("kg.build", "demo-two"),
    ]
    trail, projects = tables.audit, tables.projects
    for action, target in rows:  # schema 0006 has no audit.project_id: the insert names only the columns it sets
        sql(hub_db, insert(trail).values(actor_id=user_id, action=action, target=target))

    result = migrate(hub_db.dsn)
    assert result.applied[0] == "0007"
    filed = sql(
        hub_db,
        select(trail.c.action, trail.c.target, projects.c.name).select_from(
            trail.outerjoin(projects, projects.c.id == trail.c.project_id)
        ),
    )
    assert [name for _, _, name in sorted(filed, key=lambda row: rows.index(row[:2]))] == [
        "demo",
        "demo",
        "demo",
        "demo",
        None,
        "demo",
        None,
        None,
        "demo-two",
    ]
    with pytest.raises(Exception, match="audit rows are never updated"):  # the trigger is back on
        sql(hub_db, update(trail).values(target="changed"))

    move_to(hub_db, "0006", down=True)
    described = table("columns", column("table_name"), column("column_name"), schema="information_schema")
    columns = sql(hub_db, select(described.c.column_name).where(described.c.table_name == "audit"))
    assert "project_id" not in {row[0] for row in columns}
    assert len(sql(hub_db, select(trail.c.id))) == len(rows)


# Retention of the audit trail


def old_row(db, days: float) -> int:
    """A grant.put row written ``days`` days ago; its id."""
    trail = tables.audit
    row = insert(trail).values(
        at=func.now() - timedelta(days=days), action="grant.put", target="demo/member role=reader max_level=internal"
    )
    return sql(db, row.returning(trail.c.id))[0][0]


def prune_audit(client, **changes) -> dict:
    """One pass of the daily job hub.prune_audit, as the worker runs it, on the api's configuration with ``changes``."""
    state = client.app.state
    found = SimpleNamespace(engine=state.engine, config=dataclasses.replace(state.config, **changes))
    return client.portal.call(queue.tasks[jobs.PRUNE_AUDIT].func, SimpleNamespace(additional_context={"hub": found}))


def test_the_audit_retention_prunes_rows_older_than_evo_hub_audit_days_and_logs_how_many(
    client, github, hub_db, admin, caplog
):
    caplog.set_level(logging.INFO, logger="evo_agents.hub")
    add_project(hub_db)
    ancient, past_a_year, within_a_year, past_a_month, recent = (
        old_row(hub_db, days) for days in (900, 365.5, 364.5, 31, 29)
    )
    signed_in = [row["id"] for row in audit_page(client, admin, action="auth.login")["items"]]
    assert signed_in, "the admin's sign-in is a row of today"

    assert prune_audit(client) == {"deleted": 2, "days": 365}
    (line,) = [record for record in caplog.records if record.getMessage() == "audit pruned"]
    assert (line.deleted, line.days) == (2, 365)
    kept = {row[0] for row in sql(hub_db, select(tables.audit.c.id))}
    assert not {ancient, past_a_year} & kept
    assert {within_a_year, past_a_month, recent, *signed_in} <= kept
    # what reads the trail goes on: its pages, its actions and the admin overview
    page = audit_page(client, admin, action="grant.put", limit=200)
    assert [row["id"] for row in page["items"]] == [recent, past_a_month, within_a_year]
    assert client.get("/v1/admin/audit/actions", headers=admin).json() == ["auth.login", "grant.put"]
    assert client.get("/v1/admin/overview", headers=admin).status_code == 200

    assert prune_audit(client) == {"deleted": 0, "days": 365}  # nothing left that old
    assert prune_audit(client, audit_days=30) == {"deleted": 2, "days": 30}
    assert [row["id"] for row in audit_page(client, admin, action="grant.put")["items"]] == [recent]
    assert [row["id"] for row in audit_page(client, admin, action="auth.login")["items"]] == signed_in


def test_the_audit_retention_runs_daily_and_is_queued_at_most_once():
    periodic = {p.task.name: p for p in queue.periodic_registry.periodic_tasks.values()}
    assert periodic[jobs.PRUNE_AUDIT].cron == "33 4 * * *"
    assert queue.tasks[jobs.PRUNE_AUDIT].queueing_lock == jobs.PRUNE_AUDIT


def test_the_audit_retention_keeps_365_days_unless_evo_hub_audit_days_says_otherwise():
    env = {"EVO_HUB_DSN": "postgresql://hub@db/hub"}
    assert load_config(env).audit_days == 365
    assert load_config({**env, "EVO_HUB_AUDIT_DAYS": ""}).audit_days == 365  # empty, as compose passes it unset
    assert load_config({**env, "EVO_HUB_AUDIT_DAYS": "30"}).audit_days == 30
    assert load_config({**env, "EVO_HUB_AUDIT_DAYS": "3650"}).audit_days == 3650
    for value in ("0", "29", "3651", "a year"):
        with pytest.raises(ConfigError) as caught:
            load_config({**env, "EVO_HUB_AUDIT_DAYS": value})
        assert caught.value.variable == "EVO_HUB_AUDIT_DAYS" and "EVO_HUB_AUDIT_DAYS" in str(caught.value)


@pytest.mark.parametrize("command", ["serve", "worker"])
def test_a_bad_audit_retention_stops_the_hub_and_names_the_variable(command):
    env = pg.clean_env(EVO_HUB_DSN=f"postgresql://hub@127.0.0.1:{pg.free_port()}/hub", EVO_HUB_AUDIT_DAYS="7")
    result = pg.cli(["hub", command], env=env)
    assert result.returncode == 2, result.stderr
    (line,) = pg.log_lines(result.stderr)
    assert line["variable"] == "EVO_HUB_AUDIT_DAYS" and "must be at least 30" in line["msg"]


# Tokens of every user


def tokens_page(client, admin, **params) -> dict:
    response = client.get("/v1/admin/tokens", params=params, headers=admin)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_token_list_shows_every_user_s_tokens_and_pages_by_cursor(client, github, hub_db, admin):
    laptop = live.sign_in(client, github, "member", 501, host="laptop")
    desk = live.sign_in(client, github, "member", 501, host="desk")
    other = live.sign_in(client, github, "other", 502, host="mac")
    session = web_sign_in(client, github, Account("member", 501))
    assert client.get("/v1/auth/whoami", headers=bearer(laptop["token"])).status_code == 200

    listed = tokens_page(client, admin, limit=200)["items"]
    by_id = {row["id"]: row for row in listed}
    assert {laptop["token_id"], desk["token_id"], other["token_id"]} <= set(by_id)
    row = by_id[laptop["token_id"]]
    assert (row["login"], row["kind"], row["host"], row["state"], row["current"]) == (
        "member",
        "machine",
        "laptop",
        "active",
        False,
    )
    assert row["last_used_at"] is not None and row["expires_at"] > row["created_at"]
    web = [row for row in listed if row["kind"] == "web"]
    assert [(row["login"], row["host"]) for row in web] == [("member", None)]
    assert [row for row in listed if row["current"]][0]["login"] == ADMIN
    assert "token_hash" not in row and session not in str(listed)

    members = tokens_page(client, admin, login="Member", kind="machine")["items"]
    assert [row["host"] for row in members] == ["desk", "laptop"]  # newest first

    first = tokens_page(client, admin, limit=2)
    second = tokens_page(client, admin, limit=2, cursor=first["next_cursor"])
    third = tokens_page(client, admin, limit=2, cursor=second["next_cursor"])
    ids = [row["id"] for page in (first, second, third) for row in page["items"]]
    assert ids == [row["id"] for row in listed] and third["next_cursor"] is None


def test_revoking_any_token_makes_it_401_and_is_audited(client, github, hub_db, admin):
    member = live.sign_in(client, github, "member", 501, host="laptop")
    path = f"/v1/admin/tokens/{member['token_id']}"
    assert client.delete(path, headers=admin).status_code == 204
    refused = client.get("/v1/auth/whoami", headers=bearer(member["token"]))
    assert refused.status_code == 401 and "evo-agents hub login" in refused.json()["message"]

    again = client.delete(path, headers=admin)
    assert again.status_code == 409 and again.json()["error"] == "conflict"
    missing = client.delete("/v1/admin/tokens/999999", headers=admin)
    assert missing.status_code == 404 and missing.json()["error"] == "not_found"
    assert client.delete("/v1/admin/tokens/0", headers=admin).status_code == 422

    (row,) = audit_page(client, admin, action="token.revoke")["items"]
    assert (row["actor"], row["target"], row["project"]) == (ADMIN, f"token:{member['token_id']} login=member", None)
    assert member["token"] not in str(sql(hub_db, select(tables.audit.c.target)))

    revoked = tokens_page(client, admin, state="revoked")["items"]
    assert [(row["id"], row["state"]) for row in revoked] == [(member["token_id"], "revoked")]
    assert member["token_id"] not in {row["id"] for row in tokens_page(client, admin)["items"]}
    assert member["token_id"] in {row["id"] for row in tokens_page(client, admin, state="any")["items"]}


def test_an_admin_revoking_their_own_web_session_is_signed_out(client, github):
    session = web_sign_in(client, github, Account(ADMIN, ADMIN_ID))
    me = client.get("/v1/auth/whoami", headers=cookie(session)).json()["token"]["id"]
    current = [row for row in tokens_page(client, cookie(session))["items"] if row["current"]]
    assert [row["id"] for row in current] == [me]

    csrf = {"X-Evo-CSRF": csrf_for(client, session)}
    response = client.delete(f"/v1/admin/tokens/{me}", headers={**cookie(session), **csrf})
    assert response.status_code == 204
    deleted = set_cookie(response, SESSION_COOKIE)
    assert deleted is not None and deleted["value"] == "" and deleted.get("max-age") == "0"
    assert client.get("/v1/auth/whoami", headers=cookie(session)).status_code == 401


def test_the_contract_documents_the_refusals_of_the_new_routes():
    paths = document()["paths"]
    for path, method in (
        ("/v1/admin/audit", "get"),
        ("/v1/admin/audit/actions", "get"),
        ("/v1/admin/tokens", "get"),
        ("/v1/admin/tokens/{token_id}", "delete"),
    ):
        responses = paths[path][method]["responses"]
        assert {"401", "403"} <= set(responses), path
    assert {"404", "409"} <= set(paths["/v1/admin/tokens/{token_id}"]["delete"]["responses"])
