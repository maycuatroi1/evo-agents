"""Threads of comments anchored to a passage of an item (``evo_agents.hub.server.srs.threads``, migration 0227): a
passage found again in a later version or said to have changed, answers joining their thread, the notice srs_comment to
the people of a thread and the author of the item's revision, resolve and reopen by a member only, and @ba asked in a
comment answering in the thread. The sample is ``tests/hub/fixtures/srs/demo-shop-srs.md``: chi writes it."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from evo_agents.hub.server.srs import threads
from evo_agents.hub.server.srs.doc import Document, Item
from tests.hub.test_ba_agent import (  # noqa: F401 (fixtures)
    DOC,
    OTHER,
    PROJECT,
    agent_of,
    client,
    enable_ba,
    hub,
    imported,
    item,
    members,
    owners_worker,
    running,
    saved,
    srs_core,
    tool,
)
from tests.hub.test_credentials_api import app_key  # noqa: F401
from tests.hub.test_srs_notices import kind_of

PASSAGE = "holds at most 50 lines"


def said(client, headers, **body):
    answer = client.post(f"{DOC}/comments", json=body, headers=headers)
    assert answer.status_code == 201, answer.text
    return answer.json()


def listed(client, headers, **query) -> list[dict]:
    answer = client.get(f"{DOC}/comments", params=query, headers=headers)
    assert answer.status_code == 200, answer.text
    return answer.json()


def test_srs_threads_anchor_finds_a_passage_by_its_words_and_says_when_it_changed():
    doc = Document(
        items=[Item(kind="business_rule", title="Cart rules", body="- A cart **holds** at most 50 lines.", key="b")]
    )
    assert threads.anchor(doc, "b", "holds at  most 50 lines") == threads.FOUND
    assert threads.anchor(doc, "b", "holds at most 40 lines") == threads.CHANGED
    assert threads.anchor(doc, "gone", "anything") == threads.CHANGED
    assert threads.anchor(doc, "b", None) is None
    assert threads.plain("| A | [**B**](x) |") == "a b"


def test_srs_a_thread_on_a_passage_follows_it_into_later_versions_and_answers_join_it(client, hub):
    imported(client, hub["chi"])
    cart = item(client, hub["chi"], "BR ORD1-1")
    root = said(
        client,
        hub["other"],
        body="Why 50?",
        item="BR ORD1-1",
        quote=PASSAGE,
        field="body",
        prefix="A cart ",
        suffix=" and",
    )
    assert (root["version"], root["quote"], root["field"], root["anchor"]) == (1, PASSAGE, "body", "found")
    answer = said(client, hub["chi"], body="The warehouse limit", parent_id=root["id"])
    again = said(client, hub["other"], body="Thanks", parent_id=answer["id"])
    assert answer["parent_id"] == again["parent_id"] == root["id"], "an answer to an answer joins the root's thread"
    # the use case's threads hold those of the business rules under it
    shown = listed(client, hub["reader"], item="UC ORD-1")
    assert [c["id"] for c in shown] == [root["id"], answer["id"], again["id"]]
    assert listed(client, hub["reader"], item="UC ORD-2") == []
    # version 2 changes the passage: the latest says it changed, version 1 still holds it
    saved(client, hub["chi"], 1, {"key": cart["key"], "body": "- A cart holds at most 40 lines."})
    assert listed(client, hub["reader"], item="BR ORD1-1")[0]["anchor"] == "changed"
    assert listed(client, hub["reader"], item="BR ORD1-1", version=1)[0]["anchor"] == "found"
    refused = client.post(f"{DOC}/comments", json={"body": "x", "quote": PASSAGE}, headers=hub["other"])
    assert refused.status_code == 422 and "names the item" in refused.json()["message"], refused.text


def test_srs_a_comment_tells_the_people_of_its_thread_and_the_author_of_the_revision(client, hub):
    imported(client, hub["chi"])
    root = said(client, hub["other"], body="Why 50?", item="BR ORD1-1", quote=PASSAGE)
    (notice,) = kind_of(client, hub["chi"], threads.NOTICE)  # chi wrote the revision of UC ORD-1
    key = item(client, hub["chi"], "BR ORD1-1")["key"]
    assert notice["link"] == f"/p/{PROJECT}/x/srs/documents/demo-shop?s={key}&c={root['id']}"
    assert notice["title"] == f"{OTHER} commented on BR ORD1-1 in {PROJECT}"
    assert PASSAGE in notice["body"] and notice["details"]["thread"] == root["id"]
    assert kind_of(client, hub["other"], threads.NOTICE) == [], "never its own author"
    said(client, hub["chi"], body="Ask @owner", parent_id=root["id"])
    assert [n["details"]["thread"] for n in kind_of(client, hub["other"], threads.NOTICE)] == [root["id"]]
    assert len(kind_of(client, hub["owner"], threads.NOTICE)) == 1, "a member named in it"


def test_srs_a_member_resolves_and_reopens_a_thread(client, hub):
    imported(client, hub["chi"])
    root = said(client, hub["other"], body="Why 50?", item="BR ORD1-1", quote=PASSAGE)
    reply = said(client, hub["chi"], body="Kept", parent_id=root["id"])
    closed = client.post(f"{DOC}/comments/{reply['id']}/resolve", headers=hub["chi"])
    assert closed.status_code == 200 and closed.json()["id"] == root["id"] and closed.json()["resolved_by"] == "chi"
    assert listed(client, hub["reader"], status="open") == []
    twice = client.post(f"{DOC}/comments/{root['id']}/resolve", headers=hub["other"])
    assert twice.status_code == 200 and twice.json()["resolved_by"] == "chi", "resolving it again changes nothing"
    opened = client.post(f"{DOC}/comments/{root['id']}/reopen", headers=hub["other"])
    assert opened.status_code == 200 and opened.json()["resolved_by"] is None
    assert [c["id"] for c in listed(client, hub["reader"], status="open")] == [root["id"], reply["id"]]
    assert client.post(f"{DOC}/comments/{root['id']}/reopen", headers=hub["other"]).json()["resolved_by"] is None
    assert client.post(f"{DOC}/comments/{root['id']}/resolve", headers=hub["reader"]).status_code == 403


def test_srs_ba_asked_in_a_comment_reads_the_thread_and_answers_in_it(client, hub):
    enable_ba(client, hub)
    studio = owners_worker(client, hub)
    imported(client, hub["chi"])
    root = said(client, hub["other"], body="@ba does billing cap carts too?", item="BR ORD1-1", quote=PASSAGE)
    assert root["ask_run_id"] is not None, root
    spec = running(client, studio, "task")
    assert spec["id"] == root["ask_run_id"]
    assert f"srs__comment (document demo-shop, parent_id {root['id']}" in spec["prompt"], spec["prompt"]
    agent = agent_of(studio, spec["id"])
    text, error = tool(client, agent, "srs__read", PROJECT, document="demo-shop", thread=root["id"])
    assert not error and PASSAGE in text and "still in version 1" in text and "does billing cap carts" in text, text
    text, error = tool(client, agent, "srs__read", PROJECT, document="demo-shop", item="UC ORD-1")
    assert not error and f"thread {root['id']} by {OTHER}" in text, text
    text, error = tool(
        client, agent, "srs__comment", PROJECT, document="demo-shop", parent_id=root["id"], body="No: 30 lines there."
    )
    assert not error, text
    thread = listed(client, hub["reader"], item="BR ORD1-1")
    assert [(c["body"], c["run_id"] is not None) for c in thread] == [
        ("@ba does billing cap carts too?", False),
        ("No: 30 lines there.", True),
    ]
    told = [n for n in kind_of(client, hub["other"], threads.NOTICE) if n["details"]["comment"] == thread[1]["id"]]
    assert told and told[0]["title"].startswith("@ba commented on BR ORD1-1"), told
    # no agent resolves a thread
    text, error = tool(
        client, agent, "srs__comment", PROJECT, document="demo-shop", parent_id=root["id"], body="@ba again"
    )
    assert not error and listed(client, hub["reader"], item="BR ORD1-1")[-1]["ask_run_id"] is None, "an agent asks none"
