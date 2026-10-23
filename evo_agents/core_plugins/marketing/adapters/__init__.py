"""The API adapters of the plugin marketing, one for each channel that has a posting API: GitHub (Releases and
Discussions), X, LinkedIn, Reddit, Facebook Page and TikTok (Content Posting API). The channels
without one post through the browser adapter, which is a queue and a script, not an adapter of this package
(``browser``): ``for_channel`` answers None for them.
"""

from __future__ import annotations

from evo_agents.core_plugins.marketing.adapters import apis
from evo_agents.core_plugins.marketing.adapters.base import (
    Adapter,
    AdapterError,
    Content,
    HttpRequest,
    HttpResponse,
    HttpTransport,
    Published,
    Transport,
)


def for_channel(channel: str, transport: Transport | None = None) -> Adapter | None:
    """The API adapter of ``channel`` on ``transport`` (the real one when none), None when it has none."""
    found = apis.ADAPTERS.get(channel)
    return found(transport or HttpTransport()) if found else None


__all__ = [
    "Adapter",
    "AdapterError",
    "Content",
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
    "Published",
    "Transport",
    "for_channel",
]
