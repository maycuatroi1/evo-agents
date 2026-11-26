"""The work chat of a trace of an agent system (``chats.WORK``; ``docs/hub.md``, Work chats); ``service`` tells how it
opens and how work is handed over in it.

Layered as ``service`` (opening the chat, the hand-offs, the hub's lines and the copies) over ``repository`` (every SQL
query) over ``models`` (the trace, the hand-off and the limits); it has no routes. The import-linter contract
hub-work-chats-layers holds it.
"""
