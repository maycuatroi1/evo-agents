"""The messages an isolated plugin posts and reads in a group chat with its token (``evo_agents.hub.server
.plugin_chats_api``), as criterion b4 of the plan agent-qa-bugs-through-dev-system names it.

Granted chats:write, a plugin posts a message, or a reply in a thread, to a chat that holds an agent of its project, in
the name of the admin who granted it its permissions, a member of the chat: the message goes the way theirs does (a
mention wakes an agent, a message to the entry agent of an agent system opens a trace, a person mentioned gets a
notice, the audit row chat.message), comes ``via`` plugin, and its audit target names the plugin. A chat the admin
is not in, one of people alone or without an agent of the project, one with an agent of a project that does not grant
the plugin chats:write, the feature chats off, and an admin below writer are refused, with nothing written. Granted
chats:read, it reads the messages of one thread.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select, update

from evo_agents.hub import tables
from tests.hub import live
from tests.hub.live import sql
from tests.hub.test_chat_runs import (  # noqa: F401
    OTHER,
    OWNER,
    PROJECT,
    app_key,
    chat_runs_of,
    chat_worker,
    claim,
    client,
    created,
    hub,
    sent,
)
from tests.hub.test_chats import agent, notices
from tests.hub.test_plans import registration
from tests.hub.test_plugin_runtimes import install
from tests.hub.test_runs import audit_rows
from tests.hub.test_system_traces import SLUG, put_system
from tests.hub.test_task_runs import enable_plugin, plugin_key

PLUGIN = "lumen-qa"
READS, WRITES = "chats:read", "chats:write"
KB = "lumen-kb"


def plugin(client, hub, db, permissions=(WRITES, READS), name: str = PLUGIN) -> dict:
    """The headers of the token of plugin ``name``, installed asking for ``permissions`` and enabled by owner with
    them granted."""
    plugin_id = install(db, name=name, contributes={}, permissions=list(permissions))
    enable_plugin(client, hub["owner"], name, list(permissions))
    return plugin_key(client, plugin_id)


def path(chat_id: int, project: str = PROJECT) -> str:
    return f"/v1/plugin-api/projects/{project}/chats/{chat_id}/messages"


def post(client, key: dict, chat_id: int, body: str, **more):
    return client.post(path(chat_id), json={"body": body, **more}, headers=key)


def posted(client, key: dict, chat_id: int, body: str, **more) -> dict:
    response = post(client, key, chat_id, body, **more)
    assert response.status_code == 201, response.text
    return response.json()


def refused(response, status: int, words: str) -> None:
    assert response.status_code == status, response.text
    assert words in response.json()["message"], response.json()["message"]


def messages_of(db) -> int:
    return sql(db, select(func.count()).select_from(tables.chat_messages))[0][0]


def user_id(db, login: str) -> int:
    return sql(db, select(tables.users.c.id).where(tables.users.c.login == login))[0][0]


def test_a_plugin_posts_and_replies_in_the_granters_name_as_their_own_message_goes(client, hub, hub_db):
    put_system(client, hub["owner"])
    studio = chat_worker(client, hub["owner"], "studio", slots=3)
    chat = created(client, hub["owner"], people=[OTHER], agents=["reviewer", "fixer"])
    key = plugin(client, hub, hub_db)

    first = posted(client, key, chat["id"], f"@reviewer Login fails on Safari since round 12. @{OTHER} FYI")
    assert (first["author"]["kind"], first["author"]["login"], first["via"]) == ("person", OWNER, "plugin")
    assert (first["chat_id"], first["thread_root_id"], first["thread"]) == (chat["id"], None, first["id"])
    reached = {mention["name"]: (mention["via"], mention["refusal"]) for mention in first["mentions"]}
    assert reached == {"reviewer": ("handle", None), OTHER: ("login", None)}
    # the mention woke @reviewer: a chat run dispatched by the admin who granted the plugin, on their worker
    spec = claim(client, studio)
    assert spec is not None and spec["kind"] == "chat", spec
    ((run_id, _, _, hop, dispatched_by),) = chat_runs_of(hub_db)
    assert (run_id, hop, dispatched_by) == (spec["id"], 1, user_id(hub_db, OWNER))
    # @reviewer is the entry agent of the system review: the message opened its trace
    t = tables.system_traces
    traces = sql(hub_db, select(t.c.system_slug, t.c.chat_id, t.c.message_id))
    assert traces == [(SLUG, chat["id"], first["id"])]
    # the person mentioned is told, as for the admin's own message
    told = notices(client, hub["other"])
    assert [(n["notice_kind"], n["body"]) for n in told if n["notice_kind"] == "chat_mention"] == [
        ("chat_mention", first["body"])
    ]
    target = f"chat:{chat['id']} message:{first['id']} plugin:{PLUGIN}"
    assert ("chat.message", target, OWNER, None) in audit_rows(hub_db, "chat")

    # a reply to the reply goes in the thread of the first message, which the answer names
    theirs = sent(client, hub["other"], chat["id"], "Seen on my machine too.", reply_to=first["id"])
    reply = posted(client, key, chat["id"], "@reviewer Still failing in round 13.", reply_to=theirs["id"])
    assert (reply["thread_root_id"], reply["thread"], reply["via"]) == (first["id"], first["id"], "plugin")
    assert [(m["name"], m["run_id"]) for m in reply["mentions"]] == [("reviewer", run_id)]
    members = client.get(f"/v1/chats/{chat['id']}/messages", params={"thread": first["id"]}, headers=hub["other"])
    assert [(m["id"], m["via"]) for m in members.json()["messages"]] == [
        (first["id"], "plugin"),
        (theirs["id"], None),
        (reply["id"], "plugin"),
    ]

    # with chats:read, the plugin reads the thread from any of its messages, a page at a time
    read = client.get(path(chat["id"]), params={"thread": reply["id"]}, headers=key)
    assert read.status_code == 200, read.text
    assert (read.json()["thread"], read.json()["more"]) == (first["id"], False)
    assert [m["id"] for m in read.json()["messages"]] == [first["id"], theirs["id"], reply["id"]]
    latest = client.get(path(chat["id"]), params={"thread": first["id"], "limit": 2}, headers=key).json()
    assert ([m["id"] for m in latest["messages"]], latest["more"]) == ([theirs["id"], reply["id"]], True)
    older = {"thread": first["id"], "before": latest["messages"][0]["seq"]}
    assert [m["id"] for m in client.get(path(chat["id"]), params=older, headers=key).json()["messages"]] == [
        first["id"]
    ]


def test_a_plugin_posts_only_where_its_granter_may_and_every_project_of_the_chat_grants_it(client, hub, hub_db):
    key = plugin(client, hub, hub_db)
    chat = created(client, hub["owner"], people=[OTHER], agents=["reviewer"])
    before = messages_of(hub_db)

    assert post(client, {}, chat["id"], "hello").status_code == 401
    reader_only = plugin(client, hub, hub_db, permissions=(READS,), name="lumen-reader")
    refused(post(client, reader_only, chat["id"], "hello"), 403, "with chats:write granted")
    writer_only = plugin(client, hub, hub_db, permissions=(WRITES,), name="lumen-writer")
    read = client.get(path(chat["id"]), params={"thread": 1}, headers=writer_only)
    refused(read, 403, "with chats:read granted")
    assert post(client, key, chat["id"], "  ").status_code == 422
    assert post(client, key, chat["id"], "hello", attachment_ids=[1]).status_code == 422

    # a chat the admin is not in, and one of people alone
    theirs = created(client, hub["other"], agents=["reviewer"])
    refused(post(client, key, theirs["id"], "hello"), 404, f"chat {theirs['id']}")
    people = created(client, hub["owner"], people=[OTHER], agents=())
    refused(post(client, key, people["id"], "hello"), 403, f"chat {people['id']} holds no agent of project {PROJECT}")
    refused(post(client, key, chat["id"], "hello", reply_to=10**6), 404, "has no message 1000000")

    # an agent of another project that does not grant the plugin chats:write, until it does
    assert client.put(f"/v1/projects/{KB}", json=registration(), headers=hub["admin"]).status_code == 200
    grant = {"role": "admin", "max_level": "internal"}
    assert client.put(f"/v1/admin/projects/{KB}/grants/{OWNER}", json=grant, headers=hub["admin"]).status_code == 200
    agent(client, hub["owner"], "kb-bot", project=KB)
    both = created(client, hub["owner"], agents=["reviewer", "kb-bot"])
    refused(post(client, key, both["id"], "hello"), 403, f"holds an agent of project {KB}, which does not grant")
    asked = {"permissions": [WRITES, READS]}
    enabled = client.post(f"/v1/projects/{KB}/plugins/{PLUGIN}/enable", json=asked, headers=hub["owner"])
    assert enabled.status_code == 200, enabled.text
    assert posted(client, key, both["id"], "hello")["via"] == "plugin"
    before += 1

    # the feature chats off for the hub
    off = client.post("/v1/admin/features", json={"values": {"chats": False}}, headers=hub["admin"])
    assert off.status_code == 200, off.text
    refused(post(client, key, chat["id"], "hello"), 404, "chats is turned off on this hub")
    on = client.post("/v1/admin/features", json={"unset": ["chats"]}, headers=hub["admin"])
    assert on.status_code == 200, on.text

    # the admin who granted the plugin holds less than writer now
    g = tables.grants
    with live.engine(hub_db).begin() as conn:
        conn.execute(update(g).where(g.c.user_id == user_id(hub_db, OWNER)).values(role="reader"))
    refused(post(client, key, chat["id"], "hello"), 403, f"in the name of {OWNER}, who granted it its permissions")
    assert messages_of(hub_db) == before
