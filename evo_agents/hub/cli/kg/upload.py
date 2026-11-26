"""``evo-agents hub kg upload`` and ``hub kg uploads``: send an archive as a source of a project's graph, and list
what was sent.

``upload FILE --project P [--source NAME] [--label L]`` sends a .zip, .tar, .tar.gz or .tgz (the writer role);
``--new-project P`` in place of ``--project P`` has the hub create P first, with no harness, as its creator's
(``evo_agents.hub.server.kg_upload_project``), which takes what registering a project takes. It
hashes the file, asks the hub to start the upload (POST /v1/kg/{project}/uploads), which checks the size, the role and
the label against what the project's hub sink clears before any byte of the archive leaves this machine, then PUTs
the parts the hub does not hold yet straight to the blob store, PARALLEL at a time, each through a presigned URL and
tried PUT_ATTEMPTS times, and asks the hub to join them (``evo_agents.hub.kg_routes.Uploads.send``). Run again after an
interruption, it sends only the parts that are missing. Then it follows the flow after the upload (GET
/v1/kg/{project}/uploads/{id}/pipeline, ``evo_agents.hub.server.kg_pipeline``) every POLL_SECONDS: it prints
"Knowledge graph build in progress" with the phase each time the phase or what it waits for changes, and "Knowledge
graph ready" once the Curator's kg_init run after the build (and the rebuild it queued) ended, exit 0; a phase that
failed is printed with its reason, exit 1. ``--no-wait`` returns once the parts are joined, as before. ``pipeline
--project P [--upload ID]`` prints where the flow of an upload (the newest without ``--upload``) stands, once.
``uploads --project P`` lists the project's uploads with their state, size, the files read and skipped and the build
that followed. ``--json`` prints the hub's answer. Standard library only.
"""

from __future__ import annotations

import sys
import time  # noqa: F401  tests slow the follow down through upload.time.sleep
from collections.abc import Callable
from pathlib import Path
from typing import Any

from evo_agents.clikit import NOTHING, Arg, Result, Text, View, returns_object
from evo_agents.hub import kg_routes
from evo_agents.hub.cli.base import HubCommand, HubContext
from evo_agents.hub.kg_routes import Uploads

__all__ = ["KG_PIPELINE", "KG_UPLOAD", "KG_UPLOADS", "follow", "progress_line"]

SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz")
UPLOAD = (
    "id",
    "source",
    "name",
    "state",
    "size",
    "label",
    "sender",
    "created_at",
    "uploaded_at",
    "ended_at",
    "files",
    "skipped",
    "assets",
    "paths",
    "build_id",
    "build_status",
    "error",
    "replaced_by",
)
SENT = ("sent", "kept")  # the parts this run sent, and those the hub already held
PIPELINE = (
    "upload_id",
    "project",
    "state",
    "phase",
    "phases",
    "error",
    "build_id",
    "init_id",
    "rebuild_id",
    "told_at",
)
POLL_SECONDS = 15.0  # between two reads of the flow while ``upload`` waits
ENDED = ("ready", "failed", "replaced")
HUB_PROJECT = "project name on the hub"


def _bytes(size: int) -> str:
    for unit, scale in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} B"


def _phases(found: dict) -> str:
    return ", ".join(f"{p['name']} {p['state']}" for p in found["phases"])


def _current(found: dict) -> dict | None:
    return next((p for p in found["phases"] if p["name"] == found["phase"]), None)


def progress_line(found: dict) -> str:
    """The line printed for ``found``, the flow as the hub answers it."""
    current = _current(found)
    detail = f": {current['detail']}" if current and current.get("detail") else ""
    if found["state"] == "running":
        return f"Knowledge graph build in progress: {found['phase']}{detail}"
    if found["state"] == "failed":
        return f"Knowledge graph build failed at {found['phase']}: {found['error'] or 'no reason given'}"
    if found["state"] == "replaced":
        return f"upload {found['upload_id']} was replaced by a later upload of its source, whose flow goes on"
    return f"Knowledge graph ready ({_phases(found)})"


def follow(
    uploads: Uploads,
    upload_id: int,
    say: Callable[[str], Any],
    *,
    poll: float | None = None,
    sleep: Callable[[float], Any] | None = None,
) -> dict:
    """Read the flow after upload ``upload_id`` until it ends, calling ``say`` with each line that changed; its end.
    A read the hub did not answer (``client.transient``: a redeploy, a proxy's 502) is read again, not an end."""
    return kg_routes.follow(
        lambda: uploads.pipeline(upload_id),
        progress_line,
        lambda found: found["state"] in ENDED,
        say,
        what="the flow",
        poll=POLL_SECONDS if poll is None else poll,
        sleep=sleep,
    )


def _exit(found: dict | None) -> int:
    return 1 if found is not None and found["state"] == "failed" else 0


def _stderr(line: str) -> None:
    print(line, file=sys.stderr)


class KgUpload(HubCommand):
    """Streams: the upload's line and the flow's lines are printed as they come."""

    name = "upload"
    help = "send an archive (.zip, .tar, .tar.gz, .tgz) as a source of the graph"
    arguments = (Arg("file", help="the archive"),)
    output = returns_object(*UPLOAD, *SENT, "pipeline", schema="KgUpload", added=(*SENT, "pipeline"))
    json_help = (
        "machine-readable output: the upload, with the parts sent and kept and the flow's end (null with --no-wait)"
    )

    def add_arguments(self, parser) -> None:
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("--project", help=HUB_PROJECT)
        target.add_argument(
            "--new-project",
            metavar="PROJECT",
            help="create this project, with no harness, and upload into it (a hub admin, or an unused register right)",
        )
        parser.add_argument("--source", help="the source it becomes (default: the file's name without its suffix)")
        parser.add_argument(
            "--label",
            help="a level of the project (default: the project's default label; internal for a new project)",
        )
        parser.add_argument(
            "--no-wait",
            action="store_true",
            help="return once the parts are joined, without following the build, starter questions and Curator after it",
        )

    def check(self, args) -> None:
        path = Path(args.file)
        if not path.is_file():
            sys.exit(f"no file {args.file}")
        if not path.name.lower().endswith(SUFFIXES):
            sys.exit(f"{path.name} is not an archive the hub reads: {', '.join(SUFFIXES)}")

    def run(self, ctx: HubContext) -> Result:
        args, path = ctx.args, Path(ctx.args.file)
        project = args.new_project or args.project
        uploads = Uploads(ctx.hub, project)
        done, sent, kept = uploads.send(
            path,
            source=args.source,
            label=args.label,
            new_project=bool(args.new_project),
            progress=None if args.json else _stderr,
        )
        if not args.json:
            made = f"new project {project}" if args.new_project else f"project {project}"
            print(
                f"uploaded {path.name} ({_bytes(done['size'])}) to {made} as source {done['source']}: "
                f"upload {done['id']} is {done['state']}",
                file=ctx.out,
            )
            if args.new_project:
                _stderr(f"an interrupted upload continues with --project {project}")
        if done["state"] == "failed":
            return Result({**done, "sent": sent, "kept": kept, "pipeline": None}, NOTHING, 1)
        found = None
        view: View = NOTHING
        if not args.no_wait:
            found = follow(uploads, done["id"], _stderr if args.json else self.streamed(ctx))
            if found["state"] == "ready":
                view = Text(f"the graph: {uploads.web()}")
        return Result({**done, "sent": sent, "kept": kept, "pipeline": found}, view, _exit(found))

    @staticmethod
    def streamed(ctx: HubContext) -> Callable[[str], None]:
        return lambda line: print(line, file=ctx.out, flush=True)


class KgPipeline(HubCommand):
    name = "pipeline"
    help = "where the flow after an upload stands: upload, extract, build, entities, questions, Curator"
    arguments = (
        Arg("--project", required=True, help=HUB_PROJECT),
        Arg("--upload", type=int, help="the upload, as `hub kg uploads` lists it (default: the newest)"),
    )
    output = returns_object(*PIPELINE, schema="KgPipeline")

    def run(self, ctx: HubContext) -> Result:
        uploads = Uploads(ctx.hub, ctx.project)
        upload_id = ctx.args.upload
        if upload_id is None:
            listed = uploads.list(1)["uploads"]
            if not listed:
                sys.exit(f"project {ctx.project} has no upload your grant sees")
            upload_id = listed[0]["id"]
        found = uploads.pipeline(upload_id)
        lines = [f"upload {found['upload_id']}: {progress_line(found)}"]
        for phase in found["phases"]:
            detail = f"  {phase['detail']}" if phase.get("detail") else ""
            lines.append(f"  {phase['name']:<10} {phase['state']:<8}{detail}")
        return Result(found, Text(*lines), _exit(found))


def _skipped(skipped: dict) -> str:
    return ", ".join(f"{reason} {count}" for reason, count in sorted(skipped.items())) or "-"


def _upload_lines(item: dict) -> list[str]:
    files = "-" if item["files"] is None else str(item["files"])
    build = "-" if item["build_id"] is None else f"#{item['build_id']} {item['build_status'] or ''}".strip()
    line = (
        f"#{item['id']} {item['source']} {item['state']} {_bytes(item['size'])} files {files} "
        f"skipped {_skipped(item['skipped'])} build {build}"
    )
    return [line, f"  {item['error']}"] if item.get("error") else [line]


class KgUploads(HubCommand):
    name = "uploads"
    help = "list a project's archive uploads"
    arguments = (
        Arg("--project", required=True, help=HUB_PROJECT),
        Arg("--limit", type=int, default=50, help="the newest uploads listed (1 to 200)"),
    )
    output = returns_object("uploads", schema="KgUploadList")

    def run(self, ctx: HubContext) -> Result:
        found = Uploads(ctx.hub, ctx.project).list(ctx.args.limit)
        if not found["uploads"]:
            return Result(found, Text(f"project {ctx.project} has no upload yet"))
        return Result(found, Text(*(line for item in found["uploads"] for line in _upload_lines(item))))


KG_UPLOAD = KgUpload()
KG_PIPELINE = KgPipeline()
KG_UPLOADS = KgUploads()
