"""The kg_init run on the hub (plan kg-data-profile-curator, criteria a5 and a17): when one is asked for, what it costs
and what the Curator reads without the database, and on Postgres the run a writer asks for, the triggers of a build,
the phases ``kg_init.advance`` moves a run through, the prompt (no item text), and the decision it records."""

import pytest

from evo_agents.hub import kg_init as model
from tests.hub import pg

# Without the database


def profile(lines: int = 1000, sources=("repo",), languages=None, share=None) -> dict:
    languages = languages or {"python": lines}
    return {
        "items": 10, "content_items": 9, "lines": lines, "chars": lines * 30, "nodes": {"File": 10}, "edges": 4,
        "classes": {"code": {"item_share": 1.0, "share": 1.0, "lines": lines}},
        "languages": {k: {"files": 3, "lines": v, "calls_ratio": 0.1, "inherits_ratio": 0.0}
                      for k, v in languages.items()},
        "length": {"p50": 10, "p90": 20, "p99": 30, "max": 40, "passages": 10, "nodes_with_passages": 9},
        "natural_language": {"share": share or {}}, "identifier_candidates": [],
        "sensitive": {}, "mentions": {"mentions": 4, "resolved": 3, "resolved_ratio": 0.75, "dangling": 1},
        "noise": {"ratio": 0.0, "binary": 0, "tiny": 0}, "history": {"commits": 2, "pull_requests": 1},
        "sources": [{"id": s, "items": 5, "lines": lines // len(sources)} for s in sources],
    }  # fmt: skip


def test_the_first_build_of_a_project_with_a_config_asks_for_a_run():
    now = profile()
    assert model.wants_run(has_config=True, first_build=True, last_profile=None, profile=now) == [model.FIRST_BUILD]
    assert model.wants_run(has_config=False, first_build=True, last_profile=None, profile=now) == []
    assert model.wants_run(has_config=True, first_build=True, last_profile=None, profile=None) == []


def test_a_project_that_never_had_a_run_waits_to_be_called():
    assert model.wants_run(has_config=True, first_build=False, last_profile=None, profile=profile()) == []


def test_a_profile_that_moved_far_asks_again_and_one_that_did_not_does_not():
    before = profile()
    same = profile(lines=1100)
    assert model.wants_run(has_config=True, first_build=False, last_profile=before, profile=same) == []
    added = profile(sources=("repo", "docs"))
    why = model.wants_run(has_config=True, first_build=False, last_profile=before, profile=added)
    assert why == ["sources added: docs"]
    doubled = profile(lines=2000)
    assert "content lines went from 1000 to 2000" in model.why_changed(before, doubled)[0]
    other = profile(languages={"python": 500, "markdown": 500})
    assert model.why_changed(before, other) == ["now at least 10% of the content lines: language:markdown"]
    vi = profile(share={"vi": 0.4})
    assert model.why_changed(before, vi) == ["now at least 10% of the content lines: text:vi"]


def test_the_budget_is_the_charters_review_cap_under_the_hub_default():
    assert model.budget(None)["max_usd"] == model.HUB_CAP_USD
    assert model.budget({"review": {"budget_usd": 0.5}})["max_usd"] == 0.5
    assert model.budget({"review": {"budget_usd": 9}})["max_usd"] == model.HUB_CAP_USD
    assert model.budget({}, {"max_usd": 1, "max_turns": 5})["max_turns"] == 5


def test_the_last_optimize_figures_go_into_the_prompt_for_the_curator_to_judge():
    assert model.previous_optimize(None) == [] and model.previous_optimize({"optimize": {}}) == []
    lines = model.previous_optimize({"optimize": {"before": {"n": 4, "hit@5": 60.0}, "after": {"n": 4, "hit@5": 50.0},
                                                  "changes": ["weight 1 to 2"]}})  # fmt: skip
    text = "\n".join(lines)
    assert "you decide to keep or to revert" in text and "hit@5 60.0%" in text and "weight 1 to 2" in text


def test_the_prompt_holds_the_profile_and_the_decision_help_and_only_the_names_given():
    text = model.prompt(
        project="demo", run_id=3, why=[model.FIRST_BUILD], profile=profile(), coverage={"sources": 1}, strategies=[],
        measures={"words": {"n": 4, "hit@5": 50.0}, "meaning": None}, settings={"semantic": "off"},
        samples=["pkg/ok.py"],
    )  # fmt: skip
    assert "items 10" in text and "## Your decision" in text and "--decide FILE" in text and "pkg/ok.py" in text
    assert "secret" not in text


def test_the_summary_and_the_report_say_what_was_decided():
    assert model.summary("demo", "queued", None) == "KG init of demo is queued"
    assert model.summary("demo", "failed", {"error": "boom"}) == "KG init of demo failed: boom"
    decision = {
        "strategy": {"use": "documents"},
        "reasons": [{"about": "strategy", "why": "prose", "figures": {"x": 1}}],
    }
    body = {"decision": decision, "applied": {"strategy": {"name": "documents"}}}
    assert model.summary("demo", "done", body) == "KG init of demo decided strategy"
    assert model.summary("demo", "done", {}) == "KG init of demo decided to change nothing"
    text = model.report("demo", 3, ["asked"], "done", body, None)
    assert text.startswith("# KG init #3 of demo: done") and "- strategy: prose (x 1)" in text
    assert "applied strategy" in text


# On Postgres

if pg.DSN:
    from functools import partial
    from types import SimpleNamespace

    from sqlalchemy import func, select, update

    from evo_agents.hub import kg_init_db, kg_settings, runs, tables
    from evo_agents.hub.server import kg_init
    from evo_agents.hub.server.collect import review_prompt
    from tests.hub.live import sql
    from tests.hub.test_curator import PROJECT, count, night, set_run  # noqa: F401 (night is a fixture)
    from tests.hub.test_kg_optimize import built, configured, project_id
    from tests.hub.test_review_runs import review  # noqa: F401 (a fixture)
    from tests.hub.test_runs import web_client  # noqa: F401 (a fixture)

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
INIT = f"/v1/kg/{PROJECT if pg.DSN else 'x'}/init"
SECRET = "secret-customer-ledger.py"


def init_row(db, run_id: int | None = None) -> dict:
    t = tables.kg_init_runs
    query = select(t).order_by(t.c.id.desc()).limit(1) if run_id is None else select(t).where(t.c.id == run_id)
    (row,) = sql(db, query)
    return dict(zip(t.c.keys(), row, strict=True))


def with_profile(db, pid: int, value: dict) -> int:
    """A succeeded build of the project whose coverage holds ``value`` as its profile."""
    build = built(db, pid)
    b = tables.kg_builds
    sql(db, update(b).where(b.c.id == build).values(coverage={"sources": 1, "profile": value}))
    return build


async def starter(engine, row, blobs, data_dir):
    return {"words": {"n": 20, "hit@5": 55.0, "tokens_per_hit": 90.0, "p95_ms": 12.0}, "meaning": None}


def advanced(world, measure=starter) -> dict:
    engine = world.client.app.state.engine
    return world.client.portal.call(partial(kg_init.advance, engine, measure=measure))


@needs_pg
def test_a_writer_starts_a_run_and_a_second_call_returns_the_one_in_flight(review):
    client, db, headers = review.client, review.db, review.headers
    refused = client.post(INIT, headers=headers["owner"])
    assert refused.status_code == 409 and "no knowledge config" in refused.json()["message"]
    configured(review)
    assert client.post(INIT, headers=headers["reader"]).status_code == 403
    first = client.post(INIT, headers=headers["owner"])
    assert first.status_code == 201, first.text
    assert (first.json()["via"], first.json()["state"]) == ("manual", "queued")  # a project with no nodes is queued
    again = client.post(INIT, headers=headers["other"])
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
    listed = client.get(INIT, headers=headers["reader"]).json()
    assert listed["active"]["id"] == first.json()["id"] and count(db, tables.kg_init_runs) == 1


@needs_pg
def test_the_first_build_queues_a_run_a_far_profile_queues_another_and_an_older_project_none(review):
    db = review.db
    configured(review)  # the build of configured() holds no profile: no run
    pid = project_id(db)
    assert count(db, tables.kg_init_runs) == 0

    async def on_build(build_id, coverage):
        async with review.client.app.state.engine.begin() as conn:
            return await kg_init_db.on_build(conn, pid, build_id, coverage)

    call = review.client.portal.call
    sql(db, tables.kg_builds.delete())
    first = built(db, pid)
    assert call(partial(on_build, first, {"profile": profile()})) is not None
    row = init_row(db)
    assert row["via"] == "first_build" and row["state"] == "queued" and row["profile"]["lines"] == 1000
    second = built(db, pid)
    assert call(partial(on_build, second, {"profile": profile(lines=3000)})) is None  # one in flight
    sql(db, update(tables.kg_init_runs).values(state="done"))
    third = built(db, pid)
    assert call(partial(on_build, third, {"profile": profile(lines=1050)})) is None  # not far
    fourth = built(db, pid)
    assert call(partial(on_build, fourth, {"profile": profile(lines=3000)})) is not None
    assert init_row(db)["via"] == "profile"
    sql(db, tables.kg_init_runs.delete())
    fifth = built(db, pid)
    assert call(partial(on_build, fifth, {"profile": profile()})) is None  # earlier builds, no run: wait to be called


@needs_pg
def test_a_run_is_measured_and_queued_as_a_review_run_whose_prompt_holds_the_profile_and_no_item_text(review):
    client, db, owner = review.client, review.db, review.headers["owner"]
    pid = project_id(db)
    configured(review)
    with_profile(db, pid, profile())
    run_id = client.post(INIT, headers=owner).json()["id"]
    assert advanced(review) == {"queued": 1}
    row = init_row(db, run_id)
    assert row["state"] == "running" and row["agent_run_id"] and row["body"]["measures"]["words"]["hit@5"] == 55.0
    r = tables.runs
    (run,) = sql(db, select(r.c.kind, r.c.dispatched_via, r.c.schedule_id, r.c.budget, r.c.state).where(
        r.c.id == row["agent_run_id"]))  # fmt: skip
    assert (run[0], run[1], run[2], run[4]) == ("review", "machine", None, "queued")
    assert run[3]["max_usd"] == model.HUB_CAP_USD or run[3]["max_usd"] <= model.HUB_CAP_USD

    async def prompt():
        view = SimpleNamespace(id=row["agent_run_id"], project=PROJECT, repos=[])
        async with client.app.state.engine.begin() as conn:
            return await review_prompt(conn, view)

    text = client.portal.call(prompt)
    assert "items 10" in text and "## Your decision" in text and "hit@5 55.0%" in text
    assert SECRET not in text and "kg_init" in text
    assert advanced(review) == {"running": 1}  # the agent run is still queued: the run waits


@needs_pg
def test_a_project_with_no_repo_is_reviewed_with_none_by_a_daemon_that_runs_such_a_review(review):
    """A project fed by uploads alone has no repo: its KG init waits for a daemon that says it reviews with none."""
    from tests.hub.test_review_runs import REVIEW_KINDS

    client, db, owner = review.client, review.db, review.headers["owner"]
    pid = project_id(db)
    configured(review)
    with_profile(db, pid, profile())
    sql(db, tables.project_repos.delete().where(tables.project_repos.c.project_id == pid))
    run_id = client.post(INIT, headers=owner).json()["id"]
    assert advanced(review) == {"waiting": 1}  # no worker fits it yet
    row = init_row(db, run_id)
    assert row["agent_run_id"] is None and "runs no review with no repo" in row["body"]["notes"][-1]
    worker = review.worker
    body = {"runtimes": worker["runtimes"], "checkouts": {}, "free_slots": 1, "runs": [], "agent_version": "0.7.0",
            "run_kinds": REVIEW_KINDS, "features": [runs.REPOLESS_REVIEW]}  # fmt: skip
    assert client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"]).status_code == 200
    advanced(review)
    row = init_row(db, run_id)
    assert row["state"] == "running" and row["agent_run_id"]
    r = tables.runs
    ((kind, repos),) = sql(db, select(r.c.kind, r.c.repos).where(r.c.id == row["agent_run_id"]))
    assert (kind, repos) == ("review", None)


@needs_pg
def test_a_decision_is_validated_applied_with_its_reasons_and_taken_once(review):
    client, db, owner = review.client, review.db, review.headers["owner"]
    pid = project_id(db)
    configured(review)
    with_profile(db, pid, profile())
    run_id = client.post(INIT, headers=owner).json()["id"]
    url = f"{INIT}/{run_id}/decision"
    bare = client.post(url, headers=owner, json={"decision": {"settings": {"semantic_mode": "index"}}})
    assert bare.status_code == 422 and "cites figures" in bare.json()["message"]
    bad = client.post(url, headers=owner, json={"decision": {"strategy": {"use": "nope"}, "reasons": [
        {"about": "strategy", "why": "x", "figures": {"a": 1}}]}})  # fmt: skip
    assert bad.status_code == 422 and "no built-in strategy" in bad.json()["message"]
    assert client.post(url, headers=review.headers["reader"], json={"decision": {}}).status_code == 403
    decision = {"settings": {"semantic_mode": "index"},
                "reasons": [{"about": "settings", "why": "prose is a third of the lines", "figures": {"prose": 0.33}}]}  # fmt: skip
    done = client.post(url, headers=owner, json={"decision": decision})
    assert done.status_code == 200, done.text
    assert done.json()["applied"]["settings"] == {"semantic_mode": "index"}
    assert init_row(db, run_id)["body"]["decision"]["settings"] == {"semantic_mode": "index"}

    async def read():
        async with client.app.state.engine.begin() as conn:
            return await kg_settings.read(conn, pid)

    assert client.portal.call(read).semantic_mode == "index"
    assert client.post(url, headers=owner, json={"decision": decision}).status_code == 409
    assert client.post(f"{INIT}/9999/decision", headers=owner, json={"decision": decision}).status_code == 404


@needs_pg
def test_a_run_ends_done_with_a_decision_and_failed_without_one(review):
    client, db, owner = review.client, review.db, review.headers["owner"]
    pid = project_id(db)
    configured(review)
    with_profile(db, pid, profile())
    run_id = client.post(INIT, headers=owner).json()["id"]
    advanced(review)
    agent = init_row(db, run_id)["agent_run_id"]
    set_run(db, agent, state="cancelled", finished_at=func.now())
    assert advanced(review) == {"failed": 1}
    row = init_row(db, run_id)
    assert row["state"] == "failed" and "decision" in row["error"]
    assert "failed" in row["report"]
    run_id = client.post(INIT, headers=owner).json()["id"]
    advanced(review)
    decision = {"strategy": {"use": "documents"},
                "reasons": [{"about": "strategy", "why": "prose", "figures": {"prose": 0.9}}]}  # fmt: skip
    applied = client.post(f"{INIT}/{run_id}/decision", headers=owner, json={"decision": decision})
    assert applied.status_code == 200, applied.text
    set_run(db, init_row(db, run_id)["agent_run_id"], state="cancelled", finished_at=func.now())
    assert advanced(review) == {"done": 1}
    row = init_row(db, run_id)
    assert row["state"] == "done" and "decided strategy" in client.get(INIT, headers=owner).json()["runs"][0]["summary"]


@needs_pg
def test_an_admin_asks_for_every_project_and_a_non_admin_cannot(review):
    client, db, headers = review.client, review.db, review.headers
    configured(review)
    assert client.post("/v1/admin/kg-init", headers=headers["owner"]).status_code in (201, 403)
    refused = client.post("/v1/admin/kg-init", headers=headers["reader"])
    assert refused.status_code == 403
    assert count(db, tables.kg_init_runs) in (0, 1)
