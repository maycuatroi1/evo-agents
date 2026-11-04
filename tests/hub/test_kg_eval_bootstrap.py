"""The question set a project gets from its first graph (``evo_agents.hub.kg_eval_starter``, plan
kg-data-profile-curator, criterion a2): the questions out of the graph's file alone, as the hub has no checkout, and the
rule that only a project with no set gets one. The statements run against a fake connection; the database is the
tables of ``kg_eval_sets``, which ``tests.hub.test_kg_health`` covers on Postgres."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from evo_agents.hub import kg_eval_starter as starter
from evo_agents.hub.server import kg_eval_sets
from evo_agents.kg.build import build_project
from evo_agents.kg.evaluate import QuestionSet, parse_set
from evo_agents.kg.sync import sync_project
from tests.kg.projects import commit, make_project, write


@pytest.fixture
def kg_env(tmp_path, monkeypatch):
    home = tmp_path / "kg-home"
    monkeypatch.setenv("EVO_KG_HOME", str(home))
    return home


class Conn:
    """A connection whose first select finds ``version`` (or nothing) and that keeps the inserts."""

    def __init__(self, version=None, project=None):
        self.version, self.project, self.inserted = version, project, []

    async def execute(self, statement):
        text = str(statement)
        if text.startswith("INSERT"):
            self.inserted.append(statement.compile().params)
            return SimpleNamespace(rowcount=1)
        if "FROM projects" in text:
            return SimpleNamespace(first=lambda: self.project)
        return SimpleNamespace(first=lambda: None if self.version is None else (self.version,))


def graph_of(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    write(
        app / "app/search.py",
        "def rank_results(items):\n    return sorted(items)\n\n\ndef search(query):\n    return 1\n",
    )
    commit(app, "Make search return a constant while the ranker is rebuilt")
    assert all(r.ok for r in sync_project(project)) and build_project(project).ok
    return project.root / "graph.sqlite"


def test_the_first_graph_gives_the_questions_from_its_file_alone(tmp_path, kg_env):
    graph = graph_of(tmp_path, kg_env)
    questions = starter.questions_of(graph, None)
    assert [q["files"] for q in questions] == [["app:app/search.py"]]
    assert questions[0]["type"] == "commit"
    assert starter.questions_of(graph, (-1, -1)) == []  # a label above the set's reads nothing


def test_the_questions_are_a_set_kg_eval_reads_and_the_digest_holds(tmp_path, kg_env):
    questions = starter.questions_of(graph_of(tmp_path, kg_env), None)
    lines = [json.dumps({"set": starter.SET_NAME})] + [json.dumps(q) for q in questions]
    assert isinstance(parse_set(lines), QuestionSet)
    entries = starter.clean(questions)
    assert starter.set_digest(entries) == starter.set_digest(starter.clean(list(entries)))
    assert kg_eval_sets.set_digest is starter.set_digest  # the route and the first build digest alike


def test_a_project_without_a_set_gets_version_one():
    conn = Conn()
    made = asyncio.run(starter.start(conn, 7, "demo", [{"id": "commit-app-1", "question": "q", "files": ["app:a.py"]}]))
    assert made == 1
    row = conn.inserted[0]
    assert (row["project_id"], row["version"], row["name"]) == (7, 1, "commits")
    assert row["created_by"] is None and row["digest"]


def test_a_project_with_a_set_or_no_question_gets_nothing():
    asked = [{"id": "commit-app-1", "question": "q", "files": ["app:a.py"]}]
    held = Conn(version=3)
    assert asyncio.run(starter.start(held, 7, "demo", asked)) is None and not held.inserted
    empty = Conn()
    assert asyncio.run(starter.start(empty, 7, "demo", [])) is None and not empty.inserted


def test_the_ceiling_is_the_default_label_of_the_project():
    row = SimpleNamespace(
        levels=["public", "internal", "customer"],
        locations=["any"],
        default_label={"level": "internal", "location": "any", "projects": ["demo"]},
    )
    assert asyncio.run(starter.ceiling_of(Conn(project=row), 7, "demo")) == (1, 0)
    assert asyncio.run(starter.ceiling_of(Conn(), 7, "demo")) is None
