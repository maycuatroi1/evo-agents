"""The words of ``evo-agents hub marketing ...`` without ``--json``: the routes answer them for the query
``render=text`` (``cli.json``, ``output.render``), and the CLI prints them as they are. Each function takes what the
route answers as JSON. Standard library only.
"""

from __future__ import annotations

UNCHANGED = "Nothing changed: it was so already"


def table(header: tuple[str, ...], rows: list[tuple]) -> list[str]:
    """The lines of a table as the CLI prints one: each column as wide as its widest cell, two spaces between."""
    widths = [max(len(str(cell)) for cell in column) for column in zip(header, *rows, strict=False)]
    lines = (("  ".join(str(c).ljust(w) for c, w in zip(row, widths, strict=True))).rstrip() for row in (header, *rows))
    return list(lines)


def _health(channel: dict) -> str:
    said = channel.get("health") or "unknown"
    return f"{said}: {channel['health_reason']}"[:70] if channel.get("health_reason") else said


def channels(found: list[dict]) -> str:
    rows = [
        (c["id"], c["channel"], c["account"], c["kind"], c["mode"], "open" if c["paused"] else "closed", _health(c))
        for c in found
    ]
    return "\n".join(table(("id", "channel", "account", "kind", "mode", "breaker", "health"), rows)) if rows else ""


def channel(found: dict) -> str:
    return f"Channel {found['id']} is {found['channel']} {found['account']} ({found['kind']}, {found['mode']})"


def posts(found: list[dict]) -> str:
    rows = [
        (p["id"], p["channel"], p["account"], p["state"], (p["reason"] or p["posted_url"] or "")[:70]) for p in found
    ]
    return "\n".join(table(("id", "channel", "account", "state", "reason or link"), rows)) if rows else ""


def done(found: dict, said: str) -> str:
    return said if found["ok"] else UNCHANGED


def denylist(found: list[dict]) -> str:
    rows = [(d["id"], d["kind"], d["value"], d["added_at"][:16]) for d in found]
    return "\n".join(table(("id", "kind", "value", "added"), rows)) if rows else ""
