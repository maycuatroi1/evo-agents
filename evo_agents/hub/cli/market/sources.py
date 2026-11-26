"""``hub market source list|add|remove|refresh`` (hub admins): the markets the hub reads, GitHub repos holding
market.json. Standard library only, like the rest of the client.
"""

from __future__ import annotations

import sys

from evo_agents.clikit import Arg, Group, Result, Table, Text, Views, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext, when
from evo_agents.hub.cli.market.resources import SOURCE_KEYS, SOURCES_KEYS, MarketSources
from evo_agents.hub.client import HubError

SOURCES_HEADER = ("ID", "REPO", "INDEX", "PLUGINS", "READ", "ERROR")
NO_SOURCE = "The hub reads no market: add one with `hub market source add OWNER/REPO`."
LOCKED = "This hub's operator locked its publishers: installing trusts no new signer."
SOURCE_ID = Arg("id", type=int, metavar="ID", help="the source's id, as `source list` prints it")


def source_row(item: dict) -> tuple:
    return (
        item["id"],
        item["repo"] + (" (default)" if item["builtin"] else ""),
        f"{item['ref']}:{item['path']}",
        item["plugins"],
        when(item["fetched_at"]) if item["fetched_at"] else "never",
        item["error"] or "-",
    )


def source_result(item: dict, verb: str, as_json: bool) -> Result:
    """A source added or read again; exit status 1 when it did not read (not with ``--json``)."""
    lines = [f"{verb} market {item['repo']} ({item['id']}): {item['plugins']} plugins."]
    if item["error"]:
        lines.append(f"  It did not read: {item['error']}")
    return Result(item, Text(*lines), 1 if item["error"] and not as_json else 0)


class SourceList(HubCommand):
    name = "list"
    help = "every market, the operator's first, with its last read"
    output = returns_object(*SOURCES_KEYS, schema="MarketSources")

    def run(self, ctx: HubContext) -> Result:
        found = MarketSources(ctx.hub).list()
        if not found["sources"]:
            return Result(found, Text(NO_SOURCE))
        rows = Table(SOURCES_HEADER, [source_row(item) for item in found["sources"]])
        return Result(found, Views(rows, Text(LOCKED)) if found["publishers_locked"] else rows)


class SourceAdd(HubCommand):
    name = "add"
    help = "read a market from a GitHub repo, now and every six hours"
    arguments = (
        Arg("repo", metavar="OWNER/REPO", help="the GitHub repo holding the index"),
        Arg("--ref", default="main", help="the branch or tag to read (default: main)"),
        Arg("--path", default="market.json", help="the index's path in the repo (default: market.json)"),
        Arg(
            "--token-stdin",
            action="store_true",
            help="read a GitHub token for a private repo from stdin; sealed on the hub",
        ),
    )
    output = returns_object(*SOURCE_KEYS, schema="MarketSource")

    def run(self, ctx: HubContext) -> Result:
        args, sources = ctx.args, MarketSources(ctx.hub)
        body = {"repo": args.repo, "ref": args.ref, "path": args.path}
        if args.token_stdin:
            token = sys.stdin.readline().strip()
            if not token:
                raise HubError("--token-stdin read no token")
            body["token"] = token
        return source_result(sources.add(body), "Added", bool(args.json))


class SourceRemove(HubCommand):
    name = "remove"
    help = "stop reading a market"
    arguments = (SOURCE_ID,)
    output = returns_object(*SOURCES_KEYS, schema="MarketSources")

    def run(self, ctx: HubContext) -> Result:
        found = MarketSources(ctx.hub).remove(ctx.args.id)
        return Result(
            found, Text(f"Removed market source {ctx.args.id}; the plugins installed from it stay installed.")
        )


class SourceRefresh(HubCommand):
    name = "refresh"
    help = "read a market again now"
    arguments = (SOURCE_ID,)
    output = returns_object(*SOURCE_KEYS, schema="MarketSource")

    def run(self, ctx: HubContext) -> Result:
        return source_result(MarketSources(ctx.hub).refresh(ctx.args.id), "Read", bool(ctx.args.json))


MARKET_SOURCE = Group(
    "source",
    "the markets the hub reads: GitHub repos holding market.json (hub admins)",
    (SourceList(), SourceAdd(), SourceRemove(), SourceRefresh()),
    dest="market_source_command",
)
