"""The core plugin marketing on the hub (``evo_agents.core_plugins.marketing``), as criterion a14 of plan
autonomous-oss-growth names it: channel accounts with a kind, a mode and an adapter; a posting gate (deny-list, source
rule, caps, gap, Judge, monthly budget) that a post passes with no human; a draft with a reason for a post that fails;
an audit of each post; a breaker for each channel and a kill switch for the project; personal accounts that only draft
unless their owner switched them; numbers at +24h and +7d pushed to Signal. A project that did not enable the plugin
writes nothing and answers 404. No test calls a platform: a fake transport answers, and a fake Judge scores."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from importlib.metadata import EntryPoint
from types import SimpleNamespace

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select

from evo_agents.core_plugins.marketing import flow, job, store
from evo_agents.core_plugins.marketing import tables as marketing_tables
from evo_agents.core_plugins.marketing.adapters.base import HttpResponse
from evo_agents.hub import plugin_calls, tables
from evo_agents.hub.server import core_plugin_api as hub_api
from evo_agents.hub.server.app import create_app
from evo_agents.plugins import core
from evo_agents.plugins.core import ENTRY_POINT_GROUP
from tests.hub import live
from tests.hub.live import sql
from tests.hub.test_tasks_plugin import PROJECT, configure, enable, members  # noqa: F401 (fixtures)

CORE = {"marketing": EntryPoint("marketing", "evo_agents.core_plugins.marketing:manifest", ENTRY_POINT_GROUP)}
BASE = f"/v1/x/marketing/{PROJECT}"
RELEASE = "https://github.com/maycuatroi1/evo-agents/releases/tag/v1"
GOOD = f"evo-agents ships the loop {RELEASE}"
PASSING = {"truth": 5, "brand": 5, "voice": 5, "privacy": 5}
TOKEN = "tok-not-a-real-credential"
OPEN_RULES = {"min_gap_hours": 0, "posts_per_day": 100, "posts_per_week": 100}


class Platform:
    """The fake transport: answers by the url, keeps every request."""

    def __init__(self):
        self.sent, self.answers, self.status = [], {}, 200

    async def send(self, request):
        self.sent.append(request)
        if self.status >= 300:
            return HttpResponse(self.status, {})
        for part, answer in self.answers.items():
            if part in request.url and (request.method == "GET") == part.startswith("GET "):
                return answer
        for part, answer in self.answers.items():
            if part in request.url:
                return answer
        return HttpResponse(
            200,
            {
                "data": {"id": str(len(self.sent))},
                "id": str(len(self.sent)),
                "html_url": "https://github.com/acme/widgets/releases/v1",
            },
        )


class Judge:
    """A Judge that answers at once with ``axes``, or not yet with None."""

    def __init__(self):
        self.axes, self.calls = dict(PASSING), 0

    async def __call__(self, conn, project, post, channel):
        self.calls += 1
        return self.axes


class Clock:
    def __init__(self):
        self.at = datetime.now(UTC) + timedelta(minutes=5)  # after every autonomous_since the test sets

    def __call__(self):
        return self.at

    def after(self, **delta):
        self.at += timedelta(**delta)


@pytest.fixture
def client(hub_db, tmp_path, github, monkeypatch):
    points = [point for point in core.entry_points(group=ENTRY_POINT_GROUP) if point.name not in CORE]
    points += list(CORE.values())
    monkeypatch.setattr(core, "entry_points", lambda *, group: list(points) if group == ENTRY_POINT_GROUP else [])
    plugin_calls.core_registry.cache_clear()
    config = live.hub_config(hub_db, tmp_path, github, secrets_key=secrets.token_bytes(32))
    with TestClient(create_app(config), base_url="https://hub.test") as client:
        yield client
    plugin_calls.core_registry.cache_clear()


@pytest.fixture
def mk(client, members, hub_db):  # noqa: F811
    """The project with plugin marketing enabled, a credential for each API channel, no dry run, and the fakes."""
    enable(client, members["owner"], "marketing")
    values = {f"{c}_token": TOKEN for c in ("github", "x", "linkedin", "reddit", "facebook_page", "tiktok")}
    configure(client, members["owner"], "marketing", {**values, "dry_run_hours": 0})
    world = SimpleNamespace(h=members, platform=Platform(), judge=Judge(), clock=Clock(), client=client, db=hub_db)
    client.app.state.marketing_runtime = flow.Runtime(judge=world.judge, transport=world.platform, now=world.clock)
    return world


def rules(world, values: dict) -> None:
    configure(world.client, world.h["owner"], "marketing", {"rules": values})


def channel(world, name="github", account="acme", who="owner", target=None, kind="project", mode="autonomous") -> dict:
    body = {"channel": name, "account": account, "kind": kind, "target": target}
    made = world.client.post(f"{BASE}/channels", json=body, headers=world.h[who])
    assert made.status_code == 201, made.text
    assert made.json()["mode"] == "draft_only"
    if mode == "autonomous":
        switched = world.client.put(
            f"{BASE}/channels/{made.json()['id']}/mode", json={"mode": mode}, headers=world.h[who]
        )
        assert switched.status_code == 200, switched.text
        return switched.json()
    return made.json()


def github(world) -> dict:
    return channel(world, "github", "acme", target="acme/widgets")


def propose(world, chan: dict, text: str = GOOD, who="alice", expect: int = 201, route="posts", **more) -> dict:
    body = {"channel_id": chan["id"], "body": text, **more}
    answer = world.client.post(f"{BASE}/{route}", json=body, headers=world.h[who])
    assert answer.status_code == expect, answer.text
    return answer.json()


def status(world, who="alice") -> dict:
    return world.client.get(f"{BASE}/status", headers=world.h[who]).json()


def actions(db) -> list[str]:
    return [row[0] for row in sql(db, select(tables.audit.c.action).order_by(tables.audit.c.id))]


def run_job(world) -> dict:
    engine = world.client.app.state.engine
    config = world.client.app.state.config
    runtime = world.client.app.state.marketing_runtime
    return world.client.portal.call(lambda: job.run_project(engine, PROJECT, hub_config=config, runtime=runtime))


# Channels


def test_marketing_channel_registry_makes_accounts_draft_only_and_refuses_the_wrong_kind(mk):
    c, h = mk.client, mk.h
    made = channel(mk, "github", "acme", target="acme/widgets", mode="draft_only")
    assert (made["kind"], made["mode"], made["adapter"], made["paused"]) == ("project", "draft_only", "api", False)
    refused = c.post(f"{BASE}/channels", json={"channel": "github", "account": "x"}, headers=h["alice"])
    assert refused.status_code == 403
    assert (
        c.post(f"{BASE}/channels", json={"channel": "github", "account": "acme"}, headers=h["owner"]).status_code == 409
    )
    wrong = c.post(f"{BASE}/channels", json={"channel": "facebook_profile", "account": "me"}, headers=h["owner"])
    assert wrong.status_code == 422 and "facebook_profile" in wrong.json()["message"]
    browser = channel(mk, "viblo", "evo", mode="draft_only")
    assert browser["adapter"] == "browser"
    assert [x["channel"] for x in c.get(f"{BASE}/channels", headers=h["eve"]).json()] == ["github", "viblo"]
    assert c.post(f"{BASE}/channels", json={"channel": "nope", "account": "x"}, headers=h["owner"]).status_code == 422


def test_marketing_channel_a_project_account_goes_autonomous_only_by_an_admin_and_hackernews_never(mk):
    c, h = mk.client, mk.h
    made = channel(mk, "github", "acme", target="acme/widgets", mode="draft_only")
    refused = c.put(f"{BASE}/channels/{made['id']}/mode", json={"mode": "autonomous"}, headers=h["alice"])
    assert refused.status_code == 403
    on = c.put(f"{BASE}/channels/{made['id']}/mode", json={"mode": "autonomous"}, headers=h["owner"]).json()
    assert on["mode"] == "autonomous" and on["autonomous_since"]
    hn = channel(mk, "hackernews", "evo", mode="draft_only")
    blocked = c.put(f"{BASE}/channels/{hn['id']}/mode", json={"mode": "autonomous"}, headers=h["owner"])
    assert blocked.status_code == 422
    assert "marketing.channel.autonomous" in actions(mk.db)


# Personal accounts


def test_marketing_personal_account_is_made_draft_only_and_a_post_command_to_it_is_refused(mk):
    c, h = mk.client, mk.h
    mine = channel(mk, "tiktok", "alice", who="alice", kind="personal", mode="draft_only")
    assert mine["kind"] == "personal" and mine["mode"] == "draft_only" and mine["owner"] == "alice"
    refused = propose(mk, mine, expect=403)
    assert "draft_only" in refused["message"]
    other = channel(mk, "x", "alice_x", who="alice", kind="personal", mode="draft_only")
    assert "never posts by itself" in propose(mk, other, expect=403)["message"]
    saved = propose(mk, mine, "Note for my follow-up", route="drafts")
    assert saved["state"] == "draft" and mk.platform.sent == []
    assert (
        c.put(f"{BASE}/channels/{other['id']}/mode", json={"mode": "autonomous"}, headers=h["alice"]).status_code == 422
    )
    assert sql(mk.db, select(func.count()).select_from(marketing_tables.marketing_posts))[0][0] == 1


def test_marketing_personal_account_goes_autonomous_only_for_its_owner_and_posts_originals_only(mk):
    c, h = mk.client, mk.h
    mine = channel(mk, "tiktok", "alice", who="alice", kind="personal", mode="draft_only")
    not_owner = c.put(f"{BASE}/channels/{mine['id']}/mode", json={"mode": "autonomous"}, headers=h["owner"])
    assert not_owner.status_code == 403 and "owns" in not_owner.json()["message"]
    assert c.put(f"{BASE}/channels/{mine['id']}/mode", json={"mode": "autonomous"}, headers=h["bao"]).status_code == 403
    on = c.put(f"{BASE}/channels/{mine['id']}/mode", json={"mode": "autonomous"}, headers=h["alice"])
    assert on.status_code == 200 and on.json()["mode"] == "autonomous"
    reply = propose(mk, mine, expect=403, kind="reply", reply_to="t1")
    assert "never a reply" in reply["message"]
    sent = propose(mk, mine, "A clip", links=["https://cdn.example/a.png"])
    assert sent["state"] == "posted"
    assert mk.platform.sent[0].json["source_info"]["photo_images"] == ["https://cdn.example/a.png"]
    off = c.put(f"{BASE}/channels/{mine['id']}/mode", json={"mode": "draft_only"}, headers=h["owner"])
    assert off.status_code == 200  # an admin may always take a personal account back to draft_only
    assert propose(mk, mine, expect=403)["message"]


def test_marketing_an_agent_cannot_change_a_mode(mk):
    from evo_agents.core_plugins.marketing import server
    from evo_agents.hub.server.core_plugin_api._types import issue_session
    from evo_agents.hub.server.security import MACHINE, Principal, RunScope

    row = SimpleNamespace(account_kind="project", owner_id=None)
    scope = RunScope(7, "northwind", "writer", "internal")
    agent = issue_session(1, "lumen", 0, False, principal=Principal(1, "lumen", False, 0, MACHINE, "", scope=scope))
    with pytest.raises(store.MarketingError) as refused:
        server._may_switch(SimpleNamespace(), agent, row, "autonomous")
    assert refused.value.status == 403 and "agent" in refused.value.message
    with pytest.raises(store.MarketingError):
        server._may_switch(SimpleNamespace(), agent, SimpleNamespace(account_kind="personal", owner_id=1), "autonomous")


# The gate


def test_marketing_gate_a_post_that_passes_every_layer_goes_out_with_no_human(mk):
    chan = github(mk)
    sent = propose(mk, chan)
    assert sent["state"] == "posted" and sent["posted_url"]
    assert set(sent["layers"]) == {"switches", "denylist", "source", "rules", "judge", "budget", "adapter"}
    assert all(layer["ok"] for layer in sent["layers"].values())
    assert mk.platform.sent[0].url == "https://api.github.com/repos/acme/widgets/releases"
    assert mk.platform.sent[0].headers["Authorization"] == f"Bearer {TOKEN}"
    assert mk.judge.calls == 1


def test_marketing_gate_a_draft_only_account_never_posts_by_itself(mk):
    chan = channel(mk, "github", "acme", target="acme/widgets", mode="draft_only")
    draft = propose(mk, chan)
    assert draft["state"] == "draft" and "draft_only" in draft["reason"] and mk.platform.sent == []


def test_marketing_gate_deny_list_is_checked_first_and_only_an_admin_edits_it(mk):
    c, h = mk.client, mk.h
    chan = github(mk)
    assert c.post(f"{BASE}/denylist", json={"kind": "term", "value": "crypto"}, headers=h["alice"]).status_code == 403
    assert c.post(f"{BASE}/denylist", json={"kind": "term", "value": "crypto"}, headers=h["owner"]).status_code == 201
    entries = c.get(f"{BASE}/denylist", headers=h["alice"]).json()
    assert [e["value"] for e in entries] == ["crypto"]
    blocked = propose(mk, chan, f"{GOOD} for crypto fans")
    assert (
        blocked["state"] == "draft"
        and "denied term" in blocked["reason"]
        and set(blocked["layers"]) == {"switches", "denylist"}
    )
    assert mk.judge.calls == 0 and mk.platform.sent == []
    assert c.delete(f"{BASE}/denylist/{entries[0]['id']}", headers=h["owner"]).json() == {"ok": True}
    assert propose(mk, chan, f"{GOOD} for crypto fans")["state"] == "posted"


def test_marketing_gate_source_rule_turns_an_unsourced_claim_into_a_draft(mk):
    chan = github(mk)
    draft = propose(mk, chan, "evo-agents is the best tool ever.")
    assert draft["state"] == "draft" and "link to docs, a release or the changelog" in draft["reason"]
    assert draft["layers"]["source"]["ok"] is False and "rules" not in draft["layers"]
    sourced = [
        {"quote": "evo-agents is the best tool ever", "url": "https://github.com/a/evo-agents/blob/main/docs/x.md"}
    ]
    assert propose(mk, chan, "evo-agents is the best tool ever.", sources=sourced)["state"] == "posted"


def test_marketing_gate_caps_and_gap_stop_a_channel_that_posts_too_often(mk):
    chan = github(mk)
    assert propose(mk, chan)["state"] == "posted"
    early = propose(mk, chan)
    assert early["state"] == "draft" and "post a day reached" in early["reason"]
    mk.clock.after(hours=25)
    assert propose(mk, chan)["state"] == "posted"
    mk.clock.after(hours=25)
    capped = propose(mk, chan)
    assert capped["state"] == "draft" and "2 posts a week" in capped["reason"]


def test_marketing_gate_reddit_keeps_self_promotion_under_a_tenth_of_what_the_account_posts(mk):
    chan = channel(mk, "reddit", "evo", target="r/python")
    rules(mk, {"reddit": {"min_gap_hours": 0, "posts_per_day": 100, "posts_per_week": 100}})
    first = propose(mk, chan)
    assert first["state"] == "draft" and "self-promotion" in first["reason"]
    for n in range(9):
        replied = propose(mk, chan, f"Answer {n} about asyncio", kind="reply", reply_to=f"t1_{n}")
        assert replied["state"] == "posted"
    assert propose(mk, chan)["state"] == "posted"
    assert propose(mk, chan)["state"] == "draft"


def test_marketing_gate_the_judge_fails_a_post_below_four_and_flags_one_near_the_line(mk):
    chan = github(mk)
    mk.judge.axes = {**PASSING, "truth": 2}
    low = propose(mk, chan)
    assert low["state"] == "withdrawn" and "truth" in low["reason"] and low["judge"]["ok"] is False
    assert mk.platform.sent == []
    assert sql(mk.db, select(func.count()).select_from(tables.notifications))[0][0] == 0
    mk.judge.axes = {**PASSING, "voice": 3}
    near = propose(mk, chan)
    assert near["state"] == "withdrawn" and near["judge"]["near_line"] is True
    notes = sql(mk.db, select(tables.notifications.c.title, tables.notifications.c.notice_kind))
    assert notes and all(kind == "plugin_notice" for _, kind in notes) and "3 on one axis" in notes[0][0]


def test_marketing_gate_a_channel_made_autonomous_lately_only_drafts_during_its_dry_run(mk):
    configure(mk.client, mk.h["owner"], "marketing", {"dry_run_hours": 72})
    chan = github(mk)
    dry = propose(mk, chan)
    assert dry["state"] == "draft" and dry["dry_run"] is True and "dry run" in dry["reason"]
    mk.clock.after(hours=80)
    assert propose(mk, chan)["state"] == "posted"


def test_marketing_gate_the_judge_that_answers_later_leaves_the_post_judging_until_the_job_runs(mk):
    chan = github(mk)

    async def later(conn, project, post, channel):
        return mk.judge.axes if mk.judge.calls else None

    mk.client.app.state.marketing_runtime = flow.Runtime(judge=later, transport=mk.platform, now=mk.clock)
    waiting = propose(mk, chan)
    assert waiting["state"] == "judging" and mk.platform.sent == []
    assert run_job(mk)["posts"] == {waiting["id"]: "judging"}
    mk.judge.calls = 1
    assert run_job(mk)["posts"] == {waiting["id"]: "posted"}
    done = mk.client.get(f"{BASE}/posts/{waiting['id']}", headers=mk.h["alice"]).json()
    assert done["state"] == "posted" and done["layers"]["judge"]["ok"]


# Budget


def test_marketing_budget_a_paid_channel_stops_when_the_month_is_spent_and_resumes_next_month(mk):
    assert status(mk)["budget"]["budget_usd"] == 50
    configure(mk.client, mk.h["owner"], "marketing", {"budget_usd": 0.015})
    paid = channel(mk, "x", "evo")
    free = github(mk)
    first = propose(mk, paid, "Hello")
    assert first["state"] == "posted" and first["cost_usd"] == pytest.approx(0.01)
    mk.clock.after(hours=5)
    second = propose(mk, paid, "Hello again")
    assert second["state"] == "draft" and "budget" in second["reason"] and second["layers"]["budget"]["ok"] is False
    assert propose(mk, free)["state"] == "posted"  # a free channel is never stopped by the budget
    assert status(mk)["budget"]["spent_usd"] == pytest.approx(0.01)
    mk.clock.after(days=31)
    assert propose(mk, paid, "A new month")["state"] == "posted"


def test_marketing_budget_a_spend_above_it_sets_the_kill_switch(mk):
    configure(mk.client, mk.h["owner"], "marketing", {"budget_usd": 0.005})
    paid = channel(mk, "x", "evo")
    assert propose(mk, paid, "Hello")["state"] == "draft"  # 0.01 does not fit 0.005: not even reserved
    assert status(mk)["killed"] is False


# The breaker and the kill switch


def test_marketing_breaker_two_failed_posts_in_a_row_open_it_and_only_an_admin_closes_it(mk):
    c, h = mk.client, mk.h
    chan = channel(mk, "x", "evo")
    mk.platform.status = 500
    assert propose(mk, chan, "one")["state"] == "failed"
    mk.clock.after(hours=5)
    assert propose(mk, chan, "two")["state"] == "failed"
    assert status(mk)["open_breakers"] == 1
    mk.platform.status = 200
    mk.clock.after(days=2)
    held = propose(mk, chan, "three")
    assert held["state"] == "draft" and "breaker of the channel is open" in held["reason"]
    assert c.post(f"{BASE}/channels/{chan['id']}/resume", headers=h["alice"]).status_code == 403
    assert c.post(f"{BASE}/channels/{chan['id']}/resume", headers=h["owner"]).json() == {"ok": True}
    assert propose(mk, chan, "four")["state"] == "posted"
    assert "marketing.breaker.open" in actions(mk.db) and "marketing.breaker.close" in actions(mk.db)
    titles = [t for (t,) in sql(mk.db, select(tables.notifications.c.title))]
    assert any("breaker open on x/evo" in t for t in titles)


def test_marketing_breaker_a_locked_account_opens_it_at_once_and_a_failure_costs_nothing(mk):
    chan = channel(mk, "x", "evo")
    mk.platform.status = 403
    assert propose(mk, chan, "one")["state"] == "failed"
    assert status(mk)["open_breakers"] == 1 and status(mk)["budget"]["spent_usd"] == 0


def test_marketing_breaker_a_report_about_a_post_opens_it_at_once(mk):
    chan = github(mk)
    sent = propose(mk, chan)
    reported = mk.client.post(
        f"{BASE}/posts/{sent['id']}/report", json={"reason": "spam complaint"}, headers=mk.h["alice"]
    )
    assert reported.json() == {"ok": True} and status(mk)["open_breakers"] == 1
    channels = mk.client.get(f"{BASE}/channels", headers=mk.h["eve"]).json()
    assert channels[0]["paused"] is True and "spam complaint" in channels[0]["paused_reason"]


def test_marketing_breaker_three_channels_open_within_a_day_set_the_kill_switch(mk):
    c, h = mk.client, mk.h
    chans = [github(mk), channel(mk, "x", "evo"), channel(mk, "linkedin", "evo", target="urn:li:person:1")]
    for chan in chans:
        paused = c.post(f"{BASE}/channels/{chan['id']}/pause", json={"reason": "check"}, headers=h["alice"])
        assert paused.json() == {"ok": True}
    now = status(mk)
    assert now["killed"] is True and "3 channels" in now["reason"] and now["open_breakers"] == 3
    assert "marketing.kill" in actions(mk.db)


def test_marketing_breaker_the_kill_switch_stops_every_channel_and_only_an_admin_clears_it(mk):
    c, h = mk.client, mk.h
    chan = github(mk)
    assert c.post(f"{BASE}/kill", json={"reason": "stop"}, headers=h["eve"]).json() == {"ok": True}
    assert c.post(f"{BASE}/kill", json={"reason": "again"}, headers=h["alice"]).json() == {"ok": False}
    stopped = propose(mk, chan)
    assert stopped["state"] == "draft" and "kill switch" in stopped["reason"] and mk.platform.sent == []
    assert c.post(f"{BASE}/resume", headers=h["alice"]).status_code == 403
    assert c.post(f"{BASE}/resume", headers=h["owner"]).json() == {"ok": True}
    assert status(mk)["killed"] is False and propose(mk, chan)["state"] == "posted"
    assert {"marketing.kill", "marketing.resume"} <= set(actions(mk.db))


def test_marketing_breaker_three_judge_failures_in_a_row_open_it(mk):
    chan = github(mk)
    mk.judge.axes = {**PASSING, "brand": 1}
    for _ in range(3):
        assert propose(mk, chan)["state"] == "withdrawn"
    assert status(mk)["open_breakers"] == 1


def test_marketing_breaker_a_reddit_score_of_zero_or_less_on_two_of_three_posts_opens_it(mk):
    chan = channel(mk, "reddit", "evo", target="r/python")
    rules(mk, {"reddit": {**OPEN_RULES, "self_promo_ratio": 1}})
    mk.platform.answers = {
        "/info": HttpResponse(200, {"data": {"children": [{"data": {"score": 0, "num_comments": 0}}]}}),
        "/submit": HttpResponse(
            200, {"json": {"errors": [], "data": {"url": "https://reddit.com/r/python/1", "name": "t3_1"}}}
        ),
    }
    for n in range(3):
        assert propose(mk, chan, f"Post {n}")["state"] == "posted"
        mk.clock.after(hours=1)
    mk.clock.after(hours=25)
    assert run_job(mk)["metrics"] > 0
    assert status(mk)["open_breakers"] == 1


# The audit


def test_marketing_audit_each_post_keeps_its_channel_account_content_link_run_layers_and_cost(mk):
    chan = channel(mk, "x", "evo")
    sent = propose(mk, chan, "Hello world", run_id=None)
    row = mk.client.get(f"{BASE}/posts/{sent['id']}", headers=mk.h["alice"]).json()
    assert (row["channel"], row["account"], row["body"], row["kind"]) == ("x", "evo", "Hello world", "post")
    assert row["posted_url"].startswith("https://x.com/evo/status/") and row["cost_usd"] == pytest.approx(0.01)
    assert row["judge"]["ok"] and set(row["layers"]) >= {"denylist", "source", "rules", "judge", "budget", "adapter"}
    assert "marketing.post.posted" in actions(mk.db)
    target = sql(mk.db, select(tables.audit.c.target).where(tables.audit.c.action == "marketing.post.posted"))[0][0]
    assert target == f"post:{sent['id']}"
    stored = sql(
        mk.db, select(marketing_tables.marketing_posts.c.proposed_by, marketing_tables.marketing_posts.c.external_id)
    )[0]
    assert stored[0] and stored[1]


def test_marketing_audit_a_failing_post_stays_as_a_draft_with_its_reason_and_the_layers_it_met(mk):
    chan = github(mk)
    draft = propose(mk, chan, "evo-agents rocks.")
    listed = mk.client.get(f"{BASE}/posts", params={"state": "draft"}, headers=mk.h["alice"]).json()
    assert [p["id"] for p in listed] == [draft["id"]] and listed[0]["reason"]
    assert mk.client.get(f"{BASE}/posts", params={"state": "posted"}, headers=mk.h["alice"]).json() == []
    assert "marketing.post.draft" in actions(mk.db)
    assert mk.client.get(f"{BASE}/posts", headers=mk.h["eve"]).status_code == 403


def test_marketing_a_credential_never_comes_back_in_an_answer_a_reason_or_the_audit(mk):
    chan = channel(mk, "x", "evo")
    mk.platform.status = 500
    seen = [propose(mk, chan, "one"), status(mk)]
    seen.append(mk.client.get(f"{BASE}/channels", headers=mk.h["alice"]).json())
    seen.append(mk.client.get(f"{BASE}/posts", headers=mk.h["alice"]).json())
    stored = sql(mk.db, select(marketing_tables.marketing_posts.c.reason, marketing_tables.marketing_posts.c.layers))
    assert TOKEN not in json.dumps([seen, stored, actions(mk.db)], default=str)
    assert mk.platform.sent and TOKEN not in repr(mk.platform.sent[0])


def test_marketing_a_post_without_a_credential_is_a_draft_that_says_so(client, members, hub_db):  # noqa: F811
    enable(client, members["owner"], "marketing")
    configure(client, members["owner"], "marketing", {"dry_run_hours": 0})
    mk = SimpleNamespace(h=members, platform=Platform(), judge=Judge(), clock=Clock(), client=client, db=hub_db)
    client.app.state.marketing_runtime = flow.Runtime(judge=mk.judge, transport=mk.platform, now=mk.clock)
    chan = channel(mk, "linkedin", "evo", target="urn:li:person:1")
    draft = propose(mk, chan)
    assert draft["state"] == "draft" and "no credential for linkedin" in draft["reason"]
    assert mk.platform.sent == []


def test_marketing_a_channel_with_no_adapter_yet_leaves_a_draft_for_a_person(mk):
    chan = channel(mk, "viblo", "evo")
    draft = propose(mk, chan)
    assert draft["state"] == "draft" and "viblo" in draft["reason"] and mk.platform.sent == []


# Numbers


def declare(db, owner: str, names: list[str]) -> None:
    project = sql(db, select(tables.projects.c.id).where(tables.projects.c.name == PROJECT))[0][0]
    user = sql(db, select(tables.users.c.id).where(tables.users.c.login == owner))[0][0]
    keys = [{"project_id": project, "name": n, "kind": "number", "created_by": user} for n in names]
    sql(db, insert(tables.signal_keys), keys)


def pushed(db) -> dict[str, list[float]]:
    s, k = tables.signals, tables.signal_keys
    found: dict[str, list[float]] = {}
    for name, value in sql(db, select(k.c.name, s.c.value).join_from(s, k, k.c.id == s.c.key_id).order_by(s.c.id)):
        found.setdefault(name, []).append(value)
    return found


def test_marketing_metrics_are_read_at_24h_and_7d_and_pushed_to_signal_once_declared(mk):
    chan = channel(mk, "x", "evo")
    mk.platform.answers = {
        "tweet.fields": HttpResponse(200, {"data": {"public_metrics": {"like_count": 3, "impression_count": 40}}}),
        "/2/tweets": HttpResponse(201, {"data": {"id": "99"}}),
    }
    assert propose(mk, chan, "Hello")["state"] == "posted"
    assert run_job(mk)["metrics"] == 0
    declare(
        mk.db,
        "owner",
        ["marketing.likes_24h", "marketing.likes_7d", "marketing.posts_week", "marketing.spend_usd_month"],
    )
    mk.clock.after(hours=25)
    done = run_job(mk)
    assert done["metrics"] == 2 and done["signals"]["pushed"]
    assert pushed(mk.db)["marketing.likes_24h"] == [3.0] and "marketing.likes_7d" not in pushed(mk.db)
    assert pushed(mk.db)["marketing.posts_week"] == [1.0]
    assert pushed(mk.db)["marketing.spend_usd_month"] == [pytest.approx(0.01)]
    assert run_job(mk)["metrics"] == 0
    mk.clock.after(days=7)
    assert run_job(mk)["metrics"] == 2
    assert pushed(mk.db)["marketing.likes_7d"] == [3.0] and pushed(mk.db)["marketing.likes_24h"] == [3.0]
    assert "tweet" not in json.dumps(sql(mk.db, select(tables.signals.c.text)))


def test_marketing_metrics_of_facebook_and_tiktok_posts_are_never_read(mk):
    page = channel(mk, "facebook_page", "evo", target="123")
    assert propose(mk, page)["state"] == "posted"
    mk.clock.after(days=8)
    before = len(mk.platform.sent)
    assert run_job(mk)["metrics"] == 0 and len(mk.platform.sent) == before


# A project that did not enable the plugin


def test_marketing_a_project_that_does_not_enable_it_writes_nothing_and_its_routes_answer_404(client, members, hub_db):  # noqa: F811
    engine = client.app.state.engine
    assert client.portal.call(lambda: job.run_project(engine, PROJECT)) == {}
    for table in (
        marketing_tables.marketing_channels,
        marketing_tables.marketing_posts,
        marketing_tables.marketing_budget,
        marketing_tables.marketing_state,
    ):
        assert sql(hub_db, select(func.count()).select_from(table))[0][0] == 0
    assert sql(hub_db, select(func.count()).select_from(tables.signal_ingest_keys))[0][0] == 0
    for headers in (members["owner"], members["alice"]):
        assert client.get(f"{BASE}/channels", headers=headers).status_code == 404
        assert client.get(f"{BASE}/status", headers=headers).status_code == 404
        assert client.post(f"{BASE}/kill", json={}, headers=headers).status_code == 404
    body = {"channel": "github", "account": "acme"}
    assert client.post(f"{BASE}/channels", json=body, headers=members["owner"]).status_code == 404
    assert (
        client.post(f"{BASE}/posts", json={"channel_id": 1, "body": "x"}, headers=members["alice"]).status_code == 404
    )


def test_marketing_the_manifest_declares_the_tables_the_job_the_judge_and_the_sealed_credentials(client):
    from evo_agents.core_plugins.marketing import MANIFEST

    assert MANIFEST["id"] == "marketing" and MANIFEST["tier"] == "core"
    secret = {name for name, field in MANIFEST["config"]["properties"].items() if field.get("x-secret")}
    assert {
        "github_token",
        "x_token",
        "linkedin_token",
        "reddit_token",
        "facebook_page_token",
        "tiktok_token",
    } <= secret
    assert MANIFEST["config"]["properties"]["budget_usd"]["default"] == 50
    assert [a["name"] for a in MANIFEST["contributes"]["agent_types"]] == ["marketing-judge", "marketer"]
    assert hub_api.plugin("evo_agents.core_plugins.marketing.settings").id == "marketing"


def test_marketing_tables_are_the_plugins_own_adopted_as_the_hub_made_them(hub_db):
    """The seven marketing tables live in the plugin's own metadata; its chain adopts the tables the hub's migrations
    made, which compare equal."""
    from evo_agents.core_plugins.marketing import MANIFEST

    assert MANIFEST["schema"] == {
        "metadata": "evo_agents.core_plugins.marketing.tables:metadata",
        "migrations": "evo_agents.core_plugins.marketing.migrations",
        "adopts": list(marketing_tables.NAMES),
    }
    with live.engine(hub_db).begin() as conn:
        assert hub_api.adopt_tables(conn, marketing_tables.metadata, list(marketing_tables.NAMES)) == []
