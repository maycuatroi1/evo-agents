"""The KG optimize loop (plan kg-retrieval-quality, criterion a7): the gates and the noise of
``evo_agents.hub.kg_optimize`` without the database, and on Postgres the run a writer asks for (one in flight a
project, a second call answering with it), the night's run that holds the night's review and adds the kg_health lens,
each phase ``kg_optimize.advance`` moves a run through with a measure the test gives, the undo of a run that made the
holdout worse, the report the owner gets and the morning brief's line, and the asked weight the next build reads."""

from datetime import timedelta
from functools import partial
from types import SimpleNamespace

import pytest

from evo_agents.hub import kg_optimize as model
from tests.hub import pg

# Without the database


def found(hit: float, n: int = 400, tph: float = 100.0, tpf: float = 50.0, p95: float = 200.0) -> dict:
    return {"n": n, "hit@5": hit, "tokens_per_hit": tph, "tokens_per_file": tpf, "p95_ms": p95}


def test_figures_take_hit_at_five_in_percent_and_leave_out_an_empty_row():
    row = {"n": 20, "hit@5": 0.55, "tokens_per_hit": 812.5, "tokens_per_file": 400.0, "p95_ms": 31.0, "mrr": 0.4}
    assert model.figures(row) == {"n": 20, "hit@5": 55.0, "tokens_per_hit": 812.5, "tokens_per_file": 400.0,
                                  "p95_ms": 31.0}  # fmt: skip
    assert model.figures({"n": 0}) is None and model.figures(None) is None


def test_the_noise_comes_from_the_questions_asked_with_a_floor():
    assert model.hit_noise(found(50.0, n=100)) == 10.0  # two standard errors of a proportion
    assert model.hit_noise(found(99.9, n=100000)) == model.NOISE_FLOOR_POINTS
    assert model.token_noise(found(60.0, n=400)) == pytest.approx(0.0816, abs=1e-4)
    assert model.token_noise(found(99.0, n=100000)) == model.TOKEN_NOISE_FLOOR


def test_a_regression_is_a_move_beyond_the_noise_of_before():
    before = found(60.0)  # noise 4.9 points, tokens 8.2%
    assert model.regressions(before, found(57.0)) == []
    assert model.regressions(before, found(60.0, tph=105.0)) == []
    worse = model.regressions(before, found(54.0, tph=109.0))
    assert len(worse) == 2 and worse[0].startswith("hit@5 fell from 60% to 54%")
    assert "tokens per hit" in worse[1]
    assert model.regressions(None, found(10.0)) == [] and model.regressions(before, None) == []


def test_the_semantic_gate_is_criterion_a12():
    words = found(50.0)
    on = model.semantic_gate(words, found(56.0, p95=300.0), "index")
    assert (on["decision"], on["mode"], on["gain"]) == (model.ON, "on", 6.0)
    short = model.semantic_gate(words, found(53.0), "on")
    assert (short["decision"], short["mode"]) == (model.OFF, "index")
    assert "+3 points" in short["reason"]
    assert model.semantic_gate(words, found(53.0), "off")["decision"] == model.KEEP
    costly = model.semantic_gate(words, found(60.0, tpf=60.0), "index")
    assert costly["decision"] == model.KEEP and "tokens per answer file" in costly["reason"]
    slow = model.semantic_gate(words, found(60.0, p95=1000.0), "on")
    assert slow["mode"] == "index" and "p95" in slow["reason"]
    none = model.semantic_gate(words, None, "off", "the graph holds no vectors")
    assert (none["decision"], none["mode"], none["reason"]) == (model.UNAVAILABLE, "off", "the graph holds no vectors")


def test_the_weight_moves_only_beyond_the_noise_with_enough_questions_and_labels():
    tuned = {"0": found(40.0), "0.5": found(48.0), "1": found(50.0), "2": found(60.0)}
    moved = model.best_weight(tuned, 1.0, 25)
    assert (moved["weight"], moved["changed"]) == (2.0, True)
    assert moved["tried"] == {"0": 40.0, "0.5": 48.0, "1": 50.0, "2": 60.0}
    assert model.best_weight(tuned, 1.0, 5)["changed"] is False  # too few new labels
    few = {key: {**value, "n": 10} for key, value in tuned.items()}
    assert model.best_weight(few, 1.0, 25)["changed"] is False  # too few tune questions
    close = {"1": found(50.0), "2": found(53.0)}
    assert model.best_weight(close, 1.0, 25) == {**model.best_weight(close, 1.0, 25), "weight": 1.0, "changed": False}


def test_the_report_holds_the_figures_the_decisions_and_the_undo():
    body = {
        "before": {"build_id": 3, "holdout": found(60.0), "review_open": 4, "coverage": {"sources": 2},
                   "sessions": {"click_rate": 0.42, "searches": 50, "new_labels": 21}},
        "after": {"build_id": 5, "holdout": found(50.0), "review_open": 1},
        "semantic": {"decision": "keep", "mode": "off", "reason": "no vectors", "words": found(60.0)},
        "asked": {"weight": 1.0, "reason": "the weight stays"},
        "decisions": {"numbers": [7, 8], "why": "the review queue's fixes"},
        "pulls": [{"repo": "evo-agents", "pr_url": "https://github.com/o/r/pull/9", "state": "merged"}],
        "regressions": ["hit@5 fell from 60% to 50%, beyond the noise of 4.9 points"],
        "undone": {"decisions": [7, 8], "reverts": [12], "asked_weight": 1.0, "semantic": "off"},
    }  # fmt: skip
    text = model.report_text("demo", 4, body, "reverted")
    assert text.startswith("**KG optimize of demo reverted after a regression: hit@5 60.0% to 50.0%** (KG optimize")
    for piece in ("click_rate 0.42", "21 new labels", "Search by meaning: keep", "#7, #8", "pull/9: merged",
                  "revert proposals #12", "Open review items: 4 before, 1 after", "Jev: off"):  # fmt: skip
        assert piece in text, piece
    assert "·" not in text
    assert model.summary("demo", {}, "running") == "KG optimize of demo in flight (running)"
    lines = model.prompt_lines(4, body)
    assert lines[1] == "# KG optimize run #4" and any("kg_health" in line for line in lines)


# On Postgres

if pg.DSN:
    from sqlalchemy import func, insert, select, update

    from evo_agents.hub import kg_asked, tables
    from evo_agents.hub.server import brief, kg_health, kg_optimize, kg_review
    from evo_agents.hub.server.collect import collect, review_prompt
    from tests.hub.live import sql
    from tests.hub.test_curator import NIGHT, OWNER, PROJECT, count, night, set_run, user_id  # noqa: F401
    from tests.hub.test_review_runs import collected, review  # noqa: F401 (review is a fixture)
    from tests.hub.test_runs import web_client  # noqa: F401 (a fixture)

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
KNOWLEDGE = {"project": PROJECT if pg.DSN else "x", "sources": []}
OPTIMIZE = f"/v1/kg/{PROJECT if pg.DSN else 'x'}/optimize"


def project_id(db) -> int:
    return sql(db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]


def configured(world) -> int:
    """A knowledge config and one succeeded build of ``world``'s project; the build's id."""
    db, pid = world.db, project_id(world.db)
    values = {"project_id": pid, "digest": "sha256:" + "0" * 64, "knowledge": KNOWLEDGE}
    sql(db, insert(tables.kg_configs).values(**values, updated_by=user_id(db, OWNER)))
    return built(db, pid)


def built(db, pid: int) -> int:
    b = tables.kg_builds
    values = {
        "status": "succeeded",
        "content_hash": "sha256:" + "c" * 64,
        "nodes": 9,
        "edges": 9,
        "started_at": func.now(),
    }
    values |= {"finished_at": func.now(), "artifact_sha256": "e" * 64, "artifact_size": 10, "coverage": {"sources": 1}}
    return sql(db, insert(b).values(project_id=pid, **values).returning(b.c.id))[0][0]


def optimize_row(db, run_id: int) -> dict:
    o = tables.kg_optimize_runs
    (row,) = sql(db, select(o).where(o.c.id == run_id))
    return dict(zip(o.c.keys(), row, strict=True))


class Measures:
    """A measure of the graph for ``advance``: the holdout's figures ``hits`` gives, one a call, by words alone."""

    def __init__(self, db, *hits: float):
        self.db, self.hits, self.calls = db, list(hits), []

    async def __call__(self, engine, ctx, blobs, data_dir, weight, tune):
        self.calls.append((weight, tune))
        b = tables.kg_builds
        async with engine.begin() as conn:
            build_id = await conn.scalar(
                select(func.max(b.c.id)).where(b.c.project_id == ctx.project_id, b.c.status == "succeeded")
            )
        row = {"n": 400, "hit@5": self.hits.pop(0) / 100, "tokens_per_hit": 100.0, "tokens_per_file": 50.0,
               "p95_ms": 20.0}  # fmt: skip
        figures = model.figures(row)
        return {"build_id": build_id, "words": figures, "meaning": None, "why_meaning": "the graph holds no vectors",
                "tuned": {}, "review": {"build_id": build_id, "open": {"dangling_mention": 3}}}  # fmt: skip


def advanced(world, measure, now=None) -> dict:
    return world.client.portal.call(partial(kg_optimize.advance, world.client.app.state.engine, measure=measure,
                                            now=now))  # fmt: skip


def applying(monkeypatch, numbers: list[int]) -> list:
    """kg_review.apply_review, which reads the graph, as a fake that decides ``numbers``; the calls it got."""
    calls = []

    async def apply_review(request, project, body, user):
        calls.append((project, body.origin, body.run_id, user.login))
        decisions = [SimpleNamespace(number=n) for n in numbers]
        return SimpleNamespace(rev=len(numbers), left=0, items=[SimpleNamespace(decisions=decisions)])

    monkeypatch.setattr(kg_review, "apply_review", apply_review)
    return calls


@needs_pg
def test_a_writer_starts_a_run_and_a_second_call_returns_the_one_in_flight(review):
    client, db, headers = review.client, review.db, review.headers
    refused = client.post(OPTIMIZE, headers=headers["owner"])
    assert refused.status_code == 409 and "no knowledge config" in refused.json()["message"]
    configured(review)
    assert client.post(OPTIMIZE, headers=headers["reader"]).status_code == 403
    first = client.post(OPTIMIZE, headers=headers["owner"])
    assert first.status_code == 201, first.text
    run = first.json()
    assert (run["via"], run["state"], run["project"]) == ("manual", "measuring", PROJECT)
    again = client.post(OPTIMIZE, headers=headers["other"])
    assert again.status_code == 200 and again.json()["id"] == run["id"]
    listed = client.get(OPTIMIZE, headers=headers["reader"]).json()
    assert listed["active"]["id"] == run["id"] and [item["id"] for item in listed["runs"]] == [run["id"]]
    assert count(db, tables.kg_optimize_runs) == 1


@needs_pg
def test_without_a_night_shift_nothing_starts(review):
    configured(review)
    sql(review.db, tables.schedules.delete())
    refused = review.client.post(OPTIMIZE, headers=review.headers["owner"])
    assert refused.status_code == 409 and "no night shift" in refused.json()["message"]


@needs_pg
def test_an_asked_for_run_measures_reviews_applies_measures_again_and_reports(review, monkeypatch):
    client, db, owner = review.client, review.db, review.headers["owner"]
    before = configured(review)
    run_id = client.post(OPTIMIZE, headers=owner).json()["id"]
    measure = Measures(db, 60.0, 61.0)
    assert advanced(review, measure) == {"measured": 1}
    row = optimize_row(db, run_id)
    assert row["state"] == "running" and row["before_build_id"] == before and row["agent_run_id"] is not None
    assert measure.calls == [(1.0, True)]
    body = row["body"]
    assert body["before"]["holdout"]["hit@5"] == 60.0 and body["before"]["review_open"] == 3
    assert body["before"]["coverage"] == {"sources": 1} and body["before"]["sessions"]["searches"] == 0
    assert body["semantic"]["decision"] == model.UNAVAILABLE and body["semantic"]["mode"] == "off"
    r = tables.runs
    agent = row["agent_run_id"]
    ((kind, title, state, budget),) = sql(db, select(r.c.kind, r.c.title, r.c.state, r.c.budget).where(r.c.id == agent))
    assert (kind, title, state) == ("review", f"KG optimize of {PROJECT}", "queued")
    assert budget["max_usd"] <= 0.5  # the charter's cap
    view = SimpleNamespace(id=agent, project=PROJECT, repos=[])
    prompt = client.portal.call(partial(_prompt, client.app.state.engine, view))
    assert "# KG optimize run #" in prompt and "- kg_health:" in prompt and "- environment:" not in prompt

    outside = NIGHT + timedelta(hours=10)  # 09:30 local: the night's queued runs are cancelled, not this one
    collected(client, outside)
    assert sql(db, select(r.c.state).where(r.c.id == agent))[0][0] == "queued"

    assert advanced(review, measure) == {"running": 1}
    set_run(db, agent, state="cancelled", finished_at=func.now())  # as an ended run
    calls = applying(monkeypatch, [])
    assert advanced(review, measure) == {"applied": 1}
    assert calls == [(PROJECT, "kg-optimize", agent, OWNER)]
    assert optimize_row(db, run_id)["state"] == "after"
    assert advanced(review, measure) == {"waiting": 1}  # the build queued after the review run has not run
    later = sql(db, select(func.now() + timedelta(hours=7)))[0][0]
    assert advanced(review, measure, now=later) == {"done": 1}
    row = optimize_row(db, run_id)
    assert row["state"] == "done" and row["ended_at"] is not None and row["body"]["regressions"] == []
    assert "no new build succeeded" in " ".join(row["body"]["notes"])
    n = tables.notifications
    ((user, notice, title, text),) = sql(
        db, select(n.c.user_id, n.c.notice_kind, n.c.title, n.c.body).where(n.c.notice_kind == "kg_optimize")
    )
    assert (user, notice) == (user_id(db, OWNER), "kg_optimize")
    assert title == f"KG optimize of {PROJECT} kept: hit@5 60.0% to 61.0%" and text == row["report"]
    assert row["notification_id"] is not None and "click_rate" in text

    async def facts(engine):
        async with engine.begin() as conn:
            return await kg_optimize.brief_facts(conn, project_id(db))

    shown = client.portal.call(partial(facts, client.app.state.engine))
    assert (shown["id"], shown["state"]) == (run_id, "done")
    line = brief.compose(PROJECT, {**_brief_facts(), "kg_optimize": shown}, "UTC")[1]
    assert f"KG optimize run #{run_id} (done): KG optimize of {PROJECT} kept" in line
    assert client.post(OPTIMIZE, headers=owner).status_code == 201  # none in flight: a new one


async def _prompt(engine, view) -> str:
    async with engine.begin() as conn:
        return await review_prompt(conn, view)


def _brief_facts() -> dict:
    """The facts of an empty night, as ``brief._facts`` gives them."""
    return {
        "night": "2026-10-08",
        "in_window": False,
        "window": {"start": "22:00", "end": "06:00", "timezone": "UTC"},
        "paused": False,
        "state": "idle",
        "runs": {"total": 0, "done": 0, "failed": 0, "cancelled": 0, "active": 0, "listed": []},
        "cost_usd": 0.0,
        "budget_usd": 2.0,
        "review_run": None,
        "merged": [],
        "awaiting_merge": {"total": 0, "listed": []},
        "planned_changes": {"total": 0, "listed": []},
        "decisions": {"open": 0, "listed": []},
        "proposals": {"open": 0, "in_inbox": 0, "listed": []},
        "worker": {
            "name": "night-mac",
            "online": True,
            "last_heartbeat_at": None,
            "last_heartbeat_local": None,
            "revoked": False,
            "draining": False,
        },
    }


@needs_pg
def test_a_worse_holdout_undoes_the_decisions_and_reverts_the_run(review, monkeypatch):
    client, db, owner = review.client, review.db, review.headers["owner"]
    configured(review)
    pid = project_id(db)
    d = tables.kg_decisions
    for number in (1, 2):
        sql(db, insert(d).values(project_id=pid, number=number, kind="deprecate", state="active", src=f"n{number}"))
    run_id = client.post(OPTIMIZE, headers=owner).json()["id"]
    measure = Measures(db, 60.0, 40.0)
    assert advanced(review, measure) == {"measured": 1}
    agent = optimize_row(db, run_id)["agent_run_id"]
    set_run(db, agent, state="cancelled", finished_at=func.now())
    applying(monkeypatch, [1, 2])
    assert advanced(review, measure) == {"applied": 1}
    o = tables.kg_optimize_runs
    sql(db, update(o).where(o.c.id == run_id).values(asked_weight=0.5))  # as a measure that moved the weight
    later = sql(db, select(func.now() + timedelta(hours=7)))[0][0]
    assert advanced(review, measure, now=later) == {"reverted": 1}
    row = optimize_row(db, run_id)
    assert row["state"] == "reverted" and row["body"]["regressions"][0].startswith("hit@5 fell from 60% to 40%")
    assert row["body"]["undone"]["decisions"] == [1, 2] and row["body"]["undone"]["reverts"] == []
    assert sorted(sql(db, select(d.c.number, d.c.state).where(d.c.project_id == pid))) == [(1, "undone"), (2, "undone")]
    e = tables.kg_decision_events
    events = sql(db, select(e.c.action, e.c.origin, e.c.run_id).where(e.c.project_id == pid).order_by(e.c.rev))
    assert events == [("undo", "kg-optimize", agent), ("undo", "kg-optimize", agent)]
    assert "Undone: decisions #1, #2" in row["report"]

    async def weight(engine):
        async with engine.begin() as conn:
            return await kg_asked.asked_weight(conn, pid)

    assert client.portal.call(partial(weight, client.app.state.engine)) is None  # a reverted run's weight is dropped
    sql(db, update(o).where(o.c.id == run_id).values(state="done"))
    assert client.portal.call(partial(weight, client.app.state.engine)) == 0.5


@needs_pg
def test_the_night_run_holds_the_review_until_measured_then_adds_the_kg_health_lens(review, monkeypatch, tmp_path):
    client, db = review.client, review.db
    configured(review)

    async def unmeasured(engine, **options):  # the graphs' measure and the runs' advance, which read the graph
        return None

    monkeypatch.setattr(kg_health, "measure", unmeasured)
    monkeypatch.setattr(kg_optimize, "advance", unmeasured)
    measuring = client.portal.call(partial(collect, client.app.state.engine, now=NIGHT, data_dir=tmp_path))
    assert measuring == {"collected": 1, "reopened": 0}  # the job's pass, which measures: the review waits
    o = tables.kg_optimize_runs
    ((run_id, via, night_of, state),) = sql(db, select(o.c.id, o.c.via, o.c.night, o.c.state))
    assert (via, state, night_of.isoformat()) == ("night", "measuring", "2026-10-08")
    sql(db, update(o).where(o.c.id == run_id).values(state="running"))  # as advance leaves it
    assert collected(client, NIGHT + timedelta(minutes=1)) == {"review": 1, "reopened": 0}
    cf = tables.curator_figures
    ((figures, review_run),) = sql(db, select(cf.c.figures, cf.c.run_id))
    assert "kg_health" in figures["lenses"] and len(figures["lenses"]) in (3, 4)
    assert optimize_row(db, run_id)["agent_run_id"] == review_run
    assert count(db, o) == 1  # one run a night


@needs_pg
def test_a_failing_measure_ends_the_run_failed_with_a_report(review):
    client, db, owner = review.client, review.db, review.headers["owner"]
    configured(review)
    run_id = client.post(OPTIMIZE, headers=owner).json()["id"]

    async def broken(engine, ctx, blobs, data_dir, weight, tune):
        return {"why": "the project's eval set has no holdout question"}

    assert advanced(review, broken) == {"failed": 1}
    row = optimize_row(db, run_id)
    assert row["state"] == "failed" and row["error"] == "the project's eval set has no holdout question"
    assert row["report"].startswith(f"**KG optimize of {PROJECT} failed**")
