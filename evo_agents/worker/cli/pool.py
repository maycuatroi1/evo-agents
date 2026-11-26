"""``evo-agents worker pool``: the worker pools of the member signed in on this machine, and their join tokens, with
the machine token of ``evo-agents hub login`` (/v1/worker-pools, ``evo_agents.hub.server.worker_pools``).

- ``pool create NAME --project P``: a new pool, with the slots, labels, taints, accepted doctor findings and offline
  days before reaping its nodes take.
- ``pool list`` and ``pool show NAME``: the pools, and one with its nodes.
- ``pool set NAME``: its settings changed; its nodes take them from their next heartbeat answer. A list given replaces
  the pool's whole list, and ``--no-labels``, ``--no-taints`` and ``--no-accepted-findings`` empty one.
- ``pool token create NAME``: a join token, ``evj_...``, printed once; ``pool token list NAME`` and ``pool token
  revoke NAME ID``.
- ``pool user-data NAME``: a new join token, and the cloud-init user-data of a new machine of the pool on stdout
  (``pool_boot.render_user_data``). With ``--agent HANDLE`` (once per agent) instead of NAME: a join token for the pool
  ``agent-<handle>`` of each agent, and user-data that makes the machine hold one node per agent, each under its own
  Unix user ``evo-<handle>``.

A machine joins a pool with ``evo-agents worker join --url URL --token evj_...``, or with the token in
EVO_JOIN_TOKEN (``pool_boot.join_secret``). Each command prints its answer as JSON with ``--json``. The commands are
classes of ``evo_agents.clikit``; tests patch ``signed_in``.
"""

from __future__ import annotations

import argparse
import json
import re
from typing import TextIO

from evo_agents.clikit import Arg, Group, Result, Stderr, Text, Verbatim, Views
from evo_agents.hub.api import paths, route
from evo_agents.hub.client import Hub, HubError, load_credentials
from evo_agents.hub.runs import MAX_SLOTS
from evo_agents.worker.cli.base import WorkerCommand, WorkerContext
from evo_agents.worker.membership import array_of, object_of
from evo_agents.worker.pool_boot import JOIN_TOKEN_ENV, bootstrap_script, render_user_data

AGENT_POOL_PREFIX = "agent-"  # the pool of each agent: agent-<handle>
POOLS = paths.WORKER_POOLS


def signed_in() -> Hub:
    """The hub as the member signed in on this machine."""
    credentials = load_credentials()
    return Hub(credentials.url, credentials.token)


def path_of(name: str, *rest) -> str:
    return route("worker-pools", name, *rest)


class Answer(Result):
    """The hub's answer and its text: ``--json`` prints the answer as JSON in ASCII, as the pool commands always did."""

    def show(self, out: TextIO, as_json: bool) -> None:
        if as_json:
            print(json.dumps(self.data, indent=2), file=out)
        else:
            self.view.write(out)


def pool_text(pool: dict) -> str:
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


def token_text(token: dict) -> str:
    return (
        f"{token['id']}  {token['status']}  {token['uses']}/{token['max_uses']} used  expires {token['expires_at']}"
        f"  by {token['created_by']}"
    )


def settings_of(args) -> dict:
    """The settings of a pool the command line ``args`` gives."""
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


def agent_handles(values: list[str]) -> list[str]:
    handles = list(dict.fromkeys(value.strip().lstrip("@").lower() for value in values))
    bad = [handle for handle in handles if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,26}", handle)]
    if bad:
        raise HubError(f"not an agent handle: {', '.join(bad)} (lower case letters, digits, - and _)")
    return handles


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


JSON = Arg("--json", action="store_true", help="print the hub's answer as JSON")
POOL_NAME = Arg("name", help="the pool")


def setting_args(*, create: bool) -> tuple[Arg, ...]:
    """The options of a pool's settings, for ``pool create`` and ``pool set``."""
    project = Arg(
        "--project",
        action="append",
        required=create,
        metavar="PROJECT",
        help="a project its nodes take runs of, with the writer role on it; repeat it for each one"
        + ("" if create else " (the whole list)"),
    )
    shared = (
        project,
        Arg("--slots", type=_int_in(1, MAX_SLOTS), help=f"runs each node holds at once, 1 to {MAX_SLOTS}"),
        Arg("--label", action="append", help="a label every node has, a word or key=value; repeat it"),
        Arg("--taint", action="append", help="a taint every node has; repeat it"),
        Arg("--accept-finding", action="append", metavar="CODE", help="a doctor finding a node may have; repeat it"),
        Arg(
            "--reap-after-days", type=_int_in(1, 90), help="days a node stays offline before the hub revokes it (1-90)"
        ),
    )
    emptied = (
        ()
        if create
        else tuple(
            Arg(f"--no-{field}", action="store_true", help=f"empty the pool's {field.replace('-', ' ')}")
            for field in ("labels", "taints", "accepted-findings")
        )
    )
    return (*shared, *emptied, JSON)


class PoolCreate(WorkerCommand):
    name = "create"
    help = "create a pool that machines join with a join token"
    word = "pool"
    arguments = (
        Arg("name", help="its name, unique among your pools; its nodes' names start with it"),
        *setting_args(create=True),
    )

    def run(self, ctx: WorkerContext) -> Result:
        answer = object_of(signed_in(), "POST", POOLS, {"name": ctx.args.name, **settings_of(ctx.args)})
        return Answer(answer, Text(f"Created pool {answer['name']}.\n{pool_text(answer)}"))


class PoolList(WorkerCommand):
    name = "list"
    help = "your pools and their nodes"
    word = "pool"
    arguments = (JSON,)

    def run(self, ctx: WorkerContext) -> Result:
        answer = array_of(signed_in(), "GET", POOLS)
        text = "\n".join(pool_text(pool) for pool in answer) or "No pools yet (`evo-agents worker pool create`)."
        return Answer(answer, Text(text))


class PoolShow(WorkerCommand):
    name = "show"
    help = "one of your pools and its nodes"
    word = "pool"
    arguments = (Arg("name"), JSON)

    def run(self, ctx: WorkerContext) -> Result:
        answer = object_of(signed_in(), "GET", path_of(ctx.args.name))
        return Answer(answer, Text(pool_text(answer)))


class PoolSet(WorkerCommand):
    name = "set"
    help = "change a pool's settings; its nodes take them at their next heartbeat"
    word = "pool"
    arguments = (Arg("name"), *setting_args(create=False))

    def run(self, ctx: WorkerContext) -> Result:
        body = settings_of(ctx.args)
        if not body:
            raise HubError("name a setting to change: --project, --slots, --label, --taint, --accept-finding, ...")
        answer = object_of(signed_in(), "PATCH", path_of(ctx.args.name), body)
        return Answer(answer, Text(f"{pool_text(answer)}\nIts nodes take the new settings at their next heartbeat."))


class PoolDelete(WorkerCommand):
    name = "delete"
    help = "delete a pool with no live node, and its join tokens"
    word = "pool"
    arguments = (Arg("name"), JSON)

    def run(self, ctx: WorkerContext) -> Result:
        signed_in().call("DELETE", path_of(ctx.args.name))
        name = ctx.args.name
        return Answer({"deleted": name}, Text(f"Deleted pool {name} and its join tokens."))


class TokenCreate(WorkerCommand):
    name = "create"
    help = "a join token, evj_..., printed once"
    word = "pool"
    arguments = (
        POOL_NAME,
        Arg("--hours", type=_int_in(1, 720), default=24, help="how long it works, 1 to 720 (default 24)"),
        Arg("--max-uses", type=_int_in(1, 100), default=1, help="machines it joins, 1 to 100 (default 1)"),
        Arg("--json", action="store_true", help="print the hub's answer, the token included, as JSON"),
    )

    def run(self, ctx: WorkerContext) -> Result:
        body = {"hours": ctx.args.hours, "max_uses": ctx.args.max_uses}
        answer = object_of(signed_in(), "POST", path_of(ctx.args.name, "tokens"), body)
        text = (
            f"Join token {answer['id']} of pool {answer['pool']}, for {answer['max_uses']} machine(s) until "
            f"{answer['expires_at']}; it is shown this once:\n{answer['token']}\n"
            f"On each machine: {JOIN_TOKEN_ENV}=<token> evo-agents worker join --url <hub>"
        )
        return Answer(answer, Text(text))


class TokenList(WorkerCommand):
    name = "list"
    help = "the join tokens of a pool, without the tokens"
    word = "pool"
    arguments = (POOL_NAME, JSON)

    def run(self, ctx: WorkerContext) -> Result:
        answer = array_of(signed_in(), "GET", path_of(ctx.args.name, "tokens"))
        text = "\n".join(token_text(token) for token in answer) or f"Pool {ctx.args.name} has no join tokens."
        return Answer(answer, Text(text))


class TokenRevoke(WorkerCommand):
    name = "revoke"
    help = "revoke a join token; the nodes that joined with it stay"
    word = "pool"
    arguments = (POOL_NAME, Arg("id", type=int, help="the token's id, from `pool token list`"), JSON)

    def run(self, ctx: WorkerContext) -> Result:
        answer = object_of(signed_in(), "POST", path_of(ctx.args.name, "tokens", ctx.args.id, "revoke"))
        text = f"Revoked join token {answer['id']} of pool {answer['pool']}; the nodes that joined with it stay."
        return Answer(answer, Text(text))


class UserData(WorkerCommand):
    name = "user-data"
    help = "a new join token, and the cloud-init user-data that makes a new machine a node of the pool"
    word = "pool"
    arguments = (
        Arg("name", nargs="?", help="the pool"),
        Arg(
            "--agent",
            action="append",
            default=[],
            metavar="HANDLE",
            help="instead of NAME: a node of this agent's pool agent-<handle>, under the Unix user evo-<handle> "
            "(repeat for more agents on the machine)",
        ),
        Arg("--ttl", type=_int_in(1, 720), default=24, help="hours its join token works, 1 to 720 (default 24)"),
        Arg("--max-nodes", type=_int_in(1, 100), default=1, help="machines its join token joins, 1 to 100 (default 1)"),
    )

    def run(self, ctx: WorkerContext) -> Result:
        args = ctx.args
        if args.agent:
            if args.name:
                raise HubError("give a pool NAME or --agent, not both")
            return self.for_agents(args)
        if not args.name:
            raise HubError("give a pool NAME, or --agent HANDLE for the nodes of agents")
        hub = signed_in()
        script = bootstrap_script()
        answer = object_of(hub, "POST", path_of(args.name, "tokens"), {"hours": args.ttl, "max_uses": args.max_nodes})
        said = (
            f"Join token {answer['id']} of pool {answer['pool']} is in this user-data, for {answer['max_uses']} "
            f"machine(s) until {answer['expires_at']}; keep it as a secret, and revoke it with "
            f"`evo-agents worker pool token revoke {answer['pool']} {answer['id']}`."
        )
        return Result(answer, Views(Verbatim(render_user_data(hub.url, answer["token"], script)), Stderr(Text(said))))

    @staticmethod
    def for_agents(args) -> Result:
        """The user-data of a machine that holds one node per agent of ``--agent``."""
        hub = signed_in()
        script = bootstrap_script()
        tokens: dict[str, str] = {}
        issued = []
        answer: dict = {}
        for handle in agent_handles(args.agent):
            answer = object_of(
                hub,
                "POST",
                path_of(AGENT_POOL_PREFIX + handle, "tokens"),
                {"hours": args.ttl, "max_uses": args.max_nodes},
            )
            tokens[handle] = answer["token"]
            issued.append(f"{answer['pool']} {answer['id']}")
        said = (
            f"The join tokens of {len(issued)} agent pool(s) are in this user-data until {answer['expires_at']}; keep "
            "it as a secret, and revoke each with "
            + ", ".join(f"`evo-agents worker pool token revoke {item}`" for item in issued)
            + "."
        )
        return Result(tokens, Views(Verbatim(render_user_data(hub.url, None, script, tokens)), Stderr(Text(said))))


TOKEN = Group(
    "token",
    "the join tokens of a pool",
    (TokenCreate(), TokenList(), TokenRevoke()),
    dest="token_command",
)
POOL = Group(
    "pool",
    "your worker pools and their join tokens (needs `hub login`)",
    (PoolCreate(), PoolList(), PoolShow(), PoolSet(), PoolDelete(), TOKEN, UserData()),
    dest="pool_command",
)
