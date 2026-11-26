"""The group chat a direct chat's mention opens (``chats.service.groups``): where the work went."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from evo_agents.hub import chats

CONTROL = re.compile(r"[\x00-\x1f\x7f]")
TOPIC_CHARS = 60  # of the thread's first line in the group's title


@dataclass
class Opened:
    """The group a direct chat's mention went to: its id and title (None when it took none of the agents), whether
    it was made now, the agents asked that are in it, and why each of the others is not."""

    chat_id: int | None = None
    title: str = ""
    made: bool = False
    lead: str = ""
    members: list[str] = field(default_factory=list)
    refused: dict[str, str] = field(default_factory=dict)

    def note(self) -> str:
        """The hub's message in the direct chat's thread: where the work went."""
        link = f"[{self.title}]({chats.chat_link(self.chat_id or 0)})"  # a note names a group that took an agent
        who = ", ".join(f"@{handle}" for handle in self.members)
        if self.made:
            return f"Opened the group chat {link} of you, @{self.lead} and {who}: the work goes on there."
        return f"The work goes on in the group chat {link} of this thread, with {who}."
