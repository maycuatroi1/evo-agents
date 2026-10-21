"""What the hub keeps of the Telegram channel's model for itself: the one-time link codes of members' channels
(``evo_agents.hub.server.member_channels``, schema 0063), what may reach a channel outside the hub (``restricted``,
``restricted_for``), the words of a minimal message (``what_waits``), and the absolute URL of a page a notification
links to (``web_url``). Pure functions on the standard library.

The rest of the model, the bot's commands, the data of its buttons and the messages it sends, is the core plugin
telegram's (``evo_core_plugins.telegram.model`` of the distribution evo-agents-core-plugins, which
deploy/hub/core-plugins.txt pins), which reaches the hub through the API of core plugins
(``evo_agents.hub.server.core_plugin_api``) alone; ``docs/notifications.md`` has the design.

A link code is LINK_CODE_CHARS characters of ``A-Z``, ``a-z``, ``0-9``, ``_`` and ``-`` (what a Telegram start
parameter allows), which lives LINK_SECONDS; the hub keeps only its SHA-256 (``code_hash``).

A project is restricted when its hub sink is cleared at or above the level ``customer`` or the location
``domestic-only`` on the project's own ladder, as ``evo_agents.kg.policy`` reads a clearance (``restricted``), and it
fails closed: a level or location the ladder lacks, a clearance without a level, a ladder without the rung, or no hub
sink at all is restricted too. A restricted project gets messages with its name, the kind of thing that waits and the
link, nothing else (``what_waits`` names the kind). ``restricted_for`` reads a channel of any clearance the same way.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Mapping, Sequence

LINK_SECONDS = 600  # a link code lives 10 minutes
LINK_CODE_CHARS = 32
LINK_CODE = re.compile(r"[A-Za-z0-9_-]{32}")
RESTRICTED_LEVEL = "customer"  # a hub sink cleared this high or higher, or for this location or a stricter one,
RESTRICTED_LOCATION = "domestic-only"  # gets minimal messages

NOTICE_WORDS = {  # as the web's Inbox names them
    "push_default_branch": "Pushed to a default branch",
    "merge_default_branch": "Merged into a default branch",
    "plan_finished": "Plan finished",
    "run_failed": "Run failed",
    "curator_brief": "Morning brief",
    "curator_paused": "Night shift paused",
    "author_waiting": "An author run waits for your reply",
    "builder_proposal": "A change of @builder waits for your approval",
    "merge_paused": "Merge queue paused",
    "release": "Daily release",
}


def new_code() -> str:
    """A link code: 32 characters of base64url, 192 random bits."""
    return secrets.token_urlsafe(24)


def code_hash(code: str) -> str:
    """What the hub keeps of a link code: its SHA-256 in hex."""
    return hashlib.sha256(code.encode()).hexdigest()


def _rank(ladder: Sequence[str], name) -> int | None:
    return ladder.index(name) if isinstance(name, str) and name in ladder else None


def _mark(ladder: Sequence[str], name: str) -> int:
    """Where ``name`` sits on ``ladder``; on a ladder without it, the rung above the lowest (fail closed)."""
    found = _rank(ladder, name)
    return 1 if found is None else found


def restricted(clearance, levels: Sequence[str] | None, locations: Sequence[str] | None) -> bool:
    """Whether a project whose hub sink has ``clearance``, on the project's ladder of ``levels`` and ``locations``,
    gets minimal messages: cleared at or above the level customer or the location domestic-only, read as
    ``evo_agents.kg.policy`` reads a clearance (one without a location may receive any, so it sits at the top of the
    locations). It fails closed: no hub sink, no level, a level or location the ladder lacks, or a ladder without
    customer (or domestic-only) and a clearance above its lowest rung is restricted."""
    return restricted_for(clearance, levels, locations, RESTRICTED_LEVEL, RESTRICTED_LOCATION, below=True)


def _restricted_below(
    clearance: Mapping, levels: list[str], locations: list[str], level: str, location: str | None
) -> bool:
    """``restricted_for`` with ``below``: the sink must sit under ``level`` and ``location``, not at them; on a ladder
    without the rung, under the rung above its lowest."""
    sink_level = _rank(levels, clearance.get("level"))
    if sink_level is None or sink_level >= _mark(levels, level):
        return True
    named = clearance.get("location")
    sink_location = len(locations) - 1 if named is None else _rank(locations, named)
    if sink_location is None:
        return True
    return location is not None and sink_location >= _mark(locations, location)


def restricted_for(
    clearance,
    levels: Sequence[str] | None,
    locations: Sequence[str] | None,
    level: str,
    location: str | None,
    *,
    below: bool = False,
) -> bool:
    """Whether a project whose hub sink has ``clearance``, on the project's ladder of ``levels`` and ``locations``,
    is kept from a channel cleared for ``level`` and ``location``: its hub sink sits above the channel's clearance on
    the project's ladder, in level or in location. The ladder is read as ``restricted`` reads it, failing closed: no
    hub sink, a sink without a level, or a level or location (the sink's or the channel's) the ladder lacks is
    restricted. A location of None, the sink's or the channel's, sits at the top of the locations, as
    ``evo_agents.kg.policy`` reads a clearance that names none: the channel then receives any location.

    With ``below`` the channel takes what sits under its level and its location, not at them, and on a ladder that
    lacks its level or location, only the lowest rung there: ``restricted`` is the clearance customer, domestic-only,
    below. A channel's location of None still receives any location."""
    if not isinstance(clearance, Mapping):
        return True
    levels, locations = list(levels or []), list(locations or [])
    if below:
        return _restricted_below(clearance, levels, locations, level, location)
    sink_level, cleared_level = _rank(levels, clearance.get("level")), _rank(levels, level)
    if sink_level is None or cleared_level is None or sink_level > cleared_level:
        return True
    top = len(locations) - 1
    named = clearance.get("location")
    sink_location = top if named is None else _rank(locations, named)
    cleared_location = top if location is None else _rank(locations, location)
    return sink_location is None or cleared_location is None or sink_location > cleared_location


def web_url(public_url: str | None, link: str | None) -> str | None:
    """The absolute URL of a hub page a notification links to, when the hub knows its public URL."""
    if not public_url or not link or not link.startswith("/") or link.startswith("//"):
        return None
    return public_url.rstrip("/") + link


def what_waits(kind: str, notice_kind: str | None) -> str:
    """The kind of thing a notification is, in a few words, for a minimal message."""
    if kind == "decision":
        return "A decision waits for your answer"
    if kind == "proposal":
        return "A proposal of the Curator waits for your answer"
    return NOTICE_WORDS.get(notice_kind or "", "A notice")
