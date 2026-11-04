"""``kg init`` on this machine: a fake Curator answers, the decision is applied, the run records its inputs."""

import json
from pathlib import Path

import pytest
import yaml

from evo_agents.kg import init_local, init_run
from evo_agents.kg import settings as kg_settings
from evo_agents.kg.build import build_project
from evo_agents.kg.project import load_project_at
from evo_agents.kg.sync import sync_project
from tests.kg.projects import make_project

SEEN: list[str] = []


def built(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    assert all(r.ok for r in sync_project(project))
    assert build_project(project).ok
    return project


def answer(decision: dict):
    def ask(prompt: str) -> str:
        SEEN.append(prompt)
        return "I looked at it.\n```json\n" + json.dumps(decision) + "\n```\n"

    return ask


@pytest.fixture(autouse=True)
def clear():
    SEEN.clear()


def test_settings_and_strategy_are_applied_and_the_run_is_recorded(tmp_path, kg_env):
    project = built(tmp_path, kg_env)
    decision = {
        "strategy": {"use": "documents"},
        "settings": {"bm25_name": 3.0},
        "reasons": [
            {"about": "strategy", "why": "mixed content", "figures": {"lines": 100}},
            {"about": "settings", "why": "names matter", "figures": {"hit@5": 40}},
        ],
    }
    record = init_local.run(project, ask=answer(decision))
    assert record["error"] is None, record
    assert record["applied"]["settings"]["bm25_name"]["to"] == 3.0
    assert kg_settings.read(project.root).bm25_name == 3.0
    assert record["build"]["ok"]
    written = json.loads((project.root / init_local.FILE).read_text())
    names = {i["name"] for i in written["inputs"]}
    assert {"profile", "coverage", "strategies", "questions", "prompt"} <= names
    for item in written["inputs"]:
        assert item["sha256"] == init_run.digest(Path(item["path"]))
    assert "## Your decision" in SEEN[0]


def test_a_bad_answer_changes_nothing(tmp_path, kg_env):
    project = built(tmp_path, kg_env)
    before = kg_settings.read(project.root)
    record = init_local.run(project, ask=lambda prompt: "no idea")
    assert record["error"] and record["applied"] is None and record["build"] is None
    assert kg_settings.read(project.root) == before
    assert (project.root / init_local.FILE).exists()


def test_a_part_without_numbers_is_refused(tmp_path, kg_env):
    project = built(tmp_path, kg_env)
    decision = {"settings": {"bm25_name": 3.0}, "reasons": [{"about": "settings", "why": "feels right", "figures": {}}]}
    record = init_local.run(project, ask=answer(decision))
    assert record["error"]
    assert kg_settings.read(project.root).bm25_name == kg_settings.DEFAULT.bm25_name


def test_the_holdout_never_enters_the_inputs(tmp_path, kg_env):
    from evo_agents.kg.evaluate import HOLDOUT, TUNE, split_of

    project = built(tmp_path, kg_env)
    ids = [f"q{i}" for i in range(60)]
    lines = [
        json.dumps({"id": i, "question": f"where is rank_results {i}", "type": "t", "files": ["app:app/search.py"]})
        for i in ids
    ]
    path = tmp_path / "q.jsonl"
    path.write_text("\n".join(lines))
    assert {split_of(i) for i in ids} == {TUNE, HOLDOUT}
    record = init_local.run(project, questions=path, ask=answer({}), rebuild=False)
    kept = (project.root / init_local.INPUT_DIR / "questions.tune.jsonl").read_text()
    for i in ids:
        assert (f'"id": "{i}"' in kept) == (split_of(i) == TUNE)
    for i in ids:
        if split_of(i) == HOLDOUT:
            assert f'"{i}"' not in SEEN[0]
    assert record["questions"]["count"] == sum(split_of(i) == TUNE for i in ids)


def test_a_source_that_breaks_the_project_is_put_back(tmp_path, kg_env):
    project = built(tmp_path, kg_env)
    path = project.knowledge_path
    before = path.read_text()
    decision = {
        "sources": [{"id": "x", "connector": "no-such-connector-at-all"}],
        "reasons": [{"about": "sources", "why": "more data", "figures": {"items": 3}}],
    }
    record = init_local.run(project, ask=answer(decision), rebuild=False)
    data = yaml.safe_load(path.read_text())
    if record["error"]:
        assert path.read_text() == before
    else:
        assert "x" in [s["id"] for s in data["sources"]]
        assert (path.with_name(path.name + ".before-init")).exists()
    load_project_at(project.harness.root, kg_env)


def test_an_existing_source_id_is_never_overwritten(tmp_path, kg_env):
    project = built(tmp_path, kg_env)
    decision = {
        "sources": [{"id": "app", "connector": "git", "repo": "app", "include": ["nothing/**"]}],
        "reasons": [{"about": "sources", "why": "narrow", "figures": {"items": 3}}],
    }
    record = init_local.run(project, ask=answer(decision), rebuild=False)
    assert record["applied"]["sources"] == {"added": [], "skipped_existing": ["app"]}
    data = yaml.safe_load(project.knowledge_path.read_text())
    assert "include" not in next(s for s in data["sources"] if s["id"] == "app")


def test_code_changes_are_recorded_not_applied(tmp_path, kg_env):
    project = built(tmp_path, kg_env)
    decision = {
        "code_changes": [{"title": "a chunker", "why": "long files"}],
        "reasons": [{"about": "code_changes", "why": "long files", "figures": {"p95_lines": 900}}],
    }
    record = init_local.run(project, ask=answer(decision), rebuild=False)
    assert record["applied"]["code_changes"]["applied"] is False


def test_the_agent_runtime_can_be_set_by_the_environment(monkeypatch, tmp_path):
    script = tmp_path / "agent.py"
    script.write_text("import sys\nprint('got ' + str(len(sys.stdin.read())))\n")
    monkeypatch.setenv(init_local.AGENT_ENV, json.dumps(["python3", str(script)]))
    assert init_local.ask_agent("hello").startswith("got 5")
    monkeypatch.setenv(init_local.AGENT_ENV, "not json")
    with pytest.raises(init_local.InitError):
        init_local.ask_agent("hello")
