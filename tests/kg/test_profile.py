"""The data profile of a graph (plan kg-data-profile-curator, criteria a1, a11, a12, a17): counts and shapes of what
the corpus holds, model-free, in the build's coverage, and never a value."""

import json

from evo_agents.cli import main
from evo_agents.kg import profile as kg_profile
from evo_agents.kg.build import build_project
from evo_agents.kg.serve import Session
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
from tests.kg.projects import commit, make_project, write


def built(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    assert all(r.ok for r in sync_project(project))
    report = build_project(project)
    assert report.ok, report.errors
    return project, report


def profile_of(project) -> dict:
    store = Store.for_project(project)
    try:
        return kg_profile.of_coverage(store.summary()["coverage"])
    finally:
        store.close()


def test_a_build_writes_a_profile_of_its_corpus(tmp_path, kg_env):
    project, _ = built(tmp_path, kg_env)
    found = profile_of(project)
    assert found["items"] > 0 and found["chars"] > 0
    assert found["classes"]["code"]["items"] > 0 and found["classes"]["prose"]["items"] > 0
    assert any(lang == "python" for lang in found["languages"])
    assert 0 <= found["noise"]["ratio"] <= 1
    assert kg_profile.render(found).startswith("items ")


def test_the_profile_holds_counts_and_shapes_never_a_value(tmp_path, kg_env):
    project, _ = built(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    secret = "nguyen.van.a@example.org 0912345678 patient-zq"
    write(app / "docs/contacts.md", f"# Contacts\n\nWrite to {secret} about case AB-123456.\n")
    commit(app)
    assert all(r.ok for r in sync_project(project)) and build_project(project, cold=True).ok
    found = profile_of(project)
    seen = found["sensitive"]
    assert seen["email"]["strings"] >= 1 and seen["phone"]["strings"] >= 1
    text = json.dumps(found)
    for value in ("nguyen.van.a", "example.org", "0912345678", "patient-zq", "AB-123456"):
        assert value not in text
    assert isinstance(found["identifier_candidates"], list)


def test_the_profile_leaves_the_hash_and_the_answers_unchanged(tmp_path, kg_env, monkeypatch):
    project, report = built(tmp_path, kg_env)
    ask = {"query": "search"}
    with_profile = Session(project, "wide").call("kg_search", ask)
    monkeypatch.setattr("evo_agents.kg.build.build_profile", lambda *a, **k: {})
    bare = build_project(project, cold=True)
    assert bare.ok and bare.content_hash == report.content_hash
    assert Session(project, "wide").call("kg_search", ask) == with_profile


def test_kg_profile_prints_it_and_says_when_there_is_none(tmp_path, kg_env, capsys):
    project, _ = built(tmp_path, kg_env)
    assert main(["kg", "profile", "--project", project.name, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["project"] == project.name and out["profile"]["items"] > 0
    assert main(["kg", "profile", "--project", project.name]) == 0
    assert "classes:" in capsys.readouterr().out


def test_kg_bench_profile_says_so_on_an_empty_cache(tmp_path, capsys):
    assert main(["kg", "bench", "profile", "--cache", str(tmp_path / "cache"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"repos": {}}
