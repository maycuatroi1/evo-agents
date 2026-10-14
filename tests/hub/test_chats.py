"""Group chats on the hub (``evo_agents.hub.server.chats``, ``chat_members``, ``chat_store``), as criteria a6 and a7
of the plan agent-profiles-group-chat name them: a member creates a chat with people and agents; only members read it
and write to it; adding an agent of a project needs the writer role on it, and adding a person needs a grant that
clears the label of every project with an agent in the chat; messages have threads; mentions are read once and kept
with the message (a person by login, an agent by handle, a project's name naming its default agent); a person
mentioned or added gets a notice, which names the chat and links to it only in a restricted chat; reactions are one
row per message, person or agent and emoji, counted by emoji; the reactions of a run's agent follow the run by rule;
and a message, mentions or not, makes no run and asks no model. The stream and the command line are in
``tests.hub.test_chat_stream``."""

from functools import partial

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from evo_agents.hub import chats, tables
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.chat_access import chat_access
from evo_agents.hub.server.chat_store import insert_message, status_reaction
from evo_agents.hub.server.security import Principal
from tests.hub.live import sql
from tests.hub.test_plans import registration
from tests.hub.test_run_stream import PUBLIC_READER
from tests.hub.test_run_stream import members as stream_members
from tests.hub.test_runs import OTHER, OWNER, PROJECT, READER, STRANGER, audit_rows

ADMIN_GRANT = {"role": "admin", "max_level": "internal"}
# Hub sinks cleared for internal anywhere: the project is not restricted (``telegram.restricted``), its notices whole.
OPEN_SINKS = [
    {"id": "claude-code@anthropic", "kind": "agent-session", "clearance": {"level": "internal", "location": "any"}},
    {"id": "hub", "kind": "hub", "clearance": {"level": "internal", "location": "any"}},
]
EYES, CHECK, CROSS = chats.EYES, chats.CHECK, chats.CROSS
THUMBS, HEART = "\U0001f44d", "❤️"


@pytest.fixture
def client(hub_db, tmp_path, github):
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        yield client


@pytest.fixture
def hub(client, github) -> dict:
    """Project evo-agents (label internal, not restricted) with owner as its admin, someone-else a writer, reader and
    public-reader readers up to internal and up to public; the agent @reviewer of the project; the headers of each
    member, of a stranger and of a hub admin without a grant."""
    headers = stream_members(client, github)
    opened = client.put(f"/v1/projects/{PROJECT}", json=registration(OPEN_SINKS), headers=headers["admin"])
    assert opened.status_code == 200, opened.text
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", json=ADMIN_GRANT, headers=headers["admin"])
    assert granted.status_code == 200, granted.text
    agent(client, headers["owner"], "reviewer")
    return headers


def agent(client, headers, handle: str, project: str = PROJECT) -> dict:
    body = {"handle": handle, "name": handle.title()}
    response = client.post(f"/v1/projects/{project}/agents", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def create(client, headers, title: str = "Release talk", people=(), agents=()):
    return client.post(
        "/v1/chats", json={"title": title, "people": list(people), "agents": list(agents)}, headers=headers
    )


def created(client, headers, title: str = "Release talk", people=(), agents=()) -> dict:
    response = create(client, headers, title, people, agents)
    assert response.status_code == 201, response.text
    return response.json()


def send(client, headers, chat_id: int, body: str, reply_to: int | None = None):
    payload = {"body": body} | ({"reply_to": reply_to} if reply_to else {})
    return client.post(f"/v1/chats/{chat_id}/messages", json=payload, headers=headers)


def sent(client, headers, chat_id: int, body: str, reply_to: int | None = None) -> dict:
    response = send(client, headers, chat_id, body, reply_to)
    assert response.status_code == 201, response.text
    return response.json()


def page(client, headers, chat_id: int, **params) -> dict:
    response = client.get(f"/v1/chats/{chat_id}/messages", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def react(client, headers, chat_id: int, message_id: int, emoji: str):
    return client.post(f"/v1/chats/{chat_id}/messages/{message_id}/reactions", json={"emoji": emoji}, headers=headers)


def notices(client, headers) -> list[dict]:
    response = client.get("/v1/me/notifications", params={"kind": "notice"}, headers=headers)
    assert response.status_code == 200, response.text
    return [n for n in response.json()["notifications"] if (n["notice_kind"] or "").startswith("chat_")]


def counted(db, table) -> int:
    return sql(db, select(func.count()).select_from(table))[0][0]


def principal(db, login: str) -> Principal:
    u = tables.users
    (user_id,) = sql(db, select(u.c.id).where(u.c.login == login))[0]
    return Principal(user_id=user_id, login=login, admin=False, token_id=0, kind="machine", token_hash="")


def on_hub(client, work):
    """Run ``work(conn)`` in one transaction of the hub's engine, on its event loop."""

    async def run(engine):
        async with engine.begin() as conn:
            return await work(conn)

    return client.portal.call(partial(run, client.app.state.engine))


# Creating and reading


def test_a_member_creates_a_chat_with_people_and_agents_and_only_members_read_it(client, hub, hub_db):
    chat = created(client, hub["owner"], people=[OTHER, OWNER], agents=["reviewer", PROJECT])
    assert (chat["title"], chat["created_by"], chat["restricted"]) == ("Release talk", OWNER, False)
    assert [p["login"] for p in chat["people"]] == [OWNER, OTHER]
    assert [(a["handle"], a["project"], a["is_default"], a["deleted"]) for a in chat["agents"]] == [
        ("reviewer", PROJECT, False, False),
        (PROJECT, PROJECT, True, False),
    ]
    assert chat["label"] == [{"level": "internal", "location": "any", "integrity": "U", "projects": [PROJECT]}]
    for who in ("owner", "other"):
        shown = client.get(f"/v1/chats/{chat['id']}", headers=hub[who])
        assert shown.status_code == 200 and shown.json()["id"] == chat["id"]
        listed = client.get("/v1/chats", headers=hub[who]).json()
        assert [(c["id"], c["people"], c["agents"]) for c in listed] == [
            (chat["id"], [OWNER, OTHER], ["reviewer", PROJECT])
        ]
    for who in ("reader", "stranger", "admin"):  # not in it: a chat that does not exist, whoever they are
        for path in ("", "/messages"):
            refused = client.get(f"/v1/chats/{chat['id']}{path}", headers=hub[who])
            assert refused.status_code == 404 and "that you are a member of" in refused.json()["message"]
        assert send(client, hub[who], chat["id"], "hello").status_code == 404
        assert client.get("/v1/chats", headers=hub[who]).json() == []
    assert ("chat.create", f"chat:{chat['id']}", OWNER, None) in audit_rows(hub_db, "chat")
    assert ("chat.add", f"chat:{chat['id']} agent:@reviewer", OWNER, PROJECT) in audit_rows(hub_db, "chat")


def test_adding_an_agent_to_a_chat_needs_the_writer_role_on_its_project(client, hub):
    chat = created(client, hub["reader"], people=[OWNER])  # a reader of the project makes a chat of people
    refused = client.put(f"/v1/chats/{chat['id']}/agents/reviewer", headers=hub["reader"])
    assert refused.status_code == 403 and "needs the writer role on project evo-agents" in refused.json()["message"]
    assert create(client, hub["reader"], agents=["reviewer"]).status_code == 403
    unknown = client.put(f"/v1/chats/{chat['id']}/agents/nobody", headers=hub["owner"])
    assert unknown.status_code == 404
    added = client.put(f"/v1/chats/{chat['id']}/agents/reviewer", headers=hub["owner"])
    assert added.status_code == 200 and [a["handle"] for a in added.json()["agents"]] == ["reviewer"]
    again = client.put(f"/v1/chats/{chat['id']}/agents/reviewer", headers=hub["owner"])  # in it already: no change
    assert again.status_code == 200 and len(again.json()["agents"]) == 1
    agent(client, hub["owner"], "retired")
    assert client.delete(f"/v1/projects/{PROJECT}/agents/retired", headers=hub["owner"]).status_code == 200
    deleted = client.put(f"/v1/chats/{chat['id']}/agents/retired", headers=hub["owner"])
    assert deleted.status_code == 409 and "takes no new work" in deleted.json()["message"]
    # an agent of a project the caller holds no grant on is one that does not exist
    second = client.put("/v1/projects/second", json=registration(), headers=hub["admin"])
    assert second.status_code == 200, second.text
    assert client.put("/v1/admin/projects/second/grants/reader", json=ADMIN_GRANT, headers=hub["admin"]).status_code
    agent(client, hub["reader"], "outsider", project="second")
    hidden = client.put(f"/v1/chats/{chat['id']}/agents/outsider", headers=hub["owner"])
    assert hidden.status_code == 404 and "no agent @outsider that you can see" in hidden.json()["message"]


def test_adding_a_person_to_a_chat_needs_a_grant_that_clears_every_agent_project(client, hub):
    chat = created(client, hub["owner"], agents=["reviewer"])
    for login, why in ((STRANGER, "no grant on project evo-agents"), (PUBLIC_READER, "up to public, below its")):
        refused = client.put(f"/v1/chats/{chat['id']}/people/{login}", headers=hub["owner"])
        assert refused.status_code == 409 and why in refused.json()["message"], refused.text
        assert create(client, hub["owner"], people=[login], agents=["reviewer"]).status_code == 409
    nobody = client.put(f"/v1/chats/{chat['id']}/people/nobody-at-all", headers=hub["owner"])
    assert nobody.status_code == 404 and "nobody signed in to this hub" in nobody.json()["message"]
    added = client.put(f"/v1/chats/{chat['id']}/people/{READER}", headers=hub["owner"])
    assert added.status_code == 200 and [p["login"] for p in added.json()["people"]] == [OWNER, READER]
    # a chat of people takes anyone; an agent then needs every one of them to clear its project's label
    people = created(client, hub["owner"], people=[STRANGER, OTHER])
    raised = client.put(f"/v1/chats/{people['id']}/agents/reviewer", headers=hub["owner"])
    assert raised.status_code == 409 and f"{STRANGER} (no grant on project evo-agents)" in raised.json()["message"]
    # a person and an agent of the same name cannot be in one chat: @name would name either
    agent(client, hub["owner"], READER)
    clash = client.put(f"/v1/chats/{chat['id']}/agents/{READER}", headers=hub["owner"])
    assert clash.status_code == 409 and "would name either" in clash.json()["message"]


def test_a_chat_member_who_no_longer_clears_its_label_is_refused(client, hub):
    chat = created(client, hub["owner"], people=[OTHER], agents=["reviewer"])
    lowered = {"role": "writer", "max_level": "public"}
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{OTHER}", json=lowered, headers=hub["admin"])
    assert granted.status_code == 200, granted.text
    for refused in (
        client.get(f"/v1/chats/{chat['id']}", headers=hub["other"]),
        send(client, hub["other"], chat["id"], "still here?"),
    ):
        assert refused.status_code == 403 and "no longer clear its label" in refused.json()["message"]


def test_chat_messages_carry_the_label_they_were_sent_under(client, hub):
    chat = created(client, hub["owner"], agents=["reviewer"])
    before = sent(client, hub["owner"], chat["id"], "internal work")
    assert before["label"] == chat["label"] and before["label"][0]["projects"] == [PROJECT]
    assert client.delete(f"/v1/chats/{chat['id']}/agents/reviewer", headers=hub["owner"]).status_code == 204
    # with no agent the chat holds no project's work, and someone without a grant may join it
    assert client.put(f"/v1/chats/{chat['id']}/people/{STRANGER}", headers=hub["owner"]).status_code == 200
    after = sent(client, hub["owner"], chat["id"], "nothing internal")
    assert after["label"] == []
    assert [m["id"] for m in page(client, hub["stranger"], chat["id"])["messages"]] == [after["id"]]
    assert [m["id"] for m in page(client, hub["owner"], chat["id"])["messages"]] == [before["id"], after["id"]]
    assert react(client, hub["stranger"], chat["id"], before["id"], THUMBS).status_code == 404
    assert send(client, hub["stranger"], chat["id"], "a reply", reply_to=before["id"]).status_code == 404


def test_chat_members_leave_and_only_the_creator_takes_others_out(client, hub, hub_db):
    chat = created(client, hub["owner"], people=[OTHER, READER], agents=["reviewer"])
    taken = client.delete(f"/v1/chats/{chat['id']}/people/{READER}", headers=hub["other"])
    assert taken.status_code == 403 and f"only {OWNER}" in taken.json()["message"]
    assert client.delete(f"/v1/chats/{chat['id']}/people/{OTHER}", headers=hub["other"]).status_code == 204
    assert client.get(f"/v1/chats/{chat['id']}", headers=hub["other"]).status_code == 404
    assert client.delete(f"/v1/chats/{chat['id']}/people/{READER}", headers=hub["owner"]).status_code == 204
    assert client.delete(f"/v1/chats/{chat['id']}/agents/reviewer", headers=hub["owner"]).status_code == 204
    assert client.delete(f"/v1/chats/{chat['id']}/agents/reviewer", headers=hub["owner"]).status_code == 404
    back = client.put(f"/v1/chats/{chat['id']}/people/{OTHER}", headers=hub["owner"])  # one who left comes back
    assert back.status_code == 200 and [p["login"] for p in back.json()["people"]] == [OWNER, OTHER]
    assert ("chat.remove", f"chat:{chat['id']} person:{OTHER}", OTHER, None) in audit_rows(hub_db, "chat")


# Messages, threads and mentions


def test_chat_threads_put_every_reply_in_the_thread_of_the_first_message(client, hub):
    chat = created(client, hub["owner"], people=[OTHER])
    first = sent(client, hub["owner"], chat["id"], "Shall we ship 0.16?")
    reply = sent(client, hub["other"], chat["id"], "Yes", reply_to=first["id"])
    deeper = sent(client, hub["owner"], chat["id"], "Then I tag it", reply_to=reply["id"])
    other = sent(client, hub["other"], chat["id"], "Another topic")
    assert (reply["thread_root_id"], deeper["thread_root_id"], other["thread_root_id"]) == (
        first["id"],
        first["id"],
        None,
    )
    whole = page(client, hub["owner"], chat["id"])
    assert [(m["id"], m["reply_count"]) for m in whole["messages"]] == [
        (first["id"], 2),
        (reply["id"], 0),
        (deeper["id"], 0),
        (other["id"], 0),
    ]
    assert [m["seq"] for m in whole["messages"]] == sorted(m["seq"] for m in whole["messages"])
    thread = page(client, hub["other"], chat["id"], thread=first["id"])
    assert [m["id"] for m in thread["messages"]] == [first["id"], reply["id"], deeper["id"]]
    older = page(client, hub["other"], chat["id"], limit=2)
    assert ([m["id"] for m in older["messages"]], older["more"]) == ([deeper["id"], other["id"]], True)
    elsewhere = created(client, hub["other"], title="Elsewhere")
    stray = send(client, hub["other"], elsewhere["id"], "answer", reply_to=first["id"])
    assert stray.status_code == 404 and f"has no message {first['id']}" in stray.json()["message"]
    assert send(client, hub["owner"], chat["id"], "   ").status_code == 422
    assert send(client, hub["owner"], chat["id"], "x" * (chats.MAX_MESSAGE_BYTES + 1)).status_code == 422


def test_chat_mentions_are_read_once_and_kept_with_the_message(client, hub, hub_db):
    chat = created(client, hub["owner"], people=[OTHER], agents=["reviewer", PROJECT])
    text = "@Someone-Else and @reviewer, then @evo-agents. Mail me at owner@example.org; `@reviewer` and @nobody"
    message = sent(client, hub["owner"], chat["id"], text)
    assert [(m["name"], m["via"], m["login"], m["handle"], m["refusal"]) for m in message["mentions"]] == [
        (OTHER, "login", OTHER, None, None),
        ("reviewer", "handle", None, "reviewer", None),
        (PROJECT, "project", None, PROJECT, None),
    ]
    cm = tables.chat_mentions
    kept = sql(hub_db, select(cm.c.ordinal, cm.c.name).where(cm.c.message_id == message["id"]).order_by(cm.c.ordinal))
    assert kept == [(0, OTHER), (1, "reviewer"), (2, PROJECT)]
    # an agent the sender can see but the chat does not hold, or one deleted since, reaches no one; the hub says so
    agent(client, hub["owner"], "auditor")
    agent(client, hub["owner"], "retired")
    assert client.put(f"/v1/chats/{chat['id']}/agents/retired", headers=hub["owner"]).status_code == 200
    assert client.delete(f"/v1/projects/{PROJECT}/agents/retired", headers=hub["owner"]).status_code == 200
    asked = sent(client, hub["other"], chat["id"], "@auditor and @retired, please look")
    assert [(m["name"], m["via"], m["handle"]) for m in asked["mentions"]] == [
        ("auditor", None, "auditor"),
        ("retired", "handle", "retired"),
    ]
    assert asked["mentions"][0]["refusal"] == "@auditor is not in this chat: add it to the chat, then mention it again"
    assert asked["mentions"][1]["refusal"] == "@retired was deleted: it takes no new work"
    answer = page(client, hub["other"], chat["id"], thread=asked["id"])["messages"][-1]
    assert (answer["author"]["kind"], answer["thread_root_id"], answer["mentions"]) == ("hub", asked["id"], [])
    assert answer["body"].splitlines() == [m["refusal"] for m in asked["mentions"]]


def test_a_person_mentioned_in_a_chat_or_added_to_one_gets_a_notice(client, hub):
    chat = created(client, hub["owner"], people=[OTHER], agents=["reviewer"])
    added = notices(client, hub["other"])
    assert [(n["notice_kind"], n["title"], n["link"]) for n in added] == [
        ("chat_added", f"{OWNER} added you to Release talk", f"/chats/{chat['id']}")
    ]
    assert notices(client, hub["owner"]) == []  # the creator is told nothing
    message = sent(client, hub["owner"], chat["id"], "@someone-else can you check the release notes? @owner too")
    mentioned = notices(client, hub["other"])[0]
    assert (mentioned["notice_kind"], mentioned["title"], mentioned["project"]) == (
        "chat_mention",
        f"{OWNER} mentioned you in chat Release talk",
        PROJECT,
    )
    assert mentioned["body"] == message["body"]
    assert mentioned["link"] == f"/chats/{chat['id']}?message={message['id']}"
    assert notices(client, hub["owner"]) == []  # mentioning oneself tells nobody


def test_chat_notices_of_a_restricted_project_name_the_chat_and_link_only(client, hub):
    sinks = [{"id": "hub", "kind": "hub", "clearance": {"level": "customer", "location": "any"}}]
    restricted = client.put("/v1/projects/meridai", json=registration(sinks), headers=hub["admin"])
    assert restricted.status_code == 200, restricted.text
    for login in (OWNER, OTHER):
        grant = {"role": "admin", "max_level": "customer"}
        assert client.put(f"/v1/admin/projects/meridai/grants/{login}", json=grant, headers=hub["admin"]).status_code
    agent(client, hub["owner"], "analyst", project="meridai")
    chat = created(client, hub["owner"], title="Customer call", people=[OTHER], agents=["analyst"])
    assert chat["restricted"] is True
    message = sent(client, hub["owner"], chat["id"], "@someone-else the customer's numbers are in")
    added, mentioned = sorted(notices(client, hub["other"]), key=lambda n: n["notice_kind"])
    assert (added["title"], added["body"], added["project"]) == (
        "You were added to chat Customer call",
        None,
        "meridai",
    )
    assert (mentioned["title"], mentioned["body"]) == ("You were mentioned in chat Customer call", None)
    assert mentioned["link"] == f"/chats/{chat['id']}?message={message['id']}"
    assert OWNER not in mentioned["title"] and "numbers" not in mentioned["title"]


def test_chat_read_marks_count_the_unread_messages_of_others(client, hub):
    chat = created(client, hub["owner"], people=[OTHER])
    sent(client, hub["owner"], chat["id"], "one")
    last = sent(client, hub["owner"], chat["id"], "two")
    assert [c["unread"] for c in client.get("/v1/chats", headers=hub["other"]).json()] == [2]
    assert [c["unread"] for c in client.get("/v1/chats", headers=hub["owner"]).json()] == [0]
    read = client.post(f"/v1/chats/{chat['id']}/read", json={"seq": last["seq"]}, headers=hub["other"])
    assert read.status_code == 200 and read.json() == {"last_read_seq": last["seq"], "unread": 0}
    assert [c["unread"] for c in client.get("/v1/chats", headers=hub["other"]).json()] == [0]


# Reactions


def test_chat_reactions_are_one_row_per_person_and_emoji_counted_by_emoji(client, hub, hub_db):
    chat = created(client, hub["owner"], people=[OTHER])
    message = sent(client, hub["owner"], chat["id"], "Ship it?")
    for who, emoji in (("owner", THUMBS), ("other", THUMBS), ("owner", THUMBS), ("owner", HEART)):
        response = react(client, hub[who], chat["id"], message["id"], emoji)
        assert response.status_code == 200, response.text
    shown = response.json()["reactions"]
    assert [(r["emoji"], r["count"], r["mine"], r["people"], r["agents"]) for r in shown] == [
        (THUMBS, 2, True, [OWNER, OTHER], []),
        (HEART, 1, True, [OWNER], []),
    ]
    assert counted(hub_db, tables.chat_reactions) == 3
    path = f"/v1/chats/{chat['id']}/messages/{message['id']}/reactions"
    taken = client.delete(path, params={"emoji": THUMBS}, headers=hub["other"])
    assert taken.status_code == 200
    assert [(r["emoji"], r["count"], r["mine"]) for r in taken.json()["reactions"]] == [
        (THUMBS, 1, False),
        (HEART, 1, False),
    ]
    for text in ("ok", "<b>", "\U0001f44d ", "1", "‮\U0001f44d"):
        refused = react(client, hub["owner"], chat["id"], message["id"], text)
        assert refused.status_code == 422, text
    for code in range(0x1F600, 0x1F600 + chats.MAX_EMOJI_KINDS - 2):
        assert react(client, hub["other"], chat["id"], message["id"], chr(code)).status_code == 200
    full = react(client, hub["other"], chat["id"], message["id"], "\U0001f680")
    assert full.status_code == 409 and "different emoji" in full.json()["message"]
    assert react(client, hub["other"], chat["id"], message["id"], THUMBS).status_code == 200  # one already there


def test_chat_status_reactions_of_an_agent_follow_its_run_by_rule(client, hub):
    chat = created(client, hub["owner"], agents=["reviewer"])
    message = sent(client, hub["owner"], chat["id"], "@reviewer look at the diff")
    profile_id = profile_id_of(client, "reviewer")

    def mark(state: str):
        return on_hub(
            client, lambda conn: status_reaction(conn, message_id=message["id"], profile_id=profile_id, state=state)
        )

    def shown():
        (found,) = [m for m in page(client, hub["owner"], chat["id"])["messages"] if m["id"] == message["id"]]
        return [(r["emoji"], r["agents"]) for r in found["reactions"]]

    assert mark("leased") == EYES
    assert shown() == [(EYES, ["reviewer"])]
    assert mark("leased") is None  # a lost run tried again keeps its eyes
    assert mark("running") is None and mark("waiting") is None
    assert mark("done") == CHECK
    assert shown() == [(CHECK, ["reviewer"])]  # the check mark takes the eyes off
    assert mark("leased") == EYES and mark("failed") == CROSS
    assert shown() == [(CHECK, ["reviewer"]), (CROSS, ["reviewer"])]
    assert mark("cancelled") is None


def profile_id_of(client, handle: str) -> int:
    ap = tables.agent_profiles

    async def find(conn):
        return (await conn.execute(select(ap.c.id).where(ap.c.handle == handle))).scalar_one()

    return on_hub(client, find)


def test_a_soft_deleted_agent_keeps_its_name_and_avatar_on_old_chat_messages(client, hub, hub_db):
    painter = agent(client, hub["owner"], "painter")
    sha = "ab" * 32
    ap = tables.agent_profiles
    sql(hub_db, update(ap).where(ap.c.handle == "painter").values(avatar_sha256=sha))
    chat = created(client, hub["owner"], agents=["painter"])
    profile_id = profile_id_of(client, "painter")
    owner = principal(hub_db, OWNER)

    async def write(conn):
        access = await chat_access(conn, owner, chat["id"])
        message_id, _ = await insert_message(
            conn, access, "Here is the logo.", thread_root_id=None, author_profile_id=profile_id
        )
        await status_reaction(conn, message_id=message_id, profile_id=profile_id, state="done")
        return message_id

    message_id = on_hub(client, write)
    assert client.delete(f"/v1/projects/{PROJECT}/agents/painter", headers=hub["owner"]).status_code == 200
    (message,) = page(client, hub["owner"], chat["id"])["messages"]
    assert message["id"] == message_id
    author = message["author"]
    assert (author["kind"], author["handle"], author["name"], author["deleted"]) == (
        "agent",
        "painter",
        "Painter",
        True,
    )
    assert author["color"] == painter["color"]
    assert author["icon_url"] == f"/v1/projects/{PROJECT}/agents/painter/avatar?v={sha[:16]}"
    assert message["reactions"] == [{"emoji": CHECK, "count": 1, "mine": False, "people": [], "agents": ["painter"]}]
    shown = client.get(f"/v1/chats/{chat['id']}", headers=hub["owner"]).json()
    assert [(a["handle"], a["name"], a["deleted"]) for a in shown["agents"]] == [("painter", "Painter", True)]


# No mention, no model


def test_chat_no_mention_message_makes_no_run_and_asks_no_model(client, hub, hub_db):
    chat = created(client, hub["owner"], people=[OTHER, READER], agents=["reviewer", PROJECT])
    runs_before, events_before = counted(hub_db, tables.runs), counted(hub_db, tables.run_events)
    for who, text in (("owner", "Morning all"), ("other", "The build is green"), ("reader", "`@reviewer` in code")):
        message = sent(client, hub[who], chat["id"], text)
        assert message["mentions"] == []
    # a mention reaches its agent's member row, still with no run and no model: runs come with a later step
    mentioned = sent(client, hub["owner"], chat["id"], "@reviewer what do you think?")
    assert [m["via"] for m in mentioned["mentions"]] == ["handle"]
    assert counted(hub_db, tables.runs) == runs_before == 0
    assert counted(hub_db, tables.run_events) == events_before == 0
    assert counted(hub_db, tables.chat_messages) == 4
