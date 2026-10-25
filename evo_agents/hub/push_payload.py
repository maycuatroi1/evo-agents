"""What a push notification carries, the same for every sender (Web Push, APNs, FCM): a JSON object of a title, a body,
the absolute URL of the hub page it opens, a tag (one per notification, so a later push of it replaces the earlier
one), its kind and its project. Pure functions on the standard library.

A notification of a project the hub sink's clearance keeps from Telegram (``extra.restricted``, as
``member_channels.prepare`` reads it for the clearance Telegram has), or of a member who chose to hide details
(``extra.hide_details``), carries the kind of thing alone as its title ("Decision needed", "Run failed"), the
project's name as its body, and the link: nothing of what it is about. Any other carries its title and, for a
decision, its question and the label of the option its agent recommends; for anything else, the kind of thing and
the project. No push carries a decision's context, a file or its link, an image, a token or a code: none of them is
read here. The words are English, as the hub's web is.

A payload is at most MAX_PAYLOAD_BYTES of UTF-8 JSON, under the 4 KB Web Push, APNs and FCM each take with room for
a sender's own keys: the body is cut first, then the title.
"""

from __future__ import annotations

import json

from evo_agents.hub import telegram

MAX_PAYLOAD_BYTES = 3072
BODY_CHARS = 300  # of a decision's question in a push's body
KIND_WORDS = {"decision": "Decision needed", "proposal": "Proposal to review"}
NOTICE = "Notice"
FALLBACK_LINK = "/inbox"
TEST_KIND = "test"
TEST_TITLE = "Test notification"
TEST_BODY = "Push notifications from the hub reach this device."
TEST_LINK = "/settings/notifications"
ELLIPSIS = "..."


def kind_words(kind: str, notice_kind: str | None) -> str:
    """The kind of thing a notification is, in a few words: "Decision needed", "Run failed"..."""
    if kind in KIND_WORDS:
        return KIND_WORDS[kind]
    return telegram.NOTICE_WORDS.get(notice_kind or "", NOTICE)


def tag(notification_id: int) -> str:
    return f"evo-notification-{notification_id}"


def _clip(text: str, limit: int) -> str:
    """``text`` as at most ``limit`` bytes of UTF-8, ending with "..." when it was longer."""
    if len(text.encode()) <= limit:
        return text
    room = max(0, limit - len(ELLIPSIS))
    return text.encode()[:room].decode(errors="ignore").rstrip() + ELLIPSIS


def size(payload: dict) -> int:
    """The bytes of ``payload`` as the senders write it: compact JSON in UTF-8."""
    return len(encoded(payload))


def encoded(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


def fit(payload: dict, limit: int = MAX_PAYLOAD_BYTES) -> dict:
    """``payload`` within ``limit`` bytes: its body cut first, then its title."""
    fitted = dict(payload)
    for key in ("body", "title"):
        over = size(fitted) - limit
        if over > 0 and fitted.get(key):
            fitted[key] = _clip(fitted[key], max(0, len(fitted[key].encode()) - over))
    return fitted


def _recommended(options) -> str | None:
    for option in options or []:
        if isinstance(option, dict) and option.get("recommended") and isinstance(option.get("label"), str):
            return option["label"]
    return None


def _details(kind: str, words: str, project: str | None, decision) -> str:
    if kind == "decision" and isinstance(decision, dict) and isinstance(decision.get("question"), str):
        body = _clip(" ".join(decision["question"].split()), BODY_CHARS)
        label = _recommended(decision.get("options"))
        return body + (f"\nRecommended: {label}" if label else "")
    return f"{words} in {project}" if project else words


def payload(notification, public_url: str | None) -> dict:
    """The push of ``notification`` (an ``Outgoing`` whose ``extra`` a sender's prepare filled: ``restricted``,
    ``hide_details`` and ``decision``), linking to the hub at ``public_url``. It fails closed: a notification whose
    extra does not say it is unrestricted carries nothing of what it is about."""
    extra = notification.extra or {}
    words = kind_words(notification.kind, notification.notice_kind)
    project = notification.project
    link = notification.link or FALLBACK_LINK
    shown = {
        "title": words,
        "body": project,
        "url": telegram.web_url(public_url, link) or link,
        "tag": tag(notification.id),
        "kind": notification.kind,
        "project": project,
    }
    if extra.get("restricted") is False and not extra.get("hide_details"):
        shown["title"] = notification.title or words
        shown["body"] = _details(notification.kind, words, project, extra.get("decision"))
    return fit(shown)


def trial_payload(public_url: str | None) -> dict:
    """The push ``POST /v1/me/push/devices/{id}/test`` sends: it says push works, and opens the settings."""
    shown = {
        "title": TEST_TITLE,
        "body": TEST_BODY,
        "url": telegram.web_url(public_url, TEST_LINK) or TEST_LINK,
        "tag": "evo-test",
        "kind": TEST_KIND,
        "project": None,
    }
    return fit(shown)
