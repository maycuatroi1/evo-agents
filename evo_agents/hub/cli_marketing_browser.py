"""``evo-agents hub marketing login|browser-post``: the client half of the browser adapter of the core plugin marketing
(``docs/marketing-plugin.md``, Browser adapter).

``login --project P CHANNEL_ID`` opens a visible Chromium on the login page of the account's channel; the member logs in
by hand (password, two-factor, captcha: the script never types or solves any of them), presses Enter, and the session
(Playwright's storage state, cut down to the channel's cookies) is sealed into the plugin's config field
``<channel>_session`` and marked ``ok``. ``browser-post --project P`` claims the oldest queued post, posts it with the
deterministic script of ``evo_agents.hub.marketing_browser`` and reports the outcome and a screenshot; it never retries,
and it runs on a machine with Playwright and Chromium, as the member's own login (the scope of an agent's run is refused
by the hub). Standard library only here: Playwright is imported when a page opens.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from urllib.parse import quote

from evo_agents.hub.cli_client import client_command, print_json, signed_in
from evo_agents.hub.cli_marketing import CHANNEL_KEYS
from evo_agents.hub.cli_marketing import path as _path
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.hub.marketing_browser import PROFILES, Post, run_post, trim_session
from evo_agents.hub.marketing_browser_pw import BrowserUnavailable, open_page

PLUGIN = "marketing"
RESULT_KEYS = ("state", "reason")
CLAIM_KEYS = ("job",)
OpenPage = Callable[..., object]


def post_next(hub, project: str, opener: OpenPage = open_page) -> dict:
    """Claim one queued post, post it and report. ``{"job": None}`` when nothing waits, else what the hub answered to the
    report. A script that breaks reports ``failed`` with the kind of error; a hub that cannot be told is an error."""
    claimed = hub.call("POST", _path(project, "browser", "claim"), {})
    job = claimed.get("job") if isinstance(claimed, dict) else None
    if not job:
        return {"job": None}
    profile = PROFILES.get(job["channel"])
    try:
        if profile is None:
            raise LookupError(f"no profile for {job['channel']}")
        with opener(job["session"]) as (page, _context):
            result = run_post(page, profile, Post(job["channel"], job["body"], job["target"]))
        body = {"outcome": result.outcome, "url": result.url, "detail": result.detail}
        if result.screenshot:
            body["screenshot"] = base64.b64encode(result.screenshot).decode()
    except BrowserUnavailable:
        raise
    except Exception as exc:  # the script never retries: the hub learns it failed, and why in a word
        body = {"outcome": "failed", "url": None, "detail": f"the script stopped: {type(exc).__name__}"}
    return hub.call("POST", _path(project, "browser", str(job["post_id"]), "result"), body)


def log_in(
    hub, project: str, channel_id: int, opener: OpenPage = open_page, wait: Callable[[str], object] = input
) -> dict:
    """Open the login page of the channel, wait for the member, seal the session and mark it ``ok``."""
    found = next((c for c in hub.call("GET", _path(project, "channels")) if c["id"] == channel_id), None)
    if found is None:
        raise HubError(f"no channel {channel_id} in project {project}: see `evo-agents hub marketing channels`")
    profile = PROFILES.get(found["channel"])
    if found["adapter"] != "browser" or profile is None:
        raise HubError(f"channel {channel_id} posts through its API: it has no browser session to log in")
    with opener(None, headless=False) as (page, context):
        page.goto(profile.origin + profile.login)
        wait(f"Log in to {found['channel']} as {found['account']} in the browser, then press Enter here: ")
        state = context.storage_state()
    try:
        sealed = trim_session(state, profile.domains)
    except ValueError as exc:
        raise HubError(str(exc)) from None
    field = f"{found['channel']}_session"
    hub.call("POST", f"/v1/projects/{quote(project, safe='')}/plugins/{PLUGIN}/config", {"values": {field: sealed}})
    return hub.call("POST", _path(project, "channels", str(channel_id), "session"), {"state": "ok"})


@client_command
def cmd_browser_post(args) -> int:
    hub, _ = signed_in()
    try:
        found = post_next(hub, args.project)
    except BrowserUnavailable as exc:
        raise HubError(str(exc)) from None
    if args.json:
        print_json(found)
    elif found.get("state") is None:
        print("Nothing waits for the browser")
    else:
        print(f"Post is {found['state']}" + (f": {found['reason']}" if found.get("reason") else ""))
    return 0


@client_command
def cmd_login(args) -> int:
    hub, _ = signed_in()
    try:
        found = log_in(hub, args.project, args.channel_id)
    except BrowserUnavailable as exc:
        raise HubError(str(exc)) from None
    if args.json:
        print_json(found)
    else:
        print(f"The session of channel {args.channel_id} is sealed and marked {found['session_state']}")
    return 0


def register_browser(msub, command) -> None:
    posting = command(
        "browser-post", "post the oldest queued post of a browser channel (needs Chromium)", cmd_browser_post
    )
    json_option(posting, returns_object(*RESULT_KEYS, schema="MarketingBrowserResult"))
    login = command("login", "log in once in a browser and seal the session of a channel", cmd_login)
    login.add_argument("channel_id", type=int, metavar="CHANNEL_ID")
    json_option(login, returns_object(*CHANNEL_KEYS, schema="MarketingChannel"))
