"""Where the tag nightly of evo-agents points, as the hub reads it once a minute for all its workers (the job
``hub.read_nightly`` of ``evo_agents.hub.worker``) and keeps it in hub_nightly for the api, which hands it to every
daemon in its heartbeat answer.

The hub's image has no git, and the GitHub API without a token allows 60 requests an hour from one address, exactly
the pace of the job. So the hub speaks git's own HTTP protocol, version 2, the way ``git ls-remote`` does: one POST of
the command ls-refs to ``<repo>.git/git-upload-pack``, asking for the tag alone (ref-prefix) and for the commit an
annotated tag points to (peel). That endpoint is not counted against the API's quota, and the answer is one line.

A read that fails leaves the row as it was, with the time of the last read that succeeded, and logs one line; until a
read succeeds there is no row, and the heartbeat answer carries no nightly.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

import httpx
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from evo_agents import __version__
from evo_agents.hub import tables
from evo_agents.hub.runs import NIGHTLY_REPO_URL, NIGHTLY_TAG

log = logging.getLogger(__name__)

REF = f"refs/tags/{NIGHTLY_TAG}"
UPLOAD_PACK_URL = f"{NIGHTLY_REPO_URL}.git/git-upload-pack"
READ_TIMEOUT = 20.0  # seconds; the job runs every minute
COMMIT = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")  # evo_agents.hub.server.runs.models.OBJECT_NAME
HEADERS = {
    "Git-Protocol": "version=2",
    "Content-Type": "application/x-git-upload-pack-request",
    "Accept": "application/x-git-upload-pack-result",
    "User-Agent": f"git/evo-agents-hub-{__version__}",
}
DELIM, FLUSH = b"0001", b"0000"


def pkt_line(text: str) -> bytes:
    """One pkt-line of git's protocol: its length in four hexadecimal digits, the 4 included, then the text."""
    data = text.encode()
    return f"{len(data) + 4:04x}".encode() + data


LS_REFS = pkt_line("command=ls-refs\n") + DELIM + pkt_line("peel\n") + pkt_line(f"ref-prefix {REF}\n") + FLUSH


def pkt_lines(body: bytes) -> list[str]:
    """The texts of the pkt-lines of ``body``, flush and delimiter packets left out; ValueError when it is not
    pkt-lines."""
    lines, at = [], 0
    while at < len(body):
        size = int(body[at : at + 4].decode("ascii"), 16)
        if size < 4:  # 0000 flush, 0001 delimiter, 0002 response end
            at += 4
            continue
        if at + size > len(body):
            raise ValueError("a pkt-line runs past the end of the answer")
        lines.append(body[at + 4 : at + size].decode().rstrip("\n"))
        at += size
    return lines


def parse_ls_refs(body: bytes) -> str | None:
    """The commit of the tag in an answer to LS_REFS: what an annotated tag peels to, else what the tag names."""
    for line in pkt_lines(body):
        oid, _, rest = line.partition(" ")
        ref, *attributes = rest.split(" ")
        if ref != REF:
            continue
        peeled = [value for value in attributes if value.startswith("peeled:")]
        commit = peeled[0].removeprefix("peeled:") if peeled else oid
        return commit if COMMIT.match(commit) else None
    return None


async def read_commit(client: httpx.AsyncClient | None = None) -> str:
    """The commit nightly points to now; LookupError says why it could not be read."""
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=READ_TIMEOUT) as own:
                response = await own.post(UPLOAD_PACK_URL, content=LS_REFS, headers=HEADERS)
        else:
            response = await client.post(UPLOAD_PACK_URL, content=LS_REFS, headers=HEADERS)
        response.raise_for_status()
        commit = parse_ls_refs(response.content)
    except (httpx.HTTPError, ValueError, UnicodeDecodeError) as exc:
        raise LookupError(f"{type(exc).__name__}: {exc}"[:500]) from exc
    if commit is None:
        raise LookupError(f"{NIGHTLY_REPO_URL} has no tag {NIGHTLY_TAG}")
    return commit


async def refresh(engine: AsyncEngine, read=read_commit) -> dict:
    """Read where nightly points and keep it with the time of the read; a read that fails keeps what was there and
    logs one line."""
    nightly = tables.hub_nightly
    try:
        commit = await read()
    except LookupError as exc:
        async with engine.connect() as conn:
            kept = (await conn.execute(nightly.select())).one_or_none()
        held = {"commit": kept.commit_sha, "read_at": kept.read_at.isoformat()} if kept else {}
        log.warning("nightly not read, the last read kept", extra={"error": str(exc), **held})
        return {"read": False, "error": str(exc), **held}
    statement = insert(nightly).values(id=1, commit_sha=commit, read_at=func.now())
    statement = statement.on_conflict_do_update(
        index_elements=[nightly.c.id], set_={"commit_sha": commit, "read_at": func.now()}
    ).returning(nightly.c.read_at)
    async with engine.begin() as conn:
        read_at: datetime = (await conn.execute(statement)).scalar_one()
    return {"read": True, "commit": commit, "read_at": read_at.isoformat()}
