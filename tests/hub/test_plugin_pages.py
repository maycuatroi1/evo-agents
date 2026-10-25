"""Pages of plugins, criterion a10 of the plan hub-agent-plugins on the hub's side: a project's plugins list the pages
each adds to the web; an isolated plugin's page is framed from a URL whose ticket is the credential, made for the
installed bundle, served from it with a CSP that keeps the frame's origin opaque, and only while the project enables
the plugin; a core plugin's page is native and has no URL. What the page reads through the web's bridge (header
X-Evo-Plugin) the hub checks again on each call against what the plugin may do in the project now, and a project's
plugin says that grant. The store says each plugin's name, description and tier, and hub admins read the publishers
the operator trusts.

The sample plugin of tests/fixtures/plugins/sample is published and installed through the store, as the CLI does it
(``tests.hub.test_plugin_store``)."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)
pytest.importorskip("cryptography")

from pathlib import Path

from sqlalchemy import func, update

from evo_agents.hub import tables
from evo_agents.hub.server import plugin_ui
from evo_agents.hub.server.plugin_bridge import PLUGIN_HEADER
from evo_agents.hub.server.plugin_pages import ticket
from evo_agents.hub.server.security import keyed_digest
from evo_agents.plugins.bundle import pack
from tests.hub.test_plugin_store import act, publish, started
from tests.plugins.samples import ed25519_signature

SAMPLE = Path(__file__).resolve().parent.parent / "fixtures" / "plugins" / "sample"
PROJECT, OTHER_PROJECT = "demo", "elsewhere"
SECRET = "pages-test-session-secret-" + "x" * 24
ADMIN_GRANT = {"role": "admin", "max_level": "internal"}
READER_GRANT = {"role": "reader", "max_level": "internal"}


@pytest.fixture
def hub(hub_db, tmp_path, s3):
    """The store's hub with a session secret, the sample plugin installed, and two projects: alice admin of both,
    bob a reader of the first."""
    with started(hub_db, tmp_path, s3) as running:
        running.app.state.config = replace(running.app.state.config, session_secret=SECRET)
        plugin_ui.FILES.held.clear()
        bundle = pack(SAMPLE)
        assert publish(running, bundle, ed25519_signature(bundle.data)).status_code == 200
        assert act(running, "sample", "install").status_code == 200
        running.who["bob"] = live.bearer(live.insert_token(hub_db, "bob"))
        admin = running.who[live.ADMIN]
        for project in (PROJECT, OTHER_PROJECT):
            live.add_project(hub_db, project)
            granted = running.client.put(f"/v1/admin/projects/{project}/grants/alice", json=ADMIN_GRANT, headers=admin)
            assert granted.status_code == 200, granted.text
        granted = running.client.put(f"/v1/admin/projects/{PROJECT}/grants/bob", json=READER_GRANT, headers=admin)
        assert granted.status_code == 200, granted.text
        yield SimpleNamespace(**vars(running), bundle=bundle)


def sample_of(hub, login: str = "alice", project: str = PROJECT) -> dict:
    response = hub.client.get(f"/v1/projects/{project}/plugins/sample", headers=hub.who[login])
    assert response.status_code == 200, response.text
    return response.json()


def enable(hub, project: str = PROJECT) -> None:
    body = {"permissions": ["runs:read"]}
    response = hub.client.post(f"/v1/projects/{project}/plugins/sample/enable", json=body, headers=hub.who["alice"])
    assert response.status_code == 200, response.text


def upgrade_sample(hub, version: str, permissions: list[str]):
    """Publish ``version`` of the sample plugin asking for ``permissions``, upgrade the hub to it; its bundle."""
    directory = hub.tmp / f"sample-{version}"
    shutil.copytree(SAMPLE, directory, ignore=shutil.ignore_patterns("__pycache__"))
    manifest_path = directory / "evo-plugin.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps({**data, "version": version, "permissions": permissions}), encoding="utf-8")
    bundle = pack(directory)
    published = publish(hub, bundle, ed25519_signature(bundle.data))
    assert published.status_code == 200, published.text
    upgraded = act(hub, "sample", "upgrade", version)
    assert upgraded.status_code == 200, upgraded.text
    return bundle


def bridged(hub, path: str, login: str = "alice", plugin: str = "sample", method: str = "GET", **kwargs):
    """A call the web's bridge makes for a page of ``plugin``: the viewer's credential and the header naming it."""
    return hub.client.request(method, path, headers={**hub.who[login], PLUGIN_HEADER: plugin}, **kwargs)


def turn_off(hub, reason: str | None) -> None:
    p = tables.plugins
    at = None if reason is None else func.now()
    live.sql(hub.db, update(p).where(p.c.name == "sample").values(disabled_reason=reason, disabled_at=at))


def test_plugin_page_is_framed_from_its_ticket_only_where_the_project_enabled_it(hub):
    plugin = sample_of(hub, "bob")
    assert (plugin["title"], plugin["tier"]) == ("Sample", "isolated")
    (page,) = plugin["pages"]
    assert page == {
        "name": "board",
        "title": "Sample board",
        "description": "",
        "icon": "kanban",
        "menu": True,
        "native": False,
        "url": None,
        "slot": None,
    }

    enable(hub)
    url = sample_of(hub, "bob")["pages"][0]["url"]
    assert url.startswith(f"/v1/plugin-ui/sample/{PROJECT}/") and url.endswith("/index.html")
    assert sample_of(hub, "alice")["pages"][0]["url"] == url  # the same within the hour, for anyone who reads it

    # no credential of the hub: the ticket in the path is the frame's only one
    page = hub.client.get(url)
    assert page.status_code == 200, page.text
    assert page.headers["content-type"] == "text/html; charset=utf-8"
    csp = page.headers["content-security-policy"]
    for directive in ("sandbox allow-scripts allow-forms", "frame-ancestors 'self'", "connect-src 'none'"):
        assert directive in csp
    assert "allow-same-origin" not in csp
    assert page.headers["x-content-type-options"] == "nosniff" and page.headers["referrer-policy"] == "no-referrer"
    assert '<script src="app.js"></script>' in page.text
    script = hub.client.get(url.replace("index.html", "app.js"))
    assert script.status_code == 200 and script.headers["content-type"].startswith("text/javascript")

    base = url.rsplit("/", 1)[0]
    expires, _, mac = base.rsplit("/", 1)[1].partition(".")
    old = ticket(SECRET, "sample", PROJECT, hub.bundle.sha256, now=time.time() - 2 * 86400)
    refused = [
        f"{base}/missing.html",  # not in the bundle
        f"{base}/evo-plugin.json",  # the bundle's, outside pages/
        f"{base}/index.py",  # a type pages never serve
        url.replace(f"{expires}.{mac}", f"{expires}.{'0' * 32}"),  # a mac of nothing
        url.replace(f"{expires}.{mac}", old),  # run out
        url.replace(f"{expires}.{mac}", "not-a-ticket"),
        url.replace(f"/{PROJECT}/", f"/{OTHER_PROJECT}/"),  # the ticket of another project
        url.replace("/sample/", "/other/"),  # of another plugin
    ]
    for wrong in refused:
        assert hub.client.get(wrong).status_code == 404, wrong
    # the other project's own ticket frames nothing either until it enables the plugin
    elsewhere_ticket = ticket(SECRET, "sample", OTHER_PROJECT, hub.bundle.sha256)
    elsewhere = f"/v1/plugin-ui/sample/{OTHER_PROJECT}/{elsewhere_ticket}/index.html"
    assert hub.client.get(elsewhere).status_code == 404
    assert sample_of(hub, "alice", OTHER_PROJECT)["pages"][0]["url"] is None

    disabled = hub.client.post(f"/v1/projects/{PROJECT}/plugins/sample/disable", headers=hub.who["alice"])
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["pages"][0]["url"] is None
    assert hub.client.get(url).status_code == 404


def test_plugin_page_files_come_from_the_installed_bundle_and_stop_once_it_is_removed(hub):
    enable(hub)
    url = sample_of(hub)["pages"][0]["url"]
    assert hub.client.get(url).status_code == 200
    assert hub.bundle.sha256 in plugin_ui.FILES.held  # read once from the blob store, then kept by its digest
    removed = act(hub, "sample", "remove")
    assert removed.status_code == 200, removed.text
    assert hub.client.get(url).status_code == 404
    listed = hub.client.get(f"/v1/projects/{PROJECT}/plugins", headers=hub.who["alice"]).json()
    assert [plugin["name"] for plugin in listed] == []


def test_plugin_store_says_each_plugin_s_title_description_and_tier(hub):
    (listed,) = hub.client.get("/v1/plugins", headers=hub.who[live.ADMIN]).json()
    assert (listed["title"], listed["tier"]) == ("Sample", "isolated")
    assert listed["description"].startswith("The sample isolated plugin of the evo-agents tests")
    shown = hub.client.get("/v1/plugins/sample", headers=hub.who[live.ADMIN]).json()
    assert shown["manifest"]["contributes"]["pages"][0]["entry"] == "pages/index.html"


def test_plugin_publishers_name_what_signs_for_each_for_hub_admins_only(hub):
    response = hub.client.get("/v1/plugin-publishers", headers=hub.who[live.ADMIN])
    assert response.status_code == 200, response.text
    found = {item["id"]: item for item in response.json()}
    assert list(found) == ["acme", "evo", "beta"]
    assert found["evo"]["core"] and not found["acme"]["core"]
    kinds = sorted(identity.split(":", 1)[0] for identity in found["acme"]["identities"])
    assert kinds == ["ed25519", "sigstore"]
    assert hub.client.get("/v1/plugin-publishers", headers=hub.who["alice"]).status_code == 403
    assert hub.client.get("/v1/plugin-publishers").status_code == 401


def test_plugin_page_ticket_is_made_for_the_installed_bundle_and_an_upgrade_ends_it(hub):
    enable(hub)
    url = sample_of(hub)["pages"][0]["url"]
    expires, _, mac = url.split("/")[5].partition(".")
    # an HMAC under the session secret for the purpose plugin-ui, as the hub signs its other values
    assert mac == keyed_digest(SECRET, "plugin-ui", f"sample\n{PROJECT}\n{hub.bundle.sha256}\n{expires}")[:32]
    assert hub.client.get(url).status_code == 200

    upgrade_sample(hub, "1.1.0", ["runs:read"])
    fresh = sample_of(hub)["pages"][0]["url"]
    assert fresh != url and fresh.startswith(f"/v1/plugin-ui/sample/{PROJECT}/")
    # the ticket made for 1.0.0 serves nothing of 1.1.0, within the hour it was made in too
    assert hub.client.get(url).status_code == 404
    assert hub.client.get(url.replace("index.html", "app.js")).status_code == 404
    assert hub.client.get(fresh).status_code == 200
    assert hub.client.get(fresh.replace("index.html", "app.js")).status_code == 200


def test_plugin_page_reads_through_the_bridge_only_what_the_project_grants_the_plugin_now(hub):
    runs, plans = f"/v1/projects/{PROJECT}/runs", f"/v1/projects/{PROJECT}/plans"
    memories = {"scope": "project", "project": PROJECT}

    # not enabled: the viewer reads the project's runs, a page of the plugin does not
    assert hub.client.get(runs, headers=hub.who["alice"]).status_code == 200
    refused = bridged(hub, runs)
    assert refused.status_code == 403, refused.text
    assert f"plugin sample may not use runs:read in project {PROJECT} now" in refused.json()["message"]

    enable(hub)  # runs:read
    for login in ("alice", "bob"):  # on top of the viewer's own access, a reader's too
        assert bridged(hub, runs, login).status_code == 200
    assert bridged(hub, runs, "bob", params={"limit": 5}).json()["total"] == 0
    assert bridged(hub, f"{runs}/999999").status_code == 404  # past the check, the route's own answer
    assert bridged(hub, plans).status_code == 403  # not granted
    assert bridged(hub, "/v1/memories", params=memories).status_code == 403
    for method, path in (
        ("GET", f"/v1/projects/{PROJECT}/plugins"),
        ("GET", f"{runs}/stats"),
        ("GET", "/v1/auth/whoami"),
        ("POST", f"/v1/projects/{PROJECT}/plugins/sample/disable"),
    ):
        answer = bridged(hub, path, method=method)
        assert answer.status_code == 403, path
        assert "and nothing else of the hub" in answer.json()["message"], path
    assert sample_of(hub)["enabled"] is True  # the refused disable changed nothing
    assert bridged(hub, runs, plugin="other").status_code == 403  # a plugin no project enabled
    assert bridged(hub, runs, plugin="Not_A_Plugin").status_code == 403
    assert bridged(hub, f"/v1/x/sample/{PROJECT}/hello", plugin="other").status_code == 403
    assert bridged(hub, f"/v1/x/sample/{PROJECT}/hello").status_code != 403  # its own route, which checks itself

    # an upgrade that asks for less narrows what the page may use at once, though the table keeps the old grant
    upgrade_sample(hub, "1.1.0", ["plans:read", "memories:read"])
    plugin = sample_of(hub)
    assert (plugin["granted"], plugin["pending"]) == ([], ["plans:read", "memories:read"])
    assert [item["granted"] for item in plugin["permissions"]] == [False, False]
    assert bridged(hub, runs).status_code == 403
    assert bridged(hub, plans).status_code == 403

    body = {"permissions": ["plans:read", "memories:read"]}
    regranted = hub.client.post(f"/v1/projects/{PROJECT}/plugins/sample/enable", json=body, headers=hub.who["alice"])
    assert regranted.status_code == 200, regranted.text
    assert regranted.json()["granted"] == ["plans:read", "memories:read"]
    assert bridged(hub, plans).status_code == 200
    assert bridged(hub, f"{plans}/no-such-plan").status_code == 404
    assert bridged(hub, "/v1/memories", params=memories).status_code == 200
    # the project's memories only, of a project that enabled the plugin
    for params in ({"project": PROJECT}, {"scope": "personal"}, {}, {"scope": "project", "project": OTHER_PROJECT}):
        assert bridged(hub, "/v1/memories", params=params).status_code == 403, params

    # turned off by the hub, then disabled in the project: the page gets nothing, whatever the web still holds
    turn_off(hub, "plugin sample 1.1.0 does not run here")
    assert bridged(hub, plans).status_code == 403
    turn_off(hub, None)
    assert bridged(hub, plans).status_code == 200
    disabled = hub.client.post(f"/v1/projects/{PROJECT}/plugins/sample/disable", headers=hub.who["alice"])
    assert disabled.status_code == 200, disabled.text
    assert bridged(hub, plans).status_code == 403
    assert hub.client.get(plans, headers=hub.who["alice"]).status_code == 200  # the viewer's own read is unchanged
