"""The worker's PreToolUse hook of autonomy (plan autonomy-levels, criteria a8 and a9): what the agent of a claude-code
run with a snapshot of autonomy (``RunSpec.autonomy``, the card ``autonomy_card.Card.spec``) may run in Bash.

The hook reads each Bash command into its classes (``command_classes.classify``) and acts on the mode of each class for
the agent, as the card gives it (an ask a project policy sets reads auto: the merge queue and the release hold it):

- deny: it refuses the command; the reason names the class, the mode and what set it (the card's ``sources``).
- ask: it refuses the command and tells the agent to ask first, ``evo-agents worker ask --category <the class's
  category>``, and once the answer lets it, to run the command again after ``EVO_DECISION=<the decision's id>``. The
  hub then checks that the decision is the run's, of a category of the class, and answered (``POST
  /v1/worker/runs/{id}/autonomy-acts``); without the hub's word the command stays refused.
- notify: it lets the command through, records a ``system`` event of the run, and has the hub tell the principal (the
  same route), which also answers the mode now, the stricter of the snapshot and the value now: a class the hub reads
  deny or ask now is refused as above. A hub that does not answer in ACT_TIMEOUT seconds leaves the command through,
  the event saying the principal was not told.
- auto: it lets the command through and records the event.

Each event of the hook is a decision of the ledger S1 (plan autonomy-levels, criterion a10): its ``autonomy`` body names
the class, the mode, the command, the verdict, and the run, its principal and its agent.

The floor S2 (``FLOORS`` of ``autonomy_levels``): a command of K9 whose mode lets it run runs only when the run recorded
a backup made within the last 24 hours (``evo-agents worker backup``, kept in the run's ``backups.jsonl``); otherwise
the hook refuses it and says to make a backup first. This holds for every principal, A5 included.

A run without a snapshot (a local run, one queued before schema 0111) gets no hook of autonomy. The hook is a second
layer, never the boundary: a command written to a script first, piped through base64, or run in a subshell the
classifier does not read gets past it (``command_classes``). What the hub leases the run, and its gates on merges,
deploys and releases, are the boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evo_agents.hub.autonomy_card import CLASS_CATEGORIES
from evo_agents.hub.autonomy_levels import ASK, AUTO, CLASS_NAMES, DENY, NOTIFY
from evo_agents.worker.command_classes import Hit, classify, repo_slug

BACKUPS_FILE = "backups.jsonl"
BACKUP_FRESH = timedelta(hours=24)
DECISION_VARIABLE = "EVO_DECISION"
ACT_TIMEOUT = 10.0  # seconds the hook waits for the hub's word on a class in notify or ask
DECISION_PREFIX = re.compile(rf"^\s*{DECISION_VARIABLE}=#?(\d+)\s+")
GIT_TIMEOUT = 5.0
log = logging.getLogger(__name__)
SKEW = timedelta(minutes=5)  # how far in the future a backup's time may be, the clocks apart


def backups_path(home, run_id: int) -> Path:
    """The run's backups, as ``evo-agents worker backup`` keeps them: one JSON object a line."""
    return home.run_dir(run_id) / BACKUPS_FILE


def add_backup(path: Path, entry: Mapping) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(entry), ensure_ascii=False) + "\n")


def fresh_backup(path: Path, now: datetime | None = None) -> dict | None:
    """The latest backup of the run made within BACKUP_FRESH before ``now``; None when there is none."""
    now = now or datetime.now(UTC)
    found = None
    with contextlib.suppress(OSError):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
                made = datetime.fromisoformat(str(entry["made_at"]))
            except (ValueError, KeyError, TypeError):
                continue
            if made.tzinfo is not None and -SKEW <= now - made <= BACKUP_FRESH:
                if found is None or made > found[0]:
                    found = (made, entry)
    return None if found is None else found[1]


def _git(directory: Path, *args: str) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(directory), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def repo_facts(worktree: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The ``owner/name`` of the origin of each repo of the run's worktree (it, or the repos right under it), lower
    case, and the default branches their origins name."""
    slugs: list[str] = []
    branches: list[str] = []
    candidates = [worktree, *(child for child in sorted(worktree.iterdir()) if (child / ".git").exists())]
    for directory in candidates if worktree.is_dir() else []:
        url = _git(directory, "remote", "get-url", "origin")
        if url:
            slugs.append(repo_slug(url))
        head = _git(directory, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        if head.startswith("origin/"):
            branches.append(head.removeprefix("origin/"))
    return tuple(dict.fromkeys(slugs)), tuple(dict.fromkeys(branches))


def refusal(hit: Hit, mode: str, source: str) -> str:
    return f"{hit.what}: {hit.klass} ({CLASS_NAMES[hit.klass]}) is {mode} for the run's principal, set by {source}"


def ask_refusal(hit: Hit, mode: str, source: str) -> str:
    categories = CLASS_CATEGORIES.get(hit.klass, ("scope",))
    other = f" (or --category {categories[1]} for a change of real data)" if len(categories) > 1 else ""
    return (
        f"{refusal(hit, mode, source)}. Ask first: `evo-agents worker ask --category {categories[0]}`{other}, and wait "
        f"for the answer; when it lets you, run the command again as `{DECISION_VARIABLE}=<the decision's id> "
        "<command>`."
    )


def backup_refusal(hit: Hit) -> str:
    return (
        f"{hit.what}: {hit.klass} ({CLASS_NAMES[hit.klass]}) deletes or overwrites data for good, and the floor S2 "
        "holds for every principal: make a backup first, then record it with `evo-agents worker backup --target "
        '"<what>" --location "<where>" [--made-at <ISO time>] [--restore "<command>"]`; this run has no backup made '
        "within the last 24 hours."
    )


@dataclass
class Gate:
    """The hook of one run: the card's modes and sources, the run's worktree and backups, and how it records an event
    (``record``, a body) and asks the hub (``act``, a body of ``POST .../autonomy-acts``; None when the hub did not
    answer)."""

    modes: Mapping[str, str]
    sources: Mapping[str, str]
    worktree: Path
    backups: Path
    own_repos: tuple[str, ...] = ()
    default_branches: tuple[str, ...] = ()
    record: Callable[[str, dict], None] = field(default=lambda text, body: None)
    act: Callable[[dict], Awaitable[dict | None]] | None = None
    run_id: int | None = None
    principal: str | None = None  # the login of the run's principal, from its card
    agent: str | None = None  # the handle of the run's agent profile

    @classmethod
    def of(cls, autonomy: Mapping | None, worktree: Path, backups: Path, **given) -> Gate | None:
        """The gate of a run whose card is ``autonomy``; None for a run without one."""
        if not isinstance(autonomy, Mapping) or not isinstance(autonomy.get("modes"), Mapping):
            return None
        sources = autonomy.get("sources") if isinstance(autonomy.get("sources"), Mapping) else {}
        return cls(dict(autonomy["modes"]), dict(sources), Path(worktree), backups, **given)

    def source(self, klass: str) -> str:
        return self.sources.get(klass) or "the run's autonomy card"

    def read(self, command: str) -> tuple[list[tuple[Hit, str]], int | None]:
        """The classes of ``command`` with the mode of each for the agent, and the decision it names."""
        named = DECISION_PREFIX.match(command)
        hits = classify(command, self.worktree, own_repos=self.own_repos, default_branches=self.default_branches)
        return [(hit, self.modes.get(hit.klass, AUTO)) for hit in hits], int(named.group(1)) if named else None

    def refused(self, acted: list[tuple[Hit, str]], decision: int | None, now: datetime | None = None) -> str | None:
        """Why the hook refuses a command whose classes are ``acted``, before the hub has its word."""
        for hit, mode in acted:
            if mode == DENY:
                return refusal(hit, mode, self.source(hit.klass))
        for hit, mode in acted:
            if mode == ASK and decision is None:
                return ask_refusal(hit, mode, self.source(hit.klass))
        if any(hit.klass == "K9" for hit, _ in acted) and fresh_backup(self.backups, now) is None:
            return backup_refusal(next(hit for hit, _ in acted if hit.klass == "K9"))
        return None

    async def check(self, tool_name: str, tool_input) -> str | None:
        """Why the hook refuses the call of ``tool_name``; None when it lets it through (any tool but Bash)."""
        given = tool_input if isinstance(tool_input, Mapping) else {}
        if tool_name != "Bash" or not isinstance(given.get("command"), str):
            return None
        acted, decision = self.read(given["command"])
        if not acted:
            return None
        why = self.refused(acted, decision) or await self._hub(acted, decision)
        for hit, mode in acted:
            body = {"class": hit.klass, "mode": mode, "what": hit.what, "verdict": "refused" if why else "allowed"}
            body |= {"run": self.run_id, "principal": self.principal, "agent": self.agent}  # the ledger S1
            if decision is not None:
                body["decision_id"] = decision
            told = " (the hub tells the principal)" if mode == NOTIFY and not why else ""
            text = f"Autonomy hook {body['verdict']} a command of {hit.klass} ({mode}){told}: {hit.what}"
            self.record(text, body)
        return why

    async def _hub(self, acted: list[tuple[Hit, str]], decision: int | None) -> str | None:
        """The hub's word on the classes in notify and ask: refused when it reads one stricter now, or when the
        decision of a class in ask is not answered."""
        for hit, mode in acted:
            if mode not in (ASK, NOTIFY):
                continue
            body = {"klass": hit.klass, "what": hit.what}
            if mode == ASK:
                body["decision_id"] = decision
            answer = await self.act(body) if self.act is not None else None
            if answer is None:
                if mode == ASK:
                    return (
                        f"{refusal(hit, mode, self.source(hit.klass))}: the hub did not say whether decision "
                        f"#{decision} is answered; run it again later"
                    )
                continue
            now = answer.get("mode") or mode
            if now == DENY:
                return refusal(hit, now, "the principal's autonomy now (stricter than the run's snapshot)")
            if (mode == ASK or now == ASK) and not answer.get("answered"):
                if decision is None:
                    return ask_refusal(hit, now, "the principal's autonomy now (stricter than the run's snapshot)")
                return (
                    f"{refusal(hit, ASK, self.source(hit.klass))}: decision #{decision} is not an answered decision "
                    f"of this run in category {' or '.join(CLASS_CATEGORIES.get(hit.klass, ()))}"
                )
        return None


def claude_hook(gate: Gate | None):
    """The PreToolUse callback of the Claude Agent SDK for ``gate``, or None without one."""
    if gate is None:
        return None

    async def autonomy_gate(data, tool_use_id, context) -> dict:
        why = await gate.check(str(data.get("tool_name") or ""), data.get("tool_input"))
        if why is None:
            return {}
        decision = {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": why}
        return {"hookSpecificOutput": decision}

    return autonomy_gate


def for_run(run) -> Gate | None:
    """The gate of a run of the daemon (``runner.context.Run``) with a snapshot of autonomy: its events go to the run's
    log, its questions to the hub with the worker's token; None for a run without a snapshot or a worktree."""
    autonomy = run.spec.get("autonomy")
    if not autonomy or run.worktree is None:
        return None
    own_repos, branches = repo_facts(Path(run.worktree))

    async def act(body: dict) -> dict | None:
        try:
            return await asyncio.wait_for(run.daemon.hub.autonomy_act(run.id, body, timeout=ACT_TIMEOUT), ACT_TIMEOUT)
        except Exception as exc:  # no answer is the hub's silence: the gate says what it does without it
            log.info("autonomy act unanswered", extra={"run_id": run.id, "error": str(exc)[:200]})
            return None

    return Gate.of(
        autonomy,
        Path(run.worktree),
        backups_path(run.daemon.home, run.id),
        own_repos=own_repos,
        default_branches=branches,
        record=lambda text, body: run.note(text, autonomy=body),
        act=act,
        run_id=run.id,
        principal=autonomy.get("principal") if isinstance(autonomy, Mapping) else None,
        agent=(run.spec.get("profile") or {}).get("handle") if isinstance(run.spec.get("profile"), Mapping) else None,
    )
