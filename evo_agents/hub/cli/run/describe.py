"""What ``evo-agents hub run`` says of one run: what it works on (``step_text``), its repos and timeout, and every
field of ``run show`` that says something (``describe``, ``describe_local`` for a local run), as (label, text) pairs.
Standard library only, like the rest of the client."""

from __future__ import annotations

import json

from evo_agents.hub.chats import chat_link
from evo_agents.hub.cli.base import when
from evo_agents.hub.cli.run.events import RAW_CHARS, clip, clock
from evo_agents.hub.cli.run.options import kind_text
from evo_agents.hub.cli.run.spend import spend_lines
from evo_agents.hub.client import origin
from evo_agents.hub.runs import STEADY_GAP_SECONDS, STEADY_SECONDS, TERMINAL_STATES


def step_text(run: dict) -> str:
    """What a run works on: ``step 2 (Title) of plan P``, ``plan P (Title)`` for a plan run, or ``review (Title)`` for
    a review run, which works on no plan."""
    title = f" ({run['title']})" if run.get("title") else ""
    if run.get("kind") == "review":
        return f"review{title}"
    if run.get("kind") == "author":
        return f"author{title}" + (f" of plan {run['plan_id']}" if run.get("plan_id") else "")
    if run.get("kind") == "plan":
        return f"plan {run['plan_id']}{title}"
    return f"step {run['step_key']}{title} of plan {run['plan_id']}"


def repos_text(run: dict) -> str:
    """A plan run's repos, each with the branch the plan names for it."""
    return ", ".join(
        entry["repo"] + (f" ({entry['branch']})" if entry.get("branch") else "") for entry in run.get("repos") or ()
    )


def timeout_text(run: dict) -> str:
    minutes = run["timeout_min"]
    hours = run.get("kind") in ("plan", "author") and minutes % 60 == 0
    return f"{minutes // 60} h" if hours else f"{minutes} min"


def _what(run: dict, hub_url: str) -> list[tuple[str, str]]:
    """What the run does: its kind and title, or its step; its project or plan; a chat run's chat and message."""
    kind, plan = run["kind"], f"{run['plan_id']} of project {run['project']}, revision {run['plan_revision']}"
    if kind == "step":
        return [("step", f"{run['step_key']}" + (f": {run['title']}" if run["title"] else "")), ("plan", plan)]
    each = "a new session for each step" if run.get("session_per_step") else "one session"
    said = kind_text(run) or f"plan run: every step of the plan not done yet, in {each}"
    lines = [("kind", said)] + ([("title", run["title"])] if run["title"] else [])
    if kind == "review":
        lines.append(("project", run["project"]))
    if kind == "chat":
        link = origin(hub_url) + chat_link(run["chat_id"], run["chat_message_id"]) if run.get("chat_id") else "-"
        lines.append(("chat", f"#{run.get('chat_id')}, message #{run.get('chat_message_id')}, hop {run.get('hop')}"))
        lines.append(("message", link))
    if kind not in ("review", "author", "chat", "task", "local", "judge", "command") or run.get("plan_id"):
        lines.append(("plan", plan))
    return lines


def describe_local(run: dict, hub_url: str) -> list[tuple[str, str]]:
    """A local run has no worker, repos, attempts or lease: its machine, directory and account, and what it did."""
    lines = [("state", run["state"]), *_what(run, hub_url)]
    where = f"{run['machine']}, {run['cwd']}"
    lines += [
        ("machine", where),
        ("account", run["claude_email"] + (f" ({run['claude_profile']})" if run["claude_profile"] else "")),
    ]
    lines.append(("dispatched", f"by {run['dispatched_by']} at {when(run['queued_at'])} UTC"))
    if run["last_activity_at"]:
        lines.append(("last activity", f"{when(run['last_activity_at'])} UTC"))
    if run["finished_at"]:
        lines.append(("ended", f"{when(run['finished_at'])} UTC"))
    if run["waiting_since"] and run["state"] == "waiting":
        lines.append(("waiting", f"since {when(run['waiting_since'])} UTC, for the answer to a decision"))
    if run.get("overlap_wait") and run["state"] == "queued":
        wait = run["overlap_wait"]
        who = ", ".join(f"#{number}" for number in wait["run_ids"])
        lines.append(("waits for", f"run {who} to end: it edited {', '.join(wait['paths'])}, which this step names"))
    if run["session_id"]:
        lines.append(("session", run["session_id"]))
    lines.append(("events", f"{run['last_seq']}"))
    return lines


def describe(run: dict, hub_url: str = "") -> list[tuple[str, str]]:
    """The fields of a run that say something, as (label, text) pairs; ``describe_local`` says those of a local run."""
    lines = [("state", f"{run['state']}, attempt {run['attempt']} of {run['max_attempts']}"), *_what(run, hub_url)]
    lines += _placement(run)
    lines += _moves(run)
    lines += _results(run)
    lines.append(("events", f"{run['last_seq']}"))
    return lines


def _placement(run: dict) -> list[tuple[str, str]]:
    """Where and how the run works: its repos or repo with the commit, its worker, its runtime, who dispatched it."""
    plan_run = run["kind"] != "step"  # every other kind has repos, and no step
    commit = f", commit {run['commit_sha'][:12]}" if run["commit_sha"] else ""
    if plan_run:
        lines = [("repos", (repos_text(run) or "none") + commit)]
    else:
        lines = [("repo", run["repo"] + (f", branch {run['branch']}" if run["branch"] else "") + commit)]
    worker = f"{run['worker']} (#{run['worker_id']})" if run["worker_id"] else "none yet"
    if run["pinned_worker_id"]:
        worker += f", pinned to worker #{run['pinned_worker_id']}"
    runtime = run["runtime"] + (
        f" (asked for {run['requested_runtime']})" if run["runtime"] != run["requested_runtime"] else ""
    )
    runtime += f", model {run['model']}" if run["model"] else ""
    approval = "" if plan_run else f", approval {run['approval']}"
    runtime = f"{runtime}, {run['mode']}{approval}, timeout {timeout_text(run)}"
    if run["kind"] == "command":  # its runtime and mode are of record only: the machine's shell runs the command
        runtime = f"none, the machine's shell runs the command, timeout {timeout_text(run)}"
    return lines + [
        ("worker", worker),
        ("runtime", runtime),
        ("dispatched", f"by {run['dispatched_by']} at {when(run['queued_at'])} UTC{_as_owner(run)}"),
    ]


def _moves(run: dict) -> list[tuple[str, str]]:
    """How the run went so far: its agent time and times, waits, lease, the owner's asks, the run it retries, its
    session."""
    lines = []
    if run["run_seconds"]:
        lines.append(("agent time", f"{run['run_seconds'] // 60} min used of {timeout_text(run)}"))
    times = [(name, run[f"{name}_at"]) for name in ("leased", "started", "finished") if run[f"{name}_at"]]
    if times:
        lines.append(("times (UTC)", ", ".join(f"{name} {when(value)}" for name, value in times)))
    if run["waiting_since"] and run["state"] == "waiting":
        lines.append(("waiting", f"since {when(run['waiting_since'])} UTC, for the answer to a decision"))
    if run["parked_at"]:
        lines.append(("parked", f"at {when(run['parked_at'])} UTC, for want of an answer"))
    if run["resume_of_run_id"]:
        lines.append(("resumes", f"run #{run['resume_of_run_id']}, in its session and worktrees"))
    if run["lease_expires_at"] and run["state"] not in TERMINAL_STATES:
        lines.append(("lease until", f"{when(run['lease_expires_at'])} UTC"))
    asks = [(name, run[f"{name}_requested_at"]) for name in ("cancel", "takeover", "handback")]
    asked = [f"{name} at {when(value)} UTC" for name, value in asks if value]
    if asked:
        lines.append(("asked", ", ".join(asked)))
    if run["parent_run_id"]:
        lines.append(("retries", f"run #{run['parent_run_id']}"))
    if run.get("steady_wait") and run["state"] == "queued":
        lines.append(("waits for", _steady_wait(run)))
    if run["session_id"]:
        lines.append(("session", run["session_id"]))
    return lines


def _verify_text(result) -> str:
    if isinstance(result, dict):
        return f"exit {result.get('exit_code')}: {result.get('command')}"
    return json.dumps(result, ensure_ascii=False)


def _results(run: dict) -> list[tuple[str, str]]:
    """What the run left: its diffstat, verify results, usage, error and cause, evidence, request and uploads."""
    lines = []
    stat = run["diffstat"]
    if isinstance(stat, dict):
        parts = [f"{stat[key]} {key}" for key in ("files", "insertions", "deletions") if key in stat]
        lines.append(("diffstat", ", ".join(parts) or json.dumps(stat)))
    for index, result in enumerate(run["verify"] or ()):
        lines.append(("verify" if index == 0 else "", _verify_text(result)))
    lines += spend_lines(run.get("spend"))
    if run["usage"]:
        lines.append(("usage", clip(json.dumps(run["usage"], ensure_ascii=False, separators=(",", ":")), RAW_CHARS)))
    texts = [("error", run["error"]), ("cause", run.get("failure_cause")), ("evidence", run["evidence"])]
    texts.append(("request", run.get("request")))
    lines += [(label, text) for label, text in texts if text]
    blobs = [f"{name} {run[f'{name}_sha256'][:12]}" for name in ("log", "diff") if run[f"{name}_sha256"]]
    if blobs:
        lines.append(("uploaded", ", ".join(blobs)))
    return lines


def _as_owner(run: dict) -> str:
    """What `run show` says of ``as_owner`` after who dispatched the run."""
    return "; it runs as them on the hub's API, secrets, tokens and workers aside" if run.get("as_owner") else ""


def _steady_wait(run: dict) -> str:
    """Why a queued attempt waits for the worker the run before it was lost on, and until when, in three lines."""
    wait = run["steady_wait"]
    worker = f"worker {wait['worker']} (#{wait['worker_id']}), which lost run #{run['parent_run_id']}, to be steady"
    rule = f"once its heartbeats have come for {STEADY_SECONDS} s, none more than {STEADY_GAP_SECONDS} s late"
    steady = f"from {clock(wait['steady_at'])} UTC if they go on" if wait.get("steady_at") else "it sends none now"
    if run["pinned_worker_id"] == wait["worker_id"]:
        others = "the run is pinned to that worker, so no other takes it"
    else:
        others = "any other worker that may take it takes it now"
    return f"{worker}\nit takes this attempt only {rule}: {steady}\n{others}"


def shown_lines(run: dict, hub_url: str) -> list[str]:
    """``run show`` of ``run``: its number, then each field, the further lines of one indented under its first."""
    fields = (describe_local if run["kind"] == "local" else describe)(run, hub_url)
    width = max(len(label) for label, _ in fields)
    lines = [f"Run #{run['id']}"]
    for label, text in fields:
        first, *rest = str(text).splitlines() or [""]
        lines.append(f"  {label:<{width}}  {first}".rstrip())
        lines += [f"  {'':<{width}}  {line}".rstrip() for line in rest]
    return lines
