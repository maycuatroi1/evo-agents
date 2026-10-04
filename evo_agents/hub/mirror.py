"""The read-only copy of a hub plan that git keeps: how it is written, read back, exported and committed.

A copy is ``plans/<area>/<plan id>.yaml`` in the harness. Its first line is MIRROR_HEADER, then the plan, then the
hub key ``{project, revision, digest}``; the digest is ``evo_agents.harness.plan_digest`` of the plan, so
``evo-agents harness validate`` tells a copy someone edited by hand from one the hub wrote.

The text is a function of the plan alone. Postgres keeps a plan as jsonb, which drops key order, so keys are
written in one fixed order: the order of plan.schema.json for the plan, its steps, repos and hub key, a
conventional order for the items of the other sections, and alphabetical order for every key neither names. Text
longer than LONG_TEXT characters is a folded block, text holding line breaks a literal block, and a list of short
scalars a flow list. ``render`` reads the text back before returning it and falls back to double-quoted text if a
block style would change a value, so a copy always holds exactly the plan it claims to.

``export`` writes the copy of every plan of a project the caller sees, each file through a temporary file renamed
over it, removes the copy left in the other area when a plan moved (completed), and leaves every other file alone.
With ``commit`` it commits exactly the copies that differ from HEAD, so other changes in the checkout, staged or
not, stay out of the commit. With ``keep_edited``, as the SessionStart hook of the evo-hub plugin runs it, a file
that is not a copy the hub wrote as it stands (edited by hand, or never a copy) is left as it is and reported, so an
export nobody asked for never overwrites someone's work. Standard library and PyYAML only.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import yaml

from evo_agents.harness import (
    HUB_KEY,
    find_manifest,
    load_manifest,
    load_schema,
    load_yaml,
    parse_yaml,
    plan_body,
    plan_digest,
)
from evo_agents.hub.client import HubError, write_atomic
from evo_agents.hub.plans import AREAS
from evo_agents.kg.protocol import canonical_json

MIRROR_HEADER = (
    "# Mirror of evo-agents hub plan {plan_id}, revision {revision}. Do not edit; use evo harness step or "
    "evo-agents hub plan."
)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
PLAN_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,99}")  # also the file name, so nothing else reaches a path
WIDTH = 110
LONG_TEXT = 100
SHORT_ITEM = 60  # a list whose scalars are all at most this long is written [a, b, c]
FILE_MODE = 0o644
GIT_TIMEOUT = 120

_SCHEMA = load_schema("plan")
_ORDER = {
    None: tuple(_SCHEMA["properties"]),
    "steps": tuple(_SCHEMA["$defs"]["step"]["properties"]),
    "repos": tuple(_SCHEMA["$defs"]["repo"]["properties"]),
    HUB_KEY: tuple(_SCHEMA["$defs"]["hub"]["properties"]),
    "acceptance": ("criterion", "what", "status", "verify", "evidence"),
    "references": ("what", "where", "why"),
    "seams_touched": ("name", "owner", "consumers", "verify", "note", "notes"),
    "risks": ("risk", "mitigation"),
    "decisions": ("date", "by", "decision", "what", "status", "why", "note"),
    "tech_debt": ("issue", "what", "severity", "status", "fixed_at", "note"),
    "open_questions": ("issue", "what", "status", "answered_at", "blocking", "note"),
}


def _ordered(mapping: dict, order: tuple[str, ...]) -> dict:
    known = [key for key in order if key in mapping]
    rest = sorted(key for key in mapping if key not in order)
    return {key: mapping[key] for key in (*known, *rest)}


def _sorted_value(value, order: tuple[str, ...] = ()):
    """``value`` with every mapping in its written order: ``order`` for this one, alphabetical below it."""
    if isinstance(value, dict):
        return {key: _sorted_value(item) for key, item in _ordered(value, order).items()}
    if isinstance(value, list):
        return [_sorted_value(item, order) for item in value]
    return value


def ordered_plan(body: dict, hub: dict | None = None) -> dict:
    """The plan (and its hub key last) in the order a copy writes it."""
    plan = {}
    for key, value in _ordered(body, _ORDER[None]).items():
        plan[key] = _sorted_value(value, _ORDER.get(key, ()))
    if hub is not None:
        plan[HUB_KEY] = _ordered(hub, _ORDER[HUB_KEY])
    return plan


class _Dumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)  # lists indented under their key, as people write plans


class _BlockDumper(_Dumper):
    pass


class _QuotedDumper(_Dumper):
    pass


BREAKS = ("\r", "\x85", "\u2028", "\u2029")  # line breaks YAML folds in plain and single-quoted text


def _text(dumper, value: str):
    inner = value[:-1] if value.endswith("\n") else value
    if any(mark in value for mark in BREAKS):
        style = '"'
    elif "\n" in inner:
        style = "|"
    else:
        style = ">" if len(value) > LONG_TEXT or value.endswith("\n") else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


def _quoted(dumper, value: str):
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style='"')


def _short(value) -> bool:
    if isinstance(value, str):
        return "\n" not in value and len(value) <= SHORT_ITEM
    return value is None or isinstance(value, (bool, int, float))


def _list(dumper, value: list):
    flow = bool(value) and all(_short(item) for item in value)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", value, flow_style=flow)


_BlockDumper.add_representer(str, _text)
_BlockDumper.add_representer(list, _list)
_QuotedDumper.add_representer(str, _quoted)


def _dump(plan: dict, dumper) -> str:
    return yaml.dump(plan, Dumper=dumper, allow_unicode=True, sort_keys=False, width=WIDTH, default_flow_style=False)


def _reads_back(text: str, plan: dict) -> bool:
    try:
        return canonical_json(parse_yaml(text)) == canonical_json(plan)
    except yaml.YAMLError:
        return False


def render(body: dict, project: str, revision: int, digest: str) -> str:
    """The text of the copy of revision ``revision`` of a plan of ``project`` whose digest is ``digest``."""
    if not PLAN_ID.fullmatch(str(body.get("id"))):
        raise HubError(f"the hub sent a plan whose id {body.get('id')!r} is not a plan id")
    plan = ordered_plan(body, {"project": project, "revision": revision, "digest": digest})
    header = MIRROR_HEADER.format(plan_id=body["id"], revision=revision) + "\n"
    for dumper in (_BlockDumper, _QuotedDumper):  # the second escapes every character a block could change
        text = header + _dump(plan, dumper)
        if _reads_back(text, plan):
            return text
    raise HubError(f"plan {body['id']} cannot be written as YAML that reads back the same; nothing was written")


# Reading plan files


@dataclass(frozen=True)
class PlanFile:
    path: Path
    area: str | None  # active or completed, from the directory; None outside plans/<area>/
    body: dict  # without the hub key
    hub: dict | None  # the hub key as the file holds it

    @property
    def plan_id(self) -> str | None:
        return self.body.get("id") if isinstance(self.body.get("id"), str) else None

    @property
    def digest(self) -> str:
        return plan_digest(self.body)


def read_plan(path: Path) -> PlanFile:
    """A plan file; HubError naming the file when it is not YAML holding a mapping."""
    try:
        data = load_yaml(path)
    except (OSError, yaml.YAMLError) as exc:
        raise HubError(f"cannot read {path}: {exc}") from None
    if not isinstance(data, dict):
        raise HubError(f"{path} does not hold a plan (a YAML mapping with an id)")
    area = path.parent.name if path.parent.name in AREAS and path.parent.parent.name == "plans" else None
    hub = data.get(HUB_KEY) if isinstance(data.get(HUB_KEY), dict) else None
    return PlanFile(path, area, plan_body(data), hub)


def harness_project(root: Path, project: str | None = None) -> str:
    """The hub project of the harness at ``root``: ``project`` when given (it must agree with harness.yaml), else
    harness.yaml's hub.project, else the project of knowledge.yaml, else the harness name."""
    harness = load_manifest(root)
    hub = harness.manifest.get(HUB_KEY)
    named = hub.get("project") if isinstance(hub, dict) else None
    if project and named and project != named:
        raise HubError(f"{root}: harness.yaml says hub.project {named!r}, not {project!r}")
    if project or named:
        return project or named
    kpath = harness.knowledge_path()
    knowledge = load_yaml(kpath) if kpath is not None and kpath.is_file() else None
    found = knowledge.get("project") if isinstance(knowledge, dict) else None
    return found or harness.name


def harness_root(start: Path | str | None) -> Path:
    where = Path(start or Path.cwd()).expanduser()
    root = find_manifest(where)
    if root is None:
        raise HubError(f"no harness.yaml at or above {where}")
    return root


# Export


def plans_path(project: str, plan_id: str | None = None) -> str:
    base = f"/v1/projects/{quote(project, safe='')}/plans"
    return base if plan_id is None else f"{base}/{quote(plan_id, safe='')}"


def mirror_path(root: Path, area: str, plan_id: str) -> Path:
    if area not in AREAS or not PLAN_ID.fullmatch(plan_id):
        raise HubError(f"the hub sent plan {plan_id!r} in area {area!r}, which has no place in plans/")
    return root / "plans" / area / f"{plan_id}.yaml"


@dataclass
class ExportResult:
    root: Path
    project: str
    plans: list[dict] = field(default_factory=list)  # {plan_id, area, revision, path, status}
    removed: list[str] = field(default_factory=list)  # copies left in the other area, relative to root
    notes: list[str] = field(default_factory=list)
    commit: str | None = None

    @property
    def changed(self) -> list[str]:
        return [p["path"] for p in self.plans if p["status"] == "written"] + self.removed


def _check_summary(summary: dict, hub) -> None:
    revision, digest = summary.get("revision"), summary.get("digest")
    if type(revision) is not int or revision < 1 or not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise HubError(f"the hub at {hub.url} described plan {summary.get('plan_id')} without a revision and digest")


def _is_copy(path: Path, project: str) -> bool:
    try:
        found = read_plan(path).hub
    except HubError:
        return False
    return found is not None and found.get("project") == project


def write_copy(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` atomically, keeping the mode of the file it replaces."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = os.stat(path).st_mode & 0o777 if path.exists() else FILE_MODE
        write_atomic(path, text.encode("utf-8"), mode)
    except OSError as exc:
        raise HubError(f"cannot write {path} ({exc}); the file is as it was") from None


def _intact_copy(path: Path, project: str) -> bool:
    """Whether ``path`` holds a copy of a plan of ``project`` exactly as the hub wrote it: its digest is the digest
    of what it holds now."""
    try:
        found = read_plan(path)
    except HubError:
        return False
    hub = found.hub or {}
    return hub.get("project") == project and hub.get("digest") == found.digest


def _edited_note(relative: str, path: Path, project: str) -> str:
    """Why ``path`` was left as it is, with the two ways out, as ``harness validate`` words them."""
    try:
        hub = read_plan(path).hub
    except HubError:
        hub = None
    if hub is None or hub.get("project") != project:
        return (
            f"{relative} is not a copy of a plan of hub project {project}, so the export left it as it is. Push it "
            f"with `evo-agents hub plan put {relative}` or restore the hub's copy with `evo-agents hub plan export .`"
        )
    revision = hub.get("revision") if isinstance(hub.get("revision"), int) else "N"
    return (
        f"{relative} was edited outside the hub (digest mismatch), so the export left it as it is. Push it with "
        f"`evo-agents hub plan put {relative} --if-revision {revision}` or restore it with "
        "`evo-agents hub plan export .`"
    )


def _current_copy(path: Path, summary: dict, project: str) -> str | None:
    """The text a copy at ``path`` would get, when the file already holds the plan ``summary`` describes, so the
    plan need not be fetched; None when it must be."""
    try:
        found = read_plan(path)
    except HubError:
        return None
    hub = found.hub or {}
    same = (hub.get("project"), hub.get("revision"), hub.get("digest")) == (
        project,
        summary["revision"],
        summary["digest"],
    )
    if not same or found.digest != summary["digest"]:
        return None
    return render(found.body, project, summary["revision"], summary["digest"])


def export(
    hub, root: Path, project: str | None = None, *, commit: bool = False, keep_edited: bool = False
) -> ExportResult:
    """Write the copy of every plan of the project that the caller sees into the harness at ``root``. With
    ``keep_edited``, a file at a copy's path that is not that copy as the hub wrote it stays as it is (status
    ``kept``, and a note)."""
    root = harness_root(root)
    project = harness_project(root, project)
    result = ExportResult(root, project)
    listed = hub.call("GET", plans_path(project))
    if not isinstance(listed, list):
        raise HubError(f"the hub at {hub.url} did not answer with a list of plans")
    on_hub = set()
    for summary in sorted(listed, key=lambda s: str(s.get("plan_id"))):
        plan_id, area = summary.get("plan_id"), summary.get("area")
        path = mirror_path(root, area, str(plan_id))
        _check_summary(summary, hub)
        on_hub.add(plan_id)
        relative = path.relative_to(root).as_posix()
        if keep_edited and os.path.lexists(path) and not _intact_copy(path, project):
            result.notes.append(_edited_note(relative, path, project))
            result.plans.append(
                {"plan_id": plan_id, "area": area, "revision": summary["revision"], "path": relative, "status": "kept"}
            )
            continue
        text = _current_copy(path, summary, project)
        if text is None:
            plan = hub.call("GET", plans_path(project, plan_id))
            if plan.get("plan_id") != plan_id or plan.get("body", {}).get("id") != plan_id:
                raise HubError(f"the hub answered for plan {plan_id} with another plan; nothing more was written")
            _check_summary(plan, hub)
            text = render(plan["body"], project, plan["revision"], plan["digest"])
            summary = plan
        old = path.read_bytes() if path.exists() else None
        status = "unchanged" if old == text.encode("utf-8") else "written"
        if status == "written":
            write_copy(path, text)
        other = mirror_path(root, "completed" if area == "active" else "active", plan_id)
        if other.exists():
            if _intact_copy(other, project) if keep_edited else _is_copy(other, project):
                try:
                    other.unlink()
                except OSError as exc:
                    raise HubError(f"cannot remove {other}, the old copy of plan {plan_id}: {exc}") from None
                result.removed.append(other.relative_to(root).as_posix())
            else:
                result.notes.append(
                    f"{other.relative_to(root).as_posix()}: plan {plan_id} is {area} on the hub, and this file is "
                    "not a copy the hub wrote; left as it is, delete it once nothing in it is needed"
                )
        result.plans.append(
            {"plan_id": plan_id, "area": area, "revision": summary["revision"], "path": relative, "status": status}
        )
    for path in sorted((root / "plans").glob("*/*.yaml")):
        if path.stem in on_hub or path.parent.name not in AREAS:
            continue
        relative = path.relative_to(root).as_posix()
        if _is_copy(path, project):
            result.notes.append(f"{relative}: a copy of a plan the hub no longer shows you; left as it is")
        else:
            result.notes.append(f"{relative}: not on the hub; push it with `evo-agents hub plan put {relative}`")
    if commit:
        result.commit = commit_copies(root, result)
    return result


# Committing the copies


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    command = ["git", "-C", str(root), *args]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HubError(f"git {args[0]} failed in {root}: {exc}") from None
    if check and done.returncode != 0:
        raise HubError(f"git {args[0]} failed in {root}: {(done.stderr or done.stdout).strip()}")
    return done


def _differs_from_head(root: Path, paths: list[str]) -> list[str]:
    if not paths:
        return []
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *paths).stdout
    found = []
    entries = status.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":  # a rename names its source next; a copy is never staged by export
            index += 1
        found.append(path)
    return sorted(set(found))


def commit_copies(root: Path, result: ExportResult) -> str | None:
    """Commit the copies of the project that differ from HEAD, and nothing else; the new commit, or None when
    every copy already matches HEAD."""
    inside = _git(root, "rev-parse", "--is-inside-work-tree", check=False)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise HubError(f"--commit needs a git checkout, and {root} is not one; the copies are written")
    prefix = _git(root, "rev-parse", "--show-prefix").stdout.strip()
    copies = [p for p in result.plans if p["status"] != "kept"]  # a file someone edited is theirs to commit
    candidates = {p["path"] for p in copies} | set(result.removed)
    for entry in copies:  # a copy removed by an earlier export without --commit is still a change
        other = f"plans/{'completed' if entry['area'] == 'active' else 'active'}/{entry['plan_id']}.yaml"
        if not (root / other).exists():  # one still there is a file the hub did not write: never committed
            candidates.add(other)
    changed = [path[len(prefix) :] for path in _differs_from_head(root, sorted(candidates))]
    if not changed:
        return None
    revisions = {f"plans/{p['area']}/{p['plan_id']}.yaml": p for p in copies}
    named = [f"{revisions[path]['plan_id']} r{revisions[path]['revision']}" for path in changed if path in revisions]
    removed = [path for path in changed if path not in revisions]
    lines = [f"Hub plans of {result.project}: copies of {', '.join(named) or 'no plan'}"]
    if removed:
        lines += ["", "Removed copies of plans that moved: " + ", ".join(removed)]
    _git(root, "add", "-A", "--", *changed)
    _git(root, "commit", "--quiet", "--only", "-m", "\n".join(lines), "--", *changed)
    return _git(root, "rev-parse", "HEAD").stdout.strip()
