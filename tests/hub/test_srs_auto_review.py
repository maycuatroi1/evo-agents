"""@ba's review of every version that enters review (``evo_agents.core_plugins.srs.auto_review``), as criterion a6 of
plan srs-conflicts-and-tasks names it.

Each submit, in every round, from a member or from a proposal of @ba, hands @ba a review run while its agent type is
on and within its cap, and approval waits for that round's run; when no run is handed, the log says why and approval
waits for none. The run's task and @ba's pack tell it to compare each changed use case with every use case of every
document, drafts included, and with the knowledge graph, which its agent reaches on the hub's /mcp (kg_search,
kg_context); to go through actors, systems, repositories, seams, terms and business rules in Vietnamese and in
English; to record each conflict as a finding with its sources and none without; to settle from the graph first, with
no chat; and to go over the document's open findings, which srs__read lists and the task names, resolving those a new
version settles. Comments on the template stay advice. The agent on the worker is a stand-in, as in
``tests.hub.test_ba_agent``, whose stack this is; the graph is ``tests.hub.test_srs_findings``'s.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.core_plugins import srs
from evo_agents.hub import tables
from tests.hub.live import sql
from tests.hub.test_ba_agent import (  # noqa: F401 (app_key, client, hub, members and srs_core are fixtures)
    CHI,
    OWNER,
    TYPE,
    agent_of,
    app_key,
    client,
    enable_ba,
    hub,
    imported,
    item,
    members,
    mention,
    owners_worker,
    review,
    running,
    saved,
    srs_core,
    submitted,
    tool,
)
from tests.hub.test_chat_runs import created
from tests.hub.test_plugin_points import tool_names
from tests.hub.test_runs import moved
from tests.hub.test_srs_findings import billing, findings, graph, recorded, reviewing, settle  # noqa: F401 (graph)

PACK = Path(srs.__file__).parent / "packs" / "ba"
LOGGER = "evo_agents.core_plugins.srs.auto_review"


def review_titles(db) -> list[str]:
    """The titles of the task runs, by id: @ba's reviews."""
    r = tables.runs
    return [title for (title,) in sql(db, select(r.c.title).where(r.c.kind == "task").order_by(r.c.id))]


def review_run(db, number: int) -> int | None:
    """The review run of the current round of version ``number`` of demo-shop."""
    v, d = tables.srs.versions, tables.srs.documents
    query = select(v.c.review_run_id).join(d, d.c.id == v.c.document_id)
    ((run_id,),) = sql(db, query.where(d.c.slug == "demo-shop", v.c.number == number))
    return run_id


def finished(client, studio: dict) -> dict:
    """The worker claims its next run, a review, and ends it: its spec."""
    spec = running(client, studio, "task")
    moved(client, studio, spec["id"], "verifying", "done", summary="Reviewed.")
    return spec


# Every submit, every round


def test_srs_auto_review_hands_ba_a_review_at_every_submit_in_every_round_from_a_member_or_a_proposal(
    client, hub, hub_db
):
    enable_ba(client, hub)
    studio = owners_worker(client, hub)
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    first = review_run(hub_db, 1)
    assert first is not None
    returned = review(client, hub["other"], 1, "request_changes", body="Name the gateway")
    assert returned.status_code == 200 and returned.json()["state"] == "draft", "a request for changes is never held"
    assert submitted(client, hub["chi"], 1)["round"] == 2
    second = review_run(hub_db, 1)
    assert second not in (None, first)
    assert review_titles(hub_db) == ["@ba reviews demo-shop v1", "@ba reviews demo-shop v1, round 2"]
    held = review(client, hub["other"], 1)
    assert held.status_code == 409 and f"round 2 (run #{second}) is queued" in held.json()["message"], held.text
    assert finished(client, studio)["id"] == first
    again = finished(client, studio)
    assert again["id"] == second
    assert "sent to review, again (round 2): it was sent back and submitted once more" in again["prompt"]
    assert review(client, hub["other"], 1).json()["state"] == "approved"
    # a proposal of @ba enters review and is reviewed, and so is each round after it
    chat = created(client, hub["other"], "SRS", people=[OWNER, CHI], agents=("ba",))
    mention(client, hub["other"], chat["id"], "@ba cap a cart at 40 lines")
    agent = agent_of(studio, running(client, studio, "chat")["id"])
    cart = item(client, hub["chi"], "BR ORD1-1")
    change = {"key": cart["key"], "body": "- A cart holds at most 40 lines."}
    text, error = tool(
        client, agent, "srs__propose", document="demo-shop", base_version=1, summary="Carts of 40", changes=[change]
    )
    third = review_run(hub_db, 2)
    assert not error and f"@ba reviews it in run #{third}" in text, text
    returned = review(client, hub["chi"], 2, "request_changes", body="Say why 40")
    assert returned.json()["state"] == "draft", returned.text
    assert submitted(client, hub["other"], 2)["round"] == 2
    assert review_run(hub_db, 2) not in (None, third)
    assert review_titles(hub_db)[2:] == ["@ba reviews demo-shop v2", "@ba reviews demo-shop v2, round 2"]


def test_srs_auto_review_not_handed_says_why_in_the_log_and_approval_waits_for_no_run(
    client, hub, hub_db, monkeypatch, caplog
):
    enable_ba(client, hub)
    owners_worker(client, hub)
    imported(client, hub["chi"])
    spent = {"usd": 5.0}

    async def type_spend(conn, agent_type_id):
        return spent["usd"]

    monkeypatch.setattr("evo_agents.hub.server.task_dispatch.type_spend", type_spend)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        submitted(client, hub["chi"], 1)
    (why,) = [record.why for record in caplog.records if record.getMessage() == "no review by @ba"]
    assert "the task runs of agent type ba cost $5.00" in why and "reached its cap of $5.00" in why, why
    assert review_titles(hub_db) == [] and review_run(hub_db, 1) is None
    assert review(client, hub["other"], 1).json()["state"] == "approved", "no run of the round to wait for"
    # turned off: no review either, and the log says so
    spent["usd"] = 0.0
    assert client.delete(TYPE, headers=hub["owner"]).status_code == 200
    cart = item(client, hub["chi"], "BR ORD1-1")
    saved(client, hub["chi"], 1, {"key": cart["key"], "body": "- A cart holds at most 40 lines."})
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        submitted(client, hub["chi"], 2)
    (why,) = [record.why for record in caplog.records if record.getMessage() == "no review by @ba"]
    assert "agent type ba of plugin srs is not enabled" in why and "turned off" in why, why
    assert review_titles(hub_db) == [] and review_run(hub_db, 2) is None
    assert review(client, hub["other"], 2).json()["state"] == "approved"


# What the review is told, and what it reaches


def test_srs_auto_review_task_and_pack_tell_ba_to_compare_every_document_and_the_graph_with_sources(
    client, hub, hub_db
):
    billing(client, hub["chi"])  # a draft of a second document
    enable_ba(client, hub)
    studio = owners_worker(client, hub)
    imported(client, hub["chi"])
    submitted(client, hub["chi"], 1)
    spec = running(client, studio, "task")
    prompt = spec["prompt"]
    for said in (
        "No version of it was approved yet: every use case in it is a changed one.",
        "Compare each changed use case with every use case of every document of the project, drafts and versions "
        "in review included (srs__search reads the latest version of each document)",
        "kg_search finds nodes by words, kg_context shows a node with what it links to",
        "search each entry in Vietnamese and in English",
        "the actors, the systems, the repositories, the seams between them, the terms of the glossary, the business "
        "rules",
        "Record each conflict as one finding with srs__finding",
        "A conflict with no source is no finding: do not record it.",
        "a Decision line of a plan, an approved item of an SRS document, or a newer document",
        "and no settled_by). A finding settled from the graph needs no chat",
        "Go over every open finding of the document. The document had no open finding when this review was handed.",
        "as comments with srs__comment",
        "Comments are advice: they block no approval.",
        "you open no chat",
    ):
        assert said in prompt, said
    agent = agent_of(studio, spec["id"])
    assert {"kg_search", "kg_context", "srs__search", "srs__finding", "srs__comment"} <= set(tool_names(client, agent))
    text, error = tool(client, agent, "srs__search", query="UC BIL-2")
    assert not error and "- billing v1: UC BIL-2 (use_case)" in text, "a draft of another document is searched"
    # the pack says the same
    instructions = (PACK / "INSTRUCTIONS.md").read_text(encoding="utf-8")
    skill = (PACK / "skills" / "srs-review" / "SKILL.md").read_text(encoding="utf-8")
    for text in (instructions, skill):
        for said in (
            "srs__search",
            "drafts",
            "kg_search",
            "kg_context",
            "actors",
            "systems",
            "repositories",
            "seams",
            "terms",
            "business rules",
            "in Vietnamese and in English",
            "srs__finding",
            "do not record it",
            "Decision line",
            "approved",
            "newer document",
            "needs no chat",
            "open findings",
            "srs__comment",
            "Blocking:",
        ):
            assert said in text, said


def test_srs_auto_review_settles_from_the_graph_and_resolves_the_open_findings_a_new_version_settles(
    client, hub, hub_db, graph
):
    billing(client, hub["chi"])
    studio, run_id, agent = reviewing(client, hub)
    retries = recorded(client, agent)  # decide, BR ORD2-1 against BR BIL2-1
    rollout = recorded(
        client,
        agent,
        kind="kg_conflict",
        item="UC ORD-2",
        summary="Payment retries differ from the rollout plan",
        sources=[{"kind": "node", "ref": "plan:rollout/step:1"}],
    )
    decision = [{"kind": "node", "ref": "plan:rollout/step:1", "note": "Decision: three retries"}]
    text, error = settle(
        client, agent, "resolve", rollout, resolution="The plan decided three retries", sources=decision
    )
    assert not error and "Resolved from the graph: The plan decided three retries" in text, text
    rows = {row["id"]: row for row in findings(client, hub["reader"])}
    assert (rows[rollout]["status"], rows[rollout]["settled_by"], rows[rollout]["chat_link"]) == (
        "resolved",
        None,
        None,
    )
    assert rows[rollout]["resolution_sources"] == decision
    # srs__read lists the open findings of the document, and only those
    text, error = tool(client, agent, "srs__read", document="demo-shop")
    assert not error, text
    listed = (
        f"Open findings of demo-shop (1):\n- #{retries} decide contradiction, on version 1, BR ORD2-1: Retries differ"
    )
    assert listed in text and "[code BR BIL2-1]" in text, text
    assert f"#{rollout} " not in text
    moved(client, studio, run_id, "verifying", "done", summary="Two findings, one settled from the graph.")
    held = review(client, hub["other"], 1)
    assert held.status_code == 409 and f"#{retries} (contradiction, BR ORD2-1)" in held.json()["message"], held.text
    # the next version settles it, and its review resolves it citing that version
    assert review(client, hub["other"], 1, "request_changes", body="Settle the retries").json()["state"] == "draft"
    rule = item(client, hub["chi"], "BR ORD2-1")
    saved(client, hub["chi"], 1, {"key": rule["key"], "body": rule["body"].replace("3 times", "5 times")})
    submitted(client, hub["chi"], 2)
    spec = running(client, studio, "task")
    assert spec["id"] == review_run(hub_db, 2)
    assert f"The document's open findings when this review was handed: #{retries}." in spec["prompt"]
    assert 'resolve it with the source {"kind": "version", "ref": "2"}' in spec["prompt"]
    assert "The version approved last" not in spec["prompt"]
    agent = agent_of(studio, spec["id"])
    by_version = [{"kind": "version", "ref": "2"}]
    text, error = settle(
        client, agent, "resolve", retries, resolution="Version 2 retries 5 times, as billing does", sources=by_version
    )
    assert not error, text
    row = {row["id"]: row for row in findings(client, hub["reader"])}[retries]
    assert (row["status"], row["settled_by"], row["resolution_sources"]) == ("resolved", None, by_version)
    text, error = tool(client, agent, "srs__read", document="demo-shop")
    assert not error and "Open findings" not in text, text
    moved(client, studio, spec["id"], "verifying", "done", summary="Version 2 settles the retries.")
    approved = review(client, hub["other"], 2)
    assert approved.status_code == 200 and approved.json()["state"] == "approved", approved.text
