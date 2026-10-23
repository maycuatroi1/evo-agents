"""``evo-agents worker probe add``: the probe of a merged change of the Curator that has none, or the one in place of
its probe the weekly report found not reproduced (``evo_agents.hub.probes``), which the agent of a review run writes.

``probe add --change N --repo REPO [--commit SHA] (--task TEXT | --task-file PATH) --verify LINE [--replaces PROBE]``:
the task an agent is given in REPO, from the base commit (none: the tip of the default branch), and the one-line verify
that exits 0 when the agent does not repeat the mistake; with --replaces, in place of the change's probe that the
weekly report found not reproduced two weeks in a row, which the hub retires. The task and the verify are masked as
a digest is (``evo_agents.hub.redact``) before they leave this machine.

It reads the run from EVO_RUN_ID and EVO_WORKER_HOME, as ``worker playbook`` does, refuses outside a review run of
this worker, calls the hub with the worker's token (POST /v1/worker/runs/{id}/probes,
``evo_agents.hub.server.probe_routes``) and prints one line with the probe. A failure is one ``error:`` line on stderr
and exit status 1; without the worker extra, status 2. TEXT ``-`` reads stdin.
"""

from __future__ import annotations

import asyncio
import functools
import os
import sys

from evo_agents.hub import probes
from evo_agents.hub.client import HubError
from evo_agents.hub.redact import redact
from evo_agents.worker.agent_input import read_text
from evo_agents.worker.home import WorkerHome, WorkerStateError
from evo_agents.worker.run_kinds import kind_refusal

EXIT_FAILED = 1
EXIT_USAGE = 2
RUN_VARIABLE = "EVO_RUN_ID"
EXTRA_HINT = "uv tool install 'evo-ak[worker]' (or python -m pip install 'evo-ak[worker]')"
COMMAND = "probe add"


class MissingExtra(Exception):
    pass


def probe_command(func):
    """Turn the failures of ``func`` into an ``error:`` line and an exit status."""

    @functools.wraps(func)
    def run(args) -> int:
        try:
            return func(args)
        except MissingExtra as exc:
            print(f"error: evo-agents worker probe needs the worker extra ({exc}): {EXTRA_HINT}", file=sys.stderr)
            return EXIT_USAGE
        except (HubError, WorkerStateError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_FAILED

    return run


def _send(body: dict) -> dict:
    """POST the probe ``body`` for the review run of EVO_RUN_ID; the hub's probe."""
    raw = (os.environ.get(RUN_VARIABLE) or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        raise WorkerStateError(
            f"`evo-agents worker {COMMAND}` works only inside a review run of this worker: {RUN_VARIABLE} is not set"
        )
    run_id, home = int(raw), WorkerHome()
    record = home.load_run(run_id)
    if record is None or record.get("finished_at"):
        raise WorkerStateError(
            f"run {run_id} is not running on this worker: `evo-agents worker {COMMAND}` works inside it"
        )
    kind = record.get("kind") or "step"
    if kind != "review":
        raise WorkerStateError(kind_refusal(run_id, kind, ("review",), COMMAND))
    try:
        from evo_agents.worker import hubapi
    except ImportError as exc:
        raise MissingExtra(f"missing module {exc.name}") from None
    config, token = home.load_config(), home.load_token()

    async def go() -> dict:
        async with hubapi.new_session() as session:
            hub = hubapi.WorkerHub(config.url, token, session)
            return await hub.call("POST", f"/v1/worker/runs/{run_id}/probes", body)

    try:
        return asyncio.run(go())
    except hubapi.HubProblem as exc:
        raise HubError(str(exc), exc.status, exc.code) from None


def _task(args) -> str:
    if args.task_file is not None:
        text = read_text(args.task_file, "task", probes.MAX_TASK_BYTES)
    else:
        text = sys.stdin.read() if args.task == "-" else args.task
    if not text or not text.strip():
        raise WorkerStateError("a probe's task is empty: give the task text the agent is given")
    return redact(text)


@probe_command
def cmd_add(args) -> int:
    verify = redact(args.verify.strip())
    why = probes.verify_problem(verify)
    if why is not None:
        raise WorkerStateError(f"the probe's verify {why}")
    body = {"change_id": args.change, "repo": args.repo, "task": _task(args), "verify": verify}
    if args.commit:
        body["commit"] = args.commit
    if args.replaces is not None:
        body["replaces"] = args.replaces
    probe = _send(body)
    replaced = f", in place of probe #{args.replaces}" if args.replaces is not None else ""
    print(f"Probe #{probe.get('id')} of change #{probe.get('change_id')} recorded, in {probe.get('repo')}{replaced}.")
    return 0


def register_probe(wsub) -> None:
    """``worker probe`` and its command, for the agent of a review run."""
    parser = wsub.add_parser("probe", help="inside a review run: add a probe to a merged change that has none")
    psub = parser.add_subparsers(dest="probe_command", required=True)
    add = psub.add_parser("add", help="the task and the verify that say whether an agent repeats the mistake")
    add.add_argument("--change", required=True, type=int, metavar="N", help="the merged change it probes")
    add.add_argument("--repo", required=True, help="the repo of the project the task runs in")
    add.add_argument("--commit", metavar="SHA", help="the base commit; none: the tip of the default branch")
    task = add.add_mutually_exclusive_group(required=True)
    task.add_argument("--task", help="the task text the agent is given (- for stdin)")
    task.add_argument("--task-file", metavar="PATH", help="a file holding the task text (- for stdin)")
    add.add_argument(
        "--verify",
        required=True,
        help=f"one shell line, at most {probes.MAX_VERIFY_CHARS} characters, that exits 0 when the agent does not "
        "repeat the mistake",
    )
    add.add_argument(
        "--replaces",
        type=int,
        metavar="PROBE",
        help="the change's probe, not reproduced two weeks in a row, that this one replaces; the hub retires it",
    )
    add.set_defaults(func=cmd_add)
