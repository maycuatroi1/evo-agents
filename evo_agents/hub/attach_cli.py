"""``--attach FILE`` of ``evo-agents hub run send`` and ``evo-agents hub chat send``: upload each file to the hub before
the message (``evo_agents.hub.attachments``), and name the files a message carries when the CLI prints it.

``upload`` checks every file here first, so a message with one missing or too large file uploads none; then it posts
each file's bytes, streamed, to ``{target}/attachments?name=`` and returns the ids the message names. Standard
library only, like the rest of the client.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from evo_agents.hub import attachments
from evo_agents.hub.outputs import guess_media_type

ATTACH_HELP = f"attach this file (at most {attachments.MAX_FILE_BYTES // attachments.MiB} MiB); repeat it, up to "
ATTACH_HELP += f"{attachments.MAX_PER_MESSAGE} files; with a file, TEXT may be left out"


def checked(paths: list[str]) -> list[Path]:
    """``paths`` as files to upload; ValueError naming the first that is missing, too large or badly named, or when
    there are more than a message takes."""
    if len(paths) > attachments.MAX_PER_MESSAGE:
        raise ValueError(f"{len(paths)} files, over the {attachments.MAX_PER_MESSAGE} a message takes")
    files = []
    for given in paths:
        path = Path(given)
        if not path.is_file():
            raise ValueError(f"{given}: no such file")
        attachments.check_name(path.name)
        size = path.stat().st_size
        if size > attachments.MAX_FILE_BYTES:
            raise ValueError(f"{given}: {attachments.too_large(size)}")
        files.append(path)
    return files


def upload(hub, target: str, files: list[Path]) -> list[int]:
    """Upload ``files`` (``checked``) to ``target``, ``/v1/projects/{p}/runs/{id}`` or ``/v1/chats/{id}``; their
    ids, in order."""
    ids = []
    for path in files:
        found = hub.upload(f"{target}/attachments?name={quote(path.name, safe='')}", path, guess_media_type(path.name))
        ids.append(found["id"])
    return ids


def describe(files: list[dict] | None) -> str:
    """``files`` as one line: each name with its size; empty for none."""
    return "; ".join(f"{f['name']} ({f['size']} bytes)" for f in files or [])
