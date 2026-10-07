"""``evo-agents hub registry pull``: the hub projects a person sees, as clusters of the harness registry here.

The registry is the JSON file of the harness-engineering skill (``~/.claude/harness/registry.json`` unless told
otherwise), ``{"clusters": [{name, root, workspace, repos, registered_at}, ...]}``, which evo-agents reads too. Each
project the hub lists becomes the cluster named as its harness.yaml is, with absolute paths of this machine: the
workspace is ``--workspace`` or the one the project was registered with (``~/github`` stays ``~/github``, under
this home), and the harness root and repos are placed in it. A cluster of the same name or root is updated in
place, a new one is appended, and every other cluster, and every other key of the file, is kept as it was.

Nothing is written when nothing changes. Otherwise the file as it was read is saved first as
``registry.json.bak.<UTC timestamp>``, then the new content goes to a temporary file in the same directory and is
renamed over the registry, so a crash leaves the old file or the new one. A registry another program changed
while the pull ran is not overwritten. Standard library only.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from evo_agents.harness import MANIFEST, SKILL_REGISTRY
from evo_agents.hub.client import Hub, HubError, write_atomic
from evo_agents.hub.registration import place

DEFAULT_MODE = 0o644
UNCHANGED_KEYS = ("registered_at",)  # a cluster is unchanged when it differs in these only


@dataclass
class PullResult:
    registry: Path
    backup: Path | None = None
    clusters: list[dict] = field(default_factory=list)  # {name, project, root, status, present}
    skipped: list[str] = field(default_factory=list)  # one sentence per project left out

    @property
    def changed(self) -> bool:
        return any(c["status"] != "unchanged" for c in self.clusters)

    def as_json(self) -> dict:
        return {
            "registry": str(self.registry),
            "backup": str(self.backup) if self.backup else None,
            "clusters": self.clusters,
            "skipped": self.skipped,
        }


def default_registry() -> Path:
    return SKILL_REGISTRY.expanduser()


def _now() -> datetime:
    return datetime.now(UTC)


def cluster_of(project: dict, hub_url: str, workspace: Path | None) -> dict | None:
    """The registry cluster of a hub project on this machine; None for a project registered without paths."""
    harness = project.get("harness")
    if not harness:
        return None
    base = workspace if workspace is not None else Path(os.path.normpath(Path(harness["workspace"]).expanduser()))
    return {
        "name": harness["name"],
        "root": str(place(harness["path"], base)),
        "workspace": str(base),
        "repos": [str(place(repo.get("path") or repo["name"], base)) for repo in project.get("repos") or []],
        "hub": {"url": hub_url, "project": project["name"]},
    }


def _comparable(cluster: dict) -> dict:
    found = {key: value for key, value in cluster.items() if key not in UNCHANGED_KEYS}
    if isinstance(found.get("repos"), list):
        found["repos"] = sorted(found["repos"], key=str)  # the same repos in another order are the same cluster
    return found


def _same_cluster(held, cluster: dict) -> bool:
    return isinstance(held, dict) and (held.get("name") == cluster["name"] or held.get("root") == cluster["root"])


def merge(registry: dict, wanted: list[dict], now: str) -> list[tuple[dict, str]]:
    """Put the ``wanted`` clusters into ``registry`` in place; each with its status: added, updated, unchanged."""
    clusters = registry.setdefault("clusters", [])
    outcomes = []
    for cluster in wanted:
        index = next((i for i, held in enumerate(clusters) if _same_cluster(held, cluster)), None)
        if index is None:
            clusters.append({**cluster, "registered_at": now})
            outcomes.append((cluster, "added"))
            continue
        held = clusters[index]
        updated = {**held, **cluster}
        if _comparable(updated) == _comparable(held):
            outcomes.append((cluster, "unchanged"))
            continue
        clusters[index] = {**updated, "registered_at": now}
        outcomes.append((cluster, "updated"))
    return outcomes


def read_registry(path: Path) -> tuple[dict, bytes | None]:
    """The registry at ``path`` and its bytes as read; an empty registry and None when there is no file."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"clusters": []}, None
    except OSError as exc:
        raise HubError(f"cannot read the registry {path}: {exc}") from None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise HubError(f"the registry {path} is not valid JSON ({exc}); fix or move it, nothing changed") from None
    if not isinstance(data, dict) or not isinstance(data.get("clusters", []), list):
        raise HubError(f"the registry {path} is not an object with a clusters list; fix or move it, nothing changed")
    return data, raw


def _backup_path(path: Path, moment: datetime) -> Path:
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.bak.{stamp}")
    number = 1
    while candidate.exists():  # two pulls within a second
        number += 1
        candidate = path.with_name(f"{path.name}.bak.{stamp}-{number}")
    return candidate


def pull(hub: Hub, registry: Path | None = None, workspace: Path | None = None) -> PullResult:
    """Write the clusters of the hub projects the caller sees into ``registry``."""
    path = (registry or default_registry()).expanduser()
    result = PullResult(path)
    projects = hub.call("GET", "/v1/projects")
    if not isinstance(projects, list):
        raise HubError(f"the hub at {hub.url} did not answer with a list of projects")
    wanted, names = [], set()
    for project in projects:
        cluster = cluster_of(project, hub.url, workspace)
        if cluster is None:
            result.skipped.append(
                f"{project['name']}: registered without its harness paths; run `evo-agents hub project register` "
                "in its harness"
            )
        elif cluster["name"] in names:
            result.skipped.append(f"{project['name']}: another hub project already has the cluster {cluster['name']}")
        else:
            names.add(cluster["name"])
            wanted.append(cluster)

    data, original = read_registry(path)
    moment = _now()
    outcomes = merge(data, wanted, moment.isoformat(timespec="seconds"))
    result.clusters = [
        {
            "name": cluster["name"],
            "project": cluster["hub"]["project"],
            "root": cluster["root"],
            "status": status,
            "present": (Path(cluster["root"]) / MANIFEST).is_file(),
        }
        for cluster, status in outcomes
    ]
    if not result.changed:
        return result

    path.parent.mkdir(parents=True, exist_ok=True)
    mode = DEFAULT_MODE
    if original is not None:
        mode = os.stat(path).st_mode & 0o777
        result.backup = _backup_path(path, moment)
        write_atomic(result.backup, original, mode)
    content = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        current = None
    if current != original:  # another program wrote the registry since it was read: keep its version
        if result.backup is not None:
            with contextlib.suppress(OSError):
                result.backup.unlink()
        raise HubError(f"the registry {path} changed while the pull ran; nothing was written, run it again")
    write_atomic(path, content, mode)
    return result
