"""``evo-agents hub provider-account``: a member's API keys of model providers (/v1/provider-accounts,
``docs/credentials.md``, section "Provider accounts").

``add NAME --provider ID --project P`` creates one or replaces it whole: the base URL and API format are the catalog's
unless ``--base-url`` and ``--format`` say otherwise, the models are those of ``--model`` plus the catalog's default
or the endpoint's list with ``--models-from``. The key never comes from the command line, where ``ps`` and the
shell's history would see it: it is read from stdin when stdin is not a terminal, else asked for without echo; or
``--from-secret NAME`` takes the value of one of the member's env secrets, which the hub copies without sending it
here. ``set`` changes what it is given and keeps the rest, the key included unless ``--key`` or ``--from-secret``;
``pause``, ``resume`` and ``delete`` do what they say; ``test`` asks the endpoint with the stored key and prints
whether it took it and how many models it lists, ``models`` prints them. ``quota NAME`` has the hub read what a Z.ai
account has left of its five-hour and weekly windows and keep it as the account's last usage (``--agent HANDLE``: an
account of an agent of ``--project``). No command prints a key.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Command classes):
:class:`ProviderAccounts` is the gateway to /v1/provider-accounts and :class:`AgentProviderAccounts` to the provider
accounts of a project's agents; :func:`write_body` turns the options ``add`` and ``set`` share into the body to write
and :func:`account_lines` says an account to a person. :func:`provider_text` and :func:`print_gateway` are what ``hub
account`` prints of a provider account.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from evo_agents.clikit import Arg, Group, Result, Stderr, Table, Text, UsageError, Views, returns_array, returns_object
from evo_agents.hub.api import route
from evo_agents.hub.cli.base import EXIT_FAILED, HubCommand, HubContext, ProjectResource
from evo_agents.hub.client import Hub
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
QUOTA_KEYS = ("ok", "usage", "status", "error")
SHOWN_MODELS = 8  # of an account's models, on its line "models:"
NO_KEY = (
    "no API key: pipe it on stdin, such as `pbpaste | evo-agents hub provider-account add NAME ...`, run the command in "
    "a terminal to be asked for it, or give --from-secret NAME"
)
NOTHING_TO_CHANGE = "nothing to change: give --base-url, --model, --project, --worker, --weight, --key ..."


class ProviderAccounts:
    """A member's provider accounts: /v1/provider-accounts and the routes below it (Gateway)."""

    root = route("provider-accounts")

    def __init__(self, hub: Hub):
        self.hub = hub

    def path(self, name: str, tail: str = "") -> str:
        return f"{self.root}/{quote(name, safe='')}{tail}"

    def call(self, method: str, path: str, body: dict | None = None) -> Any:
        return self.hub.call(method, path, body)

    def list(self) -> list[dict]:
        return self.call("GET", self.root)

    def get(self, name: str) -> dict:
        return self.call("GET", self.path(name))

    def put(self, name: str, body: dict) -> dict:
        return self.call("PUT", self.path(name), body)

    def change(self, name: str, body: dict) -> dict:
        return self.call("PATCH", self.path(name), body)

    def delete(self, name: str) -> None:
        self.call("DELETE", self.path(name))

    def probe(self, name: str, body: dict) -> dict:
        return self.call("POST", self.path(name, "/probe"), body)

    def quota(self, name: str) -> dict | None:
        return self.call("POST", self.path(name, "/quota"), {})


class AgentProviderAccounts(ProjectResource):
    """The provider accounts of a project's agents: POST /v1/projects/{p}/agents/{handle}/provider-accounts/{name}/quota
    (Gateway)."""

    collection = "agents"

    def quota(self, handle: str, name: str) -> dict | None:
        return self.call("POST", handle.removeprefix("@"), "provider-accounts", name, "quota", body={})


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


def key_of(name: str) -> str:
    """The API key read; UsageError when none came."""
    key = read_key(name)
    if key is None:
        raise UsageError(NO_KEY)
    return key


def day_of(day: str) -> str | None:
    try:
        return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC).isoformat()
    except ValueError:
        return None


def write_body(args: argparse.Namespace) -> dict:
    """The fields ``add`` and ``set`` share, from ``args``; UsageError when ``--expires`` is no day."""
    body: dict = {}
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
        body["expires_at"] = day_of(args.expires)
        if body["expires_at"] is None:
            raise UsageError(f"--expires takes a day as YYYY-MM-DD, such as 2027-01-31, not {args.expires!r}")
    return body


def account_lines(found: dict) -> list[str]:
    """What a person reads of a provider account."""
    models = ", ".join(found["models"][:SHOWN_MODELS]) + (" ..." if len(found["models"]) > SHOWN_MODELS else "")
    lines = [
        f"{found['name']}: {found['provider_name']} ({found['provider']}), {found['api_format']} at {found['base_url']}",
        f"  models: {models or '-'}",
        f"  projects: {', '.join(found['projects'])}; workers: {', '.join(found['workers']) or 'any of yours'}",
        f"  state: {found['state']}, weight {found['weight']}, priority {found['priority']}",
    ]
    if found["expires_at"]:
        lines.append(f"  expires: {found['expires_at']}")
    return lines


def said(found: dict, first: str) -> Result:
    return Result(found, Text(first, *account_lines(found)))


def window_line(name: str, window: dict) -> str:
    share = f"{window['utilization'] * 100:.0f}%"
    resets = window.get("resets_at")
    return f"  {name}: {share} used" + (f", resets {resets}" if resets else "")


def provider_text(a: dict) -> str:
    """Who an account of `hub account list` signs in to; for a provider account, its provider, host, API format and
    count of models."""
    g = a.get("gateway")
    return (
        a["provider"] if not g else f"{g['provider']} ({g['base_url_host']}, {g['api_format']}, {g['models']} models)"
    )


def gateway_lines(a: dict) -> list[str]:
    """The line `hub account show` prints of a provider account, none for another account: what it is, and what the
    gateway last said of it."""
    if not (g := a.get("gateway")):
        return []
    return [
        f"  Provider account: {provider_text(a)}; the gateway: {g['status']}"
        + (f" ({g['reason']})." if g["reason"] else ".")
    ]


def print_gateway(a: dict) -> None:
    """Print :func:`gateway_lines` of ``a``."""
    for line in gateway_lines(a):
        print(line)


NAME = Arg("name", metavar="NAME")


def write_options(*, add: bool) -> tuple[Arg, ...]:
    """The options ``add`` and ``set`` share; ``--project`` is required by ``add``."""
    return (
        Arg("name", metavar="NAME", help="unique among your secrets, such as openrouter-main"),
        Arg("--base-url", metavar="URL", help="the https root of its API; the catalog's when left out"),
        Arg("--format", choices=API_FORMATS, help="its API format; the catalog's when left out"),
        Arg("--model", action="append", default=[], metavar="MODEL", help="a model it serves; repeatable"),
        Arg("--models-from", choices=("endpoint", "catalog"), help="add the endpoint's or catalog's models"),
        Arg("--project", action="append", default=[], required=add, metavar="PROJECT", help="writer role; repeatable"),
        Arg("--worker", action="append", default=[], metavar="WORKER", help="one of yours; repeatable"),
        Arg("--from-secret", metavar="SECRET", help="the key is the value of this env secret of yours"),
        Arg("--expires", metavar="YYYY-MM-DD", help="no run takes it from 00:00 UTC of that day"),
        Arg("--weight", type=int, help="its share in a weighted pool; 0 leaves it"),
        Arg("--priority", type=int, help="the lower, the sooner in a fill-first pool"),
    )


ONE = returns_object(*ACCOUNT_KEYS, schema="ProviderAccount")
PROBE = returns_object(*PROBE_KEYS, schema="ProviderProbe")
PROBE_MODEL = Arg("--model", metavar="MODEL", help="a model to ask when the endpoint lists none")


class ProviderAdd(HubCommand):
    name = "add"
    help = "add a provider account, or replace it whole; the key from stdin or a prompt"
    arguments = (
        Arg("--provider", required=True, metavar="ID", help="its id in `evo-agents hub model-catalog`"),
        Arg("--paused", action="store_true", help="add it paused"),
        *write_options(add=True),
    )
    output = ONE

    def check(self, args: argparse.Namespace) -> None:
        write_body(args)

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body: dict = {"provider": args.provider} | write_body(args)
        if args.paused:
            body["paused"] = True
        hub = ctx.hub  # signed in before the key is asked for: not signed in costs no paste
        if args.from_secret is not None:
            body["from_secret"] = args.from_secret
        else:
            body["value"] = key_of(args.name)
        written = ProviderAccounts(hub).put(args.name, body)
        return said(written, f"{'Created' if written['created'] else 'Replaced'} provider account {args.name}.")


class ProviderSet(HubCommand):
    name = "set"
    help = "change a provider account; what is not given stays, the key included"
    arguments = (
        Arg("--key", action="store_true", help="a new key, from stdin or a prompt; its id stays"),
        Arg("--no-expiry", action="store_true", help="drop its end"),
        *write_options(add=False),
    )
    output = ONE

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = write_body(args)
        if args.no_expiry:
            body["expires_at"] = None
        hub = ctx.hub
        if args.from_secret is not None:
            body["from_secret"] = args.from_secret
        elif args.key:
            body["value"] = key_of(args.name)
        if not body:
            raise UsageError(NOTHING_TO_CHANGE)
        return said(ProviderAccounts(hub).change(args.name, body), f"Changed provider account {args.name}.")


class ProviderPause(HubCommand):
    name = "pause"
    help = "no new run takes it"
    arguments = (NAME,)
    output = ONE

    def run(self, ctx: HubContext) -> Result:
        found = ProviderAccounts(ctx.hub).change(ctx.args.name, {"paused": True})
        return said(found, f"Changed provider account {ctx.args.name}.")


class ProviderResume(HubCommand):
    name = "resume"
    help = "runs may take it again"
    arguments = (NAME,)
    output = ONE

    def run(self, ctx: HubContext) -> Result:
        found = ProviderAccounts(ctx.hub).change(ctx.args.name, {"paused": False})
        return said(found, f"Changed provider account {ctx.args.name}.")


class ProviderShow(HubCommand):
    name = "show"
    help = "one provider account, without its key"
    arguments = (NAME,)
    output = ONE

    def run(self, ctx: HubContext) -> Result:
        found = ProviderAccounts(ctx.hub).get(ctx.args.name)
        return Result(found, Text(*account_lines(found)))


class ProviderList(HubCommand):
    name = "list"
    help = "your provider accounts, without their keys"
    output = returns_array(*ACCOUNT_KEYS, schema="ProviderAccount")

    def run(self, ctx: HubContext) -> Result:
        found = ProviderAccounts(ctx.hub).list()
        rows = [
            (a["name"], a["provider"], a["api_format"], str(len(a["models"])), ",".join(a["projects"]), a["state"])
            for a in found
        ]
        return Result(found, Table(("NAME", "PROVIDER", "FORMAT", "MODELS", "PROJECTS", "STATE"), rows, bare=True))


class ProviderDelete(HubCommand):
    name = "delete"
    help = "delete a provider account: its key and bindings go"
    arguments = (NAME,)

    def run(self, ctx: HubContext) -> Result:
        ProviderAccounts(ctx.hub).delete(ctx.args.name)
        return Result(
            None, Text(f"Deleted provider account {ctx.args.name}: its key and bindings are gone, its leases revoked.")
        )


def probed(ctx: HubContext, listing: bool) -> Result:
    """Ask the account's endpoint with its key: the models it lists when ``listing``, and whether it took the key."""
    name = ctx.args.name
    found = ProviderAccounts(ctx.hub).probe(name, {"model": ctx.args.model} if ctx.args.model else {})
    status = 0 if found["ok"] else EXIT_FAILED
    if ctx.args.json:
        return Result(found, status=status)
    models = Text(*found["models"]) if listing else Text()
    if found["ok"]:
        told = f"{name}: the endpoint took the key and lists {found['count']} models."
    else:
        told = f"{name}: the endpoint refused: {found['error']}"
    return Result(found, Views(models, Stderr(Text(told))), status)


class ProviderTest(HubCommand):
    name = "test"
    help = "call its endpoint with its key: whether it took it, how many models it lists"
    arguments = (NAME, PROBE_MODEL)
    output = PROBE

    def run(self, ctx: HubContext) -> Result:
        return probed(ctx, listing=False)


class ProviderModels(HubCommand):
    name = "models"
    help = "the models its endpoint lists, one per line"
    arguments = (NAME, PROBE_MODEL)
    output = PROBE

    def run(self, ctx: HubContext) -> Result:
        return probed(ctx, listing=True)


class ProviderQuota(HubCommand):
    name = "quota"
    help = "have the hub read what a Z.ai account has left of its five-hour and weekly windows, and keep it"
    arguments = (
        NAME,
        Arg("--agent", metavar="HANDLE", help="an account of this agent, not yours"),
        Arg("--project", metavar="PROJECT", help="the agent's project (default: the harness's)"),
    )
    output = returns_object(*QUOTA_KEYS, schema="ProviderQuota")

    def check(self, args: argparse.Namespace) -> None:
        if args.project and not args.agent:
            raise UsageError("--project goes with --agent")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        if args.agent:
            found = AgentProviderAccounts.of(ctx).quota(args.agent, args.name) or {}
        else:
            found = ProviderAccounts(ctx.hub).quota(args.name) or {}
        if args.json:
            return Result(found, status=0 if found["ok"] else EXIT_FAILED)
        if not found["ok"]:
            return Result(found, Stderr(Text(f"{args.name}: the quota was not read: {found['error']}")), EXIT_FAILED)
        usage = found["usage"]
        windows = [window_line(name, window) for name, window in sorted(usage["windows"].items())]
        return Result(found, Text(f"{args.name}: {usage['status']}", *windows))


PROVIDER_ACCOUNT = Group(
    "provider-account",
    "your API keys of model providers, reached through the gateway",
    [
        ProviderAdd(),
        ProviderSet(),
        ProviderPause(),
        ProviderResume(),
        ProviderShow(),
        ProviderList(),
        ProviderDelete(),
        ProviderTest(),
        ProviderModels(),
        ProviderQuota(),
    ],
)
