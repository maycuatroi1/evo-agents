"""A stand-in for Northwind Chat, the chat service of the sample core plugin northwind-chat
(``tests/fixtures/core_plugins/northwind_chat``): its API takes the messages the plugin sends, in memory, and the
tests build the updates the service would send the plugin's webhook. Modelled on the fake Bot API of Telegram's tests
(``web/e2e/fake_telegram.py`` keeps a copy for the Playwright stack).

``transport()`` goes into ``northwind_chat.server.TRANSPORT``. ``refuse`` makes the next sends answer that status
(429 with a retry_after, 403 for a chat that blocked the bot).
"""

from __future__ import annotations

import itertools
import json

import httpx

TOKEN = "northwind-bot-token-0123456789"
SECRET = "northwind-hook-secret-0123456789"
API_URL = "https://chat.northwind.test/api"
GROUP, USER = "g-100", "u-200"


class FakeNorthwind:
    def __init__(self):
        self.messages: list[dict] = []
        self.refuse: int | None = None
        self._ids = itertools.count(1)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._answer)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": "bad token"})
        if self.refuse is not None:
            return httpx.Response(self.refuse, json={"error": "refused", "retry_after": 3})
        body = json.loads(request.content)
        sent = {**body, "id": f"msg-{next(self._ids)}"}
        self.messages.append(sent)
        return httpx.Response(200, json={"id": sent["id"]})

    def to(self, group_id: str = GROUP, user_id: str = USER) -> list[dict]:
        return [m for m in self.messages if (m["group_id"], m["user_id"]) == (group_id, user_id)]

    def last(self, group_id: str = GROUP, user_id: str = USER) -> dict:
        return self.to(group_id, user_id)[-1]


def text(
    body: str,
    *,
    group_id: str = GROUP,
    user_id: str = USER,
    reply_to: str | None = None,
    message_id: str | None = None,
    file: dict | None = None,
) -> dict:
    """An update of a chat user's message: its id in the group when given, and a file ({name, data} in base64)."""
    found = {"group_id": group_id, "user_id": user_id, "text": body}
    extra = {"reply_to": reply_to, "message_id": message_id, "file": file}
    return {**found, **{key: value for key, value in extra.items() if value is not None}}


def button(data: str, *, group_id: str = GROUP, user_id: str = USER) -> dict:
    """An update of a chat user's press of a button."""
    return {"group_id": group_id, "user_id": user_id, "data": data}
