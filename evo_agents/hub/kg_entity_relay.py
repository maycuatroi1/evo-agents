"""How a hub build reaches Claude Code for the entities step: the build container has no ``claude``, a run of the
project's default agent pool has one, logged in with the lease of the pool's account.

A build whose entities mapping runs Claude Code (``evo_agents.kg.claude_code``) calls nothing on the hub: the step
defers each item its memo lacks (``evo_agents.kg.entities.run`` with an outbox), and the build keeps those requests as
one blob of kind REQUESTS_KIND, made by the hub (``kg_build.record``), named in ``kg_builds.coverage`` under
``entity_requests`` with their count. Each request holds the text of an item that the mapping's llm-backend sink
cleared, the node kinds and relations it may name, the model, and its key in the memo.

The nightly review run of the project (``collect.review_prompt``) is told to answer them when the latest build has
some: ``evo-agents hub kg entities --project P`` (``evo_agents.hub.kg_entities_cli``) reads them
(GET /v1/kg/P/entities/requests), asks Claude Code for each on the run's login, never an API key, and posts the
answers (POST /v1/kg/P/entities/answers, ``evo_agents.hub.server.kg_entities``), which the hub keeps as a blob of kind
ANSWERS_KIND and follows with a build. That build reads every answers blob of the project (``read_answers``) and gives
them to the step as its inbox: an answer whose key the memo lacks is cut to the build's own ontology and memoized, so
the builds after it read it from the memo, and the cache can still be deleted: the blobs give it again.

A login refused or a limit reached stops the client at that item: it posts what it has, and the next night goes on.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import blob_gc, tables
from evo_agents.hub.blobs import BlobStore, blob_key

REQUESTS_KIND = "kg-entity-requests"
ANSWERS_KIND = "kg-entity-answers"
MiB = 1024 * 1024
MAX_REQUESTS_BYTES = 64 * MiB
MAX_ANSWERS_BYTES = 8 * MiB  # one post of answers
REQUESTS_FILE = "entity-requests.json"  # in the project's cache directory
REQUEST_KEYS = ("key", "item", "text", "node_kinds", "relations", "model")
OPTIONAL_KEYS = ("style",)  # kept only when the request has it: a closed mapping's requests read as they did


class RelayError(ValueError):
    """Requests or answers that are not what this module writes."""


def write_requests(root: Path, outbox: list[dict]) -> tuple[Path, str, int, int] | None:
    """The deferred requests of a build as REQUESTS_FILE under ``root``: its path, SHA-256, size and the count; None,
    and no file, when there is none."""
    path = root / REQUESTS_FILE
    if not outbox:
        path.unlink(missing_ok=True)
        return None
    rows = sorted(
        ({**{k: r.get(k) for k in REQUEST_KEYS}, **{k: r[k] for k in OPTIONAL_KEYS if r.get(k)}} for r in outbox),
        key=lambda r: str(r["key"]),
    )
    data = json.dumps({"version": 1, "requests": rows}, ensure_ascii=False, sort_keys=True).encode()
    if len(data) > MAX_REQUESTS_BYTES:
        raise RelayError(f"the deferred entity requests are {len(data)} bytes, over {MAX_REQUESTS_BYTES}")
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest(), len(data), len(rows)


def parse_requests(data: bytes) -> list[dict]:
    try:
        found = json.loads(data)
    except ValueError:
        raise RelayError("the entity requests are not JSON") from None
    rows = found.get("requests") if isinstance(found, dict) else None
    if not isinstance(rows, list):
        raise RelayError("the entity requests hold no list")
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("key"), str)]


def clean_answers(answers) -> dict[str, dict]:
    """``answers``, {key: {nodes, edges, usage}}, with only what the step reads: lists of objects and a usage of
    numbers. RelayError when it is not a mapping of keys to objects."""
    if not isinstance(answers, dict):
        raise RelayError("the answers are not an object of keys")
    found = {}
    for key, answer in answers.items():
        if not isinstance(key, str) or not isinstance(answer, dict):
            raise RelayError("each answer is an object under the key of its request")
        usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
        found[key] = {
            "nodes": [n for n in answer.get("nodes") or [] if isinstance(n, dict)],
            "edges": [e for e in answer.get("edges") or [] if isinstance(e, dict)],
            "usage": {
                k: v
                for k, v in usage.items()
                if k in ("input_tokens", "output_tokens", "cost")
                and isinstance(v, (int, float))
                and not isinstance(v, bool)
            },
        }
    return found


def encode_answers(answers: dict[str, dict]) -> tuple[bytes, str]:
    data = json.dumps({"version": 1, "answers": answers}, ensure_ascii=False, sort_keys=True).encode()
    return data, hashlib.sha256(data).hexdigest()


def read_answers(store: BlobStore, hashes: list[str]) -> dict[str, dict]:
    """The answers of the blobs ``hashes``, in that order, a later one over an earlier one of the same key; a blob
    missing or not of this module is left out. Blocking."""
    found: dict[str, dict] = {}
    for sha in hashes:
        sink = io.BytesIO()
        got = store.fetch(blob_key(sha), sink, MAX_ANSWERS_BYTES)
        if got is None or got[0] != sha:
            continue
        try:
            data = json.loads(sink.getvalue())
            found.update(clean_answers(data.get("answers") if isinstance(data, dict) else None))
        except (ValueError, RelayError):
            continue
    return found


async def answer_hashes(conn: AsyncConnection, project_id: int) -> list[str]:
    """The answers blobs the project holds, by hash: the order ``read_answers`` reads them in."""
    b = tables.blobs
    rows = await conn.execute(
        select(b.c.sha256).where(b.c.project_id == project_id, b.c.kind == ANSWERS_KIND).order_by(b.c.sha256)
    )
    return list(rows.scalars())


async def hold(
    conn: AsyncConnection,
    store: BlobStore,
    project_id: int,
    sha256: str,
    path: Path,
    size: int,
    kind: str,
    created_by: int | None,
) -> None:
    """Record that the project holds blob ``sha256`` (uploaded from ``path`` already), put back first when its
    deletion was pending. In the caller's transaction, under the shared blob lock."""
    await blob_gc.lock_shared(conn)
    if await blob_gc.revive(conn, [sha256]):
        await asyncio.to_thread(store.put_file, sha256, path, replace=True)
    b = tables.blobs
    await conn.execute(
        pg_insert(b)
        .values(project_id=project_id, sha256=sha256, size=size, kind=kind, created_by=created_by)
        .on_conflict_do_nothing(index_elements=[b.c.project_id, b.c.sha256])
    )


def requests_of(coverage) -> tuple[str, int] | None:
    """The hash and count of the requests a build's coverage names; None without them."""
    held = coverage.get("entity_requests") if isinstance(coverage, dict) else None
    if not isinstance(held, dict) or not isinstance(held.get("sha256"), str):
        return None
    count = held.get("count")
    return held["sha256"], int(count) if isinstance(count, int) else 0


async def latest_requests(conn: AsyncConnection, project_id: int) -> tuple[int, str, int] | None:
    """The latest succeeded build of the project, the hash of its requests and their count; None when that build
    deferred nothing, or there is none."""
    k = tables.kg_builds
    row = (
        await conn.execute(
            select(k.c.id, k.c.coverage)
            .where(k.c.project_id == project_id, k.c.status == "succeeded")
            .order_by(k.c.id.desc())
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        return None
    found = requests_of(row.coverage)
    return None if found is None else (row.id, *found)


def prompt_line(project: str, count: int) -> str:
    """What the review run's prompt says when the latest build left ``count`` entity requests to answer."""
    return (
        f"\n\nThe latest knowledge-graph build of {project} left {count} item(s) for the entities step, which runs "
        f"Claude Code and which the hub cannot run. Before the lenses, run `evo-agents hub kg entities --project "
        f"{project}` once: it answers them on this run's login and the hub builds again. If it stops on a login or a "
        "limit, say so in one line and go on."
    )
