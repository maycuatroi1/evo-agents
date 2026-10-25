"""The words of ``evo-agents hub crm list|show|delete`` without ``--json``: the routes answer them for the query
``render=text`` (``cli.json``, ``output.render``), and the CLI prints them as they are. Each function takes what the
route answers as JSON. Standard library only.
"""

from __future__ import annotations

STAGES = ("lead", "invited", "activated", "retained")


def table(header: tuple[str, ...], rows: list[tuple]) -> list[str]:
    """The lines of a table as the CLI prints one: each column as wide as its widest cell, two spaces between."""
    widths = [max(len(str(cell)) for cell in column) for column in zip(header, *rows, strict=False)]
    lines = (("  ".join(str(c).ljust(w) for c, w in zip(row, widths, strict=True))).rstrip() for row in (header, *rows))
    return list(lines)


def contacts(found: dict) -> str:
    lines = ["  ".join(f"{stage} {found['counts'].get(stage, 0)}" for stage in STAGES)]
    rows = [(c["login"], c["stage"], c["source"], c["first_seen_at"][:10]) for c in found["contacts"]]
    if rows:
        lines += table(("login", "stage", "source", "first seen"), rows)
    return "\n".join(lines)


def contact(found: dict) -> str:
    lines = [
        f"{found['login']}  {found['profile_url']}",
        f"source {found['source']}, stage {found['stage']} since {found['stage_at'][:10]}",
        "Stages: " + ", ".join(f"{h['stage']} {h['reached_at'][:10]}" for h in found["history"]),
    ]
    rows = [
        (t["happened_at"][:10], t["kind"], t["ref"], t.get("url") or t.get("note") or "") for t in found["touchpoints"]
    ]
    if rows:
        lines += table(("when", "kind", "ref", "link or note"), rows)
    return "\n".join(lines)


def deleted(login: str, found: dict) -> str:
    return f"Deleted {login} and {found['touchpoints']} touchpoints"
