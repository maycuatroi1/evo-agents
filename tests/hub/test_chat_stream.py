"""What happens in a group chat as its members follow it, and ``evo-agents hub chat``: the events of a chat
(``evo_agents.hub.server.chat_events``) and its live stream, as criterion a6 of the plan agent-profiles-group-chat
names them: messages and reactions reach the open tabs as server-sent events within two seconds, on the api process's
one LISTEN connection; a stream resumed with Last-Event-ID misses nothing and repeats nothing; a member who leaves gets
an ``end`` event. Then the five commands ``hub chat create|list|show|send|react``.

The hub runs under uvicorn in a thread of this process, as in ``tests.hub.test_run_stream``: the stream needs a server
that sends a response while it is still being written."""

import json
import time
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import httpx
from sqlalchemy import column, func, select, table

from evo_agents.hub.contract import commands
from evo_agents.hub.server import chat_events, listen
from evo_agents.hub.server.app import create_app
from tests.hub.contract_keys import assert_json_keys
from tests.hub.live import sql
from tests.hub.test_chats import ADMIN_GRANT, OPEN_SINKS
from tests.hub.test_plans import registration
from tests.hub.test_run_cli import as_json, cli, ok, write_credentials
from tests.hub.test_run_stream import members, serving, wait_for
from tests.hub.test_runs import OTHER, OWNER, PROJECT, READER

THUMBS = "\U0001f44d"


@pytest.fixture
def hub(hub_db, tmp_path, github):
    """The hub under uvicorn, with the members of ``tests.hub.test_run_stream``, the project not restricted, owner its
    admin, its agent @reviewer, and a home signed in for owner and someone-else."""
    app = create_app(live.hub_config(hub_db, tmp_path, github))
    with serving(app) as url, httpx.Client(base_url=url, timeout=10) as client:
        headers = members(client, github)
        opened = client.put(f"/v1/projects/{PROJECT}", json=registration(OPEN_SINKS), headers=headers["admin"])
        assert opened.status_code == 200, opened.text
        grant = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=headers["admin"])
        assert grant.status_code == 200, grant.text
        made = client.post(
            f"/v1/projects/{PROJECT}/agents", json={"handle": "reviewer", "name": "Reviewer"}, headers=headers["owner"]
        )
        assert made.status_code == 201, made.text
        homes = {
            who: write_credentials(tmp_path / who, url, login, headers[who]["Authorization"].removeprefix("Bearer "))
            for who, login in (("owner", OWNER), ("other", OTHER))
        }
        yield SimpleNamespace(app=app, client=client, headers=headers, homes=homes, db=hub_db)


def created(hub, who: str = "owner", **body) -> dict:
    body = {"title": "Release talk", "people": [OTHER], "agents": ["reviewer"], **body}
    response = hub.client.post("/v1/chats", json=body, headers=hub.headers[who])
    assert response.status_code == 201, response.text
    return response.json()


def sent(hub, who: str, chat_id: int, text: str, **body) -> dict:
    response = hub.client.post(f"/v1/chats/{chat_id}/messages", json={"body": text, **body}, headers=hub.headers[who])
    assert response.status_code == 201, response.text
    return response.json()


class ChatStream:
    """One response of server-sent events of a chat, read a message at a time."""

    def __init__(self, hub, chat_id: int, who: str = "other", headers=None, **params):
        self._context = hub.client.stream(
            "GET", f"/v1/chats/{chat_id}/stream", params=params, headers={**hub.headers[who], **(headers or {})}
        )
        self.response = self._context.__enter__()
        self._lines = self.response.iter_lines()

    def next(self) -> dict:
        """The next message as {field: value}, ``data`` decoded, comments skipped; EOFError when the stream ended."""
        while True:
            message: dict = {}
            for line in self._lines:
                if line == "":
                    if message:
                        break
                    continue
                field, _, value = line.partition(":")
                message[field or "comment"] = value.removeprefix(" ")
            else:
                raise EOFError("the stream ended")
            if set(message) == {"comment"}:
                continue
            message["data"] = json.loads(message["data"])
            return message

    def close(self) -> None:
        self._context.__exit__(None, None, None)


def kinds(messages: list[dict]) -> list[tuple[int, str]]:
    assert all(int(m["id"]) == m["data"]["seq"] for m in messages)
    return [(m["data"]["seq"], m["data"]["kind"]) for m in messages]


# The stream


def test_chat_messages_and_reactions_reach_an_open_stream_within_two_seconds(hub, monkeypatch):
    monkeypatch.setattr(chat_events, "STREAM_POLL_SECONDS", 60.0)  # only the notification can wake it in time
    chat = created(hub)  # events 1 to 3: owner, @reviewer and someone-else added
    stream = ChatStream(hub, chat["id"])
    try:
        assert stream.response.status_code == 200
        assert stream.response.headers.get_list("cache-control") == ["no-cache, no-transform"]
        first = [stream.next() for _ in range(3)]
        assert kinds(first) == [(1, "member_added"), (2, "member_added"), (3, "member_added")]
        assert [m["data"]["member"]["member"]["name"] for m in first] == [OWNER, "Reviewer", OTHER]
        sent_at = time.monotonic()
        message = sent(hub, "owner", chat["id"], "@someone-else ready?")
        event = stream.next()
        assert time.monotonic() - sent_at < 2
        assert (event["data"]["seq"], event["data"]["kind"]) == (4, "message")
        assert (
            event["data"]["message"]["id"] == message["id"]
            and event["data"]["message"]["body"] == "@someone-else ready?"
        )
        assert event["data"]["message"]["mentions"][0]["login"] == OTHER
        sent_at = time.monotonic()
        path = f"/v1/chats/{chat['id']}/messages/{message['id']}/reactions"
        assert hub.client.post(path, json={"emoji": THUMBS}, headers=hub.headers["other"]).status_code == 200
        reaction = stream.next()["data"]
        assert time.monotonic() - sent_at < 2
        assert (reaction["kind"], reaction["reaction"]["emoji"], reaction["reaction"]["count"]) == (
            "reaction_added",
            THUMBS,
            1,
        )
        assert reaction["reaction"]["by"]["login"] == OTHER
        # the streams of chats and of runs share the api process's one LISTEN connection
        activity = table("pg_stat_activity", column("datname"), column("application_name"))
        listening = sql(
            hub.db,
            select(func.count())
            .select_from(activity)
            .where(activity.c.datname == hub.db.name, activity.c.application_name == listen.APPLICATION_NAME),
        )
        assert listening == [(1,)]
    finally:
        stream.close()
    wait_for(lambda: hub.app.state.chat_streams.open_streams() == 0, what="the closed stream ending on the hub")


def test_a_chat_stream_resumed_with_last_event_id_misses_and_repeats_nothing(hub):
    chat = created(hub)
    stream = ChatStream(hub, chat["id"])
    try:
        before = kinds([stream.next() for _ in range(3)])
    finally:
        stream.close()
    for number in range(3):  # events 4 to 6, while nobody listened
        sent(hub, "owner", chat["id"], f"note {number}")
    resumed = ChatStream(hub, chat["id"], headers={"Last-Event-ID": "3"}, after=1)
    try:
        middle = kinds([resumed.next() for _ in range(3)])
        sent(hub, "other", chat["id"], "live")
        middle += kinds([resumed.next()])
    finally:
        resumed.close()
    assert [seq for seq, _ in before + middle] == list(range(1, 8))
    page = hub.client.get(
        f"/v1/chats/{chat['id']}/events", params={"after": 4, "limit": 2}, headers=hub.headers["owner"]
    )
    assert page.status_code == 200
    assert ([e["seq"] for e in page.json()["events"]], page.json()["more"], page.json()["last_seq"]) == (
        [5, 6],
        True,
        7,
    )


def test_a_chat_stream_ends_when_its_reader_leaves_the_chat(hub, monkeypatch):
    monkeypatch.setattr(chat_events, "STREAM_POLL_SECONDS", 60.0)
    chat = created(hub)
    stream = ChatStream(hub, chat["id"], after=3)
    try:
        left = hub.client.delete(f"/v1/chats/{chat['id']}/people/{OTHER}", headers=hub.headers["other"])
        assert left.status_code == 204
        end = stream.next()
        assert end["event"] == "end" and "that you are a member of" in end["data"]["reason"]
        with pytest.raises(EOFError):
            stream.next()
    finally:
        stream.close()
    refused = hub.client.get(f"/v1/chats/{chat['id']}/stream", headers=hub.headers["other"])
    assert refused.status_code == 404 and refused.json()["error"]


# The command line


def test_the_chat_commands_are_in_the_contract():
    printed = commands()
    assert {"hub chat create", "hub chat list", "hub chat show", "hub chat send", "hub chat react"} <= set(printed)
    assert printed["hub chat show"]["json"]["schema"] == "ChatPage"
    assert printed["hub chat send"]["json"] == printed["hub chat react"]["json"]


def chat_cli(monkeypatch, capsys, home, *args: str, stdin: str | None = None):
    return cli(monkeypatch, capsys, home, "hub", "chat", *args, stdin=stdin)


def test_hub_chat_creates_lists_shows_sends_and_reacts(hub, monkeypatch, capsys):
    owner, other = hub.homes["owner"], hub.homes["other"]
    made = as_json(
        chat_cli(
            monkeypatch, capsys, owner, "create", "Release talk", "--member", OTHER, "--agent", "@reviewer", "--json"
        )
    )
    assert_json_keys("hub chat create", made)
    assert [p["login"] for p in made["people"]] == [OWNER, OTHER] and made["agents"][0]["handle"] == "reviewer"
    chat_id = str(made["id"])

    first = as_json(
        chat_cli(monkeypatch, capsys, owner, "send", chat_id, "@reviewer and @someone-else: ready?", "--json")
    )
    assert_json_keys("hub chat send", first)
    assert [m["via"] for m in first["mentions"]] == ["handle", "login"]
    reply = ok(
        chat_cli(
            monkeypatch, capsys, other, "send", chat_id, "--file", "-", "--reply-to", str(first["id"]), stdin="Yes.\n"
        )
    )
    assert f"in the thread of #{first['id']}" in reply.out and "  Yes." in reply.out
    both = chat_cli(monkeypatch, capsys, other, "send", chat_id, "text", "--file", "-", stdin="more")
    assert both.code == 2 and "not both" in both.err

    reacted = as_json(chat_cli(monkeypatch, capsys, other, "react", chat_id, str(first["id"]), THUMBS, "--json"))
    assert_json_keys("hub chat react", reacted)
    assert [(r["emoji"], r["count"]) for r in reacted["reactions"]] == [(THUMBS, 1)]
    removed = ok(chat_cli(monkeypatch, capsys, other, "react", chat_id, str(first["id"]), THUMBS, "--remove"))
    assert removed.out == f"#{first['id']}: no reactions\n"
    bad = chat_cli(monkeypatch, capsys, other, "react", chat_id, str(first["id"]), "ok")
    assert bad.code == 2 and "not an emoji" in bad.err

    listed = as_json(chat_cli(monkeypatch, capsys, owner, "list", "--json"))
    assert_json_keys("hub chat list", listed)
    assert [(c["id"], c["unread"]) for c in listed] == [(made["id"], 1)]  # the reply; their own reads up to it
    assert "Release talk" in ok(chat_cli(monkeypatch, capsys, other, "list")).out

    shown = as_json(chat_cli(monkeypatch, capsys, other, "show", chat_id, "--json"))
    assert_json_keys("hub chat show", shown)
    assert [m["body"] for m in shown["messages"]] == ["@reviewer and @someone-else: ready?", "Yes.\n"]
    text = ok(chat_cli(monkeypatch, capsys, other, "show", chat_id)).out
    assert text.startswith(f"Chat {chat_id}: Release talk, created by {OWNER}")
    assert "  agents: @reviewer (Reviewer, evo-agents)" in text and f"#{first['id']} {OWNER} at" in text
    thread = as_json(chat_cli(monkeypatch, capsys, owner, "show", chat_id, "--thread", str(first["id"]), "--json"))
    assert len(thread["messages"]) == 2
    outsider = write_credentials(
        hub.homes["owner"].parent / "reader",
        str(hub.client.base_url),
        READER,
        hub.headers["reader"]["Authorization"][7:],
    )
    hidden = chat_cli(monkeypatch, capsys, outsider, "show", chat_id)
    assert hidden.code == 1 and "that you are a member of" in hidden.err
