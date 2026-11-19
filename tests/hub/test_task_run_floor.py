"""The floor of one run's cap for an agent type (``min_run_usd``), on the hub (``evo_agents.hub.task_runs``,
``evo_agents.hub.server.agent_types``)."""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from evo_agents.hub import task_runs
from tests.hub.test_credentials_api import app_key  # noqa: F401  (a fixture)
from tests.hub.test_task_runs import PLUGIN, PROJECT, client, enabled, hub, listed, put_type  # noqa: F401

# Setting the floor (step 3)


def test_put_without_a_floor_shows_the_hub_default_and_none_set(client, hub):
    view = enabled(client, hub["owner"], daily_usd=5)
    assert (view["min_run_usd"], view["effective_min_run_usd"]) == (None, task_runs.DEFAULT_MIN_RUN_USD)
    item = listed(client, hub["reader"])[(PLUGIN, "triager")]
    assert (item["min_run_usd"], item["effective_min_run_usd"]) == (None, 1.0)


def test_put_with_a_floor_sets_it_and_get_shows_set_and_effective(client, hub):
    view = enabled(client, hub["owner"], daily_usd=5, min_run_usd=2.5)
    assert (view["min_run_usd"], view["effective_min_run_usd"]) == (2.5, 2.5)
    item = listed(client, hub["reader"])[(PLUGIN, "triager")]
    assert (item["min_run_usd"], item["effective_min_run_usd"]) == (2.5, 2.5)


def test_the_default_floor_never_exceeds_the_daily_cap(client, hub):
    view = enabled(client, hub["owner"], daily_usd=0.5)
    assert (view["min_run_usd"], view["effective_min_run_usd"]) == (None, 0.5)


@pytest.mark.parametrize("floor", [0, -1, 5.01])
def test_a_floor_not_above_zero_or_above_the_daily_cap_is_422_and_changes_nothing(client, hub, floor):
    enabled(client, hub["owner"], daily_usd=5, min_run_usd=2)
    assert put_type(client, hub["owner"], daily_usd=5, min_run_usd=floor).status_code == 422
    item = listed(client, hub["reader"])[(PLUGIN, "triager")]
    assert (item["daily_usd"], item["min_run_usd"]) == (5, 2)


def test_changing_the_daily_cap_without_a_floor_keeps_the_floor_set(client, hub):
    enabled(client, hub["owner"], daily_usd=5, min_run_usd=2)
    view = enabled(client, hub["owner"], daily_usd=10)
    assert (view["daily_usd"], view["min_run_usd"], view["effective_min_run_usd"]) == (10, 2, 2)
    view = enabled(client, hub["owner"], daily_usd=1.5)
    assert (view["min_run_usd"], view["effective_min_run_usd"]) == (2, 1.5)


def test_a_new_floor_replaces_the_one_set(client, hub):
    enabled(client, hub["owner"], daily_usd=5, min_run_usd=2)
    view = enabled(client, hub["owner"], daily_usd=5, min_run_usd=3)
    assert (view["min_run_usd"], view["effective_min_run_usd"]) == (3, 3)
