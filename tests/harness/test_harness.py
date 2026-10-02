from pathlib import Path

import pytest

from evo_agents.cli import main
from evo_agents.harness import find_manifest, load_manifest, repo_paths, validate_harness
from evo_agents.schema import errors, validate


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def harness(tmp_path: Path) -> Path:
    root = tmp_path / "demo-harness"
    write(
        root / "harness.yaml",
        f"""
name: demo
workspace: {tmp_path}
created_at: 2026-10-02T10:00:00+07:00
knowledge_file: knowledge.yaml
repos:
  - name: app
    path: app
    role: backend
""",
    )
    write(
        root / "knowledge.yaml",
        """
version: 1
project: demo
policy:
  levels: [public, internal, customer]
  locations: [any, domestic-only]
  sinks:
    - id: claude-code@anthropic
      kind: agent-session
      clearance: {level: customer}
sources:
  - id: harness
    connector: harness
    label: {level: internal, integrity: T}
  - id: app
    connector: git
    repo: app
    label: {level: internal, integrity: T}
""",
    )
    write(root / "contracts.yaml", "seams: []\n")
    write(
        root / "plans/active/demo-plan.yaml",
        """
id: demo-plan
goal: ship it
steps:
  - id: 1
    what: first
    status: done
    evidence: abc1234
  - id: 2
    what: second
    depends_on: [1]
    status: pending
""",
    )
    return root


def test_valid_harness_has_no_errors(harness: Path):
    reports = validate_harness(harness)
    assert [r.kind for r in reports] == ["harness", "contracts", "knowledge", "plan"]
    assert all(r.ok for r in reports), [str(i) for r in reports for i in r.issues]


def test_unknown_keys_warn_but_do_not_fail(harness: Path):
    text = (harness / "harness.yaml").read_text() + "invented_key: 1\n"
    (harness / "harness.yaml").write_text(text)
    report = validate_harness(harness)[0]
    assert report.ok
    assert any(i.severity == "warning" and i.path == "invented_key" for i in report.issues)


def test_knowledge_semantics_catch_bad_references(harness: Path):
    text = (harness / "knowledge.yaml").read_text()
    text = text.replace("repo: app", "repo: ghost")
    text = text.replace("level: internal, integrity: T}\n  - id: app", "level: top}\n  - id: app")
    text += "  - id: app\n    connector: nope\n"
    (harness / "knowledge.yaml").write_text(text)
    knowledge = next(r for r in validate_harness(harness) if r.kind == "knowledge")
    messages = [str(i) for i in errors(knowledge.issues)]
    assert any("duplicate source id 'app'" in m for m in messages)
    assert any("unknown connector 'nope'" in m for m in messages)
    assert any("'ghost' is not declared" in m for m in messages)
    assert any("'top' is not in policy.levels" in m for m in messages)


def test_missing_label_is_fail_closed_warning(harness: Path):
    text = (harness / "knowledge.yaml").read_text()
    text = text.replace("    label: {level: internal, integrity: T}\n  - id: app", "  - id: app", 1)
    (harness / "knowledge.yaml").write_text(text)
    knowledge = next(r for r in validate_harness(harness) if r.kind == "knowledge")
    assert any("highest level" in i.message for i in knowledge.issues)


def test_find_manifest_walks_up(harness: Path):
    nested = harness / "plans" / "active"
    assert find_manifest(nested) == harness.resolve()


def test_repo_paths_resolve_against_workspace(harness: Path, tmp_path: Path):
    h = load_manifest(harness)
    [(repo, path)] = repo_paths(h)
    assert repo["name"] == "app"
    assert path == tmp_path / "app"


def test_timestamps_are_strings_after_loading(harness: Path):
    h = load_manifest(harness)
    assert isinstance(h.manifest["created_at"], str)


def test_cli_exit_code_reflects_errors(harness: Path, capsys):
    assert main(["harness", "validate", str(harness)]) == 0
    (harness / "plans/active/demo-plan.yaml").write_text("id: demo-plan\nsteps: nope\n")
    assert main(["harness", "validate", str(harness)]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_validator_subset():
    schema = {
        "type": "object",
        "required": ["a"],
        "additionalProperties": False,
        "properties": {
            "a": {"type": "integer", "minimum": 1},
            "b": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
            "c": {"enum": ["x"], "x-severity": "warning"},
        },
    }
    assert validate({"a": 1, "b": ["s"]}, schema) == []
    issues = validate({"a": 0, "b": 3, "c": "y", "d": 1}, schema)
    severities = {(i.path, i.severity) for i in issues}
    assert ("a", "error") in severities
    assert ("b", "error") in severities
    assert ("c", "warning") in severities
    assert ("d", "error") in severities
