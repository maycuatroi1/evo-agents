"""Selling plugins, the hub feature ``seller`` (``evo_agents.hub.server.seller``): ``hub market seller-key FILE``
writes a new key and prints only its public half, ``market seller`` whether the hub sells, ``market listing
list|add|set`` the paid plugins it sells and ``market bundle upload`` their signed bundles. Standard library only, like
the rest of the client.
"""

from __future__ import annotations

import argparse
import sys

from evo_agents.clikit import Arg, Command, Context, Group, Result, Table, Text, returns_object
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.market.catalog import usd
from evo_agents.hub.cli.market.resources import LISTING_KEYS, SELLER_KEYS, Seller
from evo_agents.hub.client import HubError

LISTINGS_HEADER = ("PLUGIN", "PUBLISHER", "STATE", "PER HUB-MONTH", "LATEST", "LICENCES", "OWNER")
NOT_SELLING = "The hub cannot sell: set EVO_HUB_SELLER_ID and EVO_HUB_SELLER_KEY (`hub market seller-key`)."
PLUGIN = Arg("plugin", metavar="PLUGIN")
LISTING_OUTPUT = returns_object(*LISTING_KEYS, schema="Listing")


def seller_key_json(file: str, key: str, public: str) -> dict:
    """What ``market seller-key --json`` prints: never the seed."""
    return {"file": file, "key_id": key, "public_key": public}


class KeyExists(Exception):
    """The file of a new seller key is there already."""


def write_seller_key(file: str) -> dict:
    """Write a new Ed25519 seed to ``file``, readable by its owner only, and give what the market's index lists."""
    import base64
    import os

    from evo_agents.plugins.license import new_key
    from evo_agents.plugins.signing import key_id

    seed, public = new_key()
    try:
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise KeyExists(f"{file} exists: the hub never overwrites a seller key") from None
    with os.fdopen(fd, "w", newline="\n") as out:
        out.write(base64.b64encode(seed).decode() + "\n")
    return seller_key_json(file, key_id(base64.b64decode(public)), public)


def listing_line(item: dict) -> str:
    latest = item["bundles"][0]["version"] if item["bundles"] else "no bundle"
    return (
        f"{item['plugin']} ({item['publisher']}): {item['state']}, {usd(item['usd_per_hub_month'])} per hub "
        f"per month, {latest}, {item['active_licenses']} active licences"
    )


def listing_row(item: dict) -> tuple:
    return (
        item["plugin"],
        item["publisher"],
        item["state"],
        usd(item["usd_per_hub_month"]),
        item["bundles"][0]["version"] if item["bundles"] else "-",
        item["active_licenses"],
        item["owner"] or "-",
    )


def bundle_body(file: str, signature_file: str) -> dict:
    """The body of an upload: the bundle in base64 and its signature as an envelope. A signature file is an envelope
    or the Sigstore bundle CI writes, wrapped into one as ``hub plugin publish`` does, since the seller's hub takes
    envelopes alone."""
    import base64
    from pathlib import Path

    from evo_agents.plugins.signing import SignatureError, read_signature

    try:
        data = Path(file).read_bytes()
        signature = read_signature(Path(signature_file).read_bytes())
    except OSError as exc:
        raise HubError(f"cannot read the bundle or its signature: {exc}") from None
    except SignatureError as exc:
        raise HubError(f"{signature_file}: {exc}") from None
    return {"bundle": base64.b64encode(data).decode(), "signature": signature}


class SellerKey(Command[Context]):
    """Needs no hub: the key is written on this machine."""

    name = "seller-key"
    help = "write a new seller key to a file (0600); prints only its public key and id"
    arguments = (Arg("file", metavar="FILE", help="where to write it; never overwritten"),)
    output = returns_object("file", "key_id", "public_key")

    def context(self, args: argparse.Namespace) -> Context:
        return Context(args)

    def run(self, ctx: Context) -> Result:
        found = write_seller_key(ctx.args.file)
        return Result(
            found,
            Text(
                f"Wrote a new seller key to {found['file']} (mode 0600). Set EVO_HUB_SELLER_KEY to its content; "
                "never share it.",
                f"List it in the market's index under sellers[].keys: key id {found['key_id']}, "
                f"public key {found['public_key']}",
            ),
        )

    def failed(self, exc: Exception) -> int | None:
        if not isinstance(exc, KeyExists):
            return None
        print(exc, file=sys.stderr)
        return 1


class SellerInfo(HubCommand):
    name = "seller"
    help = "whether the hub sells plugins: its seller id and public key"
    output = returns_object(*SELLER_KEYS, schema="SellerInfo")

    def run(self, ctx: HubContext) -> Result:
        found = Seller(ctx.hub).info()
        if not found["selling"]:
            return Result(found, Text(NOT_SELLING))
        return Result(
            found,
            Text(
                f"Seller {found['seller']}, key {found['key_id']}, public key {found['public_key']}",
                f"Signals to project {found['signal_project'] or '(none)'}; payment instructions:",
                found["payment_instructions"],
            ),
        )


class ListingList(HubCommand):
    name = "list"
    help = "every listing, with its price, latest bundle and active licences"
    output = returns_object("listings", schema="Listings")

    def run(self, ctx: HubContext) -> Result:
        found = Seller(ctx.hub).listings()
        return Result(found, Table(LISTINGS_HEADER, [listing_row(item) for item in found["listings"]], bare=True))


class ListingAdd(HubCommand):
    name = "add"
    help = "list a plugin at a price in USD per hub per month, as a draft"
    arguments = (
        PLUGIN,
        Arg("--publisher", required=True, metavar="PUBLISHER"),
        Arg("--signer", required=True, metavar="SIGNER", help="what signs its bundles"),
        Arg("--usd", required=True, metavar="USD", help="the price per hub per month, such as 49.50"),
    )
    output = LISTING_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = {
            "plugin": args.plugin,
            "publisher": args.publisher,
            "signer": args.signer,
            "usd_per_hub_month": args.usd,
        }
        found = Seller(ctx.hub).add_listing(body)
        upload = (
            f"Upload a signed bundle with `hub market bundle upload {args.plugin} FILE --signature SIG` to list it."
        )
        return Result(found, Text(listing_line(found), upload))


class ListingSet(HubCommand):
    name = "set"
    help = "change a listing's price, signer or state"
    arguments = (
        PLUGIN,
        Arg("--usd", metavar="USD"),
        Arg("--signer", metavar="SIGNER"),
        Arg("--state", choices=("listed", "withdrawn")),
    )
    output = LISTING_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = {"usd_per_hub_month": args.usd, "signer": args.signer, "state": args.state}
        found = Seller(ctx.hub).change_listing(args.plugin, {k: v for k, v in body.items() if v is not None})
        return Result(found, Text(listing_line(found)))


class BundleUpload(HubCommand):
    name = "upload"
    help = "upload a signed bundle (tar.gz) of a listed plugin"
    arguments = (
        PLUGIN,
        Arg("file", metavar="FILE", help="the bundle, as `evo-agents plugin pack` writes it"),
        Arg(
            "--signature",
            required=True,
            metavar="FILE",
            help="its signature: an envelope from `evo-agents plugin sign`, or the .sigstore.json bundle of a release",
        ),
    )
    output = LISTING_OUTPUT

    def run(self, ctx: HubContext) -> Result:
        seller = Seller(ctx.hub)
        found = seller.upload_bundle(ctx.args.plugin, bundle_body(ctx.args.file, ctx.args.signature))
        return Result(found, Text(listing_line(found)))


MARKET_SELLER_KEY = SellerKey()
MARKET_SELLER = SellerInfo()
MARKET_LISTING = Group(
    "listing",
    "the paid plugins the hub sells (their publishers, signed in with GitHub)",
    (ListingList(), ListingAdd(), ListingSet()),
    dest="market_listing_command",
)
MARKET_BUNDLE = Group("bundle", "the signed bundles of a listing", (BundleUpload(),), dest="market_bundle_command")
