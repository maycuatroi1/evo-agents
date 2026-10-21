"""The accounts a run of the Curator takes (``docs/credentials.md``, section "Accounts"): only those its charter lists,
by the strategy of its owner's policy, and the same one on every ask; an account bound to the worker that the charter
does not list stays out."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.hub import tables
from tests.hub import test_credentials_api as credentials_api
from tests.hub import test_curator_changes as curator_changes
from tests.hub.live import sql
from tests.hub.test_credentials_api import env_secret, leased, sample
from tests.hub.test_curator import THE_NIGHT, written
from tests.hub.test_review_runs import second_review
from tests.hub.test_runs import report

# the fixtures of test_curator_changes, as fixtures of this module
app_key, world = credentials_api.app_key, curator_changes.world

VAR = "CLAUDE_CODE_OAUTH_TOKEN"


def taken(world, run_id: int) -> tuple[str, str]:
    """The account run ``run_id`` got for VAR and how the hub chose it."""
    names = [
        item["name"] for item in leased(world.client, world.worker, run_id)["leases"] if item.get("env_var") == VAR
    ]
    ra = tables.run_accounts
    (how,) = sql(world.db, select(ra.c.how).where(ra.c.run_id == run_id, ra.c.env_var == VAR))[0]
    (name,) = names
    return name, how


def test_a_curator_run_takes_the_accounts_of_its_charter_alone_in_turn(world):
    owner = world.headers["owner"]
    for name in ("claude-a", "claude-b"):
        env_secret(world.client, owner, name, VAR, sample(f"{name}-"))
    # bound to the worker on duty, which would come first for a member's run, but the charter does not list it
    env_secret(world.client, owner, "claude-c", VAR, sample("claude-c-"), workers=["mac-mini"])
    written(world.client, owner, curator_changes.curator_charter(env_secrets=["claude-a", "claude-b"]))

    first = curator_changes.review_run(world)
    assert taken(world, first) == ("claude-a", "charter")
    assert taken(world, first) == ("claude-a", "charter")  # the same account on every ask
    for state in ("verifying", "done"):
        assert report(world.client, world.worker, first, state).status_code == 200

    second = second_review(world, first, THE_NIGHT + timedelta(days=1))
    assert taken(world, second) == ("claude-b", "charter")
