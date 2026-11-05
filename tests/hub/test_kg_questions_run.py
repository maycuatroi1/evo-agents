"""The run that writes a project's starter questions when its data has no commit history to ask from (plan
kg-upload-archive, criteria a6 and a12): which projects need one, what the agents' sink reads of a graph and how the
hub checks the answers against it, without the database; on Postgres, the phase of the kg_init run that queues it and
waits for it, the route its questions come back through, and what the prompt and the logs hold (counts, never an
item)."""

import logging

import pytest

pytest.importorskip("sqlalchemy")

from evo_agents.hub import kg_questions as model
from evo_agents.hub import review as review_model
from evo_agents.kg.store import Store
from tests.hub import pg
from tests.hub.test_kg_eval_bootstrap import graph_of, kg_env  # noqa: F401 (kg_env is a fixture)

EVERYTHING = (100, 100)  # a ceiling above every label of the test graph
NOTHING = (-1, -1)  # one below every label


# Without the database


def test_a_project_needs_questions_without_a_set_or_with_few_commit_questions_only():
    assert model.needs_questions(None, 0, False) == "the project has no eval set"
    assert "under 30" in model.needs_questions("commits", 12, True)
    assert model.needs_questions("commits", 30, True) is None
    assert model.needs_questions("commits", 5, False) is None  # a person put it: theirs to keep
    assert model.needs_questions("evo-agents", 3, False) is None
    assert model.needs_questions("written", 3, True) is None
    assert (model.target(4), model.target(100), model.target(5000)) == (4, 100, 100)


@pytest.fixture
def store(tmp_path, kg_env):  # noqa: F811
    opened = Store.open_file(graph_of(tmp_path, kg_env))
    yield opened
    opened.close()


def documents(store) -> list:
    return [node for node in model._nodes(store, EVERYTHING) if node.cls not in model.LEFT_OUT]


def written(store) -> list[dict]:
    """One question a document of the graph, answered by its file."""
    return [{"question": f"Where is {n.node_id} explained?", "files": [f"{n.repo}:{n.path}"]} for n in documents(store)]


def test_the_sink_reads_the_documents_its_label_clears_by_class(store):
    found = model.readable(store, EVERYTHING)
    assert found.documents == len(documents(store)) > 3 and found.above == 0
    assert {"code", "prose"} <= set(found.classes) and not set(found.classes) & model.LEFT_OUT
    assert sum(found.classes.values()) == found.documents


def test_a_label_above_the_sink_reads_nothing_and_no_ceiling_reads_nothing(store):
    for ceiling in (NOTHING, None):  # None: the sink's grant clears no label
        found = model.readable(store, ceiling)
        assert (found.documents, found.classes) == (0, {}) and found.above == len(documents(store))


def test_questions_answered_by_files_and_nodes_of_the_graph_are_kept_with_ids(store):
    questions = written(store)
    node = documents(store)[0].node_id
    questions[0] = {"question": "Which file ranks the results?", "nodes": [node], "id": "mine-1"}
    checked = model.check(store, EVERYTHING, questions, model.readable(store, EVERYTHING))
    assert checked.problems == []
    assert len(checked.entries) == len(questions) and checked.entries[0]["id"] == "mine-1"
    assert all(e["id"].startswith(model.ID_PREFIX) for e in checked.entries[1:])
    assert {e["type"] for e in checked.entries} == {model.QUESTION_TYPE}
    assert set(checked.classes) == set(model.readable(store, EVERYTHING).classes)


def test_an_answer_not_in_the_graph_is_refused_without_naming_it(store):
    questions = written(store)
    questions[1] = {"question": "Where is the ledger?", "files": ["app:secret/ledger.py"]}
    questions[2] = {"question": "Which node?", "nodes": ["app:file:secret/ledger.py"]}
    checked = model.check(store, EVERYTHING, questions, model.readable(store, EVERYTHING))
    assert "question 2: an answer file is not in the graph you read" in checked.problems
    assert "question 3: an answer node is not in the graph you read" in checked.problems
    assert not any("ledger" in p for p in checked.problems)


def test_an_answer_above_the_label_of_the_sink_is_refused_as_absent(store):
    found = model.readable(store, EVERYTHING)
    checked = model.check(store, NOTHING, written(store), found)
    assert checked.entries == [] and checked.problems[0] == "question 1: an answer file is not in the graph you read"
    node = documents(store)[0].node_id
    hidden = model.check(store, NOTHING, [{"question": "Which node?", "nodes": [node]}], model.Readable())
    assert hidden.problems == ["question 1: an answer node is not in the graph you read"]
    paths = {n.path for n in documents(store)} | {n.node_id for n in documents(store)}
    assert not any(path in problem for path in paths for problem in checked.problems + hidden.problems)


def test_too_few_questions_a_class_left_out_a_repeat_or_no_answer_are_refused(store):
    found = model.readable(store, EVERYTHING)
    code = [q for q, n in zip(written(store), documents(store), strict=True) if n.cls == "code"]
    checked = model.check(store, EVERYTHING, code + [code[0], {"question": "Why?"}, "text"], found)
    assert f"{len(code)} questions are good, {found.documents} are needed" in checked.problems
    assert any(p.startswith("no question answered by a document of class") and "prose" in p for p in checked.problems)
    assert f"question {len(code) + 1} repeats the id or the text of an earlier one" in checked.problems
    assert f"question {len(code) + 2} has no answer: give files or nodes" in checked.problems
    assert f"question {len(code) + 3} is not an object" in checked.problems


def test_the_privacy_of_the_prompt_counts_and_classes_never_an_item(store):
    found = model.readable(store, EVERYTHING)
    text = model.prompt(project="demo", init_id=7, sink="claude-code@anthropic", found=found, why="no eval set")
    assert f"{found.documents} documents" in text and "code (" in text
    assert "--run 7 --questions FILE" in text and f"at least {found.documents} questions" in text
    for node in documents(store):
        assert node.path not in text and node.node_id not in text


def test_the_lens_is_on_demand_and_migration_0201_holds_the_lenses_of_its_day():
    from importlib import import_module

    assert model.LENS in review_model.LENSES and model.LENS in review_model.ON_DEMAND_LENSES
    migration = import_module("evo_agents.hub.migrations.versions.0201_kg_questions")
    assert model.LENS in migration.LENSES and set(migration.LENSES) <= set(review_model.LENSES)


# On Postgres

if pg.DSN:
    from datetime import UTC, datetime, timedelta
    from functools import partial
    from types import SimpleNamespace

    from sqlalchemy import func, insert, select

    from evo_agents.hub import kg_init as init_model
    from evo_agents.hub import tables
    from evo_agents.hub.server import kg_init
    from evo_agents.hub.server import kg_questions as server
    from evo_agents.hub.server.collect import review_prompt
    from tests.hub.live import sql
    from tests.hub.test_curator import PROJECT, night, set_run  # noqa: F401 (night is a fixture)
    from tests.hub.test_kg_init import INIT, init_row, profile, starter, with_profile
    from tests.hub.test_kg_optimize import configured, project_id
    from tests.hub.test_review_runs import review  # noqa: F401 (a fixture)
    from tests.hub.test_runs import web_client  # noqa: F401 (a fixture)

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
SECRET = "secret-customer-ledger.py"
READS = {"sink": "claude-code@anthropic", "documents": 3, "above": 2, "classes": {"code": 2, "prose": 1}}


async def reads(engine, row, blobs, data_dir):
    return dict(READS)


async def reads_nothing(engine, row, blobs, data_dir):
    return {**READS, "documents": 0, "classes": {}, "above": 5}


def advanced(world, count=reads, now=None) -> dict:
    engine = world.client.app.state.engine
    return world.client.portal.call(partial(kg_init.advance, engine, measure=starter, count=count, now=now))


def started(world) -> int:
    client, db = world.client, world.db
    configured(world)
    with_profile(db, project_id(db), profile())
    return client.post(INIT, headers=world.headers["owner"]).json()["id"]


def prompt_of(world, run_id: int) -> str:
    async def read():
        view = SimpleNamespace(id=run_id, project=PROJECT, repos=[])
        async with world.client.app.state.engine.begin() as conn:
            return await review_prompt(conn, view)

    return world.client.portal.call(read)


def kept(monkeypatch, entries=None, problems=()):
    async def check(engine, row, blobs, data_dir, questions, found):
        made = entries or [
            {"id": f"written-{n}", "question": q["question"], "type": "written", "files": q["files"]}
            for n, q in enumerate(questions)
        ]
        return {"entries": made, "classes": {"code": 2, "prose": 1}, "problems": list(problems)}

    monkeypatch.setattr(server, "check_questions", check)


QUESTIONS = [{"question": f"Where is part {n} of the ledger kept?", "files": [f"app:{SECRET}"]} for n in range(3)]


@needs_pg
def test_a_project_with_no_eval_set_gets_a_questions_run_and_its_privacy_holds_in_the_prompt(review):
    db = review.db
    run_id = started(review)
    assert advanced(review) == {"questions": 1}
    row = init_row(db, run_id)
    asked = row["body"]["questions"]
    assert row["state"] == "queued" and row["agent_run_id"] is None and "measures" not in row["body"]
    assert asked["state"] == model.RUNNING and asked["target"] == 3 and row["questions_run_id"] == asked["agent_run_id"]
    r = tables.runs
    (run,) = sql(db, select(r.c.kind, r.c.title, r.c.budget, r.c.state).where(r.c.id == asked["agent_run_id"]))
    assert (run[0], run[3]) == ("review", "queued") and "starter questions" in run[1]
    assert run[2]["max_usd"] <= init_model.HUB_CAP_USD
    text = prompt_of(review, asked["agent_run_id"])
    assert "kg_questions" in text and "3 documents" in text and f"--run {run_id} --questions FILE" in text
    assert SECRET not in text and "## Your decision" not in text
    assert advanced(review) == {"questions": 1}  # the questions run is still queued: the kg_init run waits
    status = review.client.get(INIT, headers=review.headers["owner"]).json()["runs"][0]["questions"]
    assert status["state"] == model.RUNNING and "prompt" not in status


@needs_pg
def test_the_questions_sent_are_a_new_set_version_and_the_kg_init_run_measures_after_the_run(review, monkeypatch):
    client, db, owner = review.client, review.db, review.headers["owner"]
    run_id = started(review)
    advanced(review)
    url = f"{INIT}/{run_id}/questions"
    kept(monkeypatch, problems=["question 2: an answer file is not in the graph you read"])
    refused = client.post(url, headers=owner, json={"questions": QUESTIONS})
    assert refused.status_code == 422 and "question 2" in refused.json()["message"]
    assert client.post(url, headers=review.headers["reader"], json={"questions": QUESTIONS}).status_code == 403
    kept(monkeypatch)
    sent = client.post(url, headers=owner, json={"questions": QUESTIONS})
    assert sent.status_code == 201, sent.text
    out = sent.json()
    assert (out["run"], out["set"], out["version"], out["questions"]) == (run_id, model.SET_NAME, 1, 3)
    s = tables.kg_eval_sets
    assert sql(db, select(s.c.name, s.c.version)) == [(model.SET_NAME, 1)]
    assert client.post(url, headers=owner, json={"questions": QUESTIONS}).status_code == 409  # kept once
    questions_run = init_row(db, run_id)["questions_run_id"]
    set_run(db, questions_run, state="cancelled", finished_at=func.now())
    assert advanced(review) == {"queued": 1}  # measured, then the kg_init review run
    row = init_row(db, run_id)
    assert row["state"] == "running" and row["agent_run_id"] != questions_run
    assert row["body"]["questions"]["state"] == model.DONE and row["body"]["measures"]["words"]["hit@5"] == 55.0
    status = client.get(INIT, headers=owner).json()["runs"][0]["questions"]
    assert (status["state"], status["version"], status["agent_run_id"]) == (model.DONE, 1, questions_run)


@needs_pg
def test_a_sink_that_reads_no_document_writes_no_questions_and_says_why(review):
    db = review.db
    run_id = started(review)
    assert advanced(review, count=reads_nothing) == {"queued": 1}
    row = init_row(db, run_id)
    asked = row["body"]["questions"]
    assert asked["state"] == model.SKIPPED and "clears none of the 5 documents" in asked["why"]
    assert row["questions_run_id"] is None and any(n.startswith("no starter questions") for n in row["body"]["notes"])
    assert sql(db, select(func.count()).select_from(tables.kg_eval_sets)) == [(0,)]


@needs_pg
def test_a_project_whose_set_has_enough_questions_needs_no_questions_run(review):
    db = review.db
    pid = project_id(db)
    run_id = started(review)
    questions = [{"id": f"q{n}", "question": f"q {n}", "type": "commit", "files": ["app:a.py"]} for n in range(40)]
    values = {"project_id": pid, "version": 1, "name": "commits", "questions": questions, "digest": "d" * 64}
    sql(db, insert(tables.kg_eval_sets).values(values))
    assert advanced(review) == {"queued": 1}
    assert init_row(db, run_id)["body"]["questions"]["state"] == model.NOT_NEEDED


@needs_pg
def test_after_two_hours_the_kg_init_run_measures_without_its_questions_run(review):
    db = review.db
    run_id = started(review)
    advanced(review)
    later = datetime.now(UTC) + model.WAIT + timedelta(minutes=5)
    assert advanced(review, now=later) == {"waiting": 1}  # the questions run holds the project's one review run
    asked = init_row(db, run_id)["body"]["questions"]
    assert asked["state"] == model.TIMED_OUT and "measured without it" in asked["ended"]
    assert init_row(db, run_id)["body"]["measures"]["words"]["hit@5"] == 55.0


@needs_pg
def test_the_logs_of_the_questions_run_hold_counts_and_ids_never_an_item(review, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    run_id = started(review)
    advanced(review)
    url = f"{INIT}/{run_id}/questions"
    kept(monkeypatch, problems=[f"question 1: {model.ID_PREFIX}refused"])
    review.client.post(url, headers=review.headers["owner"], json={"questions": QUESTIONS})
    kept(monkeypatch)
    assert review.client.post(url, headers=review.headers["owner"], json={"questions": QUESTIONS}).status_code == 201
    set_run(review.db, init_row(review.db, run_id)["questions_run_id"], state="cancelled", finished_at=func.now())
    advanced(review)
    records = [r for r in caplog.records if r.name == server.log.name]
    assert {r.getMessage() for r in records} >= {
        "kg starter questions run queued",
        "kg starter questions refused",
        "kg starter questions kept",
        "kg starter questions run ended",
    }
    for record in caplog.records:
        held = record.getMessage() + repr(vars(record))
        assert SECRET not in held and "ledger" not in held
