"""``hub market list|show|install|request``, and ``hub plugin install --github`` (:func:`install_github`): the plugins
of the markets the hub reads, and installing them (docs/market.md).

:func:`trusting` is the one flow of a signer the hub does not trust yet, which ``market install``, ``market buy`` and
``plugin install --github`` share: asked once at a terminal, or trusted with ``--trust``. Standard library only, like
the rest of the client.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from evo_agents.clikit import Arg, Result, Table, Text, Views, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.market.resources import (
    DETAIL_KEYS,
    INSTALLED_KEYS,
    LIST_KEYS,
    REQUEST_KEYS,
    GithubInstalls,
    Market,
    ProjectPlugins,
)
from evo_agents.hub.client import HubError

STATUS_TEXT = {
    "not_installed": "not installed",
    "installed": "installed",
    "upgrade": "upgrade waiting",
    "in_image": "in the hub's image",
    "not_in_image": "not in the hub's image",
    "needs_license": "needs a licence",
    "unavailable": "no release that reads",
}
YES = ("y", "yes")
UNTRUSTED = "untrusted publisher"
LIST_HEADER = ("NAME", "SOURCE", "PUBLISHER", "TIER", "PRICE", "LATEST", "STATUS")

SOURCE = Arg("--source", type=int, metavar="ID", help="the market that lists it (`hub market source list`)")
NAME = (Arg("name", metavar="NAME", help="the plugin's id, as the market lists it"), SOURCE)
TRUST = Arg("--trust", action="store_true", help="trust the publisher's signer, if the hub does not yet")


def price_text(price: dict) -> str:
    if price["kind"] == "free":
        return "free"
    return f"USD {price['usd_per_hub_month']:g} per hub a month, from {price['seller']}"


def usd(value) -> str:
    return f"USD {float(value):.2f}"


def confirmed(question: str) -> bool:
    stdin = sys.stdin
    if stdin is None or not stdin.isatty():
        return False
    return input(f"{question} [y/N] ").strip().lower() in YES


def trusting(post: Callable[[bool], dict], args: argparse.Namespace, doing: str = "install") -> dict:
    """``post(trust_publisher)`` for an install (or an order, ``doing``); asked once at a terminal when the hub does not
    trust the signer yet and --trust is not given."""
    try:
        return post(bool(args.trust))
    except HubError as exc:
        if exc.status != 409 or not str(exc).startswith(UNTRUSTED) or args.trust or args.json:
            raise
        print(str(exc), file=sys.stderr)  # the question at the terminal names what the hub refused
        if not confirmed(f"Trust this signer for the publisher from now on, and {doing}?"):
            raise HubError(f"nothing was done: pass --trust to trust the publisher's signer and {doing}") from None
        return post(True)


def installed_lines(done: dict) -> list[str]:
    """What the terminal prints of an install."""
    plugin = done["plugin"]
    lines = []
    if done["trusted_publisher"]:
        lines.append(f"The hub trusts the signer of publisher {plugin['publisher']} from now on.")
    verb = "Upgraded" if done["upgraded"] else "Installed"
    lines.append(
        f"{verb} plugin {plugin['name']} {plugin['installed_version']}: enable it per project with `hub plugin enable`."
    )
    return lines


def plugin_row(item: dict) -> tuple:
    return (
        item["id"],
        item["source"],
        item["publisher"]["id"],
        item["tier"],
        price_text(item["price"]),
        (item["latest"] or {}).get("version") or "-",
        STATUS_TEXT[item["status"]] + (f" {item['installed_version']}" if item["installed_version"] else ""),
    )


def detail_lines(item: dict) -> list[str]:
    """What ``market show`` prints of one plugin."""
    publisher = item["publisher"]
    lines = [
        f"{item['name']} ({item['id']}), {item['market']}: {STATUS_TEXT[item['status']]}",
        f"  {item['description']}",
        f"  publisher {publisher['name'] or publisher['id']} ({publisher['id']}), signed by {publisher['identity']}",
    ]
    trust = "trusted on this hub" if item["trusted"] else "not trusted on this hub yet"
    lines.append(f"  {trust}; repo {item['repo']}, tier {item['tier']}, {price_text(item['price'])}")
    if item["installed_version"]:
        lines.append(f"  installed {item['installed_version']}")
    latest = item["latest"]
    if latest and latest["error"]:
        lines.append(f"  latest release {latest['tag'] or '-'}: {latest['error']}")
    elif latest:
        lines.append(f"  latest release {latest['tag']}: version {latest['version']}")
        lines += [f"    asks to {permission['name']}: {permission['text']}" for permission in latest["permissions"]]
    if item["tier"] == "core":
        lines.append(f"  a core plugin: it ships in the hub's image (package {item['package']}), never from a market")
    return lines


class MarketList(HubCommand):
    name = "list"
    help = "the plugins of every market, with their status on the hub"
    arguments = (
        Arg("--search", metavar="WORDS", help="words of the id, name, description or publisher"),
        Arg("--industry", metavar="INDUSTRY"),
        Arg("--category", metavar="CATEGORY"),
        SOURCE,
    )
    output = returns_object(*LIST_KEYS, schema="MarketList")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        query = {"q": args.search, "industry": args.industry, "category": args.category, "source": args.source}
        found = Market(ctx.hub).plugins(**query)
        unread = [
            f"Market {market['repo']} did not read at its last refresh: {market['error']}"
            for market in found["markets"]
            if market["error"]
        ]
        filtered = any(value is not None for value in query.values())
        empty = "No plugin matches." if filtered else "The hub reads no market: add one with `hub market source add`."
        rows = [plugin_row(item) for item in found["plugins"]]
        return Result(found, Views(Text(*unread), Table(LIST_HEADER, rows, empty)))


class MarketShow(HubCommand):
    name = "show"
    help = "one plugin: publisher, signer, permissions, version, price and status"
    arguments = NAME
    output = returns_object(*DETAIL_KEYS, schema="MarketPluginDetail")

    def run(self, ctx: HubContext) -> Result:
        market = Market(ctx.hub)
        item = market.plugin(market.source_of(ctx.args.name, ctx.args.source), ctx.args.name)
        return Result(item, Text(*detail_lines(item)))


class MarketInstall(HubCommand):
    name = "install"
    help = "install or upgrade a free plugin from its GitHub release (hub admins)"
    arguments = (
        *NAME,
        Arg("--version", metavar="VERSION", help="the version to install (default: the latest release)"),
        TRUST,
    )
    output = returns_object(*INSTALLED_KEYS, schema="MarketInstalled")

    def run(self, ctx: HubContext) -> Result:
        args, market = ctx.args, Market(ctx.hub)
        source = market.source_of(args.name, args.source)
        body = {"version": args.version}
        done = trusting(
            lambda trust: market.post(source, args.name, "install", {**body, "trust_publisher": trust}), args
        )
        return Result(done, Text(*installed_lines(done)))


def install_github(args: argparse.Namespace) -> int:
    """``hub plugin install --github OWNER/REPO[@TAG]``: prints what was installed; a HubError goes to the caller."""
    ctx = HubContext(args)
    repo, _, tag = args.github.partition("@")
    body = {"repo": repo, "tag": tag or None, "signer": args.signer}
    if args.project:  # a self-managed project's admin: the plugin is the project's alone
        done = ProjectPlugins.of(ctx).install_github(body)
    else:
        installs = GithubInstalls(ctx.hub)
        done = trusting(lambda trust: installs.post({**body, "trust_publisher": trust}), args)
    result = Result(done, Text(*installed_lines(done)))
    result.show(ctx.out, bool(args.json))
    return result.status


class MarketRequest(HubCommand):
    name = "request"
    help = "ask the hub admins to install a plugin for a project you administer"
    arguments = (*NAME, Arg("--project", required=True, metavar="PROJECT"))
    output = returns_object(*REQUEST_KEYS, schema="MarketRequest")

    def run(self, ctx: HubContext) -> Result:
        args, market = ctx.args, Market(ctx.hub)
        source = market.source_of(args.name, args.source)
        asked = market.post(source, args.name, "request", {"project": args.project})
        if asked["created"]:
            line = f"Asked the hub admins to install {args.name} for project {args.project}."
        else:
            line = f"Project {args.project} asked for {args.name} already; the request is still open."
        return Result(asked, Text(line))


MARKET_LIST = MarketList()
MARKET_SHOW = MarketShow()
MARKET_INSTALL = MarketInstall()
MARKET_REQUEST = MarketRequest()
