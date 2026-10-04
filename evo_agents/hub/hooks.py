"""``evo-agents hub hook session-start|stop``: the Claude Code hooks of the evo-hub plugin.

SessionStart (startup, resume, clear) does in one process what three commands do for the session's directory:

- ``hub memory pull``: the hub's memories of the directory come into its memory directory;
- ``hub plan export``, when the harness around the directory names ``hub.project`` in its harness.yaml: the copies of
  the project's plans are written and never committed, and a file someone edited by hand stays as it is and is
  reported rather than overwritten (``mirror.export`` with ``keep_edited``);
- ``hub skills sync --check``: how many skills differ from the hub's, writing nothing.

It prints one line for the session as ``additionalContext``: the hub, the project, the memories pulled, what the export
did and the skills to sync. Stop does ``hub memory push`` for the directory, so another machine sees a new memory at its
next session. Stop runs after every turn, so it asks the hub only when a memory file differs from what the last sync
left in memory-state.json (``MemorySync.pending``): a turn that wrote no memory sends no request.

A hook never fails the session and never holds it long:

- the exit status is 0 whatever happens, and the output one line at most: the hub's URL, the project, counts and why
  something failed, never a token or what a memory or plan says; an unexpected exception shows as its type only;
- not signed in, nothing is sent anywhere: SessionStart says how to sign in, Stop says nothing;
- every request gives up after REQUEST_TIMEOUT seconds and the whole run after BUDGETS seconds (SIGALRM, where the
  platform has it), keeping what it finished: files are written atomically and memory-state.json records what was
  synced, so the next run does the rest. Claude Code's own hook timeout (hooks.json) is the last guard;
- a hub that did not answer is not asked again by Stop for COOLDOWN seconds (when it failed is kept in
  ~/.evo/hub/hook-state.json): the memory files wait on this machine and a later Stop pushes them;
- one memory sync runs at a time per machine: a hook waits LOCK_WAIT seconds for another, then leaves its work to the
  next run.

Stop prints nothing when all went well. A conflict, which a person has to merge, is a ``systemMessage`` for the person;
any other failure is one line on stderr, which Claude Code shows in its transcript view, so a hub that is down does not
interrupt every turn. Standard library and PyYAML only, like the rest of the client.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from evo_agents.hub.client import (
    FILE_MODE,
    Hub,
    HubError,
    NotSignedIn,
    Unreachable,
    hub_dir,
    load_credentials,
    write_atomic,
)

REQUEST_TIMEOUT = 5.0  # seconds for one request to the hub
BUDGETS = {"session-start": 20.0, "stop": 15.0}  # seconds for a whole run; hooks.json gives Claude Code's limit
LOCK_WAIT = 5.0  # seconds to wait for another memory sync of this machine
COOLDOWN = 120.0  # seconds Stop leaves a hub alone after it did not answer
STATE_FILE = "hook-state.json"
STATE_VERSION = 1
MAX_PAYLOAD = 1 << 20  # characters of the JSON Claude Code writes to a hook's stdin
MAX_LINE = 600  # characters of the line a hook prints
LOGIN_HINT = "`evo-agents hub login --url URL` signs in"
PULLED = ("pulled", "updated", "restored")  # what a pull wrote here


class OutOfTime(BaseException):
    """The run used its budget. A BaseException, so no ``except Exception`` on the way swallows it."""


@contextlib.contextmanager
def budget(seconds: float):
    """Raise OutOfTime in this thread after ``seconds``. Without SIGALRM, or off the main thread, nothing happens and
    the request timeouts and Claude Code's hook timeout bound the run."""
    if not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def expire(signum, frame):
        raise OutOfTime

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def read_payload(stream=None) -> dict:
    """The JSON object Claude Code writes to a hook's stdin; {} when there is none or it cannot be read."""
    stream = sys.stdin if stream is None else stream
    try:
        if stream is None or stream.isatty():
            return {}
        data = json.loads(stream.read(MAX_PAYLOAD) or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def session_dir(payload: dict) -> Path:
    """The session's directory: CLAUDE_PROJECT_DIR, where Claude Code started, which names its memory directory; else
    the cwd Claude Code handed the hook; else the hook's own."""
    for value in (os.environ.get("CLAUDE_PROJECT_DIR"), payload.get("cwd")):
        if isinstance(value, str) and value and os.path.isabs(value):
            return Path(value)
    return Path.cwd()


def _now() -> float:
    return time.time()


def one_line(text: str) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_LINE else text[: MAX_LINE - 3] + "..."


def _plural(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


# When a hub last failed to answer


class HookState:
    """~/.evo/hub/hook-state.json: per hub URL, when a hook last found it not answering. Advisory: a file that
    cannot be read counts as empty, and two hooks writing it at once leave one of their versions, whole."""

    def __init__(self, directory: Path):
        self.path = directory / STATE_FILE

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        down = data.get("down") if isinstance(data, dict) and data.get("version") == STATE_VERSION else None
        return {url: at for url, at in (down or {}).items() if isinstance(at, (int, float))}

    def _write(self, down: dict) -> None:
        with contextlib.suppress(OSError):  # only ever an optimization
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            text = json.dumps({"version": STATE_VERSION, "down": down}, indent=2, sort_keys=True) + "\n"
            write_atomic(self.path, text.encode(), FILE_MODE)

    def down_since(self, url: str, now: float) -> float | None:
        """When ``url`` last did not answer, if that is less than COOLDOWN seconds ago."""
        at = self._read().get(url)
        return at if at is not None and 0 <= now - at < COOLDOWN else None

    def mark_down(self, url: str, now: float) -> None:
        down = self._read()
        down[url] = now
        self._write(down)

    def mark_up(self, url: str) -> None:
        down = self._read()
        if down.pop(url, None) is not None:
            self._write(down)


# SessionStart


@dataclass
class Line:
    """The one line a hook prints, built as the run goes."""

    hub: str | None = None
    project: str | None = None
    parts: list[str] = field(default_factory=list)
    trouble: bool = False  # something a person should look at, besides the model

    def add(self, part: str, trouble: bool = False) -> None:
        self.parts.append(part)
        self.trouble = self.trouble or trouble

    def text(self) -> str:
        head = "evo-hub" + (f" {self.hub}" if self.hub else "")
        if self.hub:
            head += f", project {self.project}" if self.project else ", no hub project here (personal memories)"
        return one_line(f"{head}: {'; '.join(self.parts)}." if self.parts else f"{head}.")


def _ends_the_run(exc: BaseException) -> bool:
    """A failure every later request would meet too: the hub did not answer, or it refused the token."""
    return isinstance(exc, Unreachable) or (isinstance(exc, HubError) and exc.status == 401)


def _failure(what: str, exc: BaseException) -> str:
    """``what`` failed: the sentence of a HubError, which names no secret; the type of anything else."""
    reason = str(exc) if isinstance(exc, HubError) else f"unexpected {type(exc).__name__}"
    return f"{what}: {reason}"


def _memory_place(sync, where: Path) -> str | None:
    """The hub project the memories of ``where`` belong to; None for personal ones or when it is unknown."""
    try:
        place = sync.places.candidates(sync.target(where))[0]
    except (AttributeError, HubError):  # the places were never loaded, or two projects claim the directory
        return None
    return place.project if place.scope == "project" else None


def _pull_memories(hub: Hub, login: str, where: Path, line: Line) -> None:
    from evo_agents.hub.memory import MemorySync

    sync = MemorySync(hub, login, lock_timeout=LOCK_WAIT)
    try:
        report = sync.pull(where)
    except Exception as exc:
        if _ends_the_run(exc):
            raise
        line.project = line.project or _memory_place(sync, where)
        line.add(_failure("memories not pulled", exc), trouble=True)
        return
    line.project = line.project or _memory_place(sync, where)
    pulled = sum(report.counts.get(key, 0) for key in PULLED)
    line.add(f"{_plural(pulled, 'memory', 'memories')} pulled")
    if report.conflicts:
        line.add(
            f"{_plural(len(report.conflicts), 'memory conflict')}: the hub's version is kept under the name and this "
            "machine's next to it as a .conflict- copy, to merge and delete",
            trouble=True,
        )
    if report.errors:
        line.add(
            f"{_plural(len(report.errors), 'memory problem')} (`evo-agents hub memory pull` names them)",
            trouble=True,
        )


def _harness(where: Path, line: Line) -> tuple[Path, str] | None:
    """The harness around ``where`` and its hub.project, when it names one; read before any request, so the line
    names the project even when the hub does not answer."""
    from evo_agents.harness import find_manifest, hub_project, load_manifest

    try:
        root = find_manifest(where)
        project = hub_project(load_manifest(root)) if root is not None else None
    except Exception as exc:
        line.add(_failure("plans not exported, harness.yaml cannot be read", exc), trouble=True)
        return None
    if project is None:
        return None  # not in a harness whose plans the hub keeps
    line.project = project
    return root, project


def _export_plans(hub: Hub, root: Path, project: str, line: Line) -> None:
    from evo_agents.hub.mirror import export

    try:
        result = export(hub, root, project, keep_edited=True)
    except Exception as exc:
        if _ends_the_run(exc):
            raise
        line.add(_failure("plans not exported", exc), trouble=True)
        return
    written = sum(1 for p in result.plans if p["status"] == "written")
    kept = sum(1 for p in result.plans if p["status"] == "kept")
    done = [f"{_plural(written, 'plan copy', 'plan copies')} written"]
    if result.removed:
        done.append(f"{len(result.removed)} removed")
    line.add(", ".join(done) + f" of {_plural(len(result.plans), 'plan')} (no commit)")
    if kept:
        line.add(
            f"{_plural(kept, 'plan file')} edited outside the hub, left as {'it is' if kept == 1 else 'they are'} "
            "(`evo-agents harness validate` says how to push or restore them); mark steps with plan_step, never by "
            "editing plans/",
            trouble=True,
        )


def _check_skills(hub: Hub, line: Line) -> None:
    from evo_agents.hub.skill_sync import sync

    try:
        report = sync(hub, check=True)
    except Exception as exc:
        if _ends_the_run(exc):
            raise
        line.add(_failure("skills not checked", exc), trouble=True)
        return
    if report.errors:
        line.add(f"skills: {_plural(len(report.errors), 'error')} (`evo-agents hub skills sync --check` says why)")
    elif report.differences:
        line.add(f"{_plural(report.differences, 'skill')} to sync (`evo-agents hub skills sync`)")
    else:
        line.add("skills up to date")


def session_start(payload: dict, line: Line) -> None:
    """The SessionStart hook's work, its outcome told in ``line``."""
    try:
        credentials = load_credentials()
    except NotSignedIn:
        line.add(f"not signed in to a hub, so nothing was synced; {LOGIN_HINT}")
        return
    except HubError as exc:
        line.add(str(exc), trouble=True)
        return
    hub = Hub(credentials.url, credentials.token, timeout=REQUEST_TIMEOUT)
    line.hub = hub.url
    where = session_dir(payload)
    harness = _harness(where, line)
    state = HookState(hub_dir())
    try:
        _pull_memories(hub, credentials.login, where, line)
        if harness is not None:
            _export_plans(hub, *harness, line)
        _check_skills(hub, line)
    except Unreachable as exc:
        state.mark_down(hub.url, _now())
        line.add(
            f"the hub did not answer ({exc}), so the rest was not synced; memory files stay here and a later Stop "
            "pushes them",
            trouble=True,
        )
        return
    except HubError as exc:  # the token was refused
        line.add(
            f"the hub refused this machine's token ({exc}), so nothing more was synced; {LOGIN_HINT}", trouble=True
        )
        return
    state.mark_up(hub.url)


# Stop


@dataclass
class Outcome:
    """What Stop says: ``loud`` for the person (systemMessage), ``quiet`` for the transcript view (stderr)."""

    loud: str | None = None
    quiet: str | None = None


def stop(payload: dict, outcome: Outcome) -> None:
    """The Stop hook's work: push the memory files of the session's directory that changed."""
    from evo_agents.hub.memory import MemorySync

    try:
        credentials = load_credentials()
    except NotSignedIn:
        return
    except HubError as exc:
        outcome.quiet = f"evo-hub: {exc}"
        return
    hub = Hub(credentials.url, credentials.token, timeout=REQUEST_TIMEOUT)
    where = session_dir(payload)
    sync = MemorySync(hub, credentials.login, lock_timeout=LOCK_WAIT)
    try:
        if not sync.pending(where):
            return
    except HubError as exc:  # memory-state.json cannot be read
        outcome.quiet = f"evo-hub: memories not pushed: {exc}"
        return
    state = HookState(hub_dir())
    since = state.down_since(hub.url, _now())
    if since is not None:
        at = time.strftime("%H:%M", time.localtime(since))
        outcome.quiet = (
            f"evo-hub: {hub.url} did not answer at {at}, so memories were not pushed; they stay here for a later Stop"
        )
        return
    try:
        report = sync.push(where)
    except Unreachable as exc:
        state.mark_down(hub.url, _now())
        outcome.quiet = f"evo-hub: memories not pushed, the hub did not answer ({exc}); they stay here for a later Stop"
        return
    except HubError as exc:
        outcome.quiet = f"evo-hub: memories not pushed: {exc}"
        return
    state.mark_up(hub.url)
    if report.conflicts:
        outcome.loud = (
            f"evo-hub: {_plural(len(report.conflicts), 'memory conflict')} on {hub.url}: the hub's version is kept "
            "under the name and this machine's next to it as a .conflict- copy, to merge and delete"
        )
    if report.errors:
        outcome.quiet = (
            f"evo-hub: {_plural(len(report.errors), 'memory problem')} in the push (`evo-agents hub memory push` "
            "names them)"
        )


# Commands


def _write(stream, text: str) -> None:
    with contextlib.suppress(OSError, ValueError):  # a closed stdout or stderr must not fail the hook either
        print(text, file=stream, flush=True)


def cmd_hook_session_start(args) -> int:
    line = Line()
    seconds = BUDGETS["session-start"]
    try:
        with budget(seconds):
            session_start(read_payload(), line)
    except OutOfTime:
        line.add(f"stopped after {seconds:g}s with what was done; the rest waits for the next session", trouble=True)
    except BaseException as exc:  # a hook never fails the session
        line.add(_failure("the SessionStart hook failed", exc), trouble=True)
    text = line.text()
    output: dict = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
    if line.trouble:
        output["systemMessage"] = text
    _write(sys.stdout, json.dumps(output, ensure_ascii=False))
    return 0


def cmd_hook_stop(args) -> int:
    outcome = Outcome()
    seconds = BUDGETS["stop"]
    try:
        with budget(seconds):
            stop(read_payload(), outcome)
    except OutOfTime:
        outcome.quiet = f"evo-hub: memory push stopped after {seconds:g}s; what is left goes at a later Stop"
    except BaseException as exc:  # a hook never fails the session
        outcome.quiet = "evo-hub: " + _failure("the Stop hook failed", exc)
    if outcome.loud:
        _write(sys.stdout, json.dumps({"systemMessage": one_line(outcome.loud)}, ensure_ascii=False))
    if outcome.quiet:
        _write(sys.stderr, one_line(outcome.quiet))
    return 0


def register_hooks(hsub) -> None:
    hook = hsub.add_parser("hook", help="Claude Code hook entry points of the evo-hub plugin; they always exit 0")
    ksub = hook.add_subparsers(dest="hook_name", required=True)
    ksub.add_parser(
        "session-start",
        help="pull the memories of the session's directory, export the plans of its harness, count the skills to "
        "sync, and print one line for the session",
    ).set_defaults(func=cmd_hook_session_start)
    ksub.add_parser(
        "stop", help="push the memory files of the session's directory that changed since the last sync"
    ).set_defaults(func=cmd_hook_stop)
