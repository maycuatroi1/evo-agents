"""The health check of a channel account of the plugin marketing (plan autonomous-oss-growth, step 22), on the hub with
Postgres: an admin checks an account with a read that posts nothing, the result stays on the channel, the credential is
in no answer and no audit row, and migration 0129 adds the three columns. A fake transport answers: no test calls a
platform."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("psycopg")

from psycopg import errors
from sqlalchemy import insert, select

from evo_agents.core_plugins.marketing import tables as marketing_tables
from evo_agents.core_plugins.marketing.adapters.base import HttpResponse
from evo_agents.hub import tables
from tests.hub import live, pg
from tests.hub.live import sql
from tests.hub.test_marketing_plugin import (  # noqa: F401 (fixtures)
    BASE,
    TOKEN,
    actions,
    channel,
    client,
    members,
    mk,
)


def check(world, chan: dict, who="owner", expect=200) -> dict:
    answer = world.client.post(f"{BASE}/channels/{chan['id']}/check", json={}, headers=world.h[who])
    assert answer.status_code == expect, answer.text
    return answer.json()


def stored(world, chan: dict):
    c = marketing_tables.marketing_channels.c
    return sql(world.db, select(c.health, c.health_reason, c.health_checked_at).where(c.id == chan["id"]))[0]


def test_marketing_health_an_admin_checks_an_account_and_the_result_stays_on_the_channel(mk):
    chan = channel(mk, "github", "acme", target="acme/widgets", mode="draft_only")
    assert chan["health"] == "unknown" and chan["account_kind"] == chan["kind"] == "project"
    mk.platform.answers["api.github.com/repos/acme/widgets"] = HttpResponse(
        200, {"full_name": "acme/widgets", "permissions": {"push": True}}
    )
    found = check(mk, chan)
    assert found["health"] == "ok" and found["health_reason"] == "push on acme/widgets" and found["health_checked_at"]
    assert [(r.method, r.url) for r in mk.platform.sent] == [("GET", "https://api.github.com/repos/acme/widgets")]
    assert stored(mk, chan)[0] == "ok"
    listed = mk.client.get(f"{BASE}/channels", headers=mk.h["eve"]).json()
    assert listed[0]["health"] == "ok" and listed[0]["account_kind"] == "project"
    assert actions(mk.db)[-1] == "marketing.channel.check"
    assert sql(mk.db, select(marketing_tables.marketing_posts.c.id)) == []  # nothing was posted


def test_marketing_health_only_an_admin_checks_and_a_writer_changes_nothing(mk):
    chan = channel(mk, "x", "evo", mode="draft_only")
    for who in ("alice", "eve"):
        check(mk, chan, who, expect=403)
    assert stored(mk, chan)[0] == "unknown" and mk.platform.sent == []


def test_marketing_health_a_refused_credential_fails_with_its_reason_and_never_comes_back(mk):
    chan = channel(mk, "x", "evo", mode="draft_only")
    mk.platform.status = 401
    found = check(mk, chan)
    assert found["health"] == "failing" and "refused the credential" in found["health_reason"]
    assert stored(mk, chan)[1] == found["health_reason"]
    audit = sql(mk.db, select(tables.audit))
    assert TOKEN not in json.dumps([found, audit, [repr(r) for r in mk.platform.sent]], default=str)


def test_marketing_health_no_credential_draft_only_channels_and_browser_sessions(client, members, hub_db):  # noqa: F811
    from types import SimpleNamespace

    from tests.hub.test_marketing_plugin import Platform
    from tests.hub.test_tasks_plugin import configure, enable

    enable(client, members["owner"], "marketing")
    configure(client, members["owner"], "marketing", {"dry_run_hours": 0})
    world = SimpleNamespace(h=members, client=client, db=hub_db, platform=Platform())
    from evo_agents.core_plugins.marketing import flow

    client.app.state.marketing_runtime = flow.Runtime(transport=world.platform)
    bare = check(world, channel(world, "reddit", "evo", target="r/python", mode="draft_only"))
    assert (bare["health"], bare["health_reason"]) == ("failing", "no credential")
    news = check(world, channel(world, "hackernews", "evo", mode="draft_only"))
    assert news["health"] == "n/a"
    viblo = channel(world, "viblo", "evo", mode="draft_only")
    assert check(world, viblo)["health"] == "failing"
    session = client.post(f"{BASE}/channels/{viblo['id']}/session", json={"state": "ok"}, headers=members["owner"])
    assert session.status_code == 200, session.text
    assert check(world, viblo)["health"] == "ok" and world.platform.sent == []


@pytest.mark.empty_db
def test_0129_adds_the_health_of_a_channel_with_unknown_by_default_and_going_back_drops_it(hub_db):
    from tests.hub.test_migrate import ALL, columns, move_to, one, seed

    move_to(hub_db, "0128")
    with live.connect(hub_db) as conn:
        ids = seed(conn)
    t = marketing_tables.marketing_channels
    account = {"project_id": ids["project"], "channel": "x", "account": "evo", "account_kind": "project"}
    with live.connect(hub_db) as conn:
        conn.execute(insert(t).values(account))
    assert "health" not in columns(hub_db, "marketing_channels")
    move_to(hub_db, "0129")
    with live.connect(hub_db) as conn:
        assert one(conn, select(t.c.health)) == "unknown"  # the account made before reads as never checked
        with pytest.raises(errors.CheckViolation):
            conn.execute(insert(t).values({**account, "account": "b", "health": "maybe"}))
    move_to(hub_db, "0128", down=True)
    assert not {"health", "health_reason", "health_checked_at"} & columns(hub_db, "marketing_channels")
    assert pg.migrate_in_steps(hub_db) == ALL[ALL.index("0129") :]
