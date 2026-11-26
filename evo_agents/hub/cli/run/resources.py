"""The routes of the hub ``evo-agents hub run`` asks, one gateway each (``evo_agents.hub.cli.base.ProjectResource``).

- :class:`Runs`: GET and POST /v1/projects/{p}/runs, and every route below a run (its events and stream, chat,
  leases, candidates, outputs, messages, access, priority and the owner's controls).
- :class:`PlanRuns` and :class:`AuthorRuns`: POST /v1/projects/{p}/plan-runs and /v1/projects/{p}/author-runs.
- :class:`SystemDispatch`: POST /v1/projects/{p}/plans/{id}/system-dispatch, a plan handed to an agent system.
- :class:`Decisions`: the open decisions of the project, as ``hub run watch`` looks at them.

A query that repeats a key (``state``, ``kind``, ``source``) is given as pairs, in the order the hub gets them.
Standard library only, like the rest of the client.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlencode

from evo_agents.hub.cli.base import ProjectResource


class Runs(ProjectResource):
    """The runs of a project and the routes below one run."""

    collection = "runs"

    def path_with(self, *parts: Any, pairs: Iterable[tuple[str, Any]] = ()) -> str:
        """The path of ``parts`` with a query of ``pairs``, a key given as often as it comes."""
        query = urlencode(list(pairs))
        return self.path(*parts) + (f"?{query}" if query else "")

    def read(self, *parts: Any, pairs: Iterable[tuple[str, Any]] = ()) -> Any:
        """GET the path of ``parts`` with a query of ``pairs``."""
        return self.hub.call("GET", self.path_with(*parts, pairs=pairs))

    def list(self, pairs: Iterable[tuple[str, Any]]) -> dict:
        return self.read(pairs=pairs)

    def dispatch(self, body: dict) -> list[dict]:
        return self.call("POST", body=body)

    def get(self, run_id: int) -> dict:
        return self.call("GET", run_id)

    def events(self, run_id: int, pairs: Iterable[tuple[str, Any]]) -> dict:
        return self.read(run_id, "events", pairs=pairs)

    def stream_path(self, run_id: int, after: int | None = None) -> str:
        """The path of the run's server-sent events, from after event ``after`` when given."""
        return self.path(run_id, "stream", after=after)

    def chat(self, run_id: int) -> dict:
        return self.call("GET", run_id, "chat")

    def credentials(self, run_id: int) -> list[dict]:
        return self.call("GET", run_id, "credentials")

    def candidates(self, run_id: int) -> dict:
        return self.call("GET", run_id, "rerun-candidates")

    def outputs(self, run_id: int, sources: Iterable[str] = ()) -> dict:
        return self.read(run_id, "outputs", pairs=(("source", s) for s in sources))

    def output_link(self, run_id: int, output_id: int) -> dict:
        return self.call("GET", run_id, "outputs", output_id, "link")

    def send(self, run_id: int, body: dict) -> dict:
        return self.call("POST", run_id, "messages", body=body)

    def access(self, run_id: int, as_me: bool) -> dict:
        return self.call("PUT", run_id, "access", body={"as_me": as_me})

    def priority(self, run_id: int, level: str) -> dict:
        return self.call("POST", run_id, "priority", body={"priority": level})

    def control(self, run_id: int, action: str, body: dict | None = None) -> dict:
        """One of the owner's controls: cancel, approve, takeover, handback, rerun or finish."""
        return self.call("POST", run_id, action, body=body)


class PlanRuns(ProjectResource):
    """POST /v1/projects/{p}/plan-runs: one run that does every step of a plan not done yet."""

    collection = "plan-runs"

    def queue(self, body: dict) -> dict:
        return self.call("POST", body=body)


class AuthorRuns(ProjectResource):
    """POST /v1/projects/{p}/author-runs: a plan written, or revised, from a request."""

    collection = "author-runs"

    def queue(self, body: dict) -> dict:
        return self.call("POST", body=body)


class SystemDispatch(ProjectResource):
    """POST /v1/projects/{p}/plans/{id}/system-dispatch: a plan handed to the entry agent of an agent system."""

    collection = "plans"

    def hand(self, plan_id: str, system: str) -> dict:
        return self.call("POST", plan_id, "system-dispatch", body={"system": system})


class Decisions(ProjectResource):
    """GET /v1/projects/{p}/decisions, as ``hub run watch`` reads it."""

    collection = "decisions"

    def open(self, limit: int) -> dict:
        return self.call("GET", state="open", limit=limit)
