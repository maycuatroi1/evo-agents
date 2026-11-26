"""The orders a seller's hub takes and what they earn (``evo_agents.hub.server.seller``): ``hub market orders
[--state]``, ``market paid NUMBER --invoice``, ``market cancel NUMBER``, ``market revenue [--month YYYY-MM]`` and
``market payout --month --publisher [--note]``. Standard library only, like the rest of the client.
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Table, Text, Views, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext, when
from evo_agents.hub.cli.market.catalog import usd
from evo_agents.hub.cli.market.resources import ORDER_KEYS, REVENUE_KEYS, Seller

ORDER_HEADER = ("ORDER", "PLUGIN", "BUYER", "HUB", "MONTHS", "TOTAL", "STATE", "INVOICE", "UNTIL")
REVENUE_HEADER = ("PUBLISHER", "ORDERS", "GROSS", "PUBLISHER 80%", "PLATFORM 20%", "PAID OUT")
NUMBER = Arg("number", metavar="NUMBER", help="the order's number, such as ORD-2026-0001")
ORDER_OUTPUT = returns_object(*ORDER_KEYS, schema="Order")
REVENUE_OUTPUT = returns_object(*REVENUE_KEYS, schema="Revenue")


def order_row(order: dict) -> tuple:
    return (
        order["number"],
        order["plugin"],
        order["buyer_name"],
        order["buyer_hub"],
        order["months"],
        usd(order["usd_total"]),
        order["state"],
        order["invoice"] or "-",
        order["valid_until"] or "-",
    )


def revenue_row(line: dict) -> tuple:
    return (
        line["publisher"],
        line["orders"],
        usd(line["gross_usd"]),
        usd(line["publisher_usd"]),
        usd(line["platform_usd"]),
        when(line["paid_out"]["paid_at"]) if line["paid_out"] else "not yet",
    )


def revenue_view(found: dict) -> Views:
    """A month's revenue: its sums, then its publishers when it has any."""
    total = (
        f"Revenue of {found['month']}: {usd(found['gross_usd'])}, {usd(found['publisher_usd'])} to publishers, "
        f"{usd(found['platform_usd'])} to the platform"
    )
    return Views(Text(total), Table(REVENUE_HEADER, [revenue_row(line) for line in found["publishers"]]))


class Orders(HubCommand):
    name = "orders"
    help = "the orders buying hubs placed (hub admins of a seller)"
    arguments = (Arg("--state", choices=("open", "paid", "cancelled")),)
    output = returns_object("orders", schema="Orders")

    def run(self, ctx: HubContext) -> Result:
        state = ctx.args.state
        found = Seller(ctx.hub).orders(state)
        empty = "No order" + (f" is {state}." if state else " yet.")
        return Result(found, Table(ORDER_HEADER, [order_row(order) for order in found["orders"]], empty))


class Paid(HubCommand):
    name = "paid"
    help = "mark an order paid with its invoice number; issues the licence"
    arguments = (NUMBER, Arg("--invoice", required=True, metavar="INVOICE", help="the invoice it was paid against"))
    output = ORDER_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        found = Seller(ctx.hub).close_order(ctx.args.number, "paid", {"invoice": ctx.args.invoice})
        return Result(
            found,
            Text(
                f"Order {found['number']} is paid (invoice {found['invoice']}): licence {found['license_id']} covers "
                f"{found['plugin']} for hub {found['buyer_hub']} from {found['valid_from']} until "
                f"{found['valid_until']}.",
                "The buying hub receives it at its next check.",
            ),
        )


class Cancel(HubCommand):
    name = "cancel"
    help = "cancel an open order"
    arguments = (NUMBER,)
    output = ORDER_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        found = Seller(ctx.hub).close_order(ctx.args.number, "cancel")
        return Result(found, Text(f"Order {found['number']} is cancelled."))


class Revenue(HubCommand):
    name = "revenue"
    help = "a month's revenue by publisher, split 80/20"
    arguments = (Arg("--month", metavar="YYYY-MM", help="this month in UTC when left out"),)
    output = REVENUE_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        found = Seller(ctx.hub).revenue(ctx.args.month)
        return Result(found, revenue_view(found))


class Payout(HubCommand):
    name = "payout"
    help = "mark a publisher's share of a month paid out, once the month is over"
    arguments = (
        Arg("--month", required=True, metavar="YYYY-MM"),
        Arg("--publisher", required=True, metavar="PUBLISHER"),
        Arg("--note", help="how it was paid, for the other hub admins"),
    )
    output = REVENUE_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        found = Seller(ctx.hub).payout({"month": args.month, "publisher": args.publisher, "note": args.note})
        paid = Text(f"Marked the share of {args.publisher} for {args.month} paid out.")
        return Result(found, Views(paid, revenue_view(found)))


MARKET_ORDERS = Orders()
MARKET_PAID = Paid()
MARKET_CANCEL = Cancel()
MARKET_REVENUE = Revenue()
MARKET_PAYOUT = Payout()
