"""Group chats (``evo_agents.hub.chats``): chats of people and agents, direct chats, settle and work chats, their
messages, mentions, reactions, images and labels, the chat runs of their agents, the chat bridges and the stream of
what happens in a chat. ``docs/hub.md`` (Group chats) is the design and ``docs/workers.md`` (Chat runs) the protocol
of a chat run's agent.

The package has four layers, which import-linter keeps in order (contract ``hub-chats-layers``): ``routes`` and
``mcp`` (the HTTP routes and the read tool of the hub's /mcp), then ``service`` (who may do what, and what a chat
shows), ``repository`` (every SQL statement, on ``evo_agents.hub.tables``) and ``models`` (the Pydantic bodies, the
values the layers pass and the texts of the refusals).
"""
