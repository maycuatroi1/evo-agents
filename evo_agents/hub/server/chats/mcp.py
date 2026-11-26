"""The chat tools of /mcp (``evo_agents.hub.server.mcp``; their schemas are ``mcp_tools.CHAT_TOOLS``): chat_message
and chat_thread read group chat messages whole (``chats.service.read``) through the envelope of ``kg serve``."""

from __future__ import annotations

from evo_agents.hub.server.chats.service.read import read, readers_of, session_label
from evo_agents.hub.server.run_audience import note
from evo_agents.hub.server.security import Principal
from evo_agents.kg.serve import Session
from evo_agents.kg.sessions import READ_META


class _Answer(Session):
    """A reading through the envelope of ``kg serve``: over CAP_CHARS it is cut, and the rest waits for kg_more, which
    gives it while the reader's audience holds ``held``."""

    def __init__(self, sink: str, text: str, data: dict, held: frozenset[str]):
        super().__init__(None, sink)
        self.answered, self.held = (text, data), held

    def call(self, name: str, args: dict) -> dict:
        result = self._envelope(*self.answered)
        self.handles = {key: (made, chunks, self.held) for key, (made, chunks, *_) in self.handles.items()}
        return result


async def answer(state, user: Principal, sink: str, owner: tuple, name: str, arguments: dict) -> dict:
    """The result of tool ``name`` of ``mcp_tools.CHAT_TOOLS``, its ``arguments`` checked against its schema, for
    ``user`` in a session through ``sink``; the kg_more handles of a cut one are ``owner``'s. HTTPException 404 with
    NOT_THERE when the message may not be read."""
    async with state.engine.begin() as conn:
        readers = await readers_of(conn, user)
        reading, kept = await read(conn, readers, arguments["id"], name == "chat_thread", sink)
        if user.scope is not None:
            await note(conn, user.scope.run_id, kept)
        label = await session_label(conn, owner[1], kept)
    data = reading.model_dump(mode="json", exclude={"text"})
    data["summary"] = f"{len(reading.messages)} message(s) of chat {reading.chat_id}"
    data["label"] = label  # what the reading reveals, as a kg_* result says it
    result = state.kg_handles.call(_Answer(sink, reading.text, data, kept), owner, name, {})
    read_meta = {"project": owner[1], "label": label, "results_read": len(reading.messages)}
    for block in result.get("content") or []:
        block.setdefault("_meta", {})[READ_META] = read_meta  # the text block: what ``kg.sessions`` reads
    return result
