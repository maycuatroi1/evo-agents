"""The run events src/components/runs/trace-model.test.ts reads, made on this machine and never taken from a hub.

    PYTHONPATH=.. python3 scripts/trace_fixtures.py --runtime fake --out src/test/fixtures/trace/fake-claude-code.json
    PYTHONPATH=.. python3 scripts/trace_fixtures.py --runtime claude-code --out src/test/fixtures/trace/claude-code.json

``--runtime fake`` translates the samples of Claude Code's stream-json that tests/worker/fake_adapter.py holds, the
way that adapter does, and spaces them two seconds apart from a fixed time. Any other runtime runs its adapter for real,
as ``evo-agents worker selftest`` does, in a scratch git repository that is removed afterwards, on a prompt that makes
the agent run a command that fails, run one that succeeds, write a file and answer; with ``--two-turns`` (Claude Code)
the agent also starts a command in the background and ends its turn, so the run has two turns and two usage reports.
A real run spends a little of the owner's quota.

The events are what the adapter emitted, numbered from 3 and timed by this machine's clock as they arrived, between
the state events the hub writes (queued to leased to running before, running to verifying to done or failed after).
``usage`` is the outcome's usage, which the worker reports as the run's. Before writing, paths of the scratch
repository and of the home directory are replaced, and an ``output`` event keeps only the few keys that name what
happened (type, subtype, status, method and the like): the runtime's init, hook output and rate limits stay out, so
nothing of the machine's configuration reaches the file. Read the file before committing it.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

FIXED_START = dt.datetime(2026, 10, 7, 3, 0, 0, tzinfo=dt.UTC)
PROMPT = (
    "This is a self-test of an evo-agents worker. Do these in order, each with your shell tool: "
    "1. run `cat missing-file.txt` (it fails; that is expected, go on). "
    "2. run `ls -a`. "
    "3. create a file named selftest.txt in the current directory, containing exactly the word ok. "
    "Then reply with one short sentence saying what you did."
)
TWO_TURNS_PROMPT = (
    "This is a self-test of an evo-agents worker. Do these in order with the Bash tool: "
    "1. run `cat missing-file.txt` (it fails; that is expected, go on). "
    "2. run the command `sleep 15 && date -u` with run_in_background set to true. Do not wait for it and do not check "
    "on it: once it has started, end your turn by replying STARTED, without creating any file. "
    "When you are told that it has finished, create a file named selftest.txt in the current directory, containing "
    "exactly the word ok, then reply with one short sentence saying what you did."
)
# What an output event keeps of its raw runtime event.
OUTPUT_KEYS = ("type", "subtype", "status", "method", "task_type", "description", "hook_name", "exit_code", "outcome")
TIMEOUT = 300.0


def iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def state(seq: int, at: dt.datetime, before: str | None, after: str, reason: str) -> dict:
    body = {"to": after, "from": before, "actor": "worker", "reason": reason}
    return {"seq": seq, "at": iso(at), "kind": "state", "body": body, "truncated": False}


class Scrub:
    """Replaces the scratch repository's path, the home directory and the login in every string of a body."""

    def __init__(self, repo: Path | None) -> None:
        pairs = []
        if repo is not None:
            pairs += [
                (str(repo), "/tmp/evo-worker-selftest"),
                (str(repo).replace("/private", "", 1), "/tmp/evo-worker-selftest"),
            ]
        home = str(Path.home())
        pairs += [(home, "/Users/dev"), (os.environ.get("USER") or "\0", "dev")]
        self.pairs = [(old, new) for old, new in pairs if old and old != "\0"]

    def __call__(self, value):
        if isinstance(value, str):
            for old, new in self.pairs:
                value = value.replace(old, new)
            # Claude Code's own directory of a session's tasks, named after the user id and the mangled scratch path.
            value = re.sub(r"(?:/private)?/tmp/claude-\d+/[^/\s]+", "/tmp/claude/session", value)
            return re.sub(r"/var/folders/[^\s\"']+", "/tmp/evo-worker-selftest", value)
        if isinstance(value, list):
            return [self(item) for item in value]
        if isinstance(value, dict):
            return {key: self(item) for key, item in value.items()}
        return value


def keep_output(body: dict) -> dict:
    found = body.get("raw")
    if not isinstance(found, dict):
        return {"raw": {}}
    return {
        "raw": {key: found[key] for key in OUTPUT_KEYS if key in found and isinstance(found[key], (str, int, bool))}
    }


def fake_events() -> tuple[list[tuple[float, str, dict]], dict | None]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests.worker.fake_adapter import SAMPLES, translate

    found = []
    for index, sample in enumerate(SAMPLES):
        for event in translate(sample):
            found.append((index * 2.0, event.kind, event.body))
    return found, None


async def real_events(runtime: str, model: str | None, effort: str | None, two_turns: bool):
    from evo_agents.worker.adapter import RunContext, load_adapters
    from evo_agents.worker.selftest import scratch_repo

    cls = load_adapters().get(runtime)
    if cls is None:
        raise SystemExit(f"error: no adapter for {runtime}")
    detection = cls.detect()
    if not detection.available:
        raise SystemExit(f"error: {runtime} is unavailable here: {detection.reason}")
    repo = scratch_repo()
    run = {"id": 0, "project": "selftest", "plan_id": "selftest", "step_key": "selftest", "title": "trace fixture"}
    run.update({"runtime": runtime, "mode": "headless", "model": model, "effort": effort})
    env = {**os.environ, "EVO_RUN_ID": "0", "EVO_RUN_PROJECT": "selftest", "EVO_RUN_PLAN": "selftest"}
    adapter = cls(RunContext(run=run, worktree=repo, prompt=TWO_TURNS_PROMPT if two_turns else PROMPT, env=env))
    found: list[tuple[float, str, dict]] = []
    started = time.time()
    try:
        await adapter.start()

        async def pump() -> None:
            async for event in adapter.events():
                found.append((time.time() - started, event.kind, event.body))
                print(f"  {event.kind}", file=sys.stderr, flush=True)

        reader = asyncio.create_task(pump())
        done, _ = await asyncio.wait({reader}, timeout=TIMEOUT)
        if reader not in done:
            await adapter.interrupt()
            await asyncio.wait({reader}, timeout=60)
        outcome = await asyncio.wait_for(adapter.wait(), 60)
        print(f"{runtime}: completed={outcome.completed} error={outcome.error}", file=sys.stderr)
        return found, outcome.usage, outcome.completed, repo, f"{runtime} {detection.version}"
    finally:
        shutil.rmtree(repo, ignore_errors=True)


def build(found, usage, completed: bool, runtime: str, version: str, repo: Path | None, about: str) -> dict:
    scrub = Scrub(repo)
    events = [
        state(1, FIXED_START, "queued", "leased", "worker dev-laptop claimed it"),
        state(2, FIXED_START + dt.timedelta(seconds=1), "leased", "running", "worker dev-laptop reported running"),
    ]
    start = FIXED_START + dt.timedelta(seconds=2)
    last = start
    for offset, kind, body in found:
        at = start + dt.timedelta(seconds=round(offset, 3))
        last = max(last, at)
        body = keep_output(body) if kind == "output" else scrub(json.loads(json.dumps(body, default=str)))
        events.append({"seq": len(events) + 1, "at": iso(at), "kind": kind, "body": body, "truncated": False})
    end = last + dt.timedelta(seconds=1)
    events.append(state(len(events) + 1, end, "running", "verifying", "worker dev-laptop reported verifying"))
    final = "done" if completed else "failed"
    events.append(
        state(len(events) + 1, end + dt.timedelta(seconds=1), "verifying", final, f"worker dev-laptop reported {final}")
    )
    return {"_about": about, "runtime": runtime, "version": version, "usage": scrub(usage), "events": events}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runtime", required=True, choices=["fake", "claude-code", "opencode", "codex"])
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--two-turns", action="store_true", help="Claude Code: a background command and a second turn")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.runtime == "fake":
        found, usage = fake_events()
        about = (
            "tests/worker/fake_adapter.py's samples of Claude Code's stream-json, translated as that adapter does, two "
            "seconds apart, between the state events the hub writes. Made by web/scripts/trace_fixtures.py."
        )
        data = build(found, usage, True, "claude-code", "fake", None, about)
    else:
        effort = (
            args.effort if args.effort is not None else ("low" if args.runtime in ("claude-code", "codex") else None)
        )
        found, usage, completed, repo, version = asyncio.run(
            real_events(args.runtime, args.model, effort or None, args.two_turns)
        )
        about = (
            f"A real run of the {args.runtime} adapter on this repository's code ({version}) in a scratch git "
            f"repository, on the prompt of web/scripts/trace_fixtures.py{' with two turns' if args.two_turns else ''}, "
            "between the "
            "state events the hub writes; times are the machine's as the events arrived, from a fixed start. Paths are "
            "replaced and output events keep only the keys that name what happened. Made by "
            "web/scripts/trace_fixtures.py."
        )
        data = build(found, usage, completed, args.runtime, version, repo, about)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"Wrote {len(data['events'])} events to {args.out}.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
