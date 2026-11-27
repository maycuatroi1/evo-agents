"""The events of a group chat as its members read them (``chats.service.events``): their kinds and pages."""

from __future__ import annotations

from evo_agents.hub import chats

STREAM_BATCH = 200  # events a stream reads at a time
MAX_EVENTS_PAGE = 500
MESSAGE_EVENTS = ("message", chats.MESSAGE_CHANGED)  # each carries its message as it reads now
REACTION_EVENTS = ("reaction_added", "reaction_removed")
MEMBER_EVENTS = ("member_added", "member_removed")
STREAM_TOKEN_ENDED = "the run's token that opened this stream is revoked or its run is no longer held"
PROGRESS_PARAM = "also the progress of the agents' runs on their way under the messages they took (ChatProgress)"
