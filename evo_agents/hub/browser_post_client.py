"""The CLI's browser clients, ``client`` browser_post and browser_login of a plugin's command
(``evo_agents.hub.cli_plugin_commands``): the client half of a browser adapter, which the core plugin marketing has
(``evo-agents hub marketing browser-post|login``, ``docs/marketing-plugin.md`` of evo-agents-core-plugins, Browser adapter).

``login --project P CHANNEL_ID`` opens a visible Chromium on the login page of the account's channel; the member logs in
by hand (password, two-factor, captcha: the script never types or solves any of them), presses Enter, and the session
(Playwright's storage state, cut down to the channel's cookies) is sealed into the plugin's config field
``<channel>_session`` and marked ``ok``. ``browser-post --project P`` claims the oldest queued post, posts it with the
deterministic script of ``evo_agents.hub.browser_post`` and reports the outcome and a screenshot; it never retries,
and it runs on a machine with Playwright and Chromium, as the member's own login (the scope of an agent's run is refused
by the hub). The plugin's routes are ``browser/claim``, ``browser/<post_id>/result``, ``channels`` and
``channels/<id>/session`` below ``/v1/x/<plugin>/<project>``. Standard library only here: Playwright is imported when a
page opens.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from urllib.parse import quote

from evo_agents.hub.browser_post import PROFILES, Post, run_post, trim_session
from evo_agents.hub.browser_post_pw import BrowserUnavailable, open_page
from evo_agents.hub.cli_client import signed_in
from evo_agents.hub.client import HubError

PLUGIN = "marketing"
OpenPage = Callable[..., object]


def _path(plugin: str, project: str, *parts: str) -> str:
    return "/".join([f"/v1/x/{quote(plugin, safe='')}/{quote(project, safe='')}", *(quote(p, safe="") for p in parts)])


def post_next(hub, project: str, opener: OpenPage = open_page, plugin: str = PLUGIN) -> dict:
    """Claim one queued post, post it and report. ``{"job": None}`` when nothing waits, else what the hub answered to the
    report. A script that breaks reports ``failed`` with the kind of error; a hub that cannot be told is an error."""
    claimed = hub.call("POST", _path(plugin, project, "browser", "claim"), {})
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
    return hub.call("POST", _path(plugin, project, "browser", str(job["post_id"]), "result"), body)


def log_in(
    hub,
    project: str,
    channel_id: int,
    opener: OpenPage = open_page,
    wait: Callable[[str], object] = input,
    plugin: str = PLUGIN,
) -> dict:
    """Open the login page of the channel, wait for the member, seal the session and mark it ``ok``."""
    found = next((c for c in hub.call("GET", _path(plugin, project, "channels")) if c["id"] == channel_id), None)
    if found is None:
        raise HubError(f"no channel {channel_id} in project {project}: see `evo-agents hub {plugin} channels`")
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
    hub.call(
        "POST",
        f"/v1/projects/{quote(project, safe='')}/plugins/{quote(plugin, safe='')}/config",
        {"values": {field: sealed}},
    )
    return hub.call("POST", _path(plugin, project, "channels", str(channel_id), "session"), {"state": "ok"})


def post_command(args, plugin: str) -> tuple[dict, str]:
    """``client`` browser_post: (the hub's answer to the report, the line that says it)."""
    hub, _ = signed_in()
    try:
        found = post_next(hub, args.project, plugin=plugin)
    except BrowserUnavailable as exc:
        raise HubError(str(exc)) from None
    if found.get("state") is None:
        return found, "Nothing waits for the browser"
    return found, f"Post is {found['state']}" + (f": {found['reason']}" if found.get("reason") else "")


def login_command(args, plugin: str) -> tuple[dict, str]:
    """``client`` browser_login: (the channel with its session marked, the line that says it)."""
    hub, _ = signed_in()
    try:
        found = log_in(hub, args.project, args.channel_id, plugin=plugin)
    except BrowserUnavailable as exc:
        raise HubError(str(exc)) from None
    return found, f"The session of channel {args.channel_id} is sealed and marked {found['session_state']}"
