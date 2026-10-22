"""The audience of memories (``evo_agents.kg.policy``): a memory derived from chat messages carries the audience of
those chats, and every read of memories gives one only to a reader whose audience holds it (criterion a6 of plan
chat-history-in-kg).

A memory's label holds its audience under the key ``audience``, sorted principals, as a kg/1 label does; a memory
without one passes this rule for every reader, and one that cannot be read passes it for none. The reader's audience is
the one ``kg_audience.reader_audience`` computes from the database at that read, as for the knowledge graph: the chats a
person is in and clears the label of now, and their grants; for the agent of a chat run what both people it is cleared
for hold; for the agent of another run its owner's. ``Reader`` reads it at the first memory of a request that names an
audience, since most name none and the reads of memories run on every pull and push (docs/hub.md, Data access).

A write only keeps or raises an audience (``raised``): what the memory held, what the writer says it read (a Claude
Code session's record, sent by ``evo-agents hub mcp`` and by ``hub memory push``), and for the agent of a run what the
hub noted it was shown (``run_audience``). Lowering one is PUT /v1/memories/{id}/audience, an admin's of the memory's
project alone (``memories.lower_audience``).
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub.server import run_audience
from evo_agents.hub.server.kg_audience import reader_audience
from evo_agents.hub.server.security import Principal
from evo_agents.kg.policy import TOP_AUDIENCE, audience_below, audience_of


def label_audience(label) -> frozenset[str]:
    """The audience a stored label names: none without one, the highest (``TOP_AUDIENCE``) for one that cannot be
    read."""
    return audience_of(label.get("audience")) if isinstance(label, dict) else TOP_AUDIENCE


def names_one(label) -> bool:
    """Whether a stored label names an audience, one that cannot be read included."""
    return not isinstance(label, dict) or label.get("audience") not in (None, [])


def with_audience(label: dict, audience: frozenset[str]) -> dict:
    """``label`` holding ``audience`` instead of its own: without the key when it is empty."""
    kept = {key: value for key, value in label.items() if key != "audience"}
    return {**kept, "audience": sorted(audience)} if audience else kept


class Reader:
    """One request's reader of memories: the caller's audience, read once at the first label that names one."""

    def __init__(self, conn: AsyncConnection, user: Principal):
        self.conn = conn
        self.user = user
        self._held: frozenset[str] | None = None

    async def audience(self, label) -> frozenset[str]:
        """What a memory labelled ``label`` is judged with: the caller's audience when the label names one, else
        none, which a label without one passes."""
        if not names_one(label):
            return frozenset()
        if self._held is None:
            self._held = await reader_audience(self.conn, self.user)
        return self._held

    async def gave(self, labels: Iterable) -> None:
        """For the agent of a run, note the audiences of the memories it is given, labelled ``labels``, as what it
        was shown (``run_audience``)."""
        if self.user.scope is not None:
            shown = frozenset().union(*(label_audience(label) for label in labels))
            await run_audience.note(self.conn, self.user.scope.run_id, shown)


async def raised(conn: AsyncConnection, user: Principal, held, given: Iterable[str] | None) -> frozenset[str]:
    """The audience a memory takes when ``user`` writes it: that of its label ``held`` (None for a new one), the
    principals ``given`` with the write, and for the agent of a run what the run was shown."""
    found = set(label_audience(held)) if held is not None else set()
    found.update(given or ())
    if user.scope is not None:
        found.update(await run_audience.seen(conn, user.scope.run_id))
    return frozenset(found)


async def label_reaches(reader: Reader, access, label, through: str) -> bool:
    """Whether a row labelled ``label`` (a proposal of the Curator) reaches ``reader``'s caller through sink
    ``through`` of ``access`` (``projects.ProjectAccess``): the read rule, and the audience its label names, if any
    (the memories its evidence names, ``memory_promotion.cited_audience``)."""
    if not access.visible(label, through):
        return False
    named = label_audience(label)
    return not named or audience_below(named, await reader.audience(label))
