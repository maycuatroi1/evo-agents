"""``evo-agents worker output FILE... [--caption TEXT]``: the command of a run's agent, of any kind and runtime, that
sends files to the run's outputs on the hub (source sent), for the people who read the run: a screenshot, a video, a
report. And ``--attach FILE`` of ``worker ask`` and ``worker chat post`` (``attach_option``, ``attached``): the files
a question or a message of the agent carries, sent the same way.

Each reads the run from EVO_RUN_ID, EVO_WORKER_HOME and the run's record there, and refuses outside a run of this
worker. It checks each file here (a regular file, a name the hub takes, at most the hub's limit of a file) and asks
the hub, with the worker's token, how much the run may still take (GET /v1/worker/runs/{id}/output-settings): a file
past a limit is one ``error:`` line naming it and exit status 1, and nothing is sent. A hub that does not answer
then is no refusal: the worker sends the files once it does. Then it puts a copy of each in the run's queue
(``output_queue``), which the daemon sends in the background, a file of text redacted as the run's events are
(``runner.outputs``): ``output`` returns at once, while ``--attach`` waits, at most ATTACH_WAIT seconds, for the
outputs the hub recorded, whose ids the question or the message names (``output_ids``). ``request_scan`` has the
daemon look at the run's .evo-run/outputs/ (``worker step``). A failure is one ``error:`` line on stderr and exit
status 1; the check with the hub needs the worker extra, and without it the files go unchecked.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from evo_agents.hub import outputs
from evo_agents.hub.client import HubError
from evo_agents.worker.chat_cli import running_run
from evo_agents.worker.home import WorkerHome, WorkerStateError
from evo_agents.worker.output_queue import OutputQueue

EXIT_FAILED = 1
ATTACH_WAIT = 110.0  # seconds an attachment waits for the worker to send it, within the 2 minutes of a Bash call
WAIT_STEP = 0.1


@dataclass(frozen=True)
class File:
    path: Path
    name: str
    media_type: str
    size: int


def output_command(func):
    """Turn the failures of ``func`` into an ``error:`` line and an exit status."""

    @functools.wraps(func)
    def run(args) -> int:
        try:
            return func(args)
        except (HubError, WorkerStateError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_FAILED

    return run


def checked_files(paths: list[str], most: int) -> list[File]:
    """The files of ``paths``, at most ``most`` of them, each a regular file of at most MAX_FILE_BYTES with a name the
    hub takes; WorkerStateError naming the first that is not."""
    if not paths or len(paths) > most:
        raise WorkerStateError(f"give 1 to {most} files")
    found = []
    for raw in paths:
        path = Path(raw)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise WorkerStateError(f"cannot read {raw}: {exc.strerror or exc}") from None
        if not path.is_file():
            raise WorkerStateError(f"{raw} is not a file")
        if size > outputs.MAX_FILE_BYTES:
            raise WorkerStateError(f"{raw} has {size} bytes, and an output is at most {outputs.MAX_FILE_BYTES} bytes")
        try:
            name = outputs.check_name(path.name)
        except ValueError as exc:
            raise WorkerStateError(f"{raw}: {exc}") from None
        found.append(File(path, name, outputs.guess_media_type(name), size))
    return found


def _settings(home: WorkerHome, run_id: int) -> dict | None:
    """What the hub says of the run's outputs (output-settings); None when it does not answer or the worker extra is
    missing; HubError when it refuses."""
    try:
        from evo_agents.worker import hubapi
    except ImportError:
        return None
    config, token = home.load_config(), home.load_token()

    async def go() -> dict:
        async with hubapi.new_session() as session:
            return await hubapi.WorkerHub(config.url, token, session).output_settings(run_id)

    try:
        return asyncio.run(go())
    except hubapi.Unreachable:
        return None
    except hubapi.HubProblem as exc:
        raise HubError(f"the hub takes no outputs of run {run_id}: {exc}", exc.status, exc.code) from None


def check_room(home: WorkerHome, run_id: int, files: list[File]) -> None:
    """WorkerStateError when ``files`` would take the run past a limit the hub names; nothing when it does not
    answer, as the worker sends them once it does."""
    settings = _settings(home, run_id)
    if settings is None:
        print("warning: the hub did not answer; the worker sends the files once it does", file=sys.stderr)
        return
    most_bytes = int(settings.get("max_file_bytes") or outputs.MAX_FILE_BYTES)
    big = next((file for file in files if file.size > most_bytes), None)
    if big is not None:
        raise WorkerStateError(f"{big.path} has {big.size} bytes, and an output is at most {most_bytes} bytes")
    count, held = int(settings.get("outputs") or 0), int(settings.get("bytes") or 0)
    most_outputs = int(settings.get("max_outputs") or outputs.MAX_RUN_OUTPUTS)
    if count + len(files) > most_outputs:
        raise WorkerStateError(f"run {run_id} holds {count} outputs, and a run holds at most {most_outputs}")
    most_run = int(settings.get("max_run_bytes") or outputs.MAX_RUN_BYTES)
    if held + sum(file.size for file in files) > most_run:
        raise WorkerStateError(f"run {run_id} holds {held} bytes of outputs, and a run holds at most {most_run}")


def request_scan(home: WorkerHome, run_id: int) -> None:
    """Have the daemon send what the run's .evo-run/outputs/ holds that it has not sent: ``worker step`` reports a
    step."""
    with contextlib.suppress(OSError):
        OutputQueue(home, run_id).put_scan()


@output_command
def cmd_output(args) -> int:
    run_id, home, _ = running_run("output")
    caption = _caption(args.caption)
    files = checked_files(args.files, outputs.MAX_BATCH)
    check_room(home, run_id, files)
    queue = OutputQueue(home, run_id)
    for file in files:
        queue.put_file(file.path, name=file.name, media_type=file.media_type, caption=caption)
    names = ", ".join(file.name for file in files)
    print(
        f"Sending {len(files)} file(s) to the outputs of run #{run_id} on the hub: {names}. The worker uploads them "
        "in the background; the run's page shows them once the hub has them."
    )
    return 0


def _caption(value: str | None) -> str | None:
    try:
        return outputs.check_caption(value)
    except ValueError as exc:
        raise WorkerStateError(str(exc)) from None


def attached(home: WorkerHome, run_id: int, paths: list[str] | None) -> dict:
    """``{"output_ids": [...]}`` of the files of ``paths``, once the worker sent them as outputs of the run; ``{}``
    for none. WorkerStateError when one is refused, or not sent within ATTACH_WAIT seconds."""
    if not paths:
        return {}
    files = checked_files(paths, outputs.MAX_ATTACHED)
    check_room(home, run_id, files)
    queue = OutputQueue(home, run_id)
    waiting = {queue.put_file(f.path, name=f.name, media_type=f.media_type, wait=True): f for f in files}
    found: dict[str, int] = {}
    deadline = time.monotonic() + ATTACH_WAIT
    while len(found) < len(waiting):
        for item_id, file in waiting.items():
            answer = None if item_id in found else queue.answer(item_id)
            if answer is None:
                continue
            if not isinstance(answer.get("output_id"), int):
                raise WorkerStateError(f"{file.path} was not attached: {answer.get('error') or 'the hub took none'}")
            found[item_id] = answer["output_id"]
        if len(found) < len(waiting) and time.monotonic() > deadline:
            raise WorkerStateError(
                f"the worker did not send the attachments within {ATTACH_WAIT:g}s (the hub did not answer, or the run "
                "ended); it sends them to the run's outputs later: ask again without --attach"
            )
        time.sleep(WAIT_STEP)
    return {"output_ids": [found[item_id] for item_id in waiting]}


def attach_option(parser, what: str) -> None:
    """``--attach FILE`` on ``parser``, the files ``what`` carries."""
    parser.add_argument(
        "--attach",
        action="append",
        metavar="FILE",
        help=f"a file {what} carries, a screenshot or a report, sent to the run's outputs first; repeat it, at most "
        f"{outputs.MAX_ATTACHED}",
    )


def register_output(wsub) -> None:
    """``evo-agents worker output``, under the parser ``wsub`` of ``evo-agents worker``."""
    output = wsub.add_parser(
        "output",
        help="inside a run: send files to the run's outputs on the hub, for the people who read the run (a "
        "screenshot, a video, a report); the worker uploads them in the background",
    )
    output.add_argument("files", nargs="+", metavar="FILE", help=f"a file of at most {outputs.MAX_FILE_BYTES} bytes")
    output.add_argument(
        "--caption", metavar="TEXT", help=f"what the files show, at most {outputs.MAX_CAPTION_CHARS} characters"
    )
    output.set_defaults(func=cmd_output)
