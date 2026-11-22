"""The data profile of a graph (plan kg-data-profile-curator, criteria a1, a11, a12, a17): counts and shapes of what
the corpus holds, model-free, in the build's coverage, and never a value."""

import json

from evo_agents.cli import main
from evo_agents.kg import profile as kg_profile
from evo_agents.kg.sync import sync_project
from tests.kg import built as memory
from tests.kg.hub_graph import hub_session
from tests.kg.projects import commit, make_project, write


def built(tmp_path, kg_env):
    project = make_project(tmp_path, kg_env)
    assert all(r.ok for r in sync_project(project))
    report = memory.build(project)
    assert report.ok, report.errors
    return project, report


def profile_of(project) -> dict:
    return kg_profile.of_coverage(memory.rows_of(project).summary["coverage"])


def test_a_build_writes_a_profile_of_its_corpus(tmp_path, kg_env):
    project, _ = built(tmp_path, kg_env)
    found = profile_of(project)
    assert found["items"] > 0 and found["chars"] > 0
    assert found["classes"]["code"]["items"] > 0 and found["classes"]["prose"]["items"] > 0
    assert any(lang == "python" for lang in found["languages"])
    assert 0 <= found["noise"]["ratio"] <= 1
    assert kg_profile.render(found).startswith("items ")


def test_the_profile_counts_the_calls_of_the_symbols_a_file_defines(tmp_path, kg_env):
    project, _ = built(tmp_path, kg_env)
    python = profile_of(project)["languages"]["python"]
    assert python["calls"] > 0


def test_the_profile_holds_counts_and_shapes_never_a_value(tmp_path, kg_env):
    project, _ = built(tmp_path, kg_env)
    app = tmp_path / "demo-app"
    secret = "nguyen.van.a@example.org 0912345678 patient-zq"
    write(app / "docs/contacts.md", f"# Contacts\n\nWrite to {secret} about case AB-123456.\n")
    commit(app)
    assert all(r.ok for r in sync_project(project)) and memory.build(project, cold=True).ok
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
    with_profile = hub_session(project, "wide").call("kg_search", ask)
    monkeypatch.setattr("evo_agents.kg.build.build_profile", lambda *a, **k: {})
    bare = memory.build(project, cold=True)
    assert bare.ok and bare.content_hash == report.content_hash
    assert hub_session(project, "wide").call("kg_search", ask) == with_profile


def test_kg_profile_reads_the_hub_and_says_when_there_is_none(tmp_path, kg_env, capsys, monkeypatch):
    from tests.kg.hub_graph import sign_in

    project, _ = built(tmp_path, kg_env)
    hub = sign_in(monkeypatch, project)
    assert main(["kg", "profile", "--project", project.name, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["project"] == project.name and out["profile"]["items"] > 0
    assert main(["kg", "profile", "--project", project.name]) == 0
    assert "classes:" in capsys.readouterr().out
    assert hub.asked == [("POST", f"/v1/kg/{project.name}/tools/kg_status")] * 2
    assert main(["kg", "profile", "--project", "lumen-other"]) == 1  # a project the hub does not have
    assert "no project lumen-other" in capsys.readouterr().err


def test_kg_bench_profile_says_so_on_an_empty_cache(tmp_path, capsys):
    assert main(["kg", "bench", "profile", "--cache", str(tmp_path / "cache"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"repos": {}}


def test_the_kinds_of_an_uploaded_archive_fall_in_their_classes():
    classes = {kind: kg_profile.class_of(kind) for kind in ("pdf", "docx", "xlsx", "pptx", "html", "table", "record")}
    assert classes == {
        "pdf": "prose",
        "docx": "prose",
        "xlsx": "prose",
        "pptx": "prose",
        "html": "prose",
        "table": "structured",
        "record": "structured",
    }
    assert kg_profile.class_of("asset") == "binary"
