"""The frame of what the agent of a triage thread does on its own, in code and not in the agent's instructions.

When a run fails, its triage thread (``evo_agents.hub.server.triage``) has an agent that reads the run. It may act with
no one's say-so only inside this frame, which the hub enforces on each call (``evo_agents.hub.server.triage_actions``)
whatever the agent was told:

- run the failed step again for its owner (``RERUN_CAUSES``: the causes the policy table of the design report gives
  an automatic rerun), at most ``RERUN_CAP`` times in a chain of reruns and ``CAUSE_CAP`` time for each cause, within
  the chat's budget; a cause the hub guessed first has to be confirmed by the agent after it read the log;
- change the ``verify`` or narrow the ``what`` of a step of the failed run's plan that is still ``pending`` and
  names a repo of that run (``edit_problems``), as a new revision under the owner's name, never losing a clause of
  the ``verify``; the ``verify_failed`` cause is rerun only together with such an edit.

- when the cause or the error points at a repo of another project, invite the agent of that project into the thread
  (``named_in`` finds what the error names; the hub checks the owner's writer role there and the chat's label) and hand
  work to it by a mention, within the hop limit of a chat.

Anything else is asked of the owner with a decision. Standard library only: the plan is a plain dict.
"""

from __future__ import annotations

import re

RERUN_CAP = 2  # automatic reruns in one chain of runs (each rerun has its failed run as ``parent_run_id``)
CAUSE_CAP = 1  # automatic reruns of one chain for one cause
EDITABLE = ("verify", "what")  # the keys of a step the agent may change
VERIFY_WITH_EDIT = "verify_failed"  # rerun only by an edit of the plan (``triage plan --rerun``)

# The causes a failed run is run again of by the agent, with what each needs of the rerun.
RERUN_CAUSES = {
    "missing_tool": "on the worker the run was on, once the program is installed there",
    "rate_limit": "with another account of the owner, or another runtime",
    "account_auth": "with another account of the owner, or another runtime",
    "worker_stopped": "on a worker of the owner that is fit",
    "runtime": "on a worker or a runtime that is fit",
    "lease_lost": "on a worker of the owner that is fit",
    "network": "on the worker the run was on",
    "hub_5xx": "on the worker the run was on",
    "checkout": "on a worker that has a checkout of every repo of the run",
    VERIFY_WITH_EDIT: "after the verify or the what of a step was edited",
}


def named_in(text: str | None, names) -> dict[str, str]:
    """Which of ``names`` the ``text`` of an error names, as whole words (a name is bounded by anything but a letter, a
    digit, ``_``, ``-`` or ``.``): {name: the name}; the hub reads it as a pointer to that project, never as a fact."""
    found = {}
    for name in names:
        if name and re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", text or ""):
            found[name] = name
    return found


SAME_WORKER = ("missing_tool", "network", "hub_5xx")  # the rerun stays on the worker of the failed run
NEW_ACCOUNT = ("rate_limit", "account_auth")  # the rerun takes an account the run did not, or another runtime


def clauses(verify: str) -> list[str]:
    """The commands of a ``verify``, split on ``&&``, ``;`` and line ends."""
    return [part.strip() for part in re.split(r"&&|;|\n", verify) if part.strip()]


def rerun_problem(cause: str | None, *, inferred: bool, confirmed: bool, edited: bool) -> str | None:
    """Why a run that failed of ``cause`` is not rerun by the agent, or None when it may be."""
    if cause is None or cause not in RERUN_CAUSES:
        return (
            f"cause {cause or 'unknown'} has no automatic rerun: ask the owner with a decision, one option per thing "
            "you can do"
        )
    if cause == VERIFY_WITH_EDIT and not edited:
        return (
            "a run that failed its verify is rerun by the agent only together with an edit of the plan "
            "(`triage plan --rerun`); when the code is wrong, ask the owner"
        )
    if inferred and not confirmed:
        return (
            f"the hub guessed cause {cause} from the error: read the run's log, and when it is right say so with "
            "--confirm-cause"
        )
    return None


def chain_problem(depth: int, causes: list[str], cause: str | None) -> str | None:
    """Why the chain of reruns is full: ``depth`` automatic reruns made so far, ``causes`` the ones they were for."""
    if depth >= RERUN_CAP:
        return f"this chain of runs was rerun {depth} times already, the most ({RERUN_CAP}): ask the owner"
    if sum(1 for earlier in causes if earlier == cause) >= CAUSE_CAP:
        return f"this chain of runs was rerun once already for cause {cause}: ask the owner"
    return None


def _repo_of(step: dict) -> str | None:
    repo = step.get("repo")
    return repo if isinstance(repo, str) and repo else None


def _step_problems(was: dict, now: dict, repos: set[str]) -> list[str]:
    key = was.get("id", "?")
    problems = []
    if was.get("status") != "pending":
        problems.append(f"step {key} is {was.get('status') or 'without a status'}: only a pending step may be edited")
    if now.get("status") != was.get("status"):
        problems.append(f"step {key}: the status is not the agent's to change")
    if _repo_of(was) not in repos:
        problems.append(
            f"step {key} is a step of repo {_repo_of(was) or 'none'}, not of this run's ({', '.join(sorted(repos)) or 'none'})"
        )
    changed = {name for name in set(was) | set(now) if was.get(name) != now.get(name)}
    for name in sorted(changed - set(EDITABLE)):
        problems.append(f"step {key}: {name} is not one of {', '.join(EDITABLE)}, the only keys the agent may change")
    if "what" in changed:
        before, after = was.get("what"), now.get("what")
        if not isinstance(before, str) or not isinstance(after, str) or not after.strip():
            problems.append(f"step {key}: what stays text with some words")
        elif len(after) >= len(before):
            problems.append(f"step {key}: what is only narrowed ({len(after)} characters would replace {len(before)})")
    if "verify" in changed:
        before, after = was.get("verify"), now.get("verify")
        if not isinstance(before, str) or not isinstance(after, str):
            problems.append(f"step {key}: verify stays one command line")
        elif len(clauses(after)) < len(clauses(before)):
            problems.append(
                f"step {key}: the new verify has {len(clauses(after))} commands where the old one had "
                f"{len(clauses(before))}: dropping a check is the owner's to decide, ask"
            )
    return problems


def edit_problems(old: dict, new: dict, repos: set[str]) -> list[str]:
    """What is wrong with the plan ``new`` as an edit of ``old`` by the agent of a triage thread, one line each; none
    when it is allowed: nothing differs from ``old`` but ``EDITABLE`` keys of steps that are pending and of ``repos``."""
    problems = []
    for name in sorted((set(old) | set(new)) - {"steps"}):
        if old.get(name) != new.get(name):
            problems.append(f"{name} of the plan is not the agent's to change")
    was, now = old.get("steps"), new.get("steps")
    if not isinstance(was, list) or not isinstance(now, list) or len(was) != len(now):
        return [*problems, "the steps of the plan are not the agent's to add or remove"]
    for before, after in zip(was, now, strict=True):
        if before == after:
            continue
        if not isinstance(before, dict) or not isinstance(after, dict):
            problems.append("a step that is not a mapping cannot be edited")
            continue
        problems += _step_problems(before, after, repos)
    return problems
