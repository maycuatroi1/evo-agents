"""``evo-agents worker pool``: the worker pools of the member signed in on this machine, and their join tokens, with
the machine token of ``evo-agents hub login`` (/v1/worker-pools, ``evo_agents.hub.server.worker_pools``).

- ``pool create NAME --project P``: a new pool, with the slots, labels, taints, accepted doctor findings and offline
  days before reaping its nodes take.
- ``pool list`` and ``pool show NAME``: the pools, and one with its nodes.
- ``pool set NAME``: its settings changed; its nodes take them from their next heartbeat answer. A list given replaces
  the pool's whole list, and ``--no-labels``, ``--no-taints`` and ``--no-accepted-findings`` empty one.
- ``pool token create NAME``: a join token, ``evj_...``, printed once; ``pool token list NAME`` and ``pool token
  revoke NAME ID``.

A machine joins a pool with ``evo-agents worker join --url URL --token evj_...``, or with the token in
EVO_JOIN_TOKEN (``join_secret``), so it stays out of the shell's history and the process list. Each command prints
its answer as JSON with ``--json``.
"""

from __future__ import annotations

import argparse
import json
import os

from evo_agents.hub.client import Hub, HubError, load_credentials

JOIN_TOKEN_ENV = "EVO_JOIN_TOKEN"
POOLS = "/v1/worker-pools"


def join_secret(args) -> tuple[str | None, str | None]:
    """(code, token) of ``worker join``: its --code, or its --token, else EVO_JOIN_TOKEN; exactly one of them."""
    token = args.token
    if args.code is not None and token is not None:
        raise HubError("give --code or --token, not both: a pairing code and a join token each join a machine alone")
    if args.code is None and token is None:
        token = os.environ.get(JOIN_TOKEN_ENV) or None
        if token is None:
            raise HubError(
                f"give --code with a pairing code from the hub's web, or --token (or {JOIN_TOKEN_ENV}) with a pool's "
                "join token from `evo-agents worker pool token create`"
            )
    return args.code, token


def _hub() -> Hub:
    credentials = load_credentials()
    return Hub(credentials.url, credentials.token)


def _path(name: str, *rest) -> str:
    return "/".join((POOLS, name, *map(str, rest)))


def _printed(answer, args, text: str) -> int:
    print(json.dumps(answer, indent=2) if args.json else text)
    return 0


def _pool_text(pool: dict) -> str:
    lines = [
        f"Pool {pool['name']} (id {pool['id']}) of {pool['owner']}: projects {', '.join(pool['projects'])}, "
        f"{pool['slots']} slot(s) a node, offline nodes revoked after {pool['reap_after_days']} day(s)",
        f"  labels: {', '.join(pool['labels']) or 'none'}; taints: {', '.join(pool['taints']) or 'none'}; "
        f"accepted findings: {', '.join(pool['accepted_findings']) or 'none'}",
    ]
    nodes = pool.get("nodes") or []
    lines.append(f"  nodes: {len(nodes)}" if nodes else "  nodes: none yet (`evo-agents worker pool token create`)")
    lines += [f"    {node['id']}  {node['name']}  {node['status']}  {node['hostname']}" for node in nodes]
    return "\n".join(lines)


def _settings(args) -> dict:
    body = {}
    if args.project:
        body["projects"] = list(dict.fromkeys(args.project))
    for field in ("slots", "reap_after_days"):
        if getattr(args, field) is not None:
            body[field] = getattr(args, field)
    for field, given in (("labels", args.label), ("taints", args.taint), ("accepted_findings", args.accept_finding)):
        if given:
            body[field] = list(dict.fromkeys(given))
        elif getattr(args, f"no_{field}", False):
            body[field] = []
    return body


def cmd_create(args) -> int:
    answer = _hub().call("POST", POOLS, {"name": args.name, **_settings(args)})
    return _printed(answer, args, f"Created pool {answer['name']}.\n{_pool_text(answer)}")


def cmd_list(args) -> int:
    answer = _hub().call("GET", POOLS)
    text = "\n".join(_pool_text(pool) for pool in answer) or "No pools yet (`evo-agents worker pool create`)."
    return _printed(answer, args, text)


def cmd_show(args) -> int:
    answer = _hub().call("GET", _path(args.name))
    return _printed(answer, args, _pool_text(answer))


def cmd_set(args) -> int:
    body = _settings(args)
    if not body:
        raise HubError("name a setting to change: --project, --slots, --label, --taint, --accept-finding, ...")
    answer = _hub().call("PATCH", _path(args.name), body)
    text = f"{_pool_text(answer)}\nIts nodes take the new settings at their next heartbeat."
    return _printed(answer, args, text)


def _token_text(token: dict) -> str:
    return (
        f"{token['id']}  {token['status']}  {token['uses']}/{token['max_uses']} used  expires {token['expires_at']}"
        f"  by {token['created_by']}"
    )


def cmd_token_create(args) -> int:
    body = {"hours": args.hours, "max_uses": args.max_uses}
    answer = _hub().call("POST", _path(args.name, "tokens"), body)
    text = (
        f"Join token {answer['id']} of pool {answer['pool']}, for {answer['max_uses']} machine(s) until "
        f"{answer['expires_at']}; it is shown this once:\n{answer['token']}\n"
        f"On each machine: {JOIN_TOKEN_ENV}=<token> evo-agents worker join --url <hub>"
    )
    return _printed(answer, args, text)


def cmd_token_list(args) -> int:
    answer = _hub().call("GET", _path(args.name, "tokens"))
    text = "\n".join(_token_text(token) for token in answer) or f"Pool {args.name} has no join tokens."
    return _printed(answer, args, text)


def cmd_token_revoke(args) -> int:
    answer = _hub().call("POST", _path(args.name, "tokens", args.id, "revoke"))
    text = f"Revoked join token {answer['id']} of pool {answer['pool']}; the nodes that joined with it stay."
    return _printed(answer, args, text)


def _int_in(low: int, high: int):
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a whole number") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"{value} is not between {low} and {high}")
        return value

    return parse


def _setting_options(parser, *, create: bool) -> None:
    from evo_agents.hub.runs import MAX_SLOTS

    parser.add_argument(
        "--project",
        action="append",
        required=create,
        metavar="PROJECT",
        help="a project its nodes take runs of, with the writer role on it; repeat it for each one"
        + ("" if create else " (the whole list)"),
    )
    parser.add_argument("--slots", type=_int_in(1, MAX_SLOTS), help=f"runs each node holds at once, 1 to {MAX_SLOTS}")
    parser.add_argument("--label", action="append", help="a label every node has, a word or key=value; repeat it")
    parser.add_argument("--taint", action="append", help="a taint every node has; repeat it")
    parser.add_argument(
        "--accept-finding", action="append", metavar="CODE", help="a doctor finding a node may have; repeat it"
    )
    parser.add_argument(
        "--reap-after-days", type=_int_in(1, 90), help="days a node stays offline before the hub revokes it (1-90)"
    )
    if not create:
        for field in ("labels", "taints", "accepted-findings"):
            parser.add_argument(
                f"--no-{field}", action="store_true", help=f"empty the pool's {field.replace('-', ' ')}"
            )
    parser.add_argument("--json", action="store_true", help="print the hub's answer as JSON")


def register_pool(wsub) -> None:
    from evo_agents.worker.cli import worker_command  # the cli imports this module

    pool = wsub.add_parser("pool", help="your worker pools and their join tokens (needs `hub login`)")
    psub = pool.add_subparsers(dest="pool_command", required=True)
    create = psub.add_parser("create", help="create a pool that machines join with a join token")
    create.add_argument("name", help="its name, unique among your pools; its nodes' names start with it")
    _setting_options(create, create=True)
    create.set_defaults(func=worker_command(cmd_create))
    listed = psub.add_parser("list", help="your pools and their nodes")
    listed.add_argument("--json", action="store_true", help="print the hub's answer as JSON")
    listed.set_defaults(func=worker_command(cmd_list))
    show = psub.add_parser("show", help="one of your pools and its nodes")
    show.add_argument("name")
    show.add_argument("--json", action="store_true", help="print the hub's answer as JSON")
    show.set_defaults(func=worker_command(cmd_show))
    change = psub.add_parser("set", help="change a pool's settings; its nodes take them at their next heartbeat")
    change.add_argument("name")
    _setting_options(change, create=False)
    change.set_defaults(func=worker_command(cmd_set))

    token = psub.add_parser("token", help="the join tokens of a pool")
    tsub = token.add_subparsers(dest="token_command", required=True)
    made = tsub.add_parser("create", help="a join token, evj_..., printed once")
    made.add_argument("name", help="the pool")
    made.add_argument("--hours", type=_int_in(1, 720), default=24, help="how long it works, 1 to 720 (default 24)")
    made.add_argument("--max-uses", type=_int_in(1, 100), default=1, help="machines it joins, 1 to 100 (default 1)")
    made.add_argument("--json", action="store_true", help="print the hub's answer, the token included, as JSON")
    made.set_defaults(func=worker_command(cmd_token_create))
    tokens = tsub.add_parser("list", help="the join tokens of a pool, without the tokens")
    tokens.add_argument("name", help="the pool")
    tokens.add_argument("--json", action="store_true", help="print the hub's answer as JSON")
    tokens.set_defaults(func=worker_command(cmd_token_list))
    revoke = tsub.add_parser("revoke", help="revoke a join token; the nodes that joined with it stay")
    revoke.add_argument("name", help="the pool")
    revoke.add_argument("id", type=int, help="the token's id, from `pool token list`")
    revoke.add_argument("--json", action="store_true", help="print the hub's answer as JSON")
    revoke.set_defaults(func=worker_command(cmd_token_revoke))
