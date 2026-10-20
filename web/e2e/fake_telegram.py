"""A fake Telegram Bot API for the Playwright stack (``hub_stack.py``): what the hub's bot calls, recorded, and answered
as Telegram would, over HTTP on a free port (``serve``), which EVO_HUB_TELEGRAM_API_URL points the bot at. Nothing here
reaches Telegram.

The bot is the core plugin telegram of evo-agents-core-plugins, which deploy/hub/core-plugins.txt pins; its Python tests
and their fake, of which this is a copy without what only they use, are in that repository (``plugin_tests``).

The bot token and the webhook secret are built from pieces, so no scanner reads them as real ones.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOT = "evo_test_bot"
TOKEN = "".join(("1234567", "89:", "AAfake", "-test-", "token", "-for-the-hub"))  # <bot id>:<secret>, a fake
SECRET = "".join(("webhook", "-secret-", "for-tests"))
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"


@dataclass
class FakeTelegram:
    calls: list = field(default_factory=list)  # (method, payload), in order
    webhook: dict = field(default_factory=lambda: {"url": "", "pending_update_count": 0})
    message_id: int = 1000
    token: str = TOKEN
    lock: threading.Lock = field(default_factory=threading.Lock)

    def answer(self, path: str, payload: dict) -> tuple[int, dict]:
        prefix = f"/bot{self.token}/"
        if not path.startswith(prefix):
            return 404, {"ok": False, "error_code": 404, "description": "Not Found"}
        method = path[len(prefix) :]
        with self.lock:
            self.calls.append((method, payload))
            if method == "getMe":
                return 200, ok({"id": 123456789, "is_bot": True, "first_name": "evo hub", "username": BOT})
            if method == "sendMessage":
                self.message_id += 1
                payload["message_id"] = self.message_id  # what the test presses a button of, or replies to
                return 200, ok({"message_id": self.message_id, "chat": {"id": payload.get("chat_id")}})
            if method in ("answerCallbackQuery", "editMessageText"):
                return 200, ok(True)
            if method == "getWebhookInfo":
                return 200, ok(dict(self.webhook))
            if method == "setWebhook":
                self.webhook = {"url": payload.get("url", ""), "pending_update_count": 0}
                return 200, ok(True)
        return 404, {"ok": False, "error_code": 404, "description": "Not Found: method not found"}

    def sent(self, method: str = "sendMessage") -> list[dict]:
        with self.lock:
            return [payload for name, payload in self.calls if name == method]

    def serve(self, port: int = 0) -> ThreadingHTTPServer:
        """This fake over HTTP on 127.0.0.1:``port`` (a free one by default), in a thread; shut it down when done."""
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # the name http.server calls
                length = int(self.headers.get("content-length") or 0)
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    payload = {}
                status, body = fake.answer(self.path, payload)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server


def ok(result) -> dict:
    return {"ok": True, "result": result}


def private_chat(chat_id: int, username: str | None = "owner_tg") -> tuple[dict, dict]:
    """(chat, from) of a member's private chat with the bot: in Telegram they share the id."""
    sender = {"id": chat_id, "is_bot": False, "first_name": "Owner"}
    if username:
        sender["username"] = username
    return {"id": chat_id, "type": "private"}, sender


def text_update(update_id: int, chat: dict, sender: dict, text: str, *, reply_to: int | None = None) -> dict:
    message = {"message_id": update_id + 50_000, "date": 1_760_000_000, "chat": chat, "from": sender, "text": text}
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to, "chat": chat, "date": 1_760_000_000, "text": "..."}
    return {"update_id": update_id, "message": message}
