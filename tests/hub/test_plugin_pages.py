"""Pages of plugins, criterion a10 of the plan hub-agent-plugins on the hub's side: a project's plugins list the pages
each adds to the web; an isolated plugin's page is framed from a URL whose ticket is the credential, served from its
signed bundle with a CSP that keeps the frame's origin opaque, and only while the project enables the plugin; a core
plugin's page is native and has no URL. The store says each plugin's name, description and tier, and hub admins read
the publishers the operator trusts.

The sample plugin of tests/fixtures/plugins/sample is published and installed through the store, as the CLI does it
(``tests.hub.test_plugin_store``)."""

from __future__ import annotations

import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.hub import live, pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)
pytest.importorskip("cryptography")

from pathlib import Path

from evo_agents.hub.server import plugin_ui
from evo_agents.hub.server.plugin_pages import ticket
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
    old = ticket(SECRET, "sample", PROJECT, now=time.time() - 2 * 86400)
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
    elsewhere = f"/v1/plugin-ui/sample/{OTHER_PROJECT}/{ticket(SECRET, 'sample', OTHER_PROJECT)}/index.html"
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
