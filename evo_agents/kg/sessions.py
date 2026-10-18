"""Session labels: the join of the labels of everything one Claude Code session read through the evo-kg tools.

``evo-agents kg hook post-tool`` runs after every evo-kg tool call and joins the labels in the result into
``<kg home>/sessions/<session_id>.json``, a file of mode 0600 in a 0700 directory::

    v, session_id, level, location, integrity, projects, results_read, last_updated

The record only rises: a join never lowers the level or the location, never turns integrity U back into T,
and never drops a project; a record that exists but cannot be read is left as it is, and the join fails rather
than start the record again from one result. It names levels and projects, never ids or text from the results.
kg_status and the SessionStart note print it.

In a session without the evo-kg plugin the same kg_* tools come from the hub, through ``evo-agents hub mcp``, which
joins each result into the same record with ``record_read`` itself (``evo_agents.hub.mcp_proxy.note_read``), so a
read of the graph counts whichever server answered it. TOOL_PREFIXES therefore stays evo-kg's alone: a hook wired by
hand to the hub's tools as well would count those reads twice.

The labels come from the result itself. The server sends an MCP client the text alone, with READ_META in
the ``_meta`` of its text block: the project, the join of every label the result reveals (nodes, edges,
evidence items) and how many elements with an id carry one. Claude Code 2.1.296 hands PostToolUse the
content blocks with their ``_meta`` (tests/kg/fixtures/hooks/post_tool_kg_context_text.json); a result
that kept its structuredContent comes as that JSON as a string (post_tool_kg_search.json), whose labels
are read as they are.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from evo_agents import filelock
from evo_agents.kg.corpus import kg_home
from evo_agents.kg.ids import utc_now
from evo_agents.kg.policy import Label, Policy
from evo_agents.kg.project import resolve_project

SESSION_VERSION = 1
# Tool names of the evo-kg server: from the plugin, or declared by hand under the name evo-kg.
TOOL_PREFIXES = ("mcp__plugin_evo-kg_evo-kg__", "mcp__evo-kg__")
READ_META = "evo-agents/read"  # the _meta key of a result's text block: {project, label, results_read}
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def sessions_dir(home: Path | None = None) -> Path:
    return (home or kg_home()) / "sessions"


def safe_session_id(session_id) -> str | None:
    """The session id as a file name. Claude Code sends a UUID, kept as is; anything else keeps only letters,
    digits, - and _, plus a hash of the original so two different ids never share a file."""
    if not isinstance(session_id, str) or not session_id:
        return None
    clean = _UNSAFE.sub("_", session_id)[:96]
    if clean == session_id:
        return clean
    digest = hashlib.sha256(session_id.encode("utf-8", "surrogatepass")).hexdigest()[:16]
    return f"{clean.strip('_')[:64] or 'session'}-{digest}"


def session_path(session_id, home: Path | None = None) -> Path | None:
    name = safe_session_id(session_id)
    return None if name is None else sessions_dir(home) / f"{name}.json"


def current_session_id(payload: dict | None = None) -> str | None:
    """The Claude Code session id: from a hook's stdin, else CLAUDE_CODE_SESSION_ID, which Claude Code sets for
    hooks and for the MCP servers it starts. A server keeps the id it started with."""
    sid = (payload or {}).get("session_id")
    return sid if isinstance(sid, str) and sid else os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def read_session(session_id, home: Path | None = None) -> dict | None:
    path = session_path(session_id, home)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def describe(record: dict) -> str:
    location = record.get("location")
    where = "" if location in (None, "any") else f"/{location}"
    projects = ", ".join(record.get("projects") or []) or "no project"
    return (
        f"session label {record.get('level')}{where},{record.get('integrity')} over {projects}:"
        f" {record.get('results_read', 0)} result(s) read, last at {record.get('last_updated')}"
    )


def structured_result(tool_response) -> dict | None:
    """What an evo-kg result says it reveals, as PostToolUse receives it: the READ_META of a content block, from
    Claude Code 2.1.296; else the structuredContent, as a JSON string, a whole MCP result or a JSON text block.
    None when it holds no JSON object."""
    if isinstance(tool_response, str):
        try:
            tool_response = json.loads(tool_response)
        except ValueError:
            return None
    if isinstance(tool_response, list):
        for block in tool_response:
            meta = block.get("_meta") if isinstance(block, dict) else None
            if isinstance(meta, dict) and isinstance(meta.get(READ_META), dict):
                return meta[READ_META]
        for block in tool_response:
            if isinstance(block, dict) and block.get("type") == "text":
                found = structured_result(block.get("text"))
                if found is not None:
                    return found
        return None
    if not isinstance(tool_response, dict):
        return None
    if isinstance(tool_response.get("structuredContent"), dict):
        return tool_response["structuredContent"]
    if isinstance(tool_response.get("content"), list):
        return structured_result(tool_response["content"])
    return tool_response


def labels_in(data) -> tuple[list[dict], int]:
    """Every label in a result, and how many elements with an id carry one. Properties are skipped: they hold
    what a source said, and a label lives on the element, not in its text."""
    labels: list[dict] = []
    ids: set[str] = set()
    stack = [data]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            label = current.get("label")
            if isinstance(label, dict) and "level" in label:
                labels.append(label)
                if isinstance(current.get("id"), str):
                    ids.add(current["id"])
            stack.extend(v for k, v in current.items() if k not in ("label", "props"))
        elif isinstance(current, list):
            stack.extend(current)
    return labels, len(ids)


def _policy(project: str | None) -> Policy:
    """The policy that orders level and location names: of the project the result names, else of the project
    the session is bound to (as for kg serve: CLAUDE_PROJECT_DIR or the cwd), else the default names."""
    try:
        return resolve_project(project).policy
    except Exception:  # a project that does not load still gets its reads recorded
        return Policy(project or "", {})


def _vocabulary(known: list[str], names) -> list[str]:
    """Known names in policy order, then names the policy does not know, which rank above all of them."""
    out = list(known)
    for name in names:
        if isinstance(name, str) and name not in out:
            out.append(name)
    return out


def _rank(names: list[str], name) -> int:
    return names.index(name) if name in names else len(names) - 1  # missing: the highest (fail closed)


@contextlib.contextmanager
def _locked(path: Path):
    """Serialise read-join-write across hooks of parallel tool calls, so no join is lost."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        filelock.lock(fd)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def _stored(path: Path) -> dict:
    """The record at ``path`` that a join starts from: {} when there is none yet. Any other failure to read it, such
    as a file this process may not read or one that holds no JSON object, raises, so a join never replaces a record
    it could not read with one that has lost its levels and projects."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"the session record {path.name} holds no JSON object")
    return data


def _write(path: Path, record: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")  # mode 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def record_read(session_id, data: dict, home: Path | None = None) -> dict | None:
    """Join the labels in one evo-kg result into the session's record; return the new record, or None when the
    session id is unusable or the result names no label (an error, kg_status, kg_more). ``data`` is a READ_META,
    whose results_read gives the count, or a structuredContent, whose labelled elements are counted. Raises, and
    writes nothing, when the record exists but cannot be read (``_stored``)."""
    path = session_path(session_id, home)
    found, count = labels_in(data)
    given = data.get("results_read")
    if isinstance(given, int) and not isinstance(given, bool) and given >= 0:
        count = given
    if path is None or not found:
        return None
    project = data.get("project") if isinstance(data.get("project"), str) else None
    policy = _policy(project)
    project = project or policy.project or None

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    with _locked(path.parent / ".lock"):
        old = _stored(path)
        entries = [*found, old] if old else found
        levels = _vocabulary(policy.levels, (e.get("level") for e in entries))
        locations = _vocabulary(policy.locations, (e.get("location") for e in entries))
        label: Label | None = None
        for entry in entries:
            projects = entry.get("projects")
            if not isinstance(projects, list):
                projects = [project] if project else []
            one = Label(
                _rank(levels, entry.get("level")),
                _rank(locations, entry.get("location")),
                "T" if entry.get("integrity") == "T" else "U",
                frozenset(p for p in projects if isinstance(p, str)),
            )
            label = one if label is None else label.join(one)
        previous = old.get("results_read")
        record = {
            "v": SESSION_VERSION,
            "session_id": session_id,
            "level": levels[label.level],
            "location": locations[label.location],
            "integrity": label.integrity,
            "projects": sorted(label.projects),
            "results_read": (previous if isinstance(previous, int) else 0) + count,
            "last_updated": utc_now(),
        }
        _write(path, record)
    return record


def post_tool(payload, home: Path | None = None) -> dict | None:
    """PostToolUse for the evo-kg tools: the new session record, or None when the call is not an evo-kg tool
    or there is nothing to record."""
    if not isinstance(payload, dict):
        return None
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not tool.startswith(TOOL_PREFIXES):
        return None
    data = structured_result(payload.get("tool_response"))
    if data is None:
        return None
    return record_read(payload.get("session_id"), data, home)
