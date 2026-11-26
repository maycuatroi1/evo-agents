"""Decisions on the channels of core plugins, by their label, through the sample core plugin northwind-chat
(``tests/fixtures/core_plugins``): a decision with a label is judged by it against its project's sink of kind channel
named as the channel, and one kept off goes as the link alone, saying why, and takes no answer from the channel; a
decision without a label keeps the rule of the project's hub sink. Every view shows where its message stands on each
channel (``channel_deliveries``); a decision's delivery fails once DECISION_WINDOW passed, RetryAfter included; what
reaches a channel is redacted; a decision answered, cancelled or expired gets the edit of each message through the
outbox, tried again when it fails; an answer from a channel records its channel and label, and a second one gets 409.
``docs/notifications.md`` (Decisions on channels) is the design.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import importlib

from sqlalchemy import func, insert, select, update

from evo_agents.hub import tables
from evo_agents.hub.server.notifications.models import LABELLED_ANSWER
from tests.hub import live
from tests.hub.conftest import github, hub_db, hub_template, pg_server  # noqa: F401 (fixtures)
from tests.hub.live import sql
from tests.hub.test_curator import night  # noqa: F401 (a fixture, on web_client)
from tests.hub.test_decisions import QUESTION, asked
from tests.hub.test_plan_runs import started
from tests.hub.test_runs import OWNER, PROJECT, audit_rows, control, moved
from tests.plugins.fake_northwind import button, text
from tests.plugins.test_core_plugin_api import (  # noqa: F401 (fixtures)
    KIND,
    LINK,
    ORIGIN,
    deliver,
    fake,
    northwind,
    restrict,
    said,
    web,
    web_client,
)

AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"  # the example key of the AWS docs, split so no scan reads it as a key


def channel_sink(db, level: str) -> None:
    """The project's sink of kind channel named northwind, clearing ``level`` for any location."""
    p, ps = tables.projects, tables.project_sinks
    ((project_id,),) = sql(db, select(p.c.id).where(p.c.name == PROJECT))
    clearance = {"level": level, "location": "any"}
    sql(db, insert(ps).values(project_id=project_id, sink_id=KIND, kind="channel", clearance=clearance))


def ask_labelled(northwind, **extra):
    worker, run = started(northwind.client, northwind.headers)
    return worker, run, asked(northwind.client, worker, run["id"], **extra)


def shown(northwind, decision_id: int) -> dict:
    url = f"/v1/projects/{PROJECT}/decisions/{decision_id}"
    response = northwind.client.get(url, headers=northwind.headers["owner"])
    assert response.status_code == 200, response.text
    return response.json()


def ours(northwind, decision_id: int) -> dict:
    (entry,) = [item for item in shown(northwind, decision_id)["channel_deliveries"] if item["channel"] == KIND]
    return entry


@pytest.fixture
def prepared(monkeypatch):
    """Every Outgoing northwind-chat's class sends, as ``hub.prepare`` left it."""
    server = importlib.import_module("northwind_chat.server")
    seen: list = []
    message = server.message

    def recording(notification, url):
        seen.append(notification)
        return message(notification, url)

    monkeypatch.setattr(server, "message", recording)
    return seen


def deliveries(db) -> list[tuple]:
    dl = tables.notification_deliveries
    query = select(dl.c.id, dl.c.edit_of, dl.c.state, dl.c.withheld, dl.c.settled_at.is_not(None))
    return sql(db, query.where(dl.c.channel_id.is_not(None)).order_by(dl.c.id))


# The label against the channel's sink


def test_a1_a_label_the_channels_sink_clears_goes_with_its_content_where_the_hub_sink_would_keep_it(
    northwind, prepared
):
    restrict(northwind.db, "customer")  # the hub sink's rule alone would keep it off
    channel_sink(northwind.db, "internal")
    _, _, decision = ask_labelled(northwind, label="internal")
    assert deliver(northwind.client)["failed"] == 0
    assert northwind.fake.last()["text"] == f"Decision #{decision['id']}: {QUESTION}"
    extra = prepared[-1].extra
    assert (extra["restricted"], extra["label"], extra["label_given"]) == (False, "internal", True)
    assert (extra["decision"]["label"], extra["decision"]["label_given"]) == ("internal", True)
    entry = ours(northwind, decision["id"])
    assert (entry["state"], entry["withheld"], entry["reason"]) == ("sent", None, None)
    assert entry["external_id"] == northwind.fake.last()["id"]


def test_a3_a_label_above_the_channels_sink_goes_as_the_link_and_the_channel_answers_nothing(northwind, prepared):
    channel_sink(northwind.db, "public")
    _, _, decision = ask_labelled(northwind, label="internal")
    decision_id = decision["id"]
    assert deliver(northwind.client)["failed"] == 0
    assert (
        northwind.fake.last()["text"] == f"Something of {PROJECT} waits for you: {ORIGIN}/inbox?decision={decision_id}"
    )
    extra = prepared[-1].extra
    assert extra == {"restricted": True, "restricted_by": "label", "label": "internal", "label_given": True}
    entry = ours(northwind, decision_id)
    assert (entry["state"], entry["withheld"]) == ("sent", "label")
    assert entry["reason"] == f"its label internal keeps its content off {KIND}: the message links to the hub"
    assert said(northwind.client, button(f"d:{decision_id}:postgres")) == "refused"
    told = LABELLED_ANSWER.format(label="internal", decision_id=decision_id)
    assert told in northwind.fake.last()["text"]
    after = shown(northwind, decision_id)
    assert (after["state"], after["answered_via"]) == ("open", None)


def test_a3_a_labelled_decision_without_a_sink_of_the_channel_is_kept_off(northwind, prepared):
    _, _, decision = ask_labelled(northwind, label="public")
    assert deliver(northwind.client)["failed"] == 0
    assert prepared[-1].extra["restricted_by"] == "label"
    assert said(northwind.client, button(f"d:{decision['id']}:postgres")) == "refused"


def test_a6_a_decision_without_a_label_keeps_the_hub_sinks_rule(northwind, prepared):
    channel_sink(northwind.db, "public")  # read for labelled decisions alone
    worker, run, decision = ask_labelled(northwind)
    assert deliver(northwind.client)["failed"] == 0
    assert northwind.fake.last()["text"] == f"Decision #{decision['id']}: {QUESTION}"
    assert prepared[-1].extra["label_given"] is False
    restrict(northwind.db, "customer")
    kept = asked(northwind.client, worker, run["id"], step_key="3")
    assert deliver(northwind.client)["failed"] == 0
    assert prepared[-1].extra["restricted_by"] == "default_label"
    assert ours(northwind, kept["id"])["withheld"] == "default_label"
    assert said(northwind.client, button(f"d:{kept['id']}:postgres")) == "refused"
    assert "keeps its decisions and proposals off this channel" in northwind.fake.last()["text"]


# Where each message stands


def test_a4_a_channel_the_person_asked_has_not_linked_reads_as_not_sent_with_why(northwind):
    assert said(northwind.client, {"group_id": "g-100", "user_id": "u-200", "text": "/stop"}) == "unlinked"
    _, _, decision = ask_labelled(northwind)
    deliver(northwind.client)
    entry = ours(northwind, decision["id"])
    assert (entry["state"], entry["reason"]) == ("not_sent", f"{OWNER} has not linked {KIND}")


def test_a4_a5_a_decision_the_channel_keeps_refusing_fails_after_thirty_minutes_retry_after_included(northwind):
    client, db = northwind.client, northwind.db
    _, _, decision = ask_labelled(northwind)
    northwind.fake.refuse = 429
    assert deliver(client) == {"delivered": 1, "retried": 1, "failed": 0}
    entry = ours(northwind, decision["id"])
    assert (entry["state"], entry["error"]) == ("pending", "RetryAfter: Northwind Chat asked to wait")
    dl = tables.notification_deliveries
    back = func.now() - func.make_interval(0, 0, 0, 0, 0, 31)
    sql(db, update(dl).values(created_at=back, next_at=func.now()).where(dl.c.channel_id.is_not(None)))
    assert deliver(client) == {"delivered": 0, "retried": 0, "failed": 1}
    entry = ours(northwind, decision["id"])
    assert (entry["state"], entry["error"]) == ("failed", "RetryAfter: Northwind Chat asked to wait")


def test_a5_a_decision_the_channel_refuses_is_tried_again_no_later_than_its_window(northwind):
    client, db = northwind.client, northwind.db
    ask_labelled(northwind)
    northwind.fake.refuse = 500
    dl = tables.notification_deliveries
    back = func.now() - func.make_interval(0, 0, 0, 0, 0, 29, 30)
    sql(db, update(dl).values(created_at=back).where(dl.c.channel_id.is_not(None)))
    assert deliver(client)["retried"] == 1
    ((wait,),) = sql(db, select(dl.c.next_at - func.now()).where(dl.c.channel_id.is_not(None)))
    assert wait.total_seconds() <= 31  # the window's end, not BACKOFF_SECONDS


def test_a5_what_reaches_the_channel_is_redacted(northwind, prepared):
    _, _, decision = ask_labelled(
        northwind,
        question=f"Rotate the key {AWS_KEY} now?",
        context=f"It leaked: {AWS_KEY}",
    )
    assert deliver(northwind.client)["failed"] == 0
    assert AWS_KEY not in northwind.fake.last()["text"]
    sent = prepared[-1]
    assert AWS_KEY not in repr(sent.extra) and AWS_KEY not in (sent.title or "") + (sent.body or "")
    assert shown(northwind, decision["id"])["question"] == f"Rotate the key {AWS_KEY} now?"  # the hub keeps it


# Edits through the outbox


def test_a7_an_answer_from_the_channel_edits_its_message_records_channel_and_label_and_a_second_gets_409(
    northwind, prepared
):
    client, db = northwind.client, northwind.db
    _, _, decision = ask_labelled(northwind, label="internal")
    channel_sink(db, "internal")
    deliver(client)
    first = northwind.fake.last()
    decision_id = decision["id"]
    assert said(client, button(f"d:{decision_id}:postgres")) == "answered"
    after = shown(northwind, decision_id)
    assert (after["state"], after["answered_via"], after["label"]) == ("answered", KIND, "internal")
    (row,) = audit_rows(db, "decision")
    assert row[1].endswith(f"option=postgres label=internal via={KIND}")
    assert said(client, button(f"d:{decision_id}:sqlite")) == "refused"
    assert northwind.fake.last()["text"].endswith(
        f"decision {decision_id} is answered, not open: it takes no answer any more"
    )  # 409
    assert deliver(client) == {"delivered": 1, "retried": 0, "failed": 0}
    edit = prepared[-1].extra["edit"]
    assert edit == {"delivery_id": deliveries(db)[0][0], "external_id": first["id"], "state": "answered"}
    assert prepared[-1].extra["decision"]["answered_via"] == KIND
    (sent, edited) = deliveries(db)
    assert (sent[1], sent[4], edited[1], edited[2]) == (None, True, sent[0], "delivered")
    assert ours(northwind, decision_id)["edit"]["state"] == "sent"
    assert deliver(client) == {"delivered": 0, "retried": 0, "failed": 0}  # once


def test_a7_a_cancelled_decision_gets_its_edit_and_a_failed_edit_is_tried_again(northwind, prepared):
    client, db = northwind.client, northwind.db
    worker, run, decision = ask_labelled(northwind)
    deliver(client)
    assert control(client, northwind.headers["owner"], run["id"], "cancel").status_code == 200
    moved(client, worker, run["id"], "cancelled")
    assert shown(northwind, decision["id"])["state"] == "cancelled"
    northwind.fake.refuse = 500
    assert deliver(client) == {"delivered": 0, "retried": 1, "failed": 0}
    assert ours(northwind, decision["id"])["edit"]["state"] == "pending"
    northwind.fake.refuse = None
    dl = tables.notification_deliveries
    sql(db, update(dl).values(next_at=func.now()).where(dl.c.edit_of.is_not(None)))
    assert deliver(client) == {"delivered": 1, "retried": 0, "failed": 0}
    assert prepared[-1].extra["edit"]["state"] == "cancelled"
    assert northwind.fake.last()["text"].startswith(f"Decision #{decision['id']} is no longer open")
    assert ours(northwind, decision["id"])["edit"]["state"] == "sent"


def test_a7_through_a_channel_only_the_person_asked_answers(northwind):
    client, db = northwind.client, northwind.db
    _, _, decision = ask_labelled(northwind)
    deliver(client)
    grant = {"role": "admin", "max_level": "internal"}  # an admin of the project answers for its owner on the hub
    path = f"/v1/admin/projects/{PROJECT}/grants/{live.ADMIN}"
    assert client.put(path, json=grant, headers=northwind.headers["admin"]).status_code == 200
    session = web(client, db, live.ADMIN)
    assert said(client, text("/link", user_id="u-300")) == "link"
    url = northwind.fake.last(user_id="u-300")["text"].split()[1]
    query = dict(part.split("=", 1) for part in url.partition("?")[2].split("&"))
    assert client.post(LINK, json=query, headers=session.write).status_code == 201
    assert said(client, button(f"d:{decision['id']}:postgres", user_id="u-300")) == "refused"
    told = northwind.fake.last(user_id="u-300")["text"]
    assert f"only the person decision {decision['id']} is asked of answers it" in told
    assert shown(northwind, decision["id"])["state"] == "open"
