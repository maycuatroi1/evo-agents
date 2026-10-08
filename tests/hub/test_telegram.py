"""The Telegram channel on the hub (``evo_agents.hub.server.telegram``), against a fake Bot API: no call reaches
Telegram.

The checks step 5 of the curator-agent plan names for a7: one bot of the hub; a member links a private chat with a
one-time code that lives 10 minutes, through /start, and unlinks with /stop or on the web; a decision goes out with one
button per option and one that opens it on the web, and so does a tier 2 proposal with Accept, Reject and Defer; a
button and a reply answer through the same code as the web; the webhook checks the secret token header; the hub keeps
to the Bot FAQ's pace and waits out a 429; a project whose hub sink is cleared for customer or domestic-only gets its
name, the kind and the link only; and without its two variables the channel is off while the hub runs."""

import itertools
from datetime import timedelta
from functools import partial
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import delete, extract, func, insert, select, update

from evo_agents.hub import tables
from evo_agents.hub import telegram as model
from evo_agents.hub.server import notifications
from evo_agents.hub.server import telegram as server_telegram
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.security import CSRF_HEADER, SESSION_COOKIE, csrf_token, hash_token
from tests.hub.contract_keys import assert_json_keys
from tests.hub.fake_telegram import (
    BOT,
    SECRET,
    SECRET_HEADER,
    TOKEN,
    FakeTelegram,
    button_update,
    private_chat,
    text_update,
)
from tests.hub.live import sql
from tests.hub.test_curator import night  # noqa: F401 (a fixture, on this module's web_client)
from tests.hub.test_decisions import ANSWER_TEXT, OPTIONS, QUESTION, asked, decision
from tests.hub.test_decisions import answered as answered_decision
from tests.hub.test_plan_runs import plan_body, push, started
from tests.hub.test_review_runs import answered as answered_proposal
from tests.hub.test_review_runs import (
    held_review,
    proposed,
    review,  # noqa: F401 (a fixture)
)
from tests.hub.test_run_cli import as_json, cli, ok, write_credentials
from tests.hub.test_run_stream import serving
from tests.hub.test_runs import OTHER, OWNER, PROJECT, STRANGER, audit_rows, members, report

OWNER_CHAT, OTHER_CHAT = 7001, 7002
CHANNELS = tables.notification_channels
DELIVERIES = tables.notification_deliveries
updates = itertools.count(1)


@pytest.fixture
def bot(monkeypatch):
    """The fake Bot API in place of Telegram's, and the waits of the channel's pacing recorded instead of slept."""
    fake = FakeTelegram()
    monkeypatch.setattr(server_telegram, "TRANSPORT", fake.transport())
    waits: list[float] = []

    async def pause(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(server_telegram, "pause", pause)
    return SimpleNamespace(fake=fake, waits=waits)


def telegram_config(db, tmp_path, github, **changes):
    values = {"telegram_bot_token": TOKEN, "telegram_webhook_secret": SECRET, **changes}
    return live.hub_config(db, tmp_path, github, **live.web_changes(github), **values)


@pytest.fixture
def client(hub_db, tmp_path, github, bot):
    with TestClient(create_app(telegram_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        client.app.state.telegram_inbound = server_telegram.InboundLimit(updates=1000)  # one test may send many
        yield client


@pytest.fixture
def web_client(hub_db, tmp_path, github, bot):
    """The hub the fixtures of test_curator and test_review_runs build on, here with the Telegram channel on."""
    with TestClient(create_app(telegram_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        client.app.state.telegram_inbound = server_telegram.InboundLimit(updates=1000)
        yield client


@pytest.fixture
def hub(client, github, hub_db) -> dict:
    """test_runs' members with machine tokens, and owner_web and other_web: web sessions of owner and other, which a
    chat is linked from. The project's hub sink clears internal for any location: its messages are not restricted."""
    headers = members(client, github)
    push(client, headers["owner"], plan_body())
    unrestricted(hub_db)
    headers["owner_web"] = web(client, hub_db, OWNER)
    headers["other_web"] = web(client, hub_db, OTHER)
    return headers


def unrestricted(db, clearance: dict | None = None) -> None:
    """The project's hub sink cleared for ``clearance``, internal for any location unless it says otherwise."""
    sinks = tables.project_sinks
    cleared = clearance or {"level": "internal", "location": "any"}
    sql(db, update(sinks).values(clearance=cleared).where(sinks.c.kind == "hub"))


def web(client, db, login: str) -> dict:
    """The headers of a new web session of ``login``, as the web sends a write: its cookie and its CSRF header."""
    token, _ = live.web_session(db, login)
    return session_headers(client, token)


def session_headers(client, token: str) -> dict:
    secret = client.app.state.config.session_secret
    return {"Cookie": f"{SESSION_COOKIE}={token}", CSRF_HEADER: csrf_token(secret, hash_token(token))}


def webhook(client, update: dict, secret: str | None = SECRET):
    headers = {SECRET_HEADER: secret} if secret is not None else {}
    return client.post("/v1/telegram/webhook", json=update, headers=headers)


def handled(client, update: dict) -> str:
    response = webhook(client, update)
    assert response.status_code == 200, response.text
    return response.json()["outcome"]


def status(client, headers) -> dict:
    response = client.get("/v1/me/telegram", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def new_link(client, headers) -> str:
    response = client.post("/v1/me/telegram/link", headers=headers)
    assert response.status_code == 201, response.text
    url = response.json()["url"]
    assert url.startswith(f"https://t.me/{BOT}?start=")
    return url.rpartition("=")[2]


def say(client, chat_id: int, text: str, *, username: str | None = "owner_tg", reply_to: int | None = None) -> str:
    chat, sender = private_chat(chat_id, username)
    return handled(client, text_update(next(updates), chat, sender, text, reply_to=reply_to))


def linked(client, headers, chat_id: int = OWNER_CHAT) -> None:
    assert say(client, chat_id, f"/start {new_link(client, headers)}") == "linked"


def press(client, chat_id: int, data: str, message: dict) -> str:
    chat, sender = private_chat(chat_id)
    return handled(client, button_update(next(updates), chat, sender, data, message))


def deliver(client) -> dict:
    engine, config = client.app.state.engine, client.app.state.config
    return client.portal.call(partial(notifications.deliver_notifications, engine, config=config))


def replies(fake: FakeTelegram, chat_id: int) -> list[str]:
    return [
        payload["text"] for payload in fake.sent() if payload["chat_id"] == chat_id and "reply_markup" not in payload
    ]


def telegram_deliveries(db) -> list[tuple]:
    query = (
        select(DELIVERIES.c.state, DELIVERIES.c.attempts, DELIVERIES.c.external_id, DELIVERIES.c.last_error)
        .join_from(DELIVERIES, CHANNELS, CHANNELS.c.id == DELIVERIES.c.channel_id)
        .where(CHANNELS.c.kind == "telegram")
        .order_by(DELIVERIES.c.id)
    )
    return sql(db, query)


# Linking a chat


def test_a_member_links_a_private_chat_with_a_one_time_code_that_lives_ten_minutes(client, hub, hub_db, bot):
    owner = hub["owner_web"]
    shown = status(client, owner)
    assert (shown["configured"], shown["linked"], shown["enabled"]) == (True, False, False)

    response = client.post("/v1/me/telegram/link", headers=owner)
    assert response.status_code == 201, response.text
    made = response.json()
    code = made["url"].rpartition("=")[2]
    assert made["bot"] == BOT and model.LINK_CODE.fullmatch(code)
    links = tables.telegram_links
    ((stored, lives),) = sql(
        hub_db, select(links.c.code_hash, extract("epoch", links.c.expires_at - links.c.created_at))
    )
    assert stored == model.code_hash(code) and code not in stored and lives == 600

    assert say(client, OWNER_CHAT, f"/start {code}") == "linked"
    (welcome,) = replies(bot.fake, OWNER_CHAT)
    assert welcome.startswith(f"Linked to https://hub.test as {OWNER}.")
    shown = status(client, owner)
    assert (shown["linked"], shown["enabled"], shown["username"], shown["bot"]) == (True, True, "owner_tg", BOT)
    ((config,),) = sql(hub_db, select(CHANNELS.c.config).where(CHANNELS.c.kind == "telegram"))
    assert config == {"chat_id": OWNER_CHAT, "user_id": OWNER_CHAT, "username": "owner_tg"}
    assert audit_rows(hub_db, "telegram") == [("telegram.link", f"telegram user:{OWNER}", OWNER, None)]

    # Used, unknown, expired, or from a group: the same one reply, whichever it was.
    assert say(client, OTHER_CHAT, f"/start {code}") == "link_failed"
    assert say(client, OTHER_CHAT, "/start " + "x" * 32) == "link_failed"
    expired = new_link(client, hub["other_web"])
    eleven_minutes_ago = {
        "created_at": func.now() - timedelta(minutes=11),
        "expires_at": func.now() - timedelta(minutes=1),
    }
    sql(hub_db, update(links).values(**eleven_minutes_ago).where(links.c.used_at.is_(None)))
    assert say(client, OTHER_CHAT, f"/start {expired}") == "link_failed"
    group_code = new_link(client, hub["other_web"])
    group = {"id": -5001, "type": "group"}
    sender = {"id": OTHER_CHAT, "is_bot": False, "first_name": "Other"}
    assert handled(client, text_update(next(updates), group, sender, f"/start {group_code}")) == "link_failed"
    failures = replies(bot.fake, OTHER_CHAT) + [p["text"] for p in bot.fake.sent() if p["chat_id"] == -5001]
    assert failures == [server_telegram.LINK_FAILED] * 4
    assert sql(hub_db, select(func.count()).select_from(CHANNELS)) == [(1,)]


def test_linking_again_replaces_the_link_and_a_chat_speaks_for_one_member(client, hub, hub_db, bot):
    linked(client, hub["owner_web"])
    linked(client, hub["owner_web"], OTHER_CHAT)  # the owner's other account: the first link goes
    assert sql(hub_db, select(CHANNELS.c.config["chat_id"].as_integer())) == [(OTHER_CHAT,)]
    linked(client, hub["other_web"], OTHER_CHAT)  # someone else links that chat: the owner's link to it goes
    found = sql(hub_db, select(tables.users.c.login).join_from(CHANNELS, tables.users))
    assert found == [(OTHER,)]


def test_a_member_unlinks_on_the_web_or_with_stop(client, hub, hub_db, bot):
    owner = hub["owner"]
    linked(client, hub["owner_web"])
    worker, run = started(client, hub)
    asked(client, worker, run["id"])  # a delivery waits for the chat
    assert telegram_deliveries(hub_db) == [("pending", 0, None, None)]
    response = client.delete("/v1/me/telegram", headers=owner)
    assert response.status_code == 200, response.text
    assert response.json()["linked"] is False
    assert telegram_deliveries(hub_db) == [] and sql(hub_db, select(func.count()).select_from(CHANNELS)) == [(0,)]

    linked(client, hub["owner_web"])
    assert say(client, OWNER_CHAT, "/stop") == "unlinked"
    assert status(client, owner)["linked"] is False
    assert replies(bot.fake, OWNER_CHAT)[-1].startswith("This chat is unlinked from the hub")
    actions = [(row[0], row[1]) for row in audit_rows(hub_db, "telegram")]
    assert actions == [
        ("telegram.link", f"telegram user:{OWNER}"),
        ("telegram.unlink", f"telegram user:{OWNER} by=web"),
        ("telegram.link", f"telegram user:{OWNER}"),
        ("telegram.unlink", f"telegram user:{OWNER} by=bot"),
    ]


# The webhook


def test_the_webhook_checks_the_secret_token_before_it_reads_anything(client, hub, hub_db, bot):
    code = new_link(client, hub["owner_web"])
    chat, sender = private_chat(OWNER_CHAT)
    update = text_update(next(updates), chat, sender, f"/start {code}")
    for secret in (None, "", "wrong-secret", SECRET + "x"):
        refused = webhook(client, update, secret)
        assert refused.status_code == 403, refused.text
        assert SECRET not in refused.text
    assert bot.fake.calls[-1][0] == "getMe" and status(client, hub["owner"])["linked"] is False
    garbled = client.post("/v1/telegram/webhook", content=b"not json", headers={SECRET_HEADER: SECRET})
    assert garbled.status_code == 200 and garbled.json()["outcome"] == "ignored"
    assert handled(client, {"update_id": 1, "edited_message": {}}) == "ignored"
    for broken in ({"callback_query": {"message": "x"}}, {"message": {"chat": "x"}}, {"message": "x"}):
        assert handled(client, {"update_id": 2, **broken}) == "ignored"
    assert say(client, OWNER_CHAT, "hello") == "help"
    assert replies(bot.fake, OWNER_CHAT) == [server_telegram.HELP]


def test_the_webhook_takes_at_most_5_updates_from_one_chat_in_10_seconds(client, hub, bot):
    client.app.state.telegram_inbound = server_telegram.InboundLimit()
    outcomes = [say(client, OWNER_CHAT, "hello") for _ in range(6)]
    assert outcomes == ["help"] * 5 + ["dropped"]
    assert say(client, OTHER_CHAT, "hello") == "help"  # another chat has its own count
    limit = server_telegram.InboundLimit(updates=2, seconds=10)
    assert [limit.allow(1, now) for now in (0.0, 1.0, 2.0, 10.5, 10.8, 11.5)] == [True, True, False, True, False, True]


def test_without_its_variables_the_channel_is_off_and_the_hub_runs(hub_db, tmp_path, github, bot):
    config = live.hub_config(hub_db, tmp_path, github, **live.web_changes(github), telegram_webhook_secret=SECRET)
    assert config.telegram_missing() == ["EVO_HUB_TELEGRAM_BOT_TOKEN"]
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        headers = members(client, github)
        push(client, headers["owner"], plan_body())
        assert client.get("/v1/health/live").status_code == 200
        assert status(client, headers["owner"])["configured"] is False
        refused = client.post("/v1/me/telegram/link", headers=headers["owner"])
        assert refused.status_code == 503 and "not set up" in refused.text
        assert webhook(client, {"update_id": 1}).status_code == 404
        # A channel linked while the bot was set up fails at once, and the web still gets the notification.
        _, session = live.web_session(hub_db, OWNER)
        channel = {"user_id": user_id(hub_db, OWNER), "kind": "telegram", "config": {"chat_id": 1}, "token_id": session}
        sql(hub_db, insert(CHANNELS).values(**channel))
        worker, run = started(client, headers)
        asked(client, worker, run["id"])
        assert deliver(client) == {"delivered": 1, "retried": 0, "failed": 1}
        ((state, attempts, _, error),) = telegram_deliveries(hub_db)
        assert (state, attempts) == ("failed", 1) and error.startswith("Undeliverable: Telegram is not set up")
    assert bot.fake.calls == []


def user_id(db, login: str) -> int:
    return sql(db, select(tables.users.c.id).where(tables.users.c.login == login))[0][0]


# Decisions


def sent_decision(client, hub, bot) -> SimpleNamespace:
    """The owner's chat linked, a plan run of theirs that asked a decision, and the message it went out as."""
    linked(client, hub["owner_web"])
    worker, run = started(client, hub)
    decision = asked(client, worker, run["id"])
    assert deliver(client) == {"delivered": 2, "retried": 0, "failed": 0}
    message = bot.fake.sent()[-1]
    return SimpleNamespace(worker=worker, run=run, decision=decision, message=message)


def test_a_decision_goes_out_with_a_button_per_option_and_one_for_the_web(client, hub, hub_db, bot):
    sent = sent_decision(client, hub, bot)
    message, decision_id = sent.message, sent.decision["id"]
    assert message["chat_id"] == OWNER_CHAT and message["parse_mode"] == "HTML"
    assert QUESTION in message["text"] and f"Run #{sent.run['id']} of {PROJECT} asks a decision" in message["text"]
    assert "## Why" not in message["text"]  # the context stays on the hub
    assert message["reply_markup"]["inline_keyboard"] == [
        [{"text": "Keep SQLite", "callback_data": f"d:{decision_id}:sqlite"}],
        [{"text": "Move to Postgres (recommended)", "callback_data": f"d:{decision_id}:postgres"}],
        [{"text": "Open on the hub", "url": f"https://hub.test/inbox?decision={decision_id}"}],
    ]
    assert telegram_deliveries(hub_db) == [("delivered", 1, f"{OWNER_CHAT}:{message['message_id']}", None)]
    assert TOKEN not in str(sql(hub_db, select(DELIVERIES.c.last_error, DELIVERIES.c.external_id)))


def test_a_button_answers_the_decision_through_the_same_code_as_the_web(client, hub, hub_db, bot):
    sent = sent_decision(client, hub, bot)
    decision_id = sent.decision["id"]
    assert press(client, OWNER_CHAT, f"d:{decision_id}:postgres", sent.message) == "answered"
    shown = client.get(f"/v1/projects/{PROJECT}/decisions/{decision_id}", headers=hub["owner"]).json()
    assert (shown["state"], shown["answer_option"], shown["answered_by"]) == ("answered", "postgres", OWNER)
    assert shown["answer_run_id"] == sent.run["id"]
    inbox = tables.run_inbox
    ((body,),) = sql(hub_db, select(inbox.c.body).where(inbox.c.decision_id == decision_id))
    assert body.startswith(f"Answer to decision #{decision_id} (architecture)") and "postgres, Move to Postgres" in body
    (row,) = [row for row in audit_rows(hub_db, "decision")]
    assert row[1].endswith("option=postgres via=telegram") and row[2] == OWNER
    a = tables.audit
    assert sql(hub_db, select(a.c.token_id).where(a.c.action == "decision.answer")) == [(None,)]
    (toast,) = bot.fake.sent("answerCallbackQuery")
    assert toast["text"] == f"Answered by {OWNER}: Move to Postgres"
    (edited,) = bot.fake.sent("editMessageText")
    assert edited["message_id"] == sent.message["message_id"]
    assert edited["text"].endswith(f"<b>Answered by {OWNER}: Move to Postgres</b>")
    assert edited["reply_markup"] == {
        "inline_keyboard": [[{"text": "Open on the hub", "url": f"https://hub.test/inbox?decision={decision_id}"}]]
    }

    # A second press finds it answered: the button says so, and the message drops its options.
    assert press(client, OWNER_CHAT, f"d:{decision_id}:sqlite", sent.message) == "refused"
    toast = bot.fake.sent("answerCallbackQuery")[-1]["text"]
    assert f"decision {decision_id} is answered, not open" in toast
    assert bot.fake.sent("editMessageText")[-1]["text"].endswith(
        "<b>This is no longer open: it was answered already, or it closed.</b>"
    )


def test_a_reply_to_the_decisions_message_answers_it_in_words(client, hub, hub_db, bot):
    sent = sent_decision(client, hub, bot)
    decision_id = sent.decision["id"]
    assert say(client, OWNER_CHAT, ANSWER_TEXT, reply_to=sent.message["message_id"]) == "replied"
    shown = client.get(f"/v1/projects/{PROJECT}/decisions/{decision_id}", headers=hub["owner"]).json()
    assert (shown["state"], shown["answer_option"], shown["answer_text"]) == ("answered", None, ANSWER_TEXT)
    assert replies(bot.fake, OWNER_CHAT)[-1] == f"Your answer to decision #{decision_id} went to run #{sent.run['id']}."
    assert say(client, OWNER_CHAT, "again", reply_to=sent.message["message_id"]) == "replied"
    assert "is answered, not open" in replies(bot.fake, OWNER_CHAT)[-1]
    assert say(client, OWNER_CHAT, "to nothing", reply_to=424242) == "replied"
    assert replies(bot.fake, OWNER_CHAT)[-1].startswith("Only a decision takes an answer in words")


def test_only_the_runs_owner_answers_and_an_unlinked_chat_answers_nothing(client, hub, hub_db, bot):
    sent = sent_decision(client, hub, bot)
    decision_id = sent.decision["id"]
    linked(client, hub["other_web"], OTHER_CHAT)
    assert press(client, OTHER_CHAT, f"d:{decision_id}:sqlite", sent.message) == "refused"
    assert (
        bot.fake.sent("answerCallbackQuery")[-1]["text"]
        == f"only {OWNER}, who dispatched its run, may answer decision {decision_id}"
    )
    assert press(client, 9999, f"d:{decision_id}:sqlite", sent.message) == "refused"
    assert bot.fake.sent("answerCallbackQuery")[-1]["text"].startswith("This chat is not linked to the hub")
    assert press(client, OWNER_CHAT, f"d:{decision_id}:nope", sent.message) == "refused"
    assert "has no option 'nope'" in bot.fake.sent("answerCallbackQuery")[-1]["text"]
    assert press(client, OWNER_CHAT, "not the hub's", sent.message) == "refused"
    shown = client.get(f"/v1/projects/{PROJECT}/decisions/{decision_id}", headers=hub["owner"]).json()
    assert shown["state"] == "open" and bot.fake.sent("editMessageText") == []
    assert audit_rows(hub_db, "decision") == []


def test_a_restricted_project_gets_its_name_the_kind_and_the_link_only(client, hub, hub_db, bot):
    sinks = tables.project_sinks
    sql(
        hub_db,
        update(sinks).values(clearance={"level": "internal", "location": "domestic-only"}).where(sinks.c.kind == "hub"),
    )
    sent = sent_decision(client, hub, bot)
    decision_id = sent.decision["id"]
    url = f"https://hub.test/inbox?decision={decision_id}"
    assert sent.message["text"] == f"<b>{PROJECT}</b>: A decision waits for your answer.\n\n{url}"
    assert sent.message["reply_markup"] == {"inline_keyboard": [[{"text": "Open on the hub", "url": url}]]}
    for word in (QUESTION, OPTIONS[0]["label"], OPTIONS[1]["label"], "fleet", "Why"):
        assert word not in sent.message["text"]


# Proposals


def test_a_tier_2_proposal_takes_accept_reject_and_defer_from_telegram(review, bot):  # noqa: F811
    client, worker, db = review.client, review.worker, review.db
    unrestricted(db)
    linked(client, web(client, db, OWNER))
    run_id = held_review(review)
    made = proposed(client, worker, run_id, kind="feature", title="A wait helper instead of sleep and tail")
    assert made["tier"] == 2
    for state in ("verifying", "done"):
        assert report(client, worker, run_id, state).status_code == 200
    assert deliver(client)["failed"] == 0
    (message,) = [payload for payload in bot.fake.sent() if "Proposal #" in payload["text"]]
    assert "A wait helper instead of sleep and tail" in message["text"] and "tier 2, kind feature" in message["text"]
    assert "evo_agents/worker/wait.py" not in message["text"]  # its paths stay on the hub
    pid = made["id"]
    assert message["reply_markup"]["inline_keyboard"] == [
        [
            {"text": "Accept", "callback_data": f"p:{pid}:accept"},
            {"text": "Reject", "callback_data": f"p:{pid}:reject"},
            {"text": "Defer 7 days", "callback_data": f"p:{pid}:defer"},
        ],
        [{"text": "Open on the hub", "url": f"https://hub.test/inbox?proposal={pid}"}],
    ]
    assert press(client, OWNER_CHAT, f"p:{pid}:defer", message) == "answered"
    p = tables.proposals
    ((state, days),) = sql(
        db, select(p.c.state, extract("day", p.c.deferred_until - p.c.answered_at)).where(p.c.id == pid)
    )
    assert (state, days) == ("deferred", 7)
    assert bot.fake.sent("answerCallbackQuery")[-1]["text"] == f"Proposal #{pid} deferred by {OWNER}"
    a = tables.audit
    targets = sql(db, select(a.c.target, a.c.token_id).where(a.c.action == "curator.proposal"))
    assert targets == [(f"{PROJECT} proposal:{pid} run:{run_id} answer=defer via=telegram", None)]
    assert press(client, OWNER_CHAT, f"p:{pid}:accept", message) == "answered"  # a deferred one is answered again
    assert sql(db, select(p.c.state).where(p.c.id == pid)) == [("accepted",)]
    assert press(client, OWNER_CHAT, f"p:{pid}:reject", message) == "refused"
    assert "is accepted: only an open or deferred one is answered" in bot.fake.sent("answerCallbackQuery")[-1]["text"]


# Pace, 429 and a blocked bot


def test_the_channel_keeps_to_the_bot_faqs_pace_and_waits_out_a_429(client, hub, hub_db, bot):
    linked(client, hub["owner_web"])
    worker, run = started(client, hub)
    asked(client, worker, run["id"])
    asked(client, worker, run["id"], question="And the cache?")
    bot.fake.fail_next(429, "Too Many Requests: retry after 7", retry_after=7)
    assert deliver(client) == {"delivered": 3, "retried": 1, "failed": 0}
    first, second = telegram_deliveries(hub_db)
    assert first[:3] == ("pending", 0, None) and "429" in first[3]  # not a failed try
    assert second[0] == "delivered"
    waiting = sql(
        hub_db,
        select(extract("epoch", DELIVERIES.c.next_at - func.now())).where(DELIVERIES.c.state == "pending"),
    )
    assert 5 < waiting[0][0] <= 7
    assert bot.waits == [] or all(0 < wait <= 1.0 for wait in bot.waits)

    sql(hub_db, update(DELIVERIES).values(next_at=func.now()).where(DELIVERIES.c.state == "pending"))
    assert deliver(client) == {"delivered": 1, "retried": 0, "failed": 0}
    # Two messages to one chat in one pass: the second waits for the first's second to pass.
    asked(client, worker, run["id"], question="And the queue?")
    asked(client, worker, run["id"], question="And the logs?")
    bot.waits.clear()
    deliver(client)
    assert len(bot.waits) == 1 and 0.5 < bot.waits[0] <= 1.0


def test_a_blocked_bot_turns_the_channel_off_with_its_reason(client, hub, hub_db, bot):
    linked(client, hub["owner_web"])
    worker, run = started(client, hub)
    asked(client, worker, run["id"])
    bot.fake.fail_next(403, "Forbidden: bot was blocked by the user")
    assert deliver(client) == {"delivered": 1, "retried": 0, "failed": 1}
    ((state, attempts, _, error),) = telegram_deliveries(hub_db)
    assert (state, attempts) == ("failed", 1) and "blocked by the user" in error
    shown = status(client, hub["owner"])
    assert (shown["linked"], shown["enabled"]) == (True, False)
    assert "blocked by the user" in shown["disabled_reason"]
    asked(client, worker, run["id"], question="Still there?")
    assert telegram_deliveries(hub_db)[1:] == []  # a channel turned off gets no new delivery
    linked(client, hub["owner_web"])
    assert status(client, hub["owner"])["enabled"] is True


def test_telegram_that_cannot_be_reached_leaves_no_token_in_the_database(client, hub, hub_db, bot, monkeypatch):
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    linked(client, hub["owner_web"])
    monkeypatch.setattr(server_telegram, "TRANSPORT", httpx.MockTransport(unreachable))
    worker, run = started(client, hub)
    asked(client, worker, run["id"])
    assert deliver(client)["retried"] == 1
    ((state, attempts, _, error),) = telegram_deliveries(hub_db)
    assert (state, attempts) == ("pending", 1) and error == "BotApiError: Telegram could not be reached (ConnectError)"
    assert TOKEN.partition(":")[2] not in error


# The hub admin's view of the bot


def test_a_hub_admin_points_the_webhook_at_the_hub_with_the_secret(client, hub, hub_db, bot):
    assert client.get("/v1/admin/telegram", headers=hub["owner"]).status_code == 403
    shown = client.get("/v1/admin/telegram", headers=hub["admin"]).json()
    assert shown == {
        "configured": True,
        "bot": BOT,
        "expected_url": "https://hub.test/v1/telegram/webhook",
        "url": None,
        "pending_update_count": 0,
        "last_error_date": None,
        "last_error_message": None,
    }
    assert client.post("/v1/admin/telegram/webhook", headers=hub["owner"]).status_code == 403
    response = client.post("/v1/admin/telegram/webhook", headers=hub["admin"])
    assert response.status_code == 200, response.text
    assert response.json()["url"] == "https://hub.test/v1/telegram/webhook"
    (registered,) = bot.fake.sent("setWebhook")
    assert registered == {
        "url": "https://hub.test/v1/telegram/webhook",
        "secret_token": SECRET,
        "allowed_updates": ["message", "callback_query"],
    }
    assert [row[:2] for row in audit_rows(hub_db, "telegram")] == [
        ("telegram.webhook", "https://hub.test/v1/telegram/webhook")
    ]
    assert SECRET not in str(audit_rows(hub_db, "telegram"))


# Failing closed: what the hub's review of this channel found, each with the check that keeps it fixed


def minimal(decision_id: int) -> str:
    return f"<b>{PROJECT}</b>: A decision waits for your answer.\n\nhttps://hub.test/inbox?decision={decision_id}"


def test_a_hub_sink_above_customer_without_a_level_or_missing_gets_the_project_and_the_link_only(
    client, hub, hub_db, bot
):
    linked(client, hub["owner_web"])
    worker, run = started(client, hub)
    sinks = tables.project_sinks
    cleared = (
        {"level": "secret", "location": "any"},  # above customer on the ladder
        {"location": "any"},  # no level
        {"level": "top-secret", "location": "any"},  # a level the ladder lacks
        {"level": "internal"},  # no location: it may receive any, domestic-only included
        None,  # no hub sink at all
    )
    for index, clearance in enumerate(cleared):
        if clearance is None:
            sql(hub_db, delete(sinks).where(sinks.c.kind == "hub"))
        else:
            unrestricted(hub_db, clearance)
        made = asked(client, worker, run["id"], question=f"{QUESTION} ({index})")
        assert deliver(client)["failed"] == 0
        message = bot.fake.sent()[-1]
        url = f"https://hub.test/inbox?decision={made['id']}"
        assert message["text"] == minimal(made["id"]), clearance
        assert message["reply_markup"] == {"inline_keyboard": [[{"text": "Open on the hub", "url": url}]]}


def test_a_decision_answered_before_its_message_goes_out_sends_nothing_it_asked(client, hub, hub_db, bot):
    linked(client, hub["owner_web"])
    worker, run = started(client, hub)
    made = asked(client, worker, run["id"])
    answered_decision(client, hub["owner"], made["id"], option="postgres")
    assert deliver(client)["failed"] == 0
    message = bot.fake.sent()[-1]
    url = f"https://hub.test/inbox?decision={made['id']}"
    assert message["text"] == f"<b>{PROJECT}</b>: Decision #{made['id']} no longer waits for your answer.\n\n{url}"
    assert message["reply_markup"] == {"inline_keyboard": [[{"text": "Open on the hub", "url": url}]]}


def test_a_proposal_answered_before_its_message_goes_out_never_sends_its_summary(review, bot):  # noqa: F811
    client, worker, db, headers = review.client, review.worker, review.db, review.headers
    unrestricted(db)
    linked(client, web(client, db, OWNER))
    run_id = held_review(review)
    summary = "The **sessions** of the night waited on `sleep 30 && tail` in evo_agents/worker/wait.py, nine times."
    made = proposed(client, worker, run_id, kind="feature", title="A wait helper instead of sleep", summary=summary)
    for state in ("verifying", "done"):
        assert report(client, worker, run_id, state).status_code == 200
    answered_proposal(client, headers["owner"], made["id"], "reject")
    assert deliver(client)["failed"] == 0
    (message,) = [payload for payload in bot.fake.sent() if f"Proposal #{made['id']}" in payload["text"]]
    url = f"https://hub.test/inbox?proposal={made['id']}"
    assert message["text"] == f"<b>{PROJECT}</b>: Proposal #{made['id']} no longer waits for your answer.\n\n{url}"
    for word in ("sessions", "sleep", "wait.py", "nine times", "A wait helper"):
        assert word not in message["text"]


def test_a_member_whose_grant_went_gets_nothing_of_that_project_on_telegram(client, hub, hub_db, bot):
    linked(client, hub["owner_web"])
    worker, run = started(client, hub)
    asked(client, worker, run["id"])
    sent_before = len(bot.fake.sent())
    taken = client.delete(f"/v1/admin/projects/{PROJECT}/grants/{OWNER}", headers=hub["admin"])
    assert taken.status_code == 204, taken.text
    assert deliver(client) == {"delivered": 1, "retried": 0, "failed": 1}  # the web's, which the grant hides
    assert len(bot.fake.sent()) == sent_before
    ((state, attempts, external_id, error),) = telegram_deliveries(hub_db)
    assert (state, attempts, external_id, error) == ("failed", 1, None, notifications.NO_GRANT)
    assert status(client, hub["owner"])["enabled"] is True  # the chat stays linked for the projects left


def test_a_chat_is_linked_from_a_web_session_only(client, hub, hub_db, bot):
    refused = client.post("/v1/me/telegram/link", headers=hub["owner"])  # a machine token
    assert refused.status_code == 403 and "linked from a web session only" in refused.text
    assert sql(hub_db, select(func.count()).select_from(tables.telegram_links)) == [(0,)]
    # A code whose session signed out before Start links nothing.
    session = web(client, hub_db, OWNER)
    code = new_link(client, session)
    assert client.post("/v1/auth/logout", headers=session).status_code == 204
    assert say(client, OWNER_CHAT, f"/start {code}") == "link_failed"
    assert status(client, hub["owner"])["linked"] is False
    # From a live session it links, bound to that session.
    token, session_id = live.web_session(hub_db, OWNER)
    linked(client, session_headers(client, token))
    assert sql(hub_db, select(CHANNELS.c.token_id)) == [(session_id,)]
    links = tables.telegram_links
    assert sql(hub_db, select(links.c.token_id).where(links.c.used_at.is_not(None))) == [(session_id,)]


def test_a_chat_lives_no_longer_than_the_web_session_that_linked_it(client, hub, hub_db, bot):
    sent = sent_decision(client, hub, bot)
    decision_id = sent.decision["id"]
    assert client.post("/v1/auth/logout", headers=hub["owner_web"]).status_code == 204
    shown = status(client, hub["owner"])
    assert (shown["linked"], shown["enabled"], shown["disabled_reason"]) == (True, False, notifications.SESSION_ENDED)
    # Its buttons and replies answer nothing more.
    assert press(client, OWNER_CHAT, f"d:{decision_id}:postgres", sent.message) == "refused"
    assert bot.fake.sent("answerCallbackQuery")[-1]["text"].startswith("This chat is not linked to the hub")
    assert say(client, OWNER_CHAT, ANSWER_TEXT, reply_to=sent.message["message_id"]) == "replied"
    assert replies(bot.fake, OWNER_CHAT)[-1].startswith("This chat is not linked to the hub")
    assert decision(client, hub["owner"], decision_id)["state"] == "open"
    # It gets nothing more, and the channel turns off with the reason.
    sent_before = len(bot.fake.sent())
    asked(client, sent.worker, sent.run["id"], question="After the sign-out?")
    assert deliver(client) == {"delivered": 1, "retried": 0, "failed": 1}
    assert len(bot.fake.sent()) == sent_before
    assert telegram_deliveries(hub_db)[-1][::3] == ("failed", notifications.SESSION_ENDED)
    ((enabled, config),) = sql(hub_db, select(CHANNELS.c.enabled, CHANNELS.c.config))
    assert enabled is False and config["disabled_reason"] == notifications.SESSION_ENDED
    # /stop still unlinks it, whatever became of the session.
    assert say(client, OWNER_CHAT, "/stop") == "unlinked"
    assert status(client, hub["owner"])["linked"] is False
    # A session that expires, or that a hub admin revokes, ends the chat as a sign-out does.
    tokens = tables.tokens
    for end in ("expired", "revoked"):
        token, session_id = live.web_session(hub_db, OWNER)
        linked(client, session_headers(client, token))
        assert status(client, hub["owner"])["enabled"] is True
        if end == "expired":
            day = timedelta(days=1)
            ended = {"created_at": func.now() - 100 * day, "last_used_at": None, "expires_at": func.now() - day}
            sql(hub_db, update(tokens).values(**ended).where(tokens.c.id == session_id))
        else:
            assert client.delete(f"/v1/admin/tokens/{session_id}", headers=hub["admin"]).status_code == 204
        assert status(client, hub["owner"])["disabled_reason"] == notifications.SESSION_ENDED, end


def test_a_hub_admin_unlinks_any_members_chat(client, hub, hub_db, bot):
    linked(client, hub["owner_web"])
    path = f"/v1/admin/users/{OWNER}/telegram"
    for member in (hub["owner"], hub["owner_web"], hub["other"]):
        assert client.delete(path, headers=member).status_code == 403
    assert client.delete("/v1/admin/users/nobody-here/telegram", headers=hub["admin"]).status_code == 404
    response = client.delete(path, headers=hub["admin"])
    assert response.status_code == 200, response.text
    assert response.json() == {"login": OWNER, "unlinked": True}
    assert status(client, hub["owner"])["linked"] is False
    again = client.delete(f"/v1/admin/users/{OWNER.upper()}/telegram", headers=hub["admin"])  # a login in any case
    assert again.json() == {"login": OWNER, "unlinked": False}
    assert audit_rows(hub_db, "telegram")[-1][:3] == ("telegram.unlink", f"telegram user:{OWNER} by=admin", live.ADMIN)


def test_a_hub_admin_unlinks_a_members_chat_from_the_command_line(hub_db, tmp_path, github, monkeypatch, capsys):
    app = create_app(live.hub_config(hub_db, tmp_path, github))
    with serving(app) as url, httpx.Client(base_url=url, timeout=10) as http:
        headers = members(http, github)
        _, session = live.web_session(hub_db, OWNER)
        channel = {"user_id": user_id(hub_db, OWNER), "kind": "telegram", "config": {"chat_id": 1}, "token_id": session}
        sql(hub_db, insert(CHANNELS).values(**channel))
        token = headers["admin"]["Authorization"].removeprefix("Bearer ")
        home = write_credentials(tmp_path / "admin", url, live.ADMIN, token)
        shown = as_json(cli(monkeypatch, capsys, home, "hub", "admin", "telegram-unlink", OWNER, "--json"))
        assert_json_keys("hub admin telegram-unlink", shown)
        assert shown == {"login": OWNER, "unlinked": True}
        printed = ok(cli(monkeypatch, capsys, home, "hub", "admin", "telegram-unlink", OWNER)).out
        assert printed.strip() == f"{OWNER} has no Telegram chat linked."
        member = write_credentials(tmp_path / "owner", url, OWNER, headers["owner"]["Authorization"].split()[1])
        refused = cli(monkeypatch, capsys, member, "hub", "admin", "telegram-unlink", OWNER)
        assert refused.code != 0 and "hub admin" in refused.err


def test_a_button_of_a_project_the_member_holds_no_grant_on_says_nothing_of_it(client, hub, hub_db, bot):
    sent = sent_decision(client, hub, bot)
    decision_id = sent.decision["id"]
    for login, chat in ((STRANGER, 7003), (live.ADMIN, 7004)):  # no grant at all; a hub admin without one
        linked(client, web(client, hub_db, login), chat)
        for data in (
            f"d:{decision_id}:postgres",
            f"d:{decision_id}:nope",
            f"d:{10**12}:postgres",
            f"p:{decision_id}:accept",
        ):
            assert press(client, chat, data, sent.message) == "refused"
            toast = bot.fake.sent("answerCallbackQuery")[-1]["text"]
            assert toast == server_telegram.NOT_YOURS, (login, data)
            assert PROJECT not in toast and "postgres" not in toast and "sqlite" not in toast
    for data in (f"d:{decision_id}:-postgres", f"d:{decision_id}:_x"):  # not an option key: refused, not a 500
        assert press(client, OWNER_CHAT, data, sent.message) == "refused"
        assert bot.fake.sent("answerCallbackQuery")[-1]["text"] == server_telegram.NOT_THE_HUBS
    assert decision(client, hub["owner"], decision_id)["state"] == "open"
    assert bot.fake.sent("editMessageText") == [] and audit_rows(hub_db, "decision") == []


def test_a_restricted_project_takes_no_answer_from_telegram(client, hub, hub_db, bot):
    sent = sent_decision(client, hub, bot)  # sent while the project was not restricted
    decision_id = sent.decision["id"]
    unrestricted(hub_db, {"level": "customer", "location": "any"})  # it is now
    assert say(client, OWNER_CHAT, ANSWER_TEXT, reply_to=sent.message["message_id"]) == "replied"
    expected = f"The hub did not take that answer: {server_telegram.RESTRICTED_ANSWER}"
    assert replies(bot.fake, OWNER_CHAT)[-1] == expected
    assert press(client, OWNER_CHAT, f"d:{decision_id}:postgres", sent.message) == "refused"
    assert bot.fake.sent("answerCallbackQuery")[-1]["text"] == server_telegram.RESTRICTED_ANSWER
    assert decision(client, hub["owner"], decision_id)["state"] == "open"
    assert bot.fake.sent("editMessageText") == [] and audit_rows(hub_db, "decision") == []


def test_a_reply_finds_the_decision_of_its_own_chat_only(client, hub, hub_db, bot):
    sent = sent_decision(client, hub, bot)
    decision_id = sent.decision["id"]
    linked(client, hub["owner_web"], OTHER_CHAT)  # the owner's other chat, whose message ids are its own
    assert say(client, OTHER_CHAT, ANSWER_TEXT, reply_to=sent.message["message_id"]) == "replied"
    assert replies(bot.fake, OTHER_CHAT)[-1].startswith("Only a decision takes an answer in words")
    assert decision(client, hub["owner"], decision_id)["state"] == "open"
