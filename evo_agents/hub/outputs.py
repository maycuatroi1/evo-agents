"""The outputs of a run: the files its agent made that the hub keeps for the people who read the run. Both sides use
this module: the hub (``evo_agents.hub.server.run_outputs``, ``output_store`` and ``output_content``) and the machines
that send outputs (a worker's run, a local session). Standard library only, as the client side runs on a core install.

An output is one file of one run, from one of SOURCES: ``sent``, a file the agent sent on purpose (``evo-agents worker
output``, SendUserFile, the Artifact tool, an attachment of a question or a chat message); ``tool``, an image the
machine took out of a tool's result (a browser's screenshot, a Read of an image), which a project's admin may turn off
(``projects.output_tool_images``); ``outputs``, a file the agent left in the run's ``.evo-run/outputs/`` (OUTPUTS_DIR).
Its bytes are a blob of kind KIND in the blob store, held by the run's project; a run holds one output per SHA-256
(the hub folds a file sent again into the output it has, keeping the source that is kept longest, KEPT_ORDER).

Limits: MAX_FILE_BYTES a file, MAX_RUN_OUTPUTS outputs and MAX_RUN_BYTES bytes a run (the same SHA-256 counts once).
A name is the file's name, or its path below OUTPUTS_DIR (``check_name``); a media type is one ``type/subtype``, no
parameters (``check_media_type``). How the hub serves the bytes follows from the media type (``viewer``,
``served_type``, ``content_policy``): HTML and SVG never run with the hub's origin, since their answer carries a
Content-Security-Policy whose ``sandbox`` gives them an opaque origin and whose ``connect-src 'none'`` lets them call
nothing; text of any kind is served as text/plain; a type no viewer shows is served as an attachment.
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import PurePosixPath
from urllib.parse import quote

KIND = "run-output"  # the blob kind of an output's bytes (evo_agents.hub.blobs.KIND_LIMITS)
SENT, TOOL, OUTPUTS = "sent", "tool", "outputs"
SOURCES = (SENT, TOOL, OUTPUTS)
KEPT_ORDER = {TOOL: 0, OUTPUTS: 1, SENT: 2}  # a file sent twice keeps the source of these that ranks higher
KEPT_SOURCES = (SENT, OUTPUTS)  # kept until someone deletes them; TOOL goes with the run's events
OUTPUTS_DIR = ".evo-run/outputs"  # in a worker run's checkout, or above a plan run's worktrees

MiB = 1024 * 1024
MAX_FILE_BYTES = 200 * MiB
MAX_RUN_OUTPUTS = 500
MAX_RUN_BYTES = 1024 * MiB
MAX_BATCH = 100  # outputs one request uploads or records
MAX_ATTACHED = 20  # outputs one question or chat message names
MAX_NAME_CHARS = 255
MAX_CAPTION_CHARS = 2000
MAX_TOOL_CALL_CHARS = 200
DOWNLOAD_NAME_CHARS = 200  # of the ASCII name a download is offered under (evo_agents.hub.blobs.ATTACHMENT_NAME)

MEDIA_TYPE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

VIEWERS = ("image", "video", "audio", "pdf", "text", "html", "none")
IMAGE_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp", "image/svg+xml"}
)
VIDEO_TYPES = frozenset({"video/mp4", "video/webm", "video/quicktime", "video/ogg"})
AUDIO_TYPES = frozenset({"audio/mpeg", "audio/ogg", "audio/wav", "audio/webm", "audio/mp4", "audio/aac", "audio/flac"})
TEXT_TYPES = frozenset(  # besides text/* (text/html aside): what the web shows as text, code or a table
    {
        "application/json",
        "application/x-ndjson",
        "application/yaml",
        "application/x-yaml",
        "application/toml",
        "application/xml",
        "application/javascript",
        "application/x-sh",
        "application/sql",
    }
)
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
SVG = "image/svg+xml"
PDF = "application/pdf"

# Content-Security-Policy of each answer of an output's bytes. A policy on an image, video or audio element's bytes
# does nothing; it holds when the bytes are opened as a page of their own or in a frame, which only the hub's pages may
# hold (frame-ancestors 'self').
OPAQUE_POLICY = "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'"
HTML_POLICY = "; ".join(  # a page of its own, as a plugin's page is (evo_agents.hub.server.plugin_ui.UI_CSP)
    (
        "default-src 'none'",
        "script-src 'unsafe-inline' 'unsafe-eval'",
        "style-src 'unsafe-inline'",
        "img-src data: blob:",
        "font-src data:",
        "media-src data: blob:",
        "connect-src 'none'",
        "form-action 'none'",
        "base-uri 'none'",
        "object-src 'none'",
        "frame-src 'none'",
        "worker-src 'none'",
        "frame-ancestors 'self'",
        "sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads",
    )
)
PDF_POLICY = "frame-ancestors 'self'"  # a browser's PDF viewer runs no script with the page's origin

_EXTENSIONS = {  # what mimetypes does not know, or guesses wrong for an agent's files (.ts is not an MPEG stream)
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".log": "text/plain",
    ".jsonl": "application/x-ndjson",
    ".ndjson": "application/x-ndjson",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".ts": "text/plain",
    ".tsx": "text/plain",
    ".jsx": "text/plain",
    ".py": "text/x-python",
    ".go": "text/plain",
    ".rs": "text/plain",
    ".sql": "application/sql",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
}


def check_name(name: str) -> str:
    """``name`` trimmed, when it is a file's name or a relative path of one (components joined by ``/``, none empty,
    ``.`` or ``..``), at most MAX_NAME_CHARS with no control character and no backslash; ValueError otherwise."""
    name = name.strip()
    if not name:
        raise ValueError("an output needs a name")
    if len(name) > MAX_NAME_CHARS:
        raise ValueError(f"an output's name is at most {MAX_NAME_CHARS} characters")
    if _CONTROL.search(name) or "\\" in name:
        raise ValueError("an output's name has no control character and no backslash")
    if name.startswith("/") or any(part in ("", ".", "..") for part in name.split("/")):
        raise ValueError("an output's name is a file's name, or a relative path without '.' or '..' parts")
    return name


def check_media_type(media_type: str) -> str:
    """``media_type`` in lower case when it is one ``type/subtype`` with no parameters; ValueError otherwise."""
    found = media_type.strip().lower()
    if not MEDIA_TYPE.fullmatch(found):
        raise ValueError(f"a media type is type/subtype with no parameters, not {media_type!r}")
    return found


def check_line(value: str | None, limit: int, what: str) -> str | None:
    """``value`` trimmed, one line of at most ``limit`` characters, None for None; ValueError for a blank one."""
    if value is None:
        return None
    value = value.strip()
    if not value or len(value) > limit or _CONTROL.search(value):
        raise ValueError(f"{what} is one line of 1 to {limit} characters")
    return value


def check_caption(value: str | None) -> str | None:
    """A caption trimmed, 1 to MAX_CAPTION_CHARS characters, lines allowed; None for None."""
    if value is None:
        return None
    value = value.strip()
    if not value or len(value) > MAX_CAPTION_CHARS:
        raise ValueError(f"a caption is 1 to {MAX_CAPTION_CHARS} characters")
    if re.search(r"[\x00-\x08\x0b-\x1f\x7f]", value):
        raise ValueError("a caption has no control character but line breaks and tabs")
    return value


def guess_media_type(name: str) -> str:
    """The media type of a file called ``name``, by its extension; application/octet-stream when it is unknown."""
    suffix = PurePosixPath(name.replace("\\", "/")).suffix.lower()
    if suffix in _EXTENSIONS:
        return _EXTENSIONS[suffix]
    guessed, encoding = mimetypes.guess_type(f"x{suffix}", strict=False)
    if guessed is None or encoding is not None:
        return "application/octet-stream"
    return guessed.lower()


def viewer(media_type: str) -> str:
    """Which of VIEWERS the web opens an output of ``media_type`` with; ``none`` offers a download only."""
    if media_type in IMAGE_TYPES:
        return "image"
    if media_type in VIDEO_TYPES:
        return "video"
    if media_type in AUDIO_TYPES:
        return "audio"
    if media_type == PDF:
        return "pdf"
    if media_type in HTML_TYPES:
        return "html"
    if media_type.startswith("text/") or media_type in TEXT_TYPES:
        return "text"
    return "none"


def is_text(media_type: str) -> bool:
    """Whether a file of ``media_type`` is text a machine redacts before it sends it (text, code, data, HTML)."""
    return viewer(media_type) in ("text", "html") or media_type == SVG


def served_type(media_type: str) -> str:
    """The Content-Type the hub answers an output's bytes with: text of every kind as UTF-8 text/plain (no XML or
    script document ever renders as one), HTML as UTF-8 text/html under HTML_POLICY, the others as they are."""
    shown = viewer(media_type)
    if shown == "text":
        return "text/plain; charset=utf-8"
    if shown == "html":
        return "text/html; charset=utf-8"
    return media_type


def content_policy(media_type: str) -> str:
    """The Content-Security-Policy of an answer of an output's bytes of ``media_type``."""
    shown = viewer(media_type)
    if shown == "html":
        return HTML_POLICY
    if shown == "pdf":
        return PDF_POLICY
    return OPAQUE_POLICY


def inline(media_type: str) -> bool:
    """Whether the hub answers an output of ``media_type`` inline, unless a download is asked: one the web views."""
    return viewer(media_type) != "none"


def download_name(name: str) -> str:
    """The last part of ``name`` as an ASCII file name of letters, digits and ``._-`` (``output`` when nothing is
    left), which a Content-Disposition and a presigned GET may name."""
    base = name.rsplit("/", 1)[-1]
    safe = _UNSAFE.sub("_", base).strip("._") or "output"
    if len(safe) > DOWNLOAD_NAME_CHARS:
        _, dot, suffix = safe.rpartition(".")
        keep = f".{suffix[:16]}" if dot else ""
        safe = safe[: DOWNLOAD_NAME_CHARS - len(keep)].rstrip("._") + keep
    return safe


def content_disposition(name: str, attachment: bool) -> str:
    """A Content-Disposition naming the output: its ASCII name, and its own name in UTF-8 (RFC 6266, RFC 8187)."""
    how = "attachment" if attachment else "inline"
    base = name.rsplit("/", 1)[-1]
    return f"{how}; filename=\"{download_name(name)}\"; filename*=UTF-8''{quote(base, safe='')}"


def parse_range(header: str | None, size: int) -> tuple[int, int] | None | bool:
    """The byte range ``header`` (a Range header) asks of ``size`` bytes, as (first, last) both included; None for the
    whole (no header, another unit, several ranges, or one that does not parse: RFC 9110 lets the server send it all);
    False when the range cannot be satisfied (416)."""
    if header is None:
        return None
    unit, _, ranges = header.strip().partition("=")
    if unit.strip().lower() != "bytes" or "," in ranges:
        return None
    first_text, dash, last_text = ranges.strip().partition("-")
    if not dash or not (first_text.isdigit() or last_text.isdigit()):
        return None
    if (first_text and not first_text.isdigit()) or (last_text and not last_text.isdigit()):
        return None
    if not first_text:  # the last N bytes
        tail = int(last_text)
        if tail == 0 or size == 0:
            return False
        return max(size - tail, 0), size - 1
    first = int(first_text)
    last = int(last_text) if last_text else size - 1
    if first >= size or last < first:
        return False
    return first, min(last, size - 1)
