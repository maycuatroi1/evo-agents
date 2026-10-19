"""The core plugin chats: the history of the hub's group chats as a source of each project's knowledge graph
(criterion a4 of plan chat-history-in-kg; ``docs/plugins.md``, Core plugins).

A project that enables it and grants it ``chats:read`` has in every build of its graph on the hub each message whose
label holds a part of the project, with its thread and its chat (``kg``). A message keeps the label its chat had when
it was sent (``evo_agents.hub.server.chat_access``); in the graph its item carries the project's part of that label and
an audience: the chat, and a grant on each other project the label names, so a reader sees it only while they are in
the chat and could read the message there. Search finds a message by its text, which no tool returns.

When a message is sent, the hub queues a build of each project its label names that has a plugin reading the chats
(``evo_agents.hub.server.chat_store``), which joins a build that waits.

The hub imports this package for the manifest only: ``kg`` gives the knowledge graph the messages.
"""

KG = "evo_agents.core_plugins.chats.kg"
READS = "chats:read"  # what the source reads: the project's group chats (evo_agents.plugins.manifest.PERMISSIONS)

MANIFEST = {
    "id": "chats",
    "version": "1.0.0",
    "publisher": "evo-agents",
    "tier": "core",
    "name": "Chat history",
    "description": "The messages of the hub's group chats in the project's knowledge graph, for who is in each chat.",
    "engines": {"evo-agents": ">=0.22"},
    "plugin_api": 1,
    "permissions": [READS],
    "contributes": {
        "kg_sources": [
            {"name": "history", "title": "The messages of the group chats of the project", "python": f"{KG}:kg_source"}
        ],
    },
}

__all__ = ["MANIFEST", "READS"]
