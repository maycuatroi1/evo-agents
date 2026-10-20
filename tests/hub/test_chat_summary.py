"""What the list of chats and a chat's people show of their faces (``evo_agents.hub.server.chat_summaries``,
``chat_views``), as criterion a4 of the plan agent-direct-chats names it: ChatSummary keeps its fields and adds the
chat's kind, image, the agent of a direct chat, a few members other than the reader and the last message of its main
line, as one short line without markdown whose text a reader who does not clear its label never gets; a person in a
chat, on a message and in an event carries their GitHub avatar, none for a login that never signed in; and GET
/v1/chats takes as many queries for many chats as for one."""

from __future__ import annotations

from functools import partial

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import event, select

from evo_agents.hub import chats, tables
from tests.hub import test_chats as group_chats
from tests.hub.live import sql
from tests.hub.test_runs import OTHER, OWNER, PROJECT, STRANGER

client, hub = group_chats.client, group_chats.hub  # fixtures: project evo-agents, owner its admin, the agent @reviewer
agent, created, sent, page = group_chats.agent, group_chats.created, group_chats.sent, group_chats.page
NEVER_SIGNED_IN = "lumen-dev"


def listed(client, headers) -> dict[int, dict]:
    response = client.get("/v1/chats", headers=headers)
    assert response.status_code == 200, response.text
    return {item["id"]: item for item in response.json()}


def avatar(db, login: str) -> str | None:
    u = tables.users
    (github_id,) = sql(db, select(u.c.github_id).where(u.c.login == login))[0]
    return chats.github_avatar(github_id)


def reviewer_face(color: str) -> dict:
    face = {"kind": "agent", "login": None, "handle": "reviewer", "project": PROJECT, "name": "Reviewer"}
    return face | {"color": color, "icon_url": None, "deleted": False, "avatar_url": None}


def person_face(db, login: str) -> dict:
    face = {"kind": "person", "login": login, "handle": None, "project": None, "name": login, "color": None}
    return face | {"icon_url": None, "deleted": False, "avatar_url": avatar(db, login)}


def latest_id(db, chat_id: int) -> int:
    cm = tables.chat_messages
    return sql(db, select(cm.c.id).where(cm.c.chat_id == chat_id).order_by(cm.c.seq.desc()).limit(1))[0][0]


# The list


def test_the_chat_summary_shows_the_faces_the_agent_and_the_last_message_of_each_chat(client, hub, hub_db):
    group = created(client, hub["owner"], people=[OTHER], agents=["reviewer"])  # added: owner, reviewer, other
    color = group["agents"][0]["color"]
    root = sent(client, hub["owner"], group["id"], "## Plan\n**Ship** it, see [the plan](https://northwind.test/p)")
    sent(client, hub["other"], group["id"], "a reply in its thread", reply_to=root["id"])
    direct = client.put("/v1/chats/direct/reviewer", headers=hub["owner"]).json()
    sent(client, hub["owner"], direct["id"], "please look at `release.md`")
    quiet = created(client, hub["owner"], title="Nobody else")

    mine = listed(client, hub["owner"])
    shown = mine[group["id"]]
    assert (shown["people"], shown["agents"], shown["kind"], shown["image_url"]) == (
        [OWNER, OTHER],
        ["reviewer"],
        "group",
        None,
    )
    assert shown["direct_agent"] is None
    assert shown["faces"] == [reviewer_face(color), person_face(hub_db, OTHER)]
    last = shown["last_message"]
    assert (last["id"], last["thread_root_id"], last["hidden"]) == (root["id"], None, False)  # the main line's
    assert last["preview"] == "Plan Ship it, see the plan"
    assert last["author"] == person_face(hub_db, OWNER) and last["created_at"] == root["created_at"]
    assert listed(client, hub["other"])[group["id"]]["faces"] == [person_face(hub_db, OWNER), reviewer_face(color)]

    face = mine[direct["id"]]
    assert (face["kind"], face["direct_agent"], face["faces"]) == (
        "direct",
        reviewer_face(color),
        [reviewer_face(color)],
    )
    # a direct chat's main line holds its threads: the answer under the message is its last message
    assert face["last_message"]["id"] == latest_id(hub_db, direct["id"])
    assert (mine[quiet["id"]]["faces"], mine[quiet["id"]]["last_message"]) == ([], None)


def test_a_chat_last_message_the_reader_does_not_clear_keeps_its_text_hidden(client, hub):
    chat = created(client, hub["owner"], agents=["reviewer"])
    before = sent(client, hub["owner"], chat["id"], "internal work")
    assert client.delete(f"/v1/chats/{chat['id']}/agents/reviewer", headers=hub["owner"]).status_code == 204
    assert client.put(f"/v1/chats/{chat['id']}/people/{STRANGER}", headers=hub["owner"]).status_code == 200
    theirs = listed(client, hub["stranger"])[chat["id"]]["last_message"]
    assert (theirs["id"], theirs["hidden"], theirs["preview"]) == (before["id"], True, None)
    assert theirs["author"]["login"] == OWNER
    assert listed(client, hub["owner"])[chat["id"]]["last_message"]["preview"] == "internal work"
    after = sent(client, hub["owner"], chat["id"], "nothing internal")
    theirs = listed(client, hub["stranger"])[chat["id"]]["last_message"]
    assert (theirs["id"], theirs["hidden"], theirs["preview"]) == (after["id"], False, "nothing internal")


def test_the_chat_summary_takes_as_many_queries_for_six_chats_as_for_one(client, hub, hub_db):
    from evo_agents.hub.server.chat_summaries import chat_summaries  # after the app: its imports go round otherwise

    user = group_chats.principal(hub_db, OWNER)

    def counted() -> tuple[list, int]:
        async def run(engine):
            async with engine.begin() as conn:
                seen = []

                def count(connection, cursor, statement, parameters, context, executemany):
                    if connection is conn.sync_connection:  # the hub's own tasks use other connections
                        seen.append(statement)

                event.listen(engine.sync_engine, "before_cursor_execute", count)
                try:
                    found = await chat_summaries(conn, user.user_id)
                finally:
                    event.remove(engine.sync_engine, "before_cursor_execute", count)
                return found, len(seen)

        return client.portal.call(partial(run, client.app.state.engine))

    first = created(client, hub["owner"], people=[OTHER], agents=["reviewer"])
    sent(client, hub["owner"], first["id"], "one")
    one_chat, few = counted()
    assert [chat.id for chat in one_chat] == [first["id"]] and few == 6, few
    agent(client, hub["owner"], "auditor")
    for n, agents in enumerate((["reviewer"], ["auditor"], ["reviewer", "auditor"], [PROJECT], [])):
        chat = created(client, hub["owner"], title=f"Chat {n}", people=[OTHER], agents=agents)
        message = sent(client, hub["other"], chat["id"], f"message {n}")
        sent(client, hub["owner"], chat["id"], "reply", reply_to=message["id"])
    client.put("/v1/chats/direct/auditor", headers=hub["owner"])
    six_chats, many = counted()
    assert len(six_chats) == 7 and many == few


# People's GitHub avatars


def test_people_carry_their_github_avatar_and_a_login_never_signed_in_has_none(client, hub, hub_db):
    grant = {"role": "reader", "max_level": "internal"}
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{NEVER_SIGNED_IN}", json=grant, headers=hub["admin"])
    assert granted.status_code == 200, granted.text
    chat = created(client, hub["owner"], people=[OTHER, NEVER_SIGNED_IN], agents=["reviewer"])
    owner, other = avatar(hub_db, OWNER), avatar(hub_db, OTHER)
    assert owner.startswith(chats.GITHUB_AVATARS + "/u/") and owner != other
    assert avatar(hub_db, NEVER_SIGNED_IN) is None
    expected = [(OWNER, owner), (OTHER, other), (NEVER_SIGNED_IN, None)]
    assert [(p["login"], p["avatar_url"]) for p in chat["people"]] == expected
    shown = client.get(f"/v1/chats/{chat['id']}", headers=hub["other"]).json()
    assert [(p["login"], p["avatar_url"]) for p in shown["people"]] == expected
    message = sent(client, hub["other"], chat["id"], "hello")
    assert message["author"]["avatar_url"] == other
    assert group_chats.react(client, hub["owner"], chat["id"], message["id"], group_chats.THUMBS).status_code == 200
    assert [m["author"]["avatar_url"] for m in page(client, hub["owner"], chat["id"])["messages"]] == [other]
    events = client.get(f"/v1/chats/{chat['id']}/events", headers=hub["owner"]).json()["events"]
    members = [(e["member"]["member"]["name"], e["member"]["member"]["avatar_url"]) for e in events if e["member"]]
    assert members == [(OWNER, owner), ("Reviewer", None), (OTHER, other), (NEVER_SIGNED_IN, None)]
    (reaction,) = [e["reaction"] for e in events if e["reaction"]]
    assert (reaction["by"]["login"], reaction["by"]["avatar_url"]) == (OWNER, owner)
    (said,) = [e["message"] for e in events if e["message"]]
    assert said["author"]["avatar_url"] == other
    faces = listed(client, hub["owner"])[chat["id"]]["faces"]
    assert [(f["name"], f["avatar_url"]) for f in faces] == [
        ("Reviewer", None),
        (OTHER, other),
        (NEVER_SIGNED_IN, None),
    ]


# The preview


@pytest.mark.parametrize(
    "text, shown",
    [
        ("# Release\n\n**Bold**, _italic_ and `code`", "Release Bold, italic and code"),
        ("see [the plan](https://northwind.test/plan) and ![a chart](chart.png)", "see the plan and a chart"),
        ("> quoted\n- one\n- [x] two\n3. three\n\n---", "quoted one two three"),
        ("```python\nprint('hi')\n```", "print('hi')"),
        ("<b>bold</b> <https://lumen.test/x>", "bold https://lumen.test/x"),
        ("@reviewer look at snake_case_name and 2*3*4", "@reviewer look at snake_case_name and 2*3*4"),
        ("~~gone~~ __strong__ *em*", "gone strong em"),
        ("  many\n\n\n  lines   here ", "many lines here"),
    ],
)
def test_a_chat_preview_is_one_line_of_plain_text(text, shown):
    assert chats.preview(text) == shown


def test_a_chat_preview_cuts_long_text_with_an_ellipsis():
    cut = chats.preview("word " * 100)
    assert len(cut) == chats.PREVIEW_CHARS and cut.endswith("…") and not cut.endswith(" …")
    assert chats.preview("x" * chats.PREVIEW_CHARS) == "x" * chats.PREVIEW_CHARS
