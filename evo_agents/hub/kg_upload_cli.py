"""``evo-agents hub kg upload`` and ``hub kg uploads``: send an archive as a source of a project's graph, and list
what was sent.

``upload FILE --project P [--source NAME] [--label L]`` sends a .zip, .tar, .tar.gz or .tgz (the writer role). It
hashes the file, asks the hub to start the upload (POST /v1/kg/{project}/uploads), which checks the size, the role and
the label against what the project's hub sink clears before any byte of the archive leaves this machine, then PUTs
the parts the hub does not hold yet straight to the blob store, PARALLEL at a time, each through a presigned URL and
tried PUT_ATTEMPTS times, and asks the hub to join them (``evo_agents.hub.server.kg_uploads``). Run again after an
interruption, it sends only the parts that are missing. ``uploads --project P`` lists the project's uploads with their
state, size, the files read and skipped and the build that followed. ``--json`` prints the hub's answer. Standard
library only.
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


def upload(hub: Hub, project: str, path: Path, *, source: str | None = None, label: str | None = None, progress=None):
    """Send the archive at ``path``; (the hub's answer for the upload once joined, parts sent, parts kept)."""
    say = progress or (lambda line: None)
    size = path.stat().st_size
    ask = {"name": path.name, "size": size, "sha256": file_sha256(path)}
    if source:
        ask["source"] = source
    if label:
        ask["label"] = label
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
    done = hub.call("POST", f"{path_of}/complete", {})
    return done, len(missing), len(kept)


@client_command
def cmd_upload(args) -> int:
    path = Path(args.file)
    if not path.is_file():
        sys.exit(f"no file {args.file}")
    if not path.name.lower().endswith(SUFFIXES):
        sys.exit(f"{path.name} is not an archive the hub reads: {', '.join(SUFFIXES)}")
    hub, _ = signed_in()
    progress = None if args.json else (lambda line: print(line, file=sys.stderr))
    done, sent, kept = upload(hub, args.project, path, source=args.source, label=args.label, progress=progress)
    if args.json:
        print_json({**done, "sent": sent, "kept": kept})
        return 0
    print(
        f"uploaded {path.name} ({_bytes(done['size'])}) to project {args.project} as source {done['source']}: "
        f"upload {done['id']} is {done['state']}"
    )
    return 0 if done["state"] != "failed" else 1


def _skipped(skipped: dict) -> str:
    return ", ".join(f"{reason} {count}" for reason, count in sorted(skipped.items())) or "-"


@client_command
def cmd_uploads(args) -> int:
    hub, _ = signed_in()
    found = hub.call("GET", f"{_base(args.project)}?limit={args.limit}")
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
    parser.add_argument("--project", required=True, help="project name on the hub")
    parser.add_argument("--source", help="the source it becomes (default: the file's name without its suffix)")
    parser.add_argument("--label", help="a level of the project (default: the project's default label)")
    json_option(
        parser,
        returns_object(*UPLOAD, *SENT, schema="KgUpload", added=SENT),
        help="machine-readable output: the upload, with the parts sent and kept",
    )
    parser.set_defaults(func=cmd_upload)

    listing = ksub.add_parser("uploads", help="list a project's archive uploads")
    listing.add_argument("--project", required=True, help="project name on the hub")
    listing.add_argument("--limit", type=int, default=50, help="the newest uploads listed (1 to 200)")
    json_option(listing, returns_object("uploads", schema="KgUploadList"))
    listing.set_defaults(func=cmd_uploads)
