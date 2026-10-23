"""``evo-agents hub marketing channels|posts|pause|resume|kill|login|browser-post``: the channels and posts of a project that enabled the
core plugin marketing, through its routes under ``/v1/x/marketing/<project>`` (``docs/marketing-plugin.md``). The
plugin's code is ``evo_agents.core_plugins.marketing``, which this client never imports: it speaks HTTP only.

``channels --project X`` lists the accounts with their mode and breaker, ``posts --project X`` the posts with the state
and the reason of each (``--state`` narrows it). ``pause --project X CHANNEL_ID`` opens the breaker of one channel,
``resume --project X [CHANNEL_ID]`` closes it (an admin does; without an id it clears the kill switch of the project)
and ``kill --project X`` stops every channel. ``--json`` prints what the hub answered. Standard library only.
"""

from __future__ import annotations

from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table
from evo_agents.hub.contract import json_option, returns_array, returns_object

CHANNEL_KEYS = (
    "id",
    "channel",
    "account",
    "kind",
    "mode",
    "adapter",
    "owner",
    "target",
    "paused",
    "paused_reason",
    "autonomous_since",
    "session_state",
)
POST_KEYS = (
    "id",
    "channel_id",
    "channel",
    "account",
    "kind",
    "body",
    "links",
    "state",
    "reason",
    "layers",
    "judge",
    "run_id",
    "posted_url",
    "cost_usd",
    "dry_run",
    "created_at",
    "posted_at",
)
DONE_KEYS = ("ok",)
STATES = ("proposed", "judging", "queued", "posting", "posted", "draft", "failed", "withdrawn")


def path(project: str, *tail: str, **query: str | None) -> str:
    path = "/".join(["/v1/x/marketing", quote(project, safe=""), *tail])
    asked = {name: value for name, value in query.items() if value}
    return f"{path}?{urlencode(asked)}" if asked else path


@client_command
def cmd_channels(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", path(args.project, "channels"))
    if args.json:
        print_json(found)
        return 0
    rows = [
        (c["id"], c["channel"], c["account"], c["kind"], c["mode"], "open" if c["paused"] else "closed") for c in found
    ]
    if rows:
        table(("id", "channel", "account", "kind", "mode", "breaker"), rows)
    return 0


@client_command
def cmd_posts(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", path(args.project, "posts", state=args.state))
    if args.json:
        print_json(found)
        return 0
    rows = [
        (p["id"], p["channel"], p["account"], p["state"], (p["reason"] or p["posted_url"] or "")[:70]) for p in found
    ]
    if rows:
        table(("id", "channel", "account", "state", "reason or link"), rows)
    return 0


def _done(args, found: dict, said: str) -> int:
    if args.json:
        print_json(found)
    else:
        print(said if found["ok"] else "Nothing changed: it was so already")
    return 0


@client_command
def cmd_pause(args) -> int:
    hub, _ = signed_in()
    found = hub.call("POST", path(args.project, "channels", str(args.channel_id), "pause"), {"reason": args.reason})
    return _done(args, found, f"Breaker of channel {args.channel_id} is open")


@client_command
def cmd_resume(args) -> int:
    hub, _ = signed_in()
    if args.channel_id is None:
        found = hub.call("POST", path(args.project, "resume"), {})
        return _done(args, found, "The kill switch is cleared")
    found = hub.call("POST", path(args.project, "channels", str(args.channel_id), "resume"), {})
    return _done(args, found, f"Breaker of channel {args.channel_id} is closed")


@client_command
def cmd_kill(args) -> int:
    hub, _ = signed_in()
    found = hub.call("POST", path(args.project, "kill"), {"reason": args.reason})
    return _done(args, found, "The kill switch is set: no channel posts")


def register_marketing(hsub) -> None:
    marketing = hsub.add_parser("marketing", help="the channels and posts of a project (plugin marketing)")
    msub = marketing.add_subparsers(dest="marketing_command", required=True)

    def command(name: str, help: str, func, *, reason: bool = False):
        parser = msub.add_parser(name, help=help)
        parser.add_argument(
            "--project", required=True, metavar="PROJECT", help="the project that enabled plugin marketing"
        )
        if reason:
            parser.add_argument("--reason", default="stopped by hand", help="why, kept in the audit")
        parser.set_defaults(func=func)
        return parser

    channels = command("channels", "the accounts with their mode and breaker", cmd_channels)
    json_option(channels, returns_array(*CHANNEL_KEYS, schema="MarketingChannel"))
    posts = command("posts", "the posts with their state and reason", cmd_posts)
    posts.add_argument("--state", choices=STATES, help="only posts in this state")
    json_option(posts, returns_array(*POST_KEYS, schema="MarketingPost"))
    pause = command("pause", "open the breaker of one channel", cmd_pause, reason=True)
    pause.add_argument("channel_id", type=int, metavar="CHANNEL_ID")
    json_option(pause, returns_object(*DONE_KEYS, schema="MarketingDone"))
    resume = command("resume", "close the breaker of a channel, or clear the kill switch (admin)", cmd_resume)
    resume.add_argument(
        "channel_id", type=int, nargs="?", metavar="CHANNEL_ID", help="the channel; none clears the kill switch"
    )
    json_option(resume, returns_object(*DONE_KEYS, schema="MarketingDone"))
    kill = command("kill", "stop every channel of the project", cmd_kill, reason=True)
    json_option(kill, returns_object(*DONE_KEYS, schema="MarketingDone"))
    from evo_agents.hub.cli_marketing_browser import register_browser

    register_browser(msub, command)
