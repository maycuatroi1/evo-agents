"""``evo-agents hub signal push|list``, ``hub signal keys list|add|archive`` and ``hub signal ingest
create|list|revoke``: a project's signals, the registry of its keys and its ingest keys, through the hub's routes
(``evo_agents.hub.server.signals``, ``evo_agents.hub.server.signal_keys``; ``docs/signals.md``).

``push`` sends one signal (``--key`` with ``--value`` for a number or ``--text`` for feedback) or a batch from
``--file``, JSON ``{"items": [...]}`` or the list alone, as the member signed in; it prints how many the hub stored and
each item it refused, and exits 1 when it refused one. ``list`` prints the newest signals the member may read, of one
``--key`` or all, in a range of measured times. ``keys add`` declares a key (an admin of the project), ``keys archive``
stops it taking signals. ``ingest create`` makes an ingest key for a service and prints its ``evi_`` token, the one
time the hub shows it; ``ingest revoke ID`` revokes one. A service sends its batches itself, with its ingest key, to
POST /v1/signals/<project>; this client always speaks as the member. ``--json`` prints what the hub answered. Standard
library only, like the rest of the client.

The commands are classes of ``evo_agents.clikit`` (docs/structure.md, section Commands): :class:`Signals`,
:class:`SignalKeys` and :class:`IngestKeys` are the gateways to the three collections of a project.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from evo_agents.clikit import Arg, Group, Result, Stderr, Table, Text, View, returns_array, returns_object
from evo_agents.hub.api import route
from evo_agents.hub.cli.base import HubCommand, HubContext, ProjectResource, when
from evo_agents.hub.client import HubError

EXIT_REFUSED = 1
KINDS = ("number", "feedback")
TEXT_SHOWN = 60  # characters of feedback a line of ``list`` shows
BATCH_KEYS = ("batch_id", "accepted", "duplicates", "rejected")
SIGNAL_KEYS = (
    "id",
    "key",
    "kind",
    "value",
    "text",
    "source",
    "measured_at",
    "tenant",
    "synthetic",
    "external_id",
    "label",
    "via",
    "ingest_key",
    "login",
    "run_id",
    "batch_id",
    "received_at",
)
KEY_KEYS = ("name", "kind", "unit", "description", "source", "level", "created_by", "created_at", "archived_at")
INGEST_KEYS = ("id", "name", "max_level", "created_by", "created_at", "last_used_at", "revoked_at", "revoked_by")

PROJECT = Arg("--project", required=True, metavar="PROJECT", help="the project of the signals")


class ProjectCollection(ProjectResource):
    """A collection of a project's API whose query leaves out a value of None or False."""

    def path(self, *parts: Any, **query: Any) -> str:
        return super().path(*parts, query={key: value for key, value in query.items() if value not in (None, False)})


class Signals(ProjectCollection):
    """A project's signals: GET /v1/projects/{p}/signals, and POST /v1/signals/{p}, where a batch goes (Gateway)."""

    collection = "signals"

    def push(self, batch: dict) -> Any:
        return self.hub.call("POST", route("signals", self.project), batch)

    def list(self, query: dict) -> list[dict]:
        return self.call("GET", **query)


class SignalKeys(ProjectCollection):
    """The registry of a project's signal keys: /v1/projects/{p}/signal-keys (Gateway)."""

    collection = "signal-keys"

    def list(self, archived: bool) -> list[dict]:
        return self.call("GET", archived=archived and "true")

    def add(self, body: dict) -> dict:
        return self.call("POST", body=body)

    def archive(self, name: str) -> dict:
        return self.call("POST", name, "archive")


class IngestKeys(ProjectCollection):
    """The ingest keys of a project: /v1/projects/{p}/signal-ingest-keys (Gateway)."""

    collection = "signal-ingest-keys"

    def create(self, body: dict) -> dict:
        return self.call("POST", body=body)

    def list(self, revoked: bool) -> list[dict]:
        return self.call("GET", revoked=revoked and "true")

    def revoke(self, id_: int) -> dict:
        return self.call("POST", id_, "revoke")


def read_json(file: str) -> Any:
    try:
        text = sys.stdin.read() if file == "-" else Path(file).expanduser().read_text(encoding="utf-8")
        return json.loads(text)
    except (OSError, UnicodeDecodeError) as exc:
        raise HubError(f"cannot read {file}: {exc}") from None
    except ValueError as exc:
        raise HubError(f"{file} is no JSON: {exc}") from None


# Signals


def item_of(args: argparse.Namespace) -> dict:
    """The one item the options of ``push`` give."""
    if not args.key or (args.value is None) == (args.text is None):
        raise HubError("push takes --file, or --key with one of --value (a number) and --text (feedback)")
    item = {"key": args.key, "synthetic": args.synthetic}
    named = (
        ("value", args.value),
        ("text", args.text),
        ("source", args.source),
        ("measured_at", args.at),
        ("tenant", args.tenant),
        ("external_id", args.external_id),
        ("level", args.level),
    )
    return item | {name: value for name, value in named if value is not None}


def batch_of(args: argparse.Namespace) -> dict:
    if args.file is None:
        return {"items": [item_of(args)]}
    if args.key or args.value is not None or args.text is not None:
        raise HubError("push takes --file or --key, not both")
    data = read_json(args.file)
    return {"items": data} if isinstance(data, list) else data


@dataclass(frozen=True)
class Pushed(Result):
    """What ``push`` found: ``refused``, the items the hub refused, goes to stderr after what is shown, with
    ``--json`` too."""

    refused: View = Text()

    def show(self, out: TextIO, as_json: bool) -> None:
        super().show(out, as_json)
        self.refused.write(out)


class SignalPush(HubCommand):
    name = "push"
    help = "send one signal, or a batch from a file, as yourself"
    arguments = (
        PROJECT,
        Arg("--file", metavar="FILE", help='JSON {"items": [...]} or the list alone; - for stdin'),
        Arg("--key", metavar="KEY", help="a key of the project's registry, as kb.search.zero_result_rate"),
        Arg("--value", type=float, metavar="NUMBER", help="the number, for a key of kind number"),
        Arg("--text", metavar="TEXT", help="the feedback, for a key of kind feedback"),
        Arg("--source", metavar="TEXT", help="who measured it; the key's source when left out"),
        Arg("--at", metavar="TIME", help="when it was measured, ISO 8601 with its offset; now when left out"),
        Arg("--tenant", metavar="TEXT", help="who it is about, such as a customer"),
        Arg("--synthetic", action="store_true", help="it comes from simulated people"),
        Arg("--external-id", metavar="ID", help="your id of it: sent again, it is stored once"),
        Arg("--level", metavar="LEVEL", help="its sensitivity; the key's, else the project's default"),
    )
    output = returns_object(*BATCH_KEYS, schema="SignalBatchResult")

    def run(self, ctx: HubContext) -> Result:
        batch = batch_of(ctx.args)
        done = Signals.of(ctx).push(batch)
        repeated = f", {done['duplicates']} sent before" if done["duplicates"] else ""
        refused = f", {len(done['rejected'])} refused" if done["rejected"] else ""
        said = Text(f"Batch {done['batch_id']}: {done['accepted']} stored{repeated}{refused}.")
        items = Text(*(f"  item {item['index']} ({item['key']}): {item['reason']}" for item in done["rejected"]))
        return Pushed(done, said, EXIT_REFUSED if done["rejected"] else 0, Stderr(items))


def shown_value(signal: dict) -> str:
    if signal["value"] is not None:
        return f"{signal['value']:g}"
    text = " ".join(signal["text"].split())
    return text if len(text) <= TEXT_SHOWN else text[: TEXT_SHOWN - 3] + "..."


class SignalList(HubCommand):
    name = "list"
    help = "the newest signals you may read, of one key or all"
    arguments = (
        PROJECT,
        Arg("--key", metavar="KEY", help="one key; all when left out"),
        Arg("--since", metavar="TIME", help="measured at or after, ISO 8601 with its offset"),
        Arg("--until", metavar="TIME", help="measured before, ISO 8601 with its offset"),
        Arg("--tenant", metavar="TEXT"),
        Arg("--limit", type=int, metavar="N", help="at most N, 100 when left out, 1000 at most"),
    )
    output = returns_array(*SIGNAL_KEYS, schema="Signal")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        query = {"key": args.key, "since": args.since, "until": args.until, "tenant": args.tenant, "limit": args.limit}
        found = Signals.of(ctx).list(query)
        rows = [
            (
                when(s["measured_at"]),
                s["key"],
                shown_value(s),
                s["tenant"] or "-",
                s["ingest_key"] or s["login"] or "-",
                s["label"].get("level", "-"),
                "synthetic" if s["synthetic"] else "",
            )
            for s in found
        ]
        header = ("MEASURED", "KEY", "VALUE", "TENANT", "BY", "LEVEL", "")
        return Result(found, Table(header, rows, f"No signal in project {ctx.project} that you can read."))


# The registry of keys


def key_lines(key: dict) -> list[str]:
    archived = " (archived)" if key["archived_at"] else ""
    unit = f" in {key['unit']}" if key["unit"] else ""
    level = key["level"] or "the project's default level"
    lines = [f"{key['name']}{archived}: {key['kind']}{unit}, at {level}"]
    return lines + [f"  {name}: {key[name]}" for name in ("description", "source") if key[name]]


class KeysList(HubCommand):
    name = "list"
    help = "the keys of the registry"
    arguments = (PROJECT, Arg("--archived", action="store_true", help="archived keys too"))
    output = returns_array(*KEY_KEYS, schema="SignalKey")

    def run(self, ctx: HubContext) -> Result:
        found = SignalKeys.of(ctx).list(ctx.args.archived)
        rows = [
            (
                k["name"],
                k["kind"],
                k["unit"] or "-",
                k["level"] or "-",
                "archived" if k["archived_at"] else "",
                k["source"],
            )
            for k in found
        ]
        empty = f"No signal key in project {ctx.project}: an admin adds one with `evo-agents hub signal keys add`."
        return Result(found, Table(("KEY", "KIND", "UNIT", "LEVEL", "", "SOURCE"), rows, empty))


class KeysAdd(HubCommand):
    name = "add"
    help = "declare a key, so the hub takes its signals (an admin of the project)"
    arguments = (
        Arg("name", metavar="KEY", help="two to six dotted parts, as kb.search.zero_result_rate"),
        PROJECT,
        Arg("--kind", required=True, choices=KINDS),
        Arg("--unit", metavar="TEXT", help="as percent, ms or tickets"),
        Arg("--description", metavar="TEXT"),
        Arg("--source", metavar="TEXT", help="who measures it"),
        Arg("--level", metavar="LEVEL", help="the level its signals get when they name none"),
    )
    output = returns_object(*KEY_KEYS, schema="SignalKey")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = {"name": args.name, "kind": args.kind}
        named = (("unit", args.unit), ("description", args.description), ("source", args.source), ("level", args.level))
        body |= {name: value for name, value in named if value is not None}
        found = SignalKeys.of(ctx).add(body)
        return Result(found, Text(*key_lines(found)))


class KeysArchive(HubCommand):
    name = "archive"
    help = "stop a key taking signals; its signals stay"
    arguments = (Arg("name", metavar="KEY"), PROJECT)
    output = returns_object(*KEY_KEYS, schema="SignalKey")

    def run(self, ctx: HubContext) -> Result:
        found = SignalKeys.of(ctx).archive(ctx.args.name)
        return Result(found, Text(*key_lines(found)))


# Ingest keys


def ingest_line(key: dict) -> str:
    state = f"revoked {when(key['revoked_at'])} by {key['revoked_by']}" if key["revoked_at"] else "live"
    return f"ingest key {key['id']} {key['name']}: writes up to {key['max_level']}, {state}"


class IngestCreate(HubCommand):
    name = "create"
    help = "an ingest key for a service; prints its token once (an admin)"
    arguments = (
        Arg("name", metavar="NAME", help="the service it is for, unique among the live keys"),
        PROJECT,
        Arg("--max-level", metavar="LEVEL", help="the highest level it writes; the project's default"),
    )
    output = returns_object(*INGEST_KEYS, "token", schema="SignalIngestKeyIssued")

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        body = {"name": args.name} | ({"max_level": args.max_level} if args.max_level else {})
        found = IngestKeys.of(ctx).create(body)
        return Result(
            found,
            Text(
                f"Created {ingest_line(found)}.",
                f"Token, shown this once: {found['token']}",
                f"The service sends its batches to POST /v1/signals/{ctx.project} with Authorization: Bearer <token>.",
            ),
        )


class IngestList(HubCommand):
    name = "list"
    help = "the project's ingest keys, never a token"
    arguments = (PROJECT, Arg("--revoked", action="store_true", help="revoked keys too"))
    output = returns_array(*INGEST_KEYS, schema="SignalIngestKey")

    def run(self, ctx: HubContext) -> Result:
        found = IngestKeys.of(ctx).list(ctx.args.revoked)
        rows = [
            (
                k["id"],
                k["name"],
                k["max_level"],
                when(k["last_used_at"]),
                f"revoked {when(k['revoked_at'])}" if k["revoked_at"] else "live",
                k["created_by"],
            )
            for k in found
        ]
        header = ("ID", "NAME", "MAX LEVEL", "LAST USED", "STATE", "BY")
        return Result(found, Table(header, rows, f"No ingest key in project {ctx.project}."))


class IngestRevoke(HubCommand):
    name = "revoke"
    help = "revoke an ingest key: its next batch gets 401"
    arguments = (Arg("id", type=int, metavar="ID", help="the key's id, as ingest list prints it"), PROJECT)
    output = returns_object(*INGEST_KEYS, schema="SignalIngestKey")

    def run(self, ctx: HubContext) -> Result:
        found = IngestKeys.of(ctx).revoke(ctx.args.id)
        return Result(found, Text(ingest_line(found)))


SIGNAL = Group(
    "signal",
    "a project's signals: push, list, the registry of keys, ingest keys",
    [
        SignalPush(),
        SignalList(),
        Group(
            "keys",
            "the registry of the project's signal keys: list, add, archive",
            [KeysList(), KeysAdd(), KeysArchive()],
        ),
        Group(
            "ingest",
            "the ingest keys services write signals with: create, list, revoke",
            [IngestCreate(), IngestList(), IngestRevoke()],
        ),
    ],
)
