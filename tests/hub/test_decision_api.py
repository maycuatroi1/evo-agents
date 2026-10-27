"""The decision API of core plugins (API 1.7, ``evo_agents.hub.server.core_plugin_api._decision``), with the sample
plugin northwind-scribe (tests/fixtures/core_plugins/northwind_scribe), which declares an egress sink in its manifest.

- egress: a text read returns only what the project's policy lets the plugin's sink read, under the rule the judges
  apply to Codex; a project without the sink gets an empty result that says why; a plugin without egress is refused;
- the reads: why a run failed, the review queue of the graph, the corrections of a time range, the accepted rules;
- the writes: the cause of a run the hub guessed (a worker's is never overwritten, the run's log says who, a writer
  restores it), a decision on the graph and a dismissal, as the plugin, which a writer undoes as any other.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from importlib.metadata import EntryPoint
from pathlib import Path

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import insert, select, update

from evo_agents.hub import judges, plugin_calls, plugin_events, tables
from evo_agents.hub.access import ProjectRules
from evo_agents.hub.server import audit
from evo_agents.hub.server import core_plugin_api as hub_api
from evo_agents.hub.server.app import create_app
from evo_agents.hub.server.core_plugin_api import _decision as decision
from evo_agents.hub.server.core_plugin_api._types import issue_plugin
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from tests.hub import live
from tests.hub.conftest import github, hub_db, hub_template, pg_server  # noqa: F401 (fixtures)
from tests.hub.live import sql
from tests.hub.test_chat_runs import chat_worker
from tests.hub.test_curator import grant
from tests.hub.test_runs import OWNER, PLAN, PROJECT, hub  # noqa: F401 (hub: a fixture, on this module's client)
from tests.hub.test_triage import failed_step

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "core_plugins"
PLUGIN = "northwind-scribe"
SINK = "scribe@northwind"
SCRIBE = EntryPoint(PLUGIN, "northwind_scribe:manifest", ENTRY_POINT_GROUP)
INTERNAL = {"level": "internal", "projects": [PROJECT]}
CUSTOMER = {"level": "customer", "projects": [PROJECT]}


@pytest.fixture
def installed(monkeypatch):
    monkeypatch.syspath_prepend(str(FIXTURES))
    monkeypatch.setattr(core, "entry_points", lambda *, group: [SCRIBE] if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    yield
    plugin_calls.core_registry.cache_clear()


@pytest.fixture
def client(hub_db, tmp_path, github, installed):  # noqa: F811
    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github)), base_url="https://hub.test") as client:
        yield client


def enable(client, hub) -> None:
    grant(client, hub, OWNER, "admin")  # an admin of the project enables a plugin in it
    response = client.post(
        f"/v1/projects/{PROJECT}/plugins/{PLUGIN}/enable", json={"permissions": []}, headers=hub["owner"]
    )
    assert response.status_code == 200, response.text


@pytest.fixture
def scribe(client, hub, hub_db):  # noqa: F811
    """The project enabling northwind-scribe, which its policy gives the sink SINK at the level internal."""
    enable(client, hub)
    project_id = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    sql(
        hub_db,
        insert(tables.project_sinks).values(
            project_id=project_id, sink_id=SINK, kind="llm-backend", clearance={"level": "internal"}
        ),
    )
    return hub_api.plugin("northwind_scribe")


def on_hub(client, work):
    async def run():
        async with client.app.state.engine.begin() as conn:
            return await work(conn)

    return client.portal.call(run)


@pytest.fixture
def worker(client, hub):  # noqa: F811
    return chat_worker(client, hub["owner"], "mac-mini", slots=4)


def failed(client, hub, worker, *, cause, error="verify command `pytest` exited 1", **extra) -> int:
    return failed_step(client, hub, worker, cause=cause, error=error, **extra)


# The sink in the manifest, and what it reads


def test_decision_api_egress_is_declared_in_the_manifest_and_a_plugin_without_it_is_refused(scribe, client):
    assert scribe.egress == (hub_api.EgressSink(SINK, "llm-backend", "api.scribe.northwind.example"),)
    plain = issue_plugin("northwind-plain", ())  # what a plugin whose manifest has no key egress is handed
    for read in (
        lambda conn: decision.read_run_failure(conn, plain, PROJECT, 1),
        lambda conn: decision.read_accepted_rules(conn, plain, PROJECT),
        lambda conn: decision.read_corrections(conn, plain, PROJECT, since=datetime(2026, 1, 1, tzinfo=UTC)),
        lambda conn: decision.classify_run_failure(conn, plain, PROJECT, 1, "runtime", model="m", confidence=0.5),
    ):
        with pytest.raises(ValueError, match="declares no egress"):
            on_hub(client, read)
    with pytest.raises(ValueError, match="is no egress sink"):
        on_hub(client, lambda conn: decision.read_accepted_rules(conn, scribe, PROJECT, sink="codex@openai"))


def test_decision_api_egress_without_the_sink_in_the_policy_returns_nothing_and_says_why(client, hub, hub_db, worker):
    enable(client, hub)  # enabled, but the project's policy declares no such sink
    run_id = failed(client, hub, worker, cause=None)
    plugin = hub_api.plugin("northwind_scribe")
    since = datetime(2026, 1, 1, tzinfo=UTC)
    for read in (
        lambda conn: decision.read_run_failure(conn, plugin, PROJECT, run_id),
        lambda conn: decision.read_corrections(conn, plugin, PROJECT, since=since),
        lambda conn: decision.read_accepted_rules(conn, plugin, PROJECT),
    ):
        found = on_hub(client, read)
        assert found.items == () and found.sink == SINK and "declares no sink" in found.reason


def rules(sinks, levels=("public", "internal", "customer", "secret")) -> ProjectRules:
    return ProjectRules(PROJECT, list(levels), ["any", "domestic-only"], sinks)


def test_decision_api_egress_clears_a_label_as_the_judges_clear_it_for_codex():
    internal = {"id": SINK, "kind": "llm-backend", "clearance": {"level": "internal"}}
    codex = {**internal, "id": "codex@openai"}
    unknown = {**internal, "clearance": {"level": "nonexistent"}}
    foreign = {"level": "internal", "projects": ["other"]}
    for sinks, label in (
        ([internal], INTERNAL),
        ([internal], CUSTOMER),
        ([internal], None),
        ([internal], foreign),
        ([unknown], INTERNAL),
        ([], INTERNAL),
    ):
        ours = decision._declared(rules(sinks), SINK) and decision._clears(rules(sinks), label, SINK)
        asked = [{**sink, "id": "codex@openai"} for sink in sinks]
        assert ours == judges.codex_cleared(rules(asked), label), (sinks, label)
    assert judges.codex_cleared(rules([codex]), INTERNAL)


def test_decision_api_egress_a_run_above_the_sinks_clearance_is_withheld(scribe, client, hub, hub_db, worker):
    run_id = failed(client, hub, worker, cause=None, error="verify command `pytest` exited 1")
    found = on_hub(client, lambda conn: decision.read_run_failure(conn, scribe, PROJECT, run_id))
    assert found.withheld == 0 and len(found.items) == 1
    sql(hub_db, update(tables.plans).values(label=CUSTOMER).where(tables.plans.c.plan_id == PLAN))
    found = on_hub(client, lambda conn: decision.read_run_failure(conn, scribe, PROJECT, run_id))
    assert (found.items, found.withheld) == ((), 1) and "above the sink's clearance" in found.reason


# Why a run failed, and its cause


def test_decision_api_reads_why_a_run_failed_with_a_sanitized_error_and_the_inferred_flag(
    scribe, client, hub, hub_db, worker
):
    secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    guessed = failed(client, hub, worker, cause=None, error=f"verify command `pytest` exited 1 token {secret}")
    (found,) = on_hub(client, lambda conn: decision.read_run_failure(conn, scribe, PROJECT, guessed)).items
    assert found.run_id == guessed and found.state == "failed" and found.inferred is True
    assert found.failure_cause is not None and secret not in found.error and "pytest" in found.error
    assert 0 < len(found.events) <= decision.MAX_EVENT_TAIL
    assert [e.seq for e in found.events] == sorted(e.seq for e in found.events)
    assert all(len(e.text) <= decision.MAX_EVENT_CHARS for e in found.events)
    with pytest.raises(hub_api.Refused) as refused:
        on_hub(client, lambda conn: decision.read_run_failure(conn, scribe, PROJECT, guessed + 999))
    assert refused.value.status_code == 404


def test_decision_api_classifies_only_a_cause_the_hub_guessed_and_a_writer_restores_it(
    scribe, client, hub, hub_db, worker
):
    guessed = failed(client, hub, worker, cause=None, error="verify command `pytest` exited 1")
    worker_set = failed(client, hub, worker, cause="credentials", error="no credential reads evo-agents")
    r = tables.runs

    def cause(run_id):
        return sql(hub_db, select(r.c.failure_cause).where(r.c.id == run_id))[0][0]

    before = cause(guessed)
    with pytest.raises(hub_api.Refused) as refused:  # the worker's cause is never overwritten
        on_hub(
            client,
            lambda conn: decision.classify_run_failure(
                conn, scribe, PROJECT, worker_set, "runtime", model="m", confidence=0.9
            ),
        )
    assert refused.value.status_code == 409 and cause(worker_set) == "credentials"
    other = "environment" if before != "environment" else "runtime"
    for bad in ({"cause": "nonsense", "confidence": 0.5}, {"cause": other, "confidence": 1.5}):
        with pytest.raises(ValueError):
            on_hub(
                client,
                lambda conn, bad=bad: decision.classify_run_failure(
                    conn, scribe, PROJECT, guessed, bad["cause"], model="m", confidence=bad["confidence"]
                ),
            )
    done = on_hub(
        client,
        lambda conn: decision.classify_run_failure(
            conn, scribe, PROJECT, guessed, other, model="claude-x", confidence=0.8
        ),
    )
    assert (done.cause, done.previous) == (other, before) and cause(guessed) == other
    e = tables.run_events
    (note,) = sql(hub_db, select(e.c.body).where(e.c.run_id == guessed, e.c.seq == done.seq))[0]
    said = note["cause_classification"]
    assert said["plugin"] == PLUGIN and said["model"] == "claude-x" and said["confidence"] == 0.8
    assert said["previous_cause"] == before and note["failure_cause_inferred"] is True
    (found,) = on_hub(client, lambda conn: decision.read_run_failure(conn, scribe, PROJECT, guessed)).items
    assert found.inferred is True and found.failure_cause == other  # still a guess: the rerun rules read it as before
    a = tables.audit
    assert sql(hub_db, select(a.c.target).where(a.c.action == audit.RUN_CAUSE))[0][0].endswith(
        f"plugin={PLUGIN} model=claude-x"
    )
    with pytest.raises(hub_api.Refused):  # once: a writer restores it first
        on_hub(
            client,
            lambda conn: decision.classify_run_failure(conn, scribe, PROJECT, guessed, before, model="m", confidence=1),
        )
    restore = f"/v1/projects/{PROJECT}/runs/{guessed}/cause/restore"
    assert client.post(restore, headers=hub["reader"]).status_code == 403
    restored = client.post(restore, headers=hub["owner"])
    assert restored.status_code == 200, restored.text
    assert restored.json() == {"run_id": guessed, "failure_cause": before, "was": other} and cause(guessed) == before
    assert client.post(restore, headers=hub["owner"]).status_code == 409  # nothing of a plugin's is in force now


def test_decision_api_the_run_state_event_of_a_failed_run_carries_its_cause_and_whether_the_hub_guessed_it(
    scribe, client, hub, hub_db, worker, monkeypatch
):
    sent = []

    async def record(conn, kind, project_id, payload, *, plugin_id=None):
        sent.append((kind, payload))
        return 0

    monkeypatch.setattr(plugin_events, "emit", record)
    guessed = failed(client, hub, worker, cause=None, error="verify command `pytest` exited 1")
    given = failed(client, hub, worker, cause="credentials", error="no credential reads evo-agents")
    states = [p for kind, p in sent if kind == "run.state"]
    ended = {p["run_id"]: p for p in states if p["to"] == "failed"}
    assert ended[given]["failure_cause"] == "credentials" and ended[given]["failure_cause_inferred"] is False
    assert ended[guessed]["failure_cause_inferred"] is True and ended[guessed]["failure_cause"]
    assert all("failure_cause" not in p for p in states if p["to"] != "failed")


# What people corrected, and what the project accepted


def test_decision_api_reads_the_corrections_of_a_time_range_by_label(scribe, client, hub, hub_db):
    project_id = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    now = datetime.now(UTC)
    turns = ["no, use the other file", "thanks", "that is wrong, stop adding new files"]
    f = tables.session_facets

    def facet(session_id, at, label):
        sql(
            hub_db,
            insert(f).values(
                project_id=project_id,
                kind="session",
                session_id=session_id,
                at=at,
                label=label,
                body={"user_turns": turns, "corrections": [0, 2]},
            ),
        )

    facet("s-new", now - timedelta(hours=1), INTERNAL)
    facet("s-old", now - timedelta(days=40), INTERNAL)
    facet("s-secret", now - timedelta(hours=2), CUSTOMER)
    found = on_hub(client, lambda conn: decision.read_corrections(conn, scribe, PROJECT, since=now - timedelta(days=7)))
    assert [c.evidence for c in found.items] == ["session:s-new:user_turns:0", "session:s-new:user_turns:2"]
    assert found.items[0].text == turns[0] and found.withheld == 2  # the customer session stays out
    wide = on_hub(
        client,
        lambda conn: decision.read_corrections(
            conn, scribe, PROJECT, since=now - timedelta(days=90), until=now - timedelta(days=7)
        ),
    )
    assert [c.evidence for c in wide.items][0] == "session:s-old:user_turns:0"


def test_decision_api_reads_the_accepted_rules_of_the_project(scribe, client, hub, hub_db, worker):
    guessed = failed(client, hub, worker, cause=None)
    project_id = sql(hub_db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    p = tables.proposals

    def proposal(title, state, label):
        values = {
            "project_id": project_id,
            "run_id": guessed,
            "lens": "corrections",
            "kind": "docs",
            "title": title,
            "summary": f"{title}, in short",
            "paths": [],
            "tier_reasons": ["a docs change"],
            "evidence": [],
            "evidence_count": 1,
            "draft": {},
            "fingerprint": hashlib.sha256(title.encode()).hexdigest(),
            "tier": 1,
            "state": state,
            "label": label,
        }
        if state == "accepted":
            values["answered_at"] = datetime.now(UTC)
            values["answered_by"] = sql(hub_db, select(tables.users.c.id).limit(1))[0][0]
        sql(hub_db, insert(p).values(**values))

    proposal("Run the linter first", "accepted", INTERNAL)
    proposal("Never edit generated files", "open", INTERNAL)
    proposal("Customer rule", "accepted", CUSTOMER)
    found = on_hub(client, lambda conn: decision.read_accepted_rules(conn, scribe, PROJECT))
    assert [(r.kind, r.title, r.summary) for r in found.items] == [
        ("proposal", "Run the linter first", "Run the linter first, in short")
    ]
    assert found.withheld == 1


# The graph: the review queue, a dismissal and a decision


QUEUE = {
    "computed": True,
    "reason": None,
    "items": [
        {
            "fingerprint": "r:0123456789abcdef",
            "rule": "dangling_mention",
            "action": "link it or drop the mention",
            "cluster": None,
            "cluster_label": None,
            "nodes": [{"id": "docs:doc:a", "kind": "document", "name": "A"}],
            "detail": {"mentions": 2, "samples": ["see the runbook"]},
            "weight": 3,
        },
        {
            "fingerprint": "r:fedcba9876543210",
            "rule": "bulk_noise",
            "action": "check it",
            "cluster": None,
            "cluster_label": None,
            "nodes": [{"id": "docs:doc:b", "kind": "document", "name": "B"}],
            "detail": {},
            "weight": 1,
        },
    ],
    "props": {"docs:doc:a": {}, "docs:doc:b": {"text": "the text of B"}},
}


@pytest.fixture
def graph(monkeypatch):
    """The newest graph of every project as a small queue and two nodes: docs:doc:a, and an edge between them."""
    seen = {
        "found": {"docs:doc:a": "visible", "docs:doc:b": "visible", ("docs:doc:a", "mentions", "docs:doc:b"): "visible"}
    }

    async def fake(conn, config, project_id, project, rules, sink, reader):
        seen["sink"] = sink
        if reader is decision._queue:
            return QUEUE, None
        return {key: value for key, value in seen["found"].items()}, None

    monkeypatch.setattr(decision, "_graph", fake)
    return seen


def test_decision_api_reads_the_open_review_items_with_a_snippet_and_dismisses_as_the_plugin(
    scribe, client, hub, hub_db, graph
):
    config = client.app.state.config
    found = on_hub(client, lambda conn: decision.read_review_queue(conn, scribe, PROJECT, config))
    assert graph["sink"] == SINK
    assert [(i.fingerprint, i.snippet) for i in found.items] == [
        ("r:0123456789abcdef", "see the runbook"),
        ("r:fedcba9876543210", "the text of B"),
    ]
    assert found.items[0].nodes[0].name == "A"
    done = on_hub(
        client,
        lambda conn: decision.dismiss_review_item(
            conn, scribe, PROJECT, config, "r:0123456789abcdef", "  two names of one thing  "
        ),
    )
    assert (done.fingerprint, done.rule) == ("r:0123456789abcdef", "dangling_mention")
    d = tables.kg_review_dismissals
    assert sql(hub_db, select(d.c.reason, d.c.dismissed_by, d.c.dismissed_by_plugin)) == [
        ("two names of one thing", None, PLUGIN)
    ]
    again = on_hub(client, lambda conn: decision.read_review_queue(conn, scribe, PROJECT, config))
    assert [i.fingerprint for i in again.items] == ["r:fedcba9876543210"]
    a = tables.audit
    assert any(PLUGIN in t for (t,) in sql(hub_db, select(a.c.target).where(a.c.action == audit.KG_REVIEW)))
    listed = client.get(f"/v1/kg/{PROJECT}/review", headers=hub["owner"])
    if listed.status_code == 200:  # the web's list names the plugin as the one who dismissed it
        for item in listed.json().get("items", []):
            if item["fingerprint"] == "r:0123456789abcdef":
                assert item["dismissal"]["by"] == f"plugin:{PLUGIN}"
    with pytest.raises(hub_api.Refused) as refused:
        on_hub(client, lambda conn: decision.dismiss_review_item(conn, scribe, PROJECT, config, "r:nope", "x"))
    assert refused.value.status_code == 404 or refused.value.status_code == 403


def test_decision_api_reads_the_review_queue_of_the_named_rules_and_the_limit_counts_after_the_filter(
    scribe, client, graph, monkeypatch
):
    config = client.app.state.config

    def item(rule, n):
        return {**QUEUE["items"][1], "rule": rule, "fingerprint": f"r:{rule}{n}", "nodes": []}

    queue = [item("dangling_mention", n) for n in range(4)] + [item("possible_duplicate", n) for n in range(3)]
    monkeypatch.setitem(QUEUE, "items", queue)

    def read(**kwargs):
        return on_hub(client, lambda conn: decision.read_review_queue(conn, scribe, PROJECT, config, **kwargs))

    found = read(rules=("possible_duplicate",), limit=2)  # the first two items overall are another rule's
    assert [(i.rule, i.fingerprint) for i in found.items] == [
        ("possible_duplicate", "r:possible_duplicate0"),
        ("possible_duplicate", "r:possible_duplicate1"),
    ]
    assert len(read(rules=["possible_duplicate"]).items) == 3
    assert {i.rule for i in read(rules=("possible_duplicate", "bulk_noise")).items} == {"possible_duplicate"}
    assert len(read().items) == 7  # no rules: the whole queue, as before
    assert len(read(rules=None, limit=5).items) == 5
    for bad in (("no_such_rule",), ("possible_duplicate", "nope"), (), "possible_duplicate", [3]):
        with pytest.raises(ValueError, match="rules"):
            read(rules=bad)


def test_decision_api_takes_a_decision_on_the_graph_as_the_plugin_and_a_writer_undoes_it(
    scribe, client, hub, hub_db, graph
):
    config = client.app.state.config
    taken = on_hub(
        client,
        lambda conn: decision.decide_graph(
            conn, scribe, PROJECT, config, "deprecate", "docs:doc:a", reason="replaced by b"
        ),
    )
    assert (taken.number, taken.kind, taken.state, taken.rev) == (1, "deprecate", "active", 1)
    proposed = on_hub(
        client,
        lambda conn: decision.decide_graph(
            conn, scribe, PROJECT, config, "merge", "docs:doc:a", dst="docs:doc:b", reason="one thing", propose=True
        ),
    )
    assert (proposed.number, proposed.state) == (2, "proposed")
    with pytest.raises(hub_api.Refused) as refused:  # one that says the same
        on_hub(
            client,
            lambda conn: decision.decide_graph(
                conn, scribe, PROJECT, config, "deprecate", "docs:doc:a", reason="again"
            ),
        )
    assert refused.value.status_code == 409
    graph["found"]["docs:doc:b"] = "hidden"
    with pytest.raises(hub_api.Refused) as refused:  # a node the sink cannot read is not there
        on_hub(
            client,
            lambda conn: decision.decide_graph(
                conn, scribe, PROJECT, config, "deprecate", "docs:doc:b", reason="not visible"
            ),
        )
    assert refused.value.status_code == 404
    e = tables.kg_decision_events
    assert sql(hub_db, select(e.c.rev, e.c.action, e.c.actor_id, e.c.actor_plugin).order_by(e.c.rev)) == [
        (1, "decide", None, PLUGIN),
        (2, "propose", None, PLUGIN),
    ]
    a = tables.audit
    targets = [t for (t,) in sql(hub_db, select(a.c.target).where(a.c.action == audit.KG_DECISION))]
    assert targets == [
        f"{PROJECT} decision=1 action=decide plugin={PLUGIN}",
        f"{PROJECT} decision=2 action=propose plugin={PLUGIN}",
    ]
    for bad in (
        {"kind": "rename", "src": "x"},
        {"kind": "merge", "src": "x"},
        {"kind": "link", "src": "x", "dst": "y"},
    ):
        with pytest.raises(ValueError):
            on_hub(
                client, lambda conn, bad=bad: decision.decide_graph(conn, scribe, PROJECT, config, reason="r", **bad)
            )
