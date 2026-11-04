"""``evo-agents hub kg entities --project P``: answer, inside a run of P's agent pool, the items P's latest hub build
left to a machine that has Claude Code (``evo_agents.hub.kg_entity_relay``).

It reads the requests (GET /v1/kg/P/entities/requests), asks Claude Code for each one on this machine's login
(``evo_agents.kg.claude_code.answer``: in a run, the lease of the pool's account; API keys are taken out of its
environment), and posts the answers in batches (POST /v1/kg/P/entities/answers), which the hub keeps and follows with
a build. The hub takes answers from a run's token only. A login refused or a limit reached stops it at that item:
what it has is posted, the rest waits for the next run, and it exits 3. ``--limit`` caps the calls of one run. It
prints counts, never a text or a credential.
"""

from __future__ import annotations

import sys
from urllib.parse import quote

from evo_agents.hub.cli_client import client_command, print_json, signed_in
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.kg import claude_code

BATCH = 20  # answers in one post
STOPPED_EXIT = 3


def _path(project: str, tail: str) -> str:
    return f"/v1/kg/{quote(project, safe='')}/entities/{tail}"


def done_json(project: str, requests: int = 0) -> dict:
    """What ``--json`` prints, before any answer."""
    return {"project": project, "requests": requests, "answered": 0, "kept": 0, "failed": 0, "stopped": None}


def answer_all(hub, project: str, limit: int | None = None, answer=claude_code.answer) -> dict:
    """Answer the latest requests of ``project`` and post the answers: the counts and why it stopped, if it did."""
    found = hub.call("GET", _path(project, "requests"))
    requests = (found or {}).get("requests") or []
    done = done_json(project, len(requests))
    batch: dict[str, dict] = {}

    def post() -> None:
        if batch:
            kept = hub.call("POST", _path(project, "answers"), {"answers": dict(batch)})
            done["kept"] += int((kept or {}).get("kept") or 0)
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


@client_command
def cmd_entities(args) -> int:
    hub, _ = signed_in()
    if not claude_code.available():
        print("error: claude is not installed here: run this inside a run of the project's agent pool", file=sys.stderr)
        return 1
    done = answer_all(hub, args.project, args.limit)
    if args.json:
        print_json(done)
    else:
        print(
            f"{done['answered']} of {done['requests']} entity request(s) of {args.project} answered, "
            f"{done['kept']} kept by the hub, {done['failed']} failed"
            + (f"; stopped: {done['stopped']}, the rest waits for the next run" if done["stopped"] else "")
        )
    return STOPPED_EXIT if done["stopped"] else 0


def register(ksub) -> None:
    from evo_agents.hub.cli import positive

    p = ksub.add_parser(
        "entities",
        help="answer with Claude Code, in a run of the project's agent pool, the entity requests of its latest build",
    )
    p.add_argument("--project", required=True, help="project name on the hub")
    p.add_argument("--limit", type=positive, help="calls of Claude Code at most (default: every request)")
    json_option(p, returns_object("project", "requests", "answered", "kept", "failed", "stopped"))
    p.set_defaults(func=cmd_entities)
