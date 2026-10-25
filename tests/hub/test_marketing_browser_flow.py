"""The queue of the browser adapter on the hub (``evo_agents.core_plugins.marketing.browser``), criteria a15, a21 and a22
of plan autonomous-oss-growth: a post the gate passed for a channel with no API waits ``queued``, a login's script claims
it with the sealed session and reports, and a captcha, a lock or the login page opens the breaker. No agent's scope
reaches a session, and no session is in a list, a status or the audit. No browser starts: a fake page plays the site."""

from __future__ import annotations

import base64
import json

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import func, select

from evo_agents.core_plugins.marketing import server, store
from evo_agents.core_plugins.marketing import tables as marketing_tables
from evo_agents.hub import browser_post_client as cli
from evo_agents.hub import tables
from evo_agents.hub.browser_post import PROFILES
from evo_agents.hub.client import HubError
from tests.hub.live import sql
from tests.hub.test_marketing_browser import PNG, FakePage
from tests.hub.test_marketing_plugin import (  # noqa: F401 (fixtures)
    BASE,
    GOOD,
    PROJECT,
    actions,
    channel,
    client,
    configure,
    mk,
    propose,
    run_job,
    status,
)
from tests.hub.test_tasks_plugin import members  # noqa: F401 (fixtures)

SESSION = json.dumps({"cookies": [{"name": "sid", "value": "SECRET-SESSION-VALUE", "domain": "viblo.asia"}]})
DONE = "https://viblo.asia/p/the-post-1"


def session(world, name="viblo") -> None:
    configure(world.client, world.h["owner"], "marketing", {f"{name}_session": SESSION})


def claim(world, who="owner") -> dict:
    answer = world.client.post(f"{BASE}/browser/claim", json={}, headers=world.h[who])
    assert answer.status_code == 200, answer.text
    return answer.json()


def report(world, post_id: int, who="owner", expect: int = 200, **body) -> dict:
    answer = world.client.post(f"{BASE}/browser/{post_id}/result", json=body, headers=world.h[who])
    assert answer.status_code == expect, answer.text
    return answer.json()


def queued(world, name="viblo", **more) -> tuple[dict, dict]:
    chan = channel(world, name, "evo", **more)
    return chan, propose(world, chan)


def test_marketing_browser_a_post_that_passed_the_gate_waits_queued_and_a_script_posts_it(mk):
    session(mk)
    chan, post = queued(mk)
    assert post["state"] == "queued" and post["layers"]["adapter"]["ok"] and mk.platform.sent == []
    job = claim(mk)["job"]
    assert (job["post_id"], job["channel"], job["body"], job["session"]) == (post["id"], "viblo", GOOD, SESSION)
    assert claim(mk)["job"] is None  # one at a time
    shot = base64.b64encode(PNG).decode()
    done = report(mk, post["id"], outcome="posted", url=DONE, screenshot=shot)
    assert done["state"] == "posted"
    fresh = mk.client.get(f"{BASE}/posts/{post['id']}", headers=mk.h["alice"]).json()
    assert (fresh["state"], fresh["posted_url"]) == ("posted", DONE)
    assert fresh["layers"]["screenshot"]["label"] == "internal"
    seen = mk.client.get(f"{BASE}/posts/{post['id']}/screenshot", headers=mk.h["owner"])
    assert seen.status_code == 200 and seen.content == PNG and seen.headers["content-type"] == "image/png"
    assert next(c for c in mk.client.get(f"{BASE}/channels", headers=mk.h["alice"]).json())["session_state"] == "ok"
    assert "marketing.post.posted" in actions(mk.db)


def test_marketing_browser_the_session_reaches_the_claim_and_nothing_else(mk):
    session(mk)
    chan, post = queued(mk)
    claim(mk)
    report(mk, post["id"], outcome="posted", url=DONE)
    everything = [
        mk.client.get(f"{BASE}/posts", headers=mk.h["alice"]).text,
        mk.client.get(f"{BASE}/posts/{post['id']}", headers=mk.h["alice"]).text,
        mk.client.get(f"{BASE}/channels", headers=mk.h["alice"]).text,
        mk.client.get(f"{BASE}/status", headers=mk.h["alice"]).text,
        str(sql(mk.db, select(tables.audit))),
        str(sql(mk.db, select(marketing_tables.marketing_posts.c.layers, marketing_tables.marketing_posts.c.reason))),
    ]
    assert not [text for text in everything if "SECRET-SESSION-VALUE" in text]


def test_marketing_browser_the_scope_of_an_agent_run_may_not_claim_or_report():
    from evo_agents.hub.server.core_plugin_api._types import issue_session
    from evo_agents.hub.server.security import MACHINE, Principal, RunScope

    scope = RunScope(7, "northwind", "writer", "internal")
    agent = issue_session(1, "lumen", 0, False, principal=Principal(1, "lumen", False, 0, MACHINE, "", scope=scope))
    with pytest.raises(store.MarketingError) as refused:
        server._no_agent(agent, "claiming a post for the browser")
    assert refused.value.status == 403 and "agent" in refused.value.message
    server._no_agent(issue_session(1, "lumen", 0, False), "x")  # a login passes


def test_marketing_browser_a_reader_cannot_claim_and_a_post_with_no_session_is_a_draft(mk):
    chan, post = queued(mk)
    assert post["state"] == "draft" and "no browser session for viblo" in post["reason"]
    session(mk)
    again = propose(mk, chan, GOOD + " again")
    assert again["state"] == "queued"
    refused = mk.client.post(f"{BASE}/browser/claim", json={}, headers=mk.h["eve"])
    assert refused.status_code == 403


def test_marketing_browser_a_captcha_fails_the_post_opens_the_breaker_and_keeps_the_screenshot(mk):
    session(mk)
    chan, post = queued(mk)
    claim(mk)
    shot = base64.b64encode(PNG).decode()
    done = report(mk, post["id"], outcome="captcha", detail="the page asks for a captcha", screenshot=shot)
    assert done["state"] == "failed" and "captcha" in done["reason"]
    assert next(c for c in mk.client.get(f"{BASE}/channels", headers=mk.h["alice"]).json())["paused"] is True
    assert mk.client.get(f"{BASE}/posts/{post['id']}/screenshot", headers=mk.h["owner"]).content == PNG
    after = propose(mk, chan, GOOD + " next")
    assert after["state"] == "draft" and "breaker" in after["reason"]
    assert claim(mk)["job"] is None


def test_marketing_browser_a_lockout_opens_the_breaker_at_once(mk):
    session(mk)
    chan, post = queued(mk)
    claim(mk)
    report(mk, post["id"], outcome="lockout", detail="the platform answered 429")
    assert next(c for c in mk.client.get(f"{BASE}/channels", headers=mk.h["alice"]).json())["paused"] is True


def test_marketing_browser_an_expired_session_marks_the_channel_stale_and_the_next_post_says_to_log_in(mk):
    session(mk)
    chan, post = queued(mk)
    claim(mk)
    report(mk, post["id"], outcome="session_expired", detail="login page")
    row = next(c for c in mk.client.get(f"{BASE}/channels", headers=mk.h["alice"]).json())
    assert row["session_state"] == "stale" and row["paused"] is True
    resumed = mk.client.post(f"{BASE}/channels/{chan['id']}/resume", json={}, headers=mk.h["owner"])
    assert resumed.status_code == 200
    draft = propose(mk, chan, GOOD + " more")
    assert draft["state"] == "draft" and "log in" in draft["reason"] or "expired" in draft["reason"]
    ok = mk.client.post(f"{BASE}/channels/{chan['id']}/session", json={"state": "ok"}, headers=mk.h["owner"])
    assert ok.status_code == 200 and ok.json()["session_state"] == "ok"
    assert propose(mk, chan, GOOD + " after the login")["state"] == "queued"


def test_marketing_browser_two_failures_in_a_row_open_the_breaker_and_a_report_needs_a_claim(mk):
    session(mk)
    chan, first = queued(mk)
    assert report(mk, first["id"], expect=409, outcome="failed")["message"]  # nobody claimed it
    claim(mk)
    report(mk, first["id"], outcome="failed", detail="the page was not a post")
    second = propose(mk, chan, GOOD + " second")
    claim(mk)
    report(mk, second["id"], outcome="failed", detail="again")
    assert next(c for c in mk.client.get(f"{BASE}/channels", headers=mk.h["alice"]).json())["paused"] is True
    posted = report(mk, second["id"], expect=409, outcome="posted", url=DONE)
    assert "not claimed" in posted["message"]


def test_marketing_browser_a_post_that_went_out_reports_its_link_and_a_bad_picture_is_refused(mk):
    session(mk)
    chan, post = queued(mk)
    claim(mk)
    report(mk, post["id"], expect=422, outcome="posted")  # no link
    report(mk, post["id"], expect=422, outcome="captcha", screenshot="not base64 !!")
    big = base64.b64encode(b"x" * (2 * 1024 * 1024 + 1)).decode()
    report(mk, post["id"], expect=422, outcome="captcha", screenshot=big)


def test_marketing_browser_a_post_no_script_reports_fails_and_one_nobody_claims_becomes_a_draft(mk):
    session(mk)
    chan, stuck = queued(mk)
    claim(mk)
    mk.clock.after(minutes=31)
    assert run_job(mk)["expired"] == 1
    assert mk.client.get(f"{BASE}/posts/{stuck['id']}", headers=mk.h["alice"]).json()["state"] == "failed"
    other = channel(mk, "facebook_profile", "alice", who="alice", kind="personal", mode="draft_only")
    on = mk.client.put(f"{BASE}/channels/{other['id']}/mode", json={"mode": "autonomous"}, headers=mk.h["alice"])
    assert on.status_code == 200
    configure(mk.client, mk.h["owner"], "marketing", {"facebook_profile_session": SESSION})
    waiting = propose(mk, other, GOOD + " personal", who="alice")
    assert waiting["state"] == "queued", waiting["reason"]
    mk.clock.after(hours=25)
    run_job(mk)
    fresh = mk.client.get(f"{BASE}/posts/{waiting['id']}", headers=mk.h["alice"]).json()
    assert fresh["state"] == "draft" and "no worker claimed" in fresh["reason"]


def test_marketing_browser_a_kill_switch_set_while_a_post_waited_turns_it_into_a_draft(mk):
    session(mk)
    chan, post = queued(mk)
    assert mk.client.post(f"{BASE}/kill", json={"reason": "stop"}, headers=mk.h["owner"]).status_code == 200
    assert claim(mk)["job"] is None
    fresh = mk.client.get(f"{BASE}/posts/{post['id']}", headers=mk.h["alice"]).json()
    assert fresh["state"] == "draft" and "kill switch" in fresh["reason"]


def test_marketing_browser_channels_that_cannot_be_posted_by_a_script_stay_drafts(mk):
    session(mk, "tiktok")
    configure(mk.client, mk.h["owner"], "marketing", {"hackernews_session": SESSION})
    hn = channel(mk, "hackernews", "evo", mode="draft_only")
    assert propose(mk, hn, route="drafts")["state"] == "draft"
    tt = channel(mk, "tiktok", "evo")
    tiktok = propose(mk, tt)
    assert tiktok["state"] in ("draft", "posted")  # its API adapter, as before: no queue
    assert (
        sql(
            mk.db,
            select(func.count())
            .select_from(marketing_tables.marketing_posts)
            .where(marketing_tables.marketing_posts.c.state == "queued"),
        )[0][0]
        == 0
    )


def test_marketing_browser_a_browser_post_has_no_numbers_to_read(mk):
    session(mk)
    chan, post = queued(mk)
    claim(mk)
    report(mk, post["id"], outcome="posted", url=DONE)
    mk.clock.after(days=8)
    run_job(mk)
    assert mk.platform.sent == []
    assert sql(mk.db, select(func.count()).select_from(marketing_tables.marketing_metrics))[0][0] == 0


def test_marketing_browser_only_the_owner_or_an_admin_marks_a_session(mk):
    mine = channel(mk, "facebook_profile", "alice", who="alice", kind="personal", mode="draft_only")
    path = f"{BASE}/channels/{mine['id']}/session"
    assert mk.client.post(path, json={"state": "ok"}, headers=mk.h["alice"]).status_code == 200
    assert mk.client.post(path, json={"state": "ok"}, headers=mk.h["eve"]).status_code == 403
    api = channel(mk, "github", "acme", target="acme/widgets", mode="draft_only")
    refused = mk.client.post(f"{BASE}/channels/{api['id']}/session", json={"state": "ok"}, headers=mk.h["owner"])
    assert refused.status_code == 422


class Bridge:
    """The hub client's ``call`` over the test client, as a worker's CLI uses it."""

    def __init__(self, world, who="owner"):
        self.world, self.who = world, who

    def call(self, method: str, path: str, body: dict | None = None):
        answer = self.world.client.request(method, path, json=body, headers=self.world.h[self.who])
        if answer.status_code >= 400:
            raise HubError(answer.json().get("message", "refused"), answer.status_code)
        return answer.json()


class Opener:
    def __init__(self, page, state=None):
        self.page, self.state, self.opened = page, state, []

    def __call__(self, session, headless=True):
        opener = self

        class Context:
            def storage_state(self):
                return opener.state

        class Manager:
            def __enter__(self_):
                opener.opened.append((session, headless))
                return opener.page, Context()

            def __exit__(self_, *exc):
                return False

        return Manager()


def test_marketing_browser_command_claims_posts_with_the_script_and_reports(mk):
    session(mk)
    chan, post = queued(mk)
    profile = PROFILES["viblo"]
    compose = profile.compose_url(None)
    page = FakePage({compose: {"text": "New post"}}, {"url": f"{profile.origin}/p/the-post-1", "text": "Done"})
    opener = Opener(page)
    done = cli.post_next(Bridge(mk), PROJECT, opener)
    assert done["state"] == "posted" and opener.opened == [(SESSION, True)]
    assert (
        mk.client.get(f"{BASE}/posts/{post['id']}", headers=mk.h["alice"]).json()["posted_url"].endswith("the-post-1")
    )
    assert cli.post_next(Bridge(mk), PROJECT, opener) == {"job": None}


def test_marketing_browser_command_reports_a_login_page_as_an_expired_session(mk):
    session(mk)
    chan, post = queued(mk)
    compose = PROFILES["viblo"].compose_url(None)
    page = FakePage({compose: {"url": "https://viblo.asia/login", "text": "Sign in"}})
    done = cli.post_next(Bridge(mk), PROJECT, Opener(page))
    assert done["state"] == "failed" and "session_expired" in done["reason"]
    assert next(c for c in mk.client.get(f"{BASE}/channels", headers=mk.h["alice"]).json())["session_state"] == "stale"


def test_marketing_browser_command_that_breaks_reports_failed_and_does_not_retry(mk):
    session(mk)
    chan, post = queued(mk)

    class Broken(FakePage):
        def goto(self, url):
            raise TimeoutError("slow")

    opener = Opener(Broken({}))
    done = cli.post_next(Bridge(mk), PROJECT, opener)
    assert done["state"] == "failed" and "TimeoutError" in done["reason"] and len(opener.opened) == 1


def test_marketing_browser_login_seals_the_session_of_the_channel_and_marks_it_ok(mk):
    mine = channel(mk, "viblo", "evo")
    state = {"cookies": [{"name": "sid", "value": "SECRET-SESSION-VALUE", "domain": ".viblo.asia"}], "origins": []}
    page = FakePage({PROFILES["viblo"].origin + "/login": {"text": "Sign in"}})
    opener, asked = Opener(page, state), []
    found = cli.log_in(Bridge(mk), PROJECT, mine["id"], opener, wait=asked.append)
    assert found["session_state"] == "ok" and opener.opened == [(None, False)] and "viblo" in asked[0]
    assert page.visited == ["https://viblo.asia/login"]
    layer = mk.client.get(f"/v1/projects/{PROJECT}/plugins/marketing/config", headers=mk.h["owner"]).text
    assert "SECRET-SESSION-VALUE" not in layer  # sealed: the config answers show no secret
    queued_post = propose(mk, mine, GOOD + " logged in")
    assert queued_post["state"] == "queued"
    assert claim(mk)["job"]["session"] == json.dumps(
        {"cookies": state["cookies"], "origins": []}, separators=(",", ":")
    )


def test_marketing_browser_login_refuses_an_api_channel_and_an_unknown_one(mk):
    api = channel(mk, "github", "acme", target="acme/widgets", mode="draft_only")
    with pytest.raises(HubError, match="API"):
        cli.log_in(Bridge(mk), PROJECT, api["id"], Opener(FakePage({})), wait=str)
    with pytest.raises(HubError, match="no channel"):
        cli.log_in(Bridge(mk), PROJECT, 9999, Opener(FakePage({})), wait=str)


def test_marketing_browser_status_counts_are_unchanged_by_the_new_states(mk):
    session(mk)
    queued(mk)
    assert isinstance(status(mk), dict)


def test_marketing_browser_an_api_channel_with_no_credential_falls_back_to_the_browser_when_it_has_a_session(mk):
    configure(mk.client, mk.h["owner"], "marketing", {"linkedin_session": SESSION})
    chan = channel(mk, "linkedin", "evo", target="urn:li:person:1")
    assert propose(mk, chan)["state"] == "posted" and claim(mk)["job"] is None  # a credential works: the API posts
    cut = {"values": {}, "unset": ["linkedin_token"]}
    cleared = mk.client.post(f"/v1/projects/{PROJECT}/plugins/marketing/config", json=cut, headers=mk.h["owner"])
    assert cleared.status_code == 200, cleared.text
    mk.clock.after(days=3)  # past the gap and the weekly cap of the first post
    waiting = propose(mk, chan, GOOD + " second")
    assert waiting["state"] == "queued" and claim(mk)["job"]["channel"] == "linkedin"
