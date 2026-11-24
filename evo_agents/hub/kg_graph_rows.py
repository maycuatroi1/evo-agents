"""A project's graph in the hub's tables (0248, plan kg-centralized-on-hub, criterion a3): what a build writes there,
and what a snapshot of it reads back (``evo_agents.kg.snapshot.Snapshot.from_stored``).

A build writes, for one branch (0 is main):

- its corpus items to kg_corpus_items (``merge_corpus``), keyed by source and item id with the item's row hash: the
  items whose row hash is new or changed are upserted, those gone are deleted, and the rest are not touched;
- its node and edge rows (``graph_model.NODE_COLUMNS``, ``EDGE_COLUMNS``) to kg_nodes and kg_edges, and its full-text
  and code rows to kg_text_rows (``write_graph``): only a row that is new or differs is upserted, and a row the build no
  longer has is deleted, so a build that changed ten nodes writes ten rows;
- the rows of its asked index (``evo_agents.kg.asked.Row``) to kg_asked, and its clusters and review items to
  kg_review_rows;
- its build, content hash and weight of an asked match to kg_graph_heads, and a notice on the channel ``kg_graph`` (``CHANNEL``) naming the
  project, branch and build, which a reader listens for to load the new snapshot. On main the notice also names the
  rows the build wrote or deleted (``change_note``, plan kg-sound-fast-optimal), so a reader holding the snapshot
  before it reads only those (``kg_graph_delta``); a notice too large for the channel names none.

kg_memo_entries holds what an extractor made of a row hash (``memo_get``, ``memo_put``), so an item whose row hash
an earlier build read is not extracted again. The content hash of the head is the build's, so a snapshot read from
these tables hashes as one read from the build's file."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import zlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from sqlalchemy import Text, cast, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import runs, tables, wake
from evo_agents.kg.pacing import paced
from evo_agents.kg.snapshot import StoreRows, asked_order, cluster_order

CHANNEL = "kg_graph"
MAIN = 0
BATCH = 1000
READ_BATCH = 2000  # rows ``_read`` fetches at a time, giving the event loop back between two fetches
NODE_KEYS = ("node_id", "kind", "name", "props", "status", "conf", "label_lvl", "label_loc", "label_int", "aliases")
EDGE_KEYS = ("edge_id", "src", "rel", "dst", "props", "status", "conf", "label_lvl", "label_loc", "label_int")
TEXT, CODE = "t", "c"  # the kinds of a kg_text_rows row: a keyed full-text row, or the text of a file's code
ASKED = "a"  # the kind in the key of a kg_asked row
REVIEW = "r"  # the kind of a kg_review_rows row of a review item; a cluster row's is that of ``cluster_rows``
UNIT, WHERE, DERIVED = "u", "w", "d"  # the kinds of a kg_provenance row: a unit, a where-provenance, a derivation
ROW_TABLES = {
    "kg_text_rows": "node_id",
    "kg_provenance": "elem_id",
    "kg_asked": "node_id",
    "kg_review_rows": "elem_id",
}  # tables of (key, id, JSON row), by id column


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


def _json(row) -> str:
    """A row as the JSON both sides of ``diff`` compare: Postgres keeps a jsonb object's keys in its own order, so a
    row read back from the tables equals the build's only once both sort their keys. A NUL character is written as
    ``runs.NUL_STAND_IN``, since jsonb holds none, so a row read back equals the build's there too."""
    text = json.dumps(row, ensure_ascii=False, sort_keys=True)
    if "\\u0000" in text:
        text = json.dumps(runs.without_nul(row), ensure_ascii=False, sort_keys=True)
    return text


def graph_rows(rows: StoreRows) -> dict[str, dict[str, tuple]]:
    """The rows of each table of a build, by their key: what ``write_graph`` compares with the tables."""
    return {name: table_rows(rows, name) for name in WRITE_ORDER}


def table_rows(rows: StoreRows, name: str) -> dict[str, tuple]:
    """The rows of table ``name`` (one of ``GRAPH_TABLES``) of a build, by their key. ``write_graph`` makes them one
    table at a time: those of every table at once, on top of the build's own rows and of a table read back, took
    as much memory again as the build's rows."""
    if name == "kg_nodes":
        return {r[0]: _guarded(r) for r in rows.nodes}
    if name == "kg_edges":
        return {r[0]: _guarded(r) for r in rows.edges}
    if name == "kg_text_rows":
        text: dict[str, tuple] = {}
        for key, row in rows.keyed:
            text[text_row_key(TEXT, key, list(row))] = (key, _json([TEXT, key, list(row)]))
        for nid, body in rows.code.items():
            text[text_row_key(CODE, nid, body)] = (nid, _json([CODE, nid, body]))
        return text
    if name == "kg_provenance":
        prov: dict[str, tuple] = {}
        for unit in rows.units:
            prov[f"{UNIT}\x1f{unit[0]}"] = (unit[0], _json([UNIT, *unit]))
        for elem, unit, span in rows.wheres:
            prov[f"{WHERE}\x1f{elem}\x1f{unit}\x1f{span}"] = (
                elem,
                _json([WHERE, elem, unit, span]),
            )
        order: dict[str, int] = {}
        for elem, units, needs in rows.derivations:
            order[elem] = i = order.get(elem, -1) + 1
            prov[f"{DERIVED}\x1f{elem}\x1f{i:06d}"] = (elem, _json([DERIVED, elem, units, needs]))
        return prov
    if name == "kg_asked":
        return {text_row_key(ASKED, row[0], list(row)): (row[0], _json(list(row))) for row in rows.asked}
    if name == "kg_review_rows":
        review: dict[str, tuple] = {}
        for kind, key, value in rows.clusters:
            review[f"{kind}\x1f{json.dumps(key)}"] = (str(key), _json([kind, key, value]))
        for item in rows.review:
            fp = item["fingerprint"]
            review[f"{REVIEW}\x1f{fp}"] = (fp, _json([REVIEW, fp, item]))
        return review
    if name == "kg_embeddings":
        return {r[0]: (r[1], r[2], r[3], bytes(r[4])) for r in rows.vectors}
    raise ValueError(f"no graph table {name}")


def _guarded(row) -> tuple:
    """A node or edge row (``graph_model.NODE_COLUMNS``, ``EDGE_COLUMNS``) as both sides of ``diff`` hold it: its
    ``label_guards`` last, None or the JSON ``_json`` writes, however it was read (jsonb comes back parsed, or as
    Postgres writes it)."""
    guards = row[11] if len(row) > 11 else None
    if isinstance(guards, str):
        guards = json.loads(guards)
    return (*row[:11], None if guards is None else _json(guards))


def _row_values(name: str, key: str, row: Sequence) -> dict:
    if name in ("kg_nodes", "kg_edges"):
        keys = NODE_KEYS if name == "kg_nodes" else EDGE_KEYS
        guards = json.loads(row[11]) if len(row) > 11 and row[11] is not None else None
        return {**dict(zip(keys, row[:10], strict=True)), "label_aud": row[10], "label_guards": guards}
    if name == "kg_embeddings":
        return {"row_key": key, **dict(zip(("node_id", "kind", "aud", "vector"), row, strict=True))}
    return {"row_key": key, ROW_TABLES[name]: row[0], "row": json.loads(row[1])}


def _stored(name: str, values) -> tuple:
    """A row of table ``name`` as ``graph_rows`` gives it."""
    if name == "kg_nodes":
        return _guarded(tuple(values[k] for k in (*NODE_KEYS, "label_aud", "label_guards")))
    if name == "kg_edges":
        return _guarded(tuple(values[k] for k in (*EDGE_KEYS, "label_aud", "label_guards")))
    if name == "kg_embeddings":
        return (values["node_id"], values["kind"], values["aud"], bytes(values["vector"]))
    return (values[ROW_TABLES[name]], _json(values["row"]))


async def _read(conn: AsyncConnection, name: str, project_id: int, branch_id: int) -> dict[str, tuple]:
    """The rows of table ``name`` of a branch, by key, fetched ``READ_BATCH`` at a time on a server-side cursor: a
    large graph has hundreds of thousands, and fetching and decoding them in one go held the event loop for seconds,
    so every request of the api, the workers' heartbeats and claims among them, waited and timed out."""
    table = getattr(tables, name)
    key = table.primary_key.columns.values()[-1]
    query = select(table).where(table.c.project_id == project_id, table.c.branch_id == branch_id).order_by(key)
    found: dict[str, tuple] = {}
    async for part in (await conn.stream(query.execution_options(yield_per=READ_BATCH))).partitions():
        found.update((row._mapping[key.name], _stored(name, row._mapping)) for row in part)
    return found


def read_columns(name: str) -> list:
    """What ``read_table_rows`` selects of table ``name``: the key, then the columns of a row as ``_stored`` gives it."""
    table = getattr(tables, name)
    key = table.primary_key.columns.values()[-1]
    if name == "kg_nodes":
        return [key, *(table.c[k] for k in (*NODE_KEYS, "label_aud", "label_guards"))]
    if name == "kg_edges":
        return [key, *(table.c[k] for k in (*EDGE_KEYS, "label_aud", "label_guards"))]
    if name == "kg_embeddings":
        return [key, table.c.node_id, table.c.kind, table.c.aud, table.c.vector]
    return [key, table.c[ROW_TABLES[name]], cast(table.c.row, Text)]


async def read_table_rows(conn: AsyncConnection, name: str, project_id: int, branch_id: int) -> dict[str, tuple]:
    """The rows of table ``name`` of a branch, by key, as ``assembled_rows`` reads them, fetched as ``_read`` does. A
    JSON row comes as the text Postgres keeps it in, parsed once in ``assembled_rows``'s thread: ``_read`` parses it, and
    writes it again with its keys sorted for ``diff``, on the event loop, which was half the minute or two the rows of
    a large graph held the api's loop for."""
    table = getattr(tables, name)
    columns = read_columns(name)
    query = (
        select(*columns).where(table.c.project_id == project_id, table.c.branch_id == branch_id).order_by(columns[0])
    )
    found: dict[str, tuple] = {}
    async for part in (await conn.stream(query.execution_options(yield_per=READ_BATCH))).partitions():
        found.update(row_pairs(name, part))
    return found


def row_pairs(name: str, part) -> Iterator[tuple[str, tuple]]:
    """(key, row) of each row of a batch ``read_table_rows`` fetched of table ``name``."""
    if name == "kg_embeddings":
        return ((r[0], (r[1], r[2], r[3], bytes(r[4]))) for r in part)
    if name in ("kg_nodes", "kg_edges"):
        return ((r[0], _guarded(tuple(r[1:]))) for r in part)
    return ((r[0], tuple(r[1:])) for r in part)


def _sorted_object(pairs: list[tuple[str, object]]) -> dict:
    return dict(sorted(pairs))


# a JSON row parsed with every object's keys sorted, as ``_json`` writes it: the same row however it was read
_ROW_JSON = json.JSONDecoder(object_pairs_hook=_sorted_object)


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
    heads = tables.kg_graph_heads
    base = await conn.scalar(
        select(heads.c.content_hash).where(heads.c.project_id == project_id, heads.c.branch_id == branch_id)
    )
    keys: dict[str, object] = {}
    for name in WRITE_ORDER:
        fresh = table_rows(rows, name)
        old = await _read(conn, name, project_id, branch_id)
        changes = diff(old, fresh)
        await _apply(conn, name, project_id, branch_id, fresh, changes)
        changed[name] = changes.counts()
        keys[name] = touched(name, old, fresh, changes)
        del fresh, changes, old
    (level, location), audience = rows.top
    head = {
        "build_id": build_id,
        "content_hash": rows.content_hash,
        "top_label": [level, location],
        "top_audience": sorted(audience),
        "summary": rows.summary,
        "vectors": rows.vector_info,
        "asked_weight": rows.asked_weight,
        "updated_at": func.now(),
    }
    stmt = pg_insert(heads).values(project_id=project_id, branch_id=branch_id, **head)
    await conn.execute(stmt.on_conflict_do_update(index_elements=[heads.c.project_id, heads.c.branch_id], set_=head))
    await notify(conn, project_id, branch_id, build_id, change_note(base, rows.content_hash, keys))
    return changed


def touched(name: str, old: dict[str, tuple], fresh: dict[str, tuple], changes: Changes):
    """What a write of table ``name`` touched, as ``change_note`` names it: the keys of the nodes and edges, the
    (kind, key, digest of the row now or None) of the full-text rows, the elements of the provenance rows, and of
    the other tables whether any row changed."""
    if name in ("kg_nodes", "kg_edges"):
        return sorted({*changes.new, *changes.changed, *changes.deleted})
    if name == "kg_text_rows":
        rows: dict[tuple[str, str], str | None] = {(k[0], old[k][0]): None for k in changes.deleted}
        rows.update(((k[0], fresh[k][0]), k[-16:]) for k in (*changes.new, *changes.changed))
        return sorted([kind, key, digest] for (kind, key), digest in rows.items())
    if name == "kg_provenance":
        return sorted({*(fresh[k][0] for k in (*changes.new, *changes.changed)), *(old[k][0] for k in changes.deleted)})
    return bool(changes.new or changes.changed or changes.deleted)


NOTE_BYTES = 7900  # the most a notice's payload holds: Postgres's NOTIFY takes less than 8000 bytes


def change_note(base: str | None, content_hash: str, keys: dict[str, object]) -> str | None:
    """What a build changed of the rows of a graph whose content hash was ``base`` (``touched``, per table), as the
    text a notice carries (``evo_agents.hub.kg_graph_delta.note_of`` reads it): JSON, compressed, in base 85. None
    when the rows held before were none, when its vectors changed (a reader then reads the rows whole), or when it
    does not fit in a notice."""
    if base is None or keys.get("kg_embeddings"):
        return None
    note = {
        "base": base,
        "hash": content_hash,
        "nodes": keys.get("kg_nodes", []),
        "edges": keys.get("kg_edges", []),
        "text": keys.get("kg_text_rows", []),
        "provenance": keys.get("kg_provenance", []),
        "asked": bool(keys.get("kg_asked")),
        "review": bool(keys.get("kg_review_rows")),
    }
    text = base64.b85encode(zlib.compress(json.dumps(note, separators=(",", ":")).encode(), 9)).decode()
    return text if len(text) < NOTE_BYTES - 200 else None


async def notify(
    conn: AsyncConnection, project_id: int, branch_id: int, build_id: int | None, changes: str | None = None
) -> None:
    """Tell the readers listening on ``CHANNEL`` that the graph of a branch has a new build, and what it changed
    when that fits (``change_note``); sent at commit, on Postgres or on the event bus (``evo_agents.hub.wake``)."""
    note: dict[str, object] = {"project_id": project_id, "branch_id": branch_id, "build_id": build_id}
    if changes is not None:
        note["changes"] = changes
    await wake.notify(conn, CHANNEL, json.dumps(note))


async def _head(conn: AsyncConnection, project_id: int, branch_id: int):
    heads = tables.kg_graph_heads
    head = (
        await conn.execute(
            select(
                heads.c.content_hash,
                heads.c.top_label,
                heads.c.top_audience,
                heads.c.summary,
                heads.c.vectors,
                heads.c.asked_weight,
            ).where(heads.c.project_id == project_id, heads.c.branch_id == branch_id)
        )
    ).one_or_none()
    return head


async def read_head(conn: AsyncConnection, project_id: int, branch_id: int = MAIN) -> StoreRows | None:
    """The head of a branch and its clusters, without the rows of the graph's other tables: what a build of it reads
    of the graph it follows (``kg_build.build_graph``), whose whole rows took as much memory as the build's own while
    it ran. None before its first build."""
    head = await _head(conn, project_id, branch_id)
    if head is None:
        return None
    read = {name: {} for name in GRAPH_TABLES}
    read["kg_review_rows"] = await read_table_rows(conn, "kg_review_rows", project_id, branch_id)
    return await asyncio.to_thread(assembled_rows, head, read)


async def read_graph(conn: AsyncConnection, project_id: int, branch_id: int = MAIN) -> StoreRows | None:
    """The rows a snapshot of a branch is made of, as ``write_graph`` kept them; None before its first build. The
    rows are fetched a batch at a time (``read_table_rows``) and parsed and put together in a thread (``assembled_rows``), so a
    large graph does not hold the event loop."""
    fetched = await fetch_graph(conn, project_id, branch_id)
    return None if fetched is None else await assemble_graph(fetched)


async def fetch_graph(conn: AsyncConnection, project_id: int, branch_id: int = MAIN) -> tuple | None:
    """The head and the raw rows of a branch's tables, for ``assemble_graph``; None before its first build. Only this
    reads: a caller that can give its connection back before the rows are put together (``assemble_graph`` takes
    minutes on a large graph) does so between the two."""
    head = await _head(conn, project_id, branch_id)
    if head is None:
        return None
    return head, {name: await read_table_rows(conn, name, project_id, branch_id) for name in GRAPH_TABLES}


async def assemble_graph(fetched: tuple) -> StoreRows:
    """The ``StoreRows`` of what ``fetch_graph`` read, put together in a thread; it needs no connection."""
    return await asyncio.to_thread(assembled_rows, *fetched)


GRAPH_TABLES = (
    "kg_nodes",
    "kg_edges",
    "kg_text_rows",
    "kg_provenance",
    "kg_embeddings",
    "kg_asked",
    "kg_review_rows",
)  # the tables ``read_graph`` reads a branch's rows from

WRITE_ORDER = (
    "kg_nodes",
    "kg_edges",
    "kg_text_rows",
    "kg_provenance",
    "kg_asked",
    "kg_review_rows",
    "kg_embeddings",
)  # the order ``write_graph`` writes them in


def assembled_rows(head, read: dict[str, dict[str, tuple]]) -> StoreRows:
    """The ``StoreRows`` of the head of a branch and the rows of its tables (``GRAPH_TABLES``) by key."""
    keyed, code = [], {}
    for _, row in paced(read["kg_text_rows"].items()):
        kind, key, value = _ROW_JSON.decode(row[1])
        if kind == TEXT:
            keyed.append((key, tuple(value)))
        else:
            code[key] = value
    top = ((head.top_label[0], head.top_label[1]), frozenset(head.top_audience))
    units, wheres, derived = [], [], []
    for _, row in paced(sorted(read["kg_provenance"].items())):
        kind, *values = _ROW_JSON.decode(row[1])
        {UNIT: units, WHERE: wheres, DERIVED: derived}[kind].append(tuple(values))
    clusters, review = [], []
    for _, row in paced(read["kg_review_rows"].items()):
        kind, key, value = _ROW_JSON.decode(row[1])
        if kind == REVIEW:
            review.append(value)
        else:
            clusters.append((kind, key, value))
    return StoreRows(
        head.content_hash,
        tuple(read["kg_nodes"].values()),
        tuple(read["kg_edges"].values()),
        tuple(sorted(keyed)),
        code,
        top,
        tuple(units),
        tuple(sorted(wheres, key=lambda w: (w[0], w[1], w[2] is not None, w[2] or ""))),  # as SQLite orders them
        tuple(derived),
        head.summary,
        tuple((key, *row) for key, row in sorted(read["kg_embeddings"].items())),
        head.vectors,
        asked_order(tuple(_ROW_JSON.decode(row[1])) for row in read["kg_asked"].values()),
        head.asked_weight,
        cluster_order(clusters),
        tuple(sorted(review, key=lambda item: item["fingerprint"])),
    )


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
            {
                "project_id": project_id,
                "branch_id": branch_id,
                **item.__dict__,
                "label": runs.without_nul(item.label),
                "audience": list(item.audience),
                "body": runs.without_nul(item.body),
            }
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
            {"project_id": project_id, "extractor": extractor, "row_hash": h, "result": runs.without_nul(r)}
            for h, r in entries[i : i + BATCH]
        ]
        await conn.execute(pg_insert(memo).values(values).on_conflict_do_nothing())
