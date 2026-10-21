"""Taking back a member's last command to @strategist in chat (``undo_command``): the revs the agent wrote for that
member while one chat message asked for them, grouped by the message, as Yjs groups an undo by its tracked origin.

The command is the newest chat message of the member with revs on the canvas, written by an agent for them, that no
undo took back yet (an undo takes back the revs from its ``undo_of`` up to it); an undo's own revs are never a
command. Each claim the command changed goes back to the rev before
the command's first rev of it (a claim the command made is archived), in one new rev whose ``undo_of`` names that
first rev, so the next undo in chat finds the command before. Each claim must be as the command left it (the revs
after it, such as a later command and its undo, change nothing in sum), and no one else's rev may sit between the
command's own: a claim someone changed since makes the whole undo a 409 with what changed, and nothing is written. The
rules of ``rules`` hold as for every other rev. The caller holds the transaction and the lock of the canvas.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.canvas import rules, store, writes
from evo_agents.core_plugins.canvas.content import CanvasError, code
from evo_agents.core_plugins.canvas.store import CanvasRow
from evo_agents.hub import tables


@dataclass(frozen=True)
class _Rev:
    rev: int
    change: str
    undo_of: int | None
    mine: bool  # written by an agent for the member, while a chat message asked for it
    message_id: int | None


@dataclass(frozen=True)
class Undone:
    """What an undo of a command did: the chat message it took back, and each claim with the rev it went back to
    and the rev it wrote."""

    message_id: int
    claims: list[tuple[int, int, int]]  # (number, rev it went back to, rev written)


async def _revs(conn: AsyncConnection, canvas_id: int, user_id: int) -> dict[int, list[_Rev]]:
    """By claim number, every rev of each claim of the canvas an agent wrote for ``user_id`` in a chat command."""
    r, k = tables.canvas.revs, tables.canvas.claims
    mine = (r.c.actor == "agent") & (r.c.user_id == user_id) & r.c.chat_message_id.is_not(None)
    touched = select(r.c.claim_id).join_from(r, k, k.c.id == r.c.claim_id).where(k.c.canvas_id == canvas_id, mine)
    query = (
        select(k.c.number, r.c.rev, r.c.change, r.c.undo_of, mine.label("mine"), r.c.chat_message_id)
        .join_from(r, k, k.c.id == r.c.claim_id)
        .where(r.c.claim_id.in_(touched))
        .order_by(k.c.number, r.c.rev)
    )
    found: dict[int, list[_Rev]] = defaultdict(list)
    for row in await conn.execute(query):
        found[row.number].append(_Rev(row.rev, row.change, row.undo_of, bool(row.mine), row.chat_message_id))
    return found


def _taken_back(rows: list[_Rev]) -> set[int]:
    """The revs of a claim an undo took back: an undo rev takes back every rev from its ``undo_of`` to the one before
    it, the API's undo of the current rev and the undo of a command alike."""
    gone: set[int] = set()
    for row in rows:
        if row.change == "undo" and row.undo_of is not None:
            gone.update(range(row.undo_of, row.rev))
    return gone


def _last_command(revs: dict[int, list[_Rev]]) -> tuple[int, dict[int, list[int]]] | None:
    """(the newest message with revs not taken back, {claim number: those revs})."""
    commands: dict[int, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
    for number, rows in revs.items():
        gone = _taken_back(rows)
        for row in rows:
            if row.mine and row.change != "undo" and row.rev not in gone:
                commands[row.message_id][number].append(row.rev)
    if not commands:
        return None
    message_id = max(commands)
    return message_id, dict(commands[message_id])


async def undo_command(conn: AsyncConnection, writer: writes.Writer, canvas: CanvasRow, reason: str) -> Undone:
    """Take back the last command of the member ``writer`` writes for (see the module)."""
    store.writable(writer.access, canvas)
    found = _last_command(await _revs(conn, canvas.id, writer.author.user_id))
    if found is None:
        raise CanvasError(
            409, f"no command in chat changed canvas {canvas.slug} for you that is not taken back: nothing to undo"
        )
    message_id, spans = found
    held = await writes.locked(conn, writer, canvas, list(spans))
    conflicts = []
    for number, written in spans.items():
        row = held[number]
        left = await store.rev(conn, row.id, written[-1])
        between = written != list(range(written[0], written[-1] + 1))  # another rev among the command's own
        if between or left is None or left.content != row.content():  # or the claim changed since the command
            conflicts.append(await writes.conflict(conn, row, written[-1]))
    if conflicts:
        names = ", ".join(item["claim"] for item in conflicts)
        raise CanvasError(
            409, f"{names} changed since the command of message {message_id}: nothing was taken back", conflicts
        )
    evidence = await writes.facts(conn, [row.id for row in held.values()])
    done = []
    for number in sorted(spans):
        row, first = held[number], spans[number][0]
        after = await writes.undone(conn, row, first)
        rules.check(number, row.content(), after, evidence[row.id], writer.author.actor)
        why = reason or f"undo of the command of message {message_id}"
        rev = await writes.write_rev(conn, writer, row, after, why, "undo", undo_of=first)
        done.append((number, first - 1, rev))
    await writes.touch(conn, canvas)
    return Undone(message_id, done)


def undone_text(done: Undone) -> list[str]:
    """One line per claim an undo of a command took back."""
    return [
        f"- {code(number)}: {'archived' if back == 0 else f'back to rev {back}'}, as rev {rev}"
        for number, back, rev in done.claims
    ]


__all__ = ["Undone", "undo_command", "undone_text"]
