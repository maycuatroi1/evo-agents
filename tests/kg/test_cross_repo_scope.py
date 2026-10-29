"""Python module names alone must not connect independent repo sources.

Each source may have its own dependency environment. Explicit bindings carry cross-repo dependencies instead.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from evo_agents.kg import build
from evo_agents.kg.pipeline.link import Linker
from evo_agents.kg.project import load_project_at
from evo_agents.kg.serve import Session
from tests.kg.projects import commit, git, write
from tests.kg.test_end_to_end import sync_build

SCHEMA = "schemas:schemas/event.yaml"
VALIDATE = "schemas:shared_schema.py::validate"
CONSUMER = "symbol:service:consumer.py::consume"
SERVICE = "service:file:consumer.py"
SCHEMA_BODY = "def validate(payload):\n    return 'repo-schema'\n"
IMPORT_BODY = "from shared_schema import validate\n\ndef consume(payload):\n    return validate(payload)\n"
BINDING = """version: 1
bindings:
  - from: service:consumer.py::consume
    rel: depends_on
    to: schemas:schemas/event.yaml
"""


@pytest.fixture
def two_repos(tmp_path, kg_env):
    for name, files in {
        "schemas": {"shared_schema.py": SCHEMA_BODY, "schemas/event.yaml": "required: [customer_id]\n"},
        "service": {"consumer.py": IMPORT_BODY},
    }.items():
        repo = tmp_path / name
        for path, text in files.items():
            write(repo / path, text)
        git(repo, "init", "-q", "-b", "main")
        commit(repo, "initial")
    root = tmp_path / "harness"
    write(
        root / "harness.yaml",
        yaml.safe_dump(
            {
                "name": "scope",
                "workspace": "..",
                "repos": [{"name": n, "path": n} for n in ("schemas", "service")],
            }
        ),
    )
    label = {"level": "public", "integrity": "U"}
    write(
        root / "knowledge.yaml",
        yaml.safe_dump(
            {
                "version": 1,
                "project": "scope",
                "policy": {
                    "levels": ["public"],
                    "sinks": [{"id": "agent", "kind": "agent-session", "clearance": {"level": "public"}}],
                },
                "sources": [{"id": "harness", "connector": "harness", "label": label}]
                + [
                    {"id": n, "connector": "git", "repo": n, "ref": "HEAD", "pulls": False, "label": label}
                    for n in ("schemas", "service")
                ],
            }
        ),
    )
    return load_project_at(root, kg_env)


def impacted(project, seed):
    result = Session(project, "agent").call("kg_impact", {"id": seed, "depth": 3})
    assert not result.get("isError"), result
    return {row["node"]["id"]: row for row in result["structuredContent"]["impacted"]}


def test_external_python_dependency_does_not_link_to_an_unrelated_repo(two_repos, tmp_path):
    write(tmp_path / "installed/shared_schema.py", "def validate(payload):\n    return 'installed-package'\n")
    ran = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json,shared_schema; from consumer import consume; print(json.dumps([shared_schema.__file__,consume({})]))",
        ],
        cwd=tmp_path / "service",
        env={**os.environ, "PYTHONPATH": str(tmp_path / "installed")},
        capture_output=True,
        text=True,
        check=True,
    )
    module, value = json.loads(ran.stdout)
    assert module == str(tmp_path / "installed/shared_schema.py") and value == "installed-package"
    sync_build(two_repos, verify=True)
    assert CONSUMER not in impacted(two_repos, VALIDATE)
    assert SERVICE not in impacted(two_repos, "schemas:shared_schema.py")


def test_foreign_src_layout_and_inheritance_do_not_supply_python_dependencies(two_repos, tmp_path):
    write(tmp_path / "schemas/src/shared/models.py", "class Base:\n    def run(self):\n        return 1\n")
    write(
        tmp_path / "service/consumer.py",
        "from shared.models import Base\n\nclass Child(Base):\n    def run(self):\n        return super().run()\n",
    )
    for name in ("schemas", "service"):
        commit(tmp_path / name, "inheritance")
    sync_build(two_repos)
    for seed in ("schemas:src/shared/models.py::Base", "schemas:src/shared/models.py::Base.run"):
        assert not any("service:" in nid for nid in impacted(two_repos, seed))


@pytest.mark.parametrize("path", ["shared_schema.py", "src/shared_schema.py"])
def test_local_python_calls_still_resolve_despite_foreign_name_collision(two_repos, tmp_path, path):
    write(tmp_path / "service" / path, SCHEMA_BODY)
    commit(tmp_path / "service", "local dependency")
    sync_build(two_repos, verify=True)
    assert impacted(two_repos, f"service:{path}::validate")[CONSUMER]["rel"] == "calls"
    assert CONSUMER not in impacted(two_repos, VALIDATE)
    assert SERVICE not in impacted(two_repos, "schemas:shared_schema.py")


def test_documented_cross_repo_schema_binding_is_followed_and_retracted(two_repos, tmp_path):
    guide = (Path(__file__).parents[2] / "docs" / "kg-setup.md").read_text()
    assert f"```yaml\n{BINDING}```" in guide
    write(tmp_path / "service/consumer.py", "def consume(payload):\n    return payload['customer_id']\n")
    commit(tmp_path / "service", "consume schema event")
    sync_build(two_repos)
    assert CONSUMER not in impacted(two_repos, SCHEMA)
    binding = two_repos.harness.root / "bindings/schema.yaml"
    write(binding, BINDING)
    sync_build(two_repos, verify=True)
    linked = impacted(two_repos, SCHEMA)
    assert linked[CONSUMER]["rel"] == "depends_on" and linked[CONSUMER]["status"] == "declared"
    assert SERVICE in linked
    write(binding, "version: 1\nbindings: []\n")
    sync_build(two_repos, verify=True)
    assert CONSUMER not in impacted(two_repos, SCHEMA)


def test_link_version_invalidates_old_cross_repo_edges_without_source_changes(two_repos, monkeypatch):
    old_files, old_ref = Linker._module_files, Linker._module_or_commit

    def legacy_files(self, module, source):
        return old_files(self, module, source) or [e["node"] for e in self.index["module"].get(module, [])]

    def legacy_ref(self, kind, target):
        if kind == "module":
            entries = self.index["module"].get(target["value"], [])
            if len(entries) == 1:
                return entries[0]["node"]
        return old_ref(self, kind, target)

    with monkeypatch.context() as old:
        old.setattr(build, "LINK_VERSION", "11")
        old.setattr(Linker, "_module_files", legacy_files)
        old.setattr(Linker, "_module_or_commit", legacy_ref)
        before = sync_build(two_repos)
        assert CONSUMER in impacted(two_repos, VALIDATE)
        assert SERVICE in impacted(two_repos, "schemas:shared_schema.py")
    after = build.build_project(two_repos, verify=True)
    assert after.ok and not after.skipped and after.build_id != before.build_id
    assert after.verify["match"]
    assert CONSUMER not in impacted(two_repos, VALIDATE)
    assert SERVICE not in impacted(two_repos, "schemas:shared_schema.py")
