"""Source 6 and the lease's principal (plan autonomy-levels, steps 1, 2 and 8, after plan credential-sharing): the level
a credential share carries (credential_shares.level) holds a run leased under it, the lowest of them when there are
several, and only ever lowers its autonomy; each lease records the run's principal (runs.principal_id) in
credential_leases.principal_id, not the member who dispatched it.

The first test needs nothing; the others need the hub and skip without EVO_HUB_TEST_DSN."""

from __future__ import annotations

import pytest

from evo_agents.hub import autonomy_levels
from evo_agents.hub.autonomy_levels import CREDENTIAL_SHARE


def test_autonomy_level_share_source_6_is_the_lowest_level_of_the_shares_and_empty_without_one():
    empty = autonomy_levels.credential_source()
    assert (empty.name, empty.level) == (CREDENTIAL_SHARE, None)
    assert autonomy_levels.credential_source([(4, None)]) == empty
    held = autonomy_levels.credential_source([(3, "A4"), (4, None), (7, "A2"), (9, "A3")])
    assert (held.name, held.level) == (CREDENTIAL_SHARE, "A2") and "share 7" in held.detail
    principal = autonomy_levels.pref_source("lena", "A1")
    found = autonomy_levels.effective([principal, held, autonomy_levels.instance_source()])
    assert (found.level, found.level_source) == ("A1", "pref:lena"), "a share never raises the principal"
    top = autonomy_levels.effective([autonomy_levels.pref_source("lena", None), held])
    assert (top.level, top.level_source) == ("A2", CREDENTIAL_SHARE)


pytest.importorskip("sqlalchemy", reason="the leases need the hub-server extra, evo-ak[hub-server]")

from tests.hub import pg  # noqa: E402

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select, update  # noqa: E402

from evo_agents.hub import tables  # noqa: E402
from tests.hub.live import sql  # noqa: E402
from tests.hub.test_credential_share_lease import (  # noqa: E402, F401 (app_key, client, config, hub, shared: fixtures)
    OTHER,
    app_key,
    client,
    config,
    env_of,
    hub,
    run_on,
    set_share,
    share_leases,
    shared,
    user_id,
)
from tests.hub.test_credentials_api import worker_of  # noqa: E402


def snapshot(db, run_id: int) -> dict:
    return sql(db, select(tables.runs.c.autonomy).where(tables.runs.c.id == run_id))[0][0]


def test_autonomy_level_share_a_share_with_a_level_holds_the_run_it_lends_from_that_lease_on(
    client, hub, hub_db, shared
):
    set_share(hub_db, shared["share"], level="A1")
    desk = shared["desk"]
    run_id = run_on(client, hub["other"], desk, "notes")
    before = snapshot(hub_db, run_id)
    assert before["level"] == "A5", "dispatch takes no share: the run holds no lease yet"
    assert env_of(client, desk, run_id)["name"] == "team-key"
    after = snapshot(hub_db, run_id)
    assert (after["level"], after["level_source"]) == ("A1", CREDENTIAL_SHARE)
    assert after["classes"]["K5"]["mode"] == "ask" and "tightened_at" in after


def test_autonomy_level_share_a_share_without_a_level_leaves_the_run_as_it_was(client, hub, hub_db, shared):
    desk = shared["desk"]
    run_id = run_on(client, hub["other"], desk, "notes")
    before = snapshot(hub_db, run_id)
    assert env_of(client, desk, run_id)["name"] == "team-key"
    assert snapshot(hub_db, run_id) == before


def test_autonomy_level_share_each_lease_records_the_runs_principal_not_its_dispatcher(client, hub, hub_db, shared):
    box = worker_of(client, hub["owner"], "box")
    run_id = run_on(client, hub["owner"], box, "lumen-kb-docs")
    r = tables.runs
    sql(hub_db, update(r).where(r.c.id == run_id).values(principal_id=user_id(hub_db, OTHER)))
    assert env_of(client, box, run_id)["name"] == "team-key", "the dispatcher's own secret, as before"
    assert share_leases(hub_db, run_id) == [("team-key", user_id(hub_db, OTHER), None, False)]
