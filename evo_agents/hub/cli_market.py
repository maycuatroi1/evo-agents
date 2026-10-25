"""``evo-agents hub market``: the plugin markets the hub reads (``evo_agents.hub.server.market``), and installing
from them or from a GitHub release (``evo_agents.hub.server.market_install``); docs/market.md.

- ``market list [--search WORDS] [--industry I] [--category C] [--source ID]`` prints the plugins of every market with
  their status on the hub; ``market show NAME [--source ID]`` one plugin with its publisher and signer, what its latest
  release asks for, its price and whether the hub trusts its signer;
- ``market install NAME [--source ID] [--version V] [--trust]`` (hub admins) installs or upgrades a free plugin from
  its GitHub release; a signer the hub does not trust yet is asked about at a terminal, or trusted with ``--trust``;
- ``market request NAME --project P [--source ID]`` (a project's admins) asks the hub admins to install one;
- ``market source list|add REPO [--ref] [--path] [--token-stdin]|remove ID|refresh ID`` (hub admins): the markets;
- buying a paid plugin (``evo_agents.hub.server.market_licenses``): ``market buy NAME --months N --name --email
  [--trust]`` orders it from its seller and prints how to pay; ``market licenses [--check]`` the hub's orders and
  licences; ``market hub-id`` the id a licence names;
- selling them, the hub feature ``seller`` (``evo_agents.hub.server.seller``): ``market seller-key FILE`` writes a
  new key and prints only its public half, ``market seller``, ``market listing list|add|set``, ``market bundle
  upload``, ``market orders [--state]``, ``market paid NUMBER --invoice``, ``market cancel NUMBER``, ``market revenue
  [--month YYYY-MM]`` and ``market payout --month --publisher [--note]``.

``evo-agents hub plugin install --github OWNER/REPO[@TAG] [--signer S] [--trust]`` and ``hub plugin publisher
list|add|remove`` are registered here too and hang under ``hub plugin``. Standard library only, like the rest of the
client.
"""

from __future__ import annotations

import sys
from urllib.parse import urlencode

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import json_option, returns_array, returns_object

LIST_KEYS = ("plugins", "industries", "categories", "markets")
PLUGIN_KEYS = (
    "source",
    "market",
    "id",
    "name",
    "description",
    "industries",
    "categories",
    "repo",
    "publisher",
    "tier",
    "package",
    "price",
    "icon",
    "homepage",
    "license",
    "requires",
    "status",
    "installed_version",
    "latest",
)
DETAIL_KEYS = (*PLUGIN_KEYS, "trusted", "may_trust")
SOURCE_KEYS = (
    "id",
    "repo",
    "name",
    "title",
    "fetched_at",
    "error",
    "ref",
    "path",
    "builtin",
    "private",
    "added_by",
    "added_at",
    "plugins",
)
SOURCES_KEYS = ("sources", "default_source", "publishers_locked")
INSTALLED_KEYS = ("plugin", "trusted_publisher", "upgraded")
REQUEST_KEYS = ("id", "project", "plugin", "state", "created")
PUBLISHER_KEYS = ("id", "identities", "core", "operator", "trusted")
SELLER_KEYS = ("seller", "key_id", "public_key", "selling", "payment_instructions", "signal_project")
LISTING_KEYS = (
    "plugin",
    "publisher",
    "signer",
    "owner",
    "usd_per_hub_month",
    "state",
    "bundles",
    "active_licenses",
    "created_at",
    "updated_at",
)
ORDER_KEYS = (
    "number",
    "plugin",
    "publisher",
    "buyer_hub",
    "buyer_url",
    "buyer_name",
    "buyer_email",
    "months",
    "usd_per_month",
    "usd_total",
    "state",
    "payment",
    "created_at",
    "invoice",
    "closed_by",
    "closed_at",
    "license_id",
    "valid_from",
    "valid_until",
)
CHECKED_KEYS = ("checked", "received", "installed", "reminded", "lapsed", "errors")
REVENUE_KEYS = ("month", "publishers", "gross_usd", "publisher_usd", "platform_usd")
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


def _price(price: dict) -> str:
    if price["kind"] == "free":
        return "free"
    return f"USD {price['usd_per_hub_month']:g} per hub a month, from {price['seller']}"


# Browsing


@client_command
def cmd_list(args) -> int:
    hub, _ = signed_in()
    query = {"q": args.search, "industry": args.industry, "category": args.category, "source": args.source}
    params = urlencode({key: value for key, value in query.items() if value is not None})
    found = hub.call("GET", "/v1/market" + (f"?{params}" if params else ""))
    if args.json:
        print_json(found)
        return 0
    for market in found["markets"]:
        if market["error"]:
            print(f"Market {market['repo']} did not read at its last refresh: {market['error']}")
    if not found["plugins"]:
        print("No plugin matches." if params else "The hub reads no market: add one with `hub market source add`.")
        return 0
    rows = [
        (
            item["id"],
            item["source"],
            item["publisher"]["id"],
            item["tier"],
            _price(item["price"]),
            (item["latest"] or {}).get("version") or "-",
            STATUS_TEXT[item["status"]] + (f" {item['installed_version']}" if item["installed_version"] else ""),
        )
        for item in found["plugins"]
    ]
    table(("NAME", "SOURCE", "PUBLISHER", "TIER", "PRICE", "LATEST", "STATUS"), rows)
    return 0


def _source_of(hub, name: str, source: int | None) -> int:
    """The source that lists plugin ``name``: ``source``, or the only one that does."""
    if source is not None:
        return source
    found = hub.call("GET", "/v1/market")
    sources = sorted({item["source"] for item in found["plugins"] if item["id"] == name})
    if not sources:
        raise HubError(f"no market of the hub lists a plugin {name}: see `evo-agents hub market list`")
    if len(sources) > 1:
        listed = ", ".join(str(item) for item in sources)
        raise HubError(f"markets {listed} list a plugin {name}: name one with --source")
    return sources[0]


def _show(item: dict) -> None:
    publisher = item["publisher"]
    print(f"{item['name']} ({item['id']}), {item['market']}: {STATUS_TEXT[item['status']]}")
    print(f"  {item['description']}")
    print(f"  publisher {publisher['name'] or publisher['id']} ({publisher['id']}), signed by {publisher['identity']}")
    trust = "trusted on this hub" if item["trusted"] else "not trusted on this hub yet"
    print(f"  {trust}; repo {item['repo']}, tier {item['tier']}, {_price(item['price'])}")
    if item["installed_version"]:
        print(f"  installed {item['installed_version']}")
    latest = item["latest"]
    if latest and latest["error"]:
        print(f"  latest release {latest['tag'] or '-'}: {latest['error']}")
    elif latest:
        print(f"  latest release {latest['tag']}: version {latest['version']}")
        for permission in latest["permissions"]:
            print(f"    asks to {permission['name']}: {permission['text']}")
    if item["tier"] == "core":
        print(f"  a core plugin: it ships in the hub's image (package {item['package']}), never from a market")


@client_command
def cmd_show(args) -> int:
    hub, _ = signed_in()
    source = _source_of(hub, args.name, args.source)
    item = hub.call("GET", f"/v1/market/{source}/{args.name}")
    if args.json:
        print_json(item)
    else:
        _show(item)
    return 0


# Installing


def _confirmed(question: str) -> bool:
    stdin = sys.stdin
    if stdin is None or not stdin.isatty():
        return False
    return input(f"{question} [y/N] ").strip().lower() in YES


def installed(hub, path: str, body: dict, args, doing: str = "install") -> dict:
    """POST an install (or an order, ``doing``); asked once at a terminal when the hub does not trust the signer yet
    and --trust is not given."""
    try:
        return hub.call("POST", path, {**body, "trust_publisher": bool(args.trust)})
    except HubError as exc:
        if exc.status != 409 or not str(exc).startswith(UNTRUSTED) or args.trust or args.json:
            raise
        print(str(exc), file=sys.stderr)
        if not _confirmed(f"Trust this signer for the publisher from now on, and {doing}?"):
            raise HubError(f"nothing was done: pass --trust to trust the publisher's signer and {doing}") from None
        return hub.call("POST", path, {**body, "trust_publisher": True})


def _print_installed(args, done: dict) -> int:
    if args.json:
        print_json(done)
        return 0
    plugin = done["plugin"]
    if done["trusted_publisher"]:
        print(f"The hub trusts the signer of publisher {plugin['publisher']} from now on.")
    verb = "Upgraded" if done["upgraded"] else "Installed"
    print(
        f"{verb} plugin {plugin['name']} {plugin['installed_version']}: enable it per project with `hub plugin enable`."
    )
    return 0


@client_command
def cmd_install(args) -> int:
    hub, _ = signed_in()
    source = _source_of(hub, args.name, args.source)
    done = installed(hub, f"/v1/market/{source}/{args.name}/install", {"version": args.version}, args)
    return _print_installed(args, done)


def install_github(args) -> int:
    """``hub plugin install --github OWNER/REPO[@TAG]``."""
    hub, _ = signed_in()
    repo, _, tag = args.github.partition("@")
    body = {"repo": repo, "tag": tag or None, "signer": args.signer}
    return _print_installed(args, installed(hub, "/v1/plugins/install-github", body, args))


@client_command
def cmd_request(args) -> int:
    hub, _ = signed_in()
    source = _source_of(hub, args.name, args.source)
    asked = hub.call("POST", f"/v1/market/{source}/{args.name}/request", {"project": args.project})
    if args.json:
        print_json(asked)
    elif asked["created"]:
        print(f"Asked the hub admins to install {args.name} for project {args.project}.")
    else:
        print(f"Project {args.project} asked for {args.name} already; the request is still open.")
    return 0


# The sources


def _print_sources(found: dict) -> None:
    if not found["sources"]:
        print("The hub reads no market: add one with `hub market source add OWNER/REPO`.")
        return
    rows = [
        (
            item["id"],
            item["repo"] + (" (default)" if item["builtin"] else ""),
            f"{item['ref']}:{item['path']}",
            item["plugins"],
            when(item["fetched_at"]) if item["fetched_at"] else "never",
            item["error"] or "-",
        )
        for item in found["sources"]
    ]
    table(("ID", "REPO", "INDEX", "PLUGINS", "READ", "ERROR"), rows)
    if found["publishers_locked"]:
        print("This hub's operator locked its publishers: installing trusts no new signer.")


@client_command
def cmd_source_list(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", "/v1/market/sources")
    if args.json:
        print_json(found)
    else:
        _print_sources(found)
    return 0


def _print_source(args, item: dict, verb: str) -> int:
    if args.json:
        print_json(item)
        return 0
    print(f"{verb} market {item['repo']} ({item['id']}): {item['plugins']} plugins.")
    if item["error"]:
        print(f"  It did not read: {item['error']}")
        return 1
    return 0


@client_command
def cmd_source_add(args) -> int:
    hub, _ = signed_in()
    body = {"repo": args.repo, "ref": args.ref, "path": args.path}
    if args.token_stdin:
        token = sys.stdin.readline().strip()
        if not token:
            raise HubError("--token-stdin read no token")
        body["token"] = token
    return _print_source(args, hub.call("POST", "/v1/market/sources", body), "Added")


@client_command
def cmd_source_remove(args) -> int:
    hub, _ = signed_in()
    found = hub.call("DELETE", f"/v1/market/sources/{args.id}")
    if args.json:
        print_json(found)
    else:
        print(f"Removed market source {args.id}; the plugins installed from it stay installed.")
    return 0


@client_command
def cmd_source_refresh(args) -> int:
    hub, _ = signed_in()
    return _print_source(args, hub.call("POST", f"/v1/market/sources/{args.id}/refresh"), "Read")


# The publishers


@client_command
def cmd_publisher_list(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", "/v1/plugin-publishers")
    if args.json:
        print_json(found)
        return 0
    rows = []
    for item in found:
        kind = "core" if item["core"] else "isolated"
        if item["operator"]:
            operator = [i for i in item["identities"] if i not in {t["identity"] for t in item["trusted"]}]
            rows += [("-", item["id"], kind, identity, "the operator") for identity in operator]
        rows += [
            (t["id"], item["id"], kind, t["identity"], f"{t['added_by']} at {when(t['added_at'])}")
            for t in item["trusted"]
        ]
    table(("ID", "PUBLISHER", "PLUGINS", "SIGNER", "TRUSTED BY"), rows)
    return 0


@client_command
def cmd_publisher_add(args) -> int:
    hub, _ = signed_in()
    body = {"publisher": args.publisher, "signer": args.signer, "note": args.note}
    found = hub.call("POST", "/v1/plugin-publishers", body)
    if args.json:
        print_json(found)
    else:
        print(f"The hub trusts {args.signer} to sign the plugins of {args.publisher}.")
    return 0


@client_command
def cmd_publisher_remove(args) -> int:
    hub, _ = signed_in()
    found = hub.call("DELETE", f"/v1/plugin-publishers/{args.id}")
    if args.json:
        print_json(found)
    else:
        print(f"The hub no longer trusts signer {args.id}; what it signed stays installed.")
    return 0


# Buying a paid plugin


def _print_purchase(item: dict) -> None:
    state = item["license_state"] or item["state"]
    until = f" until {item['valid_until']}" if item["valid_until"] else ""
    print(
        f"{item['number']}: {item['plugin']} from {item['seller']}, {item['months']} months, {_usd(item['usd_total'])}"
    )
    print(f"  {state}{until}" + (f"; last check: {item['last_error']}" if item["last_error"] else ""))


def _usd(value) -> str:
    return f"USD {float(value):.2f}"


@client_command
def cmd_buy(args) -> int:
    hub, _ = signed_in()
    source = _source_of(hub, args.name, args.source)
    body = {"months": args.months, "contact": {"name": args.contact_name, "email": args.email}}
    bought = installed(hub, f"/v1/market/{source}/{args.name}/buy", body, args, doing="order it")
    if args.json:
        print_json(bought)
        return 0
    purchase = bought["purchase"]
    if bought["trusted_publisher"]:
        print(f"The hub trusts the signer of publisher {purchase['publisher']} from now on.")
    print(
        f"Ordered {purchase['plugin']} for {purchase['months']} months: order {purchase['number']}, "
        f"{_usd(purchase['usd_total'])}."
    )
    print(f"Pay it with the order number as the reference:\n{bought['instructions']}")
    print("Once the seller marks it paid, the hub receives the licence and installs the plugin within the hour.")
    return 0


@client_command
def cmd_licenses(args) -> int:
    hub, _ = signed_in()
    if args.check:
        found = hub.call("POST", "/v1/market/licenses/check")
        if args.json:
            print_json(found)
            return 0
        print(f"Asked about {found['checked']} open orders.")
        for word, key in (
            ("received", "received"),
            ("installed", "installed"),
            ("reminded about", "reminded"),
            ("turned off", "lapsed"),
        ):
            if found[key]:
                print(f"  {word}: {', '.join(found[key])}")
        for error in found["errors"]:
            print(f"  error: {error}", file=sys.stderr)
        return 0
    found = hub.call("GET", "/v1/market/licenses")
    if args.json:
        print_json(found)
        return 0
    if not found["purchases"]:
        print(f"Hub {found['hub']} has bought no plugin: see `evo-agents hub market buy`.")
        return 0
    rows = [
        (
            i["number"],
            i["plugin"],
            i["seller"],
            i["months"],
            _usd(i["usd_total"]),
            i["license_state"] or i["state"],
            i["valid_until"] or "-",
        )
        for i in found["purchases"]
    ]
    table(("ORDER", "PLUGIN", "SELLER", "MONTHS", "TOTAL", "STATE", "UNTIL"), rows)
    return 0


@client_command
def cmd_hub_id(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", "/v1/market/hub")
    if args.json:
        print_json(found)
    else:
        print(found["hub"])
    return 0


# Selling plugins (the hub feature seller)


def seller_key_json(file: str, key: str, public: str) -> dict:
    """What ``market seller-key --json`` prints: never the seed."""
    return {"file": file, "key_id": key, "public_key": public}


def cmd_seller_key(args) -> int:
    """Write a new Ed25519 seed to FILE, readable by its owner only, and print what the market's index lists."""
    import base64
    import os

    from evo_agents.plugins.license import new_key
    from evo_agents.plugins.signing import key_id

    seed, public = new_key()
    try:
        fd = os.open(args.file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"{args.file} exists: the hub never overwrites a seller key", file=sys.stderr)
        return 1
    with os.fdopen(fd, "w") as out:
        out.write(base64.b64encode(seed).decode() + "\n")
    found = seller_key_json(args.file, key_id(base64.b64decode(public)), public)
    if args.json:
        print_json(found)
        return 0
    print(f"Wrote a new seller key to {args.file} (mode 0600). Set EVO_HUB_SELLER_KEY to its content; never share it.")
    print(f"List it in the market's index under sellers[].keys: key id {found['key_id']}, public key {public}")
    return 0


@client_command
def cmd_seller_info(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", "/v1/seller")
    if args.json:
        print_json(found)
        return 0
    if not found["selling"]:
        print("The hub cannot sell: set EVO_HUB_SELLER_ID and EVO_HUB_SELLER_KEY (`hub market seller-key`).")
        return 0
    print(f"Seller {found['seller']}, key {found['key_id']}, public key {found['public_key']}")
    print(f"Signals to project {found['signal_project'] or '(none)'}; payment instructions:")
    print(found["payment_instructions"])
    return 0


def _print_listing(item: dict) -> None:
    latest = item["bundles"][0]["version"] if item["bundles"] else "no bundle"
    print(
        f"{item['plugin']} ({item['publisher']}): {item['state']}, {_usd(item['usd_per_hub_month'])} per hub "
        f"per month, {latest}, {item['active_licenses']} active licences"
    )


@client_command
def cmd_listing_list(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", "/v1/seller/listings")
    if args.json:
        print_json(found)
        return 0
    rows = [
        (
            i["plugin"],
            i["publisher"],
            i["state"],
            _usd(i["usd_per_hub_month"]),
            i["bundles"][0]["version"] if i["bundles"] else "-",
            i["active_licenses"],
            i["owner"] or "-",
        )
        for i in found["listings"]
    ]
    table(("PLUGIN", "PUBLISHER", "STATE", "PER HUB-MONTH", "LATEST", "LICENCES", "OWNER"), rows)
    return 0


@client_command
def cmd_listing_add(args) -> int:
    hub, _ = signed_in()
    body = {"plugin": args.plugin, "publisher": args.publisher, "signer": args.signer, "usd_per_hub_month": args.usd}
    found = hub.call("POST", "/v1/seller/listings", body)
    if args.json:
        print_json(found)
    else:
        _print_listing(found)
        print(f"Upload a signed bundle with `hub market bundle upload {args.plugin} FILE --signature SIG` to list it.")
    return 0


@client_command
def cmd_listing_set(args) -> int:
    hub, _ = signed_in()
    body = {"usd_per_hub_month": args.usd, "signer": args.signer, "state": args.state}
    found = hub.call("PATCH", f"/v1/seller/listings/{args.plugin}", {k: v for k, v in body.items() if v is not None})
    if args.json:
        print_json(found)
    else:
        _print_listing(found)
    return 0


@client_command
def cmd_bundle_upload(args) -> int:
    import base64
    import json
    from pathlib import Path

    hub, _ = signed_in()
    try:
        data = Path(args.file).read_bytes()
        signature = json.loads(Path(args.signature).read_text())
    except (OSError, ValueError) as exc:
        raise HubError(f"cannot read the bundle or its signature: {exc}") from None
    body = {"bundle": base64.b64encode(data).decode(), "signature": signature}
    found = hub.call("POST", f"/v1/seller/listings/{args.plugin}/bundles", body)
    if args.json:
        print_json(found)
    else:
        _print_listing(found)
    return 0


def _order_rows(orders: list[dict]) -> list[tuple]:
    return [
        (
            o["number"],
            o["plugin"],
            o["buyer_name"],
            o["buyer_hub"],
            o["months"],
            _usd(o["usd_total"]),
            o["state"],
            o["invoice"] or "-",
            o["valid_until"] or "-",
        )
        for o in orders
    ]


ORDER_HEADER = ("ORDER", "PLUGIN", "BUYER", "HUB", "MONTHS", "TOTAL", "STATE", "INVOICE", "UNTIL")


@client_command
def cmd_orders(args) -> int:
    hub, _ = signed_in()
    path = "/v1/seller/orders" + (f"?{urlencode({'state': args.state})}" if args.state else "")
    found = hub.call("GET", path)
    if args.json:
        print_json(found)
    elif not found["orders"]:
        print("No order" + (f" is {args.state}." if args.state else " yet."))
    else:
        table(ORDER_HEADER, _order_rows(found["orders"]))
    return 0


@client_command
def cmd_paid(args) -> int:
    hub, _ = signed_in()
    found = hub.call("POST", f"/v1/seller/orders/{args.number}/paid", {"invoice": args.invoice})
    if args.json:
        print_json(found)
    else:
        print(
            f"Order {found['number']} is paid (invoice {found['invoice']}): licence {found['license_id']} covers "
            f"{found['plugin']} for hub {found['buyer_hub']} from {found['valid_from']} until {found['valid_until']}."
        )
        print("The buying hub receives it at its next check.")
    return 0


@client_command
def cmd_cancel(args) -> int:
    hub, _ = signed_in()
    found = hub.call("POST", f"/v1/seller/orders/{args.number}/cancel")
    if args.json:
        print_json(found)
    else:
        print(f"Order {found['number']} is cancelled.")
    return 0


def _print_revenue(found: dict) -> None:
    rows = [
        (
            line["publisher"],
            line["orders"],
            _usd(line["gross_usd"]),
            _usd(line["publisher_usd"]),
            _usd(line["platform_usd"]),
            when(line["paid_out"]["paid_at"]) if line["paid_out"] else "not yet",
        )
        for line in found["publishers"]
    ]
    print(
        f"Revenue of {found['month']}: {_usd(found['gross_usd'])}, {_usd(found['publisher_usd'])} to publishers, "
        f"{_usd(found['platform_usd'])} to the platform"
    )
    if rows:
        table(("PUBLISHER", "ORDERS", "GROSS", "PUBLISHER 80%", "PLATFORM 20%", "PAID OUT"), rows)


@client_command
def cmd_revenue(args) -> int:
    hub, _ = signed_in()
    path = "/v1/seller/revenue" + (f"?{urlencode({'month': args.month})}" if args.month else "")
    found = hub.call("GET", path)
    if args.json:
        print_json(found)
    else:
        _print_revenue(found)
    return 0


@client_command
def cmd_payout(args) -> int:
    hub, _ = signed_in()
    found = hub.call(
        "POST", "/v1/seller/payouts", {"month": args.month, "publisher": args.publisher, "note": args.note}
    )
    if args.json:
        print_json(found)
    else:
        print(f"Marked the share of {args.publisher} for {args.month} paid out.")
        _print_revenue(found)
    return 0


# The parser


def _source(parser) -> None:
    parser.add_argument("--source", type=int, metavar="ID", help="the market that lists it (`hub market source list`)")


def _name(parser) -> None:
    parser.add_argument("name", metavar="NAME", help="the plugin's id, as the market lists it")
    _source(parser)


def _sources(msub) -> None:
    source = msub.add_parser("source", help="the markets the hub reads: GitHub repos holding market.json (hub admins)")
    ssub = source.add_subparsers(dest="market_source_command", required=True)
    listed = ssub.add_parser("list", help="every market, the operator's first, with its last read")
    json_option(listed, returns_object(*SOURCES_KEYS, schema="MarketSources"))
    listed.set_defaults(func=cmd_source_list)
    added = ssub.add_parser("add", help="read a market from a GitHub repo, now and every six hours")
    added.add_argument("repo", metavar="OWNER/REPO", help="the GitHub repo holding the index")
    added.add_argument("--ref", default="main", help="the branch or tag to read (default: main)")
    added.add_argument("--path", default="market.json", help="the index's path in the repo (default: market.json)")
    added.add_argument(
        "--token-stdin",
        action="store_true",
        help="read a GitHub token for a private repo from stdin; sealed on the hub",
    )
    json_option(added, returns_object(*SOURCE_KEYS, schema="MarketSource"))
    added.set_defaults(func=cmd_source_add)
    for verb, func, text, output in (
        ("remove", cmd_source_remove, "stop reading a market", returns_object(*SOURCES_KEYS, schema="MarketSources")),
        ("refresh", cmd_source_refresh, "read a market again now", returns_object(*SOURCE_KEYS, schema="MarketSource")),
    ):
        changed = ssub.add_parser(verb, help=text)
        changed.add_argument("id", type=int, metavar="ID", help="the source's id, as `source list` prints it")
        json_option(changed, output)
        changed.set_defaults(func=func)


def _selling(msub) -> None:
    key = msub.add_parser(
        "seller-key", help="write a new seller key to a file (0600); prints only its public key and id"
    )
    key.add_argument("file", metavar="FILE", help="where to write it; never overwritten")
    json_option(key, returns_object("file", "key_id", "public_key"))
    key.set_defaults(func=cmd_seller_key)

    info = msub.add_parser("seller", help="whether the hub sells plugins: its seller id and public key")
    json_option(info, returns_object(*SELLER_KEYS, schema="SellerInfo"))
    info.set_defaults(func=cmd_seller_info)

    listing = msub.add_parser(
        "listing", help="the paid plugins the hub sells (their publishers, signed in with GitHub)"
    )
    lsub = listing.add_subparsers(dest="market_listing_command", required=True)
    listed = lsub.add_parser("list", help="every listing, with its price, latest bundle and active licences")
    json_option(listed, returns_object("listings", schema="Listings"))
    listed.set_defaults(func=cmd_listing_list)
    added = lsub.add_parser("add", help="list a plugin at a price in USD per hub per month, as a draft")
    added.add_argument("plugin", metavar="PLUGIN")
    added.add_argument("--publisher", required=True, metavar="PUBLISHER")
    added.add_argument("--signer", required=True, metavar="SIGNER", help="what signs its bundles")
    added.add_argument("--usd", required=True, metavar="USD", help="the price per hub per month, such as 49.50")
    json_option(added, returns_object(*LISTING_KEYS, schema="Listing"))
    added.set_defaults(func=cmd_listing_add)
    changed = lsub.add_parser("set", help="change a listing's price, signer or state")
    changed.add_argument("plugin", metavar="PLUGIN")
    changed.add_argument("--usd", metavar="USD")
    changed.add_argument("--signer", metavar="SIGNER")
    changed.add_argument("--state", choices=("listed", "withdrawn"))
    json_option(changed, returns_object(*LISTING_KEYS, schema="Listing"))
    changed.set_defaults(func=cmd_listing_set)

    bundle = msub.add_parser("bundle", help="the signed bundles of a listing")
    bsub = bundle.add_subparsers(dest="market_bundle_command", required=True)
    upload = bsub.add_parser("upload", help="upload a signed bundle (tar.gz) of a listed plugin")
    upload.add_argument("plugin", metavar="PLUGIN")
    upload.add_argument("file", metavar="FILE", help="the bundle, as `evo-agents plugin pack` writes it")
    upload.add_argument(
        "--signature", required=True, metavar="FILE", help="its signature, from `evo-agents plugin sign`"
    )
    json_option(upload, returns_object(*LISTING_KEYS, schema="Listing"))
    upload.set_defaults(func=cmd_bundle_upload)

    orders = msub.add_parser("orders", help="the orders buying hubs placed (hub admins of a seller)")
    orders.add_argument("--state", choices=("open", "paid", "cancelled"))
    json_option(orders, returns_object("orders", schema="Orders"))
    orders.set_defaults(func=cmd_orders)
    for verb, func, text in (
        ("paid", cmd_paid, "mark an order paid with its invoice number; issues the licence"),
        ("cancel", cmd_cancel, "cancel an open order"),
    ):
        acted = msub.add_parser(verb, help=text)
        acted.add_argument("number", metavar="NUMBER", help="the order's number, such as ORD-2026-0001")
        if verb == "paid":
            acted.add_argument("--invoice", required=True, metavar="INVOICE", help="the invoice it was paid against")
        json_option(acted, returns_object(*ORDER_KEYS, schema="Order"))
        acted.set_defaults(func=func)

    revenue = msub.add_parser("revenue", help="a month's revenue by publisher, split 80/20")
    revenue.add_argument("--month", metavar="YYYY-MM", help="this month in UTC when left out")
    json_option(revenue, returns_object(*REVENUE_KEYS, schema="Revenue"))
    revenue.set_defaults(func=cmd_revenue)
    payout = msub.add_parser("payout", help="mark a publisher's share of a month paid out, once the month is over")
    payout.add_argument("--month", required=True, metavar="YYYY-MM")
    payout.add_argument("--publisher", required=True, metavar="PUBLISHER")
    payout.add_argument("--note", help="how it was paid, for the other hub admins")
    json_option(payout, returns_object(*REVENUE_KEYS, schema="Revenue"))
    payout.set_defaults(func=cmd_payout)


def _buying(msub) -> None:
    buy = msub.add_parser("buy", help="order a paid plugin from its seller; prints how to pay it (hub admins)")
    _name(buy)
    buy.add_argument("--months", type=int, required=True, metavar="N", help="how many months the licence covers")
    buy.add_argument("--name", dest="contact_name", required=True, metavar="NAME", help="who the seller invoices")
    buy.add_argument("--email", required=True, metavar="EMAIL", help="where the seller sends the invoice")
    buy.add_argument("--trust", action="store_true", help="trust the publisher's signer, if the hub does not yet")
    json_option(buy, returns_object("purchase", "instructions", "trusted_publisher", schema="Bought"))
    buy.set_defaults(func=cmd_buy)
    licenses = msub.add_parser("licenses", help="the hub's orders of paid plugins and their licences (hub admins)")
    licenses.add_argument(
        "--check",
        action="store_true",
        help="ask the sellers now, install what a licence covers and turn off what lapsed",
    )
    json_option(
        licenses,
        returns_object(
            "hub",
            "purchases",
            schema="Purchases",
            variants=(("--check", returns_object(*CHECKED_KEYS, schema="Checked")),),
        ),
    )
    licenses.set_defaults(func=cmd_licenses)
    hub_id = msub.add_parser("hub-id", help="the hub's stable id, which a seller's licence names")
    json_option(hub_id, returns_object("hub", "url", schema="HubIdentity"))
    hub_id.set_defaults(func=cmd_hub_id)


def register_market(hsub) -> None:
    market = hsub.add_parser("market", help="the plugin markets: browse them, install from them, add them")
    msub = market.add_subparsers(dest="market_command", required=True)

    listed = msub.add_parser("list", help="the plugins of every market, with their status on the hub")
    listed.add_argument("--search", metavar="WORDS", help="words of the id, name, description or publisher")
    listed.add_argument("--industry", metavar="INDUSTRY")
    listed.add_argument("--category", metavar="CATEGORY")
    _source(listed)
    json_option(listed, returns_object(*LIST_KEYS, schema="MarketList"))
    listed.set_defaults(func=cmd_list)

    show = msub.add_parser("show", help="one plugin: publisher, signer, permissions, version, price and status")
    _name(show)
    json_option(show, returns_object(*DETAIL_KEYS, schema="MarketPluginDetail"))
    show.set_defaults(func=cmd_show)

    install = msub.add_parser("install", help="install or upgrade a free plugin from its GitHub release (hub admins)")
    _name(install)
    install.add_argument("--version", metavar="VERSION", help="the version to install (default: the latest release)")
    install.add_argument("--trust", action="store_true", help="trust the publisher's signer, if the hub does not yet")
    json_option(install, returns_object(*INSTALLED_KEYS, schema="MarketInstalled"))
    install.set_defaults(func=cmd_install)

    request = msub.add_parser("request", help="ask the hub admins to install a plugin for a project you administer")
    _name(request)
    request.add_argument("--project", required=True, metavar="PROJECT")
    json_option(request, returns_object(*REQUEST_KEYS, schema="MarketRequest"))
    request.set_defaults(func=cmd_request)
    _sources(msub)
    _buying(msub)
    _selling(msub)


def register_publishers(psub) -> None:
    """``hub plugin publisher list|add|remove``."""
    publisher = psub.add_parser("publisher", help="the publishers whose signed plugins the hub accepts (hub admins)")
    usub = publisher.add_subparsers(dest="publisher_command", required=True)
    output = returns_array(*PUBLISHER_KEYS, schema="PluginPublisher")
    listed = usub.add_parser("list", help="every publisher and what signs for it: the operator's and the trusted")
    json_option(listed, output)
    listed.set_defaults(func=cmd_publisher_list)
    added = usub.add_parser("add", help="trust a signer for a publisher")
    added.add_argument("publisher", metavar="PUBLISHER", help="the publisher's id, as its manifests name it")
    added.add_argument("signer", metavar="SIGNER", help="sigstore:OWNER/REPO/.github/workflows/FILE.yml or ed25519:KEY")
    added.add_argument("--note", help="why, for the other hub admins")
    json_option(added, output)
    added.set_defaults(func=cmd_publisher_add)
    removed = usub.add_parser("remove", help="stop trusting a signer hub admins trusted")
    removed.add_argument("id", type=int, metavar="ID", help="the signer's id, as `publisher list` prints it")
    json_option(removed, output)
    removed.set_defaults(func=cmd_publisher_remove)
