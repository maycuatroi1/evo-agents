"""``evo-agents worker playbook add|helpful|harmful|retire``: the deltas of the Curator's playbook of a project
(``evo_agents.hub.playbook``), which the agent of a review run writes; the playbook is never written anew.

- ``add --text TEXT --evidence SPEC [--evidence SPEC ...] [--lens LENS]``: a new entry, a lesson the playbook lacks,
  with its evidence (as ``worker finding`` takes it), for one lens or, without ``--lens``, every lens;
- ``helpful ID [--evidence SPEC ...] [--note TEXT]`` and ``harmful ID [...]``: one more on the counter of entry ID
  (``pb-3`` or ``3``) when it helped or misled the run; once each way for a run;
- ``retire ID --note TEXT``: the entry no longer holds; it stays, out of the prompt.

Each reads the run from EVO_RUN_ID and EVO_WORKER_HOME, as ``worker finding`` does, refuses outside a review run of
this worker, calls the hub with the worker's token (POST /v1/worker/runs/{id}/playbook,
``evo_agents.hub.server.playbook``) and prints one line with the entry and its counters. A failure is one ``error:``
line on stderr and exit status 1; without the worker extra, status 2. TEXT ``-`` reads stdin.
"""

from __future__ import annotations

import asyncio
import functools
import os
import sys

from evo_agents.hub import playbook
from evo_agents.hub.client import HubError
from evo_agents.worker.agent_input import evidence_items
from evo_agents.worker.home import WorkerHome, WorkerStateError
from evo_agents.worker.run_kinds import kind_refusal

EXIT_FAILED = 1
EXIT_USAGE = 2
RUN_VARIABLE = "EVO_RUN_ID"
EXTRA_HINT = "uv tool install 'evo-ak[worker]' (or python -m pip install 'evo-ak[worker]')"


class MissingExtra(Exception):
    pass


def playbook_command(func):
    """Turn the failures of ``func`` into an ``error:`` line and an exit status."""

    @functools.wraps(func)
    def run(args) -> int:
        try:
            return func(args)
        except MissingExtra as exc:
            print(f"error: evo-agents worker playbook needs the worker extra ({exc}): {EXTRA_HINT}", file=sys.stderr)
            return EXIT_USAGE
        except (HubError, WorkerStateError, playbook.PlaybookProblem) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_FAILED

    return run


def _send(op: str, body: dict) -> dict:
    """POST the delta ``body`` for the review run of EVO_RUN_ID; the hub's entry."""
    raw = (os.environ.get(RUN_VARIABLE) or "").strip()
    command = f"playbook {op}"
    if not raw.isdigit() or int(raw) < 1:
        raise WorkerStateError(
            f"`evo-agents worker {command}` works only inside a review run of this worker: {RUN_VARIABLE} is not set"
        )
    run_id, home = int(raw), WorkerHome()
    record = home.load_run(run_id)
    if record is None or record.get("finished_at"):
        raise WorkerStateError(
            f"run {run_id} is not running on this worker: `evo-agents worker {command}` works inside it"
        )
    kind = record.get("kind") or "step"
    if kind != "review":
        raise WorkerStateError(kind_refusal(run_id, kind, ("review",), command))
    try:
        from evo_agents.worker import hubapi
    except ImportError as exc:
        raise MissingExtra(f"missing module {exc.name}") from None
    config, token = home.load_config(), home.load_token()

    async def go() -> dict:
        async with hubapi.new_session() as session:
            hub = hubapi.WorkerHub(config.url, token, session)
            return await hub.call("POST", f"/v1/worker/runs/{run_id}/playbook", {"op": op, **body})

    try:
        return asyncio.run(go())
    except hubapi.HubProblem as exc:
        raise HubError(str(exc), exc.status, exc.code) from None


def _text(value: str | None, what: str, limit: int) -> str | None:
    if value is None:
        return None
    return playbook.clean_text(sys.stdin.read() if value == "-" else value, what, limit)


def _said(entry: dict, what: str) -> int:
    print(
        f"Playbook entry {entry.get('ref')} {what}: helpful {entry.get('helpful')}, harmful {entry.get('harmful')}: "
        f"{entry.get('text')}"
    )
    return 0


@playbook_command
def cmd_add(args) -> int:
    body = {"text": _text(args.text, "text", playbook.MAX_TEXT_CHARS), "evidence": evidence_items(args.evidence)}
    if args.lens:
        body["lens"] = args.lens
    return _said(_send("add", body), "added")


@playbook_command
def cmd_tag(args) -> int:
    body: dict = {"entry_id": playbook.parse_ref(args.entry), "evidence": evidence_items(args.evidence or [])}
    note = _text(args.note, "note", playbook.MAX_NOTE_CHARS)
    if note:
        body["note"] = note
    return _said(_send(args.op, body), f"counted {args.op}")


@playbook_command
def cmd_retire(args) -> int:
    body = {"entry_id": playbook.parse_ref(args.entry), "note": _text(args.note, "note", playbook.MAX_NOTE_CHARS)}
    return _said(_send("retire", body), "retired")


def register_playbook(wsub) -> None:
    """``worker playbook`` and its commands, for the agent of a review run."""
    from evo_agents.hub.review import LENSES

    parser = wsub.add_parser("playbook", help="inside a review run: change the Curator's playbook by one delta")
    psub = parser.add_subparsers(dest="playbook_command", required=True)
    spec = "session:ID[:FIELD:INDEX], transcript:ID[:FILE]:LINE, run:ID:SEQ or code:REPO:PATH[:LINE]; repeat it"
    add = psub.add_parser("add", help="a new entry: a lesson the playbook lacks, with its evidence")
    add.add_argument(
        "--text", required=True, help=f"the lesson, at most {playbook.MAX_TEXT_CHARS} characters (- for stdin)"
    )
    add.add_argument("--evidence", action="append", required=True, metavar="SPEC", help=spec)
    add.add_argument("--lens", choices=list(LENSES), help="the lens it is for (default: every lens)")
    add.set_defaults(func=cmd_add)
    for op, what in (("helpful", "helped"), ("harmful", "misled")):
        tag = psub.add_parser(op, help=f"one more on the counter of an entry that {what} this run; once a run")
        tag.add_argument("entry", metavar="ID", help="the entry, pb-N or N")
        tag.add_argument("--evidence", action="append", metavar="SPEC", help=spec)
        tag.add_argument("--note", help="why, in a line (- for stdin)")
        tag.set_defaults(func=cmd_tag, op=op)
    retire = psub.add_parser("retire", help="an entry that no longer holds: it leaves the prompt and stays listed")
    retire.add_argument("entry", metavar="ID", help="the entry, pb-N or N")
    retire.add_argument("--note", required=True, help="why it no longer holds (- for stdin)")
    retire.set_defaults(func=cmd_retire)
