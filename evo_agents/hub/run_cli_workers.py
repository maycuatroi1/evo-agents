"""The worker ``evo-agents hub run`` commands name with ``--worker``, for ``evo_agents.hub.run_cli``: an id, a name or
``owner/name``, among the caller's workers and those other members share with them (``docs/workers.md``, Shared
workers). Standard library only, like the rest of the client."""

from __future__ import annotations

from evo_agents.hub.client import HubError


def worker_id(hub, login: str, worker: str) -> int:
    """The id ``worker`` names: an id as given; ``owner/name``, a worker of that owner's, the caller's own or one shared
    with them; else the caller's own worker of that name, then the one worker of that name another member shares with
    them (several: name it ``owner/name``)."""
    if worker.isascii() and worker.isdigit():
        return int(worker)
    rows = [row for row in hub.call("GET", "/v1/workers") if row.get("revoked_at") is None]
    owner, slash, name = worker.rpartition("/")
    if slash:
        found = [row["id"] for row in rows if row["name"] == name and row["owner"] == owner]
    else:
        found = [row["id"] for row in rows if row["name"] == worker and row["owner"] == login]
        found = found or [row["id"] for row in rows if row["name"] == worker and row.get("shared")]
    if len(found) == 1:
        return found[0]
    if found:
        owners = ", ".join(f"{row['owner']}/{worker}" for row in rows if row["name"] == worker and row.get("shared"))
        raise HubError(f"several members share a worker named {worker!r}: name one as owner/name ({owners})")
    raise HubError(
        f"you have no worker named {worker!r}, and no member shares one by that name with you (revoked ones aside); "
        "the Workers page of the hub lists yours and those shared with you"
    )
