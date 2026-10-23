"""The rules ``growth`` of the plugins marketing and crm on the project's Loop (criterion a16 of plan
autonomous-oss-growth), through the tick: a claim of the Customer segments cell that an approved baseline moves to
testing makes @marketer's campaign, tied to the claim; a claim of another cell makes none; a new touch from GitHub
makes @community's work, an invite request an invite item and an outsider's issue a first-reply item; an old touch or
a team member's makes none."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from evo_agents.core_plugins.crm import intake
from tests.hub.test_ba_agent import enable, members  # noqa: F401 (members is a fixture)
from tests.hub.test_loop_tick import (  # noqa: F401 (the fixtures)
    app_key,
    client,
    hub,
    item,
    items,
    loop_core,
    policy,
    project_id,
    push_baseline,
    ticked,
)
from tests.hub.test_strategist_agent import CARD, edit


def growth_items(db) -> list[tuple]:
    return [row for row in items(db) if row[1].startswith("growth_")]


def touched(client, db, *seen: intake.Seen) -> None:
    async def apply():
        async with client.app.state.engine.begin() as conn:
            return await intake.apply(conn, project_id(db), {}, list(seen))

    client.portal.call(apply)


def test_growth_loop_rules_a_customer_segment_claim_in_testing_makes_a_campaign_of_marketer(client, hub, hub_db):
    enable(client, hub["owner"], "marketing")
    policy(client, hub, {"enabled": True})
    ticked(client, hub_db)
    edit(client, hub["chi"], {"cell": "customer_segments", "text": "Developers who run agents on their own servers"})
    edit(client, hub["chi"], {"claim": "CL-2", "base_rev": 1, "status": "testing", "test_card": CARD})
    edit(client, hub["chi"], {"claim": "CL-1", "base_rev": 2, "status": "testing", "test_card": CARD})
    push_baseline(client, hub, "CL-2")
    ticked(client, hub_db)
    (campaign,) = growth_items(hub_db)
    assert campaign[1:3] == ("growth_idea", "queued")  # a task of @marketer, which this project has not enabled
    made = item(hub_db, campaign[0])
    assert made["key"].startswith("marketing:campaign:") and made["claim_id"] is not None
    assert (
        made["detail"]["claim"] == "CL-2" and "Claim CL-2 (customer_segments) is in testing" in made["detail"]["task"]
    )
    assert (
        "marketing__propose: claim CL-2" in made["detail"]["task"] and "Vietnamese on viblo" in made["detail"]["task"]
    )
    before = len(items(hub_db))
    ticked(client, hub_db)  # the same baseline read again makes nothing more
    assert len(items(hub_db)) == before
    push_baseline(client, hub, "CL-1")  # a claim of the Problem cell: no campaign
    ticked(client, hub_db)
    assert len(growth_items(hub_db)) == 1


def test_growth_loop_rules_a_new_github_touch_makes_community_work(client, hub, hub_db):
    enable(client, hub["owner"], "crm")
    policy(client, hub, {"enabled": True})
    ticked(client, hub_db)
    now = datetime.now(UTC)
    url = "https://github.com/acme/widgets/issues/"
    touched(
        client,
        hub_db,
        intake.Seen("newbie", "invite_request", "issue#7", f"{url}7", now - timedelta(hours=2)),
        intake.Seen("stranger", "issue", "issue#8", f"{url}8", now - timedelta(hours=1)),
        intake.Seen("oldtimer", "issue", "issue#1", f"{url}1", now - timedelta(days=30)),  # too old to answer
        intake.Seen("owner", "issue", "issue#9", f"{url}9", now),  # an admin of the project: its team
    )
    ticked(client, hub_db)
    assert growth_items(hub_db) == [
        ("crm:invite:issue#7", "growth_invite", "queued", None),
        ("crm:first-reply:issue#8", "growth_draft", "queued", None),
    ]
    invite = item(hub_db, "crm:invite:issue#7")["detail"]
    assert "crm__invite with login newbie and ref issue#7" in invite["task"]
    assert "Never promise a link the tool did not make" in invite["task"]
    first = item(hub_db, "crm:first-reply:issue#8")["detail"]["task"]
    assert first.startswith("stranger, who is not a member of the repo, opened a issue") and "English" in first
    touched(client, hub_db, intake.Seen("stranger", "issue", "issue#8", f"{url}8", now))  # the same touch: no event
    ticked(client, hub_db)
    assert len(growth_items(hub_db)) == 2


def test_growth_loop_rules_handler_of_crm_makes_nothing_for_a_contact_deleted_since(client, hub, hub_db):
    from evo_agents.core_plugins.crm import loop
    from evo_agents.hub.plugin_calls import CoreContext

    context = CoreContext("crm", client.app.state.engine, None, "x", project_id(hub_db))
    event = {"id": 1, "kind": intake.TOUCH, "payload": {"contact_id": 999999, "kind": "issue", "ref": "issue#3"}}
    params = {"project": "x", "project_id": project_id(hub_db), "rule": "growth", "event": event, "items": []}
    assert client.portal.call(partial(loop.growth, params, context)) == {"effects": []}
