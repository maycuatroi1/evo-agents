"""The code backend a build used is coverage data: ``auto`` without graphify warns, naming the extra and the
non-Python items left without symbols; an explicit ``graphify-ast`` without graphify fails the build."""

import json
import sys

import pytest

from evo_agents.cli import main
from evo_agents.kg.build import build_project
from evo_agents.kg.extract import graphify as gfy
from evo_agents.kg.project import load_project_at
from evo_agents.kg.status import project_status, render_status
from evo_agents.kg.store import Store
from evo_agents.kg.sync import sync_project
from tests.kg.projects import commit, git, write

INSTALL = "pip install 'evo-ak[graphify]' or uv tool install 'evo-ak[graphify]'"
WARNING = (
    "source billing: graphify is not installed, so code backend auto fell back to python-ast: 1 non-Python code"
    f" item(s) got no symbols (typescript 1); install the extra: {INSTALL}"
)

INVOICE = "def total(lines):\n    return sum(line.amount for line in lines)\n"
CHECKOUT = """export class Checkout {
  pay(amount: number) { return amount; }
}
"""


def make_billing(base, home, backend=None):
    """A harness with one git source, ``billing``, over a repo holding one Python and one TypeScript file."""
    repo = base / "billing-service"
    write(repo / "billing/invoice.py", INVOICE)
    write(repo / "web/checkout.ts", CHECKOUT)
    git(repo, "init", "-q", "-b", "main")
    commit(repo, "init")
    root = base / "billing-harness"
    write(
        root / "harness.yaml",
        "name: billing\nworkspace: ..\nknowledge_file: knowledge.yaml\n"
        "repos:\n  - name: billing-service\n    path: billing-service\n",
    )
    code = f"    code: {{backend: {backend}}}\n" if backend else ""
    write(
        root / "knowledge.yaml",
        f"""version: 1
project: billing
policy:
  levels: [public, internal]
  sinks:
    - id: agent
      kind: agent-session
      clearance: {{level: internal}}
sources:
  - id: billing
    connector: git
    repo: billing-service
    label: {{level: internal, integrity: U}}
{code}""",
    )
    project = load_project_at(root, home)
    assert all(r.ok for r in sync_project(project))
    return project


@pytest.fixture
def no_graphify(monkeypatch):
    """graphify cannot be imported: None in sys.modules makes ``import graphify.extract`` raise ImportError."""
    monkeypatch.setitem(sys.modules, "graphify", None)
    monkeypatch.setitem(sys.modules, "graphify.extract", None)
    gfy.available.cache_clear()
    assert not gfy.available()
    yield
    gfy.available.cache_clear()


def symbol_paths(project, build_id):
    store = Store.for_project(project)
    rows = store.db.execute(
        "SELECT node_id FROM nodes WHERE kind = 'Symbol' AND tx_from <= :b AND (tx_to IS NULL OR tx_to > :b)",
        {"b": build_id},
    )
    return {r[0].split("::")[0].removeprefix("symbol:billing:") for r in rows}


def test_auto_without_graphify_warns_in_build_and_status(tmp_path, kg_env, no_graphify, capsys):
    project = make_billing(tmp_path, kg_env)
    root = str(project.harness.root)

    assert main(["kg", "build", "--project", root]) == 0
    out, err = capsys.readouterr()
    assert "code backend auto: 2 item(s); python-ast 1, no symbols 1" in out
    assert f"  warning: {WARNING}" in err.splitlines()

    assert main(["kg", "build", "--project", root, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and report["warnings"] == [WARNING]
    code = next(s for s in report["coverage"]["sources"] if s["id"] == "billing")["code"]
    assert code["backend"] == "auto"
    assert code["extractors"] == {"python-ast": 1, "none": 1}
    assert code["fallback"] == 1 and code["fallback_languages"] == {"typescript": 1}
    assert INSTALL in code["warning"]
    assert symbol_paths(project, report["build_id"]) == {"billing/invoice.py"}

    assert main(["kg", "status", "--project", root, "--json"]) == 0  # a warning, not a failure
    status = json.loads(capsys.readouterr().out)
    assert status["ok"] and status["warnings"] == [WARNING]
    sources = status["graph"]["coverage"]["sources"]
    assert next(s for s in sources if s["id"] == "billing")["code"]["fallback"] == 1

    assert main(["kg", "status", "--project", root]) == 0
    out = capsys.readouterr().out
    assert "code backend auto: 2 item(s); python-ast 1, no symbols 1" in out
    assert f"warning: {WARNING}" in out.splitlines()
    assert f"warning: {WARNING}" in render_status(project_status(project), brief=True).splitlines()


def test_explicit_graphify_backend_without_graphify_fails_the_build(tmp_path, kg_env, no_graphify, capsys):
    project = make_billing(tmp_path, kg_env, backend="graphify-ast")
    report = build_project(project)
    assert not report.ok and report.build_id is None
    message = (
        "source billing declares code backend graphify-ast, but graphify is not installed;"
        f" install the extra: {INSTALL}"
    )
    assert report.errors == [message]

    assert main(["kg", "build", "--project", str(project.harness.root)]) == 1
    out, err = capsys.readouterr()
    assert out.startswith("build FAILED for billing: source billing declares code backend graphify-ast")
    assert f"  error: {message}" in err.splitlines()


@pytest.mark.parametrize("backend", ["python-ast", "none"])
def test_a_declared_backend_without_graphify_has_no_warning(tmp_path, kg_env, no_graphify, backend):
    report = build_project(make_billing(tmp_path, kg_env, backend=backend))
    assert report.ok and report.warnings == []
    code = next(s for s in report.coverage["sources"] if s["id"] == "billing")["code"]
    assert code["backend"] == backend and code["fallback"] == 0 and code["warning"] is None


@pytest.mark.skipif(not gfy.available(), reason="graphify extra not installed")
def test_auto_with_graphify_has_no_warning(tmp_path, kg_env, capsys):
    project = make_billing(tmp_path, kg_env)
    root = str(project.harness.root)

    assert main(["kg", "build", "--project", root, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and report["warnings"] == [] and report["coverage"]["warnings"] == []
    code = next(s for s in report["coverage"]["sources"] if s["id"] == "billing")["code"]
    assert code["extractors"] == {"graphify-ast": 1, "python-ast": 1}
    assert code["fallback"] == 0 and code["warning"] is None
    assert symbol_paths(project, report["build_id"]) == {"billing/invoice.py", "web/checkout.ts"}

    assert main(["kg", "build", "--project", root]) == 0
    out, err = capsys.readouterr()
    assert "code backend auto: 2 item(s); graphify-ast 1, python-ast 1" in out
    assert "warning" not in out + err

    assert main(["kg", "status", "--project", root, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["warnings"] == []
