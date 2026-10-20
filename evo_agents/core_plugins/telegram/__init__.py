"""The core plugin telegram: the hub's Telegram channel of notifications, which members link from the web, with the
webhook that takes their answers and the routes of linking and of the hub admins (``docs/notifications.md``).

It left the hub's core as it was: the same variables (EVO_HUB_TELEGRAM_*), routes and messages. Its channel is a
kind of members' channel (``telegram``); its routes keep the paths they had (the empty prefix: each router carries its
own under /v1); its link codes are the rows of kind telegram of the hub's channel_link_codes (migrations 0015 and
0063), which it owns no more than any other core plugin does. Nothing of it is per project, so it runs hub-wide
wherever evo-ak is installed: the hub's store lists it as a core plugin installed with the image, and no project
enables it. The model it shares with the chats (what may reach a chat, the minimal message) stays in
``evo_agents.hub.telegram``.
"""

SERVER = "evo_agents.core_plugins.telegram.server"

MANIFEST = {
    "id": "telegram",
    "version": "1.0.0",
    "publisher": "evo-agents",
    "tier": "core",
    "name": "Telegram",
    "description": "The hub's Telegram channel: members link a chat from the web and answer decisions and proposals.",
    "engines": {"evo-agents": ">=0.16"},
    "plugin_api": 1,
    "contributes": {
        "channels": [{"name": "telegram", "title": "Telegram", "python": f"{SERVER}:TelegramChannel"}],
        "routes": [
            {"name": "me", "title": "A member's link", "python": f"{SERVER}:me_router", "prefix": ""},
            {"name": "webhook", "title": "Telegram's webhook", "python": f"{SERVER}:webhook_router", "prefix": ""},
            {"name": "admin", "title": "The bot and members' links", "python": f"{SERVER}:admin_router", "prefix": ""},
        ],
    },
}
