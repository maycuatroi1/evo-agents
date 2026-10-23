"""@community of the plugin crm and its tool ``crm__invite`` (criterion a16 of plan autonomous-oss-growth), on the hub
with Postgres: only a run of @community makes a link; a link is made only for a login that asked with that invite
request, once per request, at most ``invite_links_per_week`` in 7 days (3 by default), for a GitHub account at least
``invite_min_github_age_days`` old (30 by default); it is bound to the login, gives the reader role at the ladder's
lowest level and is audited. A fake stands for GitHub."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import select

from evo_agents.core_plugins.crm import MANIFEST, github, intake, settings, tools
from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import CoreContext
from tests.hub.live import sql
from tests.hub.test_tasks_plugin import (  # noqa: F401 (fixtures)
    PROJECT,
    client,
    configure,
    enable,
    members,
    tasks_core,
)

TYPE = f"/v1/projects/{PROJECT}/agent-types/crm/community"
PUBLIC = SimpleNamespace(public_url="https://hub.test")
NOW = datetime.now(UTC)


class Accounts:
    """GitHub's answer to how old an account is: a creation time, None for no such account."""

    def __init__(self, **ages: int | None):
        self.ages = ages

    async def account_created(self, login: str):
        days = self.ages.get(login, 365)
        return None if days is None else NOW - timedelta(days=days)


def project_id(db) -> int:
    return sql(db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]


@pytest.fixture
def world(client, members, hub_db, monkeypatch):  # noqa: F811
    enable(client, members["owner"], "crm")
    configure(client, members["owner"], "crm", {"repo": "acme/widgets"})
    accounts = Accounts()
    monkeypatch.setattr(github, "default_fetcher", lambda: accounts)
    return SimpleNamespace(client=client, h=members, db=hub_db, accounts=accounts)


def asked(world, *logins: str) -> None:
    """Each login asks for an invite with an issue of its own, issue#<n> in the order given."""
    seen = [
        intake.Seen(login, "invite_request", f"issue#{n}", f"https://github.com/acme/widgets/issues/{n}", NOW)
        for n, login in enumerate(logins, start=1)
    ]

    async def apply():
        async with world.client.app.state.engine.begin() as conn:
            return await intake.apply(conn, project_id(world.db), {}, seen)

    world.client.portal.call(apply)


def community_run(world) -> int:
    typed = world.client.put(TYPE, json={"run_as": "owner", "daily_usd": 5}, headers=world.h["owner"])
    assert typed.status_code == 200, typed.text

    from evo_agents.hub.server.task_dispatch import hand_own_task  # after the app: the server's modules load in order

    async def hand():
        async with world.client.app.state.engine.begin() as conn:
            return await hand_own_task(conn, "crm", project_id(world.db), "community", "Invite", "Invite")

    run_id, why = world.client.portal.call(hand)
    assert run_id is not None, why
    return run_id


def invite(world, run_id: int | None, login: str, ref: str) -> tuple[bool, str]:
    context = CoreContext("crm", world.client.app.state.engine, PUBLIC, PROJECT, project_id(world.db), run_id)
    answer = world.client.portal.call(partial(tools.invite, {"arguments": {"login": login, "ref": ref}}, context))
    return answer["is_error"], answer["content"][0]["text"]


def invites(db) -> list[tuple]:
    i = tables.invites.c
    return sql(db, select(i.for_login, i.role, i.kind, i.created_by).order_by(i.id))


def test_growth_community_manifest_declares_the_type_its_tool_the_rule_and_the_policy_defaults():
    contributes = MANIFEST["contributes"]
    (community,) = [t for t in contributes["agent_types"] if t["name"] == "community"]
    text = community["profile"]["instructions"]
    assert "crm__invite" in text and "Never reply from a personal account" in text and "do not approve" in text
    assert [tool["name"] for tool in contributes["mcp_tools"]] == ["invite"]
    (rule,) = contributes["loop_rules"]
    assert rule["on"] == ["plugin.crm.touch"] and {k["kind"] for k in rule["kinds"]} == {
        "growth_invite",
        "growth_draft",
    }
    fields = MANIFEST["config"]["properties"]
    assert (fields["invite_links_per_week"]["default"], fields["invite_min_github_age_days"]["default"]) == (3, 30)
    cfg = settings.Config(None)
    assert (cfg.invite_links_per_week, cfg.invite_min_github_age_days) == (3, 30)


def test_growth_community_invite_makes_a_link_bound_to_the_login_that_asked_once(world):
    asked(world, "newbie")
    refused, text = invite(world, None, "newbie", "issue#1")
    assert refused and "only the agent of a run of @community" in text
    run_id = community_run(world)
    made, text = invite(world, run_id, "newbie", "issue#1")
    assert not made and "Invite link for newbie" in text and "https://hub.test/" in text
    owner = sql(world.db, select(tables.users.c.id).where(tables.users.c.login == "owner"))[0][0]
    assert invites(world.db) == [("newbie", "reader", "join", owner)]
    a = tables.audit.c
    (target,) = sql(world.db, select(a.target).where(a.action == "invite.create"))
    assert target[0].startswith("invite:") and "role=reader" in target[0] and target[0].endswith("for=newbie")
    again, text = invite(world, run_id, "newbie", "issue#1")
    assert again and "already" in text
    other, text = invite(world, run_id, "someone", "issue#1")
    assert other and "made no invite request issue#1" in text
    assert len(invites(world.db)) == 1


def test_growth_community_invite_refuses_a_young_or_unknown_github_account(world):
    asked(world, "fresh", "ghost")
    world.accounts.ages.update(fresh=29, ghost=None)
    run_id = community_run(world)
    young, text = invite(world, run_id, "fresh", "issue#1")
    assert young and "younger than 30 days" in text
    unknown, text = invite(world, run_id, "ghost", "issue#2")
    assert unknown and "no account ghost" in text
    assert invites(world.db) == []


def test_growth_community_invite_keeps_to_the_links_a_week_of_the_policy(world):
    configure(world.client, world.h["owner"], "crm", {"repo": "acme/widgets", "invite_links_per_week": 2})
    asked(world, "ann", "ben", "cat")
    run_id = community_run(world)
    assert not invite(world, run_id, "ann", "issue#1")[0]
    assert not invite(world, run_id, "ben", "issue#2")[0]
    capped, text = invite(world, run_id, "cat", "issue#3")
    assert capped and "made 2 links this week" in text
    assert [row[0] for row in invites(world.db)] == ["ann", "ben"]
