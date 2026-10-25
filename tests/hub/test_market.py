"""The plugin market on the hub (plan plugin-market, criteria a5 and a6): a hub admin adds a market, a GitHub repo
holding ``market.json``; every member reads its plugins with their publisher, permissions, version, price and status;
a hub admin installs a free one from its GitHub release, trusting its publisher's signer once asked, and upgrades it on
a new release; a project admin asks the hub admins for one. Then the publishers hub admins trust.

Everything here needs the hub: it skips without EVO_HUB_TEST_DSN. GitHub is ``MarketGitHub`` below, an
``httpx.MockTransport`` the hub reads markets and releases through (``app.state.market_transport``); Sigstore is
``tests.plugins.samples.FakeSigstore``; the blob store a moto bucket (``tests.hub.s3``)."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from tests.hub import live, pg
from tests.plugins.market_samples import LUMEN_REPO, LUMEN_SIGNER, lumen_release, market_index
from tests.plugins.samples import FakeSigstore, ed25519_publishers

needs_pg = pytest.mark.skipif(not pg.DSN, reason=pg.SKIP_REASON)
pytest.importorskip("cryptography")

MARKET = "northwind/market"
PROJECT = "demo"
# evo is the core publisher (the image's), acme an operator's publisher; lumen is trusted by nobody yet.
PUBLISHERS = f"{ed25519_publishers(publisher='evo')},{ed25519_publishers()}"


class MarketGitHub:
    """GitHub's contents and releases API for the markets and plugin repos of a test, and its asset storage.

    ``files[(repo, path)]`` is a JSON file of a repo's default branch, answered with an ETag and a 304 for it;
    ``releases[repo]`` lists a repo's releases, the latest last (``tests.plugins.market_samples.lumen_release``).
    ``calls`` records (method, path, Authorization header) of each request."""

    def __init__(self):
        self.files: dict[tuple[str, str], dict] = {(MARKET, "market.json"): market_index()}
        self.releases: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str, str | None]] = []

    def transport(self):
        import httpx

        return httpx.MockTransport(self.answer)

    def asset_downloads(self) -> list[str]:
        return [path.removeprefix("/assets/") for _, path, _ in self.calls if path.startswith("/assets/")]

    def answer(self, request):
        import httpx

        path = request.url.path
        self.calls.append((request.method, path, request.headers.get("authorization")))
        if path.startswith("/assets/"):
            name = path.removeprefix("/assets/")
            for releases in self.releases.values():
                for release in releases:
                    if name in release["files"]:
                        return httpx.Response(200, content=release["files"][name])
            return httpx.Response(404, json={"message": "Not Found"})
        parts = path.strip("/").split("/")
        if len(parts) < 5 or parts[0] != "repos":
            return httpx.Response(404, json={"message": "Not Found"})
        repo = f"{parts[1]}/{parts[2]}"
        if parts[3] == "contents":
            data = self.files.get((repo, "/".join(parts[4:])))
            if data is None:
                return httpx.Response(404, json={"message": "Not Found"})
            body = json.dumps(data).encode()
            etag = f'"{hashlib.sha256(body).hexdigest()[:20]}"'
            if request.headers.get("if-none-match") == etag:
                return httpx.Response(304)
            content = {"content": base64.b64encode(body).decode(), "encoding": "base64", "sha": etag.strip('"')}
            return httpx.Response(200, json=content, headers={"ETag": etag})
        if parts[3] == "releases":
            found = self.releases.get(repo) or []
            if parts[4] == "latest":
                release = found[-1] if found else None
            else:
                release = next((item for item in found if item["tag_name"] == parts[-1]), None)
            if release is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"tag_name": release["tag_name"], "assets": release["assets"]})
        return httpx.Response(404, json={"message": "Not Found"})


def make_app(hub_db, tmp_path, s3, github: MarketGitHub, **changes):
    from evo_agents.hub.config import HubConfig
    from evo_agents.hub.plugin_calls import CoreRegistry
    from evo_agents.hub.server.app import create_app
    from evo_agents.plugins.signing import parse_publishers

    config = HubConfig(
        dsn=hub_db.dsn,
        data_dir=tmp_path / "cache",
        pool_min_size=1,
        pool_max_size=4,
        pool_timeout=5.0,
        admins=frozenset({live.ADMIN}),
        plugin_publishers=parse_publishers(PUBLISHERS),
        plugin_core_publishers=frozenset({"evo"}),
        **{**s3.config(), **changes},
    )
    app = create_app(config)
    app.state.plugin_sigstore = FakeSigstore()
    app.state.core_plugins = CoreRegistry(())
    app.state.market_transport = github.transport()
    return app


@contextmanager
def started(hub_db, tmp_path, s3, **changes):
    """A hub reading ``MarketGitHub``, with its admin, alice (a member) and bob; ``changes`` go to its config."""
    from fastapi.testclient import TestClient

    github = MarketGitHub()
    github.releases[LUMEN_REPO] = [lumen_release(tmp_path / "v1", "1.0.0")]
    app = make_app(hub_db, tmp_path, s3, github, **changes)
    with TestClient(app) as client:
        who = {login: live.bearer(live.insert_token(hub_db, login)) for login in (live.ADMIN, "alice", "bob")}
        yield SimpleNamespace(client=client, app=app, db=hub_db, github=github, who=who, tmp=tmp_path)


@pytest.fixture
def hub(hub_db, tmp_path, s3):
    with started(hub_db, tmp_path, s3) as running:
        yield running


def add_market(hub, repo: str = MARKET, **body) -> dict:
    response = hub.client.post("/v1/market/sources", json={"repo": repo, **body}, headers=hub.who[live.ADMIN])
    assert response.status_code == 200, response.text
    return response.json()


def listed(hub, login: str = "alice", **query) -> dict:
    response = hub.client.get("/v1/market", params=query, headers=hub.who[login])
    assert response.status_code == 200, response.text
    return response.json()


def by_id(found: dict) -> dict:
    return {item["id"]: item for item in found["plugins"]}


def install(hub, source: int, plugin: str = "lumen-notes", login: str = live.ADMIN, **body):
    return hub.client.post(f"/v1/market/{source}/{plugin}/install", json=body, headers=hub.who[login])


def actions(db) -> list[str]:
    from sqlalchemy import select

    from evo_agents.hub import tables

    audit = tables.audit
    query = select(audit.c.action, audit.c.target).where(live.not_by_the_image()).order_by(audit.c.id)
    return [f"{action} {target}" for action, target in live.sql(db, query)]


def publisher_rows(db) -> list[tuple]:
    from sqlalchemy import select

    from evo_agents.hub import tables

    p = tables.plugin_publishers
    query = select(p.c.publisher, p.c.signer, p.c.removed_at.is_not(None)).order_by(p.c.id)
    return live.sql(db, query)


def grant(hub, login: str, role: str) -> None:
    body = {"role": role, "max_level": "internal"}
    response = hub.client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=body, headers=hub.who[live.ADMIN])
    assert response.status_code == 200, response.text


# Reading markets


@needs_pg
def test_market_lists_the_plugins_of_a_source_with_their_status_and_filters(hub):
    source = add_market(hub)
    assert (source["repo"], source["name"], source["plugins"], source["error"]) == (MARKET, "northwind-market", 3, None)
    assert source["private"] is False and source["builtin"] is False and source["added_by"] == live.ADMIN

    found = listed(hub)
    plugins = by_id(found)
    assert list(plugins) == ["lumen-notes", "northwind-ledger", "crm"]
    assert found["industries"] == ["education", "sales", "software-outsourcing"]
    assert found["categories"] == ["documents", "sales"]
    assert [(m["repo"], m["title"], m["error"]) for m in found["markets"]] == [(MARKET, "Northwind market", None)]
    notes = plugins["lumen-notes"]
    assert notes["status"] == "not_installed" and notes["market"] == "Northwind market"
    assert notes["publisher"] == {
        "id": "lumen",
        "name": "Lumen Labs",
        "signer": LUMEN_SIGNER,
        "identity": f"sigstore:{LUMEN_REPO}/.github/workflows/ci.yml",
    }
    assert notes["latest"]["version"] == "1.0.0" and notes["latest"]["tag"] == "v1.0.0"
    assert [p["name"] for p in notes["latest"]["permissions"]] == ["runs:read", "chats:write"]
    assert notes["latest"]["permissions"][0]["text"] == "read the project's runs, their events and their outcomes"
    assert plugins["northwind-ledger"]["status"] == "needs_license"
    assert plugins["northwind-ledger"]["price"] == {
        "kind": "subscription",
        "usd_per_hub_month": 49.5,
        "seller": "northwind",
    }
    assert plugins["northwind-ledger"]["latest"] is None
    assert plugins["crm"]["status"] == "not_in_image" and plugins["crm"]["package"] == "evo-agents-core-plugins"

    assert list(by_id(listed(hub, q="meeting NOTES"))) == ["lumen-notes"]
    assert list(by_id(listed(hub, q="northwind"))) == ["northwind-ledger"]
    assert list(by_id(listed(hub, industry="sales"))) == ["crm"]
    assert list(by_id(listed(hub, category="sales"))) == ["northwind-ledger", "crm"]
    assert list(by_id(listed(hub, category="sales", industry="software-outsourcing"))) == ["northwind-ledger"]
    assert by_id(listed(hub, source=source["id"] + 1)) == {}

    detail = hub.client.get(f"/v1/market/{source['id']}/lumen-notes", headers=hub.who["alice"])
    assert detail.status_code == 200, detail.text
    assert detail.json()["trusted"] is False and detail.json()["may_trust"] is True
    assert detail.json()["homepage"] == f"https://github.com/{LUMEN_REPO}" and detail.json()["license"] == "Apache-2.0"
    crm = hub.client.get(f"/v1/market/{source['id']}/crm", headers=hub.who["alice"]).json()
    assert crm["may_trust"] is False  # evo is this hub's core publisher
    missing = hub.client.get(f"/v1/market/{source['id']}/nothing", headers=hub.who["alice"])
    assert missing.status_code == 404 and "lists no plugin nothing" in missing.json()["message"]
    assert hub.client.get("/v1/market").status_code == 401


def test_market_status_of_a_core_plugin_in_the_image_and_of_an_upgrade():
    pytest.importorskip("fastapi")  # evo_agents.hub.server.market is a router of the hub-server extra
    from evo_agents.hub.server.market import status_of

    crm = market_index()["plugins"][2]
    notes = market_index()["plugins"][0]
    assert status_of(crm, None, {"image": True, "installed_version": "1.3.0", "publisher": "evo"}) == (
        "in_image",
        "1.3.0",
    )
    assert status_of(crm, None, None) == ("not_in_image", None)
    held = {"image": False, "installed_version": "1.0.0", "publisher": "lumen"}
    assert status_of(notes, {"tag": "v1.2.0", "version": "1.2.0"}, held) == ("upgrade", "1.0.0")
    assert status_of(notes, {"tag": "v1.0.0", "version": "1.0.0"}, held) == ("installed", "1.0.0")
    # A plugin of that name by another publisher is not this entry installed.
    assert status_of(notes, {"version": "1.0.0"}, {**held, "publisher": "acme"}) == ("not_installed", None)
    assert status_of(notes, {"tag": "v1.0.0", "error": "no bundle"}, None) == ("unavailable", None)


@needs_pg
def test_market_refresh_reads_an_unchanged_index_and_release_once(hub):
    source = add_market(hub)
    assert hub.github.asset_downloads() == ["lumen-notes-1.0.0.tar.gz"]
    refreshed = hub.client.post(f"/v1/market/sources/{source['id']}/refresh", headers=hub.who[live.ADMIN])
    assert refreshed.status_code == 200, refreshed.text
    contents = [call for call in hub.github.calls if "/contents/" in call[1]]
    assert len(contents) == 2
    assert hub.github.asset_downloads() == ["lumen-notes-1.0.0.tar.gz"]  # the tag did not change

    hub.github.releases[LUMEN_REPO].append(lumen_release(hub.tmp / "v2", "1.1.0"))
    index = market_index(title="Northwind plugins")
    hub.github.files[(MARKET, "market.json")] = index
    hub.client.post(f"/v1/market/sources/{source['id']}/refresh", headers=hub.who[live.ADMIN])
    notes = by_id(listed(hub))["lumen-notes"]
    assert notes["latest"]["version"] == "1.1.0" and notes["market"] == "Northwind plugins"


@needs_pg
def test_market_source_that_fails_keeps_its_error_and_its_last_index(hub):
    source = add_market(hub)
    broken = add_market(hub, "northwind/missing")
    assert broken["plugins"] == 0 and "has no market.json at main" in broken["error"]

    hub.github.files[(MARKET, "market.json")] = {"schema": "plugin-market-v1"}
    again = hub.client.post(f"/v1/market/sources/{source['id']}/refresh", headers=hub.who[live.ADMIN]).json()
    assert again["error"].startswith("the index is not plugin-market-v1") and again["plugins"] == 3
    found = listed(hub)
    assert list(by_id(found)) == ["lumen-notes", "northwind-ledger", "crm"]
    assert [m["error"] is None for m in found["markets"]] == [False, False]

    hub.github.releases[LUMEN_REPO] = []
    hub.github.files[(MARKET, "market.json")] = market_index()
    hub.client.post(f"/v1/market/sources/{source['id']}/refresh", headers=hub.who[live.ADMIN])
    notes = by_id(listed(hub))["lumen-notes"]
    assert notes["status"] == "unavailable" and notes["latest"]["error"] == f"{LUMEN_REPO} has no release"


@needs_pg
def test_market_sources_are_for_hub_admins_and_the_default_one_stays(hub_db, tmp_path, s3):
    with started(hub_db, tmp_path, s3, market_default_source=MARKET) as hub:
        assert hub.client.get("/v1/market/sources", headers=hub.who["alice"]).status_code == 403
        assert hub.client.post("/v1/market/sources", json={"repo": "a/b"}, headers=hub.who["alice"]).status_code == 403
        held = hub.client.get("/v1/market/sources", headers=hub.who[live.ADMIN]).json()
        assert held["default_source"] == MARKET and held["publishers_locked"] is False
        (builtin,) = held["sources"]
        assert builtin["builtin"] is True and builtin["fetched_at"] is None and builtin["added_by"] is None
        refreshed = hub.client.post(f"/v1/market/sources/{builtin['id']}/refresh", headers=hub.who[live.ADMIN])
        assert refreshed.json()["plugins"] == 3
        refused = hub.client.delete(f"/v1/market/sources/{builtin['id']}", headers=hub.who[live.ADMIN])
        assert refused.status_code == 409 and "EVO_HUB_MARKET_DEFAULT_SOURCE" in refused.json()["message"]

        hub.github.files[("lumen-labs/market", "plugins/index.json")] = market_index(name="lumen-market")
        other = add_market(hub, "lumen-labs/market", path="plugins/index.json")
        assert other["name"] == "lumen-market"
        twice = hub.client.post(
            "/v1/market/sources",
            json={"repo": "lumen-labs/market", "path": "plugins/index.json"},
            headers=hub.who[live.ADMIN],
        )
        assert twice.status_code == 409
        removed = hub.client.delete(f"/v1/market/sources/{other['id']}", headers=hub.who[live.ADMIN])
        assert removed.status_code == 200 and [s["repo"] for s in removed.json()["sources"]] == [MARKET]
        assert actions(hub.db) == [
            "market.source.add market:lumen-labs/market",
            "market.source.remove market:lumen-labs/market",
        ]
        assert hub.client.delete("/v1/market/sources/999", headers=hub.who[live.ADMIN]).status_code == 404
    # The operator empties the variable: the default market goes with its next read.
    with started(hub_db, tmp_path, s3, market_default_source=None) as hub:
        assert hub.client.get("/v1/market/sources", headers=hub.who[live.ADMIN]).json()["sources"] == []


@needs_pg
def test_market_private_source_token_is_sealed_and_sent_to_github_only(hub_db, tmp_path, s3):
    token = "gho_" + secrets.token_hex(16)
    with started(hub_db, tmp_path, s3) as hub:
        refused = hub.client.post(
            "/v1/market/sources", json={"repo": MARKET, "token": token}, headers=hub.who[live.ADMIN]
        )
        assert refused.status_code == 503 and "EVO_HUB_SECRETS_KEY" in refused.json()["message"]
    with started(hub_db, tmp_path, s3, secrets_key=secrets.token_bytes(32)) as hub:
        source = add_market(hub, token=token)
        assert source["private"] is True and token not in json.dumps(source)
        from evo_agents.hub import tables

        (stored,) = live.sql(hub.db, tables.market_sources.select().with_only_columns(tables.market_sources.c.token))
        assert token not in json.dumps(stored[0]) and set(stored[0]) == {"ciphertext", "nonce", "key_id"}
        sent = {auth for _, path, auth in hub.github.calls if "/contents/" in path or "/releases/" in path}
        assert sent == {f"Bearer {token}"}
        listed_sources = hub.client.get("/v1/market/sources", headers=hub.who[live.ADMIN]).text
        assert token not in listed_sources


# Installing


@needs_pg
def test_market_install_asks_before_trusting_a_publisher_then_installs_and_upgrades(hub):
    source = add_market(hub)["id"]
    asked = install(hub, source)
    assert asked.status_code == 409, asked.text
    assert asked.json()["message"].startswith(f"untrusted publisher: this hub does not trust {LUMEN_SIGNER}")
    assert publisher_rows(hub.db) == []
    assert hub.client.get("/v1/plugins/lumen-notes", headers=hub.who[live.ADMIN]).status_code == 404
    assert install(hub, source, login="alice", trust_publisher=True).status_code == 403

    done = install(hub, source, trust_publisher=True)
    assert done.status_code == 200, done.text
    assert done.json()["trusted_publisher"] is True and done.json()["upgraded"] is False
    assert done.json()["plugin"]["installed_version"] == "1.0.0" and done.json()["plugin"]["publisher"] == "lumen"
    assert publisher_rows(hub.db) == [("lumen", LUMEN_SIGNER, False)]
    trail = actions(hub.db)
    assert f"plugin.publisher.add publisher:lumen signer:{LUMEN_SIGNER}" in trail
    assert any(line.startswith("plugin.install ") and "lumen-notes" in line for line in trail)
    assert by_id(listed(hub))["lumen-notes"]["status"] == "installed"
    detail = hub.client.get(f"/v1/market/{source}/lumen-notes", headers=hub.who["alice"]).json()
    assert detail["trusted"] is True and detail["installed_version"] == "1.0.0"

    hub.github.releases[LUMEN_REPO].append(lumen_release(hub.tmp / "v2", "1.1.0"))
    hub.client.post(f"/v1/market/sources/{source}/refresh", headers=hub.who[live.ADMIN])
    assert by_id(listed(hub))["lumen-notes"]["status"] == "upgrade"
    upgraded = install(hub, source)  # the signer is trusted now: no question
    assert upgraded.status_code == 200, upgraded.text
    assert upgraded.json()["upgraded"] is True and upgraded.json()["trusted_publisher"] is False
    assert upgraded.json()["plugin"]["installed_version"] == "1.1.0"
    assert any(line.startswith("plugin.upgrade ") and "lumen-notes" in line for line in actions(hub.db))
    older = install(hub, source, version="1.0.0")
    assert older.status_code == 409 and "above 1.0.0" in older.json()["message"]


@needs_pg
def test_market_install_refuses_a_release_another_workflow_signed(hub):
    source = add_market(hub)["id"]
    other = "https://github.com/northwind/fork/.github/workflows/ci.yml@refs/tags/v1.0.0"
    hub.github.releases[LUMEN_REPO] = [lumen_release(hub.tmp / "forged", "1.0.0", identity=other)]
    refused = install(hub, source, trust_publisher=True)
    assert refused.status_code == 422, refused.text
    assert refused.json()["message"].startswith("refused (") and "nothing was installed" in refused.json()["message"]
    assert publisher_rows(hub.db) == []
    assert hub.client.get("/v1/plugins/lumen-notes", headers=hub.who[live.ADMIN]).status_code == 404


@needs_pg
def test_market_install_refuses_core_and_paid_plugins(hub):
    source = add_market(hub)["id"]
    core = install(hub, source, "crm", trust_publisher=True)
    assert core.status_code == 409 and "core plugin" in core.json()["message"]
    paid = install(hub, source, "northwind-ledger", trust_publisher=True)
    assert paid.status_code == 409 and "licence" in paid.json()["message"]


@needs_pg
def test_market_locked_publishers_refuse_a_new_signer(hub_db, tmp_path, s3):
    with started(hub_db, tmp_path, s3, plugin_publishers_locked=True) as hub:
        source = add_market(hub)["id"]
        refused = install(hub, source, trust_publisher=True)
        assert refused.status_code == 409 and "EVO_HUB_PLUGIN_PUBLISHERS_LOCKED" in refused.json()["message"]
        assert hub.client.get(f"/v1/market/{source}/lumen-notes", headers=hub.who["alice"]).json()["may_trust"] is False
        body = {"publisher": "lumen", "signer": LUMEN_SIGNER}
        assert hub.client.post("/v1/plugin-publishers", json=body, headers=hub.who[live.ADMIN]).status_code == 409
        assert hub.client.get("/v1/market/sources", headers=hub.who[live.ADMIN]).json()["publishers_locked"] is True
        assert publisher_rows(hub.db) == []


@needs_pg
def test_market_install_from_a_github_release(hub):
    body = {"repo": LUMEN_REPO, "tag": "v1.0.0"}
    assert hub.client.post("/v1/plugins/install-github", json=body, headers=hub.who["alice"]).status_code == 403
    asked = hub.client.post("/v1/plugins/install-github", json=body, headers=hub.who[live.ADMIN])
    assert asked.status_code == 409 and "untrusted publisher" in asked.json()["message"]
    done = hub.client.post(
        "/v1/plugins/install-github", json={**body, "trust_publisher": True}, headers=hub.who[live.ADMIN]
    )
    assert done.status_code == 200, done.text
    assert done.json()["plugin"]["name"] == "lumen-notes" and done.json()["plugin"]["installed_version"] == "1.0.0"
    assert publisher_rows(hub.db) == [("lumen", LUMEN_SIGNER, False)]

    wrong = {"repo": LUMEN_REPO, "signer": "sigstore:northwind/fork/.github/workflows/ci.yml", "trust_publisher": True}
    refused = hub.client.post("/v1/plugins/install-github", json=wrong, headers=hub.who[live.ADMIN])
    assert refused.status_code == 422 and refused.json()["message"].startswith("refused (")
    missing = hub.client.post(
        "/v1/plugins/install-github", json={"repo": "lumen-labs/none"}, headers=hub.who[live.ADMIN]
    )
    assert missing.status_code == 422 and "has no release" in missing.json()["message"]


# Asking


@needs_pg
def test_market_request_of_a_project_admin_notifies_the_hub_admins(hub):
    from sqlalchemy import select

    from evo_agents.hub import tables

    live.add_project(hub.db, PROJECT)
    grant(hub, "alice", "admin")
    grant(hub, "bob", "writer")
    source = add_market(hub)["id"]
    url = f"/v1/market/{source}/lumen-notes/request"
    refused = hub.client.post(url, json={"project": PROJECT}, headers=hub.who["bob"])
    assert refused.status_code == 403 and "needs the admin role" in refused.json()["message"]
    asked = hub.client.post(url, json={"project": PROJECT}, headers=hub.who["alice"])
    assert asked.status_code == 200, asked.text
    assert asked.json() | {"id": 0} == {
        "id": 0,
        "project": PROJECT,
        "plugin": "lumen-notes",
        "state": "open",
        "created": True,
    }
    again = hub.client.post(url, json={"project": PROJECT}, headers=hub.who["alice"])
    assert again.json()["created"] is False and again.json()["id"] == asked.json()["id"]

    n, u = tables.notifications, tables.users
    query = (
        select(u.c.login, n.c.title, n.c.link, n.c.details)
        .join_from(n, u, u.c.id == n.c.user_id)
        .where(n.c.notice_kind == "market_install_request")
    )
    (notice,) = live.sql(hub.db, query)
    assert notice[0] == live.ADMIN and notice[1] == f"alice asks for Lumen notes in {PROJECT}"
    assert notice[2] == f"/market/{source}/lumen-notes"
    assert notice[3] == {"source": source, "plugin": "lumen-notes", "project": PROJECT}
    assert f"market.request {PROJECT}/market:{source}/lumen-notes" in actions(hub.db)
    # The hub admin holds no grant on the project, and reads the notice in the Inbox all the same.
    inbox = hub.client.get("/v1/me/notifications", headers=hub.who[live.ADMIN]).json()["notifications"]
    assert [item["title"] for item in inbox if item["notice_kind"] == "market_install_request"] == [notice[1]]

    install(hub, source, trust_publisher=True)
    r = tables.market_install_requests
    assert live.sql(hub.db, select(r.c.state)) == [("installed",)]


# Publishers


@needs_pg
def test_publishers_hub_admins_trust_are_listed_and_removed(hub):
    assert hub.client.get("/v1/plugin-publishers", headers=hub.who["alice"]).status_code == 403
    body = {"publisher": "lumen", "signer": LUMEN_SIGNER, "note": "the notes plugin"}
    assert hub.client.post("/v1/plugin-publishers", json=body, headers=hub.who["alice"]).status_code == 403
    added = hub.client.post("/v1/plugin-publishers", json=body, headers=hub.who[live.ADMIN])
    assert added.status_code == 200, added.text
    listed_publishers = {item["id"]: item for item in added.json()}
    assert list(listed_publishers) == ["evo", "acme", "lumen"]
    lumen = listed_publishers["lumen"]
    assert lumen["operator"] is False and lumen["core"] is False
    assert lumen["identities"] == [f"sigstore:{LUMEN_REPO}/.github/workflows/ci.yml"]
    (signer,) = lumen["trusted"]
    assert (signer["signer"], signer["added_by"], signer["note"]) == (LUMEN_SIGNER, live.ADMIN, "the notes plugin")
    assert listed_publishers["evo"]["operator"] is True and listed_publishers["evo"]["core"] is True
    again = hub.client.post("/v1/plugin-publishers", json=body, headers=hub.who[live.ADMIN])
    assert len(again.json()[2]["trusted"]) == 1

    core = hub.client.post(
        "/v1/plugin-publishers", json={"publisher": "evo", "signer": LUMEN_SIGNER}, headers=hub.who[live.ADMIN]
    )
    assert core.status_code == 409 and "core publisher" in core.json()["message"]
    bad = hub.client.post(
        "/v1/plugin-publishers", json={"publisher": "lumen", "signer": "sigstore:nope"}, headers=hub.who[live.ADMIN]
    )
    assert bad.status_code == 422

    removed = hub.client.delete(f"/v1/plugin-publishers/{signer['id']}", headers=hub.who[live.ADMIN])
    assert removed.status_code == 200 and [item["id"] for item in removed.json()] == ["evo", "acme"]
    assert hub.client.delete(f"/v1/plugin-publishers/{signer['id']}", headers=hub.who[live.ADMIN]).status_code == 404
    assert publisher_rows(hub.db) == [("lumen", LUMEN_SIGNER, True)]
    assert actions(hub.db) == [
        f"plugin.publisher.add publisher:lumen signer:{LUMEN_SIGNER}",
        f"plugin.publisher.remove publisher:lumen signer:{LUMEN_SIGNER}",
    ]
