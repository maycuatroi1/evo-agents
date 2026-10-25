"""The words of ``evo-agents hub pm daily`` without ``--json``: the route answers them for the query ``render=text``
(``cli.json``, ``output.render``), and the CLI prints them as they are. ``daily`` takes what the route answers as JSON.
Standard library only.
"""

from __future__ import annotations


def table(header: tuple[str, ...], rows: list[tuple]) -> list[str]:
    """The lines of a table as the CLI prints one: each column as wide as its widest cell, two spaces between."""
    widths = [max(len(str(cell)) for cell in column) for column in zip(header, *rows, strict=False)]
    lines = (("  ".join(str(c).ljust(w) for c, w in zip(row, widths, strict=True))).rstrip() for row in (header, *rows))
    return list(lines)


def _names(items: list[dict], key: str = "ref") -> str:
    return ", ".join(item[key] for item in items) or "-"


def _sprint(sprint: dict | None) -> list[str]:
    if sprint is None:
        return ["Sprint: none active"]
    done = "-" if sprint["done_percent"] is None else f"{sprint['done_percent']:.0f}%"
    gone = "-" if sprint["time_percent"] is None else f"{sprint['time_percent']:.0f}%"
    lines = [
        f"Sprint {sprint['name']}: scope {sprint['scope']}, done {done}, time gone {gone}, "
        f"+{sprint['added']} added, -{sprint['removed']} removed"
    ]
    lines += [f"  {case['code']}  {case['done']}/{case['total']}  {case['title']}" for case in sprint["use_cases"]]
    return lines


def daily(report: dict) -> str:
    lines = [
        f"{report['project']}  {report['date']}  (since {report['previous_working_day']})",
        f"Done use cases: {_names(report['done_use_cases'], 'code')}",
        f"Done tasks: {_names(report['done_tasks'])}",
        *_sprint(report["sprint"]),
        "Late:",
    ]
    for task in report["late"]:
        when = f"{task['days_late']} working days late" if task["kind"] == "overdue" else "at risk"
        lines.append(f"  {task['ref']}  due {task['due_on']}  {when}  {task['title']}")
    lines.append("In review:")
    lines += [f"  {task['ref']}  {task['age_days']} working days  {task['title']}" for task in report["review"]]
    lines.append("Stale:")
    for task in report["stale"]:
        lines.append(f"  {task['ref']}  idle {task['idle_days']} working days ({task['source']})  {task['title']}")
    lines.append("Blocked:")
    lines += [f"  {b['ref']}  waiting on {b['waiting_on']}: {b['reason']}" for b in report["blocked"]]
    lines.append("Escalate:")
    lines += [f"  {b['ref']}  {b['age_days']} working days  waiting on {b['waiting_on']}" for b in report["escalate"]]
    lines.append("Load:")
    rows = [
        (m["login"], m["doing"], "overloaded" if m["overloaded"] else "empty" if m["empty"] else "ok", m["late"])
        for m in report["load"]
    ]
    if rows:
        lines += table(("member", "doing", "state", "late"), rows)
    hidden = {name: count for name, count in report["hidden"].items() if count}
    if hidden:
        lines.append("Hidden from you: " + ", ".join(f"{count} {name}" for name, count in hidden.items()))
    return "\n".join(lines)
