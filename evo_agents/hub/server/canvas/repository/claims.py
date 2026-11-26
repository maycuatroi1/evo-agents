"""The claims of a canvas as versioned records (``ClaimStore``, a ``VersionedStore``): a change names the rev it was
read at (``base_rev``), and the claim's rev goes up by an UPDATE that holds only while the rev is still that one, in
the transaction that writes the rev. A write on claims changed since is refused with, per claim, the rev asked for,
the rev it is at, who changed it, both contents and what differs (``stale``). A new claim is kept at rev 1
(``create``).
"""

from __future__ import annotations

from typing import Any, ClassVar

from sqlalchemy import func, insert, null, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables
from evo_agents.hub.server.base.versions import VersionedStore
from evo_agents.hub.server.canvas.models.content import LINKS, CanvasError, Content, diff
from evo_agents.hub.server.canvas.models.rows import CanvasRow, ClaimRow
from evo_agents.hub.server.canvas.models.writing import NextRev
from evo_agents.hub.server.canvas.repository import store


class ClaimStore(VersionedStore[int, ClaimRow]):
    """The claims of canvas ``canvas_id`` of project ``project_id``, by number, at their rev."""

    noun: ClassVar[str] = "claim"

    def __init__(self, project_id: int, canvas_id: int):
        self.project_id = project_id
        self.canvas_id = canvas_id

    async def held(self, conn: AsyncConnection, key: int) -> ClaimRow | None:
        row = await store.claim_of(conn, self.project_id, key, lock=True)
        return None if row is None or row.canvas_id != self.canvas_id else row

    def version_of(self, row: ClaimRow) -> int:
        return row.rev

    async def stale(self, conn: AsyncConnection, key: int, row: ClaimRow, base: int) -> dict:
        """What a member reads when claim ``row`` changed since ``base``: both contents, who changed it, the diff."""
        before = await store.rev(conn, row.id, base) if base < row.rev else None
        [last] = await store.revs(conn, row.id, since=row.rev)
        current = row.content()
        return {
            "claim": row.code,
            "base_rev": base,
            "rev": row.rev,
            "changed_by": last.provenance.login or last.provenance.actor,
            "base": before.content.as_json() if before else None,
            "current": current.as_json(),
            "changes": diff(before.content, current) if before else [],
        }

    def refusal(self, stale: list) -> Exception:
        names = ", ".join(item["claim"] for item in stale)
        return CanvasError(409, f"{names} changed since you read them: nothing was written; read them again", stale)

    async def insert(
        self, conn: AsyncConnection, key: int, row: ClaimRow | None, version: int, values: NextRev
    ) -> None:
        """Move claim ``row`` to ``values.after`` as rev ``version``, while it is still at ``row.rev``; its open
        proposals go stale, made at a rev it no longer is at."""
        if row is None:
            raise ValueError(f"claim {key} is new: ClaimStore.create keeps it")
        k, r = tables.canvas.claims, tables.canvas.revs
        after = values.after
        moved = (
            update(k)
            .where(k.c.id == row.id, k.c.rev == row.rev)
            .values(**_columns(after), archived_at=(row.archived_at or func.now()) if after.archived else None)
            .values(rev=version, updated_at=func.now())
            .returning(k.c.rev)
        )
        if await conn.scalar(moved) is None:
            raise CanvasError(409, f"{row.code} changed since rev {row.rev}: read it again")
        p = tables.canvas.proposals  # a proposal made at an older rev can no longer be accepted
        await conn.execute(update(p).where(p.c.claim_id == row.id, p.c.status == "open").values(status="stale"))
        await conn.execute(
            insert(r).values(
                claim_id=row.id,
                rev=version,
                content=after.as_json(),
                change=values.change,
                undo_of=values.undo_of,
                reason=values.reason,
                baseline_id=values.baseline_id,
                **values.author.columns(),
            )
        )

    async def create(
        self, conn: AsyncConnection, canvas: CanvasRow, number: int, content: Content, values: NextRev
    ) -> int:
        """Keep claim ``number`` of ``canvas`` with ``content`` at rev 1, under the canvas's label; its id."""
        k, r = tables.canvas.claims, tables.canvas.revs
        row = {
            "project_id": self.project_id,
            "canvas_id": canvas.id,
            "number": number,
            **_columns(content),
            "archived_at": func.now() if content.archived else None,
            "label": canvas.label,
            "created_by": values.author.user_id,
        }
        claim_id = int(await conn.scalar(insert(k).values(**row).returning(k.c.id)) or 0)
        revision = {"claim_id": claim_id, "rev": 1, "content": content.as_json(), "change": values.change}
        await conn.execute(insert(r).values(**revision, reason=values.reason, **values.author.columns()))
        return claim_id


def _columns(content: Content) -> dict:
    return {
        "cell": content.cell,
        "position": content.position,
        "text": content.text,
        "status": content.status,
        "confidence": content.confidence,
        "delivery": content.delivery,
        "test_card": null() if content.test_card is None else content.test_card,
        **{name: list(getattr(content, name)) for name in LINKS},
    }


async def next_number(conn: AsyncConnection, project_id: int) -> int:
    """The number the next claim of project ``project_id`` takes."""
    k = tables.canvas.claims
    last = await conn.scalar(select(func.max(k.c.number)).where(k.c.project_id == project_id))
    return int(last or 0) + 1


async def next_position(conn: AsyncConnection, canvas_id: int, cell: str) -> int:
    """The place at the end of cell ``cell`` of canvas ``canvas_id``."""
    k = tables.canvas.claims
    last = await conn.scalar(select(func.max(k.c.position)).where(k.c.canvas_id == canvas_id, k.c.cell == cell))
    return 0 if last is None else int(last) + 1


async def claim_count(conn: AsyncConnection, canvas_id: int) -> int:
    """How many claims canvas ``canvas_id`` holds, archived ones included."""
    k = tables.canvas.claims
    return int(await conn.scalar(select(func.count()).select_from(k).where(k.c.canvas_id == canvas_id)) or 0)


async def command_revs(conn: AsyncConnection, canvas_id: int, user_id: int | None) -> list[Any]:
    """Every rev of each claim of canvas ``canvas_id`` an agent wrote for ``user_id`` in a chat command, by claim
    number and rev: its number, rev, change, the rev it undid, whether it is one of those (``mine``) and the chat
    message that asked for it."""
    r, k = tables.canvas.revs, tables.canvas.claims
    mine = (r.c.actor == "agent") & (r.c.user_id == user_id) & r.c.chat_message_id.is_not(None)
    touched = select(r.c.claim_id).join_from(r, k, k.c.id == r.c.claim_id).where(k.c.canvas_id == canvas_id, mine)
    query = (
        select(k.c.number, r.c.rev, r.c.change, r.c.undo_of, mine.label("mine"), r.c.chat_message_id)
        .join_from(r, k, k.c.id == r.c.claim_id)
        .where(r.c.claim_id.in_(touched))
        .order_by(k.c.number, r.c.rev)
    )
    return list(await conn.execute(query))


__all__ = ["ClaimStore", "command_revs", "claim_count", "next_number", "next_position"]
