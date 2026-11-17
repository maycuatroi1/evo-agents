"""Draft branches of a project's graph (plan kg-centralized-on-hub, criterion a5): a writer or a run's agent opens one
from main, applies operations to it (``evo_agents.kg.branch_ops``) while main does not change, reads it, its
structural diff and the ontology rules it breaks (``evo_agents.kg.ontology_rules``), then merges it into main or drops
it. The routes are ``evo_agents.hub.server.kg_branches``, the CLI ``evo-agents hub kg branch``.

A branch is a row of kg_branches (its name, the build of main it was opened at, its status) and its operations in
kg_branch_ops, in order. It keeps no copy of main's rows: its graph is main's as the tables hold it now with its
operations applied. Each operation records the rows of main it reads (``before``) when it is applied.

A merge runs in one transaction under the project's advisory lock (``lock``, which a build that writes main's
graph takes as well, ``kg_build.keep_graph``): it is refused with 409 when a row an operation read on main is no
longer what it read (main moved under it), or when the branch adds a violation of a hard rule (``HARD``) main does
not have. Otherwise main's tables take the branch's rows, a reader is notified as after a build, and the branch's
operations stay as a layer of main: each later build applies the operations of the merged branches, in the order
they merged, to what it built (``overlay``), so a merge outlives the next build. Dropping a branch deletes it and its
operations; a merged branch cannot be dropped."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from dataclasses import dataclass

from sqlalchemy import BigInteger, delete, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.kg_graph_rows import MAIN, read_graph, write_graph
from evo_agents.kg import branch_ops, ontology_rules
from evo_agents.kg.snapshot import Snapshot, StoreRows

log = logging.getLogger(__name__)

NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
RESERVED = ("main",)
MERGED_OP = "merged"  # the marker a merge appends to a branch's operations: its id orders the merged branches
HARD = ("wrong_endpoint", "cardinality", "cycle", "duplicate_key")  # rules a merge may not break anew
LOCK_BASE = 0x6B67_0000_0000  # "kg" then the project id: the advisory lock of the writes to a project's main graph


class BranchError(Exception):
    """A branch operation refused: ``status`` is the HTTP status, ``detail`` what the caller reads."""

    def __init__(self, status: int, message: str, detail: dict | None = None):
        super().__init__(message)
        self.status = status
        self.detail = detail or {}


@dataclass(frozen=True)
class Branch:
    id: int
    name: str
    status: str
    base_build_id: int | None
    created_by: int | None
    created_at: str
    ops: int

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


Reader = tuple[frozenset[str], tuple[int, int] | None]  # a reader's audience and its ceiling (level, location)


async def lock(conn: AsyncConnection, project_id: int) -> None:
    """Take the project's ``kg:<project>`` advisory lock until the caller's transaction ends."""
    await conn.execute(select(func.pg_advisory_xact_lock(literal(LOCK_BASE, BigInteger) + project_id)))


def _ops_count():
    ops = tables.kg_branch_ops
    return (
        select(func.count())
        .where(ops.c.branch_id == tables.kg_branches.c.id, ops.c.op != MERGED_OP)
        .scalar_subquery()
        .label("ops")
    )


def _branch(row) -> Branch:
    m = row._mapping
    return Branch(
        m["id"], m["name"], m["status"], m["base_build_id"], m["created_by"], m["created_at"].isoformat(), m["ops"]
    )


async def branches(conn: AsyncConnection, project_id: int, status: str | None = None) -> list[Branch]:
    """The project's branches, newest first, of ``status`` only when given."""
    b = tables.kg_branches
    query = select(b, _ops_count()).where(b.c.project_id == project_id).order_by(b.c.id.desc())
    if status is not None:
        query = query.where(b.c.status == status)
    return [_branch(row) for row in await conn.execute(query)]


async def get(conn: AsyncConnection, project_id: int, name: str) -> Branch:
    """The branch ``name`` of the project. Raises BranchError 404."""
    b = tables.kg_branches
    row = (await conn.execute(select(b, _ops_count()).where(b.c.project_id == project_id, b.c.name == name))).first()
    if row is None:
        raise BranchError(404, f"the project has no graph branch named {name!r}")
    return _branch(row)


async def _main_build(conn: AsyncConnection, project_id: int) -> int | None:
    heads = tables.kg_graph_heads
    return await conn.scalar(
        select(heads.c.build_id).where(heads.c.project_id == project_id, heads.c.branch_id == MAIN)
    )


async def _has_main(conn: AsyncConnection, project_id: int) -> bool:
    heads = tables.kg_graph_heads
    found = await conn.scalar(
        select(heads.c.project_id).where(heads.c.project_id == project_id, heads.c.branch_id == MAIN)
    )
    return found is not None


async def create(conn: AsyncConnection, project_id: int, name: str, user_id: int | None) -> Branch:
    """Open branch ``name`` from main as it is now. Raises BranchError: 422 for a name that is not one, 409 when
    the project has no graph yet or a branch of that name."""
    if not NAME.fullmatch(name or "") or name in RESERVED:
        raise BranchError(
            422,
            f"a branch name is 1 to 63 of a-z, 0-9, '.', '_', '-', starting with a letter or digit, not {RESERVED[0]}",
        )
    if not await _has_main(conn, project_id):
        raise BranchError(409, "the project has no graph on the hub yet: a branch opens from main once it is built")
    b = tables.kg_branches
    if await conn.scalar(select(b.c.id).where(b.c.project_id == project_id, b.c.name == name)) is not None:
        raise BranchError(409, f"the project has a graph branch named {name!r} already")
    base = await _main_build(conn, project_id)
    await conn.execute(insert(b).values(project_id=project_id, name=name, base_build_id=base, created_by=user_id))
    return await get(conn, project_id, name)


async def _ops(conn: AsyncConnection, branch_id: int) -> list[dict]:
    """The operations of a branch, in order, each with its ``before`` (``apply``)."""
    o = tables.kg_branch_ops
    rows = await conn.execute(
        select(o.c.payload).where(o.c.branch_id == branch_id, o.c.op != MERGED_OP).order_by(o.c.id)
    )
    return [row.payload for row in rows]


def _plain(op: dict) -> dict:
    return {k: v for k, v in op.items() if k != "before"}


def _replayed(op: dict) -> dict:
    """``op`` of a merged branch to apply on a new build: a ``node.put`` that changed a node main had then is marked
    ``update``, so it is left out rather than re-creating the node once it left the sources."""
    plain = _plain(op)
    if plain.get("op") == "node.put" and (op.get("before") or {}).get(plain.get("id")) is not None:
        plain["update"] = True
    return plain


def _json_rows(found: dict[str, tuple | None]) -> dict[str, list | None]:
    return {key: None if row is None else list(row) for key, row in found.items()}


async def _open(conn: AsyncConnection, project_id: int, name: str) -> Branch:
    branch = await get(conn, project_id, name)
    if branch.status != "open":
        raise BranchError(409, f"graph branch {name!r} is {branch.status}, not open")
    return branch


async def _main_rows(conn: AsyncConnection, project_id: int) -> StoreRows:
    rows = await read_graph(conn, project_id)
    if rows is None:
        raise BranchError(409, "the project has no graph on the hub yet")
    return rows


def _applied(rows: StoreRows, ops: list[dict]) -> StoreRows:
    try:
        return branch_ops.apply(rows, [_plain(op) for op in ops])
    except branch_ops.OpError as exc:
        raise BranchError(422, str(exc)) from None


async def apply(
    conn: AsyncConnection, project_id: int, name: str, ops: list, user_id: int | None, writer: Reader
) -> dict:
    """Add ``ops`` to open branch ``name``, after its own, each checked and recording the rows of main it reads; all
    or none, under the project's lock so no merge runs meanwhile. Raises BranchError: 422 for an operation that is
    not one or names what the branch lacks, 403 for one that changes a row ``writer`` does not read, labels one above
    its grant or lowers a label (``branch_ops.write_refusal``)."""
    await lock(conn, project_id)
    branch = await _open(conn, project_id, name)
    if not isinstance(ops, list) or not ops or len(ops) > branch_ops.MAX_OPS:
        raise BranchError(422, f"apply takes a list of 1 to {branch_ops.MAX_OPS} operations")
    try:
        fresh = [branch_ops.normalize(op) for op in ops]
    except branch_ops.OpError as exc:
        raise BranchError(422, str(exc)) from None
    main = await _main_rows(conn, project_id)
    kept = await _ops(conn, branch.id)
    before = _applied(main, kept)
    branch_rows = _applied(before, fresh)
    touched = {key for op in fresh for ids in branch_ops.touched(op) for key in ids}
    touched |= branch_ops.changed_ids(before, branch_rows)  # a merge moves edges, a delete takes them away
    refused = branch_ops.write_refusal(before, branch_rows, touched, *writer)
    if refused:
        raise BranchError(403, refused)
    values = []
    for op in fresh:
        node_ids, edge_ids = branch_ops.touched(op)
        before = _json_rows(branch_ops.rows_of(main, node_ids, edge_ids))
        values.append({"branch_id": branch.id, "op": op["op"], "payload": {**op, "before": before}, "by_user": user_id})
    await conn.execute(insert(tables.kg_branch_ops), values)
    return {
        "branch": name,
        "applied": len(fresh),
        "ops": len(kept) + len(fresh),
        "nodes": len(branch_rows.nodes),
        "edges": len(branch_rows.edges),
    }


async def graph(conn: AsyncConnection, project_id: int, name: str) -> tuple[Branch, StoreRows, StoreRows]:
    """(the branch, main's rows now, the branch's rows): main with the branch's operations applied."""
    branch = await get(conn, project_id, name)
    main = await _main_rows(conn, project_id)
    if branch.status != "open":
        return branch, main, main
    return branch, main, _applied(main, await _ops(conn, branch.id))


def _seen(main: StoreRows, rows: StoreRows, reader: Reader | None) -> set[str] | None:
    """The node and edge ids ``reader`` reads on main and on the branch, wherever each is; None reads all."""
    if reader is None:
        return None
    every = {r[0] for r in (*main.nodes, *main.edges, *rows.nodes, *rows.edges)}
    hidden = set()
    for side in (main, rows):
        hidden |= {r[0] for r in (*side.nodes, *side.edges)} - branch_ops.visible(side, *reader)
    return every - hidden


async def diff(conn: AsyncConnection, project_id: int, name: str, reader: Reader | None = None) -> dict:
    """The structural diff of branch ``name`` from main (``branch_ops.diff``), of what ``reader`` reads."""
    branch, main, rows = await graph(conn, project_id, name)
    found = branch_ops.diff(main, rows)
    seen = _seen(main, rows, reader)
    return {"branch": branch.as_dict(), **(found if seen is None else branch_ops.shown_diff(found, seen))}


async def _rules(conn: AsyncConnection, project_id: int) -> ontology_rules.Rules:
    configs = tables.kg_configs
    ontology = await conn.scalar(select(configs.c.ontology).where(configs.c.project_id == project_id))
    return ontology_rules.rules_of(ontology if isinstance(ontology, dict) else None)


def violations(main: StoreRows, rows: StoreRows, rules: ontology_rules.Rules) -> dict:
    """The ontology rule violations of ``rows`` (``ontology_rules.check``): all of them, those ``main`` does not have
    (by rule and key), and of those the ones of a HARD rule. Blocking: it builds two snapshots."""
    if not rules:
        return {"rules": False, "violations": [], "new": [], "hard": []}
    on_branch = ontology_rules.check(Snapshot.from_stored(rows), rules)
    on_main = {(v.rule, v.key) for v in ontology_rules.check(Snapshot.from_stored(main), rules)}
    shown = [dataclasses.asdict(v) for v in on_branch]
    new = [s for s, v in zip(shown, on_branch, strict=True) if (v.rule, v.key) not in on_main]
    return {"rules": True, "violations": shown, "new": new, "hard": [v for v in new if v["rule"] in HARD]}


async def check(conn: AsyncConnection, project_id: int, name: str, reader: Reader | None = None) -> dict:
    """The ontology rule violations of branch ``name`` (``violations``) and whether it would merge now; the
    violations and conflicts listed are those of what ``reader`` reads, whether it merges counts them all."""
    branch, main, rows = await graph(conn, project_id, name)
    found = await asyncio.to_thread(violations, main, rows, await _rules(conn, project_id))
    conflicts = _conflicts(main, await _ops(conn, branch.id)) if branch.status == "open" else []
    mergeable = not found["hard"] and not conflicts and branch.status == "open"
    seen = _seen(main, rows, reader)
    if seen is not None:
        found = {k: v if k == "rules" else [x for x in v if _shown(x, seen)] for k, v in found.items()}
        conflicts = [c for c in conflicts if c["id"] in seen]
    return {"branch": branch.as_dict(), **found, "conflicts": conflicts, "mergeable": mergeable}


def _shown(violation: dict, seen: set[str]) -> bool:
    return all(i in seen for i in (*violation["nodes"], *violation["edges"]))


def _conflicts(main: StoreRows, ops: list[dict]) -> list[dict]:
    """The rows an operation read on main that main no longer holds as it read them: (operation index, id)."""
    out = []
    for i, op in enumerate(ops):
        before = op.get("before") or {}
        now = _json_rows(branch_ops.rows_of(main, *branch_ops.touched(_plain(op))))
        out += [{"op": i, "id": key} for key in sorted(now) if before.get(key) != now[key]]
    return out


async def merge(conn: AsyncConnection, project_id: int, name: str, user_id: int | None) -> dict:
    """Merge open branch ``name`` into main in the caller's transaction, under the project's lock. Raises
    BranchError 409 on a conflict with main or a new violation of a hard rule."""
    await lock(conn, project_id)
    branch = await _open(conn, project_id, name)
    main = await _main_rows(conn, project_id)
    ops = await _ops(conn, branch.id)
    conflicts = _conflicts(main, ops)
    if conflicts:
        raise BranchError(
            409,
            f"main changed what graph branch {name!r} changes: open a new branch from main",
            {"conflicts": conflicts},
        )
    rows = _applied(main, ops)
    found = await asyncio.to_thread(violations, main, rows, await _rules(conn, project_id))
    if found["hard"]:
        raise BranchError(409, f"graph branch {name!r} breaks a hard ontology rule", {"hard": found["hard"]})
    build_id = await _main_build(conn, project_id)
    changed = await write_graph(conn, project_id, build_id, rows)
    b = tables.kg_branches
    await conn.execute(update(b).where(b.c.id == branch.id).values(status="merged"))
    marker = {"branch_id": branch.id, "op": MERGED_OP, "payload": {"build_id": build_id}, "by_user": user_id}
    await conn.execute(insert(tables.kg_branch_ops).values(**marker))
    return {
        "branch": name,
        "merged": True,
        "ops": len(ops),
        "build_id": build_id,
        "content_hash": rows.content_hash,
        "changed": changed,
        **branch_ops.diff(main, rows)["counts"],
    }


async def drop(conn: AsyncConnection, project_id: int, name: str) -> dict:
    """Delete open branch ``name`` and its operations, under the project's lock. Raises BranchError 409 for a merged
    one."""
    await lock(conn, project_id)
    branch = await get(conn, project_id, name)
    if branch.status == "merged":
        raise BranchError(409, f"graph branch {name!r} is merged: its operations are a layer of main")
    await conn.execute(delete(tables.kg_branches).where(tables.kg_branches.c.id == branch.id))
    return {"branch": name, "dropped": True, "ops": branch.ops}


async def merged(conn: AsyncConnection, project_id: int) -> list[list[dict]]:
    """The operations of the project's merged branches, each with its ``before``: a list a branch, in the order they
    merged."""
    b, o = tables.kg_branches, tables.kg_branch_ops
    merged_at = select(o.c.branch_id, o.c.id.label("at")).where(o.c.op == MERGED_OP).subquery()
    query = (
        select(o.c.branch_id, o.c.payload)
        .join(b, b.c.id == o.c.branch_id)
        .join(merged_at, merged_at.c.branch_id == b.c.id)
        .where(b.c.project_id == project_id, b.c.status == "merged", o.c.op != MERGED_OP)
        .order_by(merged_at.c.at, o.c.id)
    )
    groups: dict[int, list[dict]] = {}
    for row in await conn.execute(query):
        groups.setdefault(row.branch_id, []).append(row.payload)
    return list(groups.values())


def merged_hash(content_hash: str, groups: list[list[dict]]) -> str:
    """The content hash main gets from a graph of hash ``content_hash`` once ``groups`` (``merged``) merged into it,
    one branch after the other, as ``merge`` hashed them."""
    for ops in groups:
        content_hash = branch_ops.ops_hash(content_hash, [_plain(op) for op in ops])
    return content_hash


async def overlay(conn: AsyncConnection, project_id: int, rows: StoreRows, groups: list[list[dict]]) -> StoreRows:
    """``rows`` a build made with ``groups``, the operations of the project's merged branches (``merged``), applied in
    the order they merged and hashed as ``merge`` hashed them (``merged_hash``); an operation that no longer applies
    (its node went from the sources) is left out. ``rows`` when none merged."""
    if not groups:
        return rows
    skipped: list[str] = []
    out = await asyncio.to_thread(_overlaid, rows, groups, skipped)
    if skipped:
        log.warning(
            "merged graph branch operations no longer apply",
            extra={"project_id": project_id, "why": skipped[:5], "skipped": len(skipped)},
        )
    return out


def _overlaid(rows: StoreRows, groups: list[list[dict]], skipped: list[str]) -> StoreRows:
    for ops in groups:
        rows = branch_ops.apply(rows, [_replayed(op) for op in ops], skipped, [_plain(op) for op in ops])
    return rows
