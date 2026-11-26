"""``evo-agents worker probe add``: the probe of a merged change of the Curator that has none, or the one in place of
its probe the weekly report found not reproduced (``evo_agents.hub.probes``), which the agent of a review run writes.

``probe add --change N --repo REPO [--commit SHA] (--task TEXT | --task-file PATH) --verify LINE [--replaces PROBE]``:
the task an agent is given in REPO, from the base commit (none: the tip of the default branch), and the one-line verify
that exits 0 when the agent does not repeat the mistake; with --replaces, in place of the change's probe that the
weekly report found not reproduced two weeks in a row, which the hub retires. The task and the verify are masked as
a digest is (``evo_agents.hub.redact``) before they leave this machine.

It reads the run from EVO_RUN_ID and EVO_WORKER_HOME, as ``worker playbook`` does, refuses outside a review run of
this worker, calls the hub with the worker's token (POST /v1/worker/runs/{id}/probes,
``evo_agents.hub.server.probes.routes.probes``) and prints one line with the probe. A failure is one ``error:`` line on stderr
and exit status 1; without the worker extra, status 2. TEXT ``-`` reads stdin.
"""

from __future__ import annotations

import sys

from evo_agents.clikit import Arg, Group, Result, Text
from evo_agents.hub import probes
from evo_agents.hub.api import WorkerRun
from evo_agents.hub.redact import redact
from evo_agents.worker.agent_input import read_text
from evo_agents.worker.cli.base import EXIT_FAILED, AgentCommand, AgentRunContext
from evo_agents.worker.home import WorkerStateError


def _task(args) -> str:
    if args.task_file is not None:
        text = read_text(args.task_file, "task", probes.MAX_TASK_BYTES)
    else:
        text = sys.stdin.read() if args.task == "-" else args.task
    if not text or not text.strip():
        raise WorkerStateError("a probe's task is empty: give the task text the agent is given")
    return redact(text)


class ProbeAdd(AgentCommand):
    name = "add"
    words = "probe add"
    kinds = ("review",)
    hub_failed = EXIT_FAILED
    help = "the task and the verify that say whether an agent repeats the mistake"
    arguments = (
        Arg("--change", required=True, type=int, metavar="N", help="the merged change it probes"),
        Arg("--repo", required=True, help="the repo of the project the task runs in"),
        Arg("--commit", metavar="SHA", help="the base commit; none: the tip of the default branch"),
    )
    more = (
        Arg(
            "--verify",
            required=True,
            help=f"one shell line, at most {probes.MAX_VERIFY_CHARS} characters, that exits 0 when the agent does not "
            "repeat the mistake",
        ),
        Arg(
            "--replaces",
            type=int,
            metavar="PROBE",
            help="the change's probe, not reproduced two weeks in a row, that this one replaces; the hub retires it",
        ),
    )

    def add_arguments(self, parser) -> None:
        task = parser.add_mutually_exclusive_group(required=True)
        task.add_argument("--task", help="the task text the agent is given (- for stdin)")
        task.add_argument("--task-file", metavar="PATH", help="a file holding the task text (- for stdin)")
        for arg in self.more:  # after the task, where the help lists them
            arg.add_to(parser)

    def run(self, ctx: AgentRunContext) -> Result:
        args = ctx.args
        verify = redact(args.verify.strip())
        why = probes.verify_problem(verify)
        if why is not None:
            raise WorkerStateError(f"the probe's verify {why}")
        body = {"change_id": args.change, "repo": args.repo, "task": _task(args), "verify": verify}
        if args.commit:
            body["commit"] = args.commit
        if args.replaces is not None:
            body["replaces"] = args.replaces
        ctx.check()
        run_id = ctx.run_id
        probe = ctx.with_hub(lambda hub: WorkerRun(hub, run_id).call("POST", "probes", body=body))
        replaced = f", in place of probe #{args.replaces}" if args.replaces is not None else ""
        said = (
            f"Probe #{probe.get('id')} of change #{probe.get('change_id')} recorded, in {probe.get('repo')}{replaced}."
        )
        return Result(probe, Text(said))


PROBE = Group("probe", "inside a review run: add a probe to a merged change that has none", [ProbeAdd()])
