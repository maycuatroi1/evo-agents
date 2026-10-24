"""The intakes of a project (plan problem-intake, criterion a2; ``evo_agents.hub.intakes``,
``evo_agents.hub.server.intake`` and ``evo_agents.hub.cli_intake``): a writer starts an intake with the kinds and labels
of its sources, and puts its evo-intake/1 result once. Each candidate becomes a pending proposal of @builder (a new
problem, or lines of evidence for the problem it matches); a candidate with one independent source that nobody
confirmed is refused with 422 and the put writes nothing; each promise becomes a task of the plugin tasks, due when the
session inferred a date, assigned to the promiser when a member and to the requester otherwise, its description saying
whether delivery evidence was seen; without the plugin the promises stay in the intake.

The model and the schema run without Postgres; everything that needs the hub skips without EVO_HUB_TEST_DSN."""

from __future__ import annotations

import copy

import pytest

pytest.importorskip("sqlalchemy")

from evo_agents import schema as json_schema
from evo_agents.harness import load_schema
from evo_agents.hub import intakes, problems
from evo_agents.hub.contract import commands
from tests.hub import pg

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
LINE = {
    "source_kind": "messenger",
    "source_uri": "messenger://shop-group/thread/1",
    "locator": "message 4",
    "speaker": "Mira",
    "excerpt": "We ring the depot every morning to ask if the milk is coming.",
    "signal": "workaround",
    "label": "internal",
}
BASIS = {"statement": {"quote": "we ring the depot"}, "who": "inferred", "when": "inferred", "workaround": "inferred"}
NEW = {
    "statement": "Shops cannot tell when a delivery comes",
    "who": "shop owners",
    "when": "every morning before the delivery",
    "workaround": "they ring the depot",
    "basis": BASIS,
    "evidence": [LINE, {**LINE, "source_uri": "email://depot/42", "source_kind": "email", "locator": "mail 42"}],
}
RESULT = {"schema": "evo-intake/1", "candidates": [NEW], "promises": [], "questions": []}

if pg.DSN:
    from sqlalchemy import select

    from evo_agents.hub import tables
    from tests.hub.live import sql
    from tests.hub.test_runs import PROJECT, audit_rows
    from tests.hub.test_tasks_plugin import (  # noqa: F401 (client, members and tasks_core are fixtures)
        BASE,
        client,
        enable,
        members,
        shown,
        tasks_core,
    )


def issues(document: dict) -> list[str]:
    return [str(i) for i in json_schema.errors(json_schema.validate(document, load_schema("intake")))]


def test_the_intake_codes_and_the_vocabulary_of_the_sources():
    assert intakes.code(3) == "I-3" and intakes.number_of("I-3") == 3 and intakes.number_of("P-3") is None
    assert intakes.number_of("I-0") is None
    assert intakes.SOURCE_KINDS == problems.SOURCE_KINDS
    assert (
        intakes.source_text({"kind": "messenger", "label": "internal", "name": "Shop"}) == "messenger (internal) Shop"
    )


def test_the_schema_of_evo_intake_1_takes_a_result_and_refuses_what_the_hub_never_keeps():
    assert issues(RESULT) == []
    assert issues({"schema": "evo-intake/1"}) == []
    assert issues({**RESULT, "schema": "evo-intake/2"})
    personal = copy.deepcopy(RESULT)
    personal["candidates"][0]["evidence"][0]["label"] = "personal"
    assert any("label" in text for text in issues(personal))
    long = copy.deepcopy(RESULT)
    long["candidates"][0]["evidence"][0]["excerpt"] = "x" * 301
    assert any("excerpt" in text for text in issues(long))
    odd = copy.deepcopy(RESULT)
    odd["candidates"][0]["basis"]["who"] = "guessed"
    assert issues(odd)
    assert issues({**RESULT, "candidates": [{"statement": "x", "evidence": []}]})  # a line at least
    assert issues({**RESULT, "promises": [{"text": "send it", "due": "tomorrow"}]})


def test_intake_commands_are_in_the_contract():
    printed = commands()
    for name in ("start", "put", "show"):
        assert printed[f"hub intake {name}"]["json"]["schema"] == "Intake"
    assert printed["hub intake list"]["json"]["kind"] == "array"


def put(client, headers, ref: str, body: dict, project: str | None = None):
    return client.put(f"/v1/projects/{project or PROJECT}/intakes/{ref}", json=body, headers=headers)


def start(client, headers, expect: int = 201, **body):
    body = {"sources": [{"kind": "messenger", "label": "internal", "name": "Shop group"}], **body}
    answer = client.post(f"/v1/projects/{PROJECT}/intakes", json=body, headers=headers)
    assert answer.status_code == expect, answer.text
    return answer.json()


def proposals(db) -> list:
    b = tables.builder_proposals
    return sql(
        db, select(b.c.id, b.c.kind, b.c.state, b.c.target, b.c.change, b.c.base_version, b.c.chat_id).order_by(b.c.id)
    )


@needs_pg
def test_a_writer_starts_an_intake_and_members_read_it(client, members):
    made = start(client, members["alice"], note="the shop group of June")
    assert (made["code"], made["status"], made["requested_by"], made["outcome"]) == ("I-1", "open", "alice", None)
    assert made["sources"] == [{"kind": "messenger", "label": "internal", "name": "Shop group"}]
    assert start(client, members["bao"])["code"] == "I-2"
    base = f"/v1/projects/{PROJECT}/intakes"
    assert start(client, members["eve"], expect=403)["error"] == "forbidden"
    for bad in (
        {"sources": []},
        {"sources": [{"kind": "tweet", "label": "internal"}]},
        {"sources": [{"kind": "file", "label": "secret"}]},
        {"sources": [{"kind": "file", "label": "public", "content": "raw"}]},
    ):
        assert client.post(base, json=bad, headers=members["alice"]).status_code == 422, bad
    listed = client.get(base, headers=members["eve"]).json()
    assert [i["code"] for i in listed] == ["I-2", "I-1"]
    assert client.get(base + "?status=done", headers=members["eve"]).json() == []
    assert client.get(base + "/I-1", headers=members["eve"]).json()["note"] == "the shop group of June"
    assert client.get(base + "/I-9", headers=members["eve"]).status_code == 404
    no_grant = client.get(base, headers={"Authorization": "Bearer nothing"})
    assert no_grant.status_code == 401


@needs_pg
def test_a_put_makes_a_proposal_for_each_candidate_and_a_second_put_is_refused(client, members, hub_db):
    texts = {"who": "shop owners", "when": "mornings", "workaround": "ring the depot"}
    made = client.put(
        f"/v1/projects/{PROJECT}/problems/P-1", json={"statement": "Milk is late", **texts}, headers=members["alice"]
    )
    assert made.status_code == 200, made.text
    start(client, members["alice"])
    matched = {
        "matches": "P-1",
        "confirmed_by": "bao",
        "evidence": [{**LINE, "source_uri": "email://depot/77", "source_kind": "email", "locator": None}],
    }
    matched["evidence"][0].pop("locator")
    body = {**RESULT, "candidates": [NEW, matched], "questions": [{"text": "Who owns the depot contract?"}]}
    assert put(client, members["eve"], "I-1", body).status_code == 403
    assert put(client, members["alice"], "I-7", body).status_code == 404
    done = put(client, members["alice"], "I-1", body)
    assert done.status_code == 200, done.text
    result = done.json()
    assert (result["status"], result["put_by"], result["candidates"], result["promises"], result["questions"]) == (
        "done",
        "alice",
        2,
        0,
        1,
    )
    first, second = result["outcome"]["proposals"]
    assert (first["kind"], first["problem"], first["lines"]) == ("new_problem", "P-2", 2)
    assert (second["kind"], second["problem"], second["lines"]) == ("evidence", "P-1", 1)
    assert result["outcome"]["questions"] == [{"text": "Who owns the depot contract?"}]
    assert result["outcome"]["tasks"] == [] and result["outcome"]["tasks_skipped"] is None
    rows = proposals(hub_db)
    assert [(*r[1:4], r[5], r[6]) for r in rows] == [
        ("problem", "pending", {"problem": "P-2"}, 0, None),
        ("problem", "pending", {"problem": "P-1"}, 1, None),
    ]
    assert rows[0][4]["set"]["statement"] == NEW["statement"] and len(rows[0][4]["evidence"]) == 2
    assert rows[1][4]["set"] == {} and rows[1][4]["evidence"][0]["source_uri"] == "email://depot/77"
    assert put(client, members["alice"], "I-1", body).status_code == 409
    shown_again = client.get(f"/v1/projects/{PROJECT}/intakes/I-1", headers=members["eve"]).json()
    assert shown_again["outcome"]["proposals"][0]["proposal_id"] == rows[0][0]
    audit = [row[1] for row in audit_rows(hub_db, "intake")]
    assert len(audit) == 2 and audit[0].endswith("intake I-1 started sources=1")
    assert "candidates=2 promises=0 questions=1" in audit[1]
    assert all(LINE["excerpt"] not in text and "Shop group" not in text for text in audit)


@needs_pg
def test_a_candidate_with_one_source_nobody_confirmed_is_refused_and_nothing_is_written(client, members, hub_db):
    start(client, members["alice"])
    single = {**NEW, "evidence": [LINE, {**LINE, "locator": "message 9"}]}  # two lines, one source
    good = {**NEW, "statement": "A second one", "evidence": NEW["evidence"]}
    refused = put(client, members["alice"], "I-1", {**RESULT, "candidates": [good, single]})
    assert refused.status_code == 422, refused.text
    detail = refused.json()["message"]
    assert "candidate 2: 1 independent source and nobody confirmed it" in detail and detail.endswith("nothing changed")
    assert "candidate 1" not in detail
    assert proposals(hub_db) == []
    assert client.get(f"/v1/projects/{PROJECT}/intakes/I-1", headers=members["eve"]).json()["status"] == "open"
    confirmed = put(client, members["alice"], "I-1", {**RESULT, "candidates": [{**single, "confirmed_by": "bao"}]})
    assert confirmed.status_code == 200, confirmed.text
    assert len(proposals(hub_db)) == 1


@needs_pg
def test_a_put_names_every_other_thing_wrong_with_it(client, members, hub_db):
    start(client, members["alice"])
    cases = (
        ({**NEW, "basis": {"statement": "inferred"}}, "say where who, when, workaround come from"),
        ({**NEW, "matches": "P-4"}, "has no problem P-4"),
        ({**NEW, "confirmed_by": "carol"}, "confirmed_by carol holds no grant"),
        ({**NEW, "evidence": [{**LINE, "label": "personal"}]}, "evidence[0].label"),
        ({k: v for k, v in NEW.items() if k != "who"}, "a new problem says who"),
        ({**NEW, "evidence": [LINE, LINE]}, "the same line is given twice"),
    )
    for candidate, reason in cases:
        answer = put(client, members["alice"], "I-1", {**RESULT, "candidates": [candidate]})
        assert answer.status_code == 422 and reason in answer.json()["message"], (reason, answer.text)
    assert (
        put(client, members["alice"], "I-1", {**RESULT, "promises": [{"text": "x", "due": "2026-02-30"}]}).status_code
        == 422
    )
    assert put(client, members["alice"], "I-1", {"candidates": []}).status_code == 422
    assert put(client, members["alice"], "I-1", []).status_code == 422
    assert proposals(hub_db) == []


@needs_pg
def test_a_candidate_matching_a_problem_counts_the_lines_the_problem_has(client, members, hub_db):
    texts = {"who": "shop owners", "when": "mornings", "workaround": "ring the depot"}
    client.put(
        f"/v1/projects/{PROJECT}/problems/P-1", json={"statement": "Milk is late", **texts}, headers=members["alice"]
    )
    added = client.post(f"/v1/projects/{PROJECT}/problems/P-1/evidence", json=LINE, headers=members["alice"])
    assert added.status_code == 201, added.text
    start(client, members["alice"])
    same_source = {"matches": "P-1", "evidence": [{**LINE, "locator": "message 5", "excerpt": "Again."}]}
    refused = put(client, members["alice"], "I-1", {**RESULT, "candidates": [same_source]})
    assert refused.status_code == 422 and "1 independent source" in refused.json()["message"]
    repeated = {"matches": "P-1", "confirmed_by": "bao", "evidence": [LINE]}
    again = put(client, members["alice"], "I-1", {**RESULT, "candidates": [repeated]})
    assert again.status_code == 422 and "has this line already as E-1" in again.json()["message"]
    other = {"matches": "P-1", "evidence": [{**LINE, "source_uri": "email://depot/9", "source_kind": "email"}]}
    assert put(client, members["alice"], "I-1", {**RESULT, "candidates": [other]}).status_code == 200
    confirmed = client.post(f"/v1/projects/{PROJECT}/problems/P-1/evidence/E-1/confirm", headers=members["alice"])
    assert confirmed.status_code == 200
    start(client, members["alice"])
    now_confirmed = put(client, members["alice"], "I-2", {**RESULT, "candidates": [same_source]})
    assert now_confirmed.status_code == 200, now_confirmed.text  # E-1 was confirmed by a writer


PROMISES = [
    {"text": "Send the price list", "promiser": "bao", "due": "2026-11-03", "source": "Shop group"},
    {
        "text": "Fix the delivery slots",
        "promiser": "carol",
        "delivery_seen": True,
        "delivery_note": "mail 42 says shipped",
    },
    {"text": "Call the depot"},
]


@needs_pg
def test_without_the_plugin_tasks_the_promises_stay_in_the_intake_and_the_put_says_so(client, members, hub_db):
    start(client, members["alice"])
    done = put(client, members["alice"], "I-1", {**RESULT, "candidates": [], "promises": PROMISES})
    assert done.status_code == 200, done.text
    outcome = done.json()["outcome"]
    assert outcome["tasks"] == []
    assert (
        outcome["tasks_skipped"]
        == f"plugin tasks is not enabled in project {PROJECT}: the 3 promises stay in the intake"
    )
    assert done.json()["promises"] == 3
    assert sql(hub_db, select(tables.tasks.items.c.id)) == []


@needs_pg
def test_each_promise_becomes_a_task_due_assigned_and_saying_whether_delivery_was_seen(client, members, hub_db):
    enable(client, members["owner"], "tasks", ["plans:read"])
    start(client, members["alice"])
    done = put(client, members["alice"], "I-1", {**RESULT, "candidates": [], "promises": PROMISES})
    assert done.status_code == 200, done.text
    outcome = done.json()["outcome"]
    assert outcome["tasks_skipped"] is None
    assert [(t["promise"], t["task"], t["assignee"], t["due_on"], t["delivery_seen"]) for t in outcome["tasks"]] == [
        (1, "T-1", "bao", "2026-11-03", False),
        (2, "T-2", "alice", None, True),
        (3, "T-3", "alice", None, False),
    ]
    first, second, third = (shown_task(client, members["eve"], f"T-{n}") for n in (1, 2, 3))
    assert (first["title"], first["due_on"], first["assignees"]) == ("Send the price list", "2026-11-03", ["bao"])
    assert "Promised by bao, read in intake I-1 from Shop group." in first["description_md"]
    assert "Due 2026-11-03" in first["description_md"] and "No delivery evidence was seen." in first["description_md"]
    assert second["assignees"] == ["alice"]
    assert "carol (not a member of the project; alice asked for the intake)" in second["description_md"]
    assert "Delivery evidence was seen: mail 42 says shipped." in second["description_md"]
    assert third["assignees"] == ["alice"] and "someone the sources do not name" in third["description_md"]
    keys = sql(hub_db, select(tables.tasks.items.c.external_key).order_by(tables.tasks.items.c.number))
    assert [k[0] for k in keys] == [f"intake:{PROJECT_ID(hub_db)}:I-1:promise:{n}" for n in (1, 2, 3)]


def shown_task(client, headers, ref: str) -> dict:
    return shown(client, headers, ref)


def PROJECT_ID(db) -> int:
    ((project_id,),) = sql(db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))
    return project_id
