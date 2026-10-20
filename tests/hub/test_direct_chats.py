"""Direct chats on the hub (``evo_agents.hub.server.chat_direct``), as criterion a1 of the plan agent-direct-chats names
them: PUT /v1/chats/direct/{handle} opens the caller's direct chat with an agent, the one there is (200) or one made now
(201), one per (person, agent) even when requests come at once; it refuses an agent the caller may not add to a chat,
and a deleted one while there is no chat, but a direct chat there is still reads; a direct chat is of kind direct in the
database, never read from its members, so a new chat of one person and one agent is a group; its members never change;
Chat and ChatSummary say the kind; each one made is audited. Migration 0050 makes every chat there is a group.

What a message of a direct chat hands its agent is in ``tests.hub.test_direct_chat_runs``."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from psycopg import errors
from sqlalchemy import func, insert, select

from evo_agents.hub import tables
from evo_agents.hub.migrate import migrate
from evo_agents.hub.server.chat_direct import direct_chat
from evo_agents.hub.server.security import Principal
from tests.hub import live
from tests.hub import test_chats as group_chats
from tests.hub.live import sql
from tests.hub.test_migrate import ALL, columns, move_to, one, query, seed
from tests.hub.test_run_stream import PUBLIC_READER
from tests.hub.test_runs import OTHER, OWNER, PROJECT, audit_rows

client, hub = group_chats.client, group_chats.hub  # fixtures: project evo-agents, owner its admin, the agent @reviewer
agent, created = group_chats.agent, group_chats.created


def opened(client, headers, handle: str = "reviewer"):
    return client.put(f"/v1/chats/direct/{handle}", headers=headers)


def direct_rows(db) -> list[tuple]:
    c, u, ap = tables.chats, tables.users, tables.agent_profiles
    query = (
        select(c.c.id, c.c.kind, u.c.login, ap.c.handle)
        .select_from(c.join(u, u.c.id == c.c.direct_user_id).join(ap, ap.c.id == c.c.direct_profile_id))
        .order_by(c.c.id)
    )
    return sql(db, query)


def member_count(db, chat_id: int) -> int:
    m = tables.chat_members
    return sql(db, select(func.count()).where(m.c.chat_id == chat_id, m.c.removed_at.is_(None)))[0][0]


def listed(client, headers) -> dict[int, dict]:
    response = client.get("/v1/chats", headers=headers)
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()}


# Opening one


def test_a_member_opens_one_direct_chat_with_an_agent_and_gets_the_same_one_again(client, hub, hub_db):
    first = opened(client, hub["owner"])
    assert first.status_code == 201, first.text
    chat = first.json()
    assert (chat["kind"], chat["title"], chat["created_by"]) == ("direct", "Reviewer", OWNER)
    assert [p["login"] for p in chat["people"]] == [OWNER]
    assert [(a["handle"], a["project"], a["deleted"]) for a in chat["agents"]] == [("reviewer", PROJECT, False)]
    again = opened(client, hub["owner"])
    assert again.status_code == 200, again.text
    assert again.json()["id"] == chat["id"] and again.json()["kind"] == "direct"
    theirs = opened(client, hub["other"])  # someone else's direct chat with the same agent is their own
    assert theirs.status_code == 201, theirs.text
    assert theirs.json()["id"] != chat["id"]
    assert direct_rows(hub_db) == [
        (chat["id"], "direct", OWNER, "reviewer"),
        (theirs.json()["id"], "direct", OTHER, "reviewer"),
    ]
    summary = listed(client, hub["owner"])[chat["id"]]
    assert (summary["kind"], summary["people"], summary["agents"]) == ("direct", [OWNER], ["reviewer"])
    assert theirs.json()["id"] not in listed(client, hub["owner"])
    shown = client.get(f"/v1/chats/{chat['id']}", headers=hub["owner"])
    assert shown.status_code == 200 and shown.json()["kind"] == "direct"
    assert audit_rows(hub_db, "chat") == [
        ("chat.create", f"chat:{chat['id']} direct agent:@reviewer", OWNER, PROJECT),
        ("chat.create", f"chat:{theirs.json()['id']} direct agent:@reviewer", OTHER, PROJECT),
    ]


def test_requests_at_once_make_one_direct_chat(client, hub, hub_db):
    start = threading.Barrier(4)

    def ask(_):
        start.wait()
        return opened(client, hub["owner"])

    with ThreadPoolExecutor(4) as pool:
        answers = list(pool.map(ask, range(4)))
    assert sorted(answer.status_code for answer in answers) == [200, 200, 200, 201], [a.text for a in answers]
    (chat_id,) = {answer.json()["id"] for answer in answers}
    assert direct_rows(hub_db) == [(chat_id, "direct", OWNER, "reviewer")]
    assert member_count(hub_db, chat_id) == 2
    assert [row[0] for row in audit_rows(hub_db, "chat")] == ["chat.create"]


def test_the_second_of_two_requests_at_once_waits_on_the_key_and_finds_the_first_chat(client, hub, hub_db):
    """Both find no chat; the first makes one and has not committed when the second inserts: the second waits on
    chats_direct_key, inserts nothing once the first commits, and answers the first's chat."""
    (user_id,) = sql(hub_db, select(tables.users.c.id).where(tables.users.c.login == OWNER))[0]
    owner = Principal(user_id=user_id, login=OWNER, admin=False, token_id=None, kind="machine", token_hash="")

    async def race(engine):
        async with engine.connect() as first_conn:
            first_tx = await first_conn.begin()
            first = await direct_chat(first_conn, owner, "reviewer")

            async def second():
                async with engine.begin() as conn:
                    return await direct_chat(conn, owner, "reviewer")

            waiting = asyncio.ensure_future(second())
            await asyncio.sleep(0.5)
            blocked = not waiting.done()
            await first_tx.commit()
            return first, await waiting, blocked

    first, second, blocked = client.portal.call(partial(race, client.app.state.engine))
    assert blocked, "the second request did not wait for the first"
    assert (first[1], second[1], second[0]) == (True, False, first[0])
    assert direct_rows(hub_db) == [(first[0], "direct", OWNER, "reviewer")]
    assert member_count(hub_db, first[0]) == 2


# What is refused


def test_a_direct_chat_is_refused_for_an_agent_the_caller_may_not_add_to_a_chat(client, hub, hub_db):
    def refused(headers, handle: str, status: int, words: str) -> None:
        answer = opened(client, headers, handle)
        assert answer.status_code == status, answer.text
        assert words in answer.json()["message"], answer.json()["message"]

    refused(hub["owner"], "ghost", 404, "no agent @ghost that you can see")
    refused(hub["stranger"], "reviewer", 404, "no agent @reviewer that you can see")
    refused(hub["reader"], "reviewer", 403, f"needs the writer role on project {PROJECT}")
    agent(client, hub["owner"], "retired")
    assert client.delete(f"/v1/projects/{PROJECT}/agents/retired", headers=hub["owner"]).status_code == 200
    refused(hub["owner"], "retired", 409, "@retired was deleted")
    lowered = {"role": "writer", "max_level": "public"}  # a writer who does not clear the label of the project
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{PUBLIC_READER}", json=lowered, headers=hub["admin"])
    assert granted.status_code == 200, granted.text
    refused(hub["public"], "reviewer", 409, f"{PUBLIC_READER} (a grant on project {PROJECT} up to public")
    agent(client, hub["owner"], OTHER)  # an agent named as a person: @someone-else would name either
    refused(hub["other"], OTHER, 409, f"{OTHER} would be both a person and an agent")
    bad = opened(client, hub["owner"], "Not_A_Handle")
    assert bad.status_code == 422, bad.text
    assert direct_rows(hub_db) == []
    assert audit_rows(hub_db, "chat") == []
    assert sql(hub_db, select(func.count()).select_from(tables.chats)) == [(0,)]


def test_a_direct_chat_of_an_agent_deleted_since_still_reads(client, hub, hub_db):
    chat = opened(client, hub["owner"]).json()
    deleted = client.delete(f"/v1/projects/{PROJECT}/agents/reviewer", headers=hub["owner"])
    assert deleted.status_code == 200, deleted.text
    again = opened(client, hub["owner"])
    assert again.status_code == 200, again.text
    assert again.json()["id"] == chat["id"]
    assert [(a["handle"], a["deleted"]) for a in again.json()["agents"]] == [("reviewer", True)]
    page = client.get(f"/v1/chats/{chat['id']}/messages", headers=hub["owner"])
    assert page.status_code == 200 and page.json()["chat"]["kind"] == "direct"
    fresh = opened(client, hub["other"])  # with no direct chat yet, the deleted agent is refused
    assert fresh.status_code == 409 and "@reviewer was deleted" in fresh.json()["message"]


# Its members


def test_the_members_of_a_direct_chat_never_change(client, hub, hub_db):
    chat = opened(client, hub["owner"]).json()
    agent(client, hub["owner"], "fixer")
    base = f"/v1/chats/{chat['id']}"
    for method, path in (
        ("put", f"{base}/people/{OTHER}"),
        ("put", f"{base}/agents/fixer"),
        ("put", f"{base}/agents/reviewer"),
        ("delete", f"{base}/people/{OWNER}"),
        ("delete", f"{base}/agents/reviewer"),
    ):
        answer = client.request(method.upper(), path, headers=hub["owner"])
        assert answer.status_code == 409, (path, answer.text)
        message = answer.json()["message"]
        assert f"chat {chat['id']} is a direct chat of one person and one agent, whose members never change" in message
        assert "Start a group chat" in message
    leaving = client.delete(f"{base}/people/{OWNER}", headers=hub["owner"]).json()["message"]
    assert "leaving it is refused" in leaving
    shown = client.get(base, headers=hub["owner"]).json()
    assert ([p["login"] for p in shown["people"]], [a["handle"] for a in shown["agents"]]) == ([OWNER], ["reviewer"])
    assert [row[0] for row in audit_rows(hub_db, "chat")] == ["chat.create"]


def test_a_new_chat_of_one_person_and_one_agent_stays_a_group(client, hub, hub_db):
    group = created(client, hub["owner"], title="Just me", agents=["reviewer"])
    assert group["kind"] == "group"
    direct = opened(client, hub["owner"])
    assert direct.status_code == 201, direct.text  # the group chat of the same pair is not it
    assert direct.json()["id"] != group["id"]
    kinds = {chat_id: item["kind"] for chat_id, item in listed(client, hub["owner"]).items()}
    assert kinds == {group["id"]: "group", direct.json()["id"]: "direct"}
    added = client.put(f"/v1/chats/{group['id']}/people/{OTHER}", headers=hub["owner"])
    assert added.status_code == 200 and added.json()["kind"] == "group"
    assert direct_rows(hub_db) == [(direct.json()["id"], "direct", OWNER, "reviewer")]


# Migration 0050


@pytest.mark.empty_db
def test_0050_makes_every_chat_a_group_and_holds_one_direct_chat_a_pair(hub_db):
    move_to(hub_db, "0049")
    t = tables
    with live.connect(hub_db) as conn:
        ids = seed(conn)
        old = one(conn, insert(t.chats).values(title="Release talk", created_by=ids["user"]).returning(t.chats.c.id))
        profile = {"project_id": ids["project"], "handle": "reviewer", "name": "Reviewer", "color": "#475569"}
        profile |= {"is_default": False, "permissions": {}, "created_by": ids["user"]}
        profile_id = one(conn, insert(t.agent_profiles).values(profile).returning(t.agent_profiles.c.id))
        message = insert(t.chat_messages).values(chat_id=old, seq=1, body="look", label=[])
        message_id = one(conn, message.returning(t.chat_messages.c.id))
        mention = {"message_id": message_id, "ordinal": 0, "name": "reviewer", "profile_id": profile_id}
        with pytest.raises(errors.CheckViolation):  # no via direct before 0050
            conn.execute(insert(t.chat_mentions).values({**mention, "via": "direct"}))
    move_to(hub_db, "0050")
    assert {"kind", "direct_user_id", "direct_profile_id"} <= columns(hub_db, "chats")
    chats = t.chats
    assert query(hub_db, select(chats.c.kind, chats.c.direct_user_id, chats.c.direct_profile_id)) == [
        ("group", None, None)
    ]
    pair = {"title": "Reviewer", "created_by": ids["user"], "direct_user_id": ids["user"]}
    with live.connect(hub_db) as conn:
        pair |= {"kind": "direct", "direct_profile_id": profile_id}
        direct = one(conn, insert(chats).values(pair).returning(chats.c.id))
        with pytest.raises(errors.UniqueViolation):
            conn.execute(insert(chats).values(pair))
        for bad in (
            {"title": "x", "created_by": ids["user"], "kind": "direct"},
            {**pair, "kind": "group"},
            {**pair, "direct_profile_id": None},
            {"title": "x", "created_by": ids["user"], "kind": "channel"},
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(insert(chats).values(bad))
        conn.execute(insert(t.chat_mentions).values({**mention, "via": "direct"}))
    move_to(hub_db, "0049", down=True)
    assert not {"kind", "direct_user_id", "direct_profile_id"} & columns(hub_db, "chats")
    assert query(hub_db, select(chats.c.id).order_by(chats.c.id)) == [(old,), (direct,)]
    assert query(hub_db, select(t.chat_mentions.c.via)) == [("thread",)]
    assert migrate(hub_db.dsn).applied == ALL[ALL.index("0050") :]
