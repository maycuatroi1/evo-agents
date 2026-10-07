"""Projects on the hub: the read rule as a table of labels, grants, sink clearances and project sets; the write rule,
whose refusals write nothing; registering a harness's project (a hub admin first, idempotent, audited without
content); and ``hub registry pull``, which writes the hub's clusters and leaves every other cluster as it was.

The rules and the registry run without Postgres; everything that needs the hub skips without EVO_HUB_TEST_DSN."""

import json
import logging
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import pytest

from evo_agents.hub import client as hub_client
from evo_agents.hub import registry as hub_registry
from evo_agents.hub.access import ProjectRules, Refused
from evo_agents.hub.client import HubError
from evo_agents.hub.registration import place, portable, registration, relative
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys

LEVELS = ["public", "internal", "customer", "secret"]
LOCATIONS = ["any", "domestic-only"]
AGENT = "claude-code@anthropic"


def rules(sinks=None, default_label=None, name="demo") -> ProjectRules:
    if sinks is None:
        sinks = [
            {"id": AGENT, "kind": "agent-session", "clearance": {"level": "customer"}},
            {"id": "agent-any", "kind": "agent-session", "clearance": {"level": "secret", "location": "any"}},
            {"id": "hub", "kind": "hub", "clearance": {"level": "internal"}},
            {"id": "typo", "kind": "cli", "clearance": {"level": "top-secret"}},
        ]
    return ProjectRules(name, LEVELS, LOCATIONS, sinks, default_label)


# The read rule


@pytest.mark.parametrize(
    "label, max_level, sink, visible",
    [
        # a reader whose grant reaches internal does not see a customer object, whatever the sink clears
        ({"level": "customer"}, "internal", AGENT, False),
        ({"level": "customer"}, "internal", "agent-any", False),
        ({"level": "internal"}, "internal", AGENT, True),
        ({"level": "public", "integrity": "T"}, "internal", AGENT, True),
        # the sink bounds what a higher grant sees through it
        ({"level": "customer"}, "customer", AGENT, True),
        ({"level": "secret"}, "secret", AGENT, False),
        ({"level": "secret"}, "secret", "agent-any", True),
        # locations: a sink without one admits every location, one with "any" admits only unrestricted objects
        ({"level": "internal", "location": "domestic-only"}, "secret", AGENT, True),
        ({"level": "internal", "location": "domestic-only"}, "secret", "agent-any", False),
        # the projects of the label must be a subset of {demo}
        ({"level": "public", "projects": ["demo"]}, "secret", AGENT, True),
        ({"level": "public", "projects": []}, "secret", AGENT, True),
        ({"level": "public", "projects": ["demo", "other"]}, "secret", AGENT, False),
        ({"level": "public", "projects": ["other"]}, "secret", AGENT, False),
        # fail closed: no grant, a max level or sink the project lacks, a sink clearing an unknown level
        ({"level": "public"}, None, AGENT, False),
        ({"level": "public"}, "top-secret", AGENT, False),
        ({"level": "public"}, "secret", "no-such-sink", False),
        ({"level": "public"}, "secret", "typo", False),
        # an unknown level or location reads as the highest, an unreadable label hides its object
        ({"level": "galactic"}, "customer", AGENT, False),
        ({"level": "galactic"}, "secret", "agent-any", True),
        ({"level": "public", "location": "moon"}, "secret", "agent-any", False),
        ({"level": "public", "projects": "demo"}, "secret", AGENT, False),
        ({}, "customer", AGENT, False),
        (None, "secret", AGENT, False),
        ("public", "secret", AGENT, False),
    ],
)
def test_the_read_rule(label, max_level, sink, visible):
    assert rules().visible(label, max_level, sink) is visible


def test_the_read_rule_across_levels_grants_clearances_and_project_sets():
    """Every object level, every grant (none and an unknown one included), every sink clearance and four project
    sets, against the rule written as index arithmetic: L <= min(M, C) and projects within {demo}."""
    sinks = [{"id": f"s-{level}", "kind": "agent-session", "clearance": {"level": level}} for level in LEVELS]
    project = rules(sinks)
    checked = 0
    for object_level in LEVELS:
        for max_level in [*LEVELS, None, "unknown"]:
            for clearance in LEVELS:
                for projects in (["demo"], [], ["demo", "other"], ["other"]):
                    label = {"level": object_level, "projects": projects}
                    expected = (
                        max_level in LEVELS
                        and LEVELS.index(object_level) <= min(LEVELS.index(max_level), LEVELS.index(clearance))
                        and set(projects) <= {"demo"}
                    )
                    assert project.visible(label, max_level, f"s-{clearance}") is expected, (
                        label,
                        max_level,
                        clearance,
                    )
                    checked += 1
    assert checked == 4 * 6 * 4 * 4


# The write rule


def refused(project: ProjectRules, label, role="writer") -> Refused:
    with pytest.raises(Refused) as caught:
        project.check_push(label, role)
    return caught.value


def test_the_write_rule_needs_a_writer_and_a_label_the_hub_sink_clears():
    project = rules(default_label={"level": "internal", "location": "any", "integrity": "U", "projects": ["demo"]})
    assert project.check_push({"level": "internal"}, "writer") == {
        "level": "internal",
        "location": "any",
        "integrity": "U",
        "projects": ["demo"],
    }
    assert project.check_push({"level": "public", "integrity": "T"}, "admin")["integrity"] == "T"
    assert project.check_push(None, "writer")["level"] == "internal"  # the harness source's label by default

    assert refused(project, {"level": "public"}, role="reader").status == 403
    assert refused(project, {"level": "public"}, role=None).status == 403
    above = refused(project, {"level": "customer"})
    assert above.status == 422 and "customer/any is above internal/domestic-only" in str(above)
    assert "Nothing was written" in str(above)
    other = refused(project, {"level": "public", "projects": ["demo", "other"]})
    assert other.status == 422 and "other projects (other)" in str(other)
    unknown = refused(project, {"level": "galactic"})
    assert unknown.status == 422 and "public, internal, customer, secret" in str(unknown)
    assert refused(project, {"level": "public", "location": "moon"}).status == 422
    assert refused(project, {"level": "public", "integrity": "X"}).status == 422
    assert refused(project, "public").status == 422


def test_a_hub_sink_that_clears_a_location_bounds_it_too():
    meridai = ProjectRules(
        "meridai",
        LEVELS,
        LOCATIONS,
        [{"id": "hub", "kind": "hub", "clearance": {"level": "customer", "location": "any"}}],
    )
    assert meridai.check_push({"level": "customer"}, "writer")["location"] == "any"
    assert refused(meridai, {"level": "customer", "location": "domestic-only"}).status == 422


def test_a_project_without_a_hub_sink_refuses_every_push_naming_the_sink_to_declare():
    agent_only = [{"id": AGENT, "kind": "agent-session", "clearance": {"level": "secret"}}]
    default = {"level": "customer", "location": "domestic-only", "integrity": "U", "projects": ["demo"]}
    project = rules(agent_only, default_label=default)
    for label in (None, {"level": "public"}):
        refusal = refused(project, label)
        assert refusal.status == 422
        assert "declares no sink of kind hub" in str(refusal)
        assert "{id: hub, kind: hub, clearance: {level: customer, location: domestic-only}}" in str(refusal)
        assert "evo-agents hub project register" in str(refusal)
    assert refused(project, {"level": "public"}, role="reader").status == 403  # the role is checked first


# Registration: what the client reads from a harness


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


HARNESS_YAML = """
name: demo
workspace: ~/ws
knowledge_file: knowledge.yaml
hub: {project: demo}
repos:
  - name: app
    path: ~/ws/app
    origin: https://example.org/app.git
    default_branch: main
  - name: tools
    path: tools
  - name: shared
    path: /opt/shared-lib
    external: true
  - name: notes
    path: ~/notes
"""
KNOWLEDGE_YAML = """
version: 1
project: demo
policy:
  levels: [public, internal, customer, secret]
  locations: [any, domestic-only]
  sinks:
    - id: claude-code@anthropic
      kind: agent-session
      clearance: {level: internal}
    - id: hub
      kind: hub
      clearance: {level: internal}
sources:
  - id: harness
    connector: harness
    label: {level: internal, integrity: U}
  - id: app
    connector: git
    repo: app
    label: {level: public, integrity: U}
"""


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A home directory of its own, so no test reads or writes the real ~/.claude or ~/.evo."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    return path


def make_harness(home: Path, harness: str = HARNESS_YAML, knowledge: str | None = KNOWLEDGE_YAML) -> Path:
    root = home / "ws" / "demo-harness"
    write(root / "harness.yaml", harness)
    if knowledge is not None:
        write(root / "knowledge.yaml", knowledge)
    return root


def test_registration_reads_the_harness_with_the_loader(home):
    root = make_harness(home)
    found = registration(root / "plans")  # any directory inside the harness
    assert found.project == "demo" and found.root == Path(os.path.realpath(root))
    body = found.body
    assert body["levels"] == LEVELS and body["locations"] == LOCATIONS
    assert body["default_label"] == {"level": "internal", "location": "any", "integrity": "U"}
    assert body["sinks"] == [
        {"id": AGENT, "kind": "agent-session", "clearance": {"level": "internal"}},
        {"id": "hub", "kind": "hub", "clearance": {"level": "internal"}},
    ]
    # Paths another machine can rebuild: relative to the workspace, ~/ under the home, absolute elsewhere.
    assert body["harness"] == {"name": "demo", "workspace": "~/ws", "path": "demo-harness"}
    assert body["repos"] == [
        {"name": "app", "origin": "https://example.org/app.git", "default_branch": "main", "path": "app"},
        {"name": "tools", "origin": None, "default_branch": None, "path": "tools"},
        {"name": "shared", "origin": None, "default_branch": None, "path": os.path.realpath("/opt/shared-lib")},
        {"name": "notes", "origin": None, "default_branch": None, "path": "~/notes"},
    ]
    assert found.push_warning() is None


def test_registration_without_knowledge_yaml_is_fail_closed(home):
    root = make_harness(home, HARNESS_YAML.replace("knowledge_file: knowledge.yaml\n", ""), knowledge=None)
    found = registration(root)
    assert found.project == "demo"  # the harness.yaml name
    assert found.body["levels"] == LEVELS and found.body["sinks"] == []
    assert found.body["default_label"]["level"] == "secret"  # no harness source: the highest level
    assert "declares no sink of kind hub" in found.push_warning()


def test_registration_refuses_a_harness_with_errors_or_a_mismatched_hub_project(home):
    root = make_harness(home)
    hub_sink = "kind: hub\n      clearance: {level: internal}"
    write(root / "knowledge.yaml", KNOWLEDGE_YAML.replace(hub_sink, "kind: hub\n      clearance: {level: top}"))
    with pytest.raises(HubError, match=r"fix the harness first: knowledge.yaml: .*'top' is not in policy.levels"):
        registration(root)
    write(root / "knowledge.yaml", KNOWLEDGE_YAML)
    write(root / "harness.yaml", HARNESS_YAML.replace("hub: {project: demo}", "hub: {project: other}"))
    with pytest.raises(HubError, match="hub.project"):
        registration(root)
    with pytest.raises(HubError, match="no harness.yaml at or above"):
        registration(home)


def test_paths_travel_relative_to_the_workspace(home):
    workspace = home / "ws"
    assert portable(workspace) == "~/ws" and portable(home) == "~"
    assert portable(Path("/opt/x")) == os.path.realpath("/opt/x")
    assert relative(workspace / "app", workspace) == "app"
    assert relative(home / "elsewhere", workspace) == "~/elsewhere"
    other = Path("/srv/other-machine/code")
    assert place("app", other) == other / "app"
    assert place("~/elsewhere", other) == home / "elsewhere"
    assert place("/opt/shared-lib", other) == Path("/opt/shared-lib")


# hub registry pull


class FakeHub:
    """Hub.call answering GET /v1/projects from a list."""

    url = "https://hub.test"

    def __init__(self, projects):
        self.projects = projects

    def call(self, method, path, body=None):
        assert (method, path) == ("GET", "/v1/projects")
        return self.projects


def hub_project(name="demo", cluster="demo", workspace="~/ws", path="demo-harness", repos=None) -> dict:
    if repos is None:
        repos = [{"name": "app", "path": "app"}, {"name": "shared", "path": "/opt/shared-lib"}, {"name": "bare"}]
    harness = {"name": cluster, "workspace": workspace, "path": path} if cluster else None
    return {"name": name, "harness": harness, "repos": repos}


FOREIGN = [
    {
        "name": "lms",
        "root": "/Users/someone/github/evo-lms-harness",
        "workspace": "/Users/someone/github",
        "repos": ["/Users/someone/github/evo-lms-django", "/Users/someone/github/evo-harness"],
        "registered_at": "2026-07-11T17:11:26+07:00",
    },
    {"name": "ghi-chú", "root": "/Users/someone/ghi chú", "note": "tiếng Việt có dấu", "weights": [1.5, 2e-3]},
    "a stray string some other tool left here",
]


def skill_format(data) -> bytes:
    """The registry as the harness-engineering skill writes it."""
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()


def fragment(entry, indent: int = 4) -> bytes:
    """One cluster of a registry in skill format, as it sits in the file's clusters list."""
    text = json.dumps(entry, indent=2, ensure_ascii=False)
    return "\n".join(" " * indent + line if i else line for i, line in enumerate(text.splitlines())).encode()


@pytest.fixture
def registry(home) -> Path:
    stale = {
        "name": "demo",
        "root": "/old/place/demo-harness",
        "workspace": "/old/place",
        "repos": ["/old/place/app"],
        "registered_at": "2026-09-01T00:00:00+00:00",
        "kept": "a key the pull does not own",
    }
    path = home / ".claude" / "harness" / "registry.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(skill_format({"version": 3, "clusters": [FOREIGN[0], stale, FOREIGN[1], FOREIGN[2]]}))
    os.chmod(path, 0o640)
    return path


def test_registry_pull_updates_hub_clusters_and_leaves_the_others_byte_identical(home, registry):
    original = registry.read_bytes()
    result = hub_registry.pull(FakeHub([hub_project(), hub_project("new", "new-cluster", path="new-harness")]))
    assert result.registry == registry and result.changed

    # The backup is the file exactly as it was, next to it, with its mode.
    assert result.backup.parent == registry.parent and result.backup.name.startswith("registry.json.bak.")
    assert result.backup.read_bytes() == original
    assert stat.S_IMODE(os.stat(result.backup).st_mode) == 0o640
    assert stat.S_IMODE(os.stat(registry).st_mode) == 0o640

    data = json.loads(registry.read_bytes())
    assert data["version"] == 3
    clusters = data["clusters"]
    assert clusters[0] == FOREIGN[0] and clusters[2] == FOREIGN[1] and clusters[3] == FOREIGN[2]
    for entry in FOREIGN[:2]:
        assert fragment(entry) in original and fragment(entry) in registry.read_bytes()

    ws = home / "ws"
    demo = clusters[1]  # updated in place, keeping a key it does not own
    assert demo["root"] == str(ws / "demo-harness") and demo["workspace"] == str(ws)
    assert demo["repos"] == [str(ws / "app"), "/opt/shared-lib", str(ws / "bare")]
    assert (
        demo["hub"] == {"url": "https://hub.test", "project": "demo"} and demo["kept"] == "a key the pull does not own"
    )
    assert demo["registered_at"] != "2026-09-01T00:00:00+00:00"
    assert clusters[4]["name"] == "new-cluster" and clusters[4]["root"] == str(ws / "new-harness")
    assert [(c["name"], c["status"], c["present"]) for c in result.clusters] == [
        ("demo", "updated", False),
        ("new-cluster", "added", False),
    ]
    assert sorted(p.name for p in registry.parent.iterdir()) == ["registry.json", result.backup.name]

    # A second pull finds nothing to change: no write, no new backup.
    written = registry.stat()
    again = hub_registry.pull(FakeHub([hub_project(), hub_project("new", "new-cluster", path="new-harness")]))
    assert not again.changed and again.backup is None
    assert {c["status"] for c in again.clusters} == {"unchanged"}
    assert registry.stat().st_mtime_ns == written.st_mtime_ns and registry.stat().st_ino == written.st_ino
    assert len(list(registry.parent.iterdir())) == 2


def test_registry_pull_places_paths_in_the_workspace_of_this_machine(home, tmp_path):
    other = tmp_path / "elsewhere" / "code"
    path = tmp_path / "custom" / "registry.json"
    result = hub_registry.pull(FakeHub([hub_project()]), registry=path, workspace=other)
    assert result.backup is None  # there was no file to back up
    (cluster,) = json.loads(path.read_bytes())["clusters"]
    assert cluster["root"] == str(other / "demo-harness") and cluster["workspace"] == str(other)
    assert cluster["repos"] == [str(other / "app"), "/opt/shared-lib", str(other / "bare")]
    assert not (home / ".claude").exists()  # the default registry was never touched


def test_registry_pull_skips_projects_without_paths_and_refuses_a_broken_registry(home, registry):
    original = registry.read_bytes()
    result = hub_registry.pull(FakeHub([hub_project("old", cluster=None)]))
    assert not result.changed and result.clusters == [] and registry.read_bytes() == original
    assert "registered without its harness paths" in result.skipped[0]

    for broken, message in (('{"clusters": [', "is not valid JSON"), ('{"clusters": {}}', "a clusters list")):
        registry.write_text(broken, encoding="utf-8")
        with pytest.raises(HubError, match=message):
            hub_registry.pull(FakeHub([hub_project()]))
        assert registry.read_text(encoding="utf-8") == broken
    assert sorted(p.name for p in registry.parent.iterdir()) == ["registry.json"]


def test_registry_pull_never_overwrites_a_registry_changed_while_it_ran(home, registry, monkeypatch):
    def another_program_writes():
        registry.write_bytes(skill_format({"clusters": [FOREIGN[0]]}))
        return hub_registry.datetime.now(hub_registry.UTC)

    monkeypatch.setattr(hub_registry, "_now", another_program_writes)
    with pytest.raises(HubError, match="changed while the pull ran"):
        hub_registry.pull(FakeHub([hub_project()]))
    assert registry.read_bytes() == skill_format({"clusters": [FOREIGN[0]]})
    assert sorted(p.name for p in registry.parent.iterdir()) == ["registry.json"]  # no backup left behind


def test_a_failed_write_leaves_the_registry_whole_and_its_backup(home, registry, monkeypatch):
    original = registry.read_bytes()
    replace = os.replace
    calls = []

    def fail_the_second(src, dst):  # the backup goes through, the registry itself does not
        calls.append(dst)
        if len(calls) == 2:
            raise OSError("disk full")
        replace(src, dst)

    monkeypatch.setattr(hub_client.os, "replace", fail_the_second)
    with pytest.raises(OSError, match="disk full"):
        hub_registry.pull(FakeHub([hub_project()]))
    monkeypatch.undo()
    assert registry.read_bytes() == original
    (backup,) = registry.parent.glob("registry.json.bak.*")
    assert backup.read_bytes() == original
    assert sorted(p.name for p in registry.parent.iterdir()) == ["registry.json", backup.name]  # no temporary file


# The server: registration, listing and access


needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)


def body(**changes) -> dict:
    """A registration as `hub project register` sends it."""
    values = {
        "levels": LEVELS,
        "locations": LOCATIONS,
        "default_label": {"level": "internal", "location": "any", "integrity": "U"},
        "sinks": [
            {"id": AGENT, "kind": "agent-session", "clearance": {"level": "customer"}},
            {"id": "hub", "kind": "hub", "clearance": {"level": "internal"}},
        ],
        "repos": [
            {"name": "app", "origin": "https://example.org/secret-app.git", "default_branch": "main", "path": "app"}
        ],
        "harness": {"name": "demo", "workspace": "~/ws", "path": "demo-harness"},
    }
    values.update(changes)
    return values


def add_probe(app) -> None:
    """Routes standing in for the memory endpoints of later steps: they read and push rows of ``memories`` through
    ``project_access``, the way those endpoints start, and audit a push in the transaction that writes it."""
    from fastapi import Body, Request

    from evo_agents.hub.db import legacy
    from evo_agents.hub.server.projects import project_access
    from evo_agents.hub.server.security import CurrentUser

    async def read(project: str, sink: str, request: Request, user: CurrentUser) -> list[str]:
        async with request.app.state.engine.begin() as conn:
            access = await project_access(conn, user, project)
            cursor = await legacy(
                conn, "SELECT name, label FROM memories WHERE project_id = %s ORDER BY name", (access.project_id,)
            )
            rows = await cursor.fetchall()
        return [name for name, label in rows if access.visible(label, sink)]

    async def push(project: str, request: Request, user: CurrentUser, data: Annotated[dict, Body()]) -> dict:
        from psycopg.types.json import Jsonb

        from evo_agents.hub.server import audit

        async with request.app.state.engine.begin() as conn:
            access = await project_access(conn, user, project)
            label = access.push_label(data.get("label"))
            await legacy(
                conn,
                "INSERT INTO memories (scope, project_id, location, name, type, owner_id, label, body, updated_by) "
                "VALUES ('project', %s, 'harness', %s, 'project', %s, %s, %s, %s)",
                (access.project_id, data["name"], user.user_id, Jsonb(label), data["body"], user.user_id),
            )
            target = f"{project}/{data['name']}"
            await audit.record(conn, actor_id=user.user_id, token_id=user.token_id, action="memory.put", target=target)
        return {"label": label}

    app.add_api_route("/v1/test-probe/{project}", read, methods=["GET"])
    app.add_api_route("/v1/test-probe/{project}", push, methods=["POST"], status_code=201)


@pytest.fixture
def client(hub_db, tmp_path, github):
    from fastapi.testclient import TestClient

    from evo_agents.hub.server.app import create_app

    app = create_app(live.hub_config(hub_db, tmp_path, github))
    add_probe(app)
    with TestClient(app) as client:
        yield client


def audit_rows(db) -> list[tuple]:
    return live.sql(
        db, "SELECT u.login, a.action, a.target FROM audit a JOIN users u ON u.id = a.actor_id ORDER BY a.id"
    )


def content_rows(db) -> dict:
    return {
        table: live.sql(db, f"SELECT count(*) FROM {table}")[0][0]
        for table in ("projects", "project_sinks", "project_repos", "memories", "audit")
    }


def users(client, github) -> dict:
    """Tokens of a hub admin, a member, and a login without any grant."""
    return {
        "admin": live.bearer(live.sign_in(client, github, live.ADMIN, 1)["token"]),
        "member": live.bearer(live.sign_in(client, github, "member", 2)["token"]),
        "stranger": live.bearer(live.sign_in(client, github, "stranger", 3)["token"]),
    }


def grant(client, admin, login: str, role: str, max_level: str, project: str = "demo") -> None:
    response = client.put(
        f"/v1/admin/projects/{project}/grants/{login}", json={"role": role, "max_level": max_level}, headers=admin
    )
    assert response.status_code == 200, response.text


@needs_pg
def test_registering_needs_a_hub_admin_first_and_is_idempotent(client, github, hub_db, caplog):
    caplog.set_level(logging.INFO, logger="evo_agents.hub")
    who = users(client, github)
    refused = client.put("/v1/projects/demo", json=body(), headers=who["member"])
    assert refused.status_code == 403 and refused.json()["error"] == "forbidden"
    assert "needs a hub admin" in refused.json()["message"]
    assert content_rows(hub_db)["projects"] == 0

    created = client.put("/v1/projects/demo", json=body(), headers=who["admin"])
    assert created.status_code == 200, created.text
    project = created.json()
    assert (project["created"], project["changed"]) == (True, True)
    assert project["harness"] == {"name": "demo", "workspace": "~/ws", "path": "demo-harness"}
    assert project["default_label"] == {"level": "internal", "location": "any", "integrity": "U", "projects": ["demo"]}
    assert [s["id"] for s in project["sinks"]] == [AGENT, "hub"] and project["repos"][0]["path"] == "app"
    assert (project["role"], project["max_level"]) == (None, None)  # a hub admin is no member by being one
    rows = content_rows(hub_db)
    assert (rows["projects"], rows["project_sinks"], rows["project_repos"]) == (1, 2, 1)

    # The same body again changes nothing and leaves no audit row.
    before = live.sql(hub_db, "SELECT updated_at FROM projects")
    again = client.put("/v1/projects/demo", json=body(), headers=who["admin"])
    assert (again.json()["created"], again.json()["changed"]) == (False, False)
    assert live.sql(hub_db, "SELECT updated_at FROM projects") == before

    # A project admin may update it; a reader may not, and a stranger learns nothing from the answer.
    grant(client, who["admin"], "member", "reader", "internal")
    for headers in (who["member"], who["stranger"]):
        denied = client.put("/v1/projects/demo", json=body(repos=[]), headers=headers)
        assert denied.status_code == 403 and denied.json()["message"] == refused.json()["message"]
    grant(client, who["admin"], "member", "admin", "internal")
    updated = client.put("/v1/projects/demo", json=body(repos=[]), headers=who["member"])
    assert updated.status_code == 200, updated.text
    assert (updated.json()["changed"], updated.json()["repos"], updated.json()["role"]) == (True, [], "admin")
    assert content_rows(hub_db)["project_repos"] == 0

    # Audit rows name the project and nothing it holds; the log neither.
    actions = [(login, action, target) for login, action, target in audit_rows(hub_db) if action.startswith("project")]
    assert actions == [(live.ADMIN, "project.register", "demo"), ("member", "project.update", "demo")]
    outcomes = [r.outcome for r in caplog.records if r.getMessage() == "project registration"]
    assert outcomes == ["registered", "unchanged", "updated"]
    stored = json.dumps(audit_rows(hub_db)) + caplog.text + json.dumps([vars(r) for r in caplog.records], default=str)
    assert "secret-app" not in stored and "demo-harness" not in stored


@needs_pg
def test_projects_are_listed_to_their_members_only(client, github, hub_db):
    who = users(client, github)
    for name in ("demo", "other"):
        harness = {"name": name, "workspace": "~/ws", "path": f"{name}-harness"}
        assert client.put(f"/v1/projects/{name}", json=body(harness=harness), headers=who["admin"]).status_code == 200
    grant(client, who["admin"], "member", "reader", "internal")

    listed = client.get("/v1/projects", headers=who["member"]).json()
    assert [(p["name"], p["role"], p["max_level"]) for p in listed] == [("demo", "reader", "internal")]
    assert listed[0]["sinks"][1] == {"id": "hub", "kind": "hub", "clearance": {"level": "internal", "location": None}}
    assert [p["name"] for p in client.get("/v1/projects", headers=who["admin"]).json()] == ["demo", "other"]
    assert client.get("/v1/projects", headers=who["stranger"]).json() == []

    assert client.get("/v1/projects/demo", headers=who["member"]).json()["name"] == "demo"
    hidden = client.get("/v1/projects/other", headers=who["member"])
    missing = client.get("/v1/projects/nope", headers=who["member"])
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json()["message"].replace("other", "X") == missing.json()["message"].replace("nope", "X")


@needs_pg
@pytest.mark.parametrize(
    "changes, message",
    [
        ({"levels": ["public", "public"]}, "levels lists public more than once"),
        ({"default_label": {"level": "top"}}, "label level 'top' is not in levels"),
        ({"default_label": {"level": "public", "location": "moon"}}, "label location 'moon' is not in locations"),
        (
            {"sinks": [{"id": "hub", "kind": "hub", "clearance": {"level": "top"}}]},
            "sink hub: clearance level 'top' is not in levels",
        ),
        (
            {
                "sinks": [
                    {"id": "hub", "kind": "hub", "clearance": {"level": "public"}},
                    {"id": "hub-2", "kind": "hub", "clearance": {"level": "public"}},
                ]
            },
            "sinks hub, hub-2 are all of kind hub",
        ),
        ({"sinks": [{"id": "s", "kind": "printer", "clearance": {"level": "public"}}]}, None),
        ({"repos": [{"name": "app"}, {"name": "app"}]}, "repos app are declared twice"),
        ({"harness": {"name": "Demo", "workspace": "~/ws", "path": "h"}}, None),
        ({"harness": {"name": "demo", "workspace": "~/ws\n", "path": "h"}}, None),
    ],
)
def test_a_registration_the_ladder_does_not_resolve_is_422_and_writes_nothing(client, github, hub_db, changes, message):
    admin = users(client, github)["admin"]
    before = content_rows(hub_db)
    response = client.put("/v1/projects/demo", json=body(**changes), headers=admin)
    assert response.status_code == 422, response.text
    if message:
        assert message in response.json()["message"]
    assert content_rows(hub_db) == before
    assert client.put("/v1/projects/Bad_Name", json=body(), headers=admin).status_code == 422


@needs_pg
def test_dropping_a_level_some_grant_reaches_is_refused(client, github, hub_db):
    who = users(client, github)
    assert client.put("/v1/projects/demo", json=body(), headers=who["admin"]).status_code == 200
    grant(client, who["admin"], "member", "reader", "secret")
    before = live.sql(hub_db, "SELECT levels, updated_at FROM projects")
    shorter = body(levels=["public", "internal", "customer"])
    refused = client.put("/v1/projects/demo", json=shorter, headers=who["admin"])
    assert refused.status_code == 409 and "member (secret)" in refused.json()["message"]
    assert live.sql(hub_db, "SELECT levels, updated_at FROM projects") == before
    grant(client, who["admin"], "member", "reader", "customer")
    assert client.put("/v1/projects/demo", json=shorter, headers=who["admin"]).json()["changed"] is True


def memory(db, name: str, label: dict) -> None:
    from psycopg.types.json import Jsonb

    live.sql(
        db,
        "INSERT INTO memories (scope, project_id, location, name, type, owner_id, label, body, updated_by) "
        "SELECT 'project', p.id, 'harness', %s, 'project', p.created_by, %s, 'body', p.created_by "
        "FROM projects p WHERE p.name = 'demo'",
        (name, Jsonb(label)),
    )


@needs_pg
def test_a_reader_with_max_level_internal_does_not_see_a_customer_object(client, github, hub_db):
    who = users(client, github)
    assert client.put("/v1/projects/demo", json=body(), headers=who["admin"]).status_code == 200
    for level in ("public", "internal", "customer", "secret"):
        memory(hub_db, f"{level}.md", {"level": level, "projects": ["demo"]})
    memory(hub_db, "shared.md", {"level": "public", "projects": ["demo", "other"]})

    grant(client, who["admin"], "member", "reader", "internal")
    seen = client.get(f"/v1/test-probe/demo?sink={AGENT}", headers=who["member"])
    assert seen.status_code == 200, seen.text
    assert seen.json() == ["internal.md", "public.md"]

    grant(client, who["admin"], "member", "reader", "secret")  # the sink still bounds it at customer
    assert client.get(f"/v1/test-probe/demo?sink={AGENT}", headers=who["member"]).json() == [
        "customer.md",
        "internal.md",
        "public.md",
    ]
    assert client.get("/v1/test-probe/demo?sink=no-such-sink", headers=who["member"]).json() == []
    # Without a grant: a hub admin manages the project but reads nothing in it; a stranger gets the 404.
    assert client.get(f"/v1/test-probe/demo?sink={AGENT}", headers=who["admin"]).json() == []
    assert client.get(f"/v1/test-probe/demo?sink={AGENT}", headers=who["stranger"]).status_code == 404


@needs_pg
def test_a_push_above_the_hub_sink_is_422_and_writes_nothing(client, github, hub_db):
    who = users(client, github)
    assert client.put("/v1/projects/demo", json=body(), headers=who["admin"]).status_code == 200
    grant(client, who["admin"], "member", "writer", "secret")
    before = content_rows(hub_db)

    above = client.post(
        "/v1/test-probe/demo",
        json={"name": "a.md", "body": "customer data", "label": {"level": "customer"}},
        headers=who["member"],
    )
    assert above.status_code == 422 and above.json()["error"] == "invalid_request"
    assert "not cleared by hub sink 'hub'" in above.json()["message"]
    assert content_rows(hub_db) == before
    assert "customer data" not in live.table_dump(hub_db)

    grant(client, who["admin"], "member", "reader", "secret")
    before = content_rows(hub_db)  # the grant added an audit row
    reader = client.post("/v1/test-probe/demo", json={"name": "a.md", "body": "x"}, headers=who["member"])
    assert reader.status_code == 403 and content_rows(hub_db) == before

    grant(client, who["admin"], "member", "writer", "secret")
    before = content_rows(hub_db)
    ok = client.post("/v1/test-probe/demo", json={"name": "a.md", "body": "x"}, headers=who["member"])
    assert ok.status_code == 201, ok.text
    assert ok.json()["label"] == {"level": "internal", "location": "any", "integrity": "U", "projects": ["demo"]}
    after = content_rows(hub_db)
    assert (after["memories"], after["audit"]) == (before["memories"] + 1, before["audit"] + 1)


@needs_pg
def test_a_project_without_a_hub_sink_refuses_every_push(client, github, hub_db):
    who = users(client, github)
    agent_only = [{"id": AGENT, "kind": "agent-session", "clearance": {"level": "secret"}}]
    assert client.put("/v1/projects/demo", json=body(sinks=agent_only), headers=who["admin"]).status_code == 200
    grant(client, who["admin"], "member", "writer", "secret")
    before = content_rows(hub_db)
    for label in (None, {"level": "public"}):
        refused = client.post(
            "/v1/test-probe/demo", json={"name": "a.md", "body": "x", "label": label}, headers=who["member"]
        )
        assert refused.status_code == 422
        message = refused.json()["message"]
        assert (
            "declares no sink of kind hub" in message
            and "{id: hub, kind: hub, clearance: {level: internal}}" in message
        )
    assert content_rows(hub_db) == before


# The CLI against `hub serve`


def cli(args: list[str], home: Path, github, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """``evo-agents hub ARGS`` as the person whose home is ``home``."""
    env = pg.clean_env(HOME=str(home), EVO_HUB_GITHUB_URL=github.url)
    command = [sys.executable, "-m", "evo_agents", "hub", *args]
    return subprocess.run(command, env=env, capture_output=True, text=True, timeout=60, cwd=cwd)


@needs_pg
def test_the_cli_registers_a_harness_and_pulls_the_registry(hub_db, tmp_path, github):
    from tests.hub.fake_github import Account

    variables = {
        "EVO_HUB_ADMINS": live.ADMIN,
        "EVO_HUB_GITHUB_CLIENT_ID": github.client_id,
        "EVO_HUB_GITHUB_URL": github.url,
        "EVO_HUB_GITHUB_API_URL": github.url,
    }
    home, laptop = tmp_path / "home", tmp_path / "laptop"
    for directory in (home, laptop):
        directory.mkdir()
    root = make_harness(home)
    registry = home / ".claude" / "harness" / "registry.json"
    registry.parent.mkdir(parents=True)
    registry.write_bytes(skill_format({"clusters": FOREIGN}))
    original = registry.read_bytes()

    with live.running_hub(hub_db, tmp_path, **variables) as hub:
        for directory in (home, laptop):
            github.device_account, github.device_steps = Account(live.ADMIN, 7), []
            signed = cli(["login", "--url", hub.url], directory, github)
            assert signed.returncode == 0, signed.stderr + hub.log()

        (root / "docs").mkdir()
        registered = cli(["project", "register"], home, github, cwd=root / "docs")  # from inside the harness
        assert registered.returncode == 0, registered.stderr + hub.log()
        assert "Registered project demo from" in registered.stdout and "4 repo(s), 2 sink(s)" in registered.stdout
        again = cli(["project", "register", str(root)], home, github)
        assert again.returncode == 0 and "nothing changed" in again.stdout

        granted = cli(
            ["admin", "grant", live.ADMIN, "demo", "--role", "admin", "--max-level", "internal"], home, github
        )
        assert granted.returncode == 0, granted.stderr
        listed = cli(["project", "list"], home, github)
        assert listed.returncode == 0 and "demo" in listed.stdout and "internal" in listed.stdout

        pulled = cli(["registry", "pull"], home, github)
        assert pulled.returncode == 0, pulled.stderr
        assert "the previous version is saved as" in pulled.stdout
        (backup,) = registry.parent.glob("registry.json.bak.*")
        assert backup.read_bytes() == original
        clusters = json.loads(registry.read_bytes())["clusters"]
        assert clusters[:3] == FOREIGN  # untouched, in place
        for entry in FOREIGN[:2]:
            assert fragment(entry) in registry.read_bytes()
        ws = home / "ws"
        assert clusters[3]["root"] == str(ws / "demo-harness")
        assert clusters[3]["repos"] == [str(ws / "app"), str(home / "notes"), "/opt/shared-lib", str(ws / "tools")]
        unchanged = cli(["registry", "pull"], home, github)
        assert unchanged.returncode == 0 and "nothing written" in unchanged.stdout
        assert len(list(registry.parent.glob("registry.json.bak.*"))) == 1

        # Another machine, where nothing is cloned yet: the same clusters under its own home.
        elsewhere = cli(["registry", "pull", "--json"], laptop, github)
        assert elsewhere.returncode == 0, elsewhere.stderr
        report = json.loads(elsewhere.stdout)
        assert_json_keys("hub registry pull", report)
        assert report["backup"] is None and report["clusters"][0]["present"] is False
        (cluster,) = json.loads((laptop / ".claude" / "harness" / "registry.json").read_bytes())["clusters"]
        assert cluster["root"] == str(laptop / "ws" / "demo-harness") and cluster["workspace"] == str(laptop / "ws")
    assert "secret-app" not in hub.log() and "example.org" not in hub.log()
