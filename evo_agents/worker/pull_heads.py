"""The head and the scope of a pull request as gh reads them now (plan super-board-parity-2, criterion a3), for a merge
approval bound to them (``evo_agents.hub.approval_heads``).

``evo-agents worker ask --category merge --pull N`` binds the decision it asks to the pull request's head and scope
(``read``); the autonomy hook reads them again when the agent runs ``EVO_DECISION=<id> gh pr merge N`` (``of_command``),
and the hub opens the gate only when they are the same. gh runs in the run's directory with the agent's environment,
so it reads the pull request the agent means, with the agent's token; a pull request gh cannot read gives None, which
opens no bound approval.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

from evo_agents.hub import approval_heads
from evo_agents.worker.command_classes import _option, simple_commands

GH_TIMEOUT = 15.0  # seconds gh may take to read a pull request
FIELDS = "number,headRefOid,files,body,url"
URL = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")
VALUED = frozenset({"--repo", "-R", "--subject", "-t", "--body", "-b", "--body-file", "-F", "--match-head-commit"})
VALUED |= {"--author-email", "-A"}


def read(ref: str | None, repo: str | None, cwd: Path, env: Mapping[str, str] | None = None) -> dict | None:
    """The approval (``approval_heads.approval``) of pull request ``ref`` (a number, a URL or a branch; the current
    branch's when None) of ``repo`` (the run's origin when None): its head and the hash of its scope. None when gh does
    not read it."""
    args = ["gh", "pr", "view", *([ref] if ref else []), "--json", FIELDS, *(["--repo", repo] if repo else [])]
    try:
        done = subprocess.run(
            args,
            cwd=cwd,
            env={**(env if env is not None else os.environ), "GH_PROMPT_DISABLED": "1"},
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    try:
        pull = json.loads(done.stdout)
    except ValueError:
        return None
    return of_view(pull)


def of_view(pull) -> dict | None:
    """The approval of a pull request as ``gh pr view --json`` gives it; None for one without a head."""
    if not isinstance(pull, Mapping) or not isinstance(pull.get("headRefOid"), str):
        return None
    head = pull["headRefOid"]
    if not approval_heads.HEAD.match(head.lower()):
        return None
    files = [item["path"] for item in pull.get("files") or [] if isinstance(item, Mapping) and item.get("path")]
    found = URL.search(pull.get("url") or "")
    name = f"{found.group(1)}/{found.group(2)}#{found.group(3)}" if found else None
    scope = approval_heads.scope_of(files, pull.get("body") if isinstance(pull.get("body"), str) else None)
    return approval_heads.approval(head, scope, name)


def merge_target(command: str) -> tuple[str | None, str | None] | None:
    """The pull request and the repo of the first ``gh pr merge`` of ``command``: (its number, URL or branch, or None
    for the current branch's; its ``--repo``); None when it has none."""
    for words in simple_commands(command):
        if os.path.basename(words[0]) != "gh" or words[1:3] != ["pr", "merge"]:
            continue
        rest = words[3:]
        ref = None
        index = 0
        while index < len(rest):
            word = rest[index]
            if word in VALUED:
                index += 2
                continue
            if not word.startswith("-"):
                ref = word
                break
            index += 1
        return ref, _option(rest, "--repo", "-R")
    return None


def of_command(command: str, cwd: Path, env: Mapping[str, str] | None = None) -> dict | None:
    """The approval of the pull request the ``gh pr merge`` of ``command`` merges, as gh reads it now; None for a
    command without one (``glab mr merge``, a push) or a pull request gh does not read."""
    target = merge_target(command)
    if target is None:
        return None
    return read(target[0], target[1], cwd, env)
