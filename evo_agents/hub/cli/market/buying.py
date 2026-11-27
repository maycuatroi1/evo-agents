"""Buying a paid plugin (``evo_agents.hub.server.market.service.licenses``): ``hub market buy NAME --months N --name --email
[--trust]`` orders it from its seller and prints how to pay; ``market licenses [--check]`` the hub's orders and
licences; ``market hub-id`` the id a licence names. Standard library only, like the rest of the client.
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Stderr, Table, Text, Views, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.market.catalog import NAME, TRUST, trusting, usd
from evo_agents.hub.cli.market.resources import CHECKED_KEYS, Licenses, Market

PURCHASES_HEADER = ("ORDER", "PLUGIN", "SELLER", "MONTHS", "TOTAL", "STATE", "UNTIL")
CHECKED_WORDS = (
    ("received", "received"),
    ("installed", "installed"),
    ("reminded about", "reminded"),
    ("turned off", "lapsed"),
)


def bought_lines(bought: dict) -> list[str]:
    """What the terminal prints of an order: what was ordered and how to pay it."""
    purchase = bought["purchase"]
    lines = []
    if bought["trusted_publisher"]:
        lines.append(f"The hub trusts the signer of publisher {purchase['publisher']} from now on.")
    lines.append(
        f"Ordered {purchase['plugin']} for {purchase['months']} months: order {purchase['number']}, "
        f"{usd(purchase['usd_total'])}."
    )
    lines.append(f"Pay it with the order number as the reference:\n{bought['instructions']}")
    lines.append("Once the seller marks it paid, the hub receives the licence and installs the plugin within the hour.")
    return lines


def checked_view(found: dict) -> Views:
    """What ``licenses --check`` prints: what moved on stdout, the errors on stderr."""
    lines = [f"Asked about {found['checked']} open orders."]
    lines += [f"  {word}: {', '.join(found[key])}" for word, key in CHECKED_WORDS if found[key]]
    return Views(Text(*lines), Stderr(Text(*(f"  error: {error}" for error in found["errors"]))))


def purchase_row(item: dict) -> tuple:
    return (
        item["number"],
        item["plugin"],
        item["seller"],
        item["months"],
        usd(item["usd_total"]),
        item["license_state"] or item["state"],
        item["valid_until"] or "-",
    )


class MarketBuy(HubCommand):
    name = "buy"
    help = "order a paid plugin from its seller; prints how to pay it (hub admins)"
    arguments = (
        *NAME,
        Arg("--months", type=int, required=True, metavar="N", help="how many months the licence covers"),
        Arg("--name", dest="contact_name", required=True, metavar="NAME", help="who the seller invoices"),
        Arg("--email", required=True, metavar="EMAIL", help="where the seller sends the invoice"),
        TRUST,
    )
    output = returns_object("purchase", "instructions", "trusted_publisher", schema="Bought")

    def run(self, ctx: HubContext) -> Result:
        args, market = ctx.args, Market(ctx.hub)
        source = market.source_of(args.name, args.source)
        body = {"months": args.months, "contact": {"name": args.contact_name, "email": args.email}}
        bought = trusting(
            lambda trust: market.post(source, args.name, "buy", {**body, "trust_publisher": trust}),
            args,
            doing="order it",
        )
        return Result(bought, Text(*bought_lines(bought)))


class MarketLicenses(HubCommand):
    name = "licenses"
    help = "the hub's orders of paid plugins and their licences (hub admins)"
    arguments = (
        Arg(
            "--check",
            action="store_true",
            help="ask the sellers now, install what a licence covers and turn off what lapsed",
        ),
    )
    output = returns_object(
        "hub",
        "purchases",
        schema="Purchases",
        variants=(("--check", returns_object(*CHECKED_KEYS, schema="Checked")),),
    )

    def run(self, ctx: HubContext) -> Result:
        licenses = Licenses(ctx.hub)
        if ctx.args.check:
            found = licenses.check()
            return Result(found, checked_view(found))
        found = licenses.list()
        empty = f"Hub {found['hub']} has bought no plugin: see `evo-agents hub market buy`."
        return Result(found, Table(PURCHASES_HEADER, [purchase_row(item) for item in found["purchases"]], empty))


class MarketHubId(HubCommand):
    name = "hub-id"
    help = "the hub's stable id, which a seller's licence names"
    output = returns_object("hub", "url", schema="HubIdentity")

    def run(self, ctx: HubContext) -> Result:
        found = Licenses(ctx.hub).hub_id()
        return Result(found, Text(found["hub"]))


MARKET_BUY = MarketBuy()
MARKET_LICENSES = MarketLicenses()
MARKET_HUB_ID = MarketHubId()
