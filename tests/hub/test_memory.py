"""Memories on the hub: the slug rule against the shapes of the directories Claude Code really made, the places
directories map to, the read rule of memory types (another member never sees one's user and feedback memories),
revisions with if_revision and tombstones, the write rule and the bounds refusing without writing, and the sync: two
homes converge, a conflict keeps both versions, deletions cross only with --prune, a second sync changes nothing.

No test touches the real ~/.claude or ~/.evo: every one runs with a home directory of its own. The one exception is
opt-in: with EVO_HUB_REAL_CLAUDE_PROJECTS=1, one test lists the directory names of this machine's ~/.claude/projects
(names only, never a file) and checks the slug rule on them. The rules and the slug run without Postgres; everything
that needs the hub skips without EVO_HUB_TEST_DSN."""

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from evo_agents.hub import client as hub_client
from evo_agents.hub import memory as hub_memory
from evo_agents.hub.client import Hub, HubError
from evo_agents.hub.hooks import _memory_place
from evo_agents.hub.memory import (
    HARNESS,
    INDEX,
    MAX_BODY,
    SLUG,
    MemorySync,
    Place,
    Places,
    add_pointers,
    body_problem,
    host_tag,
    memory_type,
    name_problem,
    personal_location,
    personal_slug,
    pointer_line,
    slug,
)
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys

# The directories under ~/.claude/projects of one macOS machine, one per line with the absolute path each was made
# for when it still existed (empty when not), pseudonymized: the home directory is /Users/someone, every other word
# (a run of [A-Za-z0-9]) is a token standing for it, the same token wherever the word occurs, and every other
# character, the depth of each path included, is as it was. Each name is the slug of its pseudonymized path.
FIXTURE = Path(__file__).parent / "fixtures" / "claude-projects.tsv"
SOMEONE = Path("/Users/someone")
WORKSPACE = "w45"  # the directory most of the list sits in, under the home directory
AGENT = "claude-code@anthropic"
LEVELS = ["public", "internal", "customer", "secret"]
REAL_CLAUDE_PROJECTS = os.environ.get("EVO_HUB_REAL_CLAUDE_PROJECTS") == "1"
REAL_CLAUDE_CONFIG = os.environ.get("CLAUDE_CONFIG_DIR")  # read before any test changes the environment


@pytest.fixture(autouse=True)
def own_home(tmp_path, monkeypatch) -> Path:
    """A home directory of this test's own, and no CLAUDE_CONFIG_DIR or EVO_KG_HOME: nothing can reach the real
    ~/.claude or ~/.evo."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("EVO_KG_HOME", raising=False)  # bound.json of `kg bind` is read under this home too
    return home


def fixture_rows() -> list[tuple[str, str]]:
    rows = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            name, _, path = line.partition("\t")
            rows.append((name, path))
    return rows


# The slug rule


def test_the_slug_rule_gives_the_names_of_the_shapes_claude_code_made():
    rows = fixture_rows()
    names = [name for name, _ in rows]
    assert len(names) == len(set(names)) == 91
    pairs = [(name, path) for name, path in rows if path]
    assert len(pairs) == 51
    for name, path in pairs:
        assert slug(path) == name, path
    for name in names:  # every name is a slug: the rule leaves it as it is
        assert SLUG.fullmatch(name) and slug(name) == name and name.startswith("-")
    paths = [path for _, path in pairs]
    # the shapes that make the rule worth checking: a space, a dot, an underscore, upper case, a slug inside a path
    assert any(" " in p for p in paths) and any("/." in p for p in paths) and any("_" in p for p in paths)
    assert any("." in p.rpartition("/")[2] for p in paths) and any(re.search(r"/[^/]*[A-Z]", p) for p in paths)
    assert any("/-Users-someone-" in p for p in paths)
    assert slug("/Users/someone/ghi chú/x.y") == "-Users-someone-ghi-ch--x-y"
    assert slug("/tmp/\N{GRINNING FACE}") == "-tmp---"  # two UTF-16 units, as JavaScript counts them


def _runs(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+|[^A-Za-z0-9]+", text)


def _spells(path: str, name: str, *, whole: bool) -> bool:
    """Whether ``path`` (or, unless ``whole``, the start of a path beginning with it) spells ``name`` with its words
    exact and every run of other characters standing for a run of '-' of any length. That is the slug rule without
    its one '-' per character, which the test then checks on what this finds."""
    have, want = _runs(path), _runs(name)
    if len(have) > len(want) or (whole and len(have) != len(want)):
        return False
    for run, wanted in zip(have, want[: len(have)], strict=True):
        is_word = run[0].isascii() and run[0].isalnum()
        if (run != wanted) if is_word else (set(wanted) != {"-"}):
            return False
    return True


def _paths_spelling(name: str, skip: Path, depth: int = 40) -> list[str]:
    """The existing directories whose path spells ``name``, found by listing directory names from / down, never
    inside ``skip``."""
    found = []

    def walk(path: str, level: int) -> None:
        try:
            entries = os.listdir(path or "/")
        except OSError:
            return
        for entry in entries:
            child = f"{path}/{entry}"
            if Path(child) == skip or not _spells(child, name, whole=False) or not os.path.isdir(child):
                continue
            if _spells(child, name, whole=True):
                found.append(child)
            if level < depth:
                walk(child, level + 1)

    walk("", 0)
    return found


@pytest.mark.skipif(not REAL_CLAUDE_PROJECTS, reason="EVO_HUB_REAL_CLAUDE_PROJECTS=1 checks this machine's directories")
def test_the_slug_rule_on_the_directories_of_this_machine(capsys):
    import pwd

    config = Path(REAL_CLAUDE_CONFIG).expanduser() if REAL_CLAUDE_CONFIG else Path(pwd.getpwuid(os.getuid()).pw_dir)
    base = (config if REAL_CLAUDE_CONFIG else config / ".claude") / "projects"
    names = sorted(entry.name for entry in os.scandir(base) if entry.is_dir(follow_symlinks=False))  # names only
    resolvable = mismatches = 0
    for name in names:
        paths = _paths_spelling(name, base)
        if paths:
            resolvable += 1
            mismatches += not any(slug(path) == name for path in paths)
    with capsys.disabled():  # counts only: the names stay on this machine
        print(f"\nslug rule on this machine: checked {len(names)}, resolvable {resolvable}, mismatches {mismatches}")
    assert names and resolvable, "no directory of ~/.claude/projects resolves to a path here"
    assert mismatches == 0, f"{mismatches} of {resolvable} directories that resolve break the slug rule"


def hub_projects(workspace: str = f"~/{WORKSPACE}") -> list[dict]:
    """Three projects as GET /v1/projects lists them, on directories of the fixture: alpha has a repo the list lacks,
    beta's harness has directories inside it and a sibling whose name starts with its own, and gamma's harness is
    also one of its repos."""

    def project(name, harness, repos):
        return {
            "name": name,
            "harness": {"name": name, "workspace": workspace, "path": harness},
            "repos": [{"name": repo, "path": repo} for repo in repos],
        }

    return [
        project("alpha", "w82-w87-w29", ["w82-w87", "w82-w132"]),
        project("beta", "w41-w29", ["w78-w17-w87", "w78-w25-w119-w75", "w78-w92-w68"]),
        project("gamma", "w82-w33-w29", ["w82-w33-w29", "w82-w38-w75"]),
        {"name": "unplaced", "harness": None, "repos": []},  # registered without paths: owns no directory
    ]


def test_fixture_directories_map_to_their_project_or_to_a_personal_place(monkeypatch):
    monkeypatch.setenv("HOME", str(SOMEONE))
    places = Places.from_hub(hub_projects(), {"clusters": []}, "https://hub.test", SOMEONE)
    mapped = {name: places.candidates(name)[0] for name, _ in fixture_rows()}
    projects = {name: (p.project, p.location) for name, p in mapped.items() if p.scope == "project"}
    ws = f"-Users-someone-{WORKSPACE}"
    assert projects == {
        f"{ws}-w82-w87-w29": ("alpha", "harness"),
        f"{ws}-w82-w132": ("alpha", "w82-w132"),
        f"{ws}-w41-w29": ("beta", "harness"),
        f"{ws}-w78-w17-w87": ("beta", "w78-w17-w87"),
        f"{ws}-w78-w25-w119-w75": ("beta", "w78-w25-w119-w75"),
        f"{ws}-w78-w92-w68": ("beta", "w78-w92-w68"),
        f"{ws}-w82-w33-w29": ("gamma", "harness"),  # the harness before the repo at its root
        f"{ws}-w82-w38-w75": ("gamma", "w82-w38-w75"),
    }
    personal = {name: p.location for name, p in mapped.items() if p.scope == "personal"}
    assert len(personal) == 91 - 8
    # Only an exact match belongs to a project: a directory inside a harness, a sibling named after it, and a slug
    # holding a project's slug inside it are personal.
    paths = dict(fixture_rows())
    assert paths[f"{ws}-w41-w29-w68-w20"].endswith("/w41-w29/w68/w20")
    assert personal[f"{ws}-w41-w29-w68-w20"] == f"{WORKSPACE}-w41-w29-w68-w20"
    assert paths[f"{ws}-w41-w29-w55"].endswith(f"/{WORKSPACE}/w41-w29-w55")
    assert personal[f"{ws}-w41-w29-w55"] == f"{WORKSPACE}-w41-w29-w55"
    assert personal["-Users-someone"] == "~"
    assert personal[ws] == WORKSPACE
    outside = [name for name in personal if not name.startswith("-Users-someone")]
    assert len(outside) == 9 and all(personal[name] == name for name in outside)  # outside home: the whole slug
    assert any(f"--Users-someone-{WORKSPACE}-w41-w29-" in name for name in outside)
    dotted = [name for name in personal if name.startswith(ws) and "--" in name]
    assert dotted and all(personal[name] == name[len("-Users-someone-") :] for name in dotted)

    # Personal places come back to the same directory here, and under another home directory on another machine.
    other = Path("/home/bob")
    for name, location in personal.items():
        assert personal_slug(location, SOMEONE) == name
        moved = personal_slug(location, other)
        assert moved == (name if not name.startswith("-Users-someone") else "-home-bob" + name[len("-Users-someone") :])
        assert places.directory(Place("personal", None, location)) == name
    # A path under the home directory whose first part starts with a dot keeps its own form.
    dotted = slug(SOMEONE / ".claude" / "x")
    assert personal_location(dotted, SOMEONE) == "~--claude-x" and personal_slug("~--claude-x", SOMEONE) == dotted
    for bad in ("", "a/b", "..", "~/x", "x.y", "-" * 300):
        assert personal_slug(bad, SOMEONE) is None

    # Project places come back to the directory of their path here.
    for name, (project, location) in projects.items():
        assert places.directory(Place("project", project, location)) == name
    assert places.directory(Place("project", "gamma", "w82-w33-w29")) == f"{ws}-w82-w33-w29"
    assert places.directory(Place("project", "alpha", "gone-repo")) is None
    # A personal memory is never pulled into a directory a project holds here.
    assert places.directory(Place("personal", None, f"{WORKSPACE}-w82-w132")) is None


def test_the_registry_places_projects_and_a_directory_two_projects_claim_syncs_with_neither(monkeypatch):
    monkeypatch.setenv("HOME", str(SOMEONE))
    registry = {"clusters": [{"name": "alpha", "root": "/Volumes/code/w82-w87-w29", "workspace": "/Volumes/code"}]}
    places = Places.from_hub(hub_projects(), registry, "https://hub.test", SOMEONE)
    assert places.candidates("-Volumes-code-w82-w87-w29")[0] == Place("project", "alpha", "harness")
    assert places.candidates("-Volumes-code-w82-w132")[0] == Place("project", "alpha", "w82-w132")
    assert places.candidates(f"-Users-someone-{WORKSPACE}-w82-w132")[0].scope == "personal"  # moved with the registry
    assert places.candidates(f"-Users-someone-{WORKSPACE}-w41-w29")[0].project == "beta"

    clash = hub_projects()
    clash[1]["repos"].append({"name": "w82-w132", "path": "w82-w132"})  # beta claims alpha's repo too
    places = Places.from_hub(clash, {"clusters": []}, "https://hub.test", SOMEONE)
    with pytest.raises(HubError, match="projects alpha, beta at once"):
        places.candidates(f"-Users-someone-{WORKSPACE}-w82-w132")
    assert places.directory(Place("project", "beta", "w82-w132")) is None


def lister(*repos: str, name: str = "omega", level: str = "internal", harness: str | None = None) -> dict:
    """A project as GET /v1/projects lists it whose repos include directories of other projects, the way a harness
    lists the harnesses of the projects consuming its seams."""
    return {
        "name": name,
        "harness": {"name": name, "workspace": f"~/{WORKSPACE}", "path": harness or f"{name}-harness"},
        "default_label": {"level": level},
        "repos": [{"name": repo, "path": repo} for repo in repos],
    }


def placed(projects: list, bindings: dict | None = None) -> Places:
    return Places.from_hub(projects, {"clusters": []}, "https://hub.test", SOMEONE, bindings)


def test_a_harness_root_belongs_to_its_project_whatever_lists_it_as_a_repo(monkeypatch):
    monkeypatch.setenv("HOME", str(SOMEONE))
    ws = f"-Users-someone-{WORKSPACE}"
    # omega lists the harnesses of alpha and beta, gamma's harness (a repo of gamma's own too) and a repo of gamma.
    places = placed([*hub_projects(), lister("w82-w87-w29", "w41-w29", "w82-w33-w29", "w82-w38-w75")])
    assert places.candidates(f"{ws}-w82-w87-w29") == [Place("project", "alpha", HARNESS)]
    assert places.candidates(f"{ws}-w41-w29") == [Place("project", "beta", HARNESS)]
    assert places.candidates(f"{ws}-w82-w33-w29") == [
        Place("project", "gamma", HARNESS),
        Place("project", "gamma", "w82-w33-w29"),
    ]
    assert places.candidates(f"{ws}-omega-harness") == [Place("project", "omega", HARNESS)]
    # A pull brings omega's memories of those repos to no directory here, and each harness its own.
    for repo in ("w82-w87-w29", "w41-w29", "w82-w33-w29"):
        assert places.directory(Place("project", "omega", repo)) is None
    assert places.directory(Place("project", "alpha", HARNESS)) == f"{ws}-w82-w87-w29"
    assert places.directory(Place("project", "gamma", "w82-w33-w29")) == f"{ws}-w82-w33-w29"
    # A plain repo two projects list is still refused, and the message names the binding that settles it.
    with pytest.raises(HubError, match="a repo of projects gamma, omega at once") as refused:
        places.candidates(f"{ws}-w82-w38-w75")
    assert f"`evo-agents kg bind --project <name> {SOMEONE / WORKSPACE / 'w82-w38-w75'}`" in str(refused.value)
    assert places.directory(Place("project", "gamma", "w82-w38-w75")) is None
    # The hooks place a session's directory through the same rule.
    hook = SimpleNamespace(places=places, target=slug)
    assert _memory_place(hook, SOMEONE / WORKSPACE / "w41-w29") == "beta"
    assert _memory_place(hook, SOMEONE / WORKSPACE / "w82-w38-w75") is None


def test_two_projects_with_one_harness_root_are_refused_whatever_else_claims_it(monkeypatch):
    monkeypatch.setenv("HOME", str(SOMEONE))
    root = SOMEONE / WORKSPACE / "w82-w87-w29"
    twin = lister("w82-w87-w29", name="delta", harness="w82-w87-w29")  # registered with alpha's harness root
    for bindings in (None, {str(root): {"project": "alpha"}}):  # not even a binding settles it
        places = placed([*hub_projects(), twin], bindings)
        with pytest.raises(HubError, match="the harness root of projects alpha, delta at once"):
            places.candidates(slug(root))
        assert places.directory(Place("project", "alpha", HARNESS)) is None
        assert places.directory(Place("project", "delta", HARNESS)) is None
        assert places.directory(Place("project", "delta", "w82-w87-w29")) is None
        assert places.directory(Place("personal", None, f"{WORKSPACE}-w82-w87-w29")) is None


def test_a_binding_chooses_among_the_projects_listing_a_repo_and_never_beyond_them(monkeypatch):
    monkeypatch.setenv("HOME", str(SOMEONE))
    repo = SOMEONE / WORKSPACE / "w82-w132"
    name = slug(repo)
    clash = hub_projects()
    clash[1]["repos"].append({"name": "w82-w132", "path": "w82-w132"})  # beta lists alpha's repo too
    with pytest.raises(HubError, match="a repo of projects alpha, beta at once"):
        placed(clash).candidates(name)

    places = placed(clash, {str(repo): {"project": "beta", "harness_root": "/elsewhere/beta-harness"}})
    assert places.candidates(name) == [Place("project", "beta", "w82-w132")]
    assert places.directory(Place("project", "beta", "w82-w132")) == name
    assert places.directory(Place("project", "alpha", "w82-w132")) is None
    # Bindings read as the knowledge graph reads them: the nearest bound ancestor, the directory's own over it.
    above = {str(SOMEONE / WORKSPACE): {"project": "alpha"}}
    assert placed(clash, above).candidates(name) == [Place("project", "alpha", "w82-w132")]
    assert placed(clash, {**above, str(repo): {"project": "beta"}}).candidates(name)[0].project == "beta"

    # A binding to a project that does not list the directory refuses it, even where one project alone lists it:
    # that project has no place for it, and the one listing it is not the one the person chose.
    for projects, bindings in (
        (clash, {str(repo): {"project": "gamma"}}),
        (clash, {str(repo): {"harness_root": "/somewhere"}}),
        (hub_projects(), {str(repo): {"project": "gamma"}}),
    ):
        with pytest.raises(HubError, match="bound to (gamma|no project) .* Choose one of those with `evo-agents kg"):
            placed(projects, bindings).candidates(name)
        assert placed(projects, bindings).directory(Place("project", "alpha", "w82-w132")) is None
    # A binding gives no project a directory no project lists: it stays personal.
    loose = SOMEONE / WORKSPACE / "w41-w29-w55"
    assert placed(clash, {str(loose): {"project": "beta"}}).candidates(slug(loose)) == [
        Place("personal", None, f"{WORKSPACE}-w41-w29-w55")
    ]


def test_a_customer_project_never_takes_the_harness_root_of_another_project_by_a_weaker_claim(monkeypatch):
    """alpha's harness root, listed as a repo by a customer-level project and even bound to it, stays alpha's; the
    other way round, the customer project's harness root never goes to the project of lower clearance listing it."""
    monkeypatch.setenv("HOME", str(SOMEONE))
    root = SOMEONE / WORKSPACE / "w82-w87-w29"
    vault = lister("w82-w87-w29", "w82-w132", name="vault", level="customer")
    consumer = lister("vault-harness", name="omega", level="internal")
    for bindings in (None, {str(root): {"project": "vault"}}):
        places = placed([*hub_projects(), vault, consumer], bindings)
        assert places.candidates(slug(root)) == [Place("project", "alpha", HARNESS)]
        assert places.directory(Place("project", "vault", "w82-w87-w29")) is None
        own = slug(SOMEONE / WORKSPACE / "vault-harness")
        assert places.candidates(own) == [Place("project", "vault", HARNESS)]
        assert places.directory(Place("project", "omega", "vault-harness")) is None
        assert places.directory(Place("project", "vault", HARNESS)) == own


def test_the_spelling_finder_of_the_opt_in_check_is_looser_than_the_rule(tmp_path):
    """The opt-in check finds paths by words and separators, so a path whose separators the rule would count
    differently is found, and then fails the rule: the check is not the rule checking itself."""
    (tmp_path / "a__b" / ".c").mkdir(parents=True)
    (tmp_path / "a-b").mkdir()
    root = slug(tmp_path)
    assert sorted(_paths_spelling(f"{root}-a-b", tmp_path / "skip")) == [f"{tmp_path}/a-b", f"{tmp_path}/a__b"]
    assert [slug(p) for p in _paths_spelling(f"{root}-a-b--c", tmp_path / "skip")] == [f"{root}-a--b--c"]
    assert _paths_spelling(f"{root}-a-b", tmp_path / "a__b") == [f"{tmp_path}/a-b"]  # never inside the skipped one


# Files: names, bodies, types, MEMORY.md


def test_names_and_bodies_a_memory_may_have():
    for good in ("user-math-background.md", "ghi chú.md", "a.b.md", "x" * 252 + ".md"):
        assert name_problem(good) is None, good
    for bad in ("MEMORY.md", "memory.md", "x.txt", ".md", ".hidden.md", "../x.md", "a/b.md", "a\\b.md", "a\nb.md"):
        assert name_problem(bad), bad
    assert name_problem("x" * 253 + ".md") and name_problem("\ud800.md")
    assert body_problem("x" * MAX_BODY) is None and body_problem("é" * (MAX_BODY // 2)) is None
    assert body_problem("x" * (MAX_BODY + 1)) and body_problem("é" * (MAX_BODY // 2 + 1))
    assert body_problem("a\x00b") and body_problem("\udc80")


def test_the_type_comes_from_metadata_type_then_type_then_defaults_to_user():
    assert memory_type("---\nname: a\nmetadata:\n  type: feedback\n---\nbody") == "feedback"
    assert memory_type("---\nname: a\ntype: Reference\n---\n") == "reference"
    assert memory_type("---\r\nmetadata: {type: project}\r\n---\r\nbody") == "project"
    assert memory_type("---\nmetadata:\n  type: project\ntype: user\n---\n") == "project"  # metadata.type first
    for untyped in ("no frontmatter", "---\ntype: galaxy\n---\n", "---\n: : bad yaml [\n---\n", "---\n---\n", ""):
        assert memory_type(untyped) == "user"


def test_memory_md_gets_the_pointer_lines_it_lacks_and_keeps_every_line(tmp_path):
    directory = tmp_path / "memory"
    directory.mkdir()
    index = directory / INDEX
    held = "# Memory\n- [Gone](gone.md) — a file that is not here any more\n- [Math](./math.md) — kept as written"
    index.write_text(held, encoding="utf-8")  # no newline at the end
    files = {
        "math.md": "---\nname: Math\n---\n",
        "new one.md": "---\nname: New [one]\ndescription: |\n  two\n  lines\n---\n",
        "plain.md": "no frontmatter",
    }
    assert add_pointers(directory, files) == ["new one.md", "plain.md"]
    assert index.read_text(encoding="utf-8") == (
        held + "\n- [New (one)](<new one.md>) — two lines\n- [plain](plain.md)\n"
    )
    before = index.stat()
    assert add_pointers(directory, files) == []  # nothing missing: the file is not written again
    assert index.stat().st_mtime_ns == before.st_mtime_ns and index.stat().st_ino == before.st_ino
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert add_pointers(fresh, {"a.md": "---\ndescription: d\n---\n"}) == ["a.md"]
    assert (fresh / INDEX).read_text(encoding="utf-8") == "- [a](a.md) — d\n"
    assert pointer_line("x.md", "---\nname: X\n---\n") == "- [X](x.md)"


# The server


needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)

PROJECT = {
    "levels": LEVELS,
    "locations": ["any"],
    "default_label": {"level": "internal"},
    "sinks": [
        {"id": AGENT, "kind": "agent-session", "clearance": {"level": "customer"}},
        {"id": "hub", "kind": "hub", "clearance": {"level": "internal"}},
    ],
    "repos": [{"name": "app", "path": "app"}],
    "harness": {"name": "demo", "workspace": "~/ws", "path": "demo-harness"},
}
MEMBERS = {"alice": ("writer", "internal"), "bob": ("writer", "internal"), "carol": ("reader", "public")}


@pytest.fixture
def client(hub_db, tmp_path, github):
    from fastapi.testclient import TestClient

    from evo_agents.hub.server.app import create_app

    with TestClient(create_app(live.hub_config(hub_db, tmp_path, github))) as client:
        yield client


@pytest.fixture
def who(client, hub_db) -> dict:
    """Bearer headers by login: the admin, three members of demo and a stranger."""
    members = {login: live.bearer(live.insert_token(hub_db, login)) for login in (live.ADMIN, *MEMBERS)}
    members["stranger"] = live.bearer(live.insert_token(hub_db, "stranger"))
    assert client.put("/v1/projects/demo", json=PROJECT, headers=members[live.ADMIN]).status_code == 200
    for login, (role, level) in MEMBERS.items():
        grant = {"role": role, "max_level": level}
        response = client.put(f"/v1/admin/projects/demo/grants/{login}", json=grant, headers=members[live.ADMIN])
        assert response.status_code == 200, response.text
    return members


def put(client, headers, **fields):
    body = {
        "scope": "project",
        "project": "demo",
        "location": "harness",
        "name": "note.md",
        "type": "project",
        "body": "---\nname: Note\n---\nthe note\n",
        **fields,
    }
    return client.put("/v1/memories", json=body, headers=headers)


def names(client, headers, **params) -> list[str]:
    response = client.get("/v1/memories", params={"limit": 500, **params}, headers=headers)
    assert response.status_code == 200, response.text
    return sorted(m["name"] for m in response.json()["items"])


def counts(db) -> dict:
    return {
        table: live.sql(db, f"SELECT count(*) FROM {table}")[0][0]
        for table in ("memories", "memory_revisions", "audit")
    }


@needs_pg
def test_other_members_never_see_user_and_feedback_memories(client, who, hub_db):
    created = {}
    for kind in ("user", "feedback", "project", "reference"):
        response = put(client, who["alice"], name=f"{kind}.md", type=kind, body=f"alice {kind} kangaroo")
        assert response.status_code == 200, response.text
        created[kind] = response.json()
    personal = client.put(
        "/v1/memories",
        json={"scope": "personal", "location": "notes-math", "name": "math.md", "type": "project", "body": "kangaroo"},
        headers=who["alice"],
    )
    assert personal.status_code == 200, personal.text
    assert personal.json()["label"] == {}

    assert names(client, who["alice"]) == ["feedback.md", "math.md", "project.md", "reference.md", "user.md"]
    assert names(client, who["bob"]) == ["project.md", "reference.md"]
    assert names(client, who["bob"], scope="personal") == []
    # The label rule on top: carol's grant reaches public, and the project's memories are internal.
    assert names(client, who["carol"]) == []
    assert client.get(f"/v1/memories/{created['project']['id']}", headers=who["carol"]).status_code == 404
    # A hub admin without a grant manages the project and reads nothing in it; a stranger does not see it at all.
    assert names(client, who[live.ADMIN]) == []
    assert client.get("/v1/memories", params={"project": "demo"}, headers=who["stranger"]).status_code == 404

    # Asked for directly, another member's user and feedback memories answer as a memory that does not exist.
    missing = client.get("/v1/memories/999999", headers=who["bob"])
    for kind in ("user", "feedback"):
        hidden = client.get(f"/v1/memories/{created[kind]['id']}", headers=who["bob"])
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json()["message"].replace(str(created[kind]["id"]), "N") == missing.json()["message"].replace(
            "999999", "N"
        )
    assert client.get(f"/v1/memories/{personal.json()['id']}", headers=who["bob"]).status_code == 404
    assert (
        client.get(f"/v1/memories/{created['user']['id']}", headers=who["alice"]).json()["body"]
        == "alice user kangaroo"
    )

    # Nor are they found by searching, by deleting, or by writing over them.
    found = client.get("/v1/memories/search", params={"q": "kangaroo"}, headers=who["bob"]).json()["items"]
    assert sorted(m["name"] for m in found) == ["project.md", "reference.md"]
    mine = client.get("/v1/memories/search", params={"q": "kangaroo"}, headers=who["alice"]).json()["items"]
    assert len(mine) == 5
    hidden_delete = client.delete(f"/v1/memories/{created['user']['id']}?if_revision=1", headers=who["bob"])
    assert hidden_delete.status_code == 404
    bobs = put(client, who["bob"], name="user.md", type="user", body="bob's own")  # a memory of bob's own
    assert bobs.status_code == 200 and bobs.json()["created"] and bobs.json()["id"] != created["user"]["id"]
    assert (
        client.get(f"/v1/memories/{created['user']['id']}", headers=who["alice"]).json()["body"]
        == "alice user kangaroo"
    )


@needs_pg
def test_if_revision_conflicts_carry_the_current_version_and_delete_leaves_a_tombstone(client, who, hub_db, caplog):
    caplog.set_level(logging.INFO, logger="evo_agents.hub")
    first = put(client, who["alice"], body="v1 secret-content")
    assert first.status_code == 200 and (first.json()["created"], first.json()["revision"]) == (True, 1)
    memory_id = first.json()["id"]
    before = counts(hub_db)

    same = put(client, who["bob"], body="v1 secret-content")  # the same content: no change, no audit row
    assert same.status_code == 200 and (same.json()["changed"], same.json()["revision"]) == (False, 1)
    assert counts(hub_db) == before

    blind = put(client, who["bob"], body="v2 from bob")
    assert blind.status_code == 409 and blind.json()["error"] == "conflict"
    assert blind.json()["current"]["body"] == "v1 secret-content" and blind.json()["current"]["revision"] == 1
    assert "request_id" in blind.json()

    second = put(client, who["bob"], body="v2 from bob", if_revision=1)
    assert second.status_code == 200 and second.json()["revision"] == 2 and second.json()["updated_by"] == "bob"
    stale = put(client, who["alice"], body="v2 from alice", if_revision=1)
    assert stale.status_code == 409 and stale.json()["current"]["body"] == "v2 from bob"

    assert client.delete(f"/v1/memories/{memory_id}?if_revision=1", headers=who["alice"]).status_code == 409
    gone = client.delete(f"/v1/memories/{memory_id}?if_revision=2", headers=who["alice"])
    assert gone.status_code == 200, gone.text
    assert (gone.json()["deleted"], gone.json()["body"], gone.json()["revision"]) == (True, "", 3)
    again = client.delete(f"/v1/memories/{memory_id}?if_revision=2", headers=who["alice"])
    assert again.status_code == 200 and again.json()["changed"] is False
    assert names(client, who["bob"]) == [] and names(client, who["bob"], deleted=True) == ["note.md"]
    assert client.get("/v1/memories/search", params={"q": "v2"}, headers=who["bob"]).json()["items"] == []

    back = put(client, who["alice"], body="v4 back")  # a tombstone counts as nothing: create without if_revision
    assert back.status_code == 200 and (back.json()["id"], back.json()["revision"]) == (memory_id, 4)
    assert put(client, who["alice"], body="x", if_revision=9, name="other.md").status_code == 409  # nothing there

    history = live.sql(
        hub_db,
        "SELECT revision, body, deleted FROM memory_revisions WHERE memory_id = %s ORDER BY revision",
        (memory_id,),
    )
    assert history == [(1, "v1 secret-content", False), (2, "v2 from bob", False), (3, "", True), (4, "v4 back", False)]
    audit = live.sql(
        hub_db,
        "SELECT a.action, a.target, p.name FROM audit a LEFT JOIN projects p ON p.id = a.project_id "
        "WHERE a.action LIKE 'memory.%%' ORDER BY a.id",
    )
    assert audit == [("memory.put", f"memory:{memory_id}", "demo")] * 2 + [
        ("memory.delete", f"memory:{memory_id}", "demo")
    ] + [("memory.put", f"memory:{memory_id}", "demo")]  # filed under the memory's project (schema 0007)
    logged = caplog.text + json.dumps([vars(r) for r in caplog.records], default=str)
    stored = json.dumps(live.sql(hub_db, "SELECT * FROM audit"), default=str) + logged
    for content in ("secret-content", "from bob", "note.md", "v4 back"):
        assert content not in stored
    outcomes = [r.outcome for r in caplog.records if r.getMessage() == "memory write"]
    assert outcomes == ["created", "updated", "deleted", "restored"]


@needs_pg
def test_the_write_rule_and_the_bounds_refuse_without_writing_anything(client, who, hub_db):
    put(client, who["alice"], name="kept.md")
    before = counts(hub_db)

    def refused(response, status, needle=None):
        assert response.status_code == status, response.text
        assert response.headers["content-type"] == "application/json"
        if needle:
            assert needle in response.text

    refused(put(client, who["carol"]), 403, "writer role")
    refused(put(client, who["stranger"]), 404, "no project demo that you can see")
    refused(put(client, who[live.ADMIN]), 403, "writer role")  # a hub admin is no writer by being one
    refused(put(client, who["alice"], label={"level": "customer"}), 422, "not cleared by hub sink")
    refused(put(client, who["alice"], location="nope"), 422, "harness or a repo of project demo")
    personal = {"scope": "personal", "location": "notes", "name": "a.md", "type": "user", "body": "x"}
    refused(client.put("/v1/memories", json={**personal, "label": {"level": "public"}}, headers=who["alice"]), 422)
    refused(client.put("/v1/memories", json={**personal, "location": "a/b"}, headers=who["alice"]), 422)
    refused(client.put("/v1/memories", json={**personal, "project": "demo"}, headers=who["alice"]), 422)
    for name in ("MEMORY.md", "../x.md", "x.txt", ".hidden.md", "a\\b.md"):
        refused(put(client, who["alice"], name=name), 422)
    for body in ("é" * (MAX_BODY // 2 + 1), "a\x00b"):
        refused(put(client, who["alice"], body=body), 422)
    surrogate = json.dumps({**personal, "body": "x"}).replace('"x"', '"\\ud800"').encode()
    refused(
        client.put("/v1/memories", content=surrogate, headers={**who["alice"], "content-type": "application/json"}), 422
    )
    rejected = put(client, who["alice"], body="z" * MAX_BODY + "secret-tail")
    refused(rejected, 422)
    assert "secret-tail" not in rejected.text  # validation errors never echo the input

    huge = b'{"body": "' + b"\\u0001" * (MAX_BODY + 20_000) + b'"}'
    refused(client.put("/v1/memories", content=huge, headers={**who["alice"], "content-type": "application/json"}), 413)

    def chunks():  # no content-length: the limit holds while reading
        for _ in range(200):
            yield b"x" * 10_000

    refused(client.put("/v1/memories", content=chunks(), headers=who["alice"]), 413)
    refused(client.get("/v1/memories", params={"cursor": "not-a-cursor"}, headers=who["alice"]), 422, "cursor")
    refused(client.get("/v1/memories", params={"project": "demo", "scope": "personal"}, headers=who["alice"]), 422)
    refused(client.get("/v1/memories/search", params={"q": ""}, headers=who["alice"]), 422)
    assert counts(hub_db) == before

    no_hub_sink = {**PROJECT, "sinks": PROJECT["sinks"][:1], "harness": {**PROJECT["harness"], "name": "closed"}}
    assert client.put("/v1/projects/closed", json=no_hub_sink, headers=who[live.ADMIN]).status_code == 200
    grant = {"role": "writer", "max_level": "secret"}
    client.put("/v1/admin/projects/closed/grants/alice", json=grant, headers=who[live.ADMIN])
    before = counts(hub_db)
    refused(put(client, who["alice"], project="closed"), 422, "declares no sink of kind hub")
    assert counts(hub_db) == before


@needs_pg
def test_listing_pages_by_cursor_and_search_ranks_full_text(client, who):
    ids = []
    for number in range(7):
        response = put(
            client, who["alice"], name=f"m{number}.md", body=f"memory number {number} wombat " * (number + 1)
        )
        ids.append(response.json()["id"])
    put(client, who["alice"], name="m2.md", body="changed: now about echidnas", if_revision=1)

    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        page = client.get("/v1/memories", params=params, headers=who["bob"]).json()
        seen += [m["name"] for m in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == ["m0.md", "m1.md", "m3.md", "m4.md", "m5.md", "m6.md", "m2.md"]  # by last change
    assert pages == 3

    found = client.get("/v1/memories/search", params={"q": "echidnas"}, headers=who["bob"]).json()["items"]
    assert [m["name"] for m in found] == ["m2.md"] and found[0]["rank"] > 0
    ranked = client.get("/v1/memories/search", params={"q": "wombat", "limit": 3}, headers=who["bob"]).json()["items"]
    assert [m["name"] for m in ranked] == ["m6.md", "m5.md", "m4.md"]  # more matches rank higher
    by_name = client.get("/v1/memories/search", params={"q": "m3.md"}, headers=who["bob"]).json()["items"]
    assert [m["name"] for m in by_name] == ["m3.md"]
    assert client.get("/v1/memories/search", params={"q": '"number 4" -wombat'}, headers=who["bob"]).json() == {
        "items": []
    }


@needs_pg
def test_search_breaks_a_tie_in_rank_by_the_latest_change(client, who):
    for name in ("a.md", "b.md", "c.md"):
        assert put(client, who["alice"], name=name, body="one kiwi").status_code == 200
    changed = put(client, who["alice"], name="a.md", type="reference", body="one kiwi", if_revision=1)
    assert changed.status_code == 200 and changed.json()["revision"] == 2
    found = client.get("/v1/memories/search", params={"q": "kiwi"}, headers=who["bob"]).json()["items"]
    assert len({m["rank"] for m in found}) == 1
    assert [m["name"] for m in found] == ["a.md", "c.md", "b.md"]


@needs_pg
def test_the_schema_holds_the_same_bounds(client, who, hub_db):
    from psycopg import errors

    put(client, who["alice"], name="kept.md")
    insert = (
        "INSERT INTO memories (scope, project_id, location, name, type, owner_id, label, body, updated_by, deleted) "
        "SELECT %s, p.id, %s, %s, 'project', p.created_by, '{}', %s, p.created_by, %s FROM projects p"
    )
    with pg.admin(hub_db.admin_dsn) as conn:
        for scope, location, name, body, deleted in (
            ("project", "harness", "MEMORY.md", "x", False),
            ("project", "harness", "a/b.md", "x", False),
            ("project", "harness", "x.txt", "x", False),
            ("project", "harness", "x.md", "y" * (MAX_BODY + 1), False),
            ("project", "harness", "x.md", "left behind", True),
            ("project", "a/b", "x.md", "x", False),
        ):
            with pytest.raises(errors.CheckViolation):
                conn.execute(insert, (scope, location, name, body, deleted))
        with pytest.raises(errors.CheckViolation):
            conn.execute(
                "INSERT INTO memories (scope, location, name, type, owner_id, label, body, updated_by) "
                "SELECT 'personal', 'not.a.slug', 'x.md', 'user', id, '{}', 'x', id FROM users LIMIT 1"
            )


# Sync, in process: the client library over the app


class InProcessHub:
    """``Hub.call`` through the in-process app, as one member; counts the requests."""

    url = "https://hub.test"

    def __init__(self, client, headers):
        self.client = client
        self.headers = headers
        self.calls: list[tuple[str, str]] = []

    def call(self, method, path, body=None):
        self.calls.append((method, path.partition("?")[0]))
        response = self.client.request(method, path, json=body, headers=self.headers)
        payload = response.json() if response.content else None
        if response.status_code >= 400:
            raise HubError(payload.get("message"), response.status_code, payload.get("error"), payload)
        return payload


class Machine:
    """A home directory standing for one machine of one member."""

    def __init__(self, home: Path, hub: InProcessHub, login: str, monkeypatch):
        self.home, self.hub, self.login, self.monkeypatch = home, hub, login, monkeypatch
        home.mkdir(parents=True, exist_ok=True)

    def run(self, command: str, target: Path | None = None, **options):
        self.monkeypatch.setenv("HOME", str(self.home))
        sync = MemorySync(self.hub, self.login, host="test-host", **options)
        return getattr(sync, command)(target, everything=target is None)

    def dir(self, relative: str) -> Path:
        """The memory directory of working directory ~/``relative``."""
        return self.home / ".claude" / "projects" / slug(self.home / relative) / "memory"

    def write(self, relative: str, name: str, text: str) -> Path:
        path = self.dir(relative) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return path

    def files(self) -> dict[tuple[str, str], bytes]:
        """Every memory file by (directory under the home, name); MEMORY.md and conflict copies left out."""
        base = self.home / ".claude" / "projects"
        prefix = slug(self.home)
        found = {}
        for directory in sorted(base.glob("*/memory")) if base.is_dir() else []:
            where = directory.parent.name[len(prefix) :] if directory.parent.name.startswith(prefix) else "/"
            for path in directory.iterdir():
                if path.name != INDEX and ".conflict-" not in path.name:
                    found[(where, path.name)] = path.read_bytes()
        return found

    def state(self) -> bytes:
        path = self.home / ".evo" / "hub" / "memory-state.json"
        return path.read_bytes() if path.exists() else b""


def typed(kind: str, text: str = "") -> str:
    return f"---\nname: {kind} memory\ndescription: about {kind}\nmetadata:\n  type: {kind}\n---\n{text or kind}\n"


@needs_pg
def test_a_sync_writes_nothing_twice_and_a_dry_run_writes_nothing(client, who, hub_db, tmp_path, monkeypatch):
    laptop = Machine(tmp_path / "laptop", InProcessHub(client, who["alice"]), "alice", monkeypatch)
    laptop.write("ws/demo-harness", "shared.md", typed("project", "dòng tiếng Việt\r\nCRLF kept\r\n"))
    laptop.write("ws/demo-harness", "mine.md", typed("feedback"))
    laptop.write("ws/demo-harness", INDEX, "- [Shared](shared.md)\n")
    laptop.write("ws/demo-harness", "old.conflict-other.md", "a conflict copy")
    laptop.write("notes", "math.md", typed("user", "weak at math"))
    (laptop.dir("ws/demo-harness") / "linked.md").symlink_to(tmp_path / "outside.md")
    (tmp_path / "outside.md").write_text("never pushed", encoding="utf-8")

    dry = laptop.run("push", dry_run=True)
    assert dry.counts["created"] == 3 and dict(dry.places) == {"project demo": 2, "personal": 1}
    assert counts(hub_db)["memories"] == 0 and laptop.state() == b""
    assert not (laptop.home / ".evo").exists()  # not even the lock

    pushed = laptop.run("push")
    assert pushed.counts["created"] == 3 and not pushed.conflicts
    assert any("linked.md: not pushed, a symlink" in e for e in pushed.errors)
    assert any("conflict copies wait" in n for n in pushed.notes)
    stored = sorted(r[0] for r in live.sql(hub_db, "SELECT name FROM memories"))
    assert stored == ["math.md", "mine.md", "shared.md"]  # never MEMORY.md, a conflict copy or a symlink
    held = live.sql(hub_db, "SELECT body FROM memories WHERE name = 'shared.md'")[0][0]
    assert held.encode() == (laptop.dir("ws/demo-harness") / "shared.md").read_bytes()
    assert live.sql(hub_db, "SELECT scope, location FROM memories WHERE name = 'math.md'") == [("personal", "notes")]

    state, rows = laptop.state(), counts(hub_db)
    laptop.hub.calls.clear()
    again = laptop.run("push")
    assert again.counts["unchanged"] == 3 and laptop.hub.calls == [("GET", "/v1/projects")]
    assert laptop.state() == state and counts(hub_db) == rows
    mtimes = {p: p.stat().st_mtime_ns for p in laptop.dir("ws/demo-harness").iterdir()}
    pulled = laptop.run("pull")
    assert pulled.counts["unchanged"] == 3 and pulled.counts["indexed"] == 2  # mine.md, and math.md in a new index
    assert "- [Shared](shared.md)\n- [feedback memory](mine.md) — about feedback\n" == (
        laptop.dir("ws/demo-harness") / INDEX
    ).read_text(encoding="utf-8")
    laptop.run("pull")
    assert {p: p.stat().st_mtime_ns for p in laptop.dir("ws/demo-harness").iterdir() if p.name != INDEX} == {
        p: t for p, t in mtimes.items() if p.name != INDEX
    }
    assert laptop.state() == state


@needs_pg
def test_deletions_cross_only_with_prune(client, who, hub_db, tmp_path, monkeypatch):
    hub = InProcessHub(client, who["alice"])
    one, two = (Machine(tmp_path / name, hub, "alice", monkeypatch) for name in ("one", "two"))
    for name in ("a.md", "b.md", "c.md"):
        one.write("ws/demo-harness", name, typed("project", name))
    one.run("push")
    two.run("pull")
    assert two.files() == one.files()

    (one.dir("ws/demo-harness") / "a.md").unlink()
    kept = one.run("push")
    assert kept.counts["kept_on_hub"] == 1 and names(client, who["bob"]) == ["a.md", "b.md", "c.md"]
    restored = one.run("pull")  # deleted here only: the hub's copy comes back
    assert restored.counts["restored"] == 1 and (one.dir("ws/demo-harness") / "a.md").exists()

    (one.dir("ws/demo-harness") / "a.md").unlink()
    assert one.run("push", prune=True).counts["deleted"] == 1
    assert names(client, who["bob"]) == ["b.md", "c.md"]
    assert one.run("pull").counts["pulled"] == 0 and not (one.dir("ws/demo-harness") / "a.md").exists()

    waiting = two.run("pull")  # without --prune the other machine keeps its copy, and says so
    assert waiting.counts["kept_here"] == 1 and (two.dir("ws/demo-harness") / "a.md").exists()
    assert two.run("push").counts["unchanged"] == 3  # the copy the hub deleted is unchanged: not pushed back
    assert two.run("pull", prune=True).counts["deleted"] == 1
    assert two.files() == one.files()


@needs_pg
def test_a_project_memory_never_falls_back_to_personal_and_a_personal_one_never_lands_in_a_project(
    client, who, hub_db, tmp_path, monkeypatch
):
    laptop = Machine(tmp_path / "laptop", InProcessHub(client, who["alice"]), "alice", monkeypatch)
    laptop.write("ws/app", "app.md", typed("project"))
    laptop.run("push")
    assert live.sql(hub_db, "SELECT scope, location FROM memories") == [("project", "app")]

    # The project's repo moves elsewhere in the registry: this directory is no longer the project's here.
    registry = laptop.home / ".claude" / "harness" / "registry.json"
    registry.parent.mkdir(parents=True)
    cluster = {
        "name": "demo",
        "root": str(tmp_path / "elsewhere" / "demo-harness"),
        "workspace": str(tmp_path / "elsewhere"),
    }
    registry.write_text(json.dumps({"clusters": [cluster]}), encoding="utf-8")
    (laptop.dir("ws/app") / "app.md").write_text(typed("project", "changed"), encoding="utf-8")
    moved = laptop.run("push")
    assert any("not pushed as personal" in e for e in moved.errors)
    assert live.sql(hub_db, "SELECT scope, body FROM memories") == [("project", typed("project"))]
    registry.unlink()

    # A personal memory whose directory belongs to the project on this machine is not pulled into it.
    personal = {"scope": "personal", "location": "ws-app", "name": "p.md", "type": "user", "body": "personal"}
    assert client.put("/v1/memories", json=personal, headers=who["alice"]).status_code == 200
    pulled = laptop.run("pull")
    assert pulled.counts["no_directory"] == 1 and not (laptop.dir("ws/app") / "p.md").exists()


def grant(client, who, project: str, body: dict, level: str = "internal") -> None:
    """Register ``project`` as ``body`` and make alice a writer of it up to ``level``."""
    admin = who[live.ADMIN]
    assert client.put(f"/v1/projects/{project}", json=body, headers=admin).status_code == 200
    access = {"role": "writer", "max_level": level}
    assert client.put(f"/v1/admin/projects/{project}/grants/alice", json=access, headers=admin).status_code == 200


PLACED = "SELECT p.name, m.location, m.body FROM memories m JOIN projects p ON p.id = m.project_id ORDER BY m.id"


@needs_pg
def test_a_harness_root_another_project_lists_as_a_repo_syncs_with_its_own_project(
    client, who, hub_db, tmp_path, monkeypatch
):
    seams = {
        **PROJECT,
        "repos": [{"name": "demo-harness", "path": "demo-harness"}],
        "harness": {"name": "seams", "workspace": "~/ws", "path": "seams-harness"},
    }
    grant(client, who, "seams", seams)
    laptop = Machine(tmp_path / "laptop", InProcessHub(client, who["alice"]), "alice", monkeypatch)
    laptop.write("ws/demo-harness", "h.md", typed("project", "harness notes"))
    pushed = laptop.run("push")
    assert pushed.counts["created"] == 1 and not pushed.errors
    assert live.sql(hub_db, PLACED) == [("demo", "harness", typed("project", "harness notes"))]

    # A memory of seams at its repo demo-harness has no directory here: the directory is demo's.
    assert put(client, who["alice"], project="seams", location="demo-harness", name="s.md").status_code == 200
    pulled = laptop.run("pull")
    assert pulled.counts["no_directory"] == 1 and not (laptop.dir("ws/demo-harness") / "s.md").exists()
    sync = MemorySync(laptop.hub, "alice", host="test-host")
    alone = sync.pull(laptop.home / "ws" / "demo-harness")
    assert alone.counts["unchanged"] == 1 and not alone.errors and not (laptop.dir("ws/demo-harness") / "s.md").exists()
    assert _memory_place(sync, laptop.home / "ws" / "demo-harness") == "demo"


@needs_pg
def test_a_repo_of_two_projects_syncs_once_a_binding_chooses_and_a_memory_never_moves_between_them(
    client, who, hub_db, tmp_path, monkeypatch
):
    grant(client, who, "demo", {**PROJECT, "repos": [*PROJECT["repos"], {"name": "shared", "path": "shared"}]})
    vault = {
        **PROJECT,
        "default_label": {"level": "customer"},
        "sinks": [
            {"id": AGENT, "kind": "agent-session", "clearance": {"level": "customer"}},
            {"id": "hub", "kind": "hub", "clearance": {"level": "customer"}},
        ],
        "repos": [{"name": "shared", "path": "shared"}],
        "harness": {"name": "vault", "workspace": "~/ws", "path": "vault-harness"},
    }
    grant(client, who, "vault", vault, level="customer")
    laptop = Machine(tmp_path / "laptop", InProcessHub(client, who["alice"]), "alice", monkeypatch)
    path = laptop.write("ws/shared", "s.md", typed("project", "customer notes"))
    refused = laptop.run("push")
    assert [e for e in refused.errors if "a repo of projects demo, vault at once" in e and "kg bind --project" in e]
    assert counts(hub_db)["memories"] == 0

    bound = laptop.home / ".evo" / "kg" / "bound.json"  # as `evo-agents kg bind` writes it
    bound.parent.mkdir(parents=True)
    shared = str((laptop.home / "ws" / "shared").resolve())
    bound.write_text(json.dumps({shared: {"project": "vault", "harness_root": "/elsewhere"}}), encoding="utf-8")
    assert laptop.run("push").counts["created"] == 1
    assert live.sql(hub_db, PLACED) == [("vault", "shared", typed("project", "customer notes"))]

    # Bound to demo now: the memory synced with vault does not follow the directory into demo.
    bound.write_text(json.dumps({shared: {"project": "demo"}}), encoding="utf-8")
    path.write_text(typed("project", "changed"), encoding="utf-8")
    held = laptop.run("push")
    assert [e for e in held.errors if "never moves between projects by itself" in e]
    assert held.counts["created"] == held.counts["updated"] == held.counts["moved"] == 0
    assert live.sql(hub_db, PLACED) == [("vault", "shared", typed("project", "customer notes"))]
    # A new file goes to the project the binding chose.
    laptop.write("ws/shared", "t.md", typed("project", "internal notes"))
    assert laptop.run("push").counts["created"] == 1
    assert live.sql(hub_db, PLACED)[-1] == ("demo", "shared", typed("project", "internal notes"))


@needs_pg
def test_a_failed_write_leaves_the_file_whole_and_the_state_of_what_was_done(client, who, tmp_path, monkeypatch):
    hub = InProcessHub(client, who["alice"])
    one, two = (Machine(tmp_path / name, hub, "alice", monkeypatch) for name in ("one", "two"))
    one.write("ws/demo-harness", "a.md", typed("project", "first"))
    one.run("push")
    two.run("pull")
    (one.dir("ws/demo-harness") / "a.md").write_text(typed("project", "second"), encoding="utf-8")
    one.run("push")

    replace = os.replace

    def disk_full(src, dst):
        if str(dst).endswith("a.md"):
            raise OSError(28, "No space left on device")
        replace(src, dst)

    monkeypatch.setattr(hub_client.os, "replace", disk_full)
    before = (two.dir("ws/demo-harness") / "a.md").read_bytes()
    with pytest.raises(OSError, match="No space left"):
        two.run("pull")
    monkeypatch.setattr(hub_client.os, "replace", replace)
    assert (two.dir("ws/demo-harness") / "a.md").read_bytes() == before
    assert sorted(p.name for p in two.dir("ws/demo-harness").iterdir()) == [INDEX, "a.md"]  # no temporary file
    assert two.run("pull").counts["updated"] == 1  # the state still says what was synced, so it picks up again
    assert (two.dir("ws/demo-harness") / "a.md").read_text(encoding="utf-8") == typed("project", "second")


@needs_pg
def test_a_type_or_place_change_moves_a_memory_and_a_shared_copy_goes_only_with_prune(
    client, who, hub_db, tmp_path, monkeypatch
):
    laptop = Machine(tmp_path / "laptop", InProcessHub(client, who["alice"]), "alice", monkeypatch)
    path = laptop.write("ws/demo-harness", "x.md", typed("project", "shared at first"))
    laptop.run("push")
    path.write_text(typed("user", "now mine alone"), encoding="utf-8")
    held = laptop.run("push")
    assert held.counts["held"] == 1 and any("push --prune` moves it" in n for n in held.notes)
    assert names(client, who["bob"]) == ["x.md"]  # members still see the shared copy, nothing moved
    moved = laptop.run("push", prune=True)
    assert (moved.counts["created"], moved.counts["moved"]) == (1, 1)
    assert names(client, who["bob"]) == [] and names(client, who["alice"]) == ["x.md"]
    assert live.sql(hub_db, "SELECT type, deleted FROM memories ORDER BY id") == [("project", True), ("user", False)]

    path.write_text(typed("reference", "shared again"), encoding="utf-8")  # a private copy moves without --prune
    assert laptop.run("push").counts["moved"] == 1
    assert names(client, who["bob"]) == ["x.md"]

    # A directory that was personal becomes the project's: its memories move, the personal copies go.
    notes = laptop.write("ws/later", "n.md", typed("project", "personal until the repo is registered"))
    laptop.run("push")
    assert live.sql(hub_db, "SELECT scope, location FROM memories WHERE name = 'n.md'") == [("personal", "ws-later")]
    repos = [*PROJECT["repos"], {"name": "later", "path": "later"}]
    assert client.put("/v1/projects/demo", json={**PROJECT, "repos": repos}, headers=who[live.ADMIN]).status_code == 200
    assert laptop.run("push").counts["moved"] == 1
    rows = live.sql(hub_db, "SELECT scope, location, deleted FROM memories WHERE name = 'n.md' ORDER BY id")
    assert rows == [("personal", "ws-later", True), ("project", "later", False)]
    assert notes.read_text(encoding="utf-8") == typed("project", "personal until the repo is registered")


@needs_pg
def test_a_hub_failing_midway_keeps_what_was_done(client, who, hub_db, tmp_path, monkeypatch):
    hub = InProcessHub(client, who["alice"])
    laptop = Machine(tmp_path / "laptop", hub, "alice", monkeypatch)
    for name in ("a.md", "b.md", "c.md"):
        laptop.write("ws/demo-harness", name, typed("project", name))
    call = hub.call

    def down_after_two_puts(method, path, body=None):
        if method == "PUT" and sum(1 for m, _ in hub.calls if m == "PUT") >= 2:
            raise HubError("cannot reach https://hub.test: connection refused")
        return call(method, path, body)

    monkeypatch.setattr(hub, "call", down_after_two_puts)
    with pytest.raises(HubError, match="connection refused"):
        laptop.run("push")
    assert sorted(json.loads(laptop.state())["hubs"]["https://hub.test"]["files"]) == [
        f"{slug(laptop.home / 'ws/demo-harness')}/a.md",
        f"{slug(laptop.home / 'ws/demo-harness')}/b.md",
    ]
    monkeypatch.setattr(hub, "call", call)
    hub.calls.clear()
    resumed = laptop.run("push")
    assert (resumed.counts["created"], resumed.counts["unchanged"]) == (1, 2)
    assert [m for m, _ in hub.calls].count("PUT") == 1


def test_one_sync_at_a_time_and_a_state_file_it_cannot_read_stops_it(tmp_path, own_home):
    import fcntl

    directory = own_home / ".evo" / "hub"
    with hub_memory.sync_lock(directory):
        held = os.open(directory / "memory.lock", os.O_RDWR)
        with pytest.raises(BlockingIOError):
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.close(held)
        with pytest.raises(HubError, match="held .*memory.lock for 0.3s"):
            with hub_memory.sync_lock(directory, timeout=0.3):
                pass
    with hub_memory.sync_lock(directory, timeout=0.3):  # released
        pass

    state = directory / "memory-state.json"
    for broken in ("{", '{"version": 99, "hubs": {}}', "[]"):
        state.write_text(broken, encoding="utf-8")
        with pytest.raises(HubError, match="Move it away to start over"):
            hub_memory.State(state, "https://hub.test")
        assert state.read_text(encoding="utf-8") == broken


# The CLI against `hub serve`


def sign_in(home: Path, url: str, login: str, token: str) -> None:
    directory = home / ".evo" / "hub"
    directory.mkdir(parents=True)
    (directory / "token").write_text(token + "\n", encoding="utf-8")
    (directory / "config.json").write_text(json.dumps({"url": url, "login": login}), encoding="utf-8")


def memory_cli(args: list[str], home: Path, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """``evo-agents hub memory ARGS`` as the person whose home is ``home``, in ``cwd`` (default: the home)."""
    env = pg.clean_env(HOME=str(home), CLAUDE_CONFIG_DIR=str(home / ".claude"))
    command = [sys.executable, "-m", "evo_agents", "hub", "memory", *args]
    return subprocess.run(command, env=env, capture_output=True, text=True, timeout=120, cwd=cwd or home)


@pytest.fixture
def team(hub_db, tmp_path):
    """``hub serve`` with project demo; alice on two machines, bob on one, each signed in with a token of their own."""
    with live.running_hub(hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN) as served:
        admin = Hub(served.url, live.insert_token(hub_db, live.ADMIN))
        admin.call("PUT", "/v1/projects/demo", PROJECT)
        homes = {}
        for login, home in (("alice", "alice-laptop"), ("alice", "alice-desktop"), ("bob", "bob-laptop")):
            admin.call("PUT", f"/v1/admin/projects/demo/grants/{login}", {"role": "writer", "max_level": "internal"})
            homes[home] = tmp_path / home
            homes[home].mkdir()
            sign_in(homes[home], served.url, login, live.insert_token(hub_db, login))
        yield served, homes


def files_of(home: Path) -> dict:
    return Machine(home, None, "", None).files()


def write(home: Path, relative: str, name: str, text: str) -> Path:
    return Machine(home, None, "", None).write(relative, name, text)


def memory_dir(home: Path, relative: str) -> Path:
    return Machine(home, None, "", None).dir(relative)


def ok(result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@needs_pg
def test_two_homes_converge_to_the_same_content(team, hub_db):
    served, homes = team
    laptop, desktop, bob = homes["alice-laptop"], homes["alice-desktop"], homes["bob-laptop"]
    write(laptop, "ws/demo-harness", "project.md", typed("project", "dòng một\r\ndòng hai"))
    write(laptop, "ws/demo-harness", "reference.md", typed("reference"))
    write(laptop, "ws/demo-harness", "user.md", typed("user", "only alice"))
    write(laptop, "ws/demo-harness", "feedback.md", typed("feedback", "only alice too"))
    write(laptop, "ws/demo-harness", INDEX, "- [Project](project.md) — written by hand\n")
    write(laptop, "ws/app", "app.md", typed("project", "about the app"))
    write(laptop, "notes/math", "math.md", typed("reference", "personal, whatever the type"))
    write(laptop, ".dotted", "dot.md", typed("user", "under a hidden directory"))

    dry = ok(memory_cli(["push", "--all", "--dry-run", "--json"], laptop))
    report = json.loads(dry.stdout)
    assert_json_keys("hub memory push", report)
    assert report["places"] == {"personal": 2, "project demo": 5} and report["counts"] == {"created": 7}
    assert counts(hub_db)["memories"] == 0
    pushed = ok(memory_cli(["push", "--all"], laptop))
    assert "Pushed to" in pushed.stdout and "7 new" in pushed.stdout
    again = ok(memory_cli(["push", "--all"], laptop))
    assert "7 unchanged" in again.stdout and "new" not in again.stdout

    quiet = ok(memory_cli(["pull", "--all", "--quiet"], desktop))
    assert quiet.stdout == quiet.stderr == ""
    assert files_of(desktop) == files_of(laptop) and len(files_of(laptop)) == 7
    index = (memory_dir(desktop, "ws/demo-harness") / INDEX).read_text(encoding="utf-8")
    assert "- [project memory](project.md) — about project" in index and "(user.md)" in index

    # Changes on the desktop: an edit, a new file, a deletion that is pruned.
    (memory_dir(desktop, "ws/demo-harness") / "project.md").write_text(typed("project", "edited"), encoding="utf-8")
    write(desktop, "ws/demo-harness", "new.md", typed("project", "from the desktop"))
    (memory_dir(desktop, "ws/demo-harness") / "reference.md").unlink()
    ok(memory_cli(["push", "--prune", str(memory_dir(desktop, "ws/demo-harness"))], desktop))
    ok(memory_cli(["pull", "--all", "--prune"], laptop))
    assert files_of(laptop) == files_of(desktop)
    assert not (memory_dir(laptop, "ws/demo-harness") / "reference.md").exists()
    held = (memory_dir(laptop, "ws/demo-harness") / INDEX).read_text(encoding="utf-8")
    assert held.startswith("- [Project](project.md) — written by hand\n") and "(new.md)" in held

    # Another member converges on what members share, and never gets alice's user and feedback memories.
    ok(memory_cli(["pull", "--all"], bob))
    shared = {key: data for key, data in files_of(laptop).items() if key[1] in ("project.md", "new.md", "app.md")}
    assert files_of(bob) == shared
    searched = json.loads(ok(memory_cli(["search", "alice", "--json"], bob)).stdout)
    assert_json_keys("hub memory search", searched)
    assert searched == []
    assert "user.md" in ok(memory_cli(["search", "only alice"], laptop)).stdout


@needs_pg
def test_a_conflict_keeps_both_versions(team):
    served, homes = team
    laptop, desktop = homes["alice-laptop"], homes["alice-desktop"]
    harness = "ws/demo-harness"
    write(laptop, harness, "plan.md", typed("project", "first"))
    write(laptop, harness, "ref.md", typed("reference", "first"))
    ok(memory_cli(["push", "--all"], laptop))
    ok(memory_cli(["pull", "--all"], desktop))

    # Both machines change plan.md; the laptop pushes first.
    (memory_dir(laptop, harness) / "plan.md").write_text(typed("project", "laptop's"), encoding="utf-8")
    session = laptop / "ws" / "demo-harness"
    session.mkdir(parents=True)
    ok(memory_cli(["push", "--quiet"], laptop, cwd=session))  # as the Stop hook runs it, in the session's directory
    (memory_dir(desktop, harness) / "plan.md").write_text(typed("project", "desktop's"), encoding="utf-8")
    pushed = ok(memory_cli(["push", "--all"], desktop))
    copy = memory_dir(desktop, harness) / f"plan.conflict-{host_tag()}.md"
    assert f"conflict: {memory_dir(desktop, harness) / 'plan.md'}" in pushed.stderr and str(copy) in pushed.stderr
    assert (memory_dir(desktop, harness) / "plan.md").read_text(encoding="utf-8") == typed("project", "laptop's")
    assert copy.read_text(encoding="utf-8") == typed("project", "desktop's")
    again = ok(memory_cli(["push", "--all"], desktop))  # the copy waits for a person and is never pushed
    assert "conflict copies wait" in again.stdout and "new" not in again.stdout

    # A pull finds ref.md changed both here and on the hub.
    (memory_dir(laptop, harness) / "ref.md").write_text(typed("reference", "laptop's"), encoding="utf-8")
    ok(memory_cli(["push", "--all"], laptop))
    (memory_dir(desktop, harness) / "ref.md").write_text(typed("reference", "desktop's"), encoding="utf-8")
    pulled = ok(memory_cli(["pull", "--all", "--json"], desktop))
    assert_json_keys("hub memory pull", json.loads(pulled.stdout))
    (conflict,) = json.loads(pulled.stdout)["conflicts"]
    assert conflict["file"] == str(memory_dir(desktop, harness) / "ref.md")
    assert (memory_dir(desktop, harness) / "ref.md").read_text(encoding="utf-8") == typed("reference", "laptop's")
    assert Path(conflict["copy"]).read_text(encoding="utf-8") == typed("reference", "desktop's")
    assert files_of(desktop) == files_of(laptop)  # both machines hold the hub's versions under the names


@needs_pg
def test_the_cli_reports_errors_in_one_line_and_never_prints_a_token(team):
    served, homes = team
    laptop = homes["alice-laptop"]
    big = write(laptop, "ws/demo-harness", "big.md", "x" * (MAX_BODY + 1))
    write(laptop, "ws/demo-harness", "fine.md", typed("project"))
    result = memory_cli(["push", "--all"], laptop)
    assert result.returncode == 1
    assert f"error: {big}: not pushed, larger than 256 KiB" in result.stderr and "1 new" in result.stdout
    token = (laptop / ".evo" / "hub" / "token").read_text(encoding="utf-8").strip()
    assert token not in result.stdout + result.stderr + served.log()
    both = memory_cli(["pull", "--all", str(laptop)], laptop)
    assert both.returncode == 1 and both.stderr.strip() == "error: pass a directory or --all, not both"
    assert "fine.md" not in served.log()


@needs_pg
def test_writers_racing_on_one_memory_create_it_once(team, hub_db):
    from concurrent.futures import ThreadPoolExecutor

    served, homes = team
    token = (homes["alice-laptop"] / ".evo" / "hub" / "token").read_text(encoding="utf-8").strip()

    def create(number: int):
        body = {"scope": "project", "project": "demo", "location": "harness", "name": "race.md", "type": "project"}
        try:
            return Hub(served.url, token).call("PUT", "/v1/memories", {**body, "body": f"writer {number}"})["revision"]
        except HubError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = sorted(pool.map(create, range(8)), key=str)
    assert outcomes == [1] + [409] * 7
    assert live.sql(hub_db, "SELECT count(*) FROM memories")[0][0] == 1
    assert live.sql(hub_db, "SELECT count(*) FROM memory_revisions")[0][0] == 1
    assert [line for line in pg.log_lines(served.log()) if line["level"].lower() in ("error", "critical")] == []
