"""@marketer and the rule ``growth`` of the plugin marketing on the Loop (criterion a16 of plan autonomous-oss-growth),
on the hub with Postgres: a scheduled post waits until the Loop's publish work item releases it, then goes through the
gate; the kill switch holds it; a post that went out is measured; a reply under a project account's post makes reply
work within ``replies_per_day``, a personal account's never; the tools are @marketer's alone, refuse a personal account
and turn content for a member's channel into a proposal for the blog project its config names. The handler is called as the tick calls it; a
fake transport stands for every platform."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import insert, select

from evo_agents.core_plugins.marketing import MANIFEST, loop, store, tools
from evo_agents.core_plugins.marketing import tables as marketing_tables
from evo_agents.core_plugins.marketing.adapters.base import HttpResponse
from evo_agents.hub import tables
from evo_agents.hub.plugin_calls import CoreContext
from evo_agents.hub.server.task_dispatch import hand_own_task
from tests.hub.live import sql
from tests.hub.test_marketing_plugin import (  # noqa: F401 (fixtures)
    BASE,
    GOOD,
    OPEN_RULES,
    PROJECT,
    RELEASE,
    channel,
    client,
    github,
    members,
    mk,
    propose,
    rules,
    run_job,
)

TYPE = f"/v1/projects/{PROJECT}/agent-types/marketing/marketer"
SOURCES = [{"quote": "evo-agents ships the loop", "url": RELEASE}]


def project_id(world) -> int:
    return sql(world.db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]


def context(world, run_id: int | None = None) -> CoreContext:
    engine, config = world.client.app.state.engine, world.client.app.state.config
    return CoreContext("marketing", engine, config, PROJECT, project_id(world), run_id)


def clock(world, now: datetime, items: list[dict] | None = None) -> list[dict]:
    """The rule's handler on the clock, as the tick calls it."""
    params = {
        "project": PROJECT,
        "project_id": project_id(world),
        "rule": "growth",
        "event": None,
        "now": now.isoformat(),
        "items": items or [],
        "goals": [],
    }
    return world.client.portal.call(partial(loop.growth, params, context(world)))["effects"]


def running(kind: str, post_id: int, item_id: int = 1) -> dict:
    return {"id": item_id, "kind": kind, "state": "running", "detail": {"post_id": post_id}}


def post(world, post_id: int):
    t = marketing_tables.marketing_posts
    return sql(world.db, select(t.c.state, t.c.released_at, t.c.claim_id, t.c.signal_key).where(t.c.id == post_id))[0]


def marketer_run(world, run_as: str = "owner") -> int:
    """@marketer enabled under ``run_as``, and a task run of it handed out."""
    typed = world.client.put(TYPE, json={"run_as": run_as, "daily_usd": 5}, headers=world.h["owner"])
    assert typed.status_code == 200, typed.text

    async def hand():
        async with world.client.app.state.engine.begin() as conn:
            return await hand_own_task(conn, "marketing", project_id(world), "marketer", "Campaign", "Campaign")

    run_id, why = world.client.portal.call(hand)
    assert run_id is not None, why
    return run_id


def call(world, tool, run_id: int | None, arguments: dict) -> dict:
    return world.client.portal.call(partial(tool, {"arguments": arguments}, context(world, run_id)))


def test_growth_marketer_manifest_declares_the_type_its_tools_and_the_loop_rule():
    contributes = MANIFEST["contributes"]
    marketer = next(item for item in contributes["agent_types"] if item["name"] == "marketer")
    text = marketer["profile"]["instructions"]
    for rule in ("Vietnamese on viblo, facebook_page", "Never reply from a personal account", "do not approve"):
        assert rule in text
    assert "growth-proposals" in text and "blog project" in text and "dinh-vi.md" in text
    assert {tool["name"] for tool in contributes["mcp_tools"]} == {"propose", "draft", "personal"}
    (rule,) = contributes["loop_rules"]
    assert rule["on"] == ["canvas.baseline"] and rule["schedule"] is True
    assert {k["kind"]: k.get("agent_type") for k in rule["kinds"]} == {
        "growth_idea": "marketer",
        "growth_draft": "marketer",
        "growth_publish": None,
        "growth_review": None,
    }


def test_growth_marketer_scheduled_post_waits_for_the_loop_then_goes_through_the_gate(mk):
    rules(mk, {"github": OPEN_RULES})
    chan = github(mk)
    at = mk.clock.at + timedelta(hours=1)
    made = propose(mk, chan, sources=SOURCES, links=[RELEASE], scheduled_at=at.isoformat(), signal_key="gh.stars")
    assert made["state"] == "proposed" and made["scheduled_at"] and made["signal_key"] == "gh.stars"
    assert mk.judge.calls == 0 and run_job(mk)["posts"] == {}  # the job does not decide a post before its time
    assert clock(mk, at - timedelta(minutes=1)) == []
    (work,) = [e for e in clock(mk, at + timedelta(minutes=1)) if e["kind"] == loop.PUBLISH]
    assert work["key"] == f"publish:{made['id']}" and work["detail"] == {"post_id": made["id"]}
    item = running(loop.PUBLISH, made["id"])
    assert clock(mk, at + timedelta(minutes=2), [item]) == []  # released, still open: no move
    assert post(mk, made["id"])[1] is not None
    assert run_job(mk)["posts"] == {made["id"]: "posted"}
    moves = [e for e in clock(mk, at + timedelta(minutes=3), [item]) if e["effect"] == "move"]
    assert moves == [{"effect": "move", "item_id": 1, "state": "done", "detail": {"post_state": "posted"}}]


def test_growth_marketer_a_scheduled_post_holds_while_the_kill_switch_is_set(mk):
    rules(mk, {"github": OPEN_RULES})
    chan = github(mk)
    at = mk.clock.at + timedelta(hours=1)
    made = propose(mk, chan, sources=SOURCES, links=[RELEASE], scheduled_at=at.isoformat())
    assert mk.client.post(f"{BASE}/kill", json={"reason": "stop"}, headers=mk.h["owner"]).json() == {"ok": True}
    later = at + timedelta(minutes=5)
    assert [e for e in clock(mk, later) if e["kind"] == loop.PUBLISH] == []
    clock(mk, later, [running(loop.PUBLISH, made["id"])])
    assert post(mk, made["id"])[:2] == ("proposed", None) and mk.platform.sent == []


def test_growth_marketer_a_post_with_a_past_time_goes_through_the_gate_at_once(mk):
    rules(mk, {"github": OPEN_RULES})
    chan = github(mk)
    at = mk.clock.at - timedelta(minutes=1)
    made = propose(mk, chan, sources=SOURCES, links=[RELEASE], scheduled_at=at.isoformat())
    assert made["state"] == "posted" and len(mk.platform.sent) == 1


def test_growth_marketer_a_post_that_went_out_is_measured_and_done_after_its_7d_numbers(mk):
    rules(mk, {"github": OPEN_RULES})
    made = propose(mk, github(mk), sources=SOURCES, links=[RELEASE], signal_key="gh.stars")
    assert made["state"] == "posted"
    now = datetime.now(UTC) + timedelta(hours=1)
    (work,) = [e for e in clock(mk, now) if e["kind"] == loop.REVIEW]
    assert work["key"] == f"measure:{made['id']}"
    item = running(loop.REVIEW, made["id"])
    assert [e for e in clock(mk, now, [item]) if e["effect"] == "move"] == []
    sql(
        mk.db,
        insert(marketing_tables.marketing_metrics).values(post_id=made["id"], span="7d", metric="stars", value=12),
    )
    (moved,) = [e for e in clock(mk, now, [item]) if e["effect"] == "move"]
    assert moved["state"] == "done"
    assert moved["detail"]["signal_key"] == "gh.stars" and moved["detail"]["numbers"] == {"7d": {"stars": 12.0}}


def test_growth_marketer_replies_under_project_posts_make_reply_work_within_the_daily_cap(mk):
    rules(mk, {"reddit": {**OPEN_RULES, "self_promo_ratio": 1}})
    mk.platform.answers["oauth.reddit.com/api/submit"] = HttpResponse(
        200, {"json": {"data": {"url": "https://reddit.com/r/x/1", "name": "t3_abc"}}}
    )
    chan = channel(mk, "reddit", "evo_bot", target="r/selfhosted")
    made = propose(mk, chan, sources=SOURCES, links=[RELEASE])
    assert made["state"] == "posted", made
    comments = [
        {
            "kind": "t1",
            "data": {"name": f"t1_{n}", "author": f"user{n}", "body": f"how does it {n}?", "permalink": "/r"},
        }
        for n in range(3)
    ]
    own = {"kind": "t1", "data": {"name": "t1_me", "author": "evo_bot", "body": "thanks", "permalink": "/r"}}
    mk.platform.answers["oauth.reddit.com/comments/abc"] = HttpResponse(
        200, {"items": [{}, {"data": {"children": [*comments, own]}}]}
    )
    assert run_job(mk)["replies"] == 3  # the account's own reply is not one
    from tests.hub.test_tasks_plugin import configure

    configure(mk.client, mk.h["owner"], "marketing", {"replies_per_day": 2})
    drafts = [e for e in clock(mk, datetime.now(UTC)) if e["kind"] == loop.DRAFT]
    assert len(drafts) == 2 and all(e["key"].startswith("reply:") for e in drafts)
    assert "marketing__propose with kind reply" in drafts[0]["detail"]["task"] and "t1_0" in drafts[0]["detail"]["task"]
    assert [e for e in clock(mk, datetime.now(UTC)) if e["kind"] == loop.DRAFT] == drafts  # the oldest first, again


def test_growth_marketer_tools_are_for_a_marketer_run_alone_and_tie_a_post_to_its_claim(mk, monkeypatch):
    monkeypatch.setattr(tools, "RUNTIME", mk.client.app.state.marketing_runtime)
    rules(mk, {"github": OPEN_RULES})
    chan = github(mk)
    arguments = {"channel_id": chan["id"], "body": GOOD, "links": [RELEASE], "sources": SOURCES}
    refused = call(mk, tools.propose, None, arguments)
    assert refused["is_error"] and "@marketer" in refused["content"][0]["text"]
    run_id = marketer_run(mk)
    unknown = call(mk, tools.propose, run_id, {**arguments, "claim": "CL-9"})
    assert unknown["is_error"] and "no claim CL-9" in unknown["content"][0]["text"]
    done = call(mk, tools.propose, run_id, {**arguments, "signal_key": "gh.stars"})
    assert not done["is_error"] and "is posted" in done["content"][0]["text"]
    t = marketing_tables.marketing_posts
    (row,) = sql(mk.db, select(t.c.proposed_by, t.c.run_id, t.c.signal_key))
    owner = sql(mk.db, select(tables.users.c.id).where(tables.users.c.login == "owner"))[0][0]
    assert row == (owner, run_id, "gh.stars")


def test_growth_personal_content_is_a_draft_unless_autonomous_and_a_proposal_for_the_blog_project(mk):
    from tests.hub.test_tasks_plugin import configure

    run_id = marketer_run(mk)
    mine = channel(mk, "tiktok", "alice", who="alice", kind="personal", mode="draft_only")
    arguments = {
        "channel_id": mine["id"],
        "body": "Cách vòng lặp của evo-agents chọn việc tiếp theo",
        "links": [RELEASE],
        "sources": SOURCES,
        "topic": "Vòng lặp chọn việc thế nào",
        "reason": "gh.stars tăng 40 trong 7 ngày",
        "deadline": "2026-11-01",
    }
    refused = call(mk, tools.propose, run_id, {**arguments, "body": "x"})
    assert refused["is_error"] and "marketing__personal" in refused["content"][0]["text"]
    unset = call(mk, tools.personal, run_id, arguments)["content"][0]["text"]
    assert "No blog project is set" in unset and "growth-proposals" not in unset
    configure(mk.client, mk.h["owner"], "marketing", {"proposals_project": "team-blog"})
    answer = call(mk, tools.personal, run_id, arguments)
    text = answer["content"][0]["text"]
    assert not answer["is_error"] and "is draft" in text and mk.platform.sent == []
    assert "growth-proposals" in text and "hub project team-blog" in text
    for line in ("Đề xuất bài: Vòng lặp chọn việc thế nào", "Lý do: gh.stars", "Kênh đích: tiktok alice", "Hạn: "):
        assert line in text
    reply = call(mk, tools.personal, run_id, {**arguments, "kind": "reply", "reply_to": "x"})
    assert "kind reply" not in reply["content"][0]["text"]  # a personal account posts originals only
    assert sql(mk.db, select(store.tables.marketing_posts.c.kind).distinct()) == [("post",)]
