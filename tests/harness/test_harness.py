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


def test_seam_without_verify_needs_a_waiver(harness: Path):
    (harness / "contracts.yaml").write_text(
        """
seams:
  - name: checked
    owner: app
    verify: make check
  - name: waived
    owner: app
    verify: null
    verify_waiver: {reason: the consumer repo does not exist yet, revisit: when it does}
  - name: forgotten
    owner: app
    verify: null
""",
        encoding="utf-8",
    )
    contracts = next(r for r in validate_harness(harness) if r.kind == "contracts")
    assert contracts.ok, [str(i) for i in contracts.issues]
    assert [(i.path, i.severity) for i in contracts.issues] == [("seams[2]", "warning")]


def test_inserted_steps_may_use_fractional_order(harness: Path):
    plan = harness / "plans/active/demo-plan.yaml"
    plan.write_text(plan.read_text() + "  - id: 3\n    what: squeezed in\n    order: 1.5\n", encoding="utf-8")
    report = next(r for r in validate_harness(harness) if r.kind == "plan")
    assert report.ok, [str(i) for i in report.issues]


def _knowledge_report(harness: Path):
    return next(r for r in validate_harness(harness) if r.kind == "knowledge")


IDENTIFIERS = """identifiers:
  - kind: UseCase
    pattern: '\\bKB-\\d{2}\\b'
  - kind: Screen
    pattern: '\\bSCR-\\d+\\b'
"""


def test_ontology_extends_the_identifier_kinds(harness: Path):
    text = (harness / "knowledge.yaml").read_text()
    write(harness / "knowledge.yaml", text + "ontology: ontology.yaml\n" + IDENTIFIERS)
    write(harness / "ontology.yaml", "node_kinds:\n  - {name: Screen, description: a UI screen}\nrelations: [shows]\n")
    report = _knowledge_report(harness)
    assert report.ok, [str(i) for i in report.issues]


def test_ontology_must_be_a_conservative_extension(harness: Path):
    text = (harness / "knowledge.yaml").read_text()
    write(harness / "knowledge.yaml", text + "ontology: ontology.yaml\n" + IDENTIFIERS)
    write(harness / "ontology.yaml", "node_kinds: [Document]\nrelations: [{name: mentions}]\nsame_as: []\n")
    messages = {(i.path, i.message) for i in errors(_knowledge_report(harness).issues)}
    hub_kind = "ontology.yaml: node_kinds[0]: 'Document' is a hub kind; extend it with properties instead"
    assert ("ontology", hub_kind) in messages
    assert ("ontology", "ontology.yaml: relations[0]: 'mentions' is a hub relation") in messages
    assert ("ontology", "ontology.yaml: same_as: an extension may not declare two existing things equal") in messages
    assert ("identifiers[1].kind", "'Screen' is neither a hub kind nor declared in ontology.yaml") in messages
    assert not any(path == "identifiers[0].kind" for path, _ in messages)


def test_missing_ontology_file_is_an_error(harness: Path):
    text = (harness / "knowledge.yaml").read_text()
    write(harness / "knowledge.yaml", text + "ontology: ontology.yaml\n")
    report = _knowledge_report(harness)
    assert [str(i) for i in report.issues] == ["error: ontology: ontology.yaml: file not found"]


def test_ontology_in_another_format_is_rejected_when_referenced(harness: Path):
    text = (harness / "knowledge.yaml").read_text()
    write(harness / "knowledge.yaml", text + "ontology: ontology.yaml\n")
    write(harness / "ontology.yaml", "node_kinds:\n  parsed_from_srs: [Entity, UseCase]\n")
    messages = [i.message for i in errors(_knowledge_report(harness).issues)]
    assert messages == ["ontology.yaml: node_kinds: must be a list of names or of mappings with a name"]


def test_ontology_path_is_relative_to_knowledge_yaml(harness: Path):
    text = (harness / "knowledge.yaml").read_text()
    (harness / "knowledge.yaml").unlink()
    manifest = (harness / "harness.yaml").read_text()
    write(
        harness / "harness.yaml",
        manifest.replace("knowledge_file: knowledge.yaml", "knowledge_file: kg/knowledge.yaml"),
    )
    write(harness / "kg/knowledge.yaml", text + "ontology: ontology.yaml\n" + IDENTIFIERS)
    write(harness / "kg/ontology.yaml", "node_kinds: [Screen]\n")
    write(harness / "ontology.yaml", "node_kinds: [Document]\n")  # not the one knowledge.yaml means
    report = _knowledge_report(harness)
    assert report.ok, [str(i) for i in report.issues]


def test_unreferenced_ontology_is_not_checked(harness: Path):
    """Without the ontology key nothing changes: another tool's ontology.yaml stays out of validation
    and identifier kinds are free."""
    text = (harness / "knowledge.yaml").read_text()
    write(harness / "knowledge.yaml", text + IDENTIFIERS)
    write(harness / "ontology.yaml", "version: 1\nnode_kinds:\n  parsed_from_srs: [Entity, UseCase]\n")
    report = _knowledge_report(harness)
    assert report.issues == []


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
