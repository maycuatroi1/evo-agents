"""The Telegram channel as the hub models it: link codes, the data of inline buttons, what may reach a chat, and the
messages the hub sends. Pure functions, standard library only; ``evo_agents.hub.server.telegram`` holds the Bot API
client, the channel's class, the webhook and the routes built on them, and ``docs/notifications.md`` the design.

The hub has one bot. A member links their private chat with a one-time code of LINK_CODE_CHARS characters of
``A-Z``, ``a-z``, ``0-9``, ``_`` and ``-`` (what a start parameter allows), which lives LINK_SECONDS; the hub keeps only
its SHA-256 (``code_hash``). The link ``https://t.me/<bot>?start=<code>`` makes Telegram send the bot ``/start <code>``
from that chat (``start_code``).

A decision goes out with one button per option and one that opens it on the web, a tier 2 proposal with Accept, Reject
and Defer and the web's button. A button's ``callback_data`` names the decision or proposal and the option or answer,
never any text: ``d:<id>:<key>`` or ``p:<id>:<action>``, at most MAX_CALLBACK_BYTES (``parse_callback``). The question
of a decision goes out, its context never: the web's button leads to it, and so for a proposal's summary, its paths and
its evidence.

A project whose hub sink is cleared for the level ``customer`` or the location ``domestic-only`` (``restricted``) gets
messages with its name, the kind of thing that waits and the link, nothing else: no question, title, option, context,
diff or file name, and no answer button (``minimal_message``).

The Bot FAQ asks a bot to stay near one message a second in one chat and about 30 a second overall
(CHAT_INTERVAL_SECONDS, MESSAGES_PER_SECOND); the webhook takes at most INBOUND_UPDATES updates from one chat in
INBOUND_SECONDS and drops the rest.
"""

from __future__ import annotations

import hashlib
import html
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

LINK_SECONDS = 600  # a link code lives 10 minutes
LINK_CODE_CHARS = 32
LINK_CODE = re.compile(r"[A-Za-z0-9_-]{32}")
MAX_CALLBACK_BYTES = 64  # Telegram's bound on a button's callback_data
MAX_TEXT_CHARS = 4096  # Telegram's bound on a message's text
QUESTION_CHARS = 1200  # of a decision's question in its message
BODY_CHARS = 3000  # of a notice's body in its message
BUTTON_CHARS = 60  # of an option's label on its button
TOAST_CHARS = 200  # Telegram's bound on the text answerCallbackQuery shows
CHAT_INTERVAL_SECONDS = 1.0  # Bot FAQ: about one message a second in one chat
MESSAGES_PER_SECOND = 30  # and about 30 a second overall
INBOUND_UPDATES, INBOUND_SECONDS = 5, 10.0  # updates one chat may send the webhook in that time
RESTRICTED_LEVEL = "customer"  # a hub sink cleared this high, or for this location, gets minimal messages
RESTRICTED_LOCATION = "domestic-only"
PROPOSAL_ANSWERS = ("accept", "reject", "defer")
PARSE_MODE = "HTML"

NOTICE_WORDS = {  # as the web's Inbox names them
    "push_default_branch": "Pushed to a default branch",
    "merge_default_branch": "Merged into a default branch",
    "plan_finished": "Plan finished",
    "run_failed": "Run failed",
    "curator_brief": "Morning brief",
}
ANSWER_WORDS = {"accept": "Accept", "reject": "Reject", "defer": "Defer 7 days"}

_COMMAND = re.compile(r"^/([a-z]+)(?:@[A-Za-z0-9_]{1,64})?(?:\s+(\S+))?\s*$")
_CALLBACK = re.compile(r"^([dp]):([1-9][0-9]{0,17}):([A-Za-z0-9_-]{1,32})$")
_BOLD = re.compile(r"\*\*([^*\n]{1,200})\*\*")  # markdown's bold in a notice's body, as Telegram's HTML has it


def new_code() -> str:
    """A link code: 32 characters of base64url, 192 random bits."""
    return secrets.token_urlsafe(24)


def code_hash(code: str) -> str:
    """What the hub keeps of a link code: its SHA-256 in hex."""
    return hashlib.sha256(code.encode()).hexdigest()


def link_url(bot: str, code: str) -> str:
    return f"https://t.me/{bot}?start={code}"


def command(text: str | None) -> tuple[str, str | None] | None:
    """(name, argument) of a bot command such as ``/start CODE`` or ``/stop@hub_bot``; None for any other text."""
    found = _COMMAND.match((text or "").strip())
    return (found[1], found[2]) if found else None


def start_code(text: str | None) -> str | None:
    """The link code of ``/start <code>``, when it has the shape of one."""
    found = command(text)
    if found is None or found[0] != "start" or found[1] is None or not LINK_CODE.fullmatch(found[1]):
        return None
    return found[1]


def decision_callback(decision_id: int, key: str) -> str:
    return f"d:{decision_id}:{key}"


def proposal_callback(proposal_id: int, answer: str) -> str:
    return f"p:{proposal_id}:{answer}"


@dataclass(frozen=True)
class Callback:
    kind: str  # "decision" or "proposal"
    id: int
    value: str  # the option's key, or the answer to a proposal


def parse_callback(data) -> Callback | None:
    """The button ``data`` names, or None for anything the hub did not put on a button."""
    if not isinstance(data, str) or len(data.encode()) > MAX_CALLBACK_BYTES:
        return None
    found = _CALLBACK.match(data)
    if found is None:
        return None
    kind = "decision" if found[1] == "d" else "proposal"
    if kind == "proposal" and found[3] not in PROPOSAL_ANSWERS:
        return None
    return Callback(kind, int(found[2]), found[3])


def restricted(clearance) -> bool:
    """Whether a project whose hub sink has ``clearance`` gets minimal messages: cleared for the level customer or the
    location domestic-only, or with no hub sink at all (fail closed)."""
    if not isinstance(clearance, Mapping):
        return True
    return clearance.get("level") == RESTRICTED_LEVEL or clearance.get("location") == RESTRICTED_LOCATION


def escape(text: str) -> str:
    return html.escape(text, quote=False)


def clip(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, ending with "..." when cut."""
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def web_url(public_url: str | None, link: str | None) -> str | None:
    """The absolute URL of a hub page a notification links to, when the hub knows its public URL."""
    if not public_url or not link or not link.startswith("/") or link.startswith("//"):
        return None
    return public_url.rstrip("/") + link


@dataclass(frozen=True)
class Message:
    """A message as sendMessage takes it: HTML text and rows of inline buttons."""

    text: str
    buttons: list[list[dict]] = field(default_factory=list)

    def reply_markup(self) -> dict | None:
        return {"inline_keyboard": self.buttons} if self.buttons else None


def web_button(url: str | None) -> list[list[dict]]:
    return [[{"text": "Open on the hub", "url": url}]] if url else []


def _link_line(url: str | None, link: str | None) -> str:
    shown = url or link
    return f"\n\n{escape(shown)}" if shown else ""


def what_waits(kind: str, notice_kind: str | None) -> str:
    """The kind of thing a notification is, in a few words, for a minimal message."""
    if kind == "decision":
        return "A decision waits for your answer"
    if kind == "proposal":
        return "A proposal of the Curator waits for your answer"
    return NOTICE_WORDS.get(notice_kind or "", "A notice")


def minimal_message(
    project: str | None, kind: str, notice_kind: str | None, url: str | None, link: str | None
) -> Message:
    """The project, the kind of thing and the link: all a restricted project's message holds."""
    head = f"<b>{escape(project)}</b>: " if project else ""
    return Message(f"{head}{escape(what_waits(kind, notice_kind))}.{_link_line(url, link)}", web_button(url))


def decision_message(
    *,
    decision_id: int,
    project: str,
    run_id: int | None,
    plan_id: str | None,
    step_key: str | None,
    category: str,
    question: str,
    options: Sequence[Mapping],
    url: str | None,
    link: str | None,
) -> Message:
    """A decision: what it is about, its question, its options with the recommended one marked, a button per option
    and the web's button. Its context stays on the hub."""
    where = f"plan {plan_id}" + (f", step {step_key}" if step_key else "") if plan_id else "its plan"
    run = f"Run #{run_id}" if run_id else "A run"
    lines = [
        f"<b>{escape(run)} of {escape(project)} asks a decision</b> ({escape(category)}, {escape(where)})",
        "",
        escape(clip(question, QUESTION_CHARS)),
        "",
    ]
    buttons = []
    for option in options:
        label = str(option.get("label") or option.get("key"))
        recommended = option.get("recommended") is True
        lines.append(f"{escape(str(option['key']))}: {escape(label)}" + (" (recommended)" if recommended else ""))
        shown = clip(label, BUTTON_CHARS) + (" (recommended)" if recommended else "")
        buttons.append([{"text": shown, "callback_data": decision_callback(decision_id, str(option["key"]))}])
    lines.append("")
    lines.append("Tap an option, or reply to this message with your answer in words.")
    text = "\n".join(lines) + _link_line(url, link)
    return Message(clip(text, MAX_TEXT_CHARS), buttons + web_button(url))


def proposal_message(
    *,
    proposal_id: int,
    project: str,
    title: str,
    tier: int | None,
    kind: str | None,
    lens: str | None,
    evidence: int | None,
    url: str | None,
    link: str | None,
) -> Message:
    """A tier 2 proposal of the Curator: its title and what kind of change it is, Accept, Reject and Defer, and the
    web's button. Its summary, paths, evidence and draft plan stay on the hub."""
    facts = [f"tier {tier}" if tier is not None else None, f"kind {kind}" if kind else None]
    facts += [f"lens {lens}" if lens else None, f"{evidence} pieces of evidence" if evidence else None]
    lines = [
        f"<b>Proposal #{proposal_id} of {escape(project)}</b>",
        escape(clip(title, QUESTION_CHARS)),
        escape(", ".join(fact for fact in facts if fact)),
    ]
    answers = [
        {"text": ANSWER_WORDS[answer], "callback_data": proposal_callback(proposal_id, answer)}
        for answer in PROPOSAL_ANSWERS
    ]
    text = "\n".join(line for line in lines if line) + _link_line(url, link)
    return Message(clip(text, MAX_TEXT_CHARS), [answers, *web_button(url)])


def notice_message(
    *, project: str | None, notice_kind: str | None, title: str, body: str | None, url: str | None, link: str | None
) -> Message:
    """A notice: its kind, its title and its body as plain text (its markdown bold kept), and the web's button."""
    head = NOTICE_WORDS.get(notice_kind or "", "Notice")
    lines = [f"<b>{escape(head)}</b>" + (f" in {escape(project)}" if project else ""), escape(title)]
    if body:
        lines += ["", _BOLD.sub(r"<b>\1</b>", escape(clip(body, BODY_CHARS)))]
    text = "\n".join(lines) + _link_line(url, link)
    return Message(clip(text, MAX_TEXT_CHARS), web_button(url))


def answered_text(original: str, outcome: str) -> str:
    """The text a message is edited to once its buttons took an answer, or could not: the original, as Telegram sends
    it back without markup, then the outcome."""
    return clip(f"{escape(original)}\n\n<b>{escape(outcome)}</b>", MAX_TEXT_CHARS)


def url_buttons(markup) -> list[list[dict]]:
    """The buttons of an inline keyboard that open a URL, the ones a message keeps once it is answered."""
    rows = markup.get("inline_keyboard") if isinstance(markup, Mapping) else None
    kept = []
    for row in rows if isinstance(rows, list) else []:
        links = [button for button in row if isinstance(button, Mapping) and isinstance(button.get("url"), str)]
        if links:
            kept.append([{"text": str(button.get("text") or "Open"), "url": button["url"]} for button in links])
    return kept
