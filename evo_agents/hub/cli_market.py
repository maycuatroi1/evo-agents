"""``evo-agents hub market``: the plugin markets the hub reads (``evo_agents.hub.server.market``), and installing
from them or from a GitHub release (``evo_agents.hub.server.market_install``); docs/market.md.

- ``market list [--search WORDS] [--industry I] [--category C] [--source ID]`` prints the plugins of every market with
  their status on the hub; ``market show NAME [--source ID]`` one plugin with its publisher and signer, what its latest
  release asks for, its price and whether the hub trusts its signer;
- ``market install NAME [--source ID] [--version V] [--trust]`` (hub admins) installs or upgrades a free plugin from
  its GitHub release; a signer the hub does not trust yet is asked about at a terminal, or trusted with ``--trust``;
- ``market request NAME --project P [--source ID]`` (a project's admins) asks the hub admins to install one;
- ``market source list|add REPO [--ref] [--path] [--token-stdin]|remove ID|refresh ID`` (hub admins): the markets.

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


def installed(hub, path: str, body: dict, args) -> dict:
    """POST an install; asked once at a terminal when the hub does not trust the signer yet and --trust is not given."""
    try:
        return hub.call("POST", path, {**body, "trust_publisher": bool(args.trust)})
    except HubError as exc:
        if exc.status != 409 or not str(exc).startswith(UNTRUSTED) or args.trust or args.json:
            raise
        print(str(exc), file=sys.stderr)
        if not _confirmed("Trust this signer for the publisher from now on, and install?"):
            raise HubError("nothing was installed: pass --trust to trust the publisher's signer") from None
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
