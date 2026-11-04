"""Starter questions from the commit history (plan kg-data-profile-curator, criterion a2): a commit's message asks, the
files it changed that the graph holds answer, and the commit's own node never does."""

import json

from evo_agents.cli import main
from evo_agents.kg import eval_commits
from evo_agents.kg.build import build_project
from evo_agents.kg.evaluate import HOLDOUT, TUNE, parse_questions, split_of
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
from tests.kg.projects import commit, make_project, write

SEARCH = (
    "def rank_results(items):\n    return sorted(items)\n\n\ndef search(query):\n    return rank_results([query]){}\n"
)


def history(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    write(app / "app/search.py", SEARCH.format("  # keep the order of equal items stable"))
    commit(app, "Keep the order of equal items stable when ranking search results")
    write(
        app / "app/server.py",
        "from app.search import search\n\n\nclass Server:\n    def handle(self, q):\n        return search(q.strip())\n",
    )
    write(app / "uv.lock", "lock = 1\n")
    commit(app, "Strip the query before the server hands it to search (#12)\n\nSigned-off-by: someone")
    write(app / "docs/guide.md", "# Guide\n\nTiny edit.\n")
    commit(app, "fix")
    write(app / "docs/old.md", "# Old\n\nObsolete notes.\n")
    commit(
        app,
        "Bump deps in the lock files and rewrite the old notes",
    )
    assert all(r.ok for r in sync_project(project))
    assert build_project(project).ok
    return project


def questions_of(project, **kw):
    store = Store.for_project(project)
    try:
        return eval_commits.commit_questions(store, **kw)
    finally:
        store.close()


def test_a_commit_message_asks_and_the_files_it_changed_answer(tmp_path, kg_env):
    found = questions_of(history(tmp_path, kg_env))
    by_text = {q["question"].splitlines()[0].strip(): q for q in found.questions}
    first = by_text["Keep the order of equal items stable when ranking search results"]
    assert first["files"] == ["app:app/search.py"] and first["type"] == "commit" and first["repo"] == "app"
    second = by_text["Strip the query before the server hands it to search"]  # the (#12) reference is taken out
    assert second["files"] == ["app:app/server.py"]  # uv.lock is a generated file, so left out of the answer
    assert "Signed-off-by" not in second["question"]


def test_no_answer_is_a_commit_or_a_pull_request_node(tmp_path, kg_env):
    found = questions_of(history(tmp_path, kg_env))
    assert found.questions
    for q in found.questions:
        assert all(":file:" not in f and ":commit:" not in f and not f.startswith("pr:") for f in q["files"])
        assert all(not f.startswith("commit") for f in q["files"])
    assert not [q for q in found.questions if "nodes" in q]


def test_short_messages_sweeps_and_the_initial_commit_are_left_out(tmp_path, kg_env):
    found = questions_of(history(tmp_path, kg_env), max_files=1)
    texts = " ".join(q["question"] for q in found.questions)
    assert "fix" not in texts.split() and "Bump deps" not in texts
    assert found.left_out  # the short message, and the first commit's five files over the limit of one
    assert found.commits >= 5


def test_the_ids_hold_so_a_commit_keeps_its_part(tmp_path, kg_env):
    project = history(tmp_path, kg_env)
    one, two = questions_of(project), questions_of(project)
    assert [q["id"] for q in one.questions] == [q["id"] for q in two.questions]
    assert all(q["id"].startswith("commit-app-") for q in one.questions)
    assert {split_of(q["id"]) for q in one.questions} <= {TUNE, HOLDOUT}
    assert len(questions_of(project, limit=1).questions) == 1
    assert parse_questions(json.dumps(q) for q in one.questions)  # the format of kg eval


def test_a_commit_above_the_ceiling_asks_nothing(tmp_path, kg_env):
    project = history(tmp_path, kg_env)
    found = questions_of(project, ceiling=(-1, -1))
    assert not found.questions and found.left_out["above the label of the set"] >= 1


def test_kg_eval_questions_from_commits_prints_a_set_kg_eval_reads(tmp_path, kg_env, capsys):
    project = history(tmp_path, kg_env)
    assert main(["kg", "eval-questions", "--from-commits", "--project", project.name]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines and len(parse_questions(lines)) == len(lines)
