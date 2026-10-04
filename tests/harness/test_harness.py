import copy
from pathlib import Path

import pytest
import yaml

from evo_agents.cli import main
from evo_agents.harness import (
    find_manifest,
    load_manifest,
    load_schema,
    load_yaml,
    plan_digest,
    plan_semantics,
    repo_paths,
    validate_harness,
)
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
    evidence: app@abc1234
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
    assert not [str(i) for r in reports for i in r.issues]


def test_unknown_keys_warn_but_do_not_fail(harness: Path):
    text = (harness / "harness.yaml").read_text() + "invented_key: 1\n"
    (harness / "harness.yaml").write_text(text)
    report = validate_harness(harness)[0]
    assert report.ok
    assert any(i.severity == "warning" and i.path == "invented_key" for i in report.issues)


def test_git_source_allow_is_a_known_key(harness: Path):
    text = (harness / "knowledge.yaml").read_text()
    (harness / "knowledge.yaml").write_text(text + '    allow: [".claude/CLAUDE.md"]\n')
    assert not _knowledge_report(harness).issues
    (harness / "knowledge.yaml").write_text(text + "    allow: .claude/CLAUDE.md\n")
    assert [i.path for i in errors(_knowledge_report(harness).issues)] == ["sources[1].allow"]


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


def test_step_with_order_but_no_id_warns(harness: Path):
    plan = harness / "plans/active/demo-plan.yaml"
    plan.write_text(plan.read_text() + "  - order: 3\n    what: numbered the old way\n", encoding="utf-8")
    report = next(r for r in validate_harness(harness) if r.kind == "plan")
    assert report.ok
    assert [(i.path, i.severity) for i in report.issues] == [("steps[2]", "warning")]
    assert report.issues[0].message.startswith("order without id")


def test_repos_named_by_plans_and_seams_must_be_declared(harness: Path, capsys):
    manifest = harness / "harness.yaml"
    manifest.write_text(manifest.read_text() + "  - name: vendor-lib\n    external: true\n", encoding="utf-8")
    (harness / "plans/active/demo-plan.yaml").write_text(
        """
id: demo-plan
repos:
  - repo: app
  - repo: ghost
steps:
  - id: 1
    what: the harness repo goes by its directory name
    repo: demo-harness
  - id: 2
    what: an outside repo declared external
    repo: vendor-lib
  - id: 3
    what: the cluster name is not a repo
    repo: demo
""",
        encoding="utf-8",
    )
    (harness / "contracts.yaml").write_text(
        """
seams:
  - name: api
    owner: ghost
    consumers: [app, phantom]
    verify: make check
    source:
      - {repo: app, path: api/schema.json}
      - {repo: phantom, path: client/api.ts}
""",
        encoding="utf-8",
    )
    reports = validate_harness(harness)
    assert all(r.ok for r in reports), [str(i) for r in reports for i in r.issues]
    by_kind = {r.kind: [(i.path, i.severity) for i in r.issues] for r in reports}
    assert by_kind["harness"] == []
    assert by_kind["plan"] == [("repos[1].repo", "warning"), ("steps[2].repo", "warning")]
    assert by_kind["contracts"] == [
        ("seams[0].owner", "warning"),
        ("seams[0].consumers[1]", "warning"),
        ("seams[0].source[1].repo", "warning"),
    ]
    contracts = next(r for r in reports if r.kind == "contracts")
    assert "'ghost' is not declared in harness.yaml" in contracts.issues[0].message
    assert main(["harness", "validate", str(harness)]) == 0
    assert "warning(s)" in capsys.readouterr().out


def test_seam_source_should_be_a_list_of_repo_path(harness: Path):
    (harness / "contracts.yaml").write_text(
        """
seams:
  - {name: listed, owner: app, verify: make a, source: [{repo: app, path: a.py}]}
  - {name: plain, owner: app, verify: make b, source: b.py}
  - {name: single, owner: app, verify: make c, source: {repo: app, path: c.py}}
  - {name: mixed, owner: app, verify: make d, source: [{repo: app, path: d.py}, e.py]}
  - {name: none, owner: app, verify: make e}
""",
        encoding="utf-8",
    )
    contracts = next(r for r in validate_harness(harness) if r.kind == "contracts")
    assert contracts.ok, [str(i) for i in contracts.issues]
    assert [(i.path, i.severity) for i in contracts.issues] == [
        ("seams[1].source", "warning"),
        ("seams[2].source", "warning"),
        ("seams[3].source[1]", "warning"),
    ]


FULL_SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.mark.parametrize(
    ("evidence", "message"),
    [
        ("commit abc1234 on main", "bare commit abc1234: write repo@abc1234"),
        ("tests pass at `abc1234`.", "bare commit abc1234: write repo@abc1234"),
        ("app abc1234 and app 1234abc", "bare commits abc1234, 1234abc: write repo@sha, e.g. app@abc1234"),
        ("app commit abc1234", "bare commit abc1234: write app@abc1234"),
        ("range abc1234..def5678", "bare commit abc1234: write repo@abc1234"),
        (f"merged as {FULL_SHA}", f"bare commit {FULL_SHA}: write repo@{FULL_SHA}"),
        ("app@abc1234 and app@abc1234..def5678", None),
        ("image sha256:abc1234def5, digest 0a1b2c3d, user_id 0a1b2c3d, sha256 0a1b2c3d", None),
        ("tenant 0a1b2c3d-1234-4abc-8def-0123456789ab, run 1234567890, build 20261002", None),
        ("https://example.test/org/app/commit/abc1234 and key=abc1234 and 0xabc1234", None),
        ("account 0123456789abcdef0123456789abcdef, truncated 0a1b2c3d…, word deadbeefcafe", None),
    ],
)
def test_bare_commits_in_evidence(harness: Path, evidence: str, message: str | None):
    data = {"id": "demo-plan", "steps": [{"id": 1, "what": "x", "status": "done", "evidence": evidence}]}
    issues = plan_semantics(data, harness / "plans/active/demo-plan.yaml", load_manifest(harness))
    assert [(i.path, i.message, i.severity) for i in issues] == (
        [("steps[0].evidence", message, "warning")] if message else []
    )


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


HUB_SINK = "    - id: hub\n      kind: hub\n      clearance: {level: internal}\n"
DIGEST = "sha256:" + "0" * 64


def add_hub_sink(harness: Path, sink: str = HUB_SINK) -> None:
    path = harness / "knowledge.yaml"
    path.write_text(path.read_text().replace("sources:\n", sink + "sources:\n"), encoding="utf-8")


def report(harness: Path, kind: str):
    return next(r for r in validate_harness(harness) if r.kind == kind)


def test_the_hub_keys_validate(harness: Path):
    add_hub_sink(harness)
    manifest = harness / "harness.yaml"
    manifest.write_text(manifest.read_text() + "hub: {project: demo}\n", encoding="utf-8")
    plan = harness / "plans/active/demo-plan.yaml"
    digest = plan_digest(load_yaml(plan))  # with hub.project set, the digest must be the plan's own
    plan.write_text(plan.read_text() + f"hub: {{project: demo, revision: 3, digest: '{digest}'}}\n", encoding="utf-8")
    reports = validate_harness(harness)
    assert all(r.ok for r in reports) and not [str(i) for r in reports for i in r.issues]


@pytest.mark.parametrize(
    "kind, text, path",
    [
        ("harness", "hub: {}\n", "hub.project"),
        ("harness", "hub: {project: Demo}\n", "hub.project"),
        ("plan", f"hub: {{project: demo, revision: 0, digest: '{DIGEST}'}}\n", "hub.revision"),
        ("plan", "hub: {project: demo, revision: 1, digest: 'abc'}\n", "hub.digest"),
        ("plan", f"hub: {{project: demo, digest: '{DIGEST}'}}\n", "hub.revision"),
    ],
)
def test_malformed_hub_keys_are_errors(harness: Path, kind: str, text: str, path: str):
    target = harness / ("harness.yaml" if kind == "harness" else "plans/active/demo-plan.yaml")
    target.write_text(target.read_text() + text, encoding="utf-8")
    assert path in [i.path for i in errors(report(harness, kind).issues)]


def test_older_schemas_only_warn_on_the_hub_keys(harness: Path):
    """Releases before the hub keys read them as unknown keys: a warning, so a harness carrying them still
    validates there. Only ``kind: hub`` in a sink needs this release (the seam knowledge-manifest-v1)."""
    manifest = load_yaml(harness / "harness.yaml") | {"hub": {"project": "demo"}}
    plan = load_yaml(harness / "plans/active/demo-plan.yaml") | {
        "hub": {"project": "demo", "revision": 1, "digest": DIGEST}
    }
    for data, name in ((manifest, "harness"), (plan, "plan")):
        older = copy.deepcopy(load_schema(name))
        del older["properties"]["hub"]
        issues = validate(data, older)
        assert [(i.path, i.severity) for i in issues] == [("hub", "warning")]
        assert validate(data, load_schema(name)) == []


def test_a_project_declares_one_hub_sink(harness: Path):
    add_hub_sink(harness, HUB_SINK + HUB_SINK.replace("id: hub", "id: hub-2"))
    knowledge = report(harness, "knowledge")
    assert any("are all of kind hub" in i.message for i in errors(knowledge.issues))


def test_harness_and_knowledge_must_agree_on_the_hub_project(harness: Path):
    manifest = harness / "harness.yaml"
    manifest.write_text(manifest.read_text() + "hub: {project: other}\n", encoding="utf-8")
    knowledge = report(harness, "knowledge")
    assert any("hub.project 'other'" in i.message for i in errors(knowledge.issues))


def test_a_hub_sink_below_the_harness_label_is_a_warning(harness: Path):
    add_hub_sink(harness, HUB_SINK.replace("level: internal", "level: public"))
    knowledge = report(harness, "knowledge")
    assert knowledge.ok
    (warning,) = [i for i in knowledge.issues if i.severity == "warning"]
    assert warning.path == "sources[0].label" and "hub sink 'hub' does not clear" in warning.message


def hub_managed(harness: Path, revision: int = 4) -> Path:
    """The demo harness with its plans on the hub: hub.project in harness.yaml and the plan as the hub's copy."""
    from evo_agents.hub.mirror import read_plan, render

    add_hub_sink(harness)
    manifest = harness / "harness.yaml"
    manifest.write_text(manifest.read_text() + "hub: {project: demo}\n", encoding="utf-8")
    plan = harness / "plans/active/demo-plan.yaml"
    body = read_plan(plan).body
    plan.write_text(render(body, "demo", revision, plan_digest(body)), encoding="utf-8")
    return plan


def mirror_errors(harness: Path) -> list[tuple[str, str]]:
    return [(i.path, i.message) for i in errors(report(harness, "plan").issues)]


def test_a_copy_the_hub_wrote_validates(harness: Path):
    plan = hub_managed(harness)
    assert plan.read_text().startswith(
        "# Mirror of evo-agents hub plan demo-plan, revision 4. Do not edit; use evo harness step or evo-agents "
        "hub plan.\n"
    )
    assert all(r.ok for r in validate_harness(harness))


def test_a_hand_edited_copy_fails_validate_with_the_way_back(harness: Path, capsys):
    plan = hub_managed(harness)
    plan.write_text(plan.read_text().replace("what: second", "what: second, edited by hand"), encoding="utf-8")
    message = (
        "plans/active/demo-plan.yaml was edited outside the hub (digest mismatch). Push it with "
        "`evo-agents hub plan put plans/active/demo-plan.yaml --if-revision 4` or restore it with "
        "`evo-agents hub plan export .`"
    )
    assert mirror_errors(harness) == [("hub.digest", message)]
    assert main(["harness", "validate", str(harness)]) == 1
    assert message in capsys.readouterr().out


def test_the_digest_ignores_key_order_comments_and_yaml_style(harness: Path):
    plan = hub_managed(harness)
    data = load_yaml(plan)
    reordered = {key: data[key] for key in reversed(list(data))}
    text = "# a comment of my own\n" + yaml.safe_dump(reordered, default_flow_style=True, width=1000)
    plan.write_text(text, encoding="utf-8")
    assert mirror_errors(harness) == []
    assert plan_digest(load_yaml(plan)) == data["hub"]["digest"]


def test_a_plan_without_a_hub_key_or_of_another_project_is_an_error(harness: Path):
    plan = hub_managed(harness)
    original = plan.read_text()
    plan.write_text(original.replace("project: demo", "project: other"), encoding="utf-8")
    ((path, message),) = mirror_errors(harness)
    assert path == "hub.project" and "hub project 'other'" in message and "hub plan export ." in message

    data = load_yaml(plan)
    del data["hub"]
    plan.write_text(yaml.safe_dump(data), encoding="utf-8")
    ((path, message),) = mirror_errors(harness)
    assert path == "hub" and "`evo-agents hub plan put plans/active/demo-plan.yaml`" in message


def test_without_hub_project_plans_are_not_checked_as_copies(harness: Path):
    plan = harness / "plans/active/demo-plan.yaml"
    plan.write_text(plan.read_text() + f"hub: {{project: demo, revision: 2, digest: '{DIGEST}'}}\n", encoding="utf-8")
    assert all(r.ok for r in validate_harness(harness))  # a digest that matches nothing, and no hub.project


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
