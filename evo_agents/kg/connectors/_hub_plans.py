"""The plans of the hub, for the harness connector of a harness whose harness.yaml names a hub project.

There the hub holds the plans and ``plans/`` only copies, which lag whenever nobody exported since the last change.
``kg sync`` hands the connector a reader (``ConnectorContext.plans``, ``evo_agents.hub.kg_plans.reader``, which this
package does not import): ``reader(harness, project, held)`` gives None on a machine not signed in to a hub,
``{"error": ...}`` when the hub could not be read, else ``{"plans": [...]}``, each plan with its ``plan_id``,
``area``, ``revision``, ``digest``, ``label``, ``text`` (its copy at that revision, as ``hub plan export`` writes it),
``updated_at`` and ``url`` (its page on the hub).

Each plan read from the hub becomes the item ``plans/<area>/<id>.yaml`` (``item``): that text, the hub's label on
it, which raises the source's label and never lowers it, the link to its page, and the revision's time; the files of
that plan under ``plans/`` are not read. The run ends with a cursor that keeps, for each plan, what the item was made
of but its text (``held``). A run that does not read the hub (not signed in, the hub down, no reader) reads the
plans from disk, except that a copy which is, as the hub wrote it, the revision the cursor holds gives the very item
the hub's revision gave: so a laptop offline for a day keeps the hub's labels, and a plan whose revision did not change
gives the same item whether or not the hub was read, which leaves the corpus as it was and builds nothing.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from evo_agents.harness import HUB_KEY, hub_project, parse_yaml, plan_digest
from evo_agents.kg.connectors._files import file_item, git_blob_sha
from evo_agents.kg.ids import epoch_to_iso, parse_utc
from evo_agents.kg.protocol import finalize_item

AREAS = ("active", "completed")
HELD = ("area", "revision", "digest", "label", "updated_at", "url")  # what the cursor keeps of each plan


def plan_copy(rel: str) -> str | None:
    """The plan id of ``plans/<area>/<id>.yaml``; None for any other path."""
    parts = rel.split("/")
    if len(parts) == 3 and parts[0] == "plans" and parts[1] in AREAS and parts[2].endswith(".yaml"):
        return parts[2][: -len(".yaml")]
    return None


def held_plans(cursor, project: str) -> dict:
    """What the cursor of the source's last run that read the hub keeps of each plan of ``project``."""
    found = cursor.get("plans") if isinstance(cursor, dict) else None
    if not isinstance(found, dict) or found.get("project") != project or not isinstance(found.get("held"), dict):
        return {}
    return {pid: entry for pid, entry in found["held"].items() if isinstance(entry, dict)}


def known_copies(root: Path, project: str, held: dict) -> dict[str, dict]:
    """The plans whose copy on disk is, as the hub wrote it, the revision ``held`` names (its hub key and the digest
    of what it holds): what the hub's revision gave, with the copy's text."""
    found = {}
    for plan_id, entry in sorted(held.items()):
        if entry.get("area") not in AREAS:
            continue
        try:
            text = (root / "plans" / entry["area"] / f"{plan_id}.yaml").read_text(encoding="utf-8")
            data = parse_yaml(text)
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        hub = data.get(HUB_KEY) if isinstance(data, dict) else None
        key = {"project": project, "revision": entry.get("revision"), "digest": entry.get("digest")}
        if hub == key and plan_digest(data) == entry.get("digest"):
            found[plan_id] = {**entry, "plan_id": plan_id, "text": text}
    return found


def read(ctx, root: Path):
    """The plans the run takes from the hub, by id, and the cursor it ends with (None when the hub was not read);
    ``({}, None)`` in a harness without a hub project. Yields a warning when the hub could not be read."""
    project = hub_project(ctx.harness)
    if project is None:
        return {}, None
    held = held_plans(ctx.cursor, project)
    found = None
    if ctx.plans is not None:
        try:
            found = ctx.plans(ctx.harness, project, held)
        except Exception as exc:  # a reader that fails leaves the copies, as a hub that cannot be read does
            found = {"error": f"the plans of hub project {project} were not read ({type(exc).__name__})"}
        if found is not None and (found.get("error") or not isinstance(found.get("plans"), list)):
            yield {"type": "log", "level": "warning", "message": str(found.get("error") or "the hub sent no plans")}
            found = None
    if found is None:
        return known_copies(root, project, held), None
    plans = {plan["plan_id"]: plan for plan in found["plans"]}
    kept = {plan_id: {key: plan.get(key) for key in HELD} for plan_id, plan in sorted(plans.items())}
    return plans, {"plans": {"project": project, "held": kept}}


def _time(value) -> str | None:
    try:
        return value if isinstance(value, str) and parse_utc(value) else None
    except ValueError:
        return None


def item(sid: str, plan: dict, repo_name: str) -> dict:
    """The item of a plan the hub holds: the text of its copy at the hub's revision, the hub's label (level, location
    and integrity: kg/1 labels name no projects), the link to its page and the time of the revision."""
    rel = f"plans/{plan['area']}/{plan['plan_id']}.yaml"
    text = plan["text"]
    rev_time = _time(plan.get("updated_at")) or epoch_to_iso(0)
    item = file_item(
        f"{sid}:file:{rel}",
        rel,
        text,
        rev=git_blob_sha(text.encode()),
        rev_time=rev_time,
        uri=plan.get("url"),
        kind="plan",
    )
    label = plan.get("label") if isinstance(plan.get("label"), dict) else {}
    item["label"] = {key: label[key] for key in ("level", "location") if isinstance(label.get(key), str)}
    if label.get("integrity") in ("T", "U"):
        item["label"]["integrity"] = label["integrity"]
    item["props"]["repo"] = repo_name
    return finalize_item(item)
