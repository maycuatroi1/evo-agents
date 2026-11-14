"""A project's graph in the hub's tables (0247, plan kg-centralized-on-hub, criterion a3): what a build writes there
instead of a graph.sqlite file, and what a snapshot of it reads back (``evo_agents.kg.snapshot.Snapshot.from_stored``).

A build writes, for one branch (0 is main):

- its corpus items to kg_corpus_items (``merge_corpus``), keyed by source and item id with the item's row hash: the
  items whose row hash is new or changed are upserted, those gone are deleted, and the rest are not touched;
- its node and edge rows, as a store file holds them, to kg_nodes and kg_edges, and its full-text and code rows to
  kg_text_rows (``write_graph``): only a row that is new or differs is upserted, and a row the build no longer has is
  deleted, so a build that changed ten nodes writes ten rows;
- its build and content hash to kg_graph_heads, and a notice on the channel ``kg_graph`` (``CHANNEL``) naming the
  project, branch and build, which a reader listens for to load the new snapshot.

kg_memo_entries holds what an extractor made of a row hash (``memo_get``, ``memo_put``), so an item whose row hash
an earlier build read is not extracted again. The content hash of the head is the build's, so a snapshot read from
these tables hashes as one read from the build's file."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.kg.snapshot import StoreRows

CHANNEL = "kg_graph"
MAIN = 0
BATCH = 1000
NODE_KEYS = ("node_id", "kind", "name", "props", "status", "conf", "label_lvl", "label_loc", "label_int", "aliases")
EDGE_KEYS = ("edge_id", "src", "rel", "dst", "props", "status", "conf", "label_lvl", "label_loc", "label_int")
TEXT, CODE = "t", "c"  # the kinds of a kg_text_rows row: a keyed full-text row, or the text of a file's code


@dataclass(frozen=True)
class Changes:
    """What a write changed: the keys it added, those it changed and those it deleted."""

    new: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()

    def counts(self) -> dict:
        return {"new": len(self.new), "changed": len(self.changed), "deleted": len(self.deleted)}


@dataclass(frozen=True)
class CorpusItem:
    """An item of a source as kg_corpus_items keeps it (``corpus_items``)."""

    item_key: str  # source and item id
    source_id: str
    row_hash: str
    content_sha256: str
    label: dict
    audience: tuple[str, ...]
    body: dict


def corpus_items(records) -> list[CorpusItem]:
    """The live items of a corpus (``evo_agents.kg.corpus.ItemRecord``) as kg_corpus_items keeps them: the row hash is
    the item's own hash, or that of its record when the source gives none."""
    items = []
    for r in records:
        if r.deleted:
            continue
        text = json.dumps(r.record, ensure_ascii=False, sort_keys=True)
        sha = hashlib.sha256(text.encode()).hexdigest()
        label = r.record.get("label") if isinstance(r.record.get("label"), dict) else {}
        audience = tuple(sorted(str(a) for a in (label.get("audience") or ())))
        items.append(CorpusItem(f"{r.source}\x1f{r.item_id}", r.source, r.hash or sha, sha, label, audience, r.record))
    return items


def diff(old: dict[str, tuple], fresh: dict[str, tuple]) -> Changes:
    """The keys of ``fresh`` that ``old`` lacks or holds otherwise, and those of ``old`` that ``fresh`` lacks."""
    return Changes(
        tuple(sorted(k for k in fresh if k not in old)),
        tuple(sorted(k for k, v in fresh.items() if k in old and old[k] != v)),
        tuple(sorted(k for k in old if k not in fresh)),
    )


def text_row_key(kind: str, key: str, row) -> str:
    digest = hashlib.sha256(json.dumps(row, ensure_ascii=False).encode()).hexdigest()[:16]
    return f"{kind}\x1f{key}\x1f{digest}"


def graph_rows(rows: StoreRows) -> dict[str, dict[str, tuple]]:
    """The rows of each table of a build, by their key: what ``write_graph`` compares with the tables."""
    nodes = {r[0]: tuple(r) for r in rows.nodes}
    edges = {r[0]: tuple(r) for r in rows.edges}
    text: dict[str, tuple] = {}
    for key, row in rows.keyed:
        text[text_row_key(TEXT, key, list(row))] = (key, json.dumps([TEXT, key, list(row)], ensure_ascii=False))
    for nid, body in rows.code.items():
        text[text_row_key(CODE, nid, body)] = (nid, json.dumps([CODE, nid, body], ensure_ascii=False))
    return {"kg_nodes": nodes, "kg_edges": edges, "kg_text_rows": text}


def _row_values(name: str, key: str, row: tuple) -> dict:
    if name == "kg_nodes":
        return {**dict(zip(NODE_KEYS, row[:10], strict=True)), "label_aud": row[10]}
    if name == "kg_edges":
        return {**dict(zip(EDGE_KEYS, row[:10], strict=True)), "label_aud": row[10]}
    return {"row_key": key, "node_id": row[0], "row": json.loads(row[1])}


def _stored(name: str, values) -> tuple:
    """A row of table ``name`` as ``graph_rows`` gives it."""
    if name == "kg_nodes":
        return tuple(values[k] for k in (*NODE_KEYS, "label_aud"))
    if name == "kg_edges":
        return tuple(values[k] for k in (*EDGE_KEYS, "label_aud"))
    return (values["node_id"], json.dumps(values["row"], ensure_ascii=False))


async def _read(conn: AsyncConnection, name: str, project_id: int, branch_id: int) -> dict[str, tuple]:
    table = getattr(tables, name)
    key = table.primary_key.columns.values()[-1]
    rows = await conn.execute(
        select(table).where(table.c.project_id == project_id, table.c.branch_id == branch_id).order_by(key)
    )
    return {row._mapping[key.name]: _stored(name, row._mapping) for row in rows}


async def _apply(conn: AsyncConnection, name: str, project_id: int, branch_id: int, fresh: dict, changes: Changes):
    table = getattr(tables, name)
    key = table.primary_key.columns.values()[-1]
    gone = list(changes.deleted)
    for i in range(0, len(gone), BATCH):
        scope = (table.c.project_id == project_id, table.c.branch_id == branch_id)
        await conn.execute(delete(table).where(*scope, key.in_(gone[i : i + BATCH])))
    upserts = [*changes.new, *changes.changed]
    for i in range(0, len(upserts), BATCH):
        values = [
            {"project_id": project_id, "branch_id": branch_id, **_row_values(name, k, fresh[k])}
            for k in upserts[i : i + BATCH]
        ]
        stmt = pg_insert(table).values(values)
        kept = {c.name: stmt.excluded[c.name] for c in table.columns if not c.primary_key}
        await conn.execute(stmt.on_conflict_do_update(index_elements=list(table.primary_key.columns), set_=kept))


async def write_graph(
    conn: AsyncConnection, project_id: int, build_id: int | None, rows: StoreRows, branch_id: int = MAIN
) -> dict[str, dict]:
    """Write the rows of a build to the tables of ``branch_id``, only what changed, then its head, and notify
    ``CHANNEL``; what each table changed (``Changes.counts``). Within the caller's transaction."""
    changed = {}
    for name, fresh in graph_rows(rows).items():
        changes = diff(await _read(conn, name, project_id, branch_id), fresh)
        await _apply(conn, name, project_id, branch_id, fresh, changes)
        changed[name] = changes.counts()
    heads = tables.kg_graph_heads
    (level, location), audience = rows.top
    head = {
        "build_id": build_id,
        "content_hash": rows.content_hash,
        "top_label": [level, location],
        "top_audience": sorted(audience),
        "updated_at": func.now(),
    }
    stmt = pg_insert(heads).values(project_id=project_id, branch_id=branch_id, **head)
    await conn.execute(stmt.on_conflict_do_update(index_elements=[heads.c.project_id, heads.c.branch_id], set_=head))
    await notify(conn, project_id, branch_id, build_id)
    return changed


async def notify(conn: AsyncConnection, project_id: int, branch_id: int, build_id: int | None) -> None:
    """Tell the readers listening on ``CHANNEL`` that the graph of a branch has a new build; sent at commit."""
    payload = json.dumps({"project_id": project_id, "branch_id": branch_id, "build_id": build_id})
    await conn.execute(select(func.pg_notify(CHANNEL, payload)))


async def read_graph(conn: AsyncConnection, project_id: int, branch_id: int = MAIN) -> StoreRows | None:
    """The rows a snapshot of a branch is made of, as ``write_graph`` kept them; None before its first build."""
    heads = tables.kg_graph_heads
    head = (
        await conn.execute(
            select(heads.c.content_hash, heads.c.top_label, heads.c.top_audience).where(
                heads.c.project_id == project_id, heads.c.branch_id == branch_id
            )
        )
    ).one_or_none()
    if head is None:
        return None
    nodes = await _read(conn, "kg_nodes", project_id, branch_id)
    edges = await _read(conn, "kg_edges", project_id, branch_id)
    keyed, code = [], {}
    for _, row in (await _read(conn, "kg_text_rows", project_id, branch_id)).items():
        kind, key, value = json.loads(row[1])
        if kind == TEXT:
            keyed.append((key, tuple(value)))
        else:
            code[key] = value
    top = ((head.top_label[0], head.top_label[1]), frozenset(head.top_audience))
    return StoreRows(head.content_hash, tuple(nodes.values()), tuple(edges.values()), tuple(sorted(keyed)), code, top)


async def merge_corpus(
    conn: AsyncConnection, project_id: int, items: list[CorpusItem], branch_id: int = MAIN
) -> Changes:
    """Bring kg_corpus_items of a branch to ``items``: upsert the items whose row hash is new or changed, delete those
    gone; the keys of each (``Changes``), which are the rows an extractor reads again."""
    corpus = tables.kg_corpus_items
    scope = (corpus.c.project_id == project_id, corpus.c.branch_id == branch_id)
    old = {k: (h,) for k, h in await conn.execute(select(corpus.c.item_key, corpus.c.row_hash).where(*scope))}
    by_key = {item.item_key: item for item in items}
    changes = diff(old, {k: (item.row_hash,) for k, item in by_key.items()})
    gone = list(changes.deleted)
    for i in range(0, len(gone), BATCH):
        await conn.execute(delete(corpus).where(*scope, corpus.c.item_key.in_(gone[i : i + BATCH])))
    upserts = [by_key[k] for k in (*changes.new, *changes.changed)]
    for i in range(0, len(upserts), BATCH):
        values = [
            {"project_id": project_id, "branch_id": branch_id, **item.__dict__, "audience": list(item.audience)}
            for item in upserts[i : i + BATCH]
        ]
        stmt = pg_insert(corpus).values(values)
        kept = {c: stmt.excluded[c] for c in ("source_id", "row_hash", "content_sha256", "label", "audience", "body")}
        await conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[corpus.c.project_id, corpus.c.branch_id, corpus.c.item_key],
                set_={**kept, "updated_at": func.now()},
            )
        )
    return changes


async def memo_get(conn: AsyncConnection, project_id: int, extractor: str, row_hashes) -> dict[str, dict]:
    """What ``extractor`` made of each of ``row_hashes`` an earlier build read, by row hash."""
    memo = tables.kg_memo_entries
    wanted = sorted(set(row_hashes))
    found: dict[str, dict] = {}
    for i in range(0, len(wanted), BATCH):
        rows = await conn.execute(
            select(memo.c.row_hash, memo.c.result).where(
                memo.c.project_id == project_id,
                memo.c.extractor == extractor,
                memo.c.row_hash.in_(wanted[i : i + BATCH]),
            )
        )
        found.update({h: r for h, r in rows})
    return found


async def memo_put(conn: AsyncConnection, project_id: int, extractor: str, results: dict[str, dict]) -> None:
    """Keep what ``extractor`` made of each row hash; a row hash it holds already keeps its first result."""
    memo = tables.kg_memo_entries
    entries = sorted(results.items())
    for i in range(0, len(entries), BATCH):
        values = [
            {"project_id": project_id, "extractor": extractor, "row_hash": h, "result": r}
            for h, r in entries[i : i + BATCH]
        ]
        await conn.execute(pg_insert(memo).values(values).on_conflict_do_nothing())
