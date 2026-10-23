"""Files a person attaches to a message: to a run's agent (``POST /v1/projects/{p}/runs/{id}/messages``) or in a group
chat (``POST /v1/chats/{id}/messages``). docs/hub.md, section "Attachments of messages", is the guide.

The sender uploads each file first (``POST .../attachments``), which the hub streams into its blob store as a blob of
kind KIND, at most MAX_FILE_BYTES, and keeps as an ``attachments`` row of its uploader and target (one run or one
chat) that no message holds yet. The message then names at most MAX_PER_MESSAGE of those ids (``attachment_ids``),
and the hub ties them to the message it stores, in the same transaction: a run_inbox row, or a chat message, set
once. A file no message took within STALE_AFTER goes (hub.cleanup_uploads); the files of a run's messages go with
the run's events (hub.prune_run_events); the files of a chat's messages stay with the chat.

The names and limits here are shared by the hub, its CLI and the worker, so this module imports nothing of the
server.
"""

from __future__ import annotations

import re
from datetime import timedelta

KIND = "attachment"  # the blob kind of an attachment's bytes (evo_agents.hub.blobs.KIND_LIMITS)
MiB = 1024 * 1024
MAX_FILE_BYTES = 50 * MiB
MAX_PER_MESSAGE = 10
MAX_NAME_CHARS = 255
STALE_AFTER = timedelta(hours=24)  # an attachment no message took by then is deleted, row and blob
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def check_name(name: str) -> str:
    """``name`` trimmed, when it is a file's own name: at most MAX_NAME_CHARS, no control character, no slash or
    backslash, not ``.`` or ``..``; ValueError otherwise."""
    name = name.strip()
    if not name:
        raise ValueError("an attachment needs a name")
    if len(name) > MAX_NAME_CHARS:
        raise ValueError(f"an attachment's name is at most {MAX_NAME_CHARS} characters")
    if _CONTROL.search(name) or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError("an attachment's name is a file's own name: no slash, backslash or control character")
    return name


def too_large(size: int) -> str:
    """The refusal of a file of ``size`` bytes over MAX_FILE_BYTES."""
    return f"an attachment is at most {MAX_FILE_BYTES} bytes (50 MiB); this one is {size}"
