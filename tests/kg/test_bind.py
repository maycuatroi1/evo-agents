import json
import shutil
from pathlib import Path

import pytest

from evo_agents.cli import main
from evo_agents.kg import serve
from evo_agents.kg.project import (
    ProjectError,
    bind,
    load_project_at,
    read_bindings,
    read_index,
    remember,
    resolve_project,
    unbind,
)
from tests.kg.projects import write


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    # Neither the developer's environment nor the machine's harness registries may leak in.
    monkeypatch.delenv("EVO_KG_PROJECT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setattr("evo_agents.harness.REGISTRY_PATHS", ())


def harness(base: Path, home: Path, name: str, repos: tuple[str, ...] = ()):
    """A minimal harness <base>/<name>-harness whose repos are sibling directories of it."""
    root = base / f"{name}-harness"
    listed = "".join(f"  - name: {r}\n    path: {r}\n" for r in repos) or "  []\n"
    write(root / "harness.yaml", f"name: {name}\nknowledge_file: knowledge.yaml\nrepos:\n{listed}")
    write(root / "knowledge.yaml", f"version: 1\nproject: {name}\n")
    for r in repos:
        (base / r).mkdir(parents=True, exist_ok=True)
    project = load_project_at(root, home)
    remember(project)
    return project


def shared_pair(tmp_path, home):
    """Two projects that both list the repo `shared`, so the index alone is ambiguous there."""
    alpha = harness(tmp_path, home, "alpha", ("shared", "only-alpha"))
    beta = harness(tmp_path, home, "beta", ("shared",))
    return alpha, beta, (tmp_path / "shared").resolve()


def test_bind_settles_a_directory_shared_by_several_projects(tmp_path, kg_env):
    alpha, beta, shared = shared_pair(tmp_path, kg_env)
    deep = shared / "pkg" / "mod"
    deep.mkdir(parents=True)
    with pytest.raises(ProjectError) as err:
        resolve_project(directory=deep)
    assert "several projects (alpha, beta)" in str(err.value)
    assert "evo-agents kg bind" in str(err.value)

    index_before = (kg_env / "projects.json").read_text()
    assert bind(shared, "beta").name == "beta"
    assert resolve_project(directory=deep).name == "beta"
    assert resolve_project(directory=shared).name == "beta"
    # Bindings live in their own file; the index remember() writes is untouched.
    assert (kg_env / "projects.json").read_text() == index_before
    assert set(read_index(kg_env)) == {"alpha", "beta"}
    assert read_bindings(kg_env) == {str(shared): {"project": "beta", "harness_root": str(beta.harness.root)}}
    assert not list(kg_env.glob("*.tmp"))


def test_bind_order_flag_env_binding_harness_index(tmp_path, kg_env, monkeypatch):
    alpha = harness(tmp_path, kg_env, "alpha", ("only-alpha",))
    harness(tmp_path, kg_env, "beta")
    inside_alpha = alpha.harness.root / "docs"
    inside_alpha.mkdir()
    repo = (tmp_path / "only-alpha").resolve()
    # Without a binding the harness and the index decide.
    assert resolve_project(directory=inside_alpha).name == "alpha"
    assert resolve_project(directory=repo).name == "alpha"

    bind(alpha.harness.root, "beta")
    bind(repo, "beta")
    assert resolve_project(directory=inside_alpha).name == "beta"
    assert resolve_project(directory=repo).name == "beta"
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(inside_alpha))
    assert resolve_project().name == "beta"
    monkeypatch.setenv("EVO_KG_PROJECT", "alpha")
    assert resolve_project().name == "alpha"
    assert resolve_project("beta").name == "beta"


def test_bind_nearest_bound_ancestor_wins(tmp_path, kg_env):
    harness(tmp_path, kg_env, "alpha")
    beta = harness(tmp_path, kg_env, "beta")
    outer = tmp_path / "work"
    inner = outer / "client" / "repo"
    (inner / "src").mkdir(parents=True)
    (outer / "other").mkdir()
    bind(outer, "alpha")
    bind(inner, str(beta.harness.root))  # a harness path names a project too
    assert resolve_project(directory=inner / "src").name == "beta"
    assert resolve_project(directory=inner).name == "beta"
    assert resolve_project(directory=outer / "client").name == "alpha"
    assert resolve_project(directory=outer / "other").name == "alpha"

    assert unbind(inner) == str(inner.resolve())
    assert resolve_project(directory=inner / "src").name == "alpha"
    assert unbind(inner) is None


def test_bind_refuses_a_project_that_does_not_resolve(tmp_path, kg_env):
    harness(tmp_path, kg_env, "alpha")
    loose = tmp_path / "loose"
    loose.mkdir()
    with pytest.raises(ProjectError, match="unknown project 'nope'"):
        bind(loose, "nope")
    assert not (kg_env / "bound.json").exists()
    # Without a name it pins what the directory resolves to now, so an unknown directory is refused too.
    with pytest.raises(ProjectError, match="no project contains"):
        bind(loose)
    assert not (kg_env / "bound.json").exists()


def test_bind_errors_point_to_kg_bind(tmp_path, kg_env):
    harness(tmp_path, kg_env, "alpha")
    loose = tmp_path / "loose"
    loose.mkdir()
    with pytest.raises(ProjectError) as err:
        resolve_project(directory=loose)
    assert "no project contains" in str(err.value)
    assert f"evo-agents kg bind --project NAME {loose.resolve()}" in str(err.value)


def test_bind_that_no_longer_loads_is_an_error_not_a_fallback(tmp_path, kg_env):
    alpha = harness(tmp_path, kg_env, "alpha")
    beta = harness(tmp_path, kg_env, "beta")
    inside_alpha = alpha.harness.root / "docs"
    inside_alpha.mkdir()
    bind(inside_alpha, "beta")
    shutil.rmtree(beta.harness.root)
    with pytest.raises(ProjectError) as err:
        resolve_project(directory=inside_alpha)
    message = str(err.value)
    assert "is bound to project 'beta'" in message
    assert f"evo-agents kg bind --remove {inside_alpha.resolve()}" in message


def test_bind_cli_binds_lists_and_removes(tmp_path, kg_env, monkeypatch, capsys):
    alpha, beta, shared = shared_pair(tmp_path, kg_env)
    monkeypatch.chdir(shared)

    assert main(["kg", "bind"]) == 2  # ambiguous here, and no name given
    assert "evo-agents kg bind" in capsys.readouterr().err
    assert main(["kg", "bind", "--project", "nope"]) == 2
    assert "unknown project 'nope'" in capsys.readouterr().err
    assert not (kg_env / "bound.json").exists()

    assert main(["kg", "bind", "--project", "alpha"]) == 0
    assert f"bound {shared}  ->  alpha" in capsys.readouterr().out
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert main(["kg", "bind", "--project", "beta", "--json", str(elsewhere)]) == 0
    assert json.loads(capsys.readouterr().out)["project"] == "beta"
    assert main(["kg", "status", "--json"]) in (0, 1)  # other commands now resolve from the cwd
    assert json.loads(capsys.readouterr().out)["project"] == "alpha"

    assert main(["kg", "bind", "--list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert {d: e["project"] for d, e in listed.items()} == {str(shared): "alpha", str(elsewhere.resolve()): "beta"}
    assert main(["kg", "bind", "--list"]) == 0
    assert f"{shared}  ->  alpha" in capsys.readouterr().out

    assert main(["kg", "bind", "--remove", str(shared)]) == 0
    assert f"unbound {shared}" in capsys.readouterr().out
    assert main(["kg", "bind", "--remove", str(shared)]) == 1
    assert "has no binding" in capsys.readouterr().err
    assert main(["kg", "bind", "--list", "--remove", str(shared)]) == 2
    assert main(["kg", "bind", "--list", "--project", "alpha"]) == 2
    capsys.readouterr()
    assert set(read_bindings(kg_env)) == {str(elsewhere.resolve())}


def test_bind_reaches_the_mcp_server_and_no_tool_takes_a_project(tmp_path, kg_env, monkeypatch):
    alpha, beta, shared = shared_pair(tmp_path, kg_env)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(shared))
    unbound = serve.make_session(None, "agent")
    text = unbound.call("kg_status", {})["content"][0]["text"]
    assert "evo-agents kg bind" in text

    bind(shared, "alpha")
    session = serve.make_session(None, "agent")
    assert session.project is not None and session.project.name == "alpha"
    for tool in serve.TOOLS:
        assert "project" not in tool["inputSchema"].get("properties", {}), tool["name"]
