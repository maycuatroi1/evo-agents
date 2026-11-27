"""The constants of the files of plugins' framed pages and of the hub's UI kit (``service.ui``): the types served,
the headers each answer carries, and the shapes of the paths."""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import Path

from evo_agents.hub.server.plugins.models.store import PLUGIN_NAME

MAX_CACHED_BYTES = 64 * 1024 * 1024  # the pages of every bundle kept in memory, together
UI_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
UI_CSP = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self'",
        "media-src 'self'",
        "connect-src 'none'",
        "form-action 'none'",
        "base-uri 'none'",
        "object-src 'none'",
        "frame-src 'none'",
        "worker-src 'none'",
        "frame-ancestors 'self'",
        "sandbox allow-scripts allow-forms",
    )
)
UI_HEADERS = {
    "Content-Security-Policy": UI_CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "private, max-age=300",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
}
NO_PAGE = "no such page of a plugin here: open it from the project's menu again"
KIT_VERSIONS = ("1",)
KIT_FILE = re.compile(r"[a-z][a-z0-9-]{0,60}\.(css|js)")
SAFE_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,200}")  # a part of a path below pages/: no .., no hidden file
KIT_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "public, max-age=3600",
    "Access-Control-Allow-Origin": "*",  # a sandboxed frame's origin is opaque: null
    "Cross-Origin-Resource-Policy": "cross-origin",
}
PluginName = Annotated[str, Path(pattern=PLUGIN_NAME)]
