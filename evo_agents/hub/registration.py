"""What ``evo-agents hub project register`` sends: a harness's project, read with the harness loader.

The project is the one knowledge.yaml names, or the harness.yaml name when there is no knowledge.yaml; a
``hub: {project}`` in harness.yaml must agree with it. The ladder and the sinks come from knowledge.yaml's policy
(the default ladder without one), and the default label of the project's memories and plans is the label of its
harness source, read the way the knowledge graph reads it: no harness source, or one without a label, is the
highest level (fail closed). Files with errors are refused before anything is sent.

Paths are sent so that another machine can rebuild them: the workspace as ``~/...`` when it is under the home
directory, and the harness root and every repo relative to the workspace when they are inside it (``~/...`` or
absolute otherwise). ``place`` turns them back into paths on the machine that reads them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from evo_agents.harness import find_manifest, load_manifest, load_yaml, validate_harness
from evo_agents.hub.access import ProjectRules, Refused
from evo_agents.hub.client import HubError
from evo_agents.kg.policy import Policy
from evo_agents.schema import errors

PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")  # as the hub accepts it
SHOWN_ERRORS = 5


@dataclass(frozen=True)
class Registration:
    project: str
    root: Path
    body: dict

    def rules(self) -> ProjectRules:
        """The read and write rules the hub will apply to the project, as registered by this body."""
        body = self.body
        return ProjectRules(self.project, body["levels"], body["locations"], body["sinks"], body["default_label"])

    def push_warning(self) -> str | None:
        """Why the hub will refuse memories and plans pushed without a label of their own, if it will."""
        try:
            self.rules().check_push(None, "writer")
        except Refused as exc:
            return str(exc)
        return None


def _real(path: Path) -> Path:
    return Path(os.path.realpath(path.expanduser()))


def portable(path: Path) -> str:
    """``path`` as ``~/...`` when it is under the home directory, so it means the same on another machine."""
    real, home = _real(path), _real(Path.home())
    if real == home:
        return "~"
    if real.is_relative_to(home):
        return "~/" + real.relative_to(home).as_posix()
    return str(real)


def relative(path: Path, workspace: Path) -> str:
    """``path`` relative to ``workspace`` when it is inside it, else as ``portable`` writes it."""
    real, base = _real(path), _real(workspace)
    if real != base and real.is_relative_to(base):
        return real.relative_to(base).as_posix()
    return portable(real)


def place(text: str, workspace: Path) -> Path:
    """A path the hub holds (``~/...``, absolute, or relative to the workspace) on this machine."""
    path = Path(text).expanduser()
    return Path(os.path.normpath(path if path.is_absolute() else workspace / path))


def _text(value) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def registration(start: str | Path | None = None) -> Registration:
    """The registration of the harness at or above ``start`` (default: the current directory). Raises HubError
    naming what to fix when the harness cannot be registered."""
    where = Path(start or Path.cwd()).expanduser()
    root = find_manifest(where)
    if root is None:
        raise HubError(f"no harness.yaml at or above {where}")
    reports = [r for r in validate_harness(root) if r.kind in ("harness", "knowledge")]
    problems = [f"{report.path.name}: {issue}" for report in reports for issue in errors(report.issues)]
    if problems:
        shown = "; ".join(problems[:SHOWN_ERRORS])
        more = f" and {len(problems) - SHOWN_ERRORS} more" if len(problems) > SHOWN_ERRORS else ""
        raise HubError(f"fix the harness first: {shown}{more}; `evo-agents harness validate {root}` lists them all")

    harness = load_manifest(root)
    kpath = harness.knowledge_path()
    knowledge = load_yaml(kpath) if kpath is not None and kpath.is_file() else None
    knowledge = knowledge if isinstance(knowledge, dict) else {}
    project = knowledge.get("project") or harness.manifest.get("name")
    if not isinstance(project, str) or not PROJECT_NAME.fullmatch(project):
        raise HubError(f"{root}: the project name {project!r} is not a hub project name (a-z, 0-9 and -)")
    hub = harness.manifest.get("hub")
    named = hub.get("project") if isinstance(hub, dict) else None
    if named is not None and named != project:
        raise HubError(f"{root}: harness.yaml says hub.project {named!r}, but the project is {project!r}")

    policy = Policy(project, knowledge)
    sources = [s for s in knowledge.get("sources") or [] if isinstance(s, dict)]
    harness_source = next((s.get("id") for s in sources if s.get("connector") == "harness"), None)
    label = policy.describe(policy.source_label(harness_source))  # no such source: the highest level, fail closed
    sinks = []
    for sink in (knowledge.get("policy") or {}).get("sinks") or []:
        clearance = sink.get("clearance") or {}
        sinks.append(
            {
                "id": sink["id"],
                "kind": sink.get("kind", "agent-session"),
                "clearance": {key: clearance[key] for key in ("level", "location") if clearance.get(key) is not None},
            }
        )

    workspace = harness.workspace.expanduser()
    repos = []
    for repo in harness.repos():
        if not isinstance(repo, dict) or not _text(repo.get("name")):
            continue
        path = harness.repo_path(repo)
        repos.append(
            {
                "name": repo["name"],
                "origin": _text(repo.get("origin")),
                "default_branch": _text(repo.get("default_branch")),
                "path": relative(path, workspace) if path is not None else None,
            }
        )
    body = {
        "levels": policy.levels,
        "locations": policy.locations,
        "default_label": label,
        "sinks": sinks,
        "repos": repos,
        "harness": {"name": harness.name, "workspace": portable(workspace), "path": relative(harness.root, workspace)},
    }
    return Registration(project, harness.root, body)
