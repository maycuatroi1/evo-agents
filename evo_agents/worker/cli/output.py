"""``evo-agents worker output FILE... [--caption TEXT]``: the command of a run's agent, of any kind and runtime, that
sends files to the run's outputs on the hub (source sent), for the people who read the run: a screenshot, a video, a
report. :func:`attach_arg` and :func:`interactive_arg` are ``--attach FILE`` and ``--interactive`` of ``worker ask``
and ``worker chat post``, the files a question or a message of the agent carries, sent the same way
(``evo_agents.worker.output_files``).

It reads the run from EVO_RUN_ID, EVO_WORKER_HOME and the run's record there, and refuses outside a run of this
worker. It checks the files and the room the hub leaves the run (``output_files.checked_files`` and ``check_room``):
a file past a limit is one ``error:`` line naming it and exit status 1, and nothing is sent. Then it puts a copy of
each in the run's queue, which the daemon sends in the background, and returns at once. A failure is one ``error:``
line on stderr and exit status 1; the check with the hub needs the worker extra, and without it the files go
unchecked.

``--interactive`` of ``output`` and of ``chat post`` (with ``--attach``) marks each file an interactive answer, which
the web shows inside the chat message that carries it: an HTML file of at most ``outputs.MAX_INTERACTIVE_BYTES``
(``outputs.interactive_problem``), else one ``error:`` line naming why, and nothing is sent. The hub checks it again.
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Result, Text
from evo_agents.hub import outputs
from evo_agents.worker.cli.base import EXIT_FAILED, AgentCommand, AgentRunContext
from evo_agents.worker.home import WorkerStateError
from evo_agents.worker.output_files import check_room, checked_files
from evo_agents.worker.output_queue import OutputQueue


def attach_arg(what: str) -> Arg:
    """``--attach FILE``: the files ``what`` carries."""
    return Arg(
        "--attach",
        action="append",
        metavar="FILE",
        help=f"a file {what} carries, a screenshot or a report, sent to the run's outputs first; repeat it, at most "
        f"{outputs.MAX_ATTACHED}",
    )


def interactive_arg(what: str) -> Arg:
    """``--interactive``: ``what`` is an interactive answer."""
    return Arg(
        "--interactive",
        action="store_true",
        help=f"mark {what} an interactive answer, shown inside the chat message that carries it: an HTML file of at "
        f"most {outputs.MAX_INTERACTIVE_BYTES} bytes, which may load libraries from esm.sh, cdn.jsdelivr.net and "
        "cdnjs.cloudflare.com in a chat that is not restricted",
    )


def _caption(value: str | None) -> str | None:
    try:
        return outputs.check_caption(value)
    except ValueError as exc:
        raise WorkerStateError(str(exc)) from None


class Output(AgentCommand):
    name = "output"
    kinds = None  # a run of any kind
    hub_failed = EXIT_FAILED
    help = (
        "inside a run: send files to the run's outputs on the hub, for the people who read the run (a screenshot, a "
        "video, a report); the worker uploads them in the background"
    )
    arguments = (
        Arg("files", nargs="+", metavar="FILE", help=f"a file of at most {outputs.MAX_FILE_BYTES} bytes"),
        Arg("--caption", metavar="TEXT", help=f"what the files show, at most {outputs.MAX_CAPTION_CHARS} characters"),
        interactive_arg("each file"),
    )

    def run(self, ctx: AgentRunContext) -> Result:
        args = ctx.args
        ctx.check()
        run_id, home = ctx.run_id, ctx.home
        caption = _caption(args.caption)
        interactive = bool(getattr(args, "interactive", False))
        files = checked_files(args.files, outputs.MAX_BATCH, interactive=interactive)
        check_room(home, run_id, files)
        queue = OutputQueue(home, run_id)
        for file in files:
            queue.put_file(
                file.path, name=file.name, media_type=file.media_type, caption=caption, interactive=interactive
            )
        names = ", ".join(file.name for file in files)
        kind = "interactive answer(s)" if interactive else "file(s)"
        said = (
            f"Sending {len(files)} {kind} to the outputs of run #{run_id} on the hub: {names}. The worker uploads them "
            "in the background; the run's page shows them once the hub has them."
        )
        return Result([file.name for file in files], Text(said))


OUTPUT = Output()
