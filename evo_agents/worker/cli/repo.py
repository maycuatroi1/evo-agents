"""``evo-agents worker repo open REPO`` (``chat_runs.OPEN_COMMAND``), which only a chat run's agent uses.

It asks the daemon, on the run's socket (``credential_commands.ask_open``), to open repo REPO of the project in a chat
run of this worker: the daemon asks the hub to add it to the run, takes the leases that cover it, fetches its checkout
and makes its worktree on the run's branch in the run's directory (``runner.chat``). It prints the worktree's path
alone, the same path for a repo open already. It holds no token and calls no hub itself, so it needs the core package
only; a repo this worker has no checkout of, one the project does not list, or a run that is not a chat run of this
worker is one ``error:`` line and exit status 1.
"""

from __future__ import annotations

from evo_agents.clikit import Arg, Group, Result, Stderr, Text, Views
from evo_agents.worker import credential_commands
from evo_agents.worker.cli.base import EXIT_FAILED, AgentCommand, AgentRunContext
from evo_agents.worker.home import WorkerStateError


class RepoOpen(AgentCommand):
    name = "open"
    words = "repo open"
    kinds = None  # any run of this worker; one of another kind than chat is refused below, in its own words
    hub_failed = EXIT_FAILED
    help = (
        "have the worker add a repo of the project to the chat run and make its worktree; prints the worktree's path, "
        "the same path for a repo open already"
    )
    arguments = (Arg("repo", metavar="REPO", help="the repo, as the project names it"),)

    def run(self, ctx: AgentRunContext) -> Result:
        run_id, kind = ctx.run_id, ctx.kind
        if kind != "chat":
            article = "an" if kind[:1] in "aeiou" else "a"
            raise WorkerStateError(
                f"run {run_id} is {article} {kind} run: only the agent of a chat run opens a repo with `evo-agents "
                f"worker repo open`; {article} {kind} run works in the repos it was given"
            )
        answer = credential_commands.ask_open(ctx.home, run_id, ctx.args.repo)
        path = answer.get("path")
        if answer.get("error") or not isinstance(path, str) or not path:
            raise WorkerStateError(str(answer.get("error") or f"the daemon did not say where {ctx.args.repo} is"))
        note = (
            f"note: the worktree was fetched as it opened; before your own git or gh on it, run "
            f'`eval "$(evo-agents worker env --run {run_id})"` in that shell for the run\'s credential'
        )
        return Result(answer, Views(Text(path), Stderr(Text(note))))


REPO = Group("repo", "inside a chat run: open a repo of its project, which the worker prepares", [RepoOpen()])
