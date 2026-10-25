"""Skills as the web reads them: a web session lists global and project skills as a machine token does, sees every
version with its SHA-256, size and source, and gets a presigned GET that a browser saves as ``<name>-v<n>.tar.gz``
holding exactly the bytes the version records. Someone without a grant asking for a project bundle gets 403, the
same whether the blob store is configured or not; a member of a hub without a blob store gets 503 naming what to set.

Everything here needs the hub: it skips without EVO_HUB_TEST_DSN, and gets a database and a moto bucket of its own
(``tests.hub.s3``) where it needs a blob store."""

import hashlib
from email.message import Message
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from evo_agents.hub.skills import pack
from tests.hub import live, pg

LEVELS = ["public", "internal", "customer", "secret"]
PROJECT = {
    "levels": LEVELS,
    "locations": ["any"],
    "default_label": {"level": "internal"},
    "sinks": [{"id": "hub", "kind": "hub", "clearance": {"level": "internal"}}],
    "repos": [{"name": "agent-skills", "origin": "https://github.com/example-org/agent-skills", "path": "skills"}],
    "harness": {"name": "demo", "workspace": "~/ws", "path": "demo-harness"},
}
GRANTS = {"alice": ("writer", "internal"), "carol": ("reader", "public")}

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_skill(base: Path, name: str, body: str) -> Path:
    directory = base / name
    directory.mkdir(parents=True)
    text = f"---\nname: {name}\ndescription: Use when testing {name}\n---\n{body}\n"
    (directory / "SKILL.md").write_text(text, encoding="utf-8")
    return directory


def make_hub(hub_db, tmp_path, **store):
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
        **store,
    )
    return TestClient(create_app(config))


def headers_of(hub_db, client) -> dict:
    """``who[login]`` a machine token, ``who[login, "web"]`` a web session; project demo with GRANTS."""
    from evo_agents.hub.server.security import SESSION_COOKIE, WEB

    logins = (live.ADMIN, *GRANTS, "stranger")
    who = {login: live.bearer(live.insert_token(hub_db, login)) for login in logins}
    for login in logins:
        who[login, "web"] = {"Cookie": f"{SESSION_COOKIE}={live.insert_token(hub_db, login, kind=WEB)}"}
    assert client.put("/v1/projects/demo", json=PROJECT, headers=who[live.ADMIN]).status_code == 200
    for login, (role, level) in GRANTS.items():
        grant = {"role": role, "max_level": level}
        response = client.put(f"/v1/admin/projects/demo/grants/{login}", json=grant, headers=who[live.ADMIN])
        assert response.status_code == 200, response.text
    return who


def publish(client, who, login: str, data: bytes, name: str, project: str | None, source=None) -> dict:
    """Upload, commit and publish ``data`` as the next version of skill ``name``, as `hub skills publish` does."""
    from tests.hub.s3 import put_presigned

    holder = {"project": project} if project else {}
    item = {"sha256": sha(data), "size": len(data), "kind": "skill-bundle"}
    asked = client.post("/v1/blobs/uploads", json={**holder, "items": [item]}, headers=who[login])
    assert asked.status_code == 200, asked.text
    for ticket in asked.json()["uploads"]:
        assert put_presigned(ticket["url"], data) == 200
        body = {**holder, "upload_ids": [ticket["upload_id"]]}
        assert client.post("/v1/blobs/commit", json=body, headers=who[login]).status_code == 200
    version = {"sha256": sha(data), "size": len(data)}
    if source:
        version.update(source_repo=source[0], source_commit=source[1])
    path = f"/v1/skills/projects/{project}/{name}/versions" if project else f"/v1/skills/global/{name}/versions"
    response = client.post(path, json=version, headers=who[login])
    assert response.status_code == 200, response.text
    return response.json()


def download(url: str) -> tuple[bytes, Message]:
    import urllib.request

    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read(), response.headers


@needs_pg
def test_a_bundle_downloads_as_a_named_attachment_holding_the_recorded_sha256(hub_db, tmp_path, s3):
    with make_hub(hub_db, tmp_path, **s3.config()) as client:
        who = headers_of(hub_db, client)
        first = pack(write_skill(tmp_path / "v1", "team-notes", "first"))
        second = pack(write_skill(tmp_path / "v2", "team-notes", "second"))
        publish(client, who, "alice", first.data, "team-notes", "demo", ("agent-skills", "0123abcd"))
        publish(client, who, "alice", second.data, "team-notes", "demo", ("example-org/agent-skills", "a" * 40))
        publish(
            client, who, live.ADMIN, pack(write_skill(tmp_path / "g", "house-style", "house")).data, "house-style", None
        )

        # The web session of a reader sees the global skill and the project's, with every version and its source.
        listed = client.get("/v1/skills", headers=who["carol", "web"])
        assert listed.status_code == 200, listed.text
        assert [(s["scope"], s["project"], s["name"], s["version"]) for s in listed.json()] == [
            ("global", None, "house-style", 1),
            ("project", "demo", "team-notes", 2),
        ]
        history = client.get("/v1/skills/projects/demo/team-notes", headers=who["carol", "web"]).json()
        assert [(v["version"], v["sha256"], v["size"]) for v in history["versions"]] == [
            (2, second.sha256, second.size),
            (1, first.sha256, first.size),
        ]
        assert [(v["source_repo"], v["source_commit"]) for v in history["versions"]] == [
            ("example-org/agent-skills", "a" * 40),
            ("agent-skills", "0123abcd"),
        ]

        # The latest by default, any version by number: the bytes hash to what the ticket says, and the store
        # answers as an attachment named for the skill and version.
        for version, bundle in ((None, second), (1, first), (2, second)):
            params = {} if version is None else {"version": version}
            ticket = client.get(
                "/v1/skills/projects/demo/team-notes/bundle", params=params, headers=who["carol", "web"]
            )
            assert ticket.status_code == 200, ticket.text
            ticket = ticket.json()
            assert (ticket["sha256"], ticket["size"]) == (bundle.sha256, bundle.size)
            data, headers = download(ticket["url"])
            assert sha(data) == bundle.sha256 == ticket["sha256"]
            number = version or 2
            assert headers["Content-Disposition"] == f'attachment; filename="team-notes-v{number}.tar.gz"'
            query = parse_qs(urlsplit(ticket["url"]).query)
            assert query["response-content-disposition"] == [f'attachment; filename="team-notes-v{number}.tar.gz"']
        missing = client.get("/v1/skills/projects/demo/team-notes/bundle?version=3", headers=who["carol", "web"])
        assert missing.status_code == 404 and "url" not in missing.json()
        unknown = client.get("/v1/skills/projects/demo/nope/bundle", headers=who["carol", "web"])
        assert unknown.status_code == 404
        data, _ = download(
            client.get("/v1/skills/global/house-style/bundle", headers=who["stranger", "web"]).json()["url"]
        )
        assert data.startswith(b"\x1f\x8b")  # every signed-in member reads a global skill

        # Without a grant, the project's skills, their history and their bundles are a 403, whatever the credential.
        for credential in (who["stranger"], who["stranger", "web"], who[live.ADMIN, "web"]):
            for path in ("/v1/skills/projects/demo/team-notes/bundle", "/v1/skills/projects/demo/team-notes"):
                refused = client.get(path, headers=credential)
                assert refused.status_code == 403, refused.text
                assert refused.json()["error"] == "forbidden" and "url" not in refused.json()
            assert client.get("/v1/skills", params={"project": "demo"}, headers=credential).status_code == 403


@needs_pg
def test_without_a_blob_store_a_stranger_still_gets_403_and_a_member_a_503_naming_what_to_set(hub_db, tmp_path):
    with make_hub(hub_db, tmp_path) as client:
        who = headers_of(hub_db, client)
        # A version written straight into the database: without a blob store nothing can be published.
        from sqlalchemy import insert, select

        from evo_agents.hub import tables

        skills = tables.skills
        with live.engine(hub_db).begin() as conn:
            project_id = conn.execute(select(tables.projects.c.id).where(tables.projects.c.name == "demo")).scalar_one()
            alice = conn.execute(select(tables.users.c.id).where(tables.users.c.login == "alice")).scalar_one()
            skill = {"scope": "project", "project_id": project_id, "name": "team-notes", "created_by": alice}
            skill_id = conn.execute(insert(skills).values(**skill).returning(skills.c.id)).scalar_one()
            version = {"skill_id": skill_id, "version": 1, "name": "team-notes", "description": "notes"}
            conn.execute(
                insert(tables.skill_versions).values(
                    **version, sha256="b" * 64, size=10, r2_key="blobs/sha256/" + "b" * 64, published_by=alice
                )
            )
        path = "/v1/skills/projects/demo/team-notes/bundle"
        for credential in (who["stranger", "web"], who[live.ADMIN, "web"]):
            refused = client.get(path, headers=credential)
            assert refused.status_code == 403, refused.text
        assert client.get("/v1/skills/projects/demo/other/bundle", headers=who["carol", "web"]).status_code == 404
        unavailable = client.get(path, headers=who["carol", "web"])
        assert unavailable.status_code == 503, unavailable.text
        assert unavailable.json()["error"] == "unavailable"
        assert "EVO_HUB_S3_" in unavailable.json()["message"] and "url" not in unavailable.json()
        # The listing and the history need no blob store.
        history = client.get("/v1/skills/projects/demo/team-notes", headers=who["carol", "web"])
        assert history.status_code == 200 and history.json()["versions"][0]["sha256"] == "b" * 64
        health = client.get("/v1/health").json()
        assert health["r2"] == "unconfigured"


def test_a_presigned_get_names_its_attachment_only_with_a_safe_name():
    from datetime import timedelta

    from evo_agents.hub.blobs import BlobStore

    store = BlobStore("http://127.0.0.1:9", "bucket", "AKIDTEST", "secret")
    plain = parse_qs(urlsplit(store.presign_get("c" * 64)).query)
    assert "response-content-disposition" not in plain
    named = parse_qs(urlsplit(store.presign_get("c" * 64, timedelta(seconds=30), filename="a.b_c-v2.tar.gz")).query)
    assert named["response-content-disposition"] == ['attachment; filename="a.b_c-v2.tar.gz"']
    for bad in ("", 'x".tar.gz', "a b", "a/b", "ä", "x" * 201, "a\r\nb"):
        with pytest.raises(ValueError):
            store.presign_get("c" * 64, filename=bad)


def pool_node(client, who, login: str, pool: str) -> dict:
    """The worker headers of a node that joined pool ``pool`` of ``login``, made on project demo."""
    made = client.post("/v1/worker-pools", json={"name": pool, "projects": ["demo"]}, headers=who[login])
    assert made.status_code == 201, made.text
    issued = client.post(f"/v1/worker-pools/{pool}/tokens", json={}, headers=who[login])
    assert issued.status_code == 201, issued.text
    host = {"hostname": f"{login}-box", "os": "linux", "arch": "x86_64", "agent_version": "0.45.0"}
    joined = client.post("/v1/worker/join", json={"token": issued.json()["token"], **host}, headers=WORKER_PROTOCOL)
    assert joined.status_code == 201, joined.text
    return {"Authorization": f"Bearer {joined.json()['token']}", **WORKER_PROTOCOL}


WORKER_PROTOCOL = {"X-Evo-Worker-Protocol": "1"}


@needs_pg
def test_a_pool_node_reads_the_global_skills_of_its_owner_with_its_worker_token_and_nothing_else(hub_db, tmp_path, s3):
    with make_hub(hub_db, tmp_path, session_secret=live.session_secret(), **s3.config()) as client:
        who = headers_of(hub_db, client)
        house = pack(write_skill(tmp_path / "g", "house-style", "house"))
        publish(client, who, live.ADMIN, house.data, "house-style", None)
        secret = pack(write_skill(tmp_path / "s", "admin-only", "admins"))
        publish(client, who, live.ADMIN, secret.data, "admin-only", None)
        audience = client.put(
            "/v1/skills/global/admin-only/audience", json={"audience": "admins"}, headers=who[live.ADMIN]
        )
        assert audience.status_code == 200, audience.text
        publish(
            client, who, "alice", pack(write_skill(tmp_path / "p", "team-notes", "notes")).data, "team-notes", "demo"
        )
        grant = {"role": "writer", "max_level": "internal"}
        given = client.put(f"/v1/admin/projects/demo/grants/{live.ADMIN}", json=grant, headers=who[live.ADMIN])
        assert given.status_code == 200, given.text
        node = pool_node(client, who, "alice", "ci")
        admins_node = pool_node(client, who, live.ADMIN, "ops")

        # the global skills its owner reads, and no project skill: the node has no harness to write them into
        listed = client.get("/v1/worker/skills", headers=node)
        assert listed.status_code == 200, listed.text
        assert [(s["scope"], s["name"], s["version"], s["sha256"]) for s in listed.json()] == [
            ("global", "house-style", 1, house.sha256)
        ]
        names = [s["name"] for s in client.get("/v1/worker/skills", headers=admins_node).json()]
        assert names == ["admin-only", "house-style"]  # the owner is a hub admin: the admins skills too

        ticket = client.get("/v1/worker/skills/global/house-style/bundle", params={"version": 1}, headers=node)
        assert ticket.status_code == 200, ticket.text
        assert (ticket.json()["name"], ticket.json()["sha256"]) == ("house-style", house.sha256)
        data, _ = download(ticket.json()["url"])
        assert sha(data) == house.sha256
        assert client.get("/v1/worker/skills/global/admin-only/bundle", headers=node).status_code == 404
        assert client.get("/v1/worker/skills/global/admin-only/bundle", headers=admins_node).status_code == 200

        # skills alone: the owner's routes of skills, a project skill's bundle and the runs stay closed to it
        assert client.get("/v1/skills", headers=node).status_code == 403
        assert client.get("/v1/worker/skills/projects/demo/team-notes/bundle", headers=node).status_code == 404
        assert client.get("/v1/projects/demo/runs", headers=node).status_code == 403

        # a worker outside a pool syncs nothing with its token
        code = client.post("/v1/workers/pairings", json={"name": "laptop", "projects": ["demo"]}, headers=who["alice"])
        assert code.status_code == 201, code.text
        host = {"hostname": "laptop", "os": "linux", "arch": "x86_64", "agent_version": "0.44.0"}
        paired = client.post("/v1/worker/join", json={"code": code.json()["code"], **host}, headers=WORKER_PROTOCOL)
        assert paired.status_code == 201, paired.text
        lone = {"Authorization": f"Bearer {paired.json()['token']}", **WORKER_PROTOCOL}
        refused = client.get("/v1/worker/skills", headers=lone)
        assert refused.status_code == 403 and "worker pool" in refused.json()["message"]
        assert client.get("/v1/worker/skills/global/house-style/bundle", headers=lone).status_code == 403
