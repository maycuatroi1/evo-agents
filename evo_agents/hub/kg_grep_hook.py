"""``evo-agents kg hook post-search`` on this machine: what the PostToolUse hook prints after a Grep, or an rg or grep
command in Bash (``evo_agents.kg.post_search``), with the lines the hub answers for it.

The hook reads the event and maps the matched lines to (repo, path) here; this module sends them to POST
/v1/kg/{project}/grep-links of the hub this machine is signed in to (``client.load_credentials``: EVO_HUB_TOKEN and
EVO_HUB_URL, which a worker sets for a run, else the login of ``evo-agents hub login``), and gives the request what is
left of the hook's budget (``post_search.BUDGET``). It never changes the search result and never blocks the session:
no sign-in, a project the hub does not know or holds no graph of, no grant, a hub that cannot be reached and an answer
past the budget print nothing, and any other error prints one line on stderr, by type; the exit status is always 0.
It imports only the standard library, the hub client and the hook's own modules.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from urllib.parse import quote

from evo_agents.hub import client
from evo_agents.kg import post_search

QUIET = (401, 403, 404, 409)  # no token the hub takes, no grant, no such project, no graph yet: nothing to add


def ask(project: str, body: dict, deadline: float) -> list[str]:
    """The note's lines the hub answers for ``body`` (``post_search.request``) on ``project``; [] when the hub cannot
    say (see the module). Raises ``post_search.Timeout`` past ``deadline``, HubError for another refusal."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise post_search.Timeout
    try:
        credentials = client.load_credentials()
    except client.NotSignedIn:
        return []
    hub = client.Hub(credentials.url, credentials.token, timeout=left)
    try:
        answer = hub.call("POST", f"/v1/kg/{quote(project, safe='')}/grep-links", body, timeout=left)
    except client.Unreachable:
        if time.monotonic() >= deadline:
            raise post_search.Timeout from None
        return []
    except client.HubError as exc:
        if exc.status in QUIET:
            return []
        raise
    lines = answer.get("lines") if isinstance(answer, dict) else None
    return [line for line in lines if isinstance(line, str)] if isinstance(lines, list) else []


def note(stdin_text: str, only: str | None = None, sink: str = post_search.SINK, env=None) -> str | None:
    """``post_search.post_search`` with the hub as what answers it."""
    return post_search.post_search(stdin_text, ask, only=only, sink=sink, env=env)


def run(args) -> int:
    """Print the hook's answer to the event on stdin (see the module)."""
    try:
        found = note(sys.stdin.read(), only=args.only, sink=args.sink)
    except post_search.Timeout:
        print(f"evo-kg post-search hook skipped: out of time after {post_search.BUDGET:g}s", file=sys.stderr)
        return 0
    except Exception as exc:  # a hook must never break the session
        print(f"evo-kg post-search hook skipped: {type(exc).__name__}", file=sys.stderr)
        return 0
    if found:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": found}}))
    return 0


def main(argv: list[str]) -> int:
    """``evo-agents kg hook post-search`` as ``evo_agents.cli.main`` runs it, without the rest of the command tree."""
    parser = argparse.ArgumentParser(
        prog="evo-agents kg hook post-search",
        description="PostToolUse: what the graph links to a Grep, rg or grep match",
    )
    post_search.add_arguments(parser)
    return run(parser.parse_args(argv))
