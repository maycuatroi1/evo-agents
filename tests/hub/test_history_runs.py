"""The night's history run on the hub (``evo_agents.hub.server.history``) and schema 0059, on a hub with a blob store:
test_runs' members of project evo-agents, owner an admin with a charter whose worker on duty runs every kind of run,
and the transcripts of the owner's sessions, whose deterministic facets the job hub.analyze_facets made.

Step 4 of plan history-driven-curator, criterion a3 (its part with a model): one history run a night, after the night's
review run and never before it, on the cheapest model of the reviewer's runtime within the charter's own cap (which a
charter validates as its other caps); batches of the sessions and runs without a facet of the model, the latest first,
each an excerpt cut around its errors and corrections, read and written with the run's own key alone; a facet of
content that changed since left, an item without one waiting for the next night; the facets grouped again each night,
named and joined by the model, and only a cluster of 3 sessions or more in the night's figures; the routes and the
command line that read them. Criterion a13: only additions to the contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from psycopg import errors
from sqlalchemy import delete, func, insert, select, update

from evo_agents.hub import history, runs, tables
from evo_agents.hub.server.app import create_app
from tests.hub.contract_keys import assert_json_keys
from tests.hub.live import sql
from tests.hub.test_curator import NEXT_NIGHT, NIGHT, WORKER, charter_body, fire, grant, put_charter, written
from tests.hub.test_digest import said, session_lines
from tests.hub.test_facet_job import analyzed, later
from tests.hub.test_migrate import move_to
from tests.hub.test_plan_runs import fleet_worker, push
from tests.hub.test_plan_runs import plan_body as fleet_body
from tests.hub.test_review_runs import collected
from tests.hub.test_run_cli import as_json, cli, ok, write_credentials
from tests.hub.test_run_stream import serving
from tests.hub.test_runs import OTHER, OWNER, PROJECT, claim, members, moved, report
from tests.hub.test_session_transcripts import parts_of, send, statuses, transcript

FIRST = "0199b1c2-7d3e-7a10-9c41-00000000a001"
SECOND = "0199b1c2-7d3e-7a10-9c41-00000000a002"
LABEL = {"level": "internal", "location": "any", "integrity": "U"}


def heartbeat(client, worker: dict, models=None) -> dict:
    """``worker`` after a heartbeat that says its daemon runs every kind of run, Claude Code listing ``models``."""
    runtime = {"available": True, "version": "2.1.289", **({"models": models} if models else {})}
    body = {
        "runtimes": {"claude-code": runtime},
        "checkouts": worker["checkouts"],
        "free_slots": 1,
        "runs": [],
        "agent_version": "0.7.0",
        "run_kinds": list(runs.RUN_KINDS),
    }
    response = client.post("/v1/worker/heartbeat", json=body, headers=worker["headers"])
    assert response.status_code == 200, response.text
    return worker


def lab_charter(**changes) -> dict:
    return charter_body(night_plans=[], night_budget_usd=4.0, run_budget_usd=2.0, max_runs_per_night=10, **changes)


@pytest.fixture
def lab(hub_db, tmp_path, github, s3):
    config = live.hub_config(hub_db, tmp_path, github, **live.web_changes(github), **s3.config())
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        headers = members(client, github)
        grant(client, headers, OWNER, "admin")
        push(client, headers["owner"], fleet_body())
        worker = heartbeat(client, fleet_worker(client, headers["owner"], WORKER), ["opus", "sonnet"])
        written(client, headers["owner"], lab_charter())
        yield SimpleNamespace(client=client, headers=headers, worker=worker, db=hub_db, tmp_path=tmp_path, s3=s3)


def upload(lab, session: str, lines: list | None = None, who: str = "owner") -> None:
    path = transcript(lab.tmp_path, lines, name=f"{session}.jsonl")
    assert statuses(send(lab, who, parts_of(lab, path, session=session))) == ["stored"]


def sessions(lab) -> None:
    """Two sessions of the owner with transcripts, their deterministic facets made."""
    upload(lab, FIRST)
    upload(lab, SECOND, [*session_lines("Write the docs of the queue."), said("không, viết ngắn thôi", 20)])
    assert analyzed(lab.client, later(lab.db)) == {"transcript": 2}


def reviewed(lab, now: datetime = NIGHT) -> int:
    """The night's review run, queued by curator.collect, run and ended; its id."""
    assert collected(lab.client, now)["review"] == 1
    spec = claim(lab.client, lab.worker)
    assert spec["kind"] == "review"
    moved(lab.client, lab.worker, spec["id"], "running", "verifying", "done")
    return spec["id"]


def history_run(lab, now: datetime = NIGHT) -> dict:
    """The night's history run, queued by the night shift once the review ended, claimed and running; its spec."""
    reviewed(lab, now)
    assert fire(lab.client, now + timedelta(minutes=1)).get("history") == 1
    spec = claim(lab.client, lab.worker)
    assert spec["kind"] == "history"
    moved(lab.client, lab.worker, spec["id"], "running")
    return spec


def ask(lab, run_id: int, key: str | None, path: str = "next", **body):
    headers = {**lab.worker["headers"], **({history.HISTORY_KEY_HEADER: key} if key else {})}
    return lab.client.post(f"/v1/worker/runs/{run_id}/history/{path}", json=body, headers=headers)


def answered(response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


def facet(**changes) -> dict:
    found = {
        "summary": "Ran the tests, fixed the claim and pushed.",
        "task_type": "fix",
        "outcome": "partial",
        "friction": ["the push was rejected"],
        "failure_modes": ["FM-3.2"],
        "corrections": [],
    }
    return found | changes


def llm_of(db, session: str) -> dict:
    f = tables.session_facets
    columns = (f.c.llm, f.c.llm_version, f.c.llm_fingerprint, f.c.fingerprint, f.c.llm_run_id, f.c.llm_attempts)
    rows = sql(db, select(*columns, f.c.llm_error, f.c.llm_retry_at).where(f.c.session_id == session))
    keys = ("llm", "version", "llm_fingerprint", "fingerprint", "run_id", "attempts", "error", "retry_at")
    return dict(zip(keys, rows[0], strict=True))


# The night shift


def test_the_history_run_comes_once_a_night_right_after_the_review_run_on_the_cheapest_model(lab):
    sessions(lab)
    review_id = reviewed(lab)
    assert fire(lab.client, NIGHT + timedelta(minutes=1)) == {"history": 1}
    r = tables.runs
    columns = (r.c.id, r.c.kind, r.c.model, r.c.runtime, r.c.repos, r.c.plan_id, r.c.budget, r.c.max_attempts)
    ((run_id, kind, model, runtime, repos, plan_id, budget, attempts),) = sql(
        lab.db, select(*columns).where(r.c.kind == "history")
    )
    assert run_id > review_id and (kind, plan_id, repos, attempts) == ("history", None, None, 1)
    assert (runtime, model) == ("claude-code", "haiku"), "the reviewer's runtime, its cheapest model"
    assert budget == {"max_usd": 1.0, "max_turns": 40, "max_seconds": 1800}, "the smaller of 1 USD and a quarter"
    (target,) = sql(lab.db, select(tables.audit.c.target).where(tables.audit.c.action == "curator.history"))
    assert target[0] == f"{PROJECT} run:{run_id} night=2026-10-08"
    spec = claim(lab.client, lab.worker)
    assert spec["id"] == run_id and spec["prompt"] == history.RUN_PROMPT and spec["repos"] is None
    assert spec["curator"]["role"] == "history" and spec["curator"]["history_key"]
    assert spec["curator"]["history"] == {
        "batch_items": history.BATCH_ITEMS,
        "batch_chars": history.BATCH_CHARS,
        "batch_turns": history.BATCH_TURNS,
        "naming_share": history.NAMING_SHARE,
    }
    moved(lab.client, lab.worker, run_id, "running", "verifying", "done")
    assert fire(lab.client, NIGHT + timedelta(minutes=2)).get("history") is None, "one history run a night"
    assert collected(lab.client, NEXT_NIGHT)["review"] == 1, "the next night begins with its review"


def test_no_history_run_before_the_nights_review_run_nor_without_work_or_money(lab):
    written(lab.client, lab.headers["owner"], lab_charter(review={"budget_usd": 0.001}))  # no review run fits
    sessions(lab)
    assert fire(lab.client, NIGHT) == {"no_plan": 1}, "no review run tonight, so no history run either"
    written(lab.client, lab.headers["owner"], lab_charter(history={"budget_usd": 0}))
    reviewed(lab)
    assert fire(lab.client, NIGHT + timedelta(minutes=1)) == {"no_plan": 1}, "a cap of 0 runs none"


def test_a_night_with_nothing_to_read_queues_no_history_run(lab):
    reviewed(lab)
    assert fire(lab.client, NIGHT + timedelta(minutes=1)) == {"no_plan": 1}


def test_the_history_run_takes_the_charters_model_and_a_worker_that_lists_a_cheaper_one(lab):
    sessions(lab)
    heartbeat(lab.client, lab.worker, ["opus", "claude-haiku-5", "sonnet"])
    spec = history_run(lab)
    assert spec["model"] == "claude-haiku-5"
    sql(lab.db, update(tables.runs).values(state="done", finished_at=func.now()).where(tables.runs.c.id == spec["id"]))
    written(lab.client, lab.headers["owner"], lab_charter(history={"model": "sonnet", "budget_usd": 0.3}))
    spec = history_run(lab, NEXT_NIGHT)
    assert (spec["model"], spec["budget"]["max_usd"]) == ("sonnet", 0.3)


@pytest.mark.parametrize(
    ("changes", "words"),
    [
        ({"history": {"budget_usd": 5.0}}, "history.budget_usd is over night_budget_usd"),
        ({"history": {"budget_usd": -1}}, "greater than or equal to 0"),
        ({"history": {"cap": 1}}, "Extra inputs are not permitted"),
    ],
)
def test_a_charter_checks_the_history_runs_cap_as_its_other_caps(lab, changes, words):
    response = put_charter(lab.client, lab.headers["owner"], lab_charter(**changes))
    assert response.status_code == 422 and words in response.text, response.text


def test_the_charter_shows_the_history_settings_with_their_defaults(lab):
    shown = lab.client.get(f"/v1/projects/{PROJECT}/curator/charter", headers=lab.headers["owner"]).json()
    assert shown["history"] == {"budget_usd": None, "model": None}


# The batches and the report


def test_batches_take_the_runs_own_key_and_hand_the_latest_items_with_their_excerpts(lab):
    sessions(lab)
    spec = history_run(lab)
    run_id, key = spec["id"], spec["curator"]["history_key"]
    assert ask(lab, run_id, None).status_code == 403, "the worker token alone reads no batch"
    assert ask(lab, run_id, "not-the-key").status_code == 403
    assert ask(lab, run_id, key, path="report", facets=[]).status_code == 200
    batch = answered(ask(lab, run_id, key))
    assert [item["item"] for item in batch["items"]] == [f"session:{SECOND}", f"session:{FIRST}"], "the latest first"
    assert batch["waiting"] == 2 and batch["groups"] == [] and batch["named"] == []
    first = batch["items"][1]
    assert first["kind"] == "session" and first["source"] == "Claude Code session" and first["cut"] is False
    assert first["fingerprint"] == llm_of(lab.db, FIRST)["fingerprint"] and first["anchors"] == 2
    assert "L5 Bash failed: FAILED tests/test_queue.py::test_claim" in first["excerpt"]
    assert "user: không, viết ngắn thôi" in batch["items"][0]["excerpt"]
    other = fleet_worker(lab.client, lab.headers["other"], "other-mac")
    headers = {**other["headers"], history.HISTORY_KEY_HEADER: key}
    found = lab.client.post(f"/v1/worker/runs/{run_id}/history/next", json={}, headers=headers)
    assert found.status_code == 404, "a run another worker holds"


def test_a_report_keeps_each_facet_leaves_a_stale_one_and_lets_an_item_without_one_wait(lab):
    sessions(lab)
    spec = history_run(lab)
    run_id, key = spec["id"], spec["curator"]["history_key"]
    first, second = (item for item in answered(ask(lab, run_id, key))["items"][::-1])
    reported = answered(
        ask(
            lab,
            run_id,
            key,
            path="report",
            facets=[
                {"item": first["item"], "fingerprint": first["fingerprint"], "facet": facet(summary="x" * 900)},
                {"item": second["item"], "fingerprint": second["fingerprint"], "error": "the model gave none"},
                {"item": "session:0199b1c2-7d3e-7a10-9c41-0000000000ff", "fingerprint": "f", "error": "gone"},
            ],
        )
    )
    assert reported == {"stored": 1, "stale": 1, "failed": 1, "named": 0, "waiting": 1}
    kept = llm_of(lab.db, FIRST)
    assert kept["llm"]["failure_modes"] == ["FM-3.2"] and len(kept["llm"]["summary"]) == history.MAX_SUMMARY_CHARS
    assert (kept["version"], kept["llm_fingerprint"], kept["run_id"]) == (1, kept["fingerprint"], run_id)
    waits = llm_of(lab.db, SECOND)
    now = sql(lab.db, select(func.now()))[0][0]
    assert (waits["llm"], waits["attempts"], waits["error"]) == (None, 1, "the model gave none")
    assert now + timedelta(hours=19) < waits["retry_at"] < now + timedelta(hours=21), "the next night tries it again"
    again = answered(ask(lab, run_id, key))
    assert again["items"] == [] and again["waiting"] == 1, "nothing is due before its retry"
    stale = {"item": first["item"], "fingerprint": "an-older-content", "facet": facet()}
    assert answered(ask(lab, run_id, key, path="report", facets=[stale]))["stale"] == 1
    upload_more(lab)
    assert analyzed(lab.client, later(lab.db, 120)) == {"events": 1, "transcript": 1}, "the review run's too"
    grown = [item for item in answered(ask(lab, run_id, key))["items"] if item["kind"] == "session"]
    assert [item["item"] for item in grown] == [first["item"]], "content that changed is read again"
    bad = {"item": first["item"], "fingerprint": grown[0]["fingerprint"], "facet": {"task_type": "fix"}}
    assert answered(ask(lab, run_id, key, path="report", facets=[bad]))["failed"] == 1
    h = tables.history_runs
    ((stored, failed, stale_count),) = sql(
        lab.db, select(h.c.analyzed, h.c.failed, h.c.stale).where(h.c.run_id == run_id)
    )
    assert (stored, failed, stale_count) == (1, 2, 2)
    moved(lab.client, lab.worker, run_id, "verifying", "done")
    assert ask(lab, run_id, key).status_code == 404, "a run that ended reads no batch"


def upload_more(lab) -> None:
    """FIRST grows by a turn of the person."""
    longer = [*session_lines(), said("không, chạy lại test đó thôi", 15)]
    path = transcript(lab.tmp_path, longer, name="longer.jsonl")
    start = transcript(lab.tmp_path, name="first.jsonl").stat().st_size
    parts = parts_of(lab, path, start=start, line=len(session_lines()) + 1, session=FIRST)
    assert statuses(send(lab, "owner", parts)) == ["stored"]


def test_a_review_run_and_its_log_are_items_too_and_a_history_run_never_is(lab):
    review_id = reviewed(lab)
    assert analyzed(lab.client, later(lab.db)) == {"events": 1}
    assert fire(lab.client, NIGHT + timedelta(minutes=1)) == {"history": 1}
    spec = claim(lab.client, lab.worker)
    moved(lab.client, lab.worker, spec["id"], "running")
    (item,) = answered(ask(lab, spec["id"], spec["curator"]["history_key"]))["items"]
    assert item["item"] == f"run:{review_id}" and item["source"].startswith("a run of kind review")
    assert item["excerpt"].startswith("#")
    moved(lab.client, lab.worker, spec["id"], "verifying", "done")
    assert analyzed(lab.client, later(lab.db, 120)).get("events") == 1
    f, r = tables.session_facets, tables.runs
    kinds = sql(lab.db, select(r.c.kind).join(f, f.c.run_id == r.c.id))
    assert sorted(kind for (kind,) in kinds) == ["history", "review"], "the history run has a facet of its own"
    reader = answered(lab.client.get(f"/v1/projects/{PROJECT}/curator/history", headers=lab.headers["owner"]))
    assert reader["llm"]["items"] == 1, "but it is no item of the model"


# The clusters


def put_facets(lab, entries: list[tuple[str, str, dict]]) -> None:
    """Facets of sessions with a facet of the model, without transcripts: (session, member, facet of the model)."""
    project_id = sql(lab.db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    users = dict(sql(lab.db, select(tables.users.c.login, tables.users.c.id)))
    start = datetime(2026, 10, 1, tzinfo=UTC)
    for index, (session, login, found) in enumerate(entries):
        values = {"project_id": project_id, "kind": "session", "session_id": session, "source": "transcript"}
        values |= {"fingerprint": f"f{index}", "version": 1, "user_id": users[login], "label": LABEL}
        values |= {"at": start + timedelta(hours=index), "body": {}, "llm": history.clean_facet(facet(**found))}
        values |= {"llm_version": history.LLM_VERSION, "llm_fingerprint": f"f{index}"}
        sql(lab.db, insert(tables.session_facets).values(**values))


def session(n: int) -> str:
    return f"0199b1c2-7d3e-7a10-9c41-0000000c{n:04d}"


def clustered(lab) -> None:
    """Three sessions of fixes not verified by two members, one of debugging not verified, two of docs."""
    fix, debug, docs = {}, {"task_type": "debug"}, {"task_type": "docs", "failure_modes": []}
    put_facets(
        lab,
        [
            (session(1), OWNER, fix),
            (session(2), OTHER, fix),
            (session(3), OWNER, fix),
            (session(4), OWNER, debug),
            (session(5), OWNER, docs),
            (session(6), OTHER, docs),
        ],
    )


def test_the_night_groups_the_facets_and_only_clusters_of_three_sessions_enter_its_figures(lab):
    clustered(lab)
    reviewed(lab)
    ((figures,),) = sql(lab.db, select(tables.curator_figures.c.figures))
    (fix,) = figures["clusters"]
    assert (fix["key"], fix["sessions"], fix["members"], fix["runs"]) == ("fix/FM-3.2", 3, 2, 0)
    assert fix["name"] == "fix: no or incomplete verification" and fix["named"] is False
    assert fix["representative"] == f"session:{session(3)}" and fix["examples"][-1] == f"session:{session(1)}"
    assert figures["facets"]["llm"] == {"version": 1, "items": 0, "analyzed": 0, "waiting": 0, "failed": 0}
    c = tables.facet_clusters
    counted = dict(sql(lab.db, select(c.c.key, c.c.sessions)))
    assert counted == {"fix/FM-3.2": 3, "debug/FM-3.2": 1, "docs/none": 2}


def test_the_model_names_the_new_groups_once_and_a_group_it_joined_counts_with_the_other(
    lab, tmp_path, monkeypatch, capsys
):
    clustered(lab)
    spec = history_run(lab)
    run_id, key = spec["id"], spec["curator"]["history_key"]
    batch = answered(ask(lab, run_id, key, clusters=True))
    assert [group["key"] for group in batch["groups"]] == ["fix/FM-3.2", "docs/none", "debug/FM-3.2"]
    fix = batch["groups"][0]
    assert fix["sessions"] == 3 and fix["label"] == "fix: no or incomplete verification"
    assert fix["samples"][0] == {
        "summary": "Ran the tests, fixed the claim and pushed.",
        "friction": ["the push was rejected"],
    }
    names = [
        {"key": "fix/FM-3.2", "name": "Fixes left without their tests", "description": "The agent ends early."},
        {"key": "debug/FM-3.2", "name": "Debugging without a check", "same_as": "fix/FM-3.2"},
        {"key": "docs/none", "name": "Docs", "same_as": "docs/none"},
    ]
    assert answered(ask(lab, run_id, key, path="report", names=names))["named"] == 3
    again = [{"key": "fix/FM-3.2", "name": "Another name"}]
    assert answered(ask(lab, run_id, key, path="report", names=again))["named"] == 0, "a group keeps its first name"
    later_batch = answered(ask(lab, run_id, key, clusters=True))
    assert later_batch["groups"] == [] and len(later_batch["named"]) == 3
    listed = answered(lab.client.get(f"/v1/projects/{PROJECT}/curator/history/clusters", headers=lab.headers["reader"]))
    (joined,) = listed["clusters"]
    assert (joined["name"], joined["sessions"], joined["members"]) == ("Fixes left without their tests", 4, 2)
    assert joined["keys"] == ["debug/FM-3.2", "fix/FM-3.2"] and joined["representative"] == f"session:{session(4)}"
    assert (listed["min_sessions"], listed["hidden"]) == (3, 1)
    c = tables.facet_clusters
    ((merged,),) = sql(lab.db, select(c.c.merged_into).where(c.c.key == "docs/none"))
    assert merged is None, "a group is never the same as itself"
    moved(lab.client, lab.worker, run_id, "verifying", "done", usage={"total_cost_usd": 0.12})
    with serving(create_app(lab.client.app.state.config)) as url:
        token = lab.headers["owner"]["Authorization"].removeprefix("Bearer ")
        home = write_credentials(tmp_path / "owner", url, "owner", token)
        status = as_json(
            cli(monkeypatch, capsys, home, "hub", "curator", "history", "status", "--json", "--project", PROJECT)
        )
        assert_json_keys("hub curator history status", status)
        assert status["budget_usd"] == 1.0 and status["runtime"] == "claude-code" and status["clusters"] == 1
        (ran,) = status["runs"]
        assert (ran["id"], ran["state"], ran["named"], ran["cost_usd"], ran["model"]) == (
            run_id,
            "done",
            3,
            0.12,
            "haiku",
        )
        shown = as_json(
            cli(monkeypatch, capsys, home, "hub", "curator", "history", "clusters", "--json", "--project", PROJECT)
        )
        assert_json_keys("hub curator history clusters", shown)
        out = ok(cli(monkeypatch, capsys, home, "hub", "curator", "history", "clusters", "--project", PROJECT)).out
        assert "Fixes left without their tests" in out and "1 smaller left out" in out
        out = ok(cli(monkeypatch, capsys, home, "hub", "curator", "history", "status", "--project", PROJECT)).out
        assert f"History run of {PROJECT}: at most $1.00 a night, on claude-code with" in out
        charter = ok(cli(monkeypatch, capsys, home, "hub", "curator", "charter", "show", "--project", PROJECT)).out
        assert "history" in charter and "at most $1.00 a night" in charter


def test_the_history_routes_need_a_grant_and_a_sink_that_clears_the_project(lab):
    path = f"/v1/projects/{PROJECT}/curator/history"
    assert lab.client.get(path, headers=lab.worker["headers"]).status_code == 403
    assert lab.client.get(path, headers=lab.headers["reader"]).status_code == 200
    blocked = {**lab.headers["reader"], "X-Evo-Sink": "nowhere"}
    assert lab.client.get(path + "/clusters", headers=blocked).status_code == 404, "a sink that clears nothing"


def test_a_history_run_that_fails_tells_its_owner_and_keeps_what_it_wrote(lab):
    sessions(lab)
    spec = history_run(lab)
    run_id, key = spec["id"], spec["curator"]["history_key"]
    (item, _) = answered(ask(lab, run_id, key))["items"]
    good = {"item": item["item"], "fingerprint": item["fingerprint"], "facet": facet()}
    assert answered(ask(lab, run_id, key, path="report", facets=[good]))["stored"] == 1
    assert report(lab.client, lab.worker, run_id, "failed", error="the agent stopped").status_code == 200
    n = tables.notifications
    rows = sql(lab.db, select(n.c.title).where(n.c.notice_kind == "run_failed", n.c.run_id == run_id))
    assert rows == [(f"History run #{run_id} failed",)]
    assert llm_of(lab.db, SECOND)["llm"] is not None


# Schema 0059


def test_the_rows_of_0059_hold_together_and_it_goes_down_to_0058_and_up_again(lab):
    clustered(lab)
    reviewed(lab)
    c, f = tables.facet_clusters, tables.session_facets
    for values in (
        {"key": "fix"},
        {"key": "fix/FM-4.1"},
        {"name": ""},
        {"name": "a\nb"},
        {"merged_into": "fix/FM-3.2"},
        {"sessions": -1},
        {"examples": {}},
    ):
        with pytest.raises(errors.CheckViolation):
            sql(lab.db, update(c).values(**values).where(c.c.key == "fix/FM-3.2"))
    for values in ({"llm": ["x"]}, {"llm_version": -1}, {"llm_error": "x" * 2001}):
        with pytest.raises(errors.CheckViolation):
            sql(lab.db, update(f).values(**values))
    r = tables.runs
    with live.engine(lab.db).begin() as conn:
        row = conn.execute(select(r).where(r.c.kind == "review")).mappings().first()
    copy = {key: value for key, value in row.items() if key not in ("id",)} | {"kind": "history", "state": "done"}
    with pytest.raises(errors.CheckViolation):
        sql(lab.db, insert(r).values(**{**copy, "schedule_id": None, "schedule_night": None}))
    with pytest.raises(errors.CheckViolation):
        sql(lab.db, insert(r).values(**{**copy, "plan_id": "fleet", "plan_revision": 1}))
    move_to(lab.db, "0058", down=True)
    assert sql(lab.db, select(func.to_regclass("public.facet_clusters"))) == [(None,)]
    move_to(lab.db, "head")  # not 0059: the night's collection reads the columns of every later revision
    assert sql(lab.db, select(func.count()).select_from(c)) == [(0,)]
    assert sql(lab.db, select(func.count()).select_from(f).where(f.c.llm.is_not(None))) == [(0,)]
    sql(lab.db, delete(tables.curator_figures))
    assert collected(lab.client, NEXT_NIGHT)["review"] == 1
    assert sql(lab.db, select(func.count()).select_from(c).where(c.c.sessions > 0)) == [(0,)], "their facets went too"
