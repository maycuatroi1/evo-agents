"""Plans on the hub and their read-only copies in git.

The checks step 5 of the plan names: the three completed plans of evo-agents-harness (copied into fixtures/plans,
internal names redacted) are imported and exported again with identical digests; when two clients edit one step,
the second gets a 409, retries, and no update is lost; a copy edited by hand fails ``harness validate`` with the
message naming both ways back. Around them: every refusal writes nothing, audit rows and logs carry no plan
content, copies are written atomically, and ``export --commit`` commits the copies and nothing else.

Rendering, the item update and export against an in-memory hub run without Postgres; everything that needs the
hub skips without EVO_HUB_TEST_DSN."""

import copy
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from evo_agents.harness import load_yaml, parse_yaml, plan_digest, validate_harness
from evo_agents.hub import mirror
from evo_agents.hub.client import Hub, HubError
from evo_agents.hub.mirror import MIRROR_HEADER, export, read_plan, render
from evo_agents.hub.plan_cli import complete_plan, import_plans, patch_item, put_file
from evo_agents.hub.plans import (
    MAX_PLAN_BYTES,
    PlanProblem,
    PlanTooLarge,
    check_body,
    step_index,
    summarize,
    undone_steps,
    update_item,
    update_summary,
)
from evo_agents.schema import errors
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys

FIXTURES = Path(__file__).parent / "fixtures" / "plans"
COMPLETED = sorted((FIXTURES / "completed").glob("*.yaml"))
PROJECT = "evo-agents"
LEVELS = ["public", "internal", "customer", "secret"]
REPOS = ["evo-agents", "meridai-harness", "evo-cli", "agent-skills", "evo-lms-harness"]
MARKER = "Zebra-Quokka-Plan-Content"  # in plan bodies only: must never reach an audit row or a log line

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)


def reversed_keys(value):
    if isinstance(value, dict):
        return {key: reversed_keys(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [reversed_keys(item) for item in value]
    return value


def draft(plan_id: str = "rollout", steps: int = 3) -> dict:
    return {
        "id": plan_id,
        "goal": f"Ship the rollout. {MARKER}",
        "repos": [{"repo": "evo-agents", "branch": "main", "status": "pending"}],
        "steps": [
            {"id": n, "title": f"Step {n}", "repo": "evo-agents", "what": f"do part {n}", "status": "pending"}
            for n in range(1, steps + 1)
        ],
        "tech_debt": [{"issue": "a shortcut", "severity": "low", "status": "open"}],
    }


# Copies: written the same way every time, read back as the plan


def test_the_fixtures_are_the_three_completed_plans_of_the_harness():
    assert [p.stem for p in COMPLETED] == ["evo-lms-migration", "kg-assertion-layer", "kg-prototype"]
    for path in COMPLETED:
        found = read_plan(path)
        assert found.area == "completed" and found.plan_id == path.stem and found.hub is None
        assert undone_steps(found.body) == []


@pytest.mark.parametrize("path", COMPLETED, ids=lambda p: p.stem)
def test_a_copy_reads_back_as_the_plan_whatever_its_key_order(path):
    body = read_plan(path).body
    digest = plan_digest(body)
    text = render(body, PROJECT, 3, digest)
    assert text.splitlines()[0] == (
        f"# Mirror of evo-agents hub plan {body['id']}, revision 3. Do not edit; use evo harness step or "
        "evo-agents hub plan."
    )
    data = parse_yaml(text)
    assert data["hub"] == {"project": PROJECT, "revision": 3, "digest": digest}
    assert list(data)[:2] == ["id", "goal"] and list(data)[-1] == "hub"
    assert plan_digest(data) == digest
    assert render(reversed_keys(body), PROJECT, 3, digest) == text  # jsonb keeps no order; the copy needs none


AWKWARD = [
    "",
    " ",
    " leading space",
    "trailing space ",
    "tab\tinside",
    "\tleading tab",
    "two\n\nparagraphs\n",
    "ends with a newline\n",
    "\nstarts with a newline",
    "  indented\n  lines\n",
    "lines with trailing spaces   \nnext",
    "colon: inside",
    "- dash first",
    "# hash first",
    "'single'",
    '"double"',
    "yes",
    "no",
    "on",
    "null",
    "~",
    "1",
    "1.5",
    "0x10",
    "1e3",
    "2026-10-04",
    "2026-10-04T10:00:00+07:00",
    "!tag",
    "&anchor",
    "*alias",
    "Tiếng Việt có dấu đầy đủ, và một câu rất dài " * 8,
    "x" * 500,
    "a\r\nb",
    "next\x85line",
    "line separator",
    "zero width​",
]


def test_awkward_text_numbers_and_keys_survive_a_copy():
    body = {
        "id": "awkward",
        "steps": [{"id": i, "what": text, "note": text} for i, text in enumerate(AWKWARD)],
        "numbers": [0, -1, 2**63, 3.5, 1e-9, 1e300, True, False, None],
        "nested": {"yes": {"1": [[], {}], "": "empty key", "key: with colon": "x"}},
    }
    digest = plan_digest(body)
    data = parse_yaml(render(body, PROJECT, 1, digest))
    assert plan_digest(data) == digest
    assert [step["what"] for step in data["steps"]] == AWKWARD


def test_a_body_that_cannot_be_written_back_is_refused(monkeypatch):
    monkeypatch.setattr(mirror, "_reads_back", lambda text, plan: False)
    with pytest.raises(HubError, match="cannot be written as YAML that reads back the same"):
        render(draft(), PROJECT, 1, plan_digest(draft()))
    with pytest.raises(HubError, match="not a plan id"):
        render({**draft(), "id": "../escape"}, PROJECT, 1, plan_digest(draft()))


# The item update of evo harness step, completion and the checks on a body


def test_update_item_sets_keys_as_evo_harness_step_does():
    body = draft()
    before = copy.deepcopy(body)
    updates = {"status": "done", "done_at": "2026-10-04", "evidence": "evo-agents@abc1234: 12 tests pass"}
    changed, old = update_item(body, "steps", step_index(body, "2"), updates)
    assert body == before  # the input is left alone
    assert changed["steps"][1] == {**before["steps"][1], **updates}
    assert changed["steps"][0] == before["steps"][0] and changed["tech_debt"] == before["tech_debt"]
    assert old == {"status": "pending", "done_at": None, "evidence": None}
    assert update_summary(changed, "steps", 1, old, updates) == "step 2: status pending -> done; set done_at, evidence"
    debt, _ = update_item(body, "tech_debt", 0, {"status": "fixed", "fixed_at": "2026-10-04"})
    assert update_summary(debt, "tech_debt", 0, {"status": "open"}, {"status": "fixed"}) == (
        "tech_debt[0]: status open -> fixed"
    )
    ordered = {"id": "o", "steps": [{"order": 1.5, "what": "a"}, {"what": "b"}]}
    assert (step_index(ordered, 1.5), step_index(ordered, "1")) == (0, 1)  # order, else the position


@pytest.mark.parametrize(
    "section, index, updates, message",
    [
        ("risks", 0, {"status": "done"}, "section must be one of"),
        ("open_questions", 0, {"status": "answered"}, "this plan has no 'open_questions' section"),
        ("steps", 5, {"status": "done"}, "holds 4 items, so index 5 does not exist"),
        ("steps", 3, {"status": "done"}, "is a bare string, not a mapping"),
        ("steps", 0, {"what": "rewritten"}, "'what' cannot be set on one item"),
        ("steps", 0, {"status": "finished"}, "a step's status is one of done, in_progress, pending, blocked"),
        ("steps", 0, {}, "nothing to set"),
        ("steps", 0, {"note": "x" * (64 * 1024 + 1)}, "longer than"),
        ("steps", 0, {"note": "a\x00b"}, "NUL"),
        ("steps", 0, {"status": 1}, "must be text"),
    ],
)
def test_update_item_refuses_what_evo_harness_step_refuses(section, index, updates, message):
    body = draft()
    body["steps"].append("a bare string")
    with pytest.raises(PlanProblem, match=message):
        update_item(body, section, index, updates)


def test_a_plan_is_complete_once_every_step_is_done():
    body = draft()
    assert undone_steps(body) == ["1", "2", "3"]
    for index in range(3):
        body, _ = update_item(body, "steps", index, {"status": "done"})
    assert undone_steps(body) == []
    assert undone_steps({"id": "x", "steps": [{"what": "a", "status": "done"}, "bare"]}) == ["1"]
    assert undone_steps({"id": "x"}) == []


@pytest.mark.parametrize(
    "value, message",
    [
        (float("nan"), "JSON cannot hold"),
        (float("inf"), "JSON cannot hold"),
        ({1, 2}, "a set, which JSON cannot hold"),
        (b"bytes", "a bytes, which JSON cannot hold"),
        ("nul\x00", "NUL"),
        ({"a\x00": 1}, "NUL"),
        ({1: "int key"}, "not a string"),
    ],
)
def test_a_body_must_be_json_all_the_way_down(value, message):
    with pytest.raises(PlanProblem, match=message):
        check_body({"id": "x", "value": value})


def test_a_body_needs_its_id_and_a_bounded_size():
    check_body(draft(), "rollout")
    with pytest.raises(PlanProblem, match="the plan's id is 'rollout', not 'other'"):
        check_body(draft(), "other")
    deep = "leaf"
    for _ in range(40):
        deep = [deep]
    with pytest.raises(PlanProblem, match="nested more than"):
        check_body({"id": "x", "deep": deep})
    with pytest.raises(PlanTooLarge):
        check_body({"id": "x", "context": "x" * MAX_PLAN_BYTES})


def test_a_revision_summary_names_paths_never_values():
    old = draft()
    new = copy.deepcopy(old)
    new["goal"] = "a new goal with a secret-looking value"
    new["steps"][2]["status"] = "done"
    new["steps"].append({"id": 4, "what": "more"})
    del new["tech_debt"]
    summary = summarize(old, new)
    assert summary == "changed goal, steps[2].status, +steps[3], -tech_debt"
    assert "secret" not in summary and summarize(None, new) == "created"


# Export against an in-memory hub


class MemoryHub:
    """The plan reads of a hub, in memory: what export calls."""

    url = "https://hub.test"

    def __init__(self, project: str = "demo"):
        self.project = project
        self.plans: dict[str, dict] = {}
        self.calls: list[str] = []

    def add(self, body: dict, area: str = "active", revision: int = 1) -> None:
        self.plans[body["id"]] = {
            "plan_id": body["id"],
            "area": area,
            "revision": revision,
            "digest": plan_digest(body),
            "body": body,
        }

    def call(self, method, path, body=None):
        assert method == "GET"
        self.calls.append(path)
        base = f"/v1/projects/{self.project}/plans"
        if path == base:
            return [{k: v for k, v in plan.items() if k != "body"} for plan in self.plans.values()]
        return self.plans[path.removeprefix(base + "/")]


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_harness(root: Path, project: str = "demo", plans: list[Path] = ()) -> Path:
    write(
        root / "harness.yaml",
        f"name: {project}\nworkspace: {root.parent}\nknowledge_file: knowledge.yaml\nhub: {{project: {project}}}\n"
        "repos:\n" + "".join(f"  - {{name: {name}, path: {name}}}\n" for name in REPOS),
    )
    write(
        root / "knowledge.yaml",
        f"version: 1\nproject: {project}\npolicy:\n  levels: [{', '.join(LEVELS)}]\n  locations: [any, domestic-only]\n"
        "  sinks:\n    - {id: claude-code@anthropic, kind: agent-session, clearance: {level: internal}}\n"
        "    - {id: hub, kind: hub, clearance: {level: internal}}\n"
        "sources:\n  - {id: harness, connector: harness, label: {level: internal, integrity: U}}\n",
    )
    for path in plans:
        area = root / "plans" / path.parent.name
        area.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, area / path.name)
    return root


def as_yaml(body: dict) -> str:
    import yaml

    return yaml.safe_dump(body, allow_unicode=True, sort_keys=False)


def test_export_writes_copies_moves_completed_plans_and_leaves_other_files(tmp_path):
    root = make_harness(tmp_path / "demo-harness")
    hub = MemoryHub()
    moved, kept, hidden = draft("moved"), draft("kept"), draft("hidden")
    hub.add(moved, "active", 1)
    hub.add(kept, "active", 2)
    first = export(hub, root)
    assert [(p["plan_id"], p["status"]) for p in first.plans] == [("kept", "written"), ("moved", "written")]

    hub.add(moved, "completed", 2)  # completed on the hub since
    write(root / "plans/active/draft.yaml", as_yaml(draft("draft")))  # a draft not pushed yet
    write(root / "plans/completed/kept.yaml", as_yaml(kept))  # not a copy, in the wrong area: left alone
    write(root / "plans/active/hidden.yaml", render(hidden, "demo", 1, plan_digest(hidden)))  # not shown any more
    result = export(hub, root)
    assert not (root / "plans/active/moved.yaml").exists() and result.removed == ["plans/active/moved.yaml"]
    assert read_plan(root / "plans/completed/moved.yaml").hub == {
        "project": "demo",
        "revision": 2,
        "digest": plan_digest(moved),
    }
    assert (root / "plans/completed/kept.yaml").read_text(encoding="utf-8") == as_yaml(kept)
    assert [note.split(":")[0] for note in result.notes] == [
        "plans/completed/kept.yaml",
        "plans/active/draft.yaml",
        "plans/active/hidden.yaml",
    ]
    assert "hub plan put plans/active/draft.yaml" in result.notes[1]


def test_export_fetches_only_plans_whose_copy_is_out_of_date(tmp_path):
    root = make_harness(tmp_path / "demo-harness")
    hub = MemoryHub()
    for name in ("a", "b", "c"):
        hub.add(draft(name))
    export(hub, root)
    before = {path.name: path.read_bytes() for path in (root / "plans/active").iterdir()}
    hub.calls.clear()
    again = export(hub, root)
    assert hub.calls == ["/v1/projects/demo/plans"] and {p["status"] for p in again.plans} == {"unchanged"}
    assert {path.name: path.read_bytes() for path in (root / "plans/active").iterdir()} == before

    edited = root / "plans/active/b.yaml"
    edited.write_text(edited.read_text().replace("do part 1", "do part one"), encoding="utf-8")
    hub.calls.clear()
    restored = export(hub, root)
    assert hub.calls == ["/v1/projects/demo/plans", "/v1/projects/demo/plans/b"]
    assert [p["plan_id"] for p in restored.plans if p["status"] == "written"] == ["b"]
    assert edited.read_bytes() == before["b.yaml"]


def test_a_failed_write_leaves_the_old_copy_whole(tmp_path, monkeypatch):
    root = make_harness(tmp_path / "demo-harness")
    hub = MemoryHub()
    hub.add(draft("a"), revision=1)
    export(hub, root)
    copy_path = root / "plans/active/a.yaml"
    original = copy_path.read_bytes()
    hub.add({**draft("a"), "goal": "changed"}, revision=2)

    def fail(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(HubError, match="the file is as it was"):
        export(hub, root)
    assert copy_path.read_bytes() == original
    assert sorted(p.name for p in copy_path.parent.iterdir()) == ["a.yaml"]  # no temporary file left


def test_export_refuses_a_plan_id_that_is_no_file_name(tmp_path):
    root = make_harness(tmp_path / "demo-harness")
    hub = MemoryHub()
    hub.add(draft("a"))
    hub.plans["a"]["plan_id"] = "../../escape"
    with pytest.raises(HubError, match="has no place in plans/"):
        export(hub, root)
    assert not list(tmp_path.rglob("escape*"))


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A home of its own, so git reads no global configuration (signing, hooks) of the person running the tests."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return path


def git_repo(root: Path) -> None:
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Plan Tester")
    git(root, "config", "user.email", "plans@example.test")
    write(root / "README.md", "harness\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "harness")


def test_export_commit_commits_the_copies_and_nothing_else(home, tmp_path):
    root = make_harness(tmp_path / "demo-harness")
    git_repo(root)
    write(root / "README.md", "harness, staged edit\n")
    git(root, "add", "README.md")
    write(root / "notes.txt", "untracked\n")
    write(root / "plans/active/draft.yaml", as_yaml(draft("draft")))
    write(root / "plans/active/b.yaml", as_yaml(draft("b")))  # b is completed on the hub; this is no copy of it
    hub = MemoryHub()
    hub.add(draft("a"))
    hub.add(draft("b"), "completed", 3)

    result = export(hub, root, commit=True)
    assert result.commit == git(root, "rev-parse", "HEAD").strip()
    assert git(root, "show", "--name-only", "--format=", "HEAD").split() == [
        "plans/active/a.yaml",
        "plans/completed/b.yaml",
    ]
    assert git(root, "log", "-1", "--format=%s").strip() == "Hub plans of demo: copies of a r1, b r3"
    assert git(root, "diff", "--cached", "--name-only").split() == ["README.md"]  # still staged, not committed
    untracked = git(root, "ls-files", "--others", "--exclude-standard").split()
    assert untracked == ["notes.txt", "plans/active/b.yaml", "plans/active/draft.yaml"]
    (root / "plans/active/b.yaml").unlink()

    head = result.commit
    assert export(hub, root, commit=True).commit is None and git(root, "rev-parse", "HEAD").strip() == head

    hub.add(draft("a"), "completed", 2)  # a moved: the commit removes its old copy and adds the new one
    moved = export(hub, root, commit=True)
    assert git(root, "show", "--name-status", "--no-renames", "--format=", moved.commit).split() == [
        "D",
        "plans/active/a.yaml",
        "A",
        "plans/completed/a.yaml",
    ]
    assert git(root, "diff", "--cached", "--name-only").split() == ["README.md"]


def test_export_commit_of_a_harness_inside_a_larger_checkout(home, tmp_path):
    repository = tmp_path / "monorepo"
    repository.mkdir()
    git_repo(repository)
    root = make_harness(repository / "harness")
    hub = MemoryHub()
    hub.add(draft("a"))
    commit = export(hub, root, commit=True).commit
    assert git(repository, "show", "--name-only", "--format=", commit).split() == ["harness/plans/active/a.yaml"]
    untracked = git(repository, "ls-files", "--others", "--exclude-standard").split()
    assert untracked == ["harness/harness.yaml", "harness/knowledge.yaml"]


def test_export_commit_outside_git_writes_the_copies_then_says_so(home, tmp_path):
    root = make_harness(tmp_path / "demo-harness")
    hub = MemoryHub()
    hub.add(draft("a"))
    with pytest.raises(HubError, match="--commit needs a git checkout"):
        export(hub, root, commit=True)
    assert (root / "plans/active/a.yaml").is_file()


# The hub, in process


class AppHub:
    """``evo_agents.hub.client.Hub`` over the in-process app, so the client's operations run unchanged."""

    url = "http://testserver"

    def __init__(self, client, token: str, before=None):
        self.client = client
        self.headers = live.bearer(token)
        self.before = before  # called with (method, path) before each request, to interleave another client

    def call(self, method: str, path: str, body=None):
        if self.before is not None:
            self.before(method, path)
        response = self.client.request(method, path, json=body, headers=self.headers)
        payload = response.json() if response.content else None
        if response.status_code >= 400:
            raise HubError(payload["message"], response.status_code, payload["error"])
        return payload


@pytest.fixture
def client(hub_db, tmp_path, github):
    from fastapi.testclient import TestClient

    from evo_agents.hub.server.app import create_app

    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github))) as client:
        yield client


def registration(sinks=None) -> dict:
    return {
        "levels": LEVELS,
        "locations": ["any", "domestic-only"],
        "default_label": {"level": "internal", "location": "any", "integrity": "U"},
        "sinks": sinks
        or [
            {"id": "claude-code@anthropic", "kind": "agent-session", "clearance": {"level": "internal"}},
            {"id": "hub", "kind": "hub", "clearance": {"level": "internal"}},
        ],
        "repos": [{"name": name, "path": name, "origin": origin_of(name)} for name in REPOS],
        "harness": {"name": PROJECT, "workspace": "~/ws", "path": "evo-agents-harness"},
    }


def origin_of(name: str) -> str:
    """The origin registration() lists for repo ``name``: a run is dispatched only on a repo with one."""
    return f"https://git.example.org/evo/{name}.git"


GRANTS = {"alice": ("writer", "internal"), "bob": ("writer", "internal"), "reader": ("reader", "internal")}
GRANTS |= {"public-reader": ("reader", "public"), "public-writer": ("writer", "public")}


def setup_project(client, github, sinks=None) -> dict:
    """The project registered by a hub admin, and a hub per login: writers alice and bob, readers up to internal
    and up to public, a writer up to public, a stranger, and the admin itself (no grant)."""
    logins = ["admin", *GRANTS, "stranger"]
    tokens = {
        name: live.sign_in(client, github, live.ADMIN if name == "admin" else name, number)["token"]
        for number, name in enumerate(logins, start=1)
    }
    admin = live.bearer(tokens["admin"])
    assert client.put(f"/v1/projects/{PROJECT}", json=registration(sinks), headers=admin).status_code == 200
    for login, (role, level) in GRANTS.items():
        grant = {"role": role, "max_level": level}
        response = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=admin)
        assert response.status_code == 200, response.text
    return {name: AppHub(client, token) for name, token in tokens.items()}


def plan_url(plan_id: str | None = None, *rest: str) -> str:
    return "/".join([f"/v1/projects/{PROJECT}/plans", *([plan_id] if plan_id else []), *rest])


def counts(db) -> dict:
    from sqlalchemy import func, select

    from evo_agents.hub import tables

    return {
        table.name: live.sql(db, select(func.count()).select_from(table))[0][0]
        for table in (tables.plans, tables.plan_revisions, tables.audit)
    }


def put(hub: AppHub, body: dict, **fields):
    return hub.client.put(plan_url(body["id"]), json={"body": body, **fields}, headers=hub.headers)


def checkout(tmp_path: Path) -> Path:
    """A harness of project evo-agents holding copies of the three completed plans, as a person has it."""
    return make_harness(tmp_path / "ws" / "evo-agents-harness", PROJECT, COMPLETED)


@needs_pg
def test_import_of_the_three_completed_plans_then_export_gives_identical_digests(client, github, hub_db, tmp_path):
    hubs = setup_project(client, github)
    root = checkout(tmp_path)
    sources = {path.stem: plan_digest(load_yaml(path)) for path in COMPLETED}

    imported = import_plans(hubs["alice"], root)
    assert [(p["plan_id"], p["status"], p["revision"]) for p in imported.plans] == [
        (name, "created", 1) for name in sorted(sources)
    ]
    again = import_plans(hubs["alice"], root)  # idempotent by digest
    assert {p["status"] for p in again.plans} == {"unchanged"} and counts(hub_db)["plan_revisions"] == 3

    listed = hubs["reader"].call("GET", plan_url())
    assert {p["plan_id"]: p["digest"] for p in listed} == sources
    assert {(p["area"], p["revision"], p["steps_done"] == p["steps_total"]) for p in listed} == {("completed", 1, True)}

    exported = export(hubs["reader"], root)
    assert {p["status"] for p in exported.plans} == {"written"}
    for name, digest in sources.items():
        data = load_yaml(root / "plans" / "completed" / f"{name}.yaml")
        assert plan_digest(data) == digest == data["hub"]["digest"]
        assert data["hub"] == {"project": PROJECT, "revision": 1, "digest": digest}
        assert hubs["reader"].call("GET", plan_url(name))["digest"] == digest
    reports = validate_harness(root)
    assert [str(i) for r in reports for i in errors(r.issues)] == []

    before = {path: path.read_bytes() for path in (root / "plans/completed").iterdir()}
    assert {p["status"] for p in export(hubs["reader"], root).plans} == {"unchanged"}
    assert {path: path.read_bytes() for path in (root / "plans/completed").iterdir()} == before


@needs_pg
def test_two_clients_editing_one_step_the_second_gets_409_retries_and_nothing_is_lost(client, github, hub_db):
    hubs = setup_project(client, github)
    alice, bob = hubs["alice"], hubs["bob"]
    assert put(alice, draft()).status_code == 200
    seen_by_alice = alice.call("GET", plan_url("rollout"))["revision"]
    seen_by_bob = bob.call("GET", plan_url("rollout"))["revision"]
    assert seen_by_alice == seen_by_bob == 1

    done = {"status": "done", "done_at": "2026-10-04"}
    first = client.patch(
        plan_url("rollout"),
        json={"section": "steps", "step": 2, "updates": done, "if_revision": seen_by_alice},
        headers=alice.headers,
    )
    assert first.status_code == 200 and first.json()["revision"] == 2

    evidence = {"evidence": "evo-agents@abc1234: 40 tests pass"}
    second = client.patch(
        plan_url("rollout"),
        json={"section": "steps", "step": 2, "updates": evidence, "if_revision": seen_by_bob},
        headers=bob.headers,
    )
    assert second.status_code == 409, second.text
    conflict = second.json()
    assert conflict["error"] == "revision_conflict" and "request_id" in conflict
    assert "at revision 2 on the hub, not 1" in conflict["message"]
    assert conflict["current"]["revision"] == 2 and conflict["current"]["body"]["steps"][1]["status"] == "done"
    assert counts(hub_db)["plan_revisions"] == 2  # the refused write left nothing

    retried = client.patch(
        plan_url("rollout"),
        json={"section": "steps", "step": 2, "updates": evidence, "if_revision": conflict["current"]["revision"]},
        headers=bob.headers,
    )
    assert retried.status_code == 200 and retried.json()["revision"] == 3
    step = alice.call("GET", plan_url("rollout"))["body"]["steps"][1]
    assert step == {**draft()["steps"][1], **done, **evidence}  # both updates are there

    history = alice.call("GET", plan_url("rollout", "revisions"))
    assert [(r["revision"], r["actor"], r["summary"]) for r in history] == [
        (3, "bob", "step 2: set evidence"),
        (2, "alice", "step 2: status pending -> done; set done_at"),
        (1, "alice", "created"),
    ]


@needs_pg
def test_the_client_retries_a_conflicting_patch_unless_it_would_undo_the_other_write(client, github, hub_db):
    hubs = setup_project(client, github)
    alice = hubs["alice"]
    assert put(alice, draft()).status_code == 200

    def alice_writes_first(updates):
        state = {"done": False}

        def before(method, path):
            if method == "PATCH" and not state["done"]:
                state["done"] = True
                patch_item(alice, PROJECT, "rollout", "steps", updates, step=3)

        return before

    bob = hubs["bob"]
    bob.before = alice_writes_first({"status": "in_progress"})
    result = patch_item(bob, PROJECT, "rollout", "steps", {"note": "bob's note"}, step=3)
    assert result["revision"] == 3
    step = alice.call("GET", plan_url("rollout"))["body"]["steps"][2]
    assert (step["status"], step["note"]) == ("in_progress", "bob's note")

    bob.before = alice_writes_first({"note": "alice's note"})
    with pytest.raises(HubError, match="note of step 3 in plan rollout changed on the hub") as clash:
        patch_item(bob, PROJECT, "rollout", "steps", {"note": "bob's second note"}, step=3)
    assert clash.value.status == 409
    held = alice.call("GET", plan_url("rollout"))
    assert held["revision"] == 4 and held["body"]["steps"][2]["note"] == "alice's note"  # not overwritten

    bob.before = None
    with pytest.raises(HubError) as pinned:  # with --if-revision the client never retries
        patch_item(bob, PROJECT, "rollout", "steps", {"note": "x"}, step=3, if_revision=1)
    assert pinned.value.code == "revision_conflict"


@needs_pg
@pytest.mark.parametrize(
    "body, status, message",
    [
        ({**draft(), "steps": "not a list"}, 422, "steps: expected array"),
        ({**draft(), "steps": [{"id": 1}]}, 422, "steps[0].what: is required"),
        ({**draft(), "id": "other"}, 422, "the plan's id is 'other', not 'rollout'"),
        ({**draft(), "big": 1e300}, 422, "does not survive storage unchanged"),
        ({**draft(), "context": "x" * MAX_PLAN_BYTES}, 413, "more than the"),
        ({**draft(), "nul": "a\x00b"}, 422, "NUL"),
    ],
    ids=["steps-not-a-list", "step-without-what", "id-mismatch", "number-jsonb-changes", "too-large", "nul"],
)
def test_a_plan_the_hub_cannot_keep_is_refused_with_json_and_nothing_is_written(
    client, github, hub_db, body, status, message
):
    hubs = setup_project(client, github)
    before = counts(hub_db)
    response = client.put(plan_url("rollout"), json={"body": body}, headers=hubs["alice"].headers)
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert message in response.json()["message"] and response.json()["request_id"]
    assert counts(hub_db) == before


@needs_pg
def test_json_the_hub_cannot_parse_or_keep_is_refused(client, github, hub_db):
    hubs = setup_project(client, github)
    before = counts(hub_db)
    headers = {**hubs["alice"].headers, "Content-Type": "application/json"}
    for raw in (b'{"body": {"id": "rollout", "n": NaN}}', b'{"body": {"id": "rollout", "n": 1e999}}', b"{nope"):
        response = client.put(plan_url("rollout"), content=raw, headers=headers)
        assert response.status_code == 422 and response.json()["error"] == "invalid_request", raw
    assert client.put(plan_url("Bad_Id"), json={"body": draft()}, headers=headers).status_code == 422
    assert counts(hub_db) == before


@needs_pg
def test_writes_need_the_writer_role_and_a_label_the_hub_sink_clears(client, github, hub_db):
    hubs = setup_project(client, github)
    before = counts(hub_db)
    assert put(hubs["reader"], draft()).status_code == 403
    assert put(hubs["admin"], draft()).status_code == 403  # a hub admin without a grant manages, never writes
    assert put(hubs["stranger"], draft()).status_code == 404
    above = put(hubs["alice"], draft(), label={"level": "customer"})
    assert above.status_code == 422 and "not cleared by hub sink 'hub'" in above.json()["message"]
    assert counts(hub_db) == before

    assert put(hubs["alice"], draft()).status_code == 200
    plan = hubs["alice"].call("GET", plan_url("rollout"))
    assert plan["label"] == {"level": "internal", "location": "any", "integrity": "U", "projects": [PROJECT]}
    patch = {"section": "steps", "step": 1, "updates": {"status": "done"}, "if_revision": 1}
    assert client.patch(plan_url("rollout"), json=patch, headers=hubs["reader"].headers).status_code == 403
    # A writer whose grant stops at public cannot see an internal plan, so cannot change it either.
    assert client.patch(plan_url("rollout"), json=patch, headers=hubs["public-writer"].headers).status_code == 404


@needs_pg
def test_a_project_without_a_hub_sink_takes_no_plan(client, github, hub_db):
    agent_only = [{"id": "claude-code@anthropic", "kind": "agent-session", "clearance": {"level": "secret"}}]
    hubs = setup_project(client, github, sinks=agent_only)
    before = counts(hub_db)
    refused = put(hubs["alice"], draft())
    assert refused.status_code == 422 and "declares no sink of kind hub" in refused.json()["message"]
    assert counts(hub_db) == before


@needs_pg
def test_reads_follow_the_label_rule_and_hide_what_they_refuse(client, github, hub_db):
    hubs = setup_project(client, github)
    assert put(hubs["alice"], draft("internal-plan")).status_code == 200
    assert put(hubs["alice"], draft("public-plan"), label={"level": "public"}).status_code == 200

    assert [p["plan_id"] for p in hubs["reader"].call("GET", plan_url())] == ["internal-plan", "public-plan"]
    assert [p["plan_id"] for p in hubs["public-reader"].call("GET", plan_url())] == ["public-plan"]
    hidden = client.get(plan_url("internal-plan"), headers=hubs["public-reader"].headers)
    missing = client.get(plan_url("no-such-plan"), headers=hubs["public-reader"].headers)
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json()["message"].replace("internal-plan", "X") == missing.json()["message"].replace(
        "no-such-plan", "X"
    )
    assert client.get(plan_url("internal-plan", "revisions"), headers=hubs["public-reader"].headers).status_code == 404
    assert client.get(plan_url(), headers=hubs["admin"].headers).status_code == 403  # no grant: nothing to read
    assert client.get(plan_url(), headers=hubs["stranger"].headers).status_code == 404
    sink = {**hubs["reader"].headers, "X-Evo-Sink": "no-such-sink"}
    assert client.get(plan_url(), headers=sink).json() == []  # an undeclared sink lets nothing through

    first = hubs["reader"].call("GET", plan_url("public-plan", "revisions", "1"))
    assert first["body"] == draft("public-plan") and first["label"]["level"] == "public"
    assert client.get(plan_url("public-plan", "revisions", "9"), headers=hubs["reader"].headers).status_code == 404


@needs_pg
def test_the_list_orders_by_plan_id_and_narrows_to_an_area(client, github, hub_db):
    hubs = setup_project(client, github)
    for plan_id, area in (("zeta", "active"), ("beta", "completed"), ("alpha", "active")):
        assert put(hubs["alice"], draft(plan_id), area=area).status_code == 200

    def listed(**params) -> list[tuple]:
        response = client.get(plan_url(), params=params, headers=hubs["reader"].headers)
        assert response.status_code == 200, response.text
        return [(p["plan_id"], p["area"]) for p in response.json()]

    assert listed() == [("alpha", "active"), ("beta", "completed"), ("zeta", "active")]
    assert listed(area="active") == [("alpha", "active"), ("zeta", "active")]
    assert listed(area="completed") == [("beta", "completed")]


@needs_pg
def test_put_replaces_only_the_revision_it_names(client, github, hub_db):
    hubs = setup_project(client, github)
    alice = hubs["alice"]
    created = put(alice, draft()).json()
    assert (created["created"], created["changed"], created["revision"]) == (True, True, 1)

    same = put(alice, reversed_keys(draft()))  # the same plan again, keys in another order: nothing to do
    assert same.status_code == 200 and (same.json()["changed"], same.json()["revision"]) == (False, 1)
    before = counts(hub_db)

    changed = {**draft(), "goal": "A new goal."}
    unnamed = put(alice, changed)
    assert unnamed.status_code == 409 and unnamed.json()["error"] == "revision_conflict"
    assert "pass if_revision 1 to replace it" in unnamed.json()["message"]
    assert unnamed.json()["current"]["body"] == draft()
    assert put(alice, changed, if_revision=7).status_code == 409
    assert put(alice, draft("not-there"), if_revision=1).status_code == 409
    moved = put(alice, changed, if_revision=1, area="completed")
    assert moved.status_code == 422 and "`evo-agents hub plan complete` does" in moved.json()["message"]
    assert counts(hub_db) == before

    replaced = put(alice, {**changed, "hub": {"project": PROJECT, "revision": 1, "digest": "sha256:" + "0" * 64}})
    assert replaced.status_code == 409  # the hub key of a copy is not an if_revision
    replaced = put(alice, changed, if_revision=1)
    assert replaced.status_code == 200 and replaced.json()["revision"] == 2
    assert alice.call("GET", plan_url("rollout", "revisions"))[0]["summary"] == "changed goal"
    assert "hub" not in alice.call("GET", plan_url("rollout"))["body"]


@needs_pg
@pytest.mark.parametrize(
    "patch, message",
    [
        ({"section": "steps", "step": 9, "updates": {"status": "done"}}, "no step '9' in plan rollout"),
        ({"section": "steps", "index": 7, "updates": {"status": "done"}}, "index 7 does not exist"),
        ({"section": "steps", "step": 1, "updates": {"what": "x"}}, "'what' cannot be set"),
        ({"section": "steps", "step": 1, "updates": {"status": "finished"}}, "a step's status is one of"),
        ({"section": "steps", "index": 0, "step": 1, "updates": {"status": "done"}}, "exactly one of index and step"),
        ({"section": "tech_debt", "step": 1, "updates": {"status": "fixed"}}, "steps section only"),
        ({"section": "open_questions", "index": 0, "updates": {"status": "answered"}}, "no 'open_questions'"),
        ({"section": "risks", "index": 0, "updates": {"status": "x"}}, "the request does not match the API"),
    ],
)
def test_a_patch_the_plan_cannot_take_is_422_and_writes_nothing(client, github, hub_db, patch, message):
    hubs = setup_project(client, github)
    assert put(hubs["alice"], draft()).status_code == 200
    before = counts(hub_db)
    response = client.patch(plan_url("rollout"), json={**patch, "if_revision": 1}, headers=hubs["alice"].headers)
    assert response.status_code == 422 and message in response.json()["message"], response.text
    assert counts(hub_db) == before
    no_revision = client.patch(plan_url("rollout"), json=patch, headers=hubs["alice"].headers)
    assert no_revision.status_code == 422  # every patch names the revision it was made against


@needs_pg
def test_complete_needs_every_step_done_and_moves_the_plan_once(client, github, hub_db):
    hubs = setup_project(client, github)
    alice = hubs["alice"]
    assert put(alice, draft(steps=2)).status_code == 200
    patch_item(alice, PROJECT, "rollout", "steps", {"status": "done"}, step=1)
    before = counts(hub_db)
    with pytest.raises(HubError, match="steps that are not done: 2") as refused:
        complete_plan(alice, PROJECT, "rollout")
    assert (refused.value.status, refused.value.code) == (409, "conflict") and counts(hub_db) == before

    patch_item(alice, PROJECT, "rollout", "steps", {"status": "done"}, step=2)
    stale = client.post(plan_url("rollout", "complete"), json={"if_revision": 1}, headers=alice.headers)
    assert stale.status_code == 409 and stale.json()["error"] == "revision_conflict"
    done = complete_plan(alice, PROJECT, "rollout")
    assert (done["area"], done["revision"], done["changed"]) == ("completed", 4, True)
    again = client.post(plan_url("rollout", "complete"), headers=alice.headers)
    assert again.status_code == 200 and again.json()["changed"] is False and again.json()["revision"] == 4
    assert alice.call("GET", plan_url("rollout", "revisions"))[0]["summary"] == "completed: moved from active"
    # A completed plan still takes item updates, as a debt fixed after the plan closed.
    fixed = patch_item(alice, PROJECT, "rollout", "tech_debt", {"status": "fixed", "fixed_at": "2026-10-05"}, index=0)
    assert (fixed["area"], fixed["revision"]) == ("completed", 5)


@needs_pg
def test_audit_rows_and_logs_name_plans_never_their_content(client, github, hub_db, caplog):
    caplog.set_level(logging.INFO, logger="evo_agents.hub")
    hubs = setup_project(client, github)
    alice = hubs["alice"]
    assert put(alice, draft()).status_code == 200
    patch_item(alice, PROJECT, "rollout", "steps", {"status": "done", "evidence": MARKER}, step=1)
    assert put(alice, {**draft(), "goal": MARKER * 2}, if_revision=2).status_code == 200
    from sqlalchemy import func, select

    from evo_agents.hub import tables

    audit = tables.audit
    trail = select(audit.c.action, audit.c.target).where(func.left(audit.c.action, 5) == "plan.").order_by(audit.c.id)
    rows = live.sql(hub_db, trail)
    assert rows == [
        ("plan.create", f"{PROJECT}/rollout@1"),
        ("plan.patch", f"{PROJECT}/rollout@2"),
        ("plan.put", f"{PROJECT}/rollout@3"),
    ]
    outcomes = [r.outcome for r in caplog.records if r.getMessage() == "plan write"]
    assert outcomes == ["created", "patched", "replaced"]
    logged = caplog.text + json.dumps([vars(r) for r in caplog.records], default=str)
    assert MARKER not in json.dumps(live.sql(hub_db, select(audit)), default=str) and MARKER not in logged


@needs_pg
def test_put_file_creates_or_replaces_then_rewrites_the_file_as_the_copy(client, github, hub_db, tmp_path):
    hubs = setup_project(client, github)
    root = make_harness(tmp_path / "ws" / "evo-agents-harness", PROJECT)
    path = write(root / "plans/active/rollout.yaml", as_yaml(draft()))
    created = put_file(hubs["alice"], path)
    assert created["created"] and created["project"] == PROJECT
    assert path.read_text(encoding="utf-8").startswith(MIRROR_HEADER.format(plan_id="rollout", revision=1))
    assert [str(i) for r in validate_harness(root) for i in errors(r.issues)] == []

    path.write_text(path.read_text().replace("do part 2", "do part two"), encoding="utf-8")
    with pytest.raises(HubError) as stale:
        put_file(hubs["alice"], path)  # the file is a copy of revision 1, but put never guesses the revision
    assert stale.value.code == "revision_conflict" and "do part two" in path.read_text()
    replaced = put_file(hubs["alice"], path, if_revision=1)
    assert replaced["revision"] == 2 and read_plan(path).hub["revision"] == 2
    assert [str(i) for r in validate_harness(root) for i in errors(r.issues)] == []


# The CLI against `hub serve`, and real concurrency


def signed_in(home: Path, url: str, login: str, token: str) -> Path:
    """A home whose ~/.evo/hub holds a hub token, as `hub login` leaves it."""
    directory = home / ".evo" / "hub"
    directory.mkdir(parents=True, mode=0o700)
    (directory / "token").write_text(token + "\n", encoding="utf-8")
    (directory / "config.json").write_text(json.dumps({"url": url, "login": login}), encoding="utf-8")
    for name in ("token", "config.json"):
        os.chmod(directory / name, 0o600)
    return home


def evo(args: list[str], home: Path, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = pg.clean_env(HOME=str(home), GIT_CONFIG_NOSYSTEM="1")
    command = [sys.executable, "-m", "evo_agents", *args]
    return subprocess.run(command, env=env, capture_output=True, text=True, timeout=120, cwd=cwd)


def ok(result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@needs_pg
def test_the_cli_imports_exports_commits_and_restores_a_hand_edited_copy(hub_db, tmp_path, home):
    root = make_harness(home / "ws" / "evo-agents-harness", PROJECT, COMPLETED)
    git_repo(root)  # the plans as people wrote them, committed
    with live.running_hub(hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN) as hub:  # migrated once it answers
        signed_in(home, hub.url, live.ADMIN, live.insert_token(hub_db, live.ADMIN))
        ok(evo(["hub", "project", "register", str(root)], home))
        ok(evo(["hub", "admin", "grant", live.ADMIN, PROJECT, "--role", "writer", "--max-level", "internal"], home))
        imported = ok(evo(["hub", "plan", "import", str(root)], home))
        assert "3 file(s) for project evo-agents: 3 created, 0 already on the hub, 0 not pushed" in imported.stdout

        write(root / "README.md", "harness, a staged edit of someone else\n")
        git(root, "add", "README.md")
        exported = ok(evo(["hub", "plan", "export", str(root), "--commit"], home))
        assert "3 written, 0 unchanged, 0 removed" in exported.stdout and "Committed the copies as" in exported.stdout
        committed = git(root, "show", "--name-only", "--format=", "HEAD").split()
        assert committed == [f"plans/completed/{path.name}" for path in COMPLETED]
        assert git(root, "diff", "--cached", "--name-only").split() == ["README.md"]
        assert ok(evo(["harness", "validate", str(root)], home)).stdout.endswith("0 with errors\n")

        # Edited by hand: validate names both ways back; export restores the hub's copy byte for byte.
        target = root / "plans/completed/kg-prototype.yaml"
        target.write_text(target.read_text().replace("Khung package, CLI và CI", "Khung package"), encoding="utf-8")
        failed = evo(["harness", "validate", str(root)], home)
        assert failed.returncode == 1
        assert (
            "plans/completed/kg-prototype.yaml was edited outside the hub (digest mismatch). Push it with "
            "`evo-agents hub plan put plans/completed/kg-prototype.yaml --if-revision 1` or restore it with "
            "`evo-agents hub plan export .`"
        ) in failed.stdout
        restored = ok(evo(["hub", "plan", "export", "."], home, cwd=root))
        assert "1 written, 2 unchanged" in restored.stdout
        assert git(root, "status", "--porcelain", "--", "plans").strip() == ""
        ok(evo(["harness", "validate", str(root)], home))

        # A new plan: put from a draft, steps marked through the hub, completed, its copy moved and committed.
        draft_path = write(root / "plans/active/rollout.yaml", as_yaml(draft(steps=2)))
        ok(evo(["hub", "plan", "put", "plans/active/rollout.yaml"], home, cwd=root))
        assert read_plan(draft_path).hub["revision"] == 1
        ok(evo(["hub", "plan", "step", "rollout", "1", "done", "--evidence", "evo-agents@abc1234: tests"], home, root))
        refused = evo(["hub", "plan", "complete", "rollout"], home, cwd=root)
        assert refused.returncode == 1 and "steps that are not done: 2" in refused.stderr
        ok(evo(["hub", "plan", "step", "rollout", "2", "done", "--note", "last one"], home, cwd=root))
        ok(evo(["hub", "plan", "complete", "rollout"], home, cwd=root))
        moved = ok(evo(["hub", "plan", "export", ".", "--commit"], home, cwd=root))
        assert "removed  plans/active/rollout.yaml" in moved.stdout
        assert git(root, "show", "--name-only", "--format=", "HEAD").split() == ["plans/completed/rollout.yaml"]
        assert not draft_path.exists() and git(root, "diff", "--cached", "--name-only").split() == ["README.md"]

        history = json.loads(ok(evo(["hub", "plan", "history", "rollout", "--json"], home, cwd=root)).stdout)
        assert_json_keys("hub plan history", history)
        assert [r["summary"] for r in history] == [
            "completed: moved from active",
            "step 2: status pending -> done; set done_at, note",
            "step 1: status pending -> done; set done_at, evidence",
            "created",
        ]
        shown = ok(evo(["hub", "plan", "show", "rollout"], home, cwd=root)).stdout
        assert shown == (root / "plans/completed/rollout.yaml").read_text(encoding="utf-8")
        listed = json.loads(ok(evo(["hub", "plan", "list", "--json"], home, cwd=root)).stdout)
        assert_json_keys("hub plan list", listed)
        assert [(p["plan_id"], p["area"]) for p in listed] == [
            ("evo-lms-migration", "completed"),
            ("kg-assertion-layer", "completed"),
            ("kg-prototype", "completed"),
            ("rollout", "completed"),
        ]
    assert MARKER not in hub.log()


@needs_pg
def test_concurrent_writers_over_http_lose_no_update(hub_db, tmp_path):
    writers = [f"writer-{n}" for n in range(8)]
    with live.running_hub(hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN) as server:
        tokens = {login: live.insert_token(hub_db, login) for login in [live.ADMIN, *writers]}
        admin = Hub(server.url, tokens[live.ADMIN])
        admin.call("PUT", f"/v1/projects/{PROJECT}", registration())
        for login in writers:
            admin.call(
                "PUT", f"/v1/admin/projects/{PROJECT}/grants/{login}", {"role": "writer", "max_level": "internal"}
            )
        hubs = {login: Hub(server.url, tokens[login]) for login in writers}
        hubs[writers[0]].call("PUT", plan_url("rollout"), {"body": draft(steps=6)})

        # Six writers each set the note of their own step; two more both change step 6, one its status, one its
        # evidence. Every write starts from the same revision, so most of them meet a conflict and retry.
        jobs = [(login, n + 1, {"note": f"note of {login}"}) for n, login in enumerate(writers[:6])]
        jobs += [(writers[6], 6, {"status": "done"}), (writers[7], 6, {"evidence": "evo-agents@abc1234"})]
        barrier = threading.Barrier(len(jobs))
        failures = []

        def run(login, step, updates):
            barrier.wait()
            try:
                patch_item(hubs[login], PROJECT, "rollout", "steps", updates, step=step, attempts=20)
            except Exception as exc:  # noqa: BLE001 - reported below with the login
                failures.append((login, exc))

        threads = [threading.Thread(target=run, args=job) for job in jobs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        assert failures == []
        plan = hubs[writers[0]].call("GET", plan_url("rollout"))
        steps = plan["body"]["steps"]
        assert [step.get("note") for step in steps] == [f"note of {login}" for login in writers[:6]]
        assert (steps[5]["status"], steps[5]["evidence"]) == ("done", "evo-agents@abc1234")
        assert plan["revision"] == 1 + len(jobs)
        revisions = hubs[writers[0]].call("GET", plan_url("rollout", "revisions"))
        assert [r["revision"] for r in revisions] == list(range(1 + len(jobs), 0, -1))
    log = server.log()
    assert '"status": 409' in log  # writers did meet conflicts and retried
    assert '"status": 500' not in log and '"level": "error"' not in log
