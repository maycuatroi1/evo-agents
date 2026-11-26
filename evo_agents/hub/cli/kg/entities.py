"""``evo-agents hub kg entities --project P``: answer, inside a run of P's agent pool, the items P's latest hub build
left to a machine that has Claude Code (``evo_agents.hub.kg_entity_relay``).

It reads the requests (GET /v1/kg/P/entities/requests), asks Claude Code for each one on this machine's login
(``evo_agents.kg.claude_code.answer``: in a run, the lease of the pool's account, asked of the run's socket once when
the agent's shell lacks it; API keys are taken out of its environment), and posts the answers in batches (POST /v1/kg/P/entities/answers), which the hub keeps and follows with
a build. The hub takes answers from a run's token only. A login refused or a limit reached stops it at that item:
what it has is posted, the rest waits for the next run, and it exits 3. ``--limit`` caps the calls of one run. It
prints counts, never a text or a credential.

:data:`ENTITIES` is a class command of ``evo_agents.clikit``; :class:`KgEntities` is the gateway to its routes.
"""

from __future__ import annotations

import os
import sys

from evo_agents.clikit import Arg, Result, Text, returns_object
from evo_agents.hub.cli import positive
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.cli.kg.resources import KG_PROJECT, KgResource
from evo_agents.kg import claude_code

BATCH = 20  # answers in one post
STOPPED_EXIT = 3
NO_CLAUDE = "claude is not installed here: run this inside a run of the project's agent pool"


class KgEntities(KgResource):
    """The entity requests of a project's latest build: GET /v1/kg/{p}/entities/requests, POST .../entities/answers."""

    collection = "entities"

    def requests(self) -> list[dict]:
        return (self.call("GET", "requests") or {}).get("requests") or []

    def answers(self, answers: dict[str, dict]) -> int:
        """Post ``answers``; how many the hub kept."""
        return int((self.call("POST", "answers", body={"answers": answers}) or {}).get("kept") or 0)


def done_json(project: str, requests: int = 0) -> dict:
    """What ``--json`` prints, before any answer."""
    return {"project": project, "requests": requests, "answered": 0, "kept": 0, "failed": 0, "stopped": None}


def answer_all(hub, project: str, limit: int | None = None, answer=claude_code.answer) -> dict:
    """Answer the latest requests of ``project`` and post the answers: the counts and why it stopped, if it did."""
    entities = KgEntities(hub, project)
    requests = entities.requests()
    done = done_json(project, len(requests))
    batch: dict[str, dict] = {}

    def post() -> None:
        if batch:
            done["kept"] += entities.answers(dict(batch))
            batch.clear()

    for request in requests:
        if limit is not None and done["answered"] + done["failed"] >= limit:
            break
        try:
            got = answer(request)
        except claude_code.ClaudeStop as stop:
            done["stopped"] = stop.kind
            break
        except claude_code.ClaudeError:
            done["failed"] += 1
            continue
        batch[request["key"]] = got
        done["answered"] += 1
        if len(batch) >= BATCH:
            post()
    post()
    return done


class NoClaude(Exception):
    """Claude Code is not on this machine: ``error: ...`` on stderr and exit status 1."""


class KgEntitiesCommand(HubCommand):
    name = "entities"
    help = "answer with Claude Code, in a run of the project's agent pool, the entity requests of its latest build"
    arguments = (
        KG_PROJECT,
        Arg("--limit", type=positive, help="calls of Claude Code at most (default: every request)"),
    )
    output = returns_object("project", "requests", "answered", "kept", "failed", "stopped")

    def run(self, ctx: HubContext) -> Result:
        hub = ctx.hub
        if not claude_code.available():
            raise NoClaude(NO_CLAUDE)
        env = claude_code.with_run_login(os.environ)  # one ask of the run's socket, not one a request
        done = answer_all(hub, ctx.project, ctx.args.limit, answer=lambda request: claude_code.answer(request, env=env))
        line = (
            f"{done['answered']} of {done['requests']} entity request(s) of {ctx.project} answered, "
            f"{done['kept']} kept by the hub, {done['failed']} failed"
            + (f"; stopped: {done['stopped']}, the rest waits for the next run" if done["stopped"] else "")
        )
        return Result(done, Text(line), STOPPED_EXIT if done["stopped"] else 0)

    def failed(self, exc: Exception) -> int | None:
        if not isinstance(exc, NoClaude):
            return super().failed(exc)
        print(f"error: {exc}", file=sys.stderr)
        return 1


ENTITIES = KgEntitiesCommand()
NODES = (ENTITIES,)
