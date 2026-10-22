"""What every member reads of the accounts of an agent (``evo_agents.hub.server.agent_account_views``), criterion a5 of
the plan agent-accounts.

Any member of the agent's project, a reader too, reads each account of the agent with its provider, category, state,
workers, weight, priority, end, last usage, runs and their cost over 24 hours and 7 days (null for a service account),
the runs holding it and when a run last took it, computed as a member's own account's are; per agent and in one list
over the projects the reader holds a grant on; with ``hub agent account list|show``. Someone without a grant gets the
404 or 403 of reading the profile; GET /v1/accounts stays the caller's own; no value comes back."""

import json

import httpx
import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import update

from evo_agents.hub import tables
from evo_agents.hub.server.app import create_app
from tests.hub import live
from tests.hub import test_credentials_api as credentials_api
from tests.hub.contract_keys import assert_json_keys
from tests.hub.live import bearer, sql
from tests.hub.test_agent_account_leases import VAR, finish, run_under, worker_of
from tests.hub.test_agent_accounts import added, body, path
from tests.hub.test_credentials_api import PROJECT, leased, sample
from tests.hub.test_run_cli import as_json, ok, write_credentials
from tests.hub.test_run_stream import serving
from tests.hub.test_secrets_cli import run_cli

app_key, config, client = credentials_api.app_key, credentials_api.config, credentials_api.client

ADMIN_GRANT = {"role": "admin", "max_level": "internal"}
READER_GRANT = {"role": "reader", "max_level": "internal"}
SERVICE_VAR = "GITLAB_TOKEN"
READER, READER_ID, STRANGER, STRANGER_ID = "reader", 503, "stranger", 504


def _grant(client, headers, login: str, grant: dict) -> None:
    granted = client.put(f"/v1/admin/projects/{PROJECT}/grants/{login}", json=grant, headers=headers["admin"])
    assert granted.status_code == 200, granted.text


def _members(client, github) -> dict:
    """The members of ``test_credentials_api``, the owner an admin, a reader, a stranger, and the agent @auditor."""
    headers = credentials_api.members(client, github)
    headers["reader"] = bearer(live.sign_in(client, github, READER, READER_ID)["token"])
    headers["stranger"] = bearer(live.sign_in(client, github, STRANGER, STRANGER_ID)["token"])
    _grant(client, headers, "owner", ADMIN_GRANT)
    _grant(client, headers, READER, READER_GRANT)
    made = client.post(
        f"/v1/projects/{PROJECT}/agents", json={"handle": "auditor", "name": "Auditor"}, headers=headers["owner"]
    )
    assert made.status_code == 201, made.text
    return headers


@pytest.fixture
def hub(client, github) -> dict:
    return _members(client, github)


def cost(db, run_id: int, usd: float) -> None:
    sql(db, update(tables.runs).where(tables.runs.c.id == run_id).values(usage={"total_cost_usd": usd}))


def by_name(found: list[dict]) -> dict[str, dict]:
    return {account["name"]: account for account in found}


def test_agent_account_figures_are_read_by_every_member_as_a_members_own_are(client, hub, hub_db):
    worker = worker_of(client, hub["owner"], "box")
    claude, gitlab = sample("agent-claude-"), sample("agent-gitlab-")
    added(client, hub["owner"], "claude-a", body(claude, workers=["box"]))
    added(client, hub["owner"], "gitlab-a", body(gitlab, workers=["box"], env_var=SERVICE_VAR))
    done = run_under(client, hub["owner"], worker)
    assert {lease["name"] for lease in leased(client, worker, done)["leases"]} >= {
        "@auditor:claude-a",
        "@auditor:gitlab-a",
    }
    finish(client, worker, done)
    cost(hub_db, done, 0.75)
    held = run_under(client, hub["owner"], worker)
    leased(client, worker, held)

    response = client.get(path(), headers=hub["reader"])
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    found = by_name(response.json())
    a, g = found["claude-a"], found["gitlab-a"]
    assert (a["provider"], a["category"], a["env_var"], a["workers"]) == ("claude", "model", VAR, ["box"])
    assert (a["state"], a["weight"], a["priority"], a["expires_at"]) == ("ready", 1, 0, None)
    assert (a["runs_24h"], a["runs_7d"], a["cost_24h_usd"], a["cost_7d_usd"]) == (2, 2, 0.75, 0.75)
    assert a["held_by"] == [{"run_id": held, "project": PROJECT, "state": "leased"}]
    assert a["last_leased_at"] is not None and a["last_usage"] is None
    assert (g["provider"], g["category"], g["runs_7d"]) == ("gitlab", "service", 2)
    assert (g["cost_24h_usd"], g["cost_7d_usd"]) == (None, None)
    one = client.get(path("claude-a"), headers=hub["other"])
    assert one.status_code == 200 and one.json() == a, one.text

    # the list over every project the reader holds a grant on: the same accounts, with their project and agent
    listed = client.get("/v1/agent-accounts", headers=hub["reader"])
    assert listed.status_code == 200 and listed.headers["cache-control"] == "no-store", listed.text
    rows = by_name(listed.json())
    assert set(rows) == {"claude-a", "gitlab-a"}
    assert {key: rows["claude-a"][key] for key in a} == a
    assert (rows["claude-a"]["project"], rows["claude-a"]["handle"], rows["claude-a"]["agent_name"]) == (
        PROJECT,
        "auditor",
        "Auditor",
    )
    assert rows["claude-a"]["color"].startswith("#") and rows["claude-a"]["icon_url"] is None
    narrowed = client.get("/v1/agent-accounts", params={"project": PROJECT, "handle": "nobody"}, headers=hub["reader"])
    assert narrowed.status_code == 200 and narrowed.json() == []

    # GET /v1/accounts stays the caller's own: no account of the agent there
    assert client.get("/v1/accounts", headers=hub["owner"]).json() == []
    for text in (claude, gitlab):
        assert text not in response.text and text not in listed.text and text not in one.text


def test_agent_account_is_hidden_from_who_cannot_read_the_agent(client, hub, hub_db):
    worker_of(client, hub["owner"], "box")
    added(client, hub["owner"], "claude-a", body(sample(), workers=["box"]))
    for who, code in (("stranger", 404), ("admin", 403)):
        for url in (path(), path("claude-a")):
            response = client.get(url, headers=hub[who])
            assert response.status_code == code, (who, url, response.text)
        assert client.get("/v1/agent-accounts", headers=hub[who]).json() == []
    assert client.get(path(handle="nobody"), headers=hub["reader"]).status_code == 404
    missing = client.get(path("claude-z"), headers=hub["reader"])
    assert missing.status_code == 404 and "no account named claude-z" in missing.text
    # the default agent and a deleted one have no account
    assert client.get(path(handle=PROJECT), headers=hub["reader"]).json() == []
    assert client.delete(f"/v1/projects/{PROJECT}/agents/auditor", headers=hub["owner"]).status_code == 200
    assert client.get(path(), headers=hub["reader"]).json() == []
    assert client.get("/v1/agent-accounts", headers=hub["reader"]).json() == []


@pytest.fixture
def cli_hub(hub_db, tmp_path, github, app_key):
    """``test_secrets_cli.hub`` with this module's members and a home signed in for the reader."""
    app = create_app(credentials_api.hub_config(hub_db, tmp_path, github, app_key))
    with serving(app) as url, httpx.Client(base_url=url, timeout=10) as client:
        headers = _members(client, github)
        token = headers["reader"]["Authorization"].removeprefix("Bearer ")
        yield client, headers, write_credentials(tmp_path / "reader", url, READER, token)


def test_agent_account_list_and_show_commands_print_figures_and_never_a_value(cli_hub, monkeypatch, capsys):
    client, headers, home = cli_hub
    worker_of(client, headers["owner"], "box")
    value = sample()
    added(client, headers["owner"], "claude-a", body(value, workers=["box"]))

    def account(*args):
        return run_cli(monkeypatch, capsys, home, "hub", "agent", "account", *args)

    listed = as_json(account("list", "--json"))
    assert_json_keys("hub agent account list", listed)
    assert [(row["project"], row["handle"], row["name"]) for row in listed] == [(PROJECT, "auditor", "claude-a")]
    assert as_json(account("list", "@auditor", "--project", PROJECT, "--json")) == listed
    shown = as_json(account("show", "@auditor", "claude-a", "--project", PROJECT, "--json"))
    assert_json_keys("hub agent account show", shown)
    assert (shown["provider"], shown["runs_7d"], shown["held_by"]) == ("claude", 0, [])
    table = ok(account("list")).out
    assert "@auditor" in table and "claude-a" in table and "RUNS 24H/7D" in table
    text = ok(account("show", "auditor", "claude-a", "--project", PROJECT)).out
    assert text.startswith(f"Account claude-a ({VAR}, claude) of @auditor: ready.")
    assert "Runs: 0 in 24 hours ($0.00), 0 in 7 days" in text
    for out in (table, text, json.dumps(listed), json.dumps(shown)):
        assert value not in out
