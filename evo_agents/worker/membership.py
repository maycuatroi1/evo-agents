"""This machine as a worker of a hub, and the member signed in on it: what ``worker join`` and ``worker register``
send and keep, the hub as that member (``member_hub``, ``owner_hub``), and the repos of the worker's projects as the
hub lists them (``refresh_repos``), and the hub's answers as the objects and arrays its routes send (``object_of``,
``array_of``). The commands of ``evo_agents.worker.cli`` share these; standard library only.
"""

from __future__ import annotations

import platform
from datetime import UTC, datetime

from evo_agents import __version__
from evo_agents.hub.api import paths
from evo_agents.hub.client import Hub, HubError, host_name, load_credentials
from evo_agents.worker.home import WorkerConfig, WorkerHome, WorkerStateError


def object_of(hub: Hub, method: str, path: str, body: dict | None = None) -> dict:
    """The hub's answer to ``method path``, for a route that answers a JSON object; HubError when it did not."""
    found = hub.call(method, path, body)
    if not isinstance(found, dict):
        raise HubError(f"the hub answered {method} {path} with no JSON object")
    return found


def array_of(hub: Hub, method: str, path: str) -> list:
    """The hub's answer to ``method path``, for a route that answers a JSON array; HubError when it did not."""
    found = hub.call(method, path)
    if not isinstance(found, list):
        raise HubError(f"the hub answered {method} {path} with no JSON array")
    return found


def host_facts() -> dict:
    return {
        "hostname": host_name(),
        "os": (platform.system() or "unknown").lower()[:40],
        "arch": (platform.machine() or "unknown").lower()[:40],
        "agent_version": __version__,
    }


def refuse_rejoin(home: WorkerHome, replace: bool) -> None:
    """HubError when this machine is a worker already and ``replace`` is not given."""
    if not home.joined() or replace:
        return
    try:
        config = home.load_config()
        who = f"worker {config.name} (id {config.worker_id}) of {config.url}"
    except WorkerStateError:
        who = "a worker already"
    raise HubError(
        f"this machine is {who}: revoke it first (`evo-agents worker revoke`), or pass --replace to keep the old "
        "worker on the hub and use the new one here"
    )


def config_of(url: str, answer: dict) -> tuple[WorkerConfig, str]:
    """The worker's config and token from the hub's answer to a join or a registration."""
    worker = answer.get("worker") if isinstance(answer, dict) else None
    token = answer.get("token") if isinstance(answer, dict) else None
    if not isinstance(worker, dict) or not isinstance(token, str) or not token:
        raise HubError(f"the hub at {url} did not answer with a worker and its token")
    config = WorkerConfig(
        url=url,
        worker_id=int(worker["id"]),
        name=str(worker["name"]),
        projects=list(worker.get("projects") or []),
        slots=int(worker.get("slots") or 1),
        labels=list(worker.get("labels") or []),
        allow_web_terminal=bool(worker.get("allow_web_terminal")),
        owner=worker.get("owner"),
        token_id=answer.get("token_id"),
        joined_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    return config, token


def member_hub(url: str) -> Hub | None:
    """The hub at ``url`` as the member signed in on this machine, when they are signed in to that hub."""
    try:
        credentials = load_credentials()
    except HubError:
        return None
    if credentials.url.rstrip("/") != url.rstrip("/"):
        return None
    return Hub(credentials.url, credentials.token)


def owner_hub(config: WorkerConfig) -> Hub:
    hub = member_hub(config.url)
    if hub is None:
        raise HubError(
            f"this needs the owner's machine token for {config.url}: run `evo-agents hub login --url {config.url}`, "
            "or do it on the web's Workers page"
        )
    return hub


def refresh_repos(config: WorkerConfig, hub: Hub | None = None) -> list[str]:
    """Keep the repos of the worker's projects, as the hub lists them, in ``config``; the projects it could not read.
    Needs a member of the hub signed in on this machine."""
    from evo_agents.worker.checkouts import hub_repos

    hub = hub or member_hub(config.url)
    if hub is None:
        return list(config.projects)
    missed = []
    for project in config.projects:
        try:
            answer = hub.call("GET", paths.project(project))
        except HubError:
            missed.append(project)
            continue
        if isinstance(answer, dict):
            config.repos[project] = hub_repos(answer)
    return missed


def checkout_lines(config: WorkerConfig) -> list[str]:
    """The checkouts of the worker's projects found on this machine, and the projects that have none."""
    from evo_agents.worker.checkouts import discover

    found = discover(config)
    lines = [f"  checkout {key}: {found[key]['path']}" for key in sorted(found)]
    missing = [p for p in config.projects if not any(key.startswith(f"{p}/") for key in found)]
    lines += [
        f"  no checkout of {project} found: a run of it clones the repos it needs; `evo-agents hub registry "
        "pull` finds those cloned here already"
        for project in missing
    ]
    return lines


def joined(home: WorkerHome, config: WorkerConfig, token: str, how: str) -> list[str]:
    """Keep the worker that joined (``how``: Joined, Registered on) on this machine; what it says of it."""
    missed = refresh_repos(config)
    home.save(config, token)
    projects = ", ".join(config.projects) or "no project"
    lines = [
        f"{how} {config.url} as worker {config.name} (id {config.worker_id}) for {projects}, {config.slots} slot(s).",
        f"The worker token is in {home.token_path}.",
    ]
    if missed and member_hub(config.url) is not None:
        lines.append(f"  the repos of {', '.join(missed)} could not be read from the hub; the harness registry is used")
    return [*lines, *checkout_lines(config), "Start it with `evo-agents worker run`."]
