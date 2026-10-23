"""K8 and K12 at the routes (``evo_agents.hub.server.route_autonomy``), as criterion a6 of plan autonomy-levels names
them: a write to a route that keeps credentials (K8: secrets, accounts) or rules (K12: grants, policies, agent profiles,
the org, autonomy), made with the credential of a run (``security.RunScope``, the agent of a run on /mcp), answers 403
when the class is deny for the run's principal, and 403 telling the agent to ask its principal when it is ask; each
refusal names the class, the mode and the source and writes the audit row ``autonomy.refused``. A read is never held,
and neither is a member's own request."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi import Request

from evo_agents.hub.server import security
from tests.hub.test_autonomy_level_lease import autonomy_worker, pref
from tests.hub.test_credentials_api import (  # noqa: F401 (app_key, client, config and hub are fixtures)
    OWNER,
    PROJECT,
    app_key,
    audit_rows,
    client,
    config,
    hub,
    plan_run,
)

SECRET = {"kind": "env", "env_var": "STRIPE_API_KEY", "value": "v", "projects": [PROJECT]}


@pytest.fixture
def as_run(client, hub):
    """The owner at K8 deny and K12 ask, a plan run of theirs claimed; requests made from then on carry its
    credential, as the agent of the run on /mcp does. The run's id."""
    pref(client, hub["owner"], K8="deny", K12="ask")
    run_id = plan_run(client, hub["owner"], autonomy_worker(client, hub["owner"]))
    scope = security.RunScope(run_id=run_id, project=PROJECT, role="writer", max_level="internal")

    def scoped(request: Request) -> security.Principal:
        return replace(security.principal(request), scope=scope)

    client.app.dependency_overrides[security.principal] = scoped
    yield run_id
    client.app.dependency_overrides.pop(security.principal, None)


def test_autonomy_level_routes_k8_deny_refuses_a_runs_write_to_a_secret_with_the_class_mode_and_source(
    client, hub, hub_db, as_run
):
    refused = client.put("/v1/secrets/stripe", json=SECRET, headers=hub["owner"])
    assert refused.status_code == 403, refused.text
    detail = refused.json()["message"]
    assert detail.startswith("PUT /v1/secrets/{name}: K8 (")
    assert "is deny for the run's principal" in detail and f"set by pref:{OWNER}" in detail
    assert "ask_member" not in detail
    ((target, login, _, by_token),) = audit_rows(hub_db, "autonomy.refused")
    assert target == f"run:{as_run} K8 PUT /v1/secrets/{{name}} mode=deny source=pref:{OWNER}"
    assert login == OWNER and by_token
    deleted = client.delete("/v1/secrets/stripe", headers=hub["owner"])
    assert deleted.status_code == 403 and "K8 (" in deleted.json()["message"]


def test_autonomy_level_routes_k12_ask_refuses_a_runs_write_to_a_rule_and_says_to_ask_the_principal(
    client, hub, hub_db, as_run
):
    body = {"level": "A1", "classes": {}}
    refused = client.put("/v1/autonomy/prefs", json=body, headers=hub["owner"])
    assert refused.status_code == 403, refused.text
    detail = refused.json()["message"]
    assert detail.startswith("PUT /v1/autonomy/prefs: K12 (") and "is ask for the run's principal" in detail
    assert f"set by pref:{OWNER}" in detail and "ask your principal with ask_member" in detail
    ((target, *_),) = audit_rows(hub_db, "autonomy.refused")
    assert target == f"run:{as_run} K12 PUT /v1/autonomy/prefs mode=ask source=pref:{OWNER}"


def test_autonomy_level_routes_a_runs_read_is_never_held(client, hub, hub_db, as_run):
    assert client.get("/v1/secrets", headers=hub["owner"]).status_code == 200
    assert client.get("/v1/autonomy", headers=hub["owner"]).status_code == 200
    assert audit_rows(hub_db, "autonomy.refused") == []


def test_autonomy_level_routes_a_members_own_write_is_not_held(client, hub, hub_db):
    pref(client, hub["owner"], K8="deny", K12="ask")
    plan_run(client, hub["owner"], autonomy_worker(client, hub["owner"]))
    written = client.put("/v1/secrets/stripe", json=SECRET, headers=hub["owner"])
    assert written.status_code == 200, written.text
    pref(client, hub["owner"], K8="deny", K12="deny")
    assert audit_rows(hub_db, "autonomy.refused") == []
