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
``{"id", "error"}`` a line). ``--json`` prints the hub's answer. The routes are those of
``evo_agents.hub.kg_routes.AnswerJobs``. Standard library only.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from evo_agents.clikit import Arg, Result, Text, returns_object
from evo_agents.hub import kg_routes
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.kg_routes import AnswerJobs

__all__ = ["KG_ANSWER", "follow", "progress_line", "read_lines", "write_answers"]

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
POLL_SECONDS = 15.0  # between two reads of the job while it runs


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


def follow(
    jobs: AnswerJobs,
    job_id: int,
    say: Callable[[str], Any],
    *,
    poll: float | None = None,
    sleep: Callable[[float], Any] | None = None,
) -> dict:
    """Read job ``job_id`` until it ends, calling ``say`` with each line that changed; its end. A read the hub did
    not answer (``client.transient``: a redeploy, a proxy's 502) is read again, not an end."""
    return kg_routes.follow(
        lambda: jobs.get(job_id),
        progress_line,
        lambda job: job["state"] != "running",
        say,
        what="the job",
        poll=POLL_SECONDS if poll is None else poll,
        sleep=sleep,
    )


def write_answers(path: Path, lines: list[dict]) -> None:
    """Write ``lines`` to ``path`` as JSON lines, whole or not at all."""
    temporary = path.with_name(f".{path.name}.part")
    with open(temporary, "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    os.replace(temporary, path)


def _job_lines(job: dict) -> list[str]:
    line = (
        f"#{job['id']} {job['state']} questions {job['questions']} answered {job['answered']} "
        f"errors {job['errors']} pending {job['pending'] + job['asking']} runs {job['runs']} {job['created_at']}"
    )
    return [line, f"  {job.get('error') or job.get('note')}"] if job.get("note") or job.get("error") else [line]


class KgAnswer(HubCommand):
    """Streams while it follows a job: the page and the job's lines are printed as they come."""

    name = "answer"
    help = "ask a file of questions of the graph and write the agents' answers"
    arguments = (
        Arg("--project", required=True, help="project name on the hub"),
        Arg("--questions", metavar="FILE", help="JSON lines, each with id and question (at most 5,000)"),
        Arg("--out", default="answers.jsonl", help="where the answers go (default: answers.jsonl)"),
        Arg("--batch", type=int, default=50, help="questions asked of one agent run (1 to 200, default 50)"),
        Arg("--no-wait", action="store_true", help="return once the hub kept the questions"),
        Arg("--job", type=int, help="the answer job: follow it and write its answers, or --reply to it"),
        Arg("--status", action="store_true", help="list the project's answer jobs"),
        Arg("--limit", type=int, default=20, help="with --status, the newest jobs listed (1 to 50)"),
        Arg(
            "--stop",
            action="store_true",
            help="with --job, stop a running job: its run is cancelled; the same file sent again asks the rest again",
        ),
        Arg(
            "--reply",
            metavar="FILE",
            help="the agent of a run: send its answers, JSON lines of id with answer or error",
        ),
    )
    output = returns_object(
        *JOB,
        *WRITTEN,
        schema="KgAnswerJob",
        added=WRITTEN,
        variants=(
            ("--status", returns_object("jobs", schema="KgAnswerJobs")),
            ("--reply", returns_object("kept", "problems", schema="KgAnswerReplied")),
            ("--stop", returns_object(*JOB, schema="KgAnswerJob")),
        ),
    )
    json_help = "machine-readable output: the job, with the file written and its lines (null with --no-wait)"

    def run(self, ctx: HubContext) -> Result:
        args = ctx.args
        jobs = AnswerJobs(ctx.hub, args.project)  # signed in first, as before
        if args.status:
            return self.status(jobs, args)
        if args.reply:
            return self.reply(jobs, args)
        if args.stop:
            return self.stop(jobs, args)
        if args.questions:
            job, ids = self.ask(jobs, args)
            if not args.json:
                asked = "sent before: asking again the questions with no answer" if job["again"] else "kept"
                line = f"answer job {job['id']} of {job['questions']} questions {asked}, batches of {job['batch']}"
                print(line, file=ctx.out)
        elif args.job:
            job, ids = jobs.get(args.job), {}
        else:
            sys.exit("give --questions FILE, --job J or --status")
        say = self.said(ctx)
        say(f"progress: {jobs.web('answers', job['id'])}")
        if args.no_wait:
            return Result({**job, "out": None, "written": None})
        job = follow(jobs, job["id"], say)
        lines = jobs.answers(job["id"], ids)
        write_answers(Path(args.out), lines)
        return Result(
            {**job, "out": args.out, "written": len(lines)},
            Text(f"wrote {len(lines)} answers to {args.out}"),
            1 if job["state"] == "failed" else 0,
        )

    @staticmethod
    def said(ctx: HubContext) -> Callable[[str], None]:
        """Where a line of progress goes: stderr under --json, else stdout as it comes."""
        if ctx.args.json:
            return lambda line: print(line, file=sys.stderr)
        return lambda line: print(line, file=ctx.out, flush=True)

    @staticmethod
    def ask(jobs: AnswerJobs, args) -> tuple[dict, dict]:
        """(the job the hub keeps for the file of --questions, {id as sent: id of the file})."""
        path = Path(args.questions)
        if not path.is_file():
            sys.exit(f"no file {args.questions}")
        questions = read_lines(path)
        ids = {str(q["id"]): q["id"] for q in questions if isinstance(q, dict) and isinstance(q.get("id"), int | str)}
        return jobs.ask(questions, args.batch), ids

    @staticmethod
    def status(jobs: AnswerJobs, args) -> Result:
        found = jobs.list(args.limit)
        if not found["jobs"]:
            return Result(found, Text(f"project {args.project} has no answer job yet"))
        return Result(found, Text(*(line for job in found["jobs"] for line in _job_lines(job))))

    @staticmethod
    def stop(jobs: AnswerJobs, args) -> Result:
        if not args.job:
            sys.exit("--stop needs --job J: the answer job to stop")
        job = jobs.stop(args.job)
        return Result(job, Text(progress_line(job)))

    @staticmethod
    def reply(jobs: AnswerJobs, args) -> Result:
        if not args.job:
            sys.exit("--reply needs --job J: the answer job your run asks")
        path = Path(args.reply)
        if not path.is_file():
            sys.exit(f"no file {args.reply}")
        found = jobs.reply(args.job, read_lines(path))
        lines = [f"kept {found['kept']} answers", *(f"  not kept: {problem}" for problem in found["problems"])]
        return Result(found, Text(*lines), 1 if found["problems"] else 0)


KG_ANSWER = KgAnswer()
