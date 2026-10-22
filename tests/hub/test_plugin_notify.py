"""The notice an isolated plugin sends the members of a project (``evo_agents.hub.server.plugin_notify``), as criterion
a1 of the plan plugin-notify-pm-workdays names it, and schema 0097, on the hub with Postgres.

The plugin, granted notifications:send, sends with its token to logins and roles: each member with a grant gets a
notification of kind notice, notice_kind plugin_notice, with the plugin, version, topic and key in its details and the
deliveries of the outbox in the same transaction; a login without a grant is dropped and named; a key sends once; a
call without a live token is 401, a project without the grant 403, a wrong body or a link outside the pages 422."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
from sqlalchemy import insert, select, update

from evo_agents.hub import runs, tables
from evo_agents.hub.migrate import migrate
from tests.hub.live import sql
from tests.hub.test_migrate import ALL, move_to
from tests.hub.test_plugin_runtimes import install, message
from tests.hub.test_runs import PROJECT
from tests.hub.test_task_runs import (  # noqa: F401 (fixtures)
    OTHER,
    OWNER,
    READER,
    app_key,
    client,
    enable_plugin,
    hub,
    plugin_key,
    user_id,
)

SEND = "notifications:send"
PLUGIN = "herald"
PATH = f"/v1/plugin-api/projects/{PROJECT}/notifications"
PAGES = {"pages": [{"name": "board", "title": "Board", "entry": "pages/board.html"}]}


@pytest.fixture
def herald(client, hub, hub_db) -> dict:  # noqa: F811
    """Plugin herald, with a page, enabled in the project with notifications:send granted; the headers of its token."""
    plugin_id = install(hub_db, name=PLUGIN, version="2.3.0", contributes=PAGES, permissions=[SEND])
    enable_plugin(client, hub["owner"], PLUGIN, [SEND])
    return {**hub, "key": plugin_key(client, plugin_id), "herald_id": plugin_id}


def send(client, headers: dict, project: str = PROJECT, **body):
    body = {"to": ["admins"], "title": "Teaching tomorrow", "topic": "reminder"} | body
    return client.post(f"/v1/plugin-api/projects/{project}/notifications", json=body, headers=headers)


def stored(db, notice_kind: str = "plugin_notice") -> list:
    n, u = tables.notifications, tables.users
    query = (
        select(u.c.login, n.c.kind, n.c.title, n.c.body, n.c.link, n.c.details)
        .join_from(n, u, u.c.id == n.c.user_id)
        .where(n.c.notice_kind == notice_kind)
        .order_by(u.c.login)
    )
    return sql(db, query)


def test_plugin_notify_sends_a_plugin_notice_to_each_member_the_roles_and_logins_name(client, herald, hub_db):
    sent = send(client, herald["key"], to=["writers", READER, "nobody"], body="Room 204.", key="k1")
    assert sent.status_code == 201, sent.text
    got = sent.json()
    assert got["duplicate"] is False and got["skipped"] == ["nobody"]
    assert got["sent"] == len(got["recipients"]) and READER in got["recipients"] and OTHER in got["recipients"]
    assert OWNER in got["recipients"], "writers reach the admins too"
    rows = stored(hub_db)
    assert sorted(row[0] for row in rows) == sorted(got["recipients"])
    login, kind, title, body, link, details = rows[0]
    assert (kind, title, body) == ("notice", "Teaching tomorrow", "Room 204.")
    assert link == f"/p/{PROJECT}/x/{PLUGIN}/", "a plugin with pages links to them by default"
    assert details == {"plugin": PLUGIN, "version": "2.3.0", "topic": "reminder", "key": "k1"}
    d = tables.notification_deliveries
    assert len(sql(hub_db, select(d.c.id))) == len(rows), "one delivery on the web for each, in the same transaction"
    assert "plugin_notice" in runs.NOTICE_KINDS


def test_plugin_notify_roles_reach_only_who_holds_them_and_a_plugin_without_pages_links_to_the_project(
    client, herald, hub_db
):
    only_admins = send(client, herald["key"], to=["admins"]).json()
    assert OWNER in only_admins["recipients"] and OTHER not in only_admins["recipients"]
    everyone = send(client, herald["key"], to=["members", "members"]).json()
    assert {OWNER, OTHER, READER} <= set(everyone["recipients"])
    assert send(client, herald["key"], to=[OTHER]).json()["sent"] == 1
    # a plugin that contributes no page links to the project
    quiet_id = install(hub_db, name="quiet", contributes={"events": []}, permissions=[SEND])
    enable_plugin(client, herald["owner"], "quiet", [SEND])
    assert send(client, plugin_key(client, quiet_id), to=[OTHER]).status_code == 201
    assert [row[4] for row in stored(hub_db) if row[0] == OTHER][-1] == f"/p/{PROJECT}"


def test_plugin_notify_a_key_sends_once_and_answers_the_first_result(client, herald, hub_db):
    first = send(client, herald["key"], to=[OTHER], key="week-1").json()
    again = send(client, herald["key"], to=["members"], title="Another", key="week-1")
    assert again.status_code == 200 and again.json() == {**first, "duplicate": True}
    assert len(stored(hub_db)) == 1
    assert send(client, herald["key"], to=[OTHER], key="week-2").status_code == 201
    assert len(stored(hub_db)) == 2
    for _ in range(2):  # no key: every call sends, and there is no rate limit
        assert send(client, herald["key"], to=[OTHER]).status_code == 201
    assert len(stored(hub_db)) == 4


def test_plugin_notify_needs_a_live_plugin_token_and_the_grant_each_call(client, herald, hub_db):
    for headers in ({}, {"Authorization": "Bearer evp_nope"}, herald["owner"], {"Authorization": "Basic x"}):
        refused = send(client, headers)
        assert refused.status_code == 401 and "a live plugin token is required" in refused.json()["message"]
    assert "with notifications:send granted" in message(send(client, herald["key"], project="lumen-other"), 403)
    other_id = install(hub_db, name="helpers", contributes={"events": []}, permissions=[SEND])
    assert "with notifications:send granted" in message(send(client, plugin_key(client, other_id)), 403), "not enabled"
    asks_nothing = install(hub_db, name="mute", contributes={"events": []})
    enable_plugin(client, herald["owner"], "mute", [])  # enabled; it asks for nothing, so nothing is granted
    assert "with notifications:send granted" in message(send(client, plugin_key(client, asks_nothing)), 403)
    # the grant is checked at each call, against what the installed version still asks for
    p = tables.plugin_versions
    old = sql(hub_db, select(p.c.manifest).where(p.c.plugin_id == herald["herald_id"]))[0][0]
    sql(hub_db, update(p).where(p.c.plugin_id == herald["herald_id"]).values(manifest={**old, "permissions": []}))
    assert send(client, herald["key"]).status_code == 403
    sql(hub_db, update(p).where(p.c.plugin_id == herald["herald_id"]).values(manifest=old))
    assert send(client, herald["key"]).status_code == 201
    # a new token revokes the old one
    old_key, herald["key"] = herald["key"], plugin_key(client, herald["herald_id"])
    assert send(client, old_key).status_code == 401 and send(client, herald["key"]).status_code == 201


def test_plugin_notify_refuses_a_wrong_body_and_a_link_outside_the_plugin_and_the_project(client, herald, hub_db):
    key = herald["key"]
    bad = (
        {"to": []},
        {"to": ["not a login!"]},
        {"title": ""},
        {"title": "two\nlines"},
        {"title": "x" * 201},
        {"topic": "Not A Slug"},
        {"topic": ""},
        {"body": "é" * (runs.MAX_NOTICE_BODY_BYTES // 2 + 1)},
        {"key": ""},
        {"extra": 1},
        {"link": "https://evil.test/"},
        {"link": "/inbox"},
        {"link": f"/p/{PROJECT}/runs/1"},
        {"link": f"/p/{PROJECT}/x/other/"},
        {"link": f"/p/{PROJECT}/x/{PLUGIN}/../../runs"},
        {"link": f"/p/{PROJECT}x"},
    )
    for body in bad:
        assert send(client, key, **body).status_code == 422, body
    assert stored(hub_db) == []
    fine = ({"body": "é" * (runs.MAX_NOTICE_BODY_BYTES // 2)}, {"title": "x" * 200})
    for body in fine:
        assert send(client, key, **body).status_code == 201, body
    for link in (f"/p/{PROJECT}", f"/p/{PROJECT}/x/{PLUGIN}/board", f"/p/{PROJECT}/x/{PLUGIN}/?day=1#top"):
        assert send(client, key, link=link, to=[OTHER]).status_code == 201, link
    assert [row[4] for row in stored(hub_db) if row[0] == OTHER] == [
        f"/p/{PROJECT}",
        f"/p/{PROJECT}/x/{PLUGIN}/board",
        f"/p/{PROJECT}/x/{PLUGIN}/?day=1#top",
    ]


def test_plugin_notify_is_audited_without_the_text(client, herald, hub_db):
    assert send(client, herald["key"], to=["members"], title="Secret title", body="Secret body").status_code == 201
    a = tables.audit
    rows = sql(hub_db, select(a.c.action, a.c.target).where(a.c.action == "plugin.notify"))
    assert len(rows) == 1 and rows[0][1].startswith(f"{PROJECT}/plugin:{PLUGIN} recipients:")
    assert "Secret" not in str(rows)


def test_plugin_notify_schema_0097_takes_the_kind_and_going_back_deletes_the_notices(client, herald, hub_db):
    assert send(client, herald["key"], to=[OTHER], key="k").status_code == 201
    n = tables.notifications
    other = {"user_id": user_id(hub_db, OTHER), "kind": "notice", "notice_kind": "pm_daily", "title": "Digest"}
    sql(hub_db, insert(n).values(other))
    move_to(hub_db, "0096", down=True)
    assert stored(hub_db) == [] and len(stored(hub_db, "pm_daily")) == 1
    with pytest.raises(psycopg.errors.CheckViolation, match="notifications_notice_kind_check"):
        sql(hub_db, insert(n).values({**other, "notice_kind": "plugin_notice"}))
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0097") :]
    sql(hub_db, insert(n).values({**other, "notice_kind": "plugin_notice"}))
