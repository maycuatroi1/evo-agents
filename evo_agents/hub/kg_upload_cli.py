"""``evo-agents hub kg upload`` and ``hub kg uploads``: send an archive as a source of a project's graph, and list
what was sent.

``upload FILE --project P [--source NAME] [--label L]`` sends a .zip, .tar, .tar.gz or .tgz (the writer role);
``--new-project P`` in place of ``--project P`` has the hub create P first, with no harness, as its creator's
(``evo_agents.hub.server.kg_upload_project``), which takes what registering a project takes. It
hashes the file, asks the hub to start the upload (POST /v1/kg/{project}/uploads), which checks the size, the role and
the label against what the project's hub sink clears before any byte of the archive leaves this machine, then PUTs
the parts the hub does not hold yet straight to the blob store, PARALLEL at a time, each through a presigned URL and
tried PUT_ATTEMPTS times, and asks the hub to join them (``evo_agents.hub.server.kg_uploads``). Run again after an
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

import hashlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from evo_agents.hub.cli_client import client_command, print_json, signed_in
from evo_agents.hub.client import Hub, HubError, put_presigned_part
from evo_agents.hub.contract import json_option, returns_object
from evo_agents.hub.kg_push import PUT_ATTEMPTS, PUT_DELAYS, transient

SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz")
PARALLEL = 4  # parts sent at once
URLS_AT_ONCE = 2 * PARALLEL  # parts whose URLs one call asks for: each works 15 minutes, so ask as they are needed
HASH_CHUNK = 4 * 1024 * 1024
PART_TIMEOUT = 600.0  # seconds for one part's PUT
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


def _base(project: str) -> str:
    return f"/v1/kg/{quote(project, safe='')}/uploads"


def _bytes(size: int) -> str:
    for unit, scale in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if size >= scale:
            return f"{size / scale:.1f} {unit}"
    return f"{size} B"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _put(url: str, path: Path, offset: int, size: int) -> None:
    """PUT one part, PUT_ATTEMPTS times at most while it fails with ``transient``."""
    for number in range(1, PUT_ATTEMPTS + 1):
        try:
            put_presigned_part(url, path, offset, size, PART_TIMEOUT)
            return
        except HubError as exc:
            if number == PUT_ATTEMPTS or not transient(exc):
                raise
            time.sleep(PUT_DELAYS[min(number - 1, len(PUT_DELAYS) - 1)])


def upload(
    hub: Hub,
    project: str,
    path: Path,
    *,
    source: str | None = None,
    label: str | None = None,
    new_project: bool = False,
    progress=None,
):
    """Send the archive at ``path``, into project ``project`` the hub first creates with ``new_project``; (the hub's
    answer for the upload once joined, parts sent, parts kept)."""
    say = progress or (lambda line: None)
    size = path.stat().st_size
    ask = {"name": path.name, "size": size, "sha256": file_sha256(path)}
    if source:
        ask["source"] = source
    if label:
        ask["label"] = label
    if new_project:
        ask["new_project"] = True
    started = hub.call("POST", _base(project), ask)
    upload_id, each = started["id"], started["part_size"]
    kept = set(started["done"])
    missing = [n for n in range(1, started["parts"] + 1) if n not in kept]
    if kept:
        say(f"upload {upload_id} resumed: {len(kept)} of {started['parts']} parts are on the hub already")
    path_of = f"{_base(project)}/{upload_id}"
    with ThreadPoolExecutor(max_workers=PARALLEL, thread_name_prefix="kg-upload") as pool:
        for start in range(0, len(missing), URLS_AT_ONCE):
            batch = missing[start : start + URLS_AT_ONCE]
            parts = hub.call("POST", f"{path_of}/parts", {"numbers": batch})["parts"]
            futures = [pool.submit(_put, p["url"], path, (p["number"] - 1) * each, p["size"]) for p in parts]
            for future in futures:
                future.result()
            say(f"sent {start + len(batch)} of {len(missing)} parts")
    done = hub.call("POST", f"{path_of}/complete", {}) or {}
    return done, len(missing), len(kept)


def pipeline_of(hub: Hub, project: str, upload_id: int) -> dict:
    return hub.call("GET", f"{_base(project)}/{upload_id}/pipeline") or {}


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


def follow(hub: Hub, project: str, upload_id: int, say, *, poll: float | None = None, sleep=None) -> dict:
    """Read the flow after upload ``upload_id`` until it ends, calling ``say`` with each line that changed; its end."""
    last = None
    while True:
        found = pipeline_of(hub, project, upload_id)
        line = progress_line(found)
        if line != last:
            say(line)
            last = line
        if found["state"] in ENDED:
            return found
        (sleep or time.sleep)(POLL_SECONDS if poll is None else poll)


def _exit(found: dict | None) -> int:
    return 1 if found is not None and found["state"] == "failed" else 0


@client_command
def cmd_upload(args) -> int:
    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"no file {args.file}")
    if not path.name.lower().endswith(SUFFIXES):
        sys.exit(f"{path.name} is not an archive the hub reads: {', '.join(SUFFIXES)}")
    hub, _ = signed_in()
    progress = None if args.json else (lambda line: print(line, file=sys.stderr))
    project = args.new_project or args.project
    done, sent, kept = upload(
        hub, project, path, source=args.source, label=args.label, new_project=bool(args.new_project), progress=progress
    )
    found = None
    if not args.json:
        made = f"new project {project}" if args.new_project else f"project {project}"
        print(
            f"uploaded {path.name} ({_bytes(done['size'])}) to {made} as source {done['source']}: "
            f"upload {done['id']} is {done['state']}"
        )
        if args.new_project:
            print(f"an interrupted upload continues with --project {project}", file=sys.stderr)
    if done["state"] == "failed":
        if args.json:
            print_json({**done, "sent": sent, "kept": kept, "pipeline": None})
        return 1
    if not args.no_wait:
        say = (lambda line: print(line, file=sys.stderr)) if args.json else (lambda line: print(line, flush=True))
        found = follow(hub, project, done["id"], say)
        if found["state"] == "ready" and not args.json:
            print(f"the graph: {hub.url}/p/{quote(project, safe='')}/kg")
    if args.json:
        print_json({**done, "sent": sent, "kept": kept, "pipeline": found})
    return _exit(found)


@client_command
def cmd_pipeline(args) -> int:
    hub, _ = signed_in()
    upload_id = args.upload
    if upload_id is None:
        listed = (hub.call("GET", f"{_base(args.project)}?limit=1") or {})["uploads"]
        if not listed:
            sys.exit(f"project {args.project} has no upload your grant sees")
        upload_id = listed[0]["id"]
    found = pipeline_of(hub, args.project, upload_id)
    if args.json:
        print_json(found)
        return _exit(found)
    print(f"upload {found['upload_id']}: {progress_line(found)}")
    for phase in found["phases"]:
        detail = f"  {phase['detail']}" if phase.get("detail") else ""
        print(f"  {phase['name']:<10} {phase['state']:<8}{detail}")
    return _exit(found)


def _skipped(skipped: dict) -> str:
    return ", ".join(f"{reason} {count}" for reason, count in sorted(skipped.items())) or "-"


@client_command
def cmd_uploads(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", f"{_base(args.project)}?limit={args.limit}") or {}
    if args.json:
        print_json(found)
        return 0
    if not found["uploads"]:
        print(f"project {args.project} has no upload yet")
        return 0
    for item in found["uploads"]:
        files = "-" if item["files"] is None else str(item["files"])
        build = "-" if item["build_id"] is None else f"#{item['build_id']} {item['build_status'] or ''}".strip()
        print(
            f"#{item['id']} {item['source']} {item['state']} {_bytes(item['size'])} files {files} "
            f"skipped {_skipped(item['skipped'])} build {build}"
        )
        if item.get("error"):
            print(f"  {item['error']}")
    return 0


def register(ksub) -> None:
    parser = ksub.add_parser("upload", help="send an archive (.zip, .tar, .tar.gz, .tgz) as a source of the graph")
    parser.add_argument("file", help="the archive")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--project", help="project name on the hub")
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
    json_option(
        parser,
        returns_object(*UPLOAD, *SENT, "pipeline", schema="KgUpload", added=(*SENT, "pipeline")),
        help="machine-readable output: the upload, with the parts sent and kept and the flow's end (null with --no-wait)",
    )
    parser.set_defaults(func=cmd_upload)

    flow = ksub.add_parser(
        "pipeline", help="where the flow after an upload stands: upload, extract, build, entities, questions, Curator"
    )
    flow.add_argument("--project", required=True, help="project name on the hub")
    flow.add_argument("--upload", type=int, help="the upload, as `hub kg uploads` lists it (default: the newest)")
    json_option(flow, returns_object(*PIPELINE, schema="KgPipeline"))
    flow.set_defaults(func=cmd_pipeline)

    listing = ksub.add_parser("uploads", help="list a project's archive uploads")
    listing.add_argument("--project", required=True, help="project name on the hub")
    listing.add_argument("--limit", type=int, default=50, help="the newest uploads listed (1 to 200)")
    json_option(listing, returns_object("uploads", schema="KgUploadList"))
    listing.set_defaults(func=cmd_uploads)
