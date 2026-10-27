"""northwind-chat, the sample core plugin outside evo_agents: a two-way channel of the chat service Northwind Chat
(``tests/plugins/fake_northwind.py`` stands in for it), written against the API of core plugins alone
(``evo_agents.hub.server.core_plugin_api``), as a plugin of another repository would be.

Its entry point names ``manifest``, which asks for the version of the API it was written for: a hub that serves
another leaves the plugin out with that reason. ``server`` holds its channel, its routes and its events, and the bridge
of a group of Northwind Chat to a group chat of the hub (API 1.5); its agent type helpdesk answers the questions a chat
user asks with ``/ask``.
"""

from evo_agents.hub.server import core_plugin_api as hub

API = (1, 6)  # the version of the API this plugin is written for
SERVER = "northwind_chat.server"
KIND = "northwind"

MANIFEST = {
    "id": "northwind-chat",
    "version": "1.0.0",
    "publisher": "northwind",
    "tier": "core",
    "name": "Northwind Chat",
    "description": "Decisions, proposals and direct chats with agents in Northwind Chat.",
    "engines": {"evo-agents": ">=0.23.1"},
    "plugin_api": 1,
    "permissions": ["chats:read", "chats:write"],
    "config": {
        "type": "object",
        "properties": {
            "api_url": {
                "type": "string",
                "title": "Northwind Chat's API",
                "default": "https://chat.northwind.test/api",
            },
            "token": {"type": "string", "title": "The bot's token", "x-secret": True},
            "webhook_secret": {"type": "string", "title": "The secret of the webhook", "x-secret": True},
            "clearance": {
                "type": "object",
                "title": "What the channel may carry",
                "properties": {"level": {"type": "string"}, "location": {"type": "string"}},
                "required": ["level"],
                "default": {"level": "internal"},
            },
        },
    },
    "contributes": {
        "channels": [{"name": KIND, "title": "Northwind Chat", "python": f"{SERVER}:NorthwindChannel"}],
        "routes": [{"name": "hooks", "title": "Linking and the webhook", "python": f"{SERVER}:router", "prefix": ""}],
        "events": [
            {"name": "replies", "on": ["chat.message"], "python": f"{SERVER}:on_chat_message"},
            {"name": "answers", "on": ["agent.answer"], "python": f"{SERVER}:on_agent_answer"},
            {"name": "bridge", "on": ["chat.bridge"], "python": f"{SERVER}:on_chat_bridge"},
        ],
        "agent_types": [
            {
                "name": "helpdesk",
                "title": "Help desk",
                "description": "Answers the questions of Northwind Chat's users about a project",
                "profile": {
                    "handle": "helpdesk",
                    "name": "Help desk",
                    "description": "Answers a user's question from the project's repos and the hub",
                    "instructions": "Answer in a few plain sentences.",
                },
                "events": ["answers"],
            }
        ],
    },
}


def manifest() -> dict:
    """The manifest, once this hub serves the version of the API the plugin is written for."""
    hub.require(*API)
    return MANIFEST
