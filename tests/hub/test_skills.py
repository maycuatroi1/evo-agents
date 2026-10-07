"""Skills on the hub: deterministic bundles and the checks every bundle passes, publishing through the blob store (a
bundle over 10 MiB refused before any URL, a version only for a bundle committed with its SHA-256), who publishes and
who reads, and ``hub skills sync`` into the skills directories of the runtimes and harnesses: links, synced/, learned/
and directories the hub did not write stay as they are, --adopt saves a directory before replacing it, --check writes
and downloads nothing, and a bundle that does not hash to what the hub recorded stops the sync before any write.

No test touches the real ~/.claude, ~/.agents, ~/.codex, ~/.cursor, ~/.gemini or ~/.evo: every test runs with a home
directory of its own, without CLAUDE_CONFIG_DIR or CODEX_HOME. The bundle checks run without Postgres; everything that
needs the hub skips without EVO_HUB_TEST_DSN and gets a database and a moto bucket of its own (``tests.hub.s3``)."""

import gzip
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from evo_agents.hub import skills as hub_skills
from evo_agents.hub.client import HubError
from evo_agents.hub.skill_sync import (
    MANAGED_BEGIN,
    MANAGED_END,
    SkillSync,
    gitignore_text,
    list_skills,
    parse_scope,
    publish,
    sync,
)
from evo_agents.hub.skills import (
    MARKER,
    MAX_BUNDLE,
    BundleError,
    pack,
    read_bundle,
    skill_frontmatter,
    tree_digest,
    write_tree,
)
from tests.hub import live, pg
from tests.hub.contract_keys import assert_json_keys

MiB = 1024 * 1024
LEVELS = ["public", "internal", "customer", "secret"]
PROJECT = {
    "levels": LEVELS,
    "locations": ["any"],
    "default_label": {"level": "internal"},
    "sinks": [{"id": "hub", "kind": "hub", "clearance": {"level": "internal"}}],
    "repos": [],
    "harness": {"name": "demo", "workspace": "~/ws", "path": "demo-harness"},
}
NO_HUB_SINK = {**PROJECT, "sinks": [], "harness": {"name": "plain", "workspace": "~/ws", "path": "plain-harness"}}

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)


@pytest.fixture(autouse=True)
def own_home(tmp_path, monkeypatch) -> Path:
    """A home directory of this test's own, and no runtime variables: nothing can reach the real runtimes."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for variable in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.delenv(variable, raising=False)
    return home


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_skill(base: Path, name: str, body: str = "", files: dict | None = None) -> Path:
    """A skill directory ``base``/``name`` with a SKILL.md naming it and ``files`` (path to text)."""
    directory = base / name
    directory.mkdir(parents=True, exist_ok=True)
    text = f"---\nname: {name}\ndescription: Use when testing {name}: it says so\n---\n{body or name}\n"
    (directory / "SKILL.md").write_text(text, encoding="utf-8")
    for path, content in (files or {}).items():
        (directory / path).parent.mkdir(parents=True, exist_ok=True)
        (directory / path).write_text(content, encoding="utf-8")
    return directory


def tree_hash(root: Path, *, times: bool = True) -> str:
    """Every entry under ``root`` and ``root`` itself: path, type, mode, mtime (with ``times``), the bytes of a file and
    the target of a link."""
    digest = hashlib.sha256()
    paths = [root]
    for directory, dirnames, filenames in os.walk(root):
        dirnames.sort()
        paths += [Path(directory) / name for name in sorted(dirnames + filenames)]
    for path in paths:
        info = os.lstat(path)
        line = f"{path.relative_to(root)}|{stat.S_IFMT(info.st_mode)}|{stat.S_IMODE(info.st_mode)}"
        if times and not stat.S_ISLNK(info.st_mode):
            line += f"|{info.st_mtime_ns}"
        if stat.S_ISLNK(info.st_mode):
            line += f"|{os.readlink(path)}"
        elif stat.S_ISREG(info.st_mode):
            line += f"|{sha(path.read_bytes())}"
        digest.update(line.encode("utf-8", "surrogateescape") + b"\n")
    return digest.hexdigest()


def case_sensitive(directory: Path) -> bool:
    probe = directory / "Case-Probe"
    probe.write_text("x")
    try:
        return not (directory / "case-probe").exists()
    finally:
        probe.unlink()


# Packing


def test_packing_the_same_directory_twice_gives_the_same_sha256(tmp_path):
    files = {"scripts/run.sh": "#!/bin/sh\necho hi\n", "ref/ghi chú.md": "tiếng Việt\n", "z.txt": "z\n"}
    source = write_skill(tmp_path / "a", "demo-skill", files=files)
    os.chmod(source / "scripts" / "run.sh", 0o755)
    first, second = pack(source), pack(source)
    assert first.data == second.data and first.sha256 == second.sha256 == sha(first.data)
    assert first.size == len(first.data) and first.name == "demo-skill"
    assert first.contents.description == "Use when testing demo-skill: it says so"

    # The same files elsewhere, written in another order, at other times, with other permission bits, and next to
    # everything packing leaves out: the same bundle.
    copy = tmp_path / "b" / "demo-skill"
    for path in sorted([*files, "SKILL.md"], reverse=True):
        (copy / path).parent.mkdir(parents=True, exist_ok=True)
        (copy / path).write_bytes((source / path).read_bytes())
        os.chmod(copy / path, 0o700 if path.endswith(".sh") else 0o600)
        os.utime(copy / path, (1_000_000, 1_000_000))
    for path in ("__pycache__/x.cpython-312.pyc", "scripts/__pycache__/y.pyc", "old.pyc", ".DS_Store", "ref/.DS_Store"):
        (copy / path).parent.mkdir(parents=True, exist_ok=True)
        (copy / path).write_bytes(b"left out")
    (copy / ".learned").mkdir()
    (copy / ".learned" / "usage.json").write_text("{}")
    (copy / ".skillfish.json").write_text("{}")
    (copy / MARKER).write_text("{}")
    assert pack(copy).sha256 == first.sha256

    with tarfile.open(fileobj=io.BytesIO(first.data), mode="r:gz") as tar:
        members = tar.getmembers()
    names = [m.name for m in members]
    assert names == sorted(names) == ["SKILL.md", "ref/ghi chú.md", "scripts/run.sh", "z.txt"]
    for m in members:
        assert m.isreg() and m.mtime == 0 and m.uid == m.gid == 0 and m.uname == m.gname == ""
        assert m.mode == (0o755 if m.name == "scripts/run.sh" else 0o644)
    assert first.data[:2] == b"\x1f\x8b" and first.data[4:8] == b"\0\0\0\0"  # gzip, mtime 0
    assert read_bundle(first.data, "demo-skill").tree == first.contents.tree == tree_digest(source)

    os.chmod(copy / "z.txt", 0o755)  # the execute bit is part of the bundle
    assert pack(copy).sha256 != first.sha256
    os.chmod(copy / "z.txt", 0o644)
    (copy / "z.txt").write_text("y\n")
    assert pack(copy).sha256 != first.sha256


def test_packing_refuses_links_odd_names_and_what_is_too_large(tmp_path):
    linked = write_skill(tmp_path, "linked")
    (linked / "elsewhere").symlink_to(tmp_path)
    with pytest.raises(BundleError, match="symbolic link"):
        pack(linked)

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(BundleError, match="has no SKILL.md"):
        pack(empty)
    renamed = write_skill(tmp_path, "renamed")
    renamed.rename(tmp_path / "other-dir")
    with pytest.raises(BundleError, match="names the skill 'renamed' but the directory is 'other-dir'"):
        pack(tmp_path / "other-dir")
    with pytest.raises(BundleError, match="never a skill"):
        pack(write_skill(tmp_path / "x", "synced"))
    no_name = tmp_path / "no-name"
    no_name.mkdir()
    (no_name / "SKILL.md").write_text("# a skill without frontmatter\n")
    with pytest.raises(BundleError, match="no frontmatter name"):
        pack(no_name)
    slashed = write_skill(tmp_path, "slashed", files={"a\\b.md": "x"})
    with pytest.raises(BundleError, match="backslash"):
        pack(slashed)
    if case_sensitive(tmp_path):
        cased = write_skill(tmp_path, "cased", files={"Read.md": "a", "read.md": "b"})
        with pytest.raises(BundleError, match="one path on macOS and Windows"):
            pack(cased)

    big = write_skill(tmp_path, "big")
    (big / "noise.bin").write_bytes(os.urandom(MAX_BUNDLE + 1))  # random bytes do not compress
    with pytest.raises(BundleError, match="over the 10 MiB a skill bundle may have; nothing was sent"):
        pack(big)


def test_frontmatter_is_read_as_the_runtimes_read_it():
    loose = "---\nname: life-cli\ndescription: Personal life: todos, notes.\n  Compact by design.\nversion: 1\n---\nx"
    assert skill_frontmatter(loose) == {
        "name": "life-cli",
        "description": "Personal life: todos, notes. Compact by design.",
        "version": "1",
    }
    assert skill_frontmatter("---\nname: 'quoted'\ndescription: \"it: works\"\n---\n")["description"] == "it: works"
    assert skill_frontmatter("---\nname: a\ndescription: >\n  folded\n  text\n---\n") == {
        "name": "a",
        "description": "folded text\n",
    }
    assert skill_frontmatter("no frontmatter") == {}


# Reading bundles


def entry(name: str, data: bytes | None = b"x", kind=tarfile.REGTYPE, linkname: str = "", **pax) -> tuple:
    info = tarfile.TarInfo(name)
    info.type, info.linkname, info.pax_headers = kind, linkname, pax
    if kind in (tarfile.REGTYPE, tarfile.AREGTYPE):
        info.size = len(data)
        return info, data
    return info, None


def tarball(*entries: tuple, skill: str | None = "evil", gzipped: bool = True) -> bytes:
    """A tar.gz holding a SKILL.md that names ``skill`` (none when None), then ``entries``."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        if skill:
            tar.addfile(*_with(entry("SKILL.md", f"---\nname: {skill}\ndescription: d\n---\n".encode())))
        for info, data in entries:
            tar.addfile(*_with((info, data)))
    return gzip.compress(raw.getvalue(), mtime=0) if gzipped else raw.getvalue()


def _with(pair: tuple) -> tuple:
    info, data = pair
    return info, (io.BytesIO(data) if data is not None else None)


@pytest.mark.parametrize(
    "bundle, problem",
    [
        (tarball(entry("../escape.sh")), "'..' part"),
        (tarball(entry("a/../../escape.sh")), "'..' part"),
        (tarball(entry("/etc/cron.d/evil")), "absolute path"),
        (tarball(entry("a\\..\\b")), "backslash"),
        (tarball(entry("a//b")), "empty"),
        (tarball(entry("ok/\x07bell")), "control character"),
        (tarball(entry(MARKER)), "never holds"),
        (tarball(entry("x/.DS_Store")), "never holds"),
        (tarball(entry("link", kind=tarfile.SYMTYPE, linkname="/etc/passwd")), "not a regular file"),
        (tarball(entry("hard", kind=tarfile.LNKTYPE, linkname="SKILL.md")), "not a regular file"),
        (tarball(entry("dev", kind=tarfile.CHRTYPE)), "not a regular file"),
        (tarball(entry("pipe", kind=tarfile.FIFOTYPE)), "not a regular file"),
        (tarball(entry("Read.md"), entry("read.md")), "one path on macOS and Windows"),
        (tarball(entry("same.md"), entry("same.md")), "one path on macOS and Windows"),
        (tarball(entry("a"), entry("a")), "one path on macOS and Windows"),
        (tarball(entry("d", kind=tarfile.DIRTYPE), entry("d")), "one path on macOS and Windows"),
        (tarball(entry("a"), entry("a/b")), "inside 'a', which is a file"),
        (tarball(entry("dir", kind=tarfile.DIRTYPE), entry("dir/../../x")), "'..' part"),
        (tarball(entry("README.md"), skill=None), "no SKILL.md at its top"),
        (tarball(skill="someone-else"), "names the skill 'someone-else', not 'evil'"),
        (tarball(entry("SKILL.md/x"), skill=None), "no SKILL.md"),
        (b"not gzip at all", "not a gzip-compressed tar"),
        (gzip.compress(b"\x01" * 2048), "not a gzip-compressed tar"),
        (tarball(entry("x.md"))[:-40], "not a gzip-compressed tar"),
        (b"\0" * (MAX_BUNDLE + 1), "over 10 MiB"),
    ],
)
def test_a_bundle_is_refused_whole_for_any_entry_that_could_land_outside_or_mislead(bundle, problem):
    with pytest.raises(BundleError, match=problem):
        read_bundle(bundle, "evil")


def test_reading_is_bounded_in_entries_and_in_bytes(monkeypatch):
    monkeypatch.setattr(hub_skills, "MAX_FILES", 2)
    with pytest.raises(BundleError, match="over 2 entries"):
        read_bundle(tarball(entry("a"), entry("b")), "evil")
    monkeypatch.setattr(hub_skills, "MAX_FILES", 10_000)
    monkeypatch.setattr(hub_skills, "MAX_UNPACKED", MiB)
    with pytest.raises(BundleError, match="over 1 MiB together"):  # declared sizes, before reading them
        read_bundle(tarball(entry("zeros.bin", bytes(2 * MiB))), "evil")
    monkeypatch.setattr(hub_skills, "TAR_OVERHEAD", 0)
    with pytest.raises(BundleError, match="decompresses to over 1 MiB"):  # a header too large to hold
        read_bundle(tarball(entry("x.md", comment="x" * (2 * MiB))), "evil")
    good = read_bundle(tarball(entry("dir", kind=tarfile.DIRTYPE), entry("dir/run.sh", b"#!/bin/sh\n")), "evil")
    assert [e.path for e in good.files] == ["SKILL.md", "dir/run.sh"] and good.name == "evil"


def test_writing_never_follows_a_link_and_never_overwrites(tmp_path):
    contents = read_bundle(tarball(entry("scripts/run.sh", b"#!/bin/sh\n")), "evil")
    outside = tmp_path / "outside"
    outside.mkdir()
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "scripts").symlink_to(outside)
    with pytest.raises(BundleError, match="is not a directory"):
        write_tree(contents, planted)
    assert list(outside.iterdir()) == []
    there = tmp_path / "there"
    there.mkdir()
    (there / "SKILL.md").write_text("held")
    with pytest.raises(FileExistsError):
        write_tree(contents, there)
    assert (there / "SKILL.md").read_text() == "held"
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    write_tree(contents, fresh)
    assert tree_digest(fresh) == contents.tree
    assert stat.S_IMODE(os.stat(fresh / "scripts" / "run.sh").st_mode) == 0o644  # the bundle said 0644


def test_scopes_and_the_managed_gitignore_block():
    assert parse_scope("global") == ("global", None)
    assert parse_scope("project:demo") == ("project", "demo")
    assert parse_scope("project", any_project=True) == ("project", None)
    for bad in ("project", "project:", "project:Demo", "local", "global:x"):
        with pytest.raises(HubError, match="the scope is"):
            parse_scope(bad)
    assert gitignore_text(None, []) is None
    block = f"{MANAGED_BEGIN}\n/a/\n/B/\n{MANAGED_END}\n"
    assert gitignore_text(None, ["B", "a"]) == block
    own = "# the repo's own\n*.log"
    assert gitignore_text(own, ["a", "B"]) == own + "\n" + block
    replaced = f"{MANAGED_BEGIN}\n/c/\n{MANAGED_END}\n"
    assert gitignore_text(own + "\n" + block + "tail\n", ["c"]) == own + "\n" + replaced + "tail\n"
    assert gitignore_text(own + "\n" + block, []) == own + "\n"


# The hub


class InProcessHub:
    """``Hub.call`` through the in-process app, as one member; records the requests."""

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


@pytest.fixture
def hub(hub_db, tmp_path, s3):
    """The hub on its own database and bucket, with project demo (a hub sink up to internal) and plain (no hub sink);
    alice writes in both, carol reads demo, the stranger has no grant, the admin is a hub admin without one."""
    from fastapi.testclient import TestClient

    from evo_agents.hub.config import HubConfig
    from evo_agents.hub.server.app import create_app

    config = HubConfig(
        dsn=hub_db.dsn,
        data_dir=tmp_path / "cache",
        pool_min_size=1,
        pool_max_size=4,
        pool_timeout=5.0,
        admins=frozenset({live.ADMIN}),
        **s3.config(),
    )
    with TestClient(create_app(config)) as client:
        who = {login: live.bearer(live.insert_token(hub_db, login)) for login in (live.ADMIN, "alice", "carol")}
        who["stranger"] = live.bearer(live.insert_token(hub_db, "stranger"))
        for name, body in (("demo", PROJECT), ("plain", NO_HUB_SINK)):
            assert client.put(f"/v1/projects/{name}", json=body, headers=who[live.ADMIN]).status_code == 200
        for login, project, role, level in (
            ("alice", "demo", "writer", "internal"),
            ("alice", "plain", "writer", "internal"),
            ("carol", "demo", "reader", "public"),
        ):
            grant = {"role": role, "max_level": level}
            response = client.put(f"/v1/admin/projects/{project}/grants/{login}", json=grant, headers=who[live.ADMIN])
            assert response.status_code == 200, response.text
        yield SimpleNamespace(
            client=client, db=hub_db, s3=s3, who=who, as_=lambda login: InProcessHub(client, who[login])
        )


def ask(hub, login: str, size: int, sha256: str = "a" * 64, project: str | None = None, kind: str = "skill-bundle"):
    body = {"items": [{"sha256": sha256, "size": size, "kind": kind}]}
    if project:
        body["project"] = project
    return hub.client.post("/v1/blobs/uploads", json=body, headers=hub.who[login])


def committed(hub, login: str, data: bytes, project: str | None = None) -> None:
    """Ask, PUT and commit ``data`` as a skill bundle of ``project`` (the hub's own without one)."""
    from tests.hub.s3 import put_presigned

    asked = ask(hub, login, len(data), sha(data), project)
    assert asked.status_code == 200, asked.text
    for ticket in asked.json()["uploads"]:
        assert put_presigned(ticket["url"], data) == 200
        body = {"upload_ids": [ticket["upload_id"]], **({"project": project} if project else {})}
        response = hub.client.post("/v1/blobs/commit", json=body, headers=hub.who[login])
        assert response.status_code == 200, response.text


def rows(db, table: str) -> int:
    return live.sql(db, f"SELECT count(*) FROM {table}")[0][0]


@needs_pg
def test_a_bundle_of_10_mib_and_one_byte_is_refused_before_any_url_is_issued(hub, tmp_path):
    from evo_agents.hub.blobs import KIND_LIMITS

    assert KIND_LIMITS["skill-bundle"] == MAX_BUNDLE == 10 * MiB
    for login, project in ((live.ADMIN, None), ("alice", "demo")):
        refused = ask(hub, login, MAX_BUNDLE + 1, project=project)
        assert refused.status_code == 413, refused.text
        body = refused.json()
        assert body["error"] == "too_large" and "no upload was issued" in body["message"]
        assert body["detail"] == [
            {"sha256": "a" * 64, "kind": "skill-bundle", "size": MAX_BUNDLE + 1, "limit": MAX_BUNDLE}
        ]
        assert "uploads" not in body and "url" not in refused.text
    assert rows(hub.db, "blob_uploads") == 0 and hub.s3.keys() == []

    # Outside a project the hub holds skill bundles only, for hub admins only.
    assert ask(hub, "alice", 10).status_code == 403
    assert ask(hub, live.ADMIN, 10, kind="kg-blob").status_code == 422
    assert rows(hub.db, "blob_uploads") == 0
    at_limit = ask(hub, live.ADMIN, MAX_BUNDLE)  # the bound is exact
    assert at_limit.status_code == 200 and len(at_limit.json()["uploads"]) == 1

    # The client refuses before it asks the hub anything.
    big = write_skill(tmp_path, "big")
    (big / "noise.bin").write_bytes(os.urandom(MAX_BUNDLE + 1))
    admin = hub.as_(live.ADMIN)
    with pytest.raises(HubError, match="over the 10 MiB"):
        publish(admin, big)
    assert admin.calls == []


@needs_pg
def test_a_version_needs_its_bundle_committed_with_that_sha256(hub, tmp_path):
    from tests.hub.s3 import put_presigned

    bundle = pack(write_skill(tmp_path, "stop-slop", files={"references/phrases.md": "- delve\n"}))
    admin = hub.who[live.ADMIN]
    path = "/v1/skills/global/stop-slop/versions"
    body = {"sha256": bundle.sha256, "size": bundle.size}

    def refused(payload: dict, message: str) -> None:
        response = hub.client.post(path, json=payload, headers=admin)
        assert response.status_code == 422, response.text
        assert message in response.json()["message"]
        assert rows(hub.db, "skill_versions") == rows(hub.db, "skills") == 0

    refused(body, "is not committed for (global)")  # never uploaded

    asked = ask(hub, live.ADMIN, bundle.size, bundle.sha256)
    (ticket,) = asked.json()["uploads"]
    assert put_presigned(ticket["url"], bundle.data) == 200
    refused(body, "is not committed")  # uploaded, not committed

    # Other bytes of the same size: the commit is refused, and the version too.
    (forged,) = ask(hub, live.ADMIN, bundle.size, bundle.sha256).json()["uploads"]
    assert put_presigned(forged["url"], bytes(reversed(bundle.data))) == 200
    commit = hub.client.post("/v1/blobs/commit", json={"upload_ids": [forged["upload_id"]]}, headers=admin)
    assert commit.status_code == 422 and "do not have the declared SHA-256" in commit.text
    refused(body, "is not committed")

    # Committed, but the version names another SHA-256 or size.
    committed(hub, live.ADMIN, bundle.data)
    refused({**body, "sha256": sha(b"another bundle")}, "is not committed")
    refused({**body, "size": bundle.size + 1}, f"has {bundle.size} bytes, not the {bundle.size + 1} declared")

    # A bundle a project committed is not one the hub holds for a global skill.
    other = pack(write_skill(tmp_path / "p", "proj-skill"))
    committed(hub, "alice", other.data, "demo")
    response = hub.client.post(
        "/v1/skills/global/proj-skill/versions", json={"sha256": other.sha256, "size": other.size}, headers=admin
    )
    assert response.status_code == 422 and rows(hub.db, "skill_versions") == 0

    # Committed bytes that are no bundle of this skill: the hub reads them back and refuses.
    evil = tarball(entry("../escape.sh"), skill="stop-slop")
    committed(hub, live.ADMIN, evil)
    refused({"sha256": sha(evil), "size": len(evil)}, "is not a bundle of skill stop-slop")

    source = {"source_repo": "agent-skills", "source_commit": "abc1234"}
    created = hub.client.post(path, json={**body, **source}, headers=admin)
    assert created.status_code == 200, created.text
    answer = created.json()
    assert answer["created"] and answer["scope"] == "global" and answer["project"] is None
    assert answer["latest"]["version"] == 1 and answer["latest"]["published_by"] == live.ADMIN
    assert live.sql(
        hub.db,
        "SELECT name, description, sha256, size, r2_key, source_repo, source_commit FROM skill_versions",
    ) == [
        (
            "stop-slop",
            "Use when testing stop-slop: it says so",
            bundle.sha256,
            bundle.size,
            f"blobs/sha256/{bundle.sha256}",
            "agent-skills",
            "abc1234",
        )
    ]
    audit = [("skill.publish", "skill:global/stop-slop")]
    assert live.sql(hub.db, "SELECT action, target FROM audit WHERE action = 'skill.publish'") == audit
    again = hub.client.post(path, json=body, headers=admin)
    assert again.status_code == 200 and not again.json()["created"] and again.json()["latest"]["version"] == 1
    assert rows(hub.db, "skill_versions") == 1
    assert live.sql(hub.db, "SELECT action, target FROM audit WHERE action = 'skill.publish'") == audit
    dump = live.table_dump(hub.db)
    assert "- delve" not in dump and "references/phrases.md" not in dump  # metadata only, never the bundle


@needs_pg
def test_who_publishes_and_who_may_download_a_project_skill(hub, tmp_path):
    from tests.hub.s3 import get_url

    alice, carol, stranger, admin = (hub.as_(login) for login in ("alice", "carol", "stranger", live.ADMIN))
    notes = write_skill(tmp_path, "team-notes", files={"steps.md": "1. read\n"})
    with pytest.raises(HubError, match="needs a hub admin"):  # refused before anything is sent
        publish(alice, notes)
    assert alice.calls == [("GET", "/v1/auth/whoami")]
    bundle = pack(notes)
    version = {"sha256": bundle.sha256, "size": bundle.size}
    assert (
        hub.client.post("/v1/skills/global/team-notes/versions", json=version, headers=hub.who["alice"]).status_code
        == 403
    )
    with pytest.raises(HubError, match="writer role"):
        publish(carol, notes, "demo")
    with pytest.raises(HubError, match="no sink of kind hub"):
        publish(alice, notes, "plain")
    committed(hub, "alice", bundle.data, "plain")  # the blob route takes it; the version does not
    response = hub.client.post("/v1/skills/projects/plain/team-notes/versions", json=version, headers=hub.who["alice"])
    assert response.status_code == 422 and "no sink of kind hub" in response.json()["message"]
    unknown = hub.client.post("/v1/skills/projects/nope/team-notes/versions", json=version, headers=hub.who["alice"])
    assert unknown.status_code == 403

    published = publish(alice, notes, "demo", "agent-skills", "0123abcd")
    assert published["created"] and published["scope"] == "project" and published["project"] == "demo"
    assert published["bundle"] == {"sha256": bundle.sha256, "size": bundle.size, "files": 2}
    publish(admin, write_skill(tmp_path, "house-style"))

    path = "/v1/skills/projects/demo/team-notes/bundle"
    for login in ("stranger", live.ADMIN):  # no grant on demo, hub admin or not
        refused = hub.client.get(path, headers=hub.who[login])
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"] == "forbidden" and "url" not in refused.json()
        assert hub.client.get("/v1/skills/projects/demo/team-notes", headers=hub.who[login]).status_code == 403
    assert hub.client.get("/v1/skills/projects/nope/team-notes/bundle", headers=hub.who["stranger"]).status_code == 403
    ticket = hub.client.get(path, headers=hub.who["carol"])
    assert ticket.status_code == 200, ticket.text
    ticket = ticket.json()
    assert (ticket["version"], ticket["sha256"], ticket["size"]) == (1, bundle.sha256, bundle.size)
    assert sha(get_url(ticket["url"])) == bundle.sha256
    assert hub.client.get(path + "?version=2", headers=hub.who["carol"]).status_code == 404
    assert hub.client.get("/v1/skills/global/house-style/bundle", headers=hub.who["stranger"]).status_code == 200

    def seen(member, *args) -> list[tuple]:
        return [(s["scope"], s["project"], s["name"]) for s in list_skills(member, *args)]

    assert seen(stranger) == seen(admin) == [("global", None, "house-style")]
    assert seen(carol) == [("global", None, "house-style"), ("project", "demo", "team-notes")]
    assert seen(carol, "global") == [("global", None, "house-style")]
    assert seen(carol, "project", "demo") == [("project", "demo", "team-notes")]
    with pytest.raises(HubError, match="for its members") as refused_list:
        list_skills(stranger, "project", "demo")
    assert refused_list.value.status == 403
    history = hub.client.get("/v1/skills/projects/demo/team-notes", headers=hub.who["carol"]).json()
    assert [v["version"] for v in history["versions"]] == [1]
    assert history["versions"][0]["source_repo"] == "agent-skills"

    # Names are unique ignoring case, and synced or learned name no skill.
    with pytest.raises(HubError, match="differing only in case"):
        publish(alice, write_skill(tmp_path / "cased", "Team-Notes"), "demo")
    reserved = hub.client.post("/v1/skills/global/learned/versions", json=version, headers=hub.who[live.ADMIN])
    assert reserved.status_code == 422 and "never a skill" in reserved.json()["message"]
    assert rows(hub.db, "skill_versions") == 2

    # Each row of the trail is filed under the project it happened in (schema 0007); global skills have none.
    filed = live.sql(
        hub.db,
        "SELECT a.action, a.target, p.name FROM audit a LEFT JOIN projects p ON p.id = a.project_id "
        "WHERE a.action IN ('blob.commit', 'skill.publish') ORDER BY a.id",
    )
    assert filed == [
        ("blob.commit", "plain", "plain"),
        ("blob.commit", "demo", "demo"),
        ("skill.publish", "skill:project/demo/team-notes", "demo"),
        ("blob.commit", "(global)", None),
        ("skill.publish", "skill:global/house-style", None),
        ("blob.commit", "demo", "demo"),  # Team-Notes: the bundle went up, the version was refused
    ]


@needs_pg
def test_the_list_orders_by_scope_then_project_then_name_ignoring_case_with_the_latest_version(hub):
    from sqlalchemy import insert, select

    from evo_agents.hub import tables

    skills, versions = tables.skills, tables.skill_versions
    with live.engine(hub.db).begin() as conn:
        admin = conn.execute(select(tables.users.c.id).where(tables.users.c.login == live.ADMIN)).scalar_one()
        projects = dict(conn.execute(select(tables.projects.c.name, tables.projects.c.id)).all())
        for scope, project, name in (
            ("project", "plain", "beta"),
            ("global", None, "zeta"),
            ("project", "demo", "Gamma"),
            ("global", None, "Alpha"),
            ("project", "demo", "delta"),
        ):
            values = {"scope": scope, "project_id": projects.get(project), "name": name, "created_by": admin}
            skill_id = conn.execute(insert(skills).values(**values).returning(skills.c.id)).scalar_one()
            for number in (1, 2):
                digest = f"{number:064x}"
                version = {"skill_id": skill_id, "version": number, "name": name, "sha256": digest, "size": number}
                conn.execute(insert(versions).values(**version, r2_key=f"blobs/sha256/{digest}", published_by=admin))
    listed = hub.client.get("/v1/skills", headers=hub.who["alice"])
    assert listed.status_code == 200, listed.text
    assert [(s["scope"], s["project"], s["name"], s["version"]) for s in listed.json()] == [
        ("global", None, "Alpha", 2),
        ("global", None, "zeta", 2),
        ("project", "demo", "delta", 2),
        ("project", "demo", "Gamma", 2),
        ("project", "plain", "beta", 2),
    ]


# Sync


def marker(path: Path) -> dict:
    return json.loads((path / MARKER).read_text(encoding="utf-8"))


def actions(report, directory: Path) -> dict[str, str]:
    (target,) = [t for t in report.targets if t["directory"] == str(directory)]
    return {a["name"]: a["action"] for a in target["actions"]}


def runtime_skills(home: Path, runtime: str) -> Path:
    return home / f".{runtime}" / "skills"


@needs_pg
def test_sync_leaves_links_synced_learned_and_directories_it_did_not_write_as_they_are(hub, tmp_path, own_home):
    home = own_home
    admin, carol = hub.as_(live.ADMIN), hub.as_("carol")
    source = tmp_path / "source"
    for name in ("alpha", "beta", "gamma"):
        directory = write_skill(source, name, files={"scripts/run.sh": f"#!/bin/sh\necho {name}\n"})
        os.chmod(directory / "scripts" / "run.sh", 0o755)
        publish(admin, directory)

    claude = runtime_skills(home, "claude")
    claude.mkdir(parents=True)
    (home / ".agents").mkdir()
    (home / ".codex").mkdir()  # the runtime is here; its skills directory is not yet
    elsewhere = tmp_path / "elsewhere"
    write_skill(elsewhere, "alpha", body="the linked alpha")
    write_skill(elsewhere, "linked")
    (claude / "alpha").symlink_to(elsewhere / "alpha")  # a link where a hub skill goes
    (claude / "linked").symlink_to(elsewhere / "linked")
    write_skill(claude / "synced", "from-another-machine")
    write_skill(claude / "learned", "a-learned-skill")
    (claude / ".learned").mkdir()
    (claude / ".learned" / "usage.json").write_text("{}")
    write_skill(claude, "beta", body="my own beta, not the hub's")  # no marker: not the hub's to change
    write_skill(claude, "mine")
    (runtime_skills(home, "codex") / ".system").mkdir(parents=True)
    write_skill(runtime_skills(home, "codex") / ".system", "codex-own")
    protected = [claude / name for name in ("synced", "learned", ".learned", "beta", "mine")]
    protected += [runtime_skills(home, "codex") / ".system", elsewhere]
    before = {path: tree_hash(path) for path in protected}

    report = sync(carol, home=home)
    assert report.errors == [] and report.backup is None
    assert actions(report, claude) == {"alpha": "kept", "beta": "unmanaged", "gamma": "install"}
    for runtime in ("agents", "codex"):
        assert actions(report, runtime_skills(home, runtime)) == dict.fromkeys(("alpha", "beta", "gamma"), "install")
    assert not (home / ".cursor").exists() and not (home / ".gemini").exists()  # runtimes not on this machine
    assert {path: tree_hash(path) for path in protected} == before
    assert os.readlink(claude / "alpha") == str(elsewhere / "alpha")
    assert os.readlink(claude / "linked") == str(elsewhere / "linked")
    assert (claude / "beta" / "SKILL.md").read_text().endswith("my own beta, not the hub's\n")

    for directory in (
        claude / "gamma",
        runtime_skills(home, "codex") / "alpha",
        runtime_skills(home, "agents") / "beta",
    ):
        held = marker(directory)
        assert set(held) == {"name", "scope", "project", "version", "sha256", "tree", "synced_at"}
        assert (held["name"], held["scope"], held["project"], held["version"]) == (directory.name, "global", None, 1)
        assert held["tree"] == tree_digest(directory)
        assert (directory / "scripts" / "run.sh").read_bytes() == (
            source / directory.name / "scripts/run.sh"
        ).read_bytes()
        assert os.access(directory / "scripts" / "run.sh", os.X_OK)
    assert not list(claude.glob(".evo-hub-*"))

    # A second sync finds everything as it left it and writes nothing.
    snapshot = tree_hash(tmp_path)
    again = sync(carol, home=home)
    assert again.counts() == {"kept": 1, "unmanaged": 1, "current": 7} and again.errors == []
    assert again.differences == 1  # beta, which waits for --adopt
    assert tree_hash(tmp_path) == snapshot

    # A new version is installed everywhere it was synced; a link and an unmarked directory still are not touched.
    write_skill(source, "alpha", body="alpha, second version")
    publish(admin, source / "alpha")
    updated = sync(carol, home=home, adopt=False)
    assert actions(updated, claude) == {"alpha": "kept", "beta": "unmanaged", "gamma": "current"}
    assert actions(updated, runtime_skills(home, "codex"))["alpha"] == "update"
    assert marker(runtime_skills(home, "codex") / "alpha")["version"] == 2
    assert "alpha, second version" in (runtime_skills(home, "agents") / "alpha" / "SKILL.md").read_text()
    assert {path: tree_hash(path) for path in protected} == before and updated.backup is None


@needs_pg
def test_adopt_saves_a_directory_before_it_replaces_it(hub, tmp_path, own_home, monkeypatch):
    home = own_home
    publish(hub.as_(live.ADMIN), write_skill(tmp_path / "source", "beta", body="the hub's beta"))
    claude = runtime_skills(home, "claude")
    old = write_skill(claude, "beta", body="the copy skillfish made", files={"notes/old.md": "kept in the backup\n"})
    (old / "notes" / "link").symlink_to("/nonexistent/target")
    original = tree_hash(old)
    content = tree_hash(old, times=False)
    carol = hub.as_("carol")

    plain = sync(carol, home=home)  # without --adopt it is reported and left alone
    assert actions(plain, claude) == {"beta": "unmanaged"} and plain.backup is None
    assert tree_hash(old) == original and not (home / ".evo" / "hub" / "backups").exists()

    # The swap fails after the copy is saved: the copy is in the backups and the directory is as it was.
    def no_space(self, final, staging):
        raise OSError(28, "No space left on device")

    with monkeypatch.context() as patch:  # only this; HOME stays the test's own
        patch.setattr(SkillSync, "_swap", no_space)
        failed = sync(carol, home=home, adopt=True)
    assert failed.errors == [f"{old}: not adopted (No space left on device)"]
    saved = Path(failed.backup) / "global" / "claude" / "beta"
    assert Path(failed.backup).parent == home / ".evo" / "hub" / "backups"
    assert tree_hash(saved, times=False) == content and os.readlink(saved / "notes" / "link") == "/nonexistent/target"
    assert tree_hash(old) == original and not list(claude.glob(".evo-hub-*"))

    adopted = sync(carol, home=home, adopt=True)
    assert adopted.errors == [] and actions(adopted, claude) == {"beta": "adopt"}
    saved = Path(adopted.backup) / "global" / "claude" / "beta"
    assert adopted.backup != failed.backup and tree_hash(saved, times=False) == content
    assert "the hub's beta" in (claude / "beta" / "SKILL.md").read_text()
    assert not (claude / "beta" / "notes").exists() and marker(claude / "beta")["version"] == 1
    assert sync(carol, home=home, adopt=True).differences == 0  # adopted once: it is the hub's now

    # A change made here to a skill the hub wrote is saved, then the hub's version comes back.
    (claude / "beta" / "SKILL.md").write_text("edited here\n")
    restored = sync(carol, home=home)
    assert actions(restored, claude) == {"beta": "restore"}
    assert (Path(restored.backup) / "global" / "claude" / "beta" / "SKILL.md").read_text() == "edited here\n"
    assert "the hub's beta" in (claude / "beta" / "SKILL.md").read_text()


def harness(home: Path) -> Path:
    """The harness of project demo on this machine, where its registration places it (~/ws/demo-harness)."""
    root = home / "ws" / "demo-harness"
    root.mkdir(parents=True)
    (root / "harness.yaml").write_text("name: demo\n")
    return root


@needs_pg
def test_check_changes_no_hash_of_the_tree_and_downloads_nothing(hub, tmp_path, own_home):
    home = own_home
    admin, carol = hub.as_(live.ADMIN), hub.as_("carol")
    source = tmp_path / "source"
    for name in ("alpha", "beta"):
        publish(admin, write_skill(source, name))
    claude = runtime_skills(home, "claude")
    claude.mkdir(parents=True)
    root = harness(home)
    (root / ".claude" / "skills").mkdir(parents=True)
    (root / ".claude" / "skills" / ".gitignore").write_text("*.log\n")
    assert sync(carol, home=home).errors == []

    # Every kind of difference: a newer version, a change made here, a new skill, a directory the hub did not write, a
    # skill the hub no longer lists, a project skill with its .gitignore line.
    write_skill(source, "alpha", body="alpha 2")
    publish(admin, source / "alpha")
    (claude / "beta" / "SKILL.md").write_text("changed here\n")
    publish(admin, write_skill(source, "gamma"))
    publish(admin, write_skill(source, "delta"))
    write_skill(claude, "delta", body="mine")
    retired = write_skill(claude, "retired")
    (retired / MARKER).write_text(json.dumps({**marker(claude / "alpha"), "name": "retired"}))
    publish(hub.as_("alice"), write_skill(source / "p", "team-notes"), "demo")

    hub.s3.stop()  # a check never reaches the blob store
    before = tree_hash(tmp_path)
    report = sync(carol, home=home, check=True, adopt=True)
    assert tree_hash(tmp_path) == before
    assert not (home / ".evo" / "hub" / "backups").exists()
    assert actions(report, claude) == {
        "alpha": "update",
        "beta": "restore",
        "delta": "adopt",
        "gamma": "install",
        "retired": "remove",
    }
    for runtime in ("claude", "agents"):
        assert actions(report, root / f".{runtime}" / "skills") == {"team-notes": "install"}
    gitignores = {t["directory"]: t["gitignore"] for t in report.targets if t["project"]}
    assert set(gitignores.values()) == {"would change"}
    assert report.differences == 5 + 2 + 2 and report.errors == []
    assert report.as_json()["check"] is True

    without_adopt = sync(carol, home=home, check=True)
    assert actions(without_adopt, claude)["delta"] == "unmanaged" and tree_hash(tmp_path) == before

    hub.s3.start()  # same port, same objects
    done = sync(carol, home=home)
    assert done.errors == [] and actions(done, claude)["delta"] == "unmanaged"
    assert (root / ".claude" / "skills" / ".gitignore").read_text() == (
        f"*.log\n{MANAGED_BEGIN}\n/team-notes/\n{MANAGED_END}\n"
    )
    assert (root / ".agents" / "skills" / ".gitignore").read_text() == f"{MANAGED_BEGIN}\n/team-notes/\n{MANAGED_END}\n"
    assert marker(root / ".agents" / "skills" / "team-notes")["project"] == "demo"
    assert not (claude / "team-notes").exists() and not (claude / "retired").exists()
    assert (Path(done.backup) / "global" / "claude" / "retired" / "SKILL.md").is_file()
    assert sync(carol, home=home, check=True).differences == 1  # delta waits for --adopt


@needs_pg
def test_a_downloaded_bundle_with_another_sha256_stops_the_sync_before_any_write(hub, tmp_path, own_home):
    from evo_agents.hub.blobs import blob_key

    home = own_home
    admin, carol = hub.as_(live.ADMIN), hub.as_("carol")
    bundles = {name: pack(write_skill(tmp_path / "source", name)) for name in ("alpha", "beta")}
    for bundle in bundles.values():
        publish(admin, tmp_path / "source" / bundle.name)
    claude = runtime_skills(home, "claude")
    write_skill(claude, "beta", body="mine, replaced only with --adopt")
    alpha = bundles["alpha"]
    key = blob_key(alpha.sha256)
    before = tree_hash(tmp_path)

    for altered in (alpha.data[:-1] + bytes([alpha.data[-1] ^ 1]), alpha.data + b"\0", b""):
        hub.s3.put(key, altered)  # what R2 serves is not what the hub verified
        with pytest.raises(HubError, match="the sync stopped and nothing was written"):
            sync(carol, home=home, adopt=True)
        assert tree_hash(tmp_path) == before
        assert not (home / ".evo").exists()  # no lock, no backups, no state

    # A bundle with the recorded SHA-256 that is no bundle this client writes (the version row put there by hand,
    # past the hub's own check) stops it the same way.
    hub.s3.put(key, alpha.data)
    evil = tarball(entry("../../escape.sh", b"#!/bin/sh\n"), skill="evil")
    committed(hub, live.ADMIN, evil)
    live.sql(
        hub.db,
        "WITH s AS (INSERT INTO skills (scope, name, created_by) SELECT 'global', 'evil', id FROM users "
        "WHERE login = %s RETURNING id, created_by) INSERT INTO skill_versions (skill_id, version, name, sha256, size, "
        "r2_key, published_by) SELECT id, 1, 'evil', %s, %s, %s, created_by FROM s",
        (live.ADMIN, sha(evil), len(evil), f"blobs/sha256/{sha(evil)}"),
    )
    with pytest.raises(HubError, match="not one this client writes"):
        sync(carol, home=home)
    assert tree_hash(tmp_path) == before and not (tmp_path / "escape.sh").exists()
    live.sql(hub.db, "DELETE FROM skills WHERE name = 'evil'")

    report = sync(carol, home=home, adopt=True)
    assert report.errors == [] and actions(report, claude) == {"alpha": "install", "beta": "adopt"}
    assert marker(claude / "alpha")["sha256"] == alpha.sha256


@needs_pg
def test_project_skills_go_into_the_harness_and_only_for_members(hub, tmp_path, own_home):
    home = own_home
    (home / ".claude").mkdir()
    root = harness(home)
    publish(hub.as_("alice"), write_skill(tmp_path / "source", "team-notes"), "demo")

    stranger = sync(hub.as_("stranger"), home=home)
    assert [t["scope"] for t in stranger.targets] == ["global"] and not (root / ".claude").exists()

    report = sync(hub.as_("carol"), home=home, runtimes=("claude",))
    assert report.errors == [] and not (root / ".agents").exists()
    synced = root / ".claude" / "skills" / "team-notes"
    assert marker(synced)["scope"] == "project" and marker(synced)["project"] == "demo"
    assert not (home / ".claude" / "skills" / "team-notes").exists()

    # A skill the hub no longer lists for the project is saved, then removed; the .gitignore follows.
    gone = write_skill(root / ".claude" / "skills", "retired")
    (gone / MARKER).write_text(json.dumps({**marker(synced), "name": "retired"}))
    removed = sync(hub.as_("carol"), home=home, runtimes=("claude",))
    assert actions(removed, root / ".claude" / "skills") == {"team-notes": "current", "retired": "remove"}
    assert (Path(removed.backup) / "project" / "demo" / "claude" / "retired" / "SKILL.md").is_file()
    assert not gone.exists()
    assert (root / ".claude" / "skills" / ".gitignore").read_text() == f"{MANAGED_BEGIN}\n/team-notes/\n{MANAGED_END}\n"


@needs_pg
def test_two_runtimes_linked_to_one_directory_get_each_skill_once(hub, tmp_path, own_home):
    home = own_home
    publish(hub.as_(live.ADMIN), write_skill(tmp_path / "source", "house-style"))
    claude = runtime_skills(home, "claude")
    claude.mkdir(parents=True)
    (home / ".agents").mkdir()
    (home / ".agents" / "skills").symlink_to(claude)  # some machines share one directory between runtimes
    report = sync(hub.as_("carol"), home=home)
    assert report.errors == [] and [t["runtime"] for t in report.targets] == ["claude"]
    assert report.notes == [f"{home / '.agents' / 'skills'} is {claude}, synced once"]
    assert marker(claude / "house-style")["version"] == 1
    assert (home / ".agents" / "skills").is_symlink()


# The CLI against `hub serve`


def sign_in(home: Path, url: str, login: str, token: str) -> None:
    directory = home / ".evo" / "hub"
    directory.mkdir(parents=True)
    (directory / "token").write_text(token + "\n", encoding="utf-8")
    (directory / "config.json").write_text(json.dumps({"url": url, "login": login}), encoding="utf-8")


def skills_cli(args: list[str], home: Path) -> subprocess.CompletedProcess:
    """``evo-agents hub skills ARGS`` as the person whose home is ``home``."""
    env = pg.clean_env(HOME=str(home))
    for variable in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        env.pop(variable, None)
    command = [sys.executable, "-m", "evo_agents", "hub", "skills", *args]
    return subprocess.run(command, env=env, capture_output=True, text=True, timeout=120, cwd=home)


def ok(result: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@needs_pg
def test_the_cli_publishes_lists_and_syncs_against_hub_serve(hub_db, tmp_path, s3):
    with live.running_hub(hub_db, tmp_path, EVO_HUB_ADMINS=live.ADMIN, **s3.env()) as served:
        admin_home, member_home = tmp_path / "admin", tmp_path / "member"
        admin_token, member_token = live.insert_token(hub_db, live.ADMIN), live.insert_token(hub_db, "member")
        sign_in(admin_home, served.url, live.ADMIN, admin_token)
        sign_in(member_home, served.url, "member", member_token)
        (member_home / ".claude").mkdir()
        skill = write_skill(tmp_path / "agent-skills" / "skills", "house-style")

        args = [
            "publish",
            str(skill),
            "--scope",
            "global",
            "--source-repo",
            "agent-skills",
            "--source-commit",
            "abc1234",
        ]
        published = ok(skills_cli(args, admin_home))
        assert published.stdout.startswith("Published global skill house-style version 1 (")
        assert "nothing changed" in ok(skills_cli(args, admin_home)).stdout
        refused = skills_cli(["publish", str(skill)], member_home)
        assert refused.returncode == 1 and refused.stdout == ""
        assert (
            refused.stderr.startswith("error: publishing a global skill needs a hub admin")
            and refused.stderr.count("\n") == 1
        )
        missing = skills_cli(["publish", str(tmp_path / "nowhere")], admin_home)
        assert missing.returncode == 1 and "is not a directory" in missing.stderr

        listed = json.loads(ok(skills_cli(["list", "--scope", "global", "--json"], member_home)).stdout)
        assert_json_keys("hub skills list", listed)
        assert [(s["name"], s["version"], s["source_commit"]) for s in listed] == [("house-style", 1, "abc1234")]
        assert "house-style" in ok(skills_cli(["list"], member_home)).stdout

        assert ok(skills_cli(["sync", "--check", "--quiet"], member_home)).stdout == "1\n"
        assert not (member_home / ".claude" / "skills").exists()
        done = ok(skills_cli(["sync"], member_home))
        assert "1 installed" in done.stdout and str(member_home / ".claude" / "skills") in done.stdout
        checked = ok(skills_cli(["sync", "--check"], member_home))
        assert "0 difference(s)" in checked.stdout
        report = json.loads(ok(skills_cli(["sync", "--check", "--json"], member_home)).stdout)
        assert_json_keys("hub skills sync", report)
        assert report["differences"] == 0 and report["counts"] == {"current": 1}
        bad = skills_cli(["sync", "--runtime", "claude,vim"], member_home)
        assert bad.returncode == 1 and "--runtime takes" in bad.stderr
    log = served.log()
    for secret in (admin_token, member_token, s3.secret_access_key, s3.access_key_id, "X-Amz-Signature"):
        assert secret not in log
    messages = [line["msg"] for line in pg.log_lines(log)]
    assert "skill published" in messages and "skill bundle handed out" in messages


# Schema


@needs_pg
def test_the_schema_holds_bundles_of_global_skills_and_names_unique_ignoring_case(hub_db):
    from psycopg import errors

    from evo_agents.hub.migrate import migrate

    migrate(hub_db.dsn)
    project = live.add_project(hub_db, "alpha")
    with pg.admin(hub_db.admin_dsn) as conn:
        user = conn.execute("SELECT created_by FROM projects WHERE id = %s", (project,)).fetchone()[0]
        insert_blob = "INSERT INTO blobs (project_id, sha256, size, kind) VALUES (%s, %s, 1, %s)"
        conn.execute(insert_blob, (None, "a" * 64, "skill-bundle"))
        conn.execute(insert_blob, (project, "a" * 64, "kg-log"))  # a project may hold the same bytes
        insert_skill = "INSERT INTO skills (scope, project_id, name, created_by) VALUES (%s, %s, %s, %s)"
        conn.execute(insert_skill, ("global", None, "stop-slop", user))
        conn.execute(insert_skill, ("project", project, "Stop-Slop", user))  # another scope
        for statement, params, error in (
            (insert_blob, (None, "a" * 64, "skill-bundle"), errors.UniqueViolation),  # NULLS NOT DISTINCT
            (insert_blob, (None, "b" * 64, "kg-log"), errors.CheckViolation),
            (
                "INSERT INTO blob_uploads (upload_id, project_id, sha256, size, kind, created_by) "
                "VALUES (%s, NULL, %s, 1, 'kg-blob', %s)",
                (str(uuid.uuid4()), "c" * 64, user),
                errors.CheckViolation,
            ),
            (insert_skill, ("global", None, "Stop-Slop", user), errors.UniqueViolation),
            (insert_skill, ("project", project, "stop-SLOP", user), errors.UniqueViolation),
            (insert_skill, ("global", None, "Synced", user), errors.CheckViolation),
            (insert_skill, ("global", None, "learned", user), errors.CheckViolation),
        ):
            with pytest.raises(error):
                conn.execute(statement, params)
