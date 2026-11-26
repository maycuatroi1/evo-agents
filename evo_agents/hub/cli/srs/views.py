"""How ``evo-agents hub srs`` shows what the hub answered: a document and its versions, an item, an import's preview,
a version's reviews and use cases, threads of comments and findings, each as the lines a person reads. ``web`` turns
a link of the hub's web into a whole URL (``evo_agents.hub.cli.srs.resources.Web``)."""

from __future__ import annotations

from collections.abc import Callable

from evo_agents.clikit import Table, Text, View
from evo_agents.hub.cli.base import when

LinkOf = Callable[[str], str]


def case_name(case: dict) -> str:
    name = case.get("code") or case.get("key") or case.get("title") or ""
    return f"{name} r{case['rev']}" if case.get("rev") else name


def item_view(item: dict) -> Text:
    rev = f" r{item['rev']}" if item.get("rev") else ""
    lines = [
        f"{item['code'] or item['title']}{rev} ({item['kind'].replace('_', ' ')}, key {item['key']}): {item['title']}"
    ]
    if item.get("rev"):
        held = item.get("released")
        lines.append(f"  approved baseline: {'r' + str(held) if held else 'none yet'}")
    for one in item.get("revisions") or []:
        what = "dropped" if one["dropped"] else f"r{one['rev']}"
        sent = f", sent to review by {one['submitted_by']}" if one.get("submitted_at") else ""
        lines.append(f"  {what} in v{one['version']} by {one['author']} {when(one['created_at'])}{sent}")
    if item["aliases"]:
        lines.append(f"  also: {', '.join(item['aliases'])}")
    lines.append(
        f"  in {item['document']} version {item['version']}"
        + (f", under {' > '.join(item['path'])}" if item["path"] else "")
    )
    for name, value in item["fields"].items():
        if isinstance(value, str) and name not in ("tail", "dash", "step_label"):
            lines.append(f"  {name}: {value}")
    for step in item["fields"].get("steps") or []:
        lines.append(
            f"  step {step}" if isinstance(step, str) else "  step " + " | ".join(str(v) for v in step.values())
        )
    for child in item["children"]:
        lines.append(f"  - {child['code'] or child['title']} ({child['kind'].replace('_', ' ')}): {child['title']}")
    return Text(*lines)


def version_summary(version: dict) -> str:
    reviews = ", ".join(
        f"{r['decision']} by {r['reviewer']}" for r in version["reviews"] if r["round"] == version["round"]
    )
    line = f"  v{version['number']} {version['state']} by {version['author']} {when(version['created_at'])}"
    return line + (f" ({reviews})" if reviews else "") + (f": {version['summary']}" if version["summary"] else "")


def document_view(doc: dict) -> Text:
    approved = f", approved version {doc['approved_version']}" if doc["approved_version"] else ""
    lines = [f"{doc['title']} ({doc['slug']}, {doc['kind']}){approved}"]
    lines.extend(version_summary(version) for version in doc["versions"])
    shown = doc["shown"]
    if shown is None:
        return Text(*lines)
    lines.append(f"Version {shown['number']}:")
    depth = {}
    for item in shown["items"]:
        depth[item["key"]] = depth.get(item["parent"], -1) + 1 if item["parent"] else 0
        code = f"{item['code']} r{item['rev']}" if item.get("rev") else item["code"]
        name = f"{code}: {item['title']}" if item["code"] else item["title"]
        lines.append("  " + "  " * depth[item["key"]] + name)
    for problem in shown["problems"]:
        lines.append(f"  {problem['level']}: {problem['check']} {problem['code'] or ''}: {problem['message']}")
    return Text(*lines)


def preview_view(done: dict) -> Text:
    preview = done["preview"]
    counts = ", ".join(f"{count} {kind.replace('_', ' ')}" for kind, count in sorted(preview["counts"].items()))
    if done["dry_run"]:
        lines = [f"Preview of {done['document']}: {counts}."]
    else:
        verb = "Created" if done["created"] else "Imported into"
        lines = [f"{verb} {done['document']}: version {done['version']}, a draft; {counts}."]
    lines.extend(f"  {name}: {', '.join(preview[name])}" for name in ("added", "changed", "removed") if preview[name])
    for problem in preview["problems"]:
        where = f" line {problem['line']}" if problem["line"] else ""
        lines.append(f"  {problem['level']}: {problem['check']}{where}: {problem['message']}")
    return Text(*lines)


def version_view(slug: str, version: dict) -> Text:
    lines = [f"{slug} version {version['number']} is {version['state'].replace('_', ' ')} (round {version['round']})."]
    for review in version["reviews"]:
        if review["round"] == version["round"]:
            role = f" as {review['role']}" if review["role"] else ""
            lines.append(f"  {review['decision'].replace('_', ' ')} by {review['reviewer']}{role}")
    return Text(*lines)


def counted(reviews: list[dict]) -> str:
    return (
        "; ".join(
            f"{r['decision'].replace('_', ' ')} by {r['reviewer']}"
            + (f" as {r['role']}" if r["role"] else "")
            + f" on v{r['version']}"
            for r in reviews
        )
        or "-"
    )


def case_state(case: dict) -> str:
    return case["state"].replace("_", " ") + (" (sent alone)" if case.get("submitted") else "")


def use_cases_view(version: dict, web: LinkOf) -> View:
    cases = version.get("use_cases") or []
    if not cases:
        return Text("  no use case to review")
    rows = [
        (
            case_name(c),
            c["change"],
            case_state(c),
            f"r{c['released']}" if c.get("released") else "-",
            counted(c["reviews"]),
            web(c.get("link") or "") or "-",
        )
        for c in cases
    ]
    return Table(("USE CASE", "CHANGE", "STATE", "APPROVED", "REVIEWS", "REVIEW"), rows)


def queue_views(found: list[dict], web: LinkOf) -> list[View]:
    views: list[View] = []
    for version in found:
        held = f", held by {version['held_by'].replace('_', ' ')}" if version.get("held_by") else ""
        views.append(
            Text(
                f"{version['document']} v{version['number']} ({version['state'].replace('_', ' ')})"
                f" by {version['author']}{held}"
            )
        )
        rows = [
            (
                case_name(c),
                case_state(c),
                "yes" if c.get("may_review") else "no",
                (c.get("held_by") or "-").replace("_", " "),
                web(c.get("link") or "") or "-",
            )
            for c in version["use_cases"]
        ]
        views.append(Table(("USE CASE", "STATE", "YOU MAY", "HELD BY", "REVIEW"), rows))
    return views


def threads_view(slug: str, comments: list[dict]) -> Text:
    roots = [c for c in comments if c["parent_id"] is None]
    if not roots:
        return Text(f"{slug} has no thread of comments that fits.")
    lines = []
    for root in roots:
        about = root["item_code"] or root["item"] or "the document"
        state = "open" if root["resolved_by"] is None else f"resolved by {root['resolved_by']}"
        lines.append(f"thread {root['id']} on {about} ({state})")
        if root.get("quote"):
            changed = " [passage changed]" if root.get("anchor") == "changed" else ""
            lines.append(f'  passage: "{root["quote"]}"{changed}')
        for one in [root, *(c for c in comments if c["parent_id"] == root["id"])]:
            by = f"{one['author']} (run #{one['run_id']})" if one["run_id"] else one["author"]
            asked = f" [asked @ba: run #{one['ask_run_id']}]" if one.get("ask_run_id") else ""
            lines.append(f"  #{one['id']} {by} {when(one['created_at'])}{asked}: {one['body']}")
    return Text(*lines)


def items(count: int) -> str:
    return f"{count} item" if count == 1 else f"{count} items"


def sources(given: list[dict]) -> str:
    return "; ".join(f"{source['kind']} {source['ref']}" for source in given)


def finding_lines(found: dict) -> list[str]:
    about = found["item_code"] or found["item"] or "the document"
    lines = [
        f"#{found['id']} {found['level']} {found['kind']} on v{found['version']}, {about}: {found['status']}"
        f" {when(found['created_at'])}",
        f"  {found['summary']}",
        f"  sources: {sources(found['sources'])}",
    ]
    lines.extend(f"  {'ABCD'[at]}. {option}" for at, option in enumerate(found.get("options") or ()))
    if found["status"] != "open":
        by = found["settled_by"] or ("the graph" if found["status"] == "resolved" else "@ba")
        lines.append(f"  {found['status']} by {by} {when(found['settled_at'])}: {found['resolution']}")
        if found["resolution_sources"]:
            lines.append(f"  settled by: {sources(found['resolution_sources'])}")
    if found["chat_link"]:
        lines.append(f"  chat: {found['chat_link']}")
    return lines
