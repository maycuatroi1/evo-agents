"""An app session at a core plugin's write route (``core_plugin_api.routes.callers._from_this_hub``), as step 1 of plan
creative-agentic-workspace names it: a write of an app session (``Principal.app_session``, Bearer) passes without the
Origin of the hub's public URL, since a browser never adds that header to another site's request; a write of the
session cookie still needs that Origin, and a machine token still gets 403 at a route of web sessions.

The route is a plugin's ``WebSession`` route added to the hub of test_app_session_auth.py, whose fixtures register
the app lumen-studio and sign the owner in on the web.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from evo_agents.hub.server import core_plugin_api as hub_api
from tests.hub.live import bearer
from tests.hub.test_app_session_auth import client as client  # noqa: F401  the fixtures
from tests.hub.test_app_session_auth import hub as hub  # noqa: F401
from tests.hub.test_app_session_auth import session

DOC = "/v1/lumen-notes/docs/brief"
ORIGIN = "https://hub.test"


async def put_doc(session: hub_api.WebSession) -> dict:
    return {"login": session.login}


@pytest.fixture
def notes(client):  # noqa: F811
    """The hub with a write route of web sessions, as the docs plugin's PUT of a document is."""
    client.app.router.add_api_route(DOC, put_doc, methods=["PUT"])
    return client


def test_an_app_session_writes_through_a_web_session_route_without_the_hubs_origin(notes, hub):  # noqa: F811
    token = session(notes, hub)["token"]
    for origin in ({}, {"Origin": "https://lumen.example"}, {"Origin": ORIGIN}):
        response = notes.put(DOC, json={}, headers={**bearer(token), **origin})
        assert response.status_code == 200, response.text
        assert response.json()["login"].endswith("lumen-studio")


def test_the_session_cookie_still_needs_the_hubs_origin_and_a_machine_token_still_gets_403(notes, hub):  # noqa: F811
    web = hub["owner_web"]
    refused = notes.put(DOC, json={}, headers=web)
    assert refused.status_code == 403 and "Origin" in refused.json()["message"]
    assert notes.put(DOC, json={}, headers={**web, "Origin": "https://lumen.example"}).status_code == 403
    assert notes.put(DOC, json={}, headers={**web, "Origin": ORIGIN}).status_code == 200
    assert notes.put(DOC, json={}, headers={**hub["owner"], "Origin": ORIGIN}).status_code == 403
