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

The file floor (``Floor``, plan super-board-parity-1, step 2) holds in every claude-code run of the worker, with a
snapshot or without one, like S2 and S3: Edit, Write, MultiEdit and NotebookEdit only into the run's directory, the
system's temporary directories and the run's own Claude configuration (CLAUDE_CONFIG_DIR, else the memory directories
under ``~/.claude/projects``); Read, Grep, Glob and the Bash commands that read or copy a file (READERS) never on a
secret file (``secret_file``). A path is resolved to the real one, symlinks and ``..`` followed, before it is compared.
Each refusal is a ``system`` event of the run (its ``floor`` body), and a check that fails refuses the call. A program
the agent runs that reads ``.env`` itself is not the floor's business.

A run without a snapshot (a local run, one queued before schema 0113) gets no hook of autonomy, only the floor. The hook is a second
layer, never the boundary: a command written to a script first, piped through base64, or run in a subshell the
classifier does not read gets past it (``command_classes``). What the hub leases the run, and its gates on merges,
deploys and releases, are the boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import glob
import json
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evo_agents.hub.autonomy_card import CLASS_CATEGORIES
from evo_agents.hub.autonomy_levels import ASK, AUTO, CLASS_NAMES, DENY, NOTIFY
from evo_agents.worker.command_classes import SHELLS, Hit, _shell_script, classify, repo_slug, simple_commands

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
        f"{refusal(hit, mode, source)}. Ask first: `evo-agents worker ask --category {categories[0]}`{other}, with "
        "the option you recommend and why (`--recommended KEY --recommended-reason TEXT`) and whether the command can "
        "be undone (`--reversible yes --undo TEXT`, or `--reversible no`), and wait for the answer; when it lets you, "
        f"run the command again as `{DECISION_VARIABLE}=<the decision's id> <command>`."
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


WRITE_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
TEMP_DIRS = ("/tmp", "/var/tmp")
ENV_NAMES = re.compile(r"^\.env(\..+)?$")
ENV_EXAMPLES = frozenset({".env.example", ".env.sample", ".env.template"})
SSH_KEYS = re.compile(r"^id_(rsa|dsa|ecdsa|ed25519)(_sk)?$")
SECRET_NAMES = frozenset({".npmrc", ".pypirc", ".netrc"})
SECRET_SUFFIXES = (".pem", ".key")
EVO_TOKENS = re.compile(r"^(token|.+\.token)$")  # the hub's and the worker's token files under ~/.evo
# A sample of secret names and one plain name: a Glob pattern that matches a secret and not the plain one asks for
# secret files.
SECRET_SAMPLES = (".env", ".env.local", "id_rsa", "id_ed25519", "x.pem", "x.key", ".npmrc", ".pypirc", ".netrc")
PLAIN_SAMPLE = "x.py"
# The programs whose file arguments the floor reads as files they read or copy: a word of theirs that names an existing
# secret file refuses the command. ``source`` and the programs that read .env themselves are left alone.
READERS = frozenset(
    {
        *("cat", "tac", "nl", "less", "more", "head", "tail", "bat", "batcat", "view", "vi", "vim", "nvim", "nano"),
        *("emacs", "cp", "mv", "scp", "rsync", "install", "dd", "base64", "base32", "xxd", "hexdump", "od"),
        *("strings", "grep", "egrep", "fgrep", "rg", "ag", "ack", "sed", "awk", "gawk", "cut", "sort", "uniq"),
        *("diff", "cmp", "comm", "paste", "tar", "zip", "gzip", "bzip2", "xz", "7z", "curl", "wget", "jq", "yq"),
        *("openssl", "gpg", "sha256sum", "md5sum", "column", "fold", "tr", "iconv", "envsubst", "gh"),
    }
)
WRITE_OUTSIDE, SECRET_FILE = "write_outside", "secret_file"


def real(path: str | os.PathLike, base: Path) -> Path:
    """``path`` as the real path it names: ``~`` expanded, relative to ``base``, symlinks and ``..`` resolved."""
    given = Path(os.path.expanduser(str(path)))
    return Path(os.path.realpath(given if given.is_absolute() else base / given))


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def secret_file(path: Path, home: Path, worktree: Path | None = None) -> bool:
    """Whether the real ``path`` is a secret file: ``.env`` and ``.env.*`` (not the examples), an SSH key, ``*.pem``,
    ``*.key``, ``.npmrc``, ``.pypirc``, ``.netrc``, ``~/.aws/credentials``, ``~/.omelet.json`` and the credentials of
    ``~/.omelet.d``, and a token file under ``~/.evo`` outside the run's worktree."""
    name = path.name
    if ENV_NAMES.match(name) and name not in ENV_EXAMPLES:
        return True
    if SSH_KEYS.match(name) or name.endswith(SECRET_SUFFIXES) or name in SECRET_NAMES:
        return True
    if (name.startswith("id_") and not name.endswith(".pub")) and path.parent == home / ".ssh":
        return True
    if path in (home / ".aws" / "credentials", home / ".omelet.json") or _within(path, home / ".omelet.d/credentials"):
        return True
    evo = home / ".evo"
    inside = worktree is not None and _within(path, worktree)
    return not inside and _within(path, evo) and (EVO_TOKENS.match(name) is not None or _within(path, evo / "secrets"))


def secret_pattern(pattern: str) -> bool:
    """Whether a Glob or Grep pattern asks for secret files: its last part matches a secret name and not a plain
    one."""
    last = pattern.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if not last or fnmatch.fnmatch(PLAIN_SAMPLE, last):
        return False
    return any(fnmatch.fnmatch(sample, last) for sample in SECRET_SAMPLES) or secret_file(
        Path(last), Path("/nonexistent")
    )


def _arguments(word: str) -> list[str]:
    """The paths a word of a reader may name: itself, after ``@`` (curl), after ``=`` (``if=``, ``--file=``), after
    ``<``."""
    found = [] if word.startswith("-") else [word.lstrip("<")]
    for mark in ("@", "="):
        if mark in word:
            found.append(word.split(mark, 1)[1])
    return [path for path in found if path]


@dataclass
class Floor:
    """The file floor of one run (module docstring): where its agent may write (``writable``, real paths), its
    worktree, the home whose secrets it keeps, and how it records a refusal (``record``, a text and a body)."""

    worktree: Path
    writable: tuple[Path, ...]
    home: Path
    record: Callable[[str, dict], None] = field(default=lambda text, body: None)
    run_id: int | None = None
    agent: str | None = None

    @classmethod
    def of(
        cls,
        worktree: Path,
        env: Mapping[str, str] | None = None,
        home: Path | None = None,
        temp: tuple | None = None,
        **given,
    ) -> Floor:
        """The floor of a run that works in ``worktree`` with the agent's environment ``env``; ``temp`` the system's
        temporary directories, TEMP_DIRS and the one ``tempfile`` names by default."""
        env = env or {}
        home = Path(os.path.realpath(home or Path.home()))
        temp = (tempfile.gettempdir(), *TEMP_DIRS) if temp is None else temp
        roots = [worktree, *temp, env.get("TMPDIR") or ""]
        config = env.get("CLAUDE_CONFIG_DIR")
        roots.append(config or home / ".claude" / "projects")
        writable = tuple(dict.fromkeys(Path(os.path.realpath(os.path.expanduser(str(r)))) for r in roots if str(r)))
        return cls(Path(os.path.realpath(worktree)), writable, home, **given)

    def refused(self, tool_name: str, tool_input) -> tuple[str, str, str] | None:
        """(the rule, the path, why) when the floor refuses the call of ``tool_name``; None when it lets it through."""
        given = tool_input if isinstance(tool_input, Mapping) else {}
        if tool_name in WRITE_TOOLS:
            target = given.get(WRITE_TOOLS[tool_name])
            if not isinstance(target, str) or not target:
                return None
            path = real(target, self.worktree)
            if any(_within(path, root) for root in self.writable):
                return None
            where = ", ".join(str(root) for root in self.writable)
            return WRITE_OUTSIDE, str(path), f"{tool_name} of {path} is outside the run's directories; write in {where}"
        if tool_name in ("Read", "Grep", "Glob"):
            return self._reads(tool_name, given)
        if tool_name == "Bash" and isinstance(given.get("command"), str):
            return self._command(given["command"])
        return None

    def _secret(self, tool: str, path: Path) -> tuple[str, str, str] | None:
        if not secret_file(path, self.home, self.worktree):
            return None
        return SECRET_FILE, str(path), f"{tool} of {path}: the run's agent never reads a secret file (the file floor)"

    def _reads(self, tool_name: str, given: Mapping) -> tuple[str, str, str] | None:
        key = "file_path" if tool_name == "Read" else "path"
        target = given.get(key)
        if isinstance(target, str) and target:
            found = self._secret(tool_name, real(target, self.worktree))
            if found:
                return found
        pattern = given.get("pattern") if tool_name == "Glob" else given.get("glob")
        if isinstance(pattern, str) and secret_pattern(pattern):
            why = f"{tool_name} for {pattern} asks for secret files: the run's agent never reads one (the file floor)"
            return SECRET_FILE, pattern, why
        return None

    def _command(self, command: str, depth: int = 0) -> tuple[str, str, str] | None:
        for words in simple_commands(command):
            script = _shell_script(words) if Path(words[0]).name in SHELLS and depth < 3 else None
            found = self._command(script, depth + 1) if script else None
            if found:
                return found
            reader = Path(words[0]).name in READERS
            for index, word in enumerate(words[1:], 1):
                redirected = words[index - 1] == "<" or word.startswith("<")
                if not (reader or redirected):
                    continue
                for argument in _arguments(word):
                    for path in self._expand(argument):
                        found = self._secret(f"the command {Path(words[0]).name}", path)
                        if found:
                            return found
        return None

    def _expand(self, argument: str) -> list[Path]:
        if not glob.has_magic(argument):  # a word that names no file is a pattern or a value: nothing it reads
            path = real(argument, self.worktree)
            return [path] if path.is_file() else []
        pattern = os.path.expanduser(argument)
        pattern = pattern if os.path.isabs(pattern) else str(self.worktree / pattern)
        return [real(found, self.worktree) for found in sorted(glob.glob(pattern, include_hidden=True))[:200]]

    def check(self, tool_name: str, tool_input) -> str | None:
        """Why the floor refuses the call, recorded as an event of the run; None when it lets it through. A check that
        fails refuses."""
        try:
            found = self.refused(tool_name, tool_input)
        except Exception as exc:  # the floor holds: a call it cannot read is refused
            found = ("check_failed", "", f"the file floor could not check the call of {tool_name} ({exc}); refused")
        if found is None:
            return None
        rule, path, why = found
        body = {"rule": rule, "tool": tool_name, "path": path, "verdict": "refused", "run": self.run_id}
        body["agent"] = self.agent
        self.record(f"File floor refused {tool_name} ({rule}): {path}", body)
        return why


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


def claude_floor_hook(floor: Floor | None):
    """The PreToolUse callback of the Claude Agent SDK for the file floor, or None without one."""
    if floor is None:
        return None

    async def file_floor(data, tool_use_id, context) -> dict:
        why = floor.check(str(data.get("tool_name") or ""), data.get("tool_input"))
        if why is None:
            return {}
        decision = {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": why}
        return {"hookSpecificOutput": decision}

    return file_floor


def floor_for_run(run, env: Mapping[str, str]) -> Floor | None:
    """The file floor of a run of the daemon whose agent gets ``env``; None for a run without a worktree."""
    if run.worktree is None:
        return None
    profile = run.spec.get("profile")
    return Floor.of(
        Path(run.worktree),
        env,
        record=lambda text, body: run.note(text, floor=body),
        run_id=run.id,
        agent=profile.get("handle") if isinstance(profile, Mapping) else None,
    )


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
