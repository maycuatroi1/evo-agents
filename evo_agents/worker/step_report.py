"""What ``evo-agents worker step`` checks and finds before it reports a step (``evo_agents.worker.cli.step``): the
verify commands it was given, the step of the plan and its repo, the proofs of the criteria the step names, and the
notice of a push to a default branch. ``step_verify`` runs the verify, commits and pushes.

``agent`` is the command's ``AgentRunContext`` (``evo_agents.worker.cli.base``): the run's id, the worker's home and
the run's record and worktrees. A problem is a WorkerStateError, which the command prints as one ``error:`` line.
"""

from __future__ import annotations

import sys
from typing import Any

from evo_agents.hub import proofs
from evo_agents.worker.home import WorkerStateError

MAX_VERIFY_COMMANDS = 50
MAX_VERIFY_CHARS = 2000
MAX_EVIDENCE_CHARS = 16 * 1024  # a step report's evidence, as the hub takes it


def plan_step(body: dict, key: str) -> dict | None:
    """The step of ``body`` whose id is ``key`` (ids compare as text), or whose order it is."""
    steps = [step for step in body.get("steps") or [] if isinstance(step, dict)]
    for step in steps:
        if str(step.get("id")) == key:
            return step
    if key.isdigit() and 1 <= int(key) <= len(steps):
        return steps[int(key) - 1]
    return None


def step_repo(body: dict, step: dict | None, workspaces: dict) -> str | None:
    """The repo a step is in: the plan's repo of the step, or the run's only repo."""
    named = step.get("repo") if isinstance(step, dict) else None
    if isinstance(named, str) and named in workspaces:
        return named
    repos = [entry.get("repo") for entry in body.get("repos") or [] if isinstance(entry, dict)]
    if not isinstance(named, str) and len(repos) == 1 and repos[0] in workspaces:
        return repos[0]
    return next(iter(workspaces)) if len(workspaces) == 1 else None


def verify_commands(args) -> list[str]:
    """The verify commands of ``worker step``, once the status and the commands agree."""
    verify = list(args.verify or [])
    if args.status in ("done", "verify") and not verify:
        raise WorkerStateError(
            f"{'a step is done' if args.status == 'done' else 'verify runs'} only with the commands that check it: "
            "give --verify COMMAND once for each; the worker runs them in the repo's worktree"
        )
    if args.status not in ("done", "verify") and (verify or args.wait is not None):
        raise WorkerStateError("--verify and --wait go with done and verify only")
    if len(verify) > MAX_VERIFY_COMMANDS or any(not c.strip() or len(c) > MAX_VERIFY_CHARS for c in verify):
        raise WorkerStateError(
            f"give 1 to {MAX_VERIFY_COMMANDS} verify commands, each 1 to {MAX_VERIFY_CHARS} characters"
        )
    if args.evidence is not None and (not args.evidence.strip() or len(args.evidence) > MAX_EVIDENCE_CHARS):
        raise WorkerStateError(f"--evidence takes 1 to {MAX_EVIDENCE_CHARS} characters")
    if args.proof and args.status != "done":
        raise WorkerStateError(f"{proofs.FLAG} goes with done only")
    return verify


def step_proofs(args, body: dict, step, worktree) -> list[dict] | None:
    """The proofs of ``worker step KEY done``, one for each criterion of the plan's acceptance the step names, each
    test found in the repo's worktree; None for a step that names none and was given none. WorkerStateError naming
    the criteria left without one, before anything is verified or pushed."""
    try:
        given = proofs.parse_all(args.proof or [])
    except proofs.ProofProblem as exc:
        raise WorkerStateError(str(exc)) from None
    wanted = proofs.criteria(body, step)
    if not wanted and not given:
        return None
    why = proofs.problem(wanted, given)
    if why is not None:
        raise WorkerStateError(f"step {args.key} is not done: {why}. Nothing was verified, committed or pushed")
    for item in given:
        missing = proofs.missing_test(item, worktree)
        if missing:
            raise WorkerStateError(f"the proof of {item['id']}: {missing}. Nothing was verified, committed or pushed")
    return proofs.stored(given)


def step_workspaces(agent: Any, repo: str | None) -> dict:
    """The run's worktrees by repo it works in, once ``--repo``, when given, names one of them: a repo the run only
    reads (one of the plan's context_repos) takes no step."""
    held = agent.workspaces()
    workspaces = {name: workspace for name, workspace in held.items() if not workspace.read_only}
    if repo is not None and repo not in workspaces:
        works = f"it works in {', '.join(workspaces) or 'no repo'}"
        if repo in held:
            raise WorkerStateError(
                f"run {agent.run_id} only reads {repo}, one of the plan's context_repos: no step is done, committed "
                f"or pushed in it; {works}"
            )
        raise WorkerStateError(f"run {agent.run_id} works in {', '.join(workspaces) or 'no repo'}, not in {repo}")
    return workspaces


def refuse_done_after_conflict(agent: Any, status: str) -> None:
    """A run whose push went to its side branch reports nothing done any more."""
    conflict = agent.home.load_conflict(agent.run_id)
    if status == "done" and conflict is not None:
        raise WorkerStateError(
            f"run {agent.run_id} ends failed (push_conflict) once this turn ends: {conflict['error']}. Nothing more is "
            "pushed or reported done; end your turn now"
        )


async def notify_push(hub, agent: Any, name: str, pushed) -> None:
    """Tell the run's owner of a change pushed to a default branch; the push happened, so a failed notice only warns."""
    from evo_agents.worker import gitops
    from evo_agents.worker.hubapi import HubProblem

    if not (pushed.default and pushed.changed):
        return
    try:
        await hub.notice(agent.run_id, gitops.push_notice(agent.run_id, name, pushed))
        print(f"Notified the run's owner of the push to {pushed.branch}, a default branch.")
    except HubProblem as exc:
        print(
            f"warning: the notice of the push to {pushed.branch} was not sent ({exc}); send it with "
            "`evo-agents worker notify --kind push_default_branch`",
            file=sys.stderr,
        )


def named_repo(name: str | None, key: str, workspaces: dict) -> str:
    """The repo a done or verify report of step ``key`` is about; WorkerStateError when none is named."""
    if name is None:
        raise WorkerStateError(f"name the repo of step {key} with --repo: one of {', '.join(workspaces)}")
    return name
