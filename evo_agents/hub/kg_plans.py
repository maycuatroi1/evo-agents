"""The plans of the hub, for the harness connector of the knowledge graph (``ConnectorContext.plans``).

In a harness whose harness.yaml names a hub project, the hub holds the plans and ``plans/`` keeps read-only copies,
which lag whenever nobody exported since the last change. ``kg sync`` therefore hands the harness connector
``reader()``: for each plan of the project the signed-in member sees, the text of its copy at the revision the hub
holds (``mirror.render``, the very text ``hub plan export`` writes), its area, its label and when it changed. The
connector uses that in place of the files under ``plans/`` (``evo_agents.kg.connectors.harness``).

One ``GET .../plans`` lists the revisions; a plan is fetched only when the connector's cursor does not hold its
revision (``held``, with the label of that revision: the list does not carry labels) or the copy on disk is not that
revision as the hub wrote it, so an hourly sync of an unchanged, exported project costs one request. A machine that is not signed in reads the copies (None); a
hub that cannot be read, or answers what no copy could hold, gives the connector an ``error`` to log, and the copies
again. Standard library and PyYAML only, like the rest of the client.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from evo_agents.hub.client import Hub, HubError, NotSignedIn, load_credentials
from evo_agents.hub.mirror import check_summary, current_copy, mirror_path, plans_path, render


def plan_url(hub, project: str, plan_id: str) -> str:
    """The plan's page on the hub's web."""
    return f"{hub.url.rstrip('/')}/p/{quote(project, safe='')}/plans/{quote(plan_id, safe='')}"


def _fetched(hub, project: str, plan_id: str) -> dict:
    plan = hub.call("GET", plans_path(project, plan_id))
    if not isinstance(plan, dict) or plan.get("plan_id") != plan_id or (plan.get("body") or {}).get("id") != plan_id:
        raise HubError(f"the hub answered for plan {plan_id} with another plan")
    check_summary(plan, hub)
    if not isinstance(plan.get("label"), dict):
        raise HubError(f"the hub sent plan {plan_id} without its label")
    return plan


def read_plans(hub, root: Path, project: str, held: dict) -> dict:
    """``{"plans": [...]}``: each plan of ``project`` the caller sees, with the text of its copy at the hub's
    revision. ``held`` is what the connector's cursor keeps of each plan, the revision and label among it. HubError
    when the hub cannot be read."""
    listed = hub.call("GET", plans_path(project))
    if not isinstance(listed, list):
        raise HubError(f"the hub at {hub.url} did not answer with a list of plans")
    plans = []
    for summary in sorted(listed, key=lambda s: str(s.get("plan_id"))):
        check_summary(summary, hub)
        plan_id = str(summary.get("plan_id"))
        known = held.get(plan_id) if isinstance(held.get(plan_id), dict) else {}
        same = (known.get("revision"), known.get("digest")) == (summary["revision"], summary["digest"])
        text = current_copy(mirror_path(root, summary.get("area"), plan_id), summary, project) if same else None
        label = known.get("label")
        if text is None or not isinstance(label, dict):
            summary = _fetched(hub, project, plan_id)
            text = render(summary["body"], project, summary["revision"], summary["digest"])
            label = summary["label"]
        mirror_path(root, summary.get("area"), plan_id)  # an area or id no copy could have is refused
        plans.append(
            {
                "plan_id": plan_id,
                "area": summary["area"],
                "revision": summary["revision"],
                "digest": summary["digest"],
                "label": label,
                "text": text,
                "updated_at": summary.get("updated_at"),
                "url": plan_url(hub, project, plan_id),
            }
        )
    return {"plans": plans}


def reader(hub=None) -> Callable:
    """What ``kg sync`` passes as ``ConnectorContext.plans``: ``read(harness, project, held)`` gives None when this
    machine is not signed in to a hub, ``{"error": ...}`` when the hub could not be read, else ``read_plans``. The
    hub client is made on the first call, from the credentials of ``hub login``, unless ``hub`` is given."""
    made: dict = {"hub": hub}

    def read(harness, project: str, held: dict | None) -> dict | None:
        try:
            if made["hub"] is None:
                credentials = load_credentials()
                made["hub"] = Hub(credentials.url, credentials.token)
            return read_plans(made["hub"], Path(harness.root), project, held or {})
        except NotSignedIn:
            return None
        except HubError as exc:
            return {"error": f"the plans of hub project {project} were not read ({exc}); plans/ holds what was used"}

    return read
