"""``evo-agents hub provider-account``: a member's API keys of model providers (/v1/provider-accounts,
``docs/credentials.md``, section "Provider accounts").

``add NAME --provider ID --project P`` creates one or replaces it whole: the base URL and API format are the catalog's
unless ``--base-url`` and ``--format`` say otherwise, the models are those of ``--model`` plus the catalog's default
or the endpoint's list with ``--models-from``. The key never comes from the command line, where ``ps`` and the
shell's history would see it: it is read from stdin when stdin is not a terminal, else asked for without echo; or
``--from-secret NAME`` takes the value of one of the member's env secrets, which the hub copies without sending it
here. ``set`` changes what it is given and keeps the rest, the key included unless ``--key`` or ``--from-secret``;
``pause``, ``resume`` and ``delete`` do what they say; ``test`` asks the endpoint with the stored key and prints
whether it took it and how many models it lists, ``models`` prints them. No command prints a key.
"""

from __future__ import annotations

import getpass
import sys
from datetime import UTC, datetime
from urllib.parse import quote

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table
from evo_agents.hub.contract import json_option, returns_array, returns_object
from evo_agents.hub.provider_accounts import API_FORMATS

ACCOUNT_KEYS = (
    "name",
    "provider",
    "provider_name",
    "alias",
    "base_url",
    "api_format",
    "models",
    "projects",
    "workers",
    "weight",
    "priority",
    "paused_at",
    "cooldown_until",
    "cooldown_reason",
    "last_leased_at",
    "expires_at",
    "state",
    "shares",
    "created_at",
    "updated_at",
)
PROBE_KEYS = ("ok", "status", "count", "models", "error")
EXIT_USAGE = 2
EXIT_FAILED = 1
NO_KEY = (
    "no API key: pipe it on stdin, such as `pbpaste | evo-agents hub provider-account add NAME ...`, run the command in "
    "a terminal to be asked for it, or give --from-secret NAME"
)


def _path(name: str, tail: str = "") -> str:
    return f"/v1/provider-accounts/{quote(name, safe='')}{tail}"


def _usage(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return EXIT_USAGE


def read_key(name: str, stdin=None) -> str | None:
    """The API key: stdin when it is not a terminal, its line break dropped; else asked for without echo."""
    stream = sys.stdin if stdin is None else stdin
    if stream is not None and stream.isatty():
        try:
            value = getpass.getpass(f"API key of provider account {name} (not shown): ")
        except EOFError:
            value = ""
    else:
        value = stream.read() if stream is not None else ""
    value = value.strip()
    return value or None


def _day(day: str) -> str | None:
    try:
        return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).isoformat()
    except ValueError:
        return None


def _common(args, body: dict) -> str | None:
    """The fields ``add`` and ``set`` share, from ``args``, into ``body``; why not, or None."""
    for flag, key in (("base_url", "base_url"), ("format", "api_format"), ("models_from", "models_from")):
        if getattr(args, flag) is not None:
            body[key] = getattr(args, flag)
    for flag, key in (("model", "models"), ("project", "projects"), ("worker", "workers")):
        if getattr(args, flag):
            body[key] = getattr(args, flag)
    for key in ("weight", "priority"):
        if getattr(args, key) is not None:
            body[key] = getattr(args, key)
    if args.expires is not None:
        body["expires_at"] = _day(args.expires)
        if body["expires_at"] is None:
            return f"--expires takes a day as YYYY-MM-DD, such as 2027-01-31, not {args.expires!r}"
    return None


def _print_account(found: dict) -> None:
    models = ", ".join(found["models"][:8]) + (" ..." if len(found["models"]) > 8 else "")
    print(
        f"{found['name']}: {found['provider_name']} ({found['provider']}), {found['api_format']} at {found['base_url']}"
    )
    print(f"  models: {models or '-'}")
    print(f"  projects: {', '.join(found['projects'])}; workers: {', '.join(found['workers']) or 'any of yours'}")
    print(f"  state: {found['state']}, weight {found['weight']}, priority {found['priority']}")
    if found["expires_at"]:
        print(f"  expires: {found['expires_at']}")


@client_command
def cmd_add(args) -> int:
    body: dict = {"provider": args.provider}
    refusal = _common(args, body)
    if refusal is not None:
        return _usage(refusal)
    if args.paused:
        body["paused"] = True
    hub, _ = signed_in()  # before the key is asked for: not signed in costs no paste
    if args.from_secret is not None:
        body["from_secret"] = args.from_secret
    else:
        key = read_key(args.name)
        if key is None:
            return _usage(NO_KEY)
        body["value"] = key
    written = hub.call("PUT", _path(args.name), body)
    if args.json:
        print_json(written)
        return 0
    print(f"{'Created' if written['created'] else 'Replaced'} provider account {args.name}.")
    _print_account(written)
    return 0


@client_command
def cmd_set(args) -> int:
    body: dict = {}
    refusal = _common(args, body)
    if refusal is not None:
        return _usage(refusal)
    if args.no_expiry:
        body["expires_at"] = None
    hub, _ = signed_in()
    if args.from_secret is not None:
        body["from_secret"] = args.from_secret
    elif args.key:
        key = read_key(args.name)
        if key is None:
            return _usage(NO_KEY)
        body["value"] = key
    if not body:
        return _usage("nothing to change: give --base-url, --model, --project, --worker, --weight, --key ...")
    return _changed(hub, args, body)


def _changed(hub, args, body: dict) -> int:
    found = hub.call("PATCH", _path(args.name), body)
    if args.json:
        print_json(found)
        return 0
    print(f"Changed provider account {args.name}.")
    _print_account(found)
    return 0


@client_command
def cmd_pause(args) -> int:
    hub, _ = signed_in()
    return _changed(hub, args, {"paused": True})


@client_command
def cmd_resume(args) -> int:
    hub, _ = signed_in()
    return _changed(hub, args, {"paused": False})


@client_command
def cmd_list(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", "/v1/provider-accounts")
    if args.json:
        print_json(found)
        return 0
    rows = [
        (a["name"], a["provider"], a["api_format"], str(len(a["models"])), ",".join(a["projects"]), a["state"])
        for a in found
    ]
    table(("NAME", "PROVIDER", "FORMAT", "MODELS", "PROJECTS", "STATE"), rows)
    return 0


@client_command
def cmd_show(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", _path(args.name))
    if args.json:
        print_json(found)
        return 0
    _print_account(found)
    return 0


@client_command
def cmd_delete(args) -> int:
    hub, _ = signed_in()
    hub.call("DELETE", _path(args.name))
    print(f"Deleted provider account {args.name}: its key and bindings are gone, its leases revoked.")
    return 0


@client_command
def cmd_test(args) -> int:
    hub, _ = signed_in()
    found = hub.call("POST", _path(args.name, "/probe"), {"model": args.model} if args.model else {})
    if args.json:
        print_json(found)
        return 0 if found["ok"] else EXIT_FAILED
    if args.list_models:
        for model in found["models"]:
            print(model)
    if found["ok"]:
        print(f"{args.name}: the endpoint took the key and lists {found['count']} models.", file=sys.stderr)
        return 0
    print(f"{args.name}: the endpoint refused: {found['error']}", file=sys.stderr)
    return EXIT_FAILED


def _write_flags(parser, *, add: bool) -> None:
    parser.add_argument("name", metavar="NAME", help="unique among your secrets, such as openrouter-main")
    parser.add_argument("--base-url", metavar="URL", help="the https root of its API; the catalog's when left out")
    parser.add_argument("--format", choices=API_FORMATS, help="its API format; the catalog's when left out")
    parser.add_argument("--model", action="append", default=[], metavar="MODEL", help="a model it serves; repeatable")
    parser.add_argument("--models-from", choices=("endpoint", "catalog"), help="add the endpoint's or catalog's models")
    parser.add_argument(
        "--project", action="append", default=[], required=add, metavar="PROJECT", help="writer role; repeatable"
    )
    parser.add_argument("--worker", action="append", default=[], metavar="WORKER", help="one of yours; repeatable")
    parser.add_argument("--from-secret", metavar="SECRET", help="the key is the value of this env secret of yours")
    parser.add_argument("--expires", metavar="YYYY-MM-DD", help="no run takes it from 00:00 UTC of that day")
    parser.add_argument("--weight", type=int, help="its share in a weighted pool; 0 leaves it")
    parser.add_argument("--priority", type=int, help="the lower, the sooner in a fill-first pool")
    json_option(parser, returns_object(*ACCOUNT_KEYS, schema="ProviderAccount"))


def register_provider_accounts(hsub) -> None:
    parser = hsub.add_parser("provider-account", help="your API keys of model providers, reached through the gateway")
    psub = parser.add_subparsers(dest="provider_account_command", required=True)
    add = psub.add_parser("add", help="add a provider account, or replace it whole; the key from stdin or a prompt")
    add.add_argument("--provider", required=True, metavar="ID", help="its id in `evo-agents hub model-catalog`")
    add.add_argument("--paused", action="store_true", help="add it paused")
    _write_flags(add, add=True)
    add.set_defaults(func=cmd_add)
    change = psub.add_parser("set", help="change a provider account; what is not given stays, the key included")
    change.add_argument("--key", action="store_true", help="a new key, from stdin or a prompt; its id stays")
    change.add_argument("--no-expiry", action="store_true", help="drop its end")
    _write_flags(change, add=False)
    change.set_defaults(func=cmd_set)
    for verb, func, help_text in (
        ("pause", cmd_pause, "no new run takes it"),
        ("resume", cmd_resume, "runs may take it again"),
        ("show", cmd_show, "one provider account, without its key"),
    ):
        command = psub.add_parser(verb, help=help_text)
        command.add_argument("name", metavar="NAME")
        json_option(command, returns_object(*ACCOUNT_KEYS, schema="ProviderAccount"))
        command.set_defaults(func=func)
    listed = psub.add_parser("list", help="your provider accounts, without their keys")
    json_option(listed, returns_array(*ACCOUNT_KEYS, schema="ProviderAccount"))
    listed.set_defaults(func=cmd_list)
    delete = psub.add_parser("delete", help="delete a provider account: its key and bindings go")
    delete.add_argument("name", metavar="NAME")
    delete.set_defaults(func=cmd_delete)
    for verb, listing, help_text in (
        ("test", False, "call its endpoint with its key: whether it took it, how many models it lists"),
        ("models", True, "the models its endpoint lists, one per line"),
    ):
        command = psub.add_parser(verb, help=help_text)
        command.add_argument("name", metavar="NAME")
        command.add_argument("--model", metavar="MODEL", help="a model to ask when the endpoint lists none")
        json_option(command, returns_object(*PROBE_KEYS, schema="ProviderProbe"))
        command.set_defaults(func=cmd_test, list_models=listing)
