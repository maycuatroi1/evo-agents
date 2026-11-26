"""``evo-agents hub kg answer``: ask a file of questions of a project's graph and write what agents answered.

``answer --project P --questions Q.jsonl [--out answers.jsonl] [--batch N]`` reads one JSON object a line, each with
``id`` and ``question`` (at most 5,000), and sends them to the hub (POST /v1/kg/{project}/answers, the writer role,
``evo_agents.hub.server.kg_answers``), which asks them in batches of N (50 by default), each batch a Claude Code run
on the project's KG pool that reads the graph with the kg_* tools. It then follows the job every POLL_SECONDS,
printing a line each time its counts change and the page that shows it, and once it ends writes ``--out`` in the
file's order: each line ``id``, ``question``, ``answer``, ``evidence``, ``model``, ``input_tokens``, ``output_tokens``,
and ``error`` for a question with no answer. Exit 1 when the job failed. The same file sent again asks only the
questions no agent answered. ``--no-wait`` returns once the hub kept the file; ``--job J`` in place of ``--questions``
follows job J and writes its answers. ``--status`` lists the project's jobs: questions, answered, errors, state.
``--job J --stop`` stops a running job: its run is cancelled and its questions without an answer get an error.
``--job J --reply FILE`` is the agent's: it sends the answers of its batch (``{"id", "answer", "evidence"}`` or
``{"id", "error"}`` a line). ``--json`` prints the hub's answer. Standard library only.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import quote

from evo_agents.hub.cli_client import client_command, print_json, signed_in
from evo_agents.hub.client import Hub, HubError, transient
from evo_agents.hub.contract import json_option, returns_object

JOB = (
    "id",
    "project",
    "state",
    "questions",
    "answered",
    "errors",
    "pending",
    "asking",
    "batch",
    "runs",
    "run_id",
    "note",
    "error",
    "created_by",
    "created_at",
    "updated_at",
    "ended_at",
    "again",
)
WRITTEN = ("out", "written")  # the file written and its lines, null with --no-wait
LINE = ("id", "question", "answer", "evidence", "model", "input_tokens", "output_tokens")
POLL_SECONDS = 15.0  # between two reads of the job while it runs
PAGE = 1000  # lines of one read of the answers


def _base(project: str) -> str:
    return f"/v1/kg/{quote(project, safe='')}/answers"


def page_of(hub: Hub, project: str, job_id: int) -> str:
    return f"{hub.url}/p/{quote(project, safe='')}/kg/answers/{job_id}"


def read_lines(path: Path) -> list:
    """The JSON objects of the lines of ``path``, blank lines skipped; exits naming a line that is not JSON."""
    found = []
    with open(path, encoding="utf-8") as handle:
        for n, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                found.append(json.loads(line))
            except json.JSONDecodeError as exc:
                sys.exit(f"{path}: line {n} is not JSON: {exc.msg}")
    return found


def progress_line(job: dict) -> str:
    """The line printed for ``job``, as the hub answers it."""
    counts = (
        f"{job['answered']} answered, {job['errors']} errors, {job['asking']} asking, {job['pending']} pending "
        f"of {job['questions']}"
    )
    if job["state"] == "running":
        waits = f"; waits: {job['note']}" if job.get("note") else ""
        return f"answer job {job['id']} running: {counts}{waits}"
    if job["state"] == "failed":
        return f"answer job {job['id']} failed: {job['error'] or 'no reason given'} ({counts})"
    return f"answer job {job['id']} done: {counts}"


def follow(hub: Hub, project: str, job_id: int, say, *, poll: float | None = None, sleep=None) -> dict:
    """Read job ``job_id`` until it ends, calling ``say`` with each line that changed; its end. A read the hub did
    not answer (``client.transient``: a redeploy, a proxy's 502) is read again, not an end."""
    last = None
    while True:
        try:
            job = hub.call("GET", f"{_base(project)}/{job_id}") or {}
        except HubError as exc:
            if not transient(exc):
                raise
            line = f"the hub did not answer ({exc}); reading the job again in a moment"
            if line != last:
                say(line)
                last = line
            (sleep or time.sleep)(POLL_SECONDS if poll is None else poll)
            continue
        line = progress_line(job)
        if line != last:
            say(line)
            last = line
        if job["state"] != "running":
            return job
        (sleep or time.sleep)(POLL_SECONDS if poll is None else poll)


def answer_lines(hub: Hub, project: str, job_id: int, ids: dict | None = None) -> list[dict]:
    """The job's answers in the file's order, as answers.jsonl holds them; ``ids`` gives back the ids of the file
    (an integer id stays one)."""
    out: list[dict] = []
    offset = 0
    while True:
        found = hub.call("GET", f"{_base(project)}/{job_id}/lines?offset={offset}&limit={PAGE}") or {}
        for item in found["lines"]:
            line = {key: item[key] for key in LINE}
            line["id"] = (ids or {}).get(item["id"], item["id"])
            if item.get("error"):
                line["error"] = item["error"]
            out.append(line)
        offset += len(found["lines"])
        if not found["lines"] or offset >= found["total"]:
            return out


def write_answers(path: Path, lines: list[dict]) -> None:
    """Write ``lines`` to ``path`` as JSON lines, whole or not at all."""
    temporary = path.with_name(f".{path.name}.part")
    with open(temporary, "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def _ask(hub: Hub, args) -> tuple[dict, dict]:
    """(the job the hub keeps for the file of --questions, {id as sent: id of the file})."""
    path = Path(args.questions)
    if not path.is_file():
        sys.exit(f"no file {args.questions}")
    questions = read_lines(path)
    ids = {str(q["id"]): q["id"] for q in questions if isinstance(q, dict) and isinstance(q.get("id"), int | str)}
    job = hub.call("POST", _base(args.project), {"questions": questions, "batch": args.batch}) or {}
    return job, ids


@client_command
def cmd_answer(args) -> int:
    hub, _ = signed_in()
    if args.status:
        return _status(hub, args)
    if args.reply:
        return _reply(hub, args)
    if args.stop:
        return _stop(hub, args)
    if args.questions:
        job, ids = _ask(hub, args)
        if not args.json:
            asked = "sent before: asking again the questions with no answer" if job["again"] else "kept"
            print(f"answer job {job['id']} of {job['questions']} questions {asked}, batches of {job['batch']}")
    elif args.job:
        job, ids = hub.call("GET", f"{_base(args.project)}/{args.job}") or {}, {}
    else:
        sys.exit("give --questions FILE, --job J or --status")
    say = (lambda line: print(line, file=sys.stderr)) if args.json else (lambda line: print(line, flush=True))
    say(f"progress: {page_of(hub, args.project, job['id'])}")
    if args.no_wait:
        if args.json:
            print_json({**job, "out": None, "written": None})
        return 0
    job = follow(hub, args.project, job["id"], say)
    lines = answer_lines(hub, args.project, job["id"], ids)
    write_answers(Path(args.out), lines)
    if args.json:
        print_json({**job, "out": args.out, "written": len(lines)})
    else:
        print(f"wrote {len(lines)} answers to {args.out}")
    return 1 if job["state"] == "failed" else 0


def _status(hub: Hub, args) -> int:
    found = hub.call("GET", f"{_base(args.project)}?limit={args.limit}") or {}
    if args.json:
        print_json(found)
        return 0
    if not found["jobs"]:
        print(f"project {args.project} has no answer job yet")
        return 0
    for job in found["jobs"]:
        print(
            f"#{job['id']} {job['state']} questions {job['questions']} answered {job['answered']} "
            f"errors {job['errors']} pending {job['pending'] + job['asking']} runs {job['runs']} {job['created_at']}"
        )
        if job.get("note") or job.get("error"):
            print(f"  {job.get('error') or job.get('note')}")
    return 0


def _stop(hub: Hub, args) -> int:
    if not args.job:
        sys.exit("--stop needs --job J: the answer job to stop")
    job = hub.call("POST", f"{_base(args.project)}/{args.job}/stop") or {}
    if args.json:
        print_json(job)
    else:
        print(progress_line(job))
    return 0


def _reply(hub: Hub, args) -> int:
    if not args.job:
        sys.exit("--reply needs --job J: the answer job your run asks")
    path = Path(args.reply)
    if not path.is_file():
        sys.exit(f"no file {args.reply}")
    found = hub.call("POST", f"{_base(args.project)}/{args.job}/replies", {"answers": read_lines(path)}) or {}
    if args.json:
        print_json(found)
    else:
        print(f"kept {found['kept']} answers")
        for problem in found["problems"]:
            print(f"  not kept: {problem}")
    return 1 if found["problems"] else 0


def register(ksub) -> None:
    parser = ksub.add_parser("answer", help="ask a file of questions of the graph and write the agents' answers")
    parser.add_argument("--project", required=True, help="project name on the hub")
    parser.add_argument("--questions", metavar="FILE", help="JSON lines, each with id and question (at most 5,000)")
    parser.add_argument("--out", default="answers.jsonl", help="where the answers go (default: answers.jsonl)")
    parser.add_argument("--batch", type=int, default=50, help="questions asked of one agent run (1 to 200, default 50)")
    parser.add_argument("--no-wait", action="store_true", help="return once the hub kept the questions")
    parser.add_argument("--job", type=int, help="the answer job: follow it and write its answers, or --reply to it")
    parser.add_argument("--status", action="store_true", help="list the project's answer jobs")
    parser.add_argument("--limit", type=int, default=20, help="with --status, the newest jobs listed (1 to 50)")
    parser.add_argument(
        "--stop",
        action="store_true",
        help="with --job, stop a running job: its run is cancelled; the same file sent again asks the rest again",
    )
    parser.add_argument(
        "--reply", metavar="FILE", help="the agent of a run: send its answers, JSON lines of id with answer or error"
    )
    json_option(
        parser,
        returns_object(
            *JOB,
            *WRITTEN,
            schema="KgAnswerJob",
            added=WRITTEN,
            variants=(
                ("--status", returns_object("jobs", schema="KgAnswerJobs")),
                ("--reply", returns_object("kept", "problems", schema="KgAnswerReplied")),
                ("--stop", returns_object(*JOB, schema="KgAnswerJob")),
            ),
        ),
        help="machine-readable output: the job, with the file written and its lines (null with --no-wait)",
    )
    parser.set_defaults(func=cmd_answer)
