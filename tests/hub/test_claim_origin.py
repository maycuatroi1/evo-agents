"""The claim of a run carries its origin (``runs.service.profiles.run_origin``): the login and GitHub id of the person
who dispatched it, whom the worker names as co-author of the commits of a run its agent authors."""

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import update

from evo_agents.hub import runs, tables
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import sql
from tests.hub.test_credentials_api import OWNER, OWNER_ID, worker_of
from tests.hub.test_profile_runs import admin, beat, dispatched, profile
from tests.hub.test_runs import claim, report

app_key, config, hub, client = (
    credentials_api.app_key,
    credentials_api.config,
    credentials_api.hub,
    credentials_api.client,
)
__all__ = ["admin"]

FEATURES = list(runs.WORKER_FEATURES)


def test_claim_carries_the_origin_login_and_github_id_for_every_profile(client, admin, hub_db):
    worker = worker_of(client, admin["owner"], "mac-mini")
    beat(client, worker, features=FEATURES)
    plain = dispatched(client, admin["owner"])
    spec = claim(client, worker)
    assert spec["id"] == plain["id"] and spec["own_home"] is False
    assert spec["origin"] == {"login": OWNER, "github_id": OWNER_ID}
    assert report(client, worker, plain["id"], "failed", error="done").status_code == 200
    profile(client, admin["owner"], "evo-sandbox")
    agent_run = dispatched(client, admin["owner"], "lumen-kb-docs", profile="evo-sandbox")
    spec = claim(client, worker)
    assert spec["id"] == agent_run["id"] and spec["own_home"] is True
    assert spec["profile"]["handle"] == "evo-sandbox"
    assert spec["origin"] == {"login": OWNER, "github_id": OWNER_ID}


def test_claim_origin_of_a_person_without_a_github_id_has_a_null_id(client, admin, hub_db):
    worker = worker_of(client, admin["owner"], "mac-mini")
    beat(client, worker, features=FEATURES)
    run = dispatched(client, admin["owner"])
    sql(hub_db, update(tables.users).where(tables.users.c.login == OWNER).values(github_id=None))
    spec = claim(client, worker)
    assert spec["id"] == run["id"] and spec["origin"] == {"login": OWNER, "github_id": None}
