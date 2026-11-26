"""Where the chat runs of an agent go when its agent type says so (``docs/plugins.md``, Agent types): the values of
``chat_runs`` and the member a profile's chat runs go to (``Route``); ``chats.service.route`` reads them.
"""

from __future__ import annotations

from dataclasses import dataclass

RUN_AS = "run_as"  # the type's chat runs go to the workers of its run_as
MENTIONER = "mentioner"  # the default: to the workers of the person who mentioned the agent
CHAT_RUNS = (MENTIONER, RUN_AS)  # plugin.schema.json, $defs.agent_type.chat_runs


@dataclass(frozen=True)
class Route:
    """The member whose workers take the chat runs of a profile, ``run_as`` of agent type ``agent_type``: their id and
    login."""

    agent_type: str
    user_id: int
    login: str


def chat_runs_of(manifest: dict, name: str) -> str:
    """Where the chat runs of agent type ``name`` of ``manifest`` (as stored) go: RUN_AS or MENTIONER."""
    for item in (manifest.get("contributes") or {}).get("agent_types") or ():
        if item.get("name") == name:
            return item.get("chat_runs") or MENTIONER
    return MENTIONER
