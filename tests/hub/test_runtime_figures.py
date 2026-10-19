"""The runtime and the harness of the night's figures on the hub (``evo_agents.hub.server.runtime_figures``), the
lenses runtime and harness with their metrics in the ledger, and schema 0058.

Step 9 of plan history-driven-curator, criterion a8: curator.collect counts, from the facets of the sessions and the
runs and from the runs' own usage, the tokens by source, the shares of the prompt cache, the cost of each session, run
and merged pull request, the peak context, the compactions, the median and the 95th percentile of each hook and tool,
and the size of the always-loaded files of each repo; the spool the digests say (plan lean-cached-context) is used, not
measured again; a finding and a proposal of each lens are kept, with metrics in the ledger; schema 0058 goes down and
up. Criterion a13: no command, flag or key changes. The api runs in this process (TestClient), as in
``tests.hub.test_review_runs``, whose fixture ``review`` holds the project, its charter and a digest."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from evo_agents.hub import facets, tables
from evo_agents.hub.migrate import migrate
from evo_agents.hub.server.collect import compute_figures
from evo_agents.hub.server.curator import _owner_access
from tests.hub.live import sql
from tests.hub.test_curator import OWNER, PROJECT, user_id
from tests.hub.test_curator import night as night  # noqa: F401 (a fixture)
from tests.hub.test_migrate import move_to
from tests.hub.test_plan_runs import plan_body as fleet_body
from tests.hub.test_plan_runs import push
from tests.hub.test_review_runs import SESSION as DIGESTED
from tests.hub.test_review_runs import (
    digest_body,
    finding,
    held_review,
    proposed,
    push_digest,
    review,  # noqa: F401 (a fixture)
)
from tests.hub.test_runs import web_client  # noqa: F401 (a fixture)

SHIPPED = "shipped"
HEAVY = "0199a3c1-0000-7000-8000-00000000e001"
LIGHT = "0199a3c1-0000-7000-8000-00000000e002"
STOP = "evo-agents hub hook stop"
MCP_TOOL = "mcp__plugin_evo-hub_evo-hub__plan_show"


def buckets(*pairs: tuple[int, int]) -> list[int]:
    found = [0] * (len(facets.BUCKETS_MS) + 1)
    for ms, n in pairs:
        found[facets.bucket(ms)] += n
    return found


def session_body(read: int, write: int, *, cost: float | None, peak: int, agents: int, hook_ms: int) -> dict:
    """The facet of a Claude Code session of the project: its tokens, cost and context, a compaction, a hook, a tool,
    the sources of its context and its always-loaded files."""
    return {
        "messages": 20,
        "model": "claude-opus-5-5",
        "tools": [
            {"gen_ai.tool.name": "Bash", "calls": 4, "errors": 0, "output_chars": 8000, "timed": 4, "ms": 1200},
            {"gen_ai.tool.name": MCP_TOOL, "calls": 1, "errors": 0, "output_chars": 2000, "timed": 1, "ms": 200},
        ],
        "errors": [],
        "user_turns": [],
        "commands": [],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 400,
            "cache_creation_input_tokens": write,
            "cache_read_input_tokens": read,
        },
        "cost_usd": cost,
        "context": {"first": 30_000, "peak": peak},
        "compactions": 1,
        "compaction": {"auto": 1, "largest_tokens": 160_000},
        "hooks": [
            {
                "hook": "Stop",
                "command": STOP,
                "runs": 2,
                "timed": 2,
                "ms": hook_ms,
                "max_ms": hook_ms,
                "buckets": buckets((hook_ms // 2, 2)),
                "failed": 0,
                "timed_out": 0,
            }
        ],
        "sources": {"always_loaded": agents + 400, "mcp_instructions": 1600, "skills": 8000, "hooks": 400},
        "loaded": [
            {"path": "~/src/evo-agents/AGENTS.md", "type": "Project", "chars": agents, "loads": 1},
            {"path": "~/.claude/CLAUDE.md", "type": "User", "chars": 400, "loads": 1},
        ],
    }


def label_of(db) -> dict:
    d = tables.session_digests
    return sql(db, select(d.c.label).where(d.c.session_id == DIGESTED))[0][0]


def project_id(db) -> int:
    return sql(db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]


def add_facet(db, **values) -> None:
    base = {"project_id": project_id(db), "version": facets.FACET_VERSION, "summary": {}, "at": func.now()}
    sql(db, insert(tables.session_facets).values(**base, **values))


def ended_run(review, plan_id: str, usage: dict, body: dict | None = None, *, hours: int = 1) -> int:
    """A step run of ``plan_id`` that ended ``hours`` ago on review's worker with ``usage``, and its facet ``body``."""
    db, r = review.db, tables.runs
    revision = sql(
        db, select(func.max(tables.plan_revisions.c.revision)).where(tables.plan_revisions.c.plan_id == plan_id)
    )
    values = {
        "worker_id": review.worker["id"],
        "leased_at": func.now() - timedelta(hours=hours + 1),
        "project_id": project_id(db),
        "plan_id": plan_id,
        "plan_revision": revision[0][0],
        "step_key": "2",
        "repo": "evo-agents",
        "kind": "step",
        "dispatched_by": user_id(db, OWNER),
        "requested_runtime": "claude-code",
        "runtime": "claude-code",
        "mode": "headless",
        "approval": "auto",
        "timeout_s": 600,
        "state": "done",
        "usage": usage,
        "started_at": func.now() - timedelta(hours=hours + 1),
        "finished_at": func.now() - timedelta(hours=hours),
    }
    run_id = sql(db, insert(r).values(**values, event_seq=1).returning(r.c.id))[0][0]
    sql(db, insert(tables.run_events).values(run_id=run_id, seq=1, kind="system", body={"text": "queued"}))
    if body is not None:
        add_facet(db, kind="run", run_id=run_id, source="events", body=body)
    return run_id


@pytest.fixture
def busy(review):  # noqa: F811
    """review's project with two Claude Code sessions whose facets say their runtime and harness (one pushed its
    digest too, with the spool), the digest of review (no tokens), a run of plan shipped whose repo evo-agents merged
    yesterday as pull request 41 (agent-skills a month ago), and a run of that plan that ended ten days ago."""
    db, owner = review.db, review.headers["owner"]
    label = label_of(db)
    heavy = session_body(9000, 1000, cost=1.5, peak=150_000, agents=4000, hook_ms=3000)
    light = session_body(1000, 500, cost=None, peak=60_000, agents=4400, hook_ms=600)
    for session, body in ((HEAVY, heavy), (LIGHT, light)):
        add_facet(db, kind="session", session_id=session, source="transcript", label=label, body=body)
    review.spool = {"spool_results": 2, "spool_original_chars": 50_000, "spool_reads": 1}
    push_digest(review.client, owner, HEAVY, {**digest_body(), **review.spool})
    now = sql(db, select(func.now()))[0][0]
    plan = fleet_body(SHIPPED)
    plan["repos"][0] = {**plan["repos"][0], "status": "merged", "merged_at": (now - timedelta(days=1)).isoformat()}
    plan["repos"][0]["pr"] = 41
    plan["repos"][1] = {**plan["repos"][1], "status": "merged", "merged_at": (now - timedelta(days=30)).isoformat()}
    push(review.client, owner, plan)
    usage = {
        "total_cost_usd": 0.6,
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_read_input_tokens": 300,
        "cache_creation_input_tokens": 50,
        "peak_context_tokens": 40_000,
        "first_request_cache_read_input_tokens": 0,
        "first_request_cache_creation_input_tokens": 9000,
    }
    tools = [{"gen_ai.tool.name": "Bash", "calls": 1, "timed": 1, "ms": 40_000, "buckets": buckets((40_000, 1))}]
    review.shipped_run = ended_run(review, SHIPPED, usage, {"tools": tools, "compactions": 2, "hooks": []})
    review.older_run = ended_run(review, SHIPPED, {"total_cost_usd": 0.3, "input_tokens": 5}, hours=24 * 10)
    return review


def test_the_nights_figures_hold_the_runtime_and_the_harness_of_the_sessions_and_runs(busy):
    db = busy.db
    held_review(busy)
    ((figures,),) = sql(db, select(tables.curator_figures.c.figures))
    runtime, harness = figures["runtime"], figures["harness"]

    sessions = runtime["sessions"]
    assert sessions["counted"] == 2, "the digest of review says no tokens: it is not counted as 0"
    assert sessions["tokens"] == {
        "input": 200,
        "output": 800,
        "cache_write": 1500,
        "cache_read": 10_000,
        "total": 12_500,
    }
    assert (sessions["cache_read_share"], sessions["priced"], sessions["cost_per_session"]) == (0.8547, 1, 1.5)
    assert sessions["context"]["peak_max"] == 150_000 and sessions["context"]["peak_p50"] == 60_000
    assert sessions["compactions"]["total"] == 2
    assert [entry["evidence"] for entry in sessions["heaviest"]] == [f"session:{HEAVY}", f"session:{LIGHT}"]

    runs = runtime["runs"]
    assert runs["counted"] == 1 and runs["cost_usd"] == 0.6, "a run that ended before the span is left out"
    assert runs["first_request"] == {
        "runs": 1,
        "cache_read_p50": 0,
        "cache_write_p50": 9000,
        "cache_write_tokens": 9000,
        "cold": 1,
    }
    assert runs["compactions"]["total"] == 2 and runs["heaviest"][0]["evidence"] == f"run:{busy.shipped_run}:1"
    merged = runtime["merged_prs"]
    assert (merged["prs"], merged["runs"], merged["cost_usd"], merged["cost_per_pr"]) == (1, 2, 0.9, 0.9), (
        "a pull request costs every run of its plan"
    )
    assert merged["entries"][0]["plan_id"] == SHIPPED and merged["entries"][0]["prs"][0]["pr"] == 41, (
        "the repo that merged a month ago is out of the span"
    )
    assert runtime["hook_ms"] == 3600
    stop = runtime["hooks"][0]
    assert (stop["hook"], stop["command"], stop["timed"], stop["p95_ms"], stop["sessions"]) == (
        "Stop",
        STOP,
        4,
        3000,
        2,
    )
    assert runtime["tools"][0]["tool"] == "Bash" and runtime["tools"][0]["runs"] == 1

    assert harness["sessions"] == 2
    sources = {entry["source"]: entry for entry in harness["sources"]}
    assert sources["always_loaded"]["tokens"] == (4400 + 4800) // 4 and sources["skills"]["tokens"] == 4000
    assert sources["mcp_output"]["chars"] == 4000 and sources["tool_output"]["chars"] == 16_000
    loaded = harness["always_loaded"]
    assert [(entry["repo"], entry["dir"], entry["chars"]) for entry in loaded["repos"]] == [
        ("evo-agents", None, 4400),
        (None, "~/.claude", 400),
    ], "a directory named for a repo of the project names its repo"
    assert harness["spool"] == {"digests": 1, **busy.spool}, "the digest of review said no spool"

    owner = user_id(db, OWNER)
    until = sql(db, select(func.now()))[0][0]

    async def counted():
        async with busy.client.app.state.engine.connect() as conn:
            access = await _owner_access(conn, owner, OWNER, PROJECT)
            return await compute_figures(conn, access, until - timedelta(days=7), until, owner)

    first, second = busy.client.portal.call(counted), busy.client.portal.call(counted)
    assert json.dumps([first["runtime"], first["harness"]], sort_keys=True) == json.dumps(
        [second["runtime"], second["harness"]], sort_keys=True
    ), "the same data, the same figures"
    assert first["runtime"]["sessions"] == runtime["sessions"] and first["harness"] == harness


# The lenses


def ledger_metrics(review, proposal_id: int) -> dict:
    response = review.client.get(
        f"/v1/projects/{PROJECT}/curator/proposals/{proposal_id}/ledger", headers=review.headers["owner"]
    )
    assert response.status_code == 200, response.text
    return {item["key"]: item["value"] for item in response.json()["lines"][0]["figures"]["metrics"]}


def test_a_finding_and_a_proposal_of_the_lenses_runtime_and_harness_carry_their_metrics(busy):
    run_id = held_review(busy)
    client, worker = busy.client, busy.worker
    made = finding(
        client,
        worker,
        run_id,
        lens="runtime",
        title="The Stop hook takes 3 s in heavy sessions",
        evidence=[{"kind": "session", "session_id": HEAVY}, {"kind": "run", "run_id": busy.shipped_run, "seq": 1}],
    )
    assert made.status_code == 201, made.text
    assert [item["resolved"] for item in made.json()["evidence"]] == ["facet", "run_event"]
    slow = proposed(client, worker, run_id, lens="runtime", evidence=[{"kind": "session", "session_id": HEAVY}])
    assert ledger_metrics(busy, slow["id"]) == {
        "session_cost_usd": 1.5,
        "cache_write_tokens": 1550.0,
        "context_peak_p95": 150_000.0,
        "compactions": 4.0,
        "hook_ms": 3600.0,
    }
    fixed = proposed(
        client,
        worker,
        run_id,
        lens="harness",
        title="One skill list a session",
        evidence=[{"kind": "session", "session_id": LIGHT}],
    )
    assert ledger_metrics(busy, fixed["id"]) == {
        "always_loaded_tokens": 1200.0,
        "fixed_context_tokens": 7300.0,
        "tool_output_tokens": 5000.0,
    }


# Schema 0058


def test_schema_0058_takes_the_two_lenses_and_going_back_deletes_what_they_wrote(busy):
    run_id = held_review(busy)
    client, worker = busy.client, busy.worker
    evidence = [{"kind": "run", "run_id": run_id, "seq": 1}]
    for lens in ("runtime", "harness", "cost"):
        made = finding(client, worker, run_id, lens=lens, title=f"a finding of {lens}", evidence=evidence)
        assert made.status_code == 201, made.text
    proposed(client, worker, run_id, lens="harness", evidence=evidence)

    move_to(busy.db, "0057", down=True)
    assert sql(busy.db, select(tables.findings.c.lens)) == [("cost",)]
    assert sql(busy.db, select(func.count()).select_from(tables.proposals))[0][0] == 0
    with pytest.raises(IntegrityError, match="findings_lens_check"):  # the check of 0057 refuses the lens
        finding(client, worker, run_id, lens="runtime", title="again", evidence=evidence)
    assert migrate(busy.db.dsn).applied == ("0058",)
    again = finding(client, worker, run_id, lens="runtime", title="again", evidence=evidence)
    assert again.status_code == 201, again.text
