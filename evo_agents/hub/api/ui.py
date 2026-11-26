"""The URLs of the hub's plugin UI kit, which a page an agent writes loads from the hub itself."""

from __future__ import annotations

from evo_agents.hub.api.routes import route

UI_KIT = route("plugin-ui/kit/1")
UI_KIT_CSS = f"{UI_KIT}/evo-ui.css"
UI_KIT_ANSWER_JS = f"{UI_KIT}/evo-answer.js"
