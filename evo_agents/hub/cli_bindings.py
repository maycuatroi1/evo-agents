"""Where a secret or an account is bound, for ``hub secret`` and ``hub account`` (``cli_secrets``): on workers named
(``--worker``), on the nodes of worker pools (``--pool``, plan worker-pools, criterion a4), or on any worker of its
owner. A pool covers each of its nodes, those that join after the binding too; a binding names workers or pools, not
both."""

from __future__ import annotations

BOTH = "give --worker or --pool, not both: a pool covers each of its nodes already"


def add_pool_flag(parser, runs: str = "get") -> None:
    """``--pool NAME``, repeatable, on ``parser``; ``runs`` is the verb of the help ("get" or "take")."""
    parser.add_argument(
        "--pool",
        action="append",
        metavar="NAME",
        help=f"only runs on a node of this worker pool of yours {runs} it, those that join later too; repeat it for "
        "several; not with --worker",
    )


def where_text(item: dict) -> str:
    """Where ``item``, a secret or an account of the API, is bound: on worker a, b, on pool ci, or on any worker of yours."""
    if item.get("pools"):
        return "on pool " + ", ".join(item["pools"])
    return "on " + (", ".join(item["workers"]) if item["workers"] else "any worker of yours")


def where_cell(item: dict) -> str:
    """The WORKERS cell of a listing: the workers, pool <name> for each pool, or any."""
    return ", ".join([*item["workers"], *(f"pool {name}" for name in item.get("pools") or ())]) or "any"


def bound_to(args, existing: dict | None) -> dict:
    """The workers and pools of a login's body: those given, else the account's when it is one already. ``str`` of
    the ValueError raised for both says why."""
    if args.worker and args.pool:
        raise ValueError(BOTH)
    if args.worker is not None or args.pool is not None:
        return {"workers": args.worker or [], "pools": args.pool or []}
    if existing is None:
        return {"workers": [], "pools": []}
    return {"workers": existing["workers"], "pools": existing.get("pools") or []}
