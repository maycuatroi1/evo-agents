"""Group chats on the hub: people and agents of projects in one conversation (``docs/hub.md``, section "Group chats").

A chat has a title, members (people by login, agents by handle) and messages. A message may answer another, which puts
it in that message's thread; a thread has one level, so ``thread_root_id`` always names the first message of it. When a
message is sent, the hub reads its mentions once (``mention_names``) and keeps them with it: ``@`` followed by a name of
``[a-z0-9-]``, not right after a letter, digit, ``@``, ``.``, ``/`` or ``-`` (so an e-mail address is no mention), and
not inside a code span or a fenced code block, ignoring case. A name resolves among the chat's members only: a
person's login, an agent's handle, or a project's name, which is the handle of that project's default agent.

A chat is of one of CHAT_KINDS, which the hub keeps with it: a group chat (GROUP), which any member adds people and
agents to, or a direct chat (DIRECT) of one person and one agent, which that person opens with the agent's handle and
whose members never change. In a direct chat each message of the person that mentions no agent is work for its agent
(``via`` DIRECT), as if it mentioned it.

A reaction is one emoji of one person or agent on one message (``emoji_problem`` says what is one); a message carries
at most MAX_EMOJI_KINDS different ones. An agent's reactions are set by rule, never by a model: STATUS_REACTIONS names
the one a run of the agent puts on the message that started it as the run moves (eyes when a worker claims it, a check
mark when it is done, a cross when it failed or was cancelled), and the check mark or the cross takes the eyes off.

Standard library only: the command line imports this module on a core install.
"""

from __future__ import annotations

import re
import unicodedata

MAX_TITLE_CHARS = 120
MAX_MESSAGE_BYTES = 16 * 1024  # of UTF-8, as an author run's chat message (``author.MAX_CHAT_BYTES``)
MAX_PEOPLE = 50  # people in one chat, its creator included
MAX_AGENTS = 10  # agents in one chat
MAX_MENTIONS = 20  # names of one message the hub reads; the ones after are text
MAX_EMOJI_KINDS = 20  # different emoji on one message
MAX_EMOJI_CHARS = 16  # code points of one emoji: a ZWJ family with skin tones fits
MAX_NAME_CHARS = 100  # of a mentioned name: a handle's bound

EYES, CHECK, CROSS = "\U0001f440", "✅", "❌"
# The reaction a run of an agent puts on the message that started it, by the state it moves to; the hub takes EYES
# off when it puts CHECK or CROSS. A run that is lost and tried again keeps its EYES.
STATUS_REACTIONS = {"leased": EYES, "done": CHECK, "failed": CROSS, "cancelled": CROSS}

GROUP, DIRECT = "group", "direct"
CHAT_KINDS = (GROUP, DIRECT)
# How a mention reached its member; thread: no @, a chat of one person; direct: no @ of the agent, a direct chat
VIA = ("login", "handle", "project", "thread", DIRECT)
EVENT_KINDS = ("message", "reaction_added", "reaction_removed", "member_added", "member_removed")
NOTICE_MENTION, NOTICE_ADDED = "chat_mention", "chat_added"  # the kinds of notice a chat sends (``runs.NOTICE_KINDS``)

_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?(?:^[ \t]*\1[ \t]*$|\Z)", re.M | re.S)
_SPAN = re.compile(r"(?<!`)(`+)(?!`).+?(?<!`)\1(?!`)", re.S)
_MENTION = re.compile(r"(?<![\w@./-])@([A-Za-z0-9][A-Za-z0-9-]{0,99})")
_KEYCAP = "⃣"
_JOINERS = {"‍", "︎", "️", _KEYCAP}  # zero width joiner, text and emoji variation selectors, keycap
_TAGS = range(0xE0020, 0xE0080)  # the tag characters of a subdivision flag
_KEYCAP_BASES = set("#*0123456789")


def _blanked(match: re.Match) -> str:
    return " " * len(match.group(0))


def mention_names(text: str) -> list[str]:
    """The names ``text`` mentions, lowered, each once, in the order they first appear, at most MAX_MENTIONS; code
    spans and fenced code blocks are left out."""
    plain = _SPAN.sub(_blanked, _FENCE.sub(_blanked, text))
    names: list[str] = []
    for found in _MENTION.finditer(plain):
        name = found.group(1).lower()
        if name not in names:
            names.append(name)
            if len(names) == MAX_MENTIONS:
                break
    return names


def _emoji_char(ch: str, keycap: bool) -> bool:
    if ch in _JOINERS or ord(ch) in _TAGS:
        return True
    if keycap and ch in _KEYCAP_BASES:
        return True
    return unicodedata.category(ch) in ("So", "Sk")


def emoji_problem(text: str) -> str | None:
    """Why ``text`` is not one emoji a reaction may be; None when it is. An emoji is a symbol (Unicode category So),
    with what joins symbols into one (zero width joiner, variation selectors, skin tones, keycaps, the tags of a
    flag): no letters, digits alone, spaces, markup or control characters."""
    if not text or len(text) > MAX_EMOJI_CHARS:
        return f"a reaction is one emoji of 1 to {MAX_EMOJI_CHARS} characters"
    keycap = _KEYCAP in text
    if not all(_emoji_char(ch, keycap) for ch in text):
        return f"{text!r} is not an emoji"
    if not any(unicodedata.category(ch) == "So" for ch in text) and not keycap:
        return f"{text!r} is not an emoji"
    return None


def status_reaction(state: str) -> tuple[str, tuple[str, ...]] | None:
    """(the emoji to put, the emoji to take off) for a run of an agent moving to ``state``; None when the state puts
    none."""
    emoji = STATUS_REACTIONS.get(state)
    if emoji is None:
        return None
    return emoji, (() if emoji == EYES else (EYES,))


def chat_link(chat_id: int, message_id: int | None = None) -> str:
    """The web page of a chat, as a notice links to it, at one message when given."""
    return f"/chats/{chat_id}" if message_id is None else f"/chats/{chat_id}?message={message_id}"


# What names a message to read it whole (the tools chat_message and chat_thread, `evo-agents worker chat read`): its
# id, or its item in the knowledge graph (``evo_agents.core_plugins.chats.kg``), or a thread's item for its thread.
MAX_ID = 2**63 - 1  # a message's id, within bigint
_ID = "[1-9][0-9]{0,18}"
READ_REF = rf"^(?:(?:plugin\.chats:message:)?{_ID}|plugin\.chats:thread:{_ID}/{_ID})$"
_READ = re.compile(rf"(?:plugin\.chats:message:)?({_ID})|plugin\.chats:thread:({_ID})/({_ID})")


def read_ref(value) -> tuple[int, int | None, bool] | None:
    """(the message ``value`` names, the chat a thread's item names or None, whether it names a thread) for an id, as
    an int or its digits, or an item of READ_REF; None for anything else, or an id past MAX_ID."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    found = _READ.fullmatch(str(value).strip())
    if found is None:
        return None
    if found.group(1):
        named = int(found.group(1)), None, False
    else:
        named = int(found.group(3)), int(found.group(2)), True
    return named if max(named[0], named[1] or 0) <= MAX_ID else None
