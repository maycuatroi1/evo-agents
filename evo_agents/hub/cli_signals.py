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
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import quote, urlencode

from evo_agents.hub.cli_client import client_command, print_json, signed_in, table, when
from evo_agents.hub.client import HubError
from evo_agents.hub.contract import json_option, returns_array, returns_object

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


def _path(*parts: str, query: dict | None = None) -> str:
    path = "/" + "/".join(quote(str(part), safe="") for part in parts)
    given = {key: value for key, value in (query or {}).items() if value not in (None, False)}
    return f"{path}?{urlencode(given)}" if given else path


def _project_path(project: str, *parts: str, query: dict | None = None) -> str:
    return _path("v1", "projects", project, *parts, query=query)


def _call(method: str, path: str, body: dict | None = None):
    hub, _ = signed_in()
    return hub.call(method, path, body)


def _read_json(file: str):
    try:
        text = sys.stdin.read() if file == "-" else Path(file).expanduser().read_text(encoding="utf-8")
        return json.loads(text)
    except (OSError, UnicodeDecodeError) as exc:
        raise HubError(f"cannot read {file}: {exc}") from None
    except ValueError as exc:
        raise HubError(f"{file} is no JSON: {exc}") from None


# Signals


def _item(args) -> dict:
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


def _batch(args) -> dict:
    if args.file is None:
        return {"items": [_item(args)]}
    if args.key or args.value is not None or args.text is not None:
        raise HubError("push takes --file or --key, not both")
    data = _read_json(args.file)
    return {"items": data} if isinstance(data, list) else data


@client_command
def cmd_push(args) -> int:
    done = _call("POST", _path("v1", "signals", args.project), _batch(args))
    if args.json:
        print_json(done)
    else:
        repeated = f", {done['duplicates']} sent before" if done["duplicates"] else ""
        refused = f", {len(done['rejected'])} refused" if done["rejected"] else ""
        print(f"Batch {done['batch_id']}: {done['accepted']} stored{repeated}{refused}.")
    for item in done["rejected"]:
        print(f"  item {item['index']} ({item['key']}): {item['reason']}", file=sys.stderr)
    return EXIT_REFUSED if done["rejected"] else 0


def _shown(signal: dict) -> str:
    if signal["value"] is not None:
        return f"{signal['value']:g}"
    text = " ".join(signal["text"].split())
    return text if len(text) <= TEXT_SHOWN else text[: TEXT_SHOWN - 3] + "..."


@client_command
def cmd_list(args) -> int:
    query = {"key": args.key, "since": args.since, "until": args.until, "tenant": args.tenant, "limit": args.limit}
    found = _call("GET", _project_path(args.project, "signals", query=query))
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"No signal in project {args.project} that you can read.")
        return 0
    rows = [
        (
            when(s["measured_at"]),
            s["key"],
            _shown(s),
            s["tenant"] or "-",
            s["ingest_key"] or s["login"] or "-",
            s["label"].get("level", "-"),
            "synthetic" if s["synthetic"] else "",
        )
        for s in found
    ]
    table(("MEASURED", "KEY", "VALUE", "TENANT", "BY", "LEVEL", ""), rows)
    return 0


# The registry of keys


def _print_key(key: dict) -> None:
    archived = " (archived)" if key["archived_at"] else ""
    unit = f" in {key['unit']}" if key["unit"] else ""
    level = key["level"] or "the project's default level"
    print(f"{key['name']}{archived}: {key['kind']}{unit}, at {level}")
    for name in ("description", "source"):
        if key[name]:
            print(f"  {name}: {key[name]}")


@client_command
def cmd_keys_list(args) -> int:
    found = _call("GET", _project_path(args.project, "signal-keys", query={"archived": args.archived and "true"}))
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"No signal key in project {args.project}: an admin adds one with `evo-agents hub signal keys add`.")
        return 0
    rows = [
        (k["name"], k["kind"], k["unit"] or "-", k["level"] or "-", "archived" if k["archived_at"] else "", k["source"])
        for k in found
    ]
    table(("KEY", "KIND", "UNIT", "LEVEL", "", "SOURCE"), rows)
    return 0


@client_command
def cmd_keys_add(args) -> int:
    body = {"name": args.name, "kind": args.kind}
    named = (("unit", args.unit), ("description", args.description), ("source", args.source), ("level", args.level))
    body |= {name: value for name, value in named if value is not None}
    found = _call("POST", _project_path(args.project, "signal-keys"), body)
    if args.json:
        print_json(found)
    else:
        _print_key(found)
    return 0


@client_command
def cmd_keys_archive(args) -> int:
    found = _call("POST", _project_path(args.project, "signal-keys", args.name, "archive"))
    if args.json:
        print_json(found)
    else:
        _print_key(found)
    return 0


# Ingest keys


def _ingest_line(key: dict) -> str:
    state = f"revoked {when(key['revoked_at'])} by {key['revoked_by']}" if key["revoked_at"] else "live"
    return f"ingest key {key['id']} {key['name']}: writes up to {key['max_level']}, {state}"


@client_command
def cmd_ingest_create(args) -> int:
    body = {"name": args.name} | ({"max_level": args.max_level} if args.max_level else {})
    found = _call("POST", _project_path(args.project, "signal-ingest-keys"), body)
    if args.json:
        print_json(found)
        return 0
    print(f"Created {_ingest_line(found)}.")
    print(f"Token, shown this once: {found['token']}")
    print(f"The service sends its batches to POST /v1/signals/{args.project} with Authorization: Bearer <token>.")
    return 0


@client_command
def cmd_ingest_list(args) -> int:
    path = _project_path(args.project, "signal-ingest-keys", query={"revoked": args.revoked and "true"})
    found = _call("GET", path)
    if args.json:
        print_json(found)
        return 0
    if not found:
        print(f"No ingest key in project {args.project}.")
        return 0
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
    table(("ID", "NAME", "MAX LEVEL", "LAST USED", "STATE", "BY"), rows)
    return 0


@client_command
def cmd_ingest_revoke(args) -> int:
    found = _call("POST", _project_path(args.project, "signal-ingest-keys", args.id, "revoke"))
    if args.json:
        print_json(found)
    else:
        print(_ingest_line(found))
    return 0


# The parser


def _project(parser) -> None:
    parser.add_argument("--project", required=True, metavar="PROJECT", help="the project of the signals")


def _register_push(ssub) -> None:
    push = ssub.add_parser("push", help="send one signal, or a batch from a file, as yourself")
    _project(push)
    push.add_argument("--file", metavar="FILE", help='JSON {"items": [...]} or the list alone; - for stdin')
    push.add_argument("--key", metavar="KEY", help="a key of the project's registry, as kb.search.zero_result_rate")
    push.add_argument("--value", type=float, metavar="NUMBER", help="the number, for a key of kind number")
    push.add_argument("--text", metavar="TEXT", help="the feedback, for a key of kind feedback")
    push.add_argument("--source", metavar="TEXT", help="who measured it; the key's source when left out")
    push.add_argument("--at", metavar="TIME", help="when it was measured, ISO 8601 with its offset; now when left out")
    push.add_argument("--tenant", metavar="TEXT", help="who it is about, such as a customer")
    push.add_argument("--synthetic", action="store_true", help="it comes from simulated people")
    push.add_argument("--external-id", metavar="ID", help="your id of it: sent again, it is stored once")
    push.add_argument("--level", metavar="LEVEL", help="its sensitivity; the key's, else the project's default")
    json_option(push, returns_object(*BATCH_KEYS, schema="SignalBatchResult"))
    push.set_defaults(func=cmd_push)


def _register_keys(ssub) -> None:
    keys = ssub.add_parser("keys", help="the registry of the project's signal keys: list, add, archive")
    ksub = keys.add_subparsers(dest="keys_command", required=True)
    listed = ksub.add_parser("list", help="the keys of the registry")
    _project(listed)
    listed.add_argument("--archived", action="store_true", help="archived keys too")
    json_option(listed, returns_array(*KEY_KEYS, schema="SignalKey"))
    listed.set_defaults(func=cmd_keys_list)
    added = ksub.add_parser("add", help="declare a key, so the hub takes its signals (an admin of the project)")
    added.add_argument("name", metavar="KEY", help="two to six dotted parts, as kb.search.zero_result_rate")
    _project(added)
    added.add_argument("--kind", required=True, choices=KINDS)
    added.add_argument("--unit", metavar="TEXT", help="as percent, ms or tickets")
    added.add_argument("--description", metavar="TEXT")
    added.add_argument("--source", metavar="TEXT", help="who measures it")
    added.add_argument("--level", metavar="LEVEL", help="the level its signals get when they name none")
    json_option(added, returns_object(*KEY_KEYS, schema="SignalKey"))
    added.set_defaults(func=cmd_keys_add)
    archived = ksub.add_parser("archive", help="stop a key taking signals; its signals stay")
    archived.add_argument("name", metavar="KEY")
    _project(archived)
    json_option(archived, returns_object(*KEY_KEYS, schema="SignalKey"))
    archived.set_defaults(func=cmd_keys_archive)


def _register_ingest(ssub) -> None:
    ingest = ssub.add_parser("ingest", help="the ingest keys services write signals with: create, list, revoke")
    isub = ingest.add_subparsers(dest="ingest_command", required=True)
    created = isub.add_parser("create", help="an ingest key for a service; prints its token once (an admin)")
    created.add_argument("name", metavar="NAME", help="the service it is for, unique among the live keys")
    _project(created)
    created.add_argument("--max-level", metavar="LEVEL", help="the highest level it writes; the project's default")
    json_option(created, returns_object(*INGEST_KEYS, "token", schema="SignalIngestKeyIssued"))
    created.set_defaults(func=cmd_ingest_create)
    listed = isub.add_parser("list", help="the project's ingest keys, never a token")
    _project(listed)
    listed.add_argument("--revoked", action="store_true", help="revoked keys too")
    json_option(listed, returns_array(*INGEST_KEYS, schema="SignalIngestKey"))
    listed.set_defaults(func=cmd_ingest_list)
    revoked = isub.add_parser("revoke", help="revoke an ingest key: its next batch gets 401")
    revoked.add_argument("id", type=int, metavar="ID", help="the key's id, as ingest list prints it")
    _project(revoked)
    json_option(revoked, returns_object(*INGEST_KEYS, schema="SignalIngestKey"))
    revoked.set_defaults(func=cmd_ingest_revoke)


def register_signals(hsub) -> None:
    signal = hsub.add_parser("signal", help="a project's signals: push, list, the registry of keys, ingest keys")
    ssub = signal.add_subparsers(dest="signal_command", required=True)
    _register_push(ssub)
    listed = ssub.add_parser("list", help="the newest signals you may read, of one key or all")
    _project(listed)
    listed.add_argument("--key", metavar="KEY", help="one key; all when left out")
    listed.add_argument("--since", metavar="TIME", help="measured at or after, ISO 8601 with its offset")
    listed.add_argument("--until", metavar="TIME", help="measured before, ISO 8601 with its offset")
    listed.add_argument("--tenant", metavar="TEXT")
    listed.add_argument("--limit", type=int, metavar="N", help="at most N, 100 when left out, 1000 at most")
    json_option(listed, returns_array(*SIGNAL_KEYS, schema="Signal"))
    listed.set_defaults(func=cmd_list)
    _register_keys(ssub)
    _register_ingest(ssub)
