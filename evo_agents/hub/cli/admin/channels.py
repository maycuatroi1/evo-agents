"""``evo-agents hub admin telegram|telegram-unlink|channel-link|channel-unlink``: the hub's Telegram bot and the
channels members are linked by (``evo_agents.hub.server.admin``).

``telegram`` prints the bot and where its webhook points, and ``--set-webhook`` points it at this hub.
``telegram-unlink LOGIN`` unlinks a member's Telegram chat. ``channel-link LOGIN KIND KEY=VALUE...`` links a member's
chat of a core plugin's channel kind, with no web session of theirs (:func:`channel_config` reads the pairs), and
``channel-unlink LOGIN KIND`` unlinks it. The commands are classes of ``evo_agents.clikit`` over the gateway
``cli.admin.resources.HubAdmin``. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import argparse
import json
import sys

from evo_agents.clikit import EXIT_USAGE, Arg, Result, Text, returns_object
from evo_agents.hub.cli.admin.resources import HubAdmin
from evo_agents.hub.cli.base import HubCommand, HubContext, when

TELEGRAM_KEYS = (
    "configured",
    "bot",
    "expected_url",
    "url",
    "pending_update_count",
    "last_error_date",
    "last_error_message",
)
TELEGRAM_UNLINK_KEYS = ("login", "unlinked")
CHANNEL_LINK_KEYS = ("login", "kind", "config", "linked_by")
CHANNEL_UNLINK_KEYS = ("login", "kind", "unlinked")
MEMBER = Arg("login", metavar="LOGIN", help="the member's GitHub login")


def channel_config(pairs: list[str]) -> dict:
    """The config of ``KEY=VALUE`` (a string) and ``KEY:=JSON`` (a number, say) pairs; ValueError naming a bad one."""
    config: dict = {}
    for pair in pairs:
        key, equals, value = pair.partition("=")
        if not equals:
            raise ValueError(f"{pair}: write KEY=VALUE, or KEY:=JSON for a number")
        if key.endswith(":"):
            key = key[:-1]
            try:
                config[key] = json.loads(value)
            except ValueError:
                raise ValueError(f"{pair}: the value after := is not JSON") from None
        else:
            config[key] = value
        if not key:
            raise ValueError(f"{pair}: the key is empty")
    return config


def telegram_lines(found: dict, set_webhook: bool) -> list[str]:
    """What ``telegram`` prints of the bot the hub answered."""
    if not found["configured"]:
        return [
            "Telegram is not set up on this hub: set EVO_HUB_TELEGRAM_BOT_TOKEN and EVO_HUB_TELEGRAM_WEBHOOK_SECRET in "
            "its environment"
        ]
    lines = [
        f"Bot: @{found['bot']}",
        f"Webhook: {found['url'] or 'none'} (expected {found['expected_url'] or 'unknown: no EVO_HUB_PUBLIC_URL'})",
        f"Updates waiting at Telegram: {found['pending_update_count'] or 0}",
    ]
    if found["last_error_message"]:
        lines.append(f"Last error: {found['last_error_message']} at {when(found['last_error_date'])} UTC")
    if found["url"] != found["expected_url"] and not set_webhook:
        lines.append(
            "The webhook does not point at this hub: `evo-agents hub admin telegram --set-webhook` points it here"
        )
    return lines


class BadConfig(Exception):
    """A pair of ``channel-link`` that is no config: the command's name, the message and exit status 2."""


class Telegram(HubCommand):
    name = "telegram"
    help = "the hub's Telegram bot and where its webhook points"
    arguments = (
        Arg(
            "--set-webhook",
            action="store_true",
            help="point the webhook at this hub's public URL with the hub's secret token",
        ),
    )
    output = returns_object(*TELEGRAM_KEYS, schema="TelegramWebhook")

    def run(self, ctx: HubContext) -> Result:
        admin = HubAdmin(ctx.hub)
        found = admin.set_webhook() if ctx.args.set_webhook else admin.telegram()
        return Result(found, Text(*telegram_lines(found, ctx.args.set_webhook)))


class TelegramUnlink(HubCommand):
    name = "telegram-unlink"
    help = "unlink a member's Telegram chat, whatever the web session that linked it"
    arguments = (MEMBER,)
    output = returns_object(*TELEGRAM_UNLINK_KEYS, schema="TelegramUnlinked")

    def run(self, ctx: HubContext) -> Result:
        found = HubAdmin(ctx.hub).unlink_telegram(ctx.args.login)
        if found["unlinked"]:
            said = (
                f"Unlinked the Telegram chat of {found['login']}: the hub sends it nothing more and takes no answer "
                "from it."
            )
        else:
            said = f"{found['login']} has no Telegram chat linked."
        return Result(found, Text(said))


class ChannelLink(HubCommand):
    name = "channel-link"
    help = "link a member's chat of a core plugin's channel kind, with no web session of theirs"
    arguments = (
        MEMBER,
        Arg("kind", metavar="KIND", help="the channel's kind, as its core plugin names it"),
        Arg(
            "config",
            metavar="KEY=VALUE",
            nargs="+",
            help="the channel's config as its plugin keeps it: KEY=VALUE for a string, KEY:=JSON for a number",
        ),
        Arg(
            "--unique-on",
            action="append",
            default=[],
            metavar="KEY",
            help="a key that names one chat: another member's channel with the same value there goes (repeat it)",
        ),
    )
    output = returns_object(*CHANNEL_LINK_KEYS, schema="ChannelLinked")

    def check(self, args: argparse.Namespace) -> None:
        try:
            channel_config(args.config)
        except ValueError as exc:
            raise BadConfig(str(exc)) from None

    def failed(self, exc: Exception) -> int | None:
        if isinstance(exc, BadConfig):
            print(f"evo-agents hub admin channel-link: {exc}", file=sys.stderr)
            return EXIT_USAGE
        return super().failed(exc)

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = {"config": channel_config(args.config), "unique_on": args.unique_on}
        found = HubAdmin(ctx.hub).link_channel(args.login, args.kind, body)
        said = (
            f"Linked the {found['kind']} channel of {found['login']} as {found['linked_by']}: it speaks for them, with "
            "no web session of theirs, until it is unlinked or they link it again."
        )
        return Result(found, Text(said))


class ChannelUnlink(HubCommand):
    name = "channel-unlink"
    help = "unlink a member's channel of a core plugin's kind, whoever linked it"
    arguments = (MEMBER, Arg("kind", metavar="KIND", help="the channel's kind"))
    output = returns_object(*CHANNEL_UNLINK_KEYS, schema="ChannelUnlinked")

    def run(self, ctx: HubContext) -> Result:
        found = HubAdmin(ctx.hub).unlink_channel(ctx.args.login, ctx.args.kind)
        if found["unlinked"]:
            said = f"Unlinked the {found['kind']} channel of {found['login']}."
        else:
            said = f"{found['login']} has no {found['kind']} channel linked."
        return Result(found, Text(said))


TELEGRAM, TELEGRAM_UNLINK = Telegram(), TelegramUnlink()
CHANNEL_LINK, CHANNEL_UNLINK = ChannelLink(), ChannelUnlink()
