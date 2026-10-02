"""Per-item stages: map and structure. Each is a deterministic function of one item (and its text).

Outputs are lists of facts (JSON-serializable dicts, memoized):

    {"t": "node", "id", "kind", "name", "props", "status", "conf", "units", "needs", "where"}
    {"t": "edge", "src", "rel", "dst", "props", "status", "conf", "units", "needs", "where"}
    {"t": "ref", "src", "rel", "target": {"type", "value", ...}, "status", "units", "where"}
    {"t": "text", "src", "unit", "text", "field"}       free text the link stage scans
    {"t": "ident", "type", "key", "node", ...}            an identifier the index learns
    {"t": "issue", "message", "unit"}                     reported in coverage, never fatal

``units`` are ownership units: ``<item_id>#<anchor>`` for a fragment, ``<item_id>#`` for the whole item.
An element is alive while one of its derivations has every unit and every needed element alive.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

import yaml

from evo_agents.kg.extract import python as py_extract
from evo_agents.kg.schema import node_kind_for_item

MAP_VERSION = "1"
STRUCTURE_VERSION = "2"


def item_unit(item_id: str) -> str:
    return f"{item_id}#"


def frag_unit(item_id: str, anchor: str) -> str:
    return f"{item_id}#{anchor}"


def node(id, kind, name, units, *, props=None, status="parsed", conf=1.0, needs=(), where=()):
    return {
        "t": "node",
        "id": id,
        "kind": kind,
        "name": name,
        "props": props or {},
        "status": status,
        "conf": conf,
        "units": sorted(set(units)),
        "needs": sorted(set(needs)),
        "where": list(where),
    }


def edge(src, rel, dst, units, *, props=None, status="parsed", conf=1.0, needs=(), where=()):
    return {
        "t": "edge",
        "src": src,
        "rel": rel,
        "dst": dst,
        "props": props or {},
        "status": status,
        "conf": conf,
        "units": sorted(set(units)),
        "needs": sorted(set(needs)),
        "where": list(where),
    }


def ref(src, rel, target, units, *, status="parsed", where=()):
    return {
        "t": "ref",
        "src": src,
        "rel": rel,
        "target": target,
        "status": status,
        "units": sorted(set(units)),
        "where": list(where),
    }


def normalize_url(url: str) -> str:
    url = url.strip().rstrip(".,;:!?)]}>\"'")
    url = url.split("#", 1)[0].split("?", 1)[0]
    if "://" in url:
        scheme, rest = url.split("://", 1)
        host, _, path = rest.partition("/")
        url = f"{scheme.lower()}://{host.lower()}/{path}"
    return url.rstrip("/")


# ---------------------------------------------------------------------------------------------
# map


def map_item(record: dict, source_id: str) -> list[dict]:
    item_id = record["id"]
    unit = item_unit(item_id)
    kind = node_kind_for_item(record["kind"])
    props = {"item_kind": record["kind"], "rev": record.get("rev"), "source": source_id}
    for key in ("uri",):
        if record.get(key):
            props[key] = record[key]
    extra = record.get("props") or {}
    for key in ("path", "repo"):
        if extra.get(key):
            props[key] = extra[key]
    name = record.get("title") or extra.get("path") or item_id
    facts = [
        node(item_id, kind, name, [unit], props=props),
        node(f"source:{source_id}", "Source", source_id, [unit]),
        edge(item_id, "in_source", f"source:{source_id}", [unit]),
    ]
    if record.get("parent"):
        facts.append(edge(item_id, "child_of", record["parent"], [unit]))
    if record.get("uri"):
        facts.append({"t": "ident", "type": "url", "key": normalize_url(record["uri"]), "node": item_id})
    if extra.get("path"):
        facts.append(
            {
                "t": "ident",
                "type": "path",
                "key": extra["path"],
                "repo": extra.get("repo"),
                "source": source_id,
                "node": item_id,
            }
        )
        module = py_extract.module_name(extra["path"])
        if module:
            facts.append({"t": "ident", "type": "module", "key": module, "source": source_id, "node": item_id})
    facts.append({"t": "ident", "type": "id", "key": item_id, "node": item_id})
    if kind == "Document":
        for frag in record.get("fragments", []):
            anchor = frag["anchor"]
            if anchor == "_preamble":
                continue
            sid = f"{item_id}#{anchor}"
            funit = frag_unit(item_id, anchor)
            facts.append(
                node(
                    sid,
                    "Section",
                    frag.get("title") or anchor,
                    [funit],
                    props={"anchor": anchor, "level": frag.get("level", 0), "item": item_id, "span": frag.get("span")},
                    where=[[funit, frag.get("span") or ""]],
                )
            )
            parent = f"{item_id}#{frag['parent']}" if frag.get("parent") else item_id
            facts.append(edge(sid, "part_of", parent, [funit]))
            facts.append({"t": "ident", "type": "id", "key": sid, "node": sid})
    return facts


# ---------------------------------------------------------------------------------------------
# structure


def _yaml(text: str):
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def origin_url(origin: str) -> str | None:
    m = re.match(r"^(?:https?://(?:[^@/]+@)?|git@|ssh://git@)([^/:]+)[:/](.+?)(?:\.git)?/?$", origin.strip())
    return f"https://{m.group(1).lower()}/{m.group(2)}" if m else None


def _manifest(item_id: str, data: dict, unit: str, record: dict) -> list[dict]:
    facts = []
    repos = [r for r in data.get("repos") or [] if isinstance(r, dict) and r.get("name")]
    own = (record.get("props") or {}).get("repo")
    if own and own not in {r["name"] for r in repos}:
        repos.append({"name": own, "role": "harness"})
    for repo in repos:
        rid = f"repo:{repo['name']}"
        props = {k: repo[k] for k in ("role", "origin", "default_branch", "path") if repo.get(k)}
        facts.append(node(rid, "Repo", repo["name"], [unit], props=props))
        facts.append(edge(item_id, "declares", rid, [unit]))
        facts.append({"t": "ident", "type": "repo", "key": repo["name"], "node": rid})
        url = origin_url(repo.get("origin") or "")
        if url:
            facts.append({"t": "ident", "type": "url", "key": normalize_url(url), "node": rid})
    return facts


def _locations(source) -> list[dict]:
    if source is None:
        return []
    entries = source if isinstance(source, list) else [source]
    out = []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("path"):
            out.append({"repo": entry.get("repo"), "path": entry["path"]})
        elif isinstance(entry, str):
            out.append({"repo": None, "path": entry})
    return out


def _contracts(item_id: str, data: dict, unit: str) -> list[dict]:
    facts = []
    for seam in data.get("seams") or []:
        if not isinstance(seam, dict) or not seam.get("name"):
            continue
        sid = f"seam:{seam['name']}"
        props = {k: seam[k] for k in ("kind", "verify", "status") if seam.get(k)}
        facts.append(node(sid, "Seam", seam["name"], [unit], props=props))
        facts.append(edge(item_id, "declares", sid, [unit]))
        facts.append({"t": "ident", "type": "seam", "key": seam["name"], "node": sid})
        if seam.get("owner"):
            facts.append(ref(sid, "owned_by", {"type": "repo", "value": seam["owner"]}, [unit]))
        for consumer in seam.get("consumers") or []:
            facts.append(ref(sid, "consumed_by", {"type": "repo", "value": consumer}, [unit]))
        for loc in _locations(seam.get("source")):
            repo = loc["repo"] or seam.get("owner")
            facts.append(ref(sid, "defined_in", {"type": "path", "value": loc["path"], "repo": repo}, [unit]))
        for field_name in ("notes", "verify"):
            if isinstance(seam.get(field_name), str):
                facts.append({"t": "text", "src": sid, "unit": unit, "text": seam[field_name], "field": field_name})
    return facts


def _step_text(step: dict) -> list[tuple[str, str]]:
    out = []
    for key in ("title", "what", "verify", "evidence", "note", "why"):
        value = step.get(key)
        if isinstance(value, str) and value.strip():
            out.append((key, value))
    return out


def _plan(item_id: str, data: dict, unit: str) -> list[dict]:
    plan_id = data.get("id")
    if not isinstance(plan_id, str):
        return [{"t": "issue", "message": "plan without id", "unit": unit}]
    pid = f"plan:{plan_id}"
    steps = [s for s in data.get("steps") or [] if isinstance(s, dict)]
    statuses = [str(s.get("status", "pending")) for s in steps]
    props = {
        "goal": (data.get("goal") or "").strip()[:500],
        "steps": len(steps),
        "done": sum(1 for s in statuses if s == "done"),
    }
    facts = [
        node(pid, "Plan", data.get("title") or plan_id, [unit], props=props),
        edge(pid, "defined_in", item_id, [unit]),
        {"t": "ident", "type": "plan", "key": plan_id, "node": pid},
    ]
    for key in ("goal", "context"):
        if isinstance(data.get(key), str):
            facts.append({"t": "text", "src": pid, "unit": unit, "text": data[key], "field": key})
    for repo in data.get("repos") or []:
        if isinstance(repo, dict) and repo.get("repo"):
            facts.append(ref(pid, "touches", {"type": "repo", "value": repo["repo"]}, [unit]))
    for seam in data.get("seams_touched") or []:
        name = seam.get("name") if isinstance(seam, dict) else seam
        if isinstance(name, str):
            facts.append(ref(pid, "touches", {"type": "seam", "value": name}, [unit]))
    for index, step in enumerate(steps):
        number = step.get("id", index + 1)
        stid = f"{pid}/step:{number}"
        title = step.get("title") or (step.get("what") or "").strip().split("\n")[0][:80] or str(number)
        sprops = {"number": number, "status": step.get("status", "pending")}
        for key in ("repo", "verify", "done_at"):
            if isinstance(step.get(key), str):
                sprops[key] = step[key]
        facts.append(node(stid, "PlanStep", title, [unit], props=sprops))
        facts.append(edge(pid, "has_step", stid, [unit]))
        facts.append({"t": "ident", "type": "step", "key": f"{plan_id}#{number}", "node": stid})
        for dep in step.get("depends_on") or []:
            facts.append(edge(stid, "depends_on", f"{pid}/step:{dep}", [unit]))
        if isinstance(step.get("repo"), str):
            facts.append(ref(stid, "in_repo", {"type": "repo", "value": step["repo"]}, [unit]))
        for key, text in _step_text(step):
            facts.append({"t": "text", "src": stid, "unit": unit, "text": text, "field": key})
    return facts


def _python(record: dict, source_id: str, text: str) -> list[dict]:
    item_id = record["id"]
    path = (record.get("props") or {}).get("path") or ""
    result = py_extract.extract(text, path)
    if result is None:
        return [{"t": "issue", "message": f"{path}: does not parse", "unit": item_unit(item_id)}]
    anchors = {f["anchor"] for f in record.get("fragments", [])}
    facts = []

    def unit_for(top: str) -> str:
        return frag_unit(item_id, top) if top in anchors else item_unit(item_id)

    by_qual = {}
    for sym in result["symbols"]:
        sid = f"symbol:{source_id}:{path}::{sym['qualname']}"
        by_qual[sym["qualname"]] = sid
        unit = unit_for(sym["top"])
        facts.append(
            node(
                sid,
                "Symbol",
                sym["qualname"],
                [unit],
                props={"kind": sym["kind"], "path": path, "line": sym["line"], "file": item_id},
                where=[[unit, f"line {sym['line']}"]],
            )
        )
        parent = by_qual.get(sym["parent"]) if sym["parent"] else None
        if parent:
            facts.append(edge(sid, "member_of", parent, [unit]))
        else:
            facts.append(edge(item_id, "defines", sid, [unit]))
        facts.append(
            {
                "t": "ident",
                "type": "symbol",
                "key": f"{path}::{sym['qualname']}",
                "name": sym["name"],
                "qualname": sym["qualname"],
                "source": source_id,
                "repo": (record.get("props") or {}).get("repo"),
                "node": sid,
            }
        )
    for imp in result["imports"]:
        facts.append(
            ref(
                item_id,
                "imports",
                {"type": "module", "value": imp["module"], "source": source_id, "optional": bool(imp.get("optional"))},
                [unit_for(imp["top"])],
            )
        )
    for call in result["calls"]:
        caller, callee = by_qual.get(call["caller"]), by_qual.get(call["callee"])
        if caller and callee:
            unit = unit_for(call["top"])
            facts.append(
                edge(
                    caller, "calls", callee, [unit], status="resolved", conf=0.8, where=[[unit, f"line {call['line']}"]]
                )
            )
    return facts


def code_extractor(record: dict, backend: str) -> str:
    """Which extractor a code item gets, with its version: part of the structure task's memo key."""
    from evo_agents.kg.extract import graphify as gfy

    path = (record.get("props") or {}).get("path") or ""
    if record.get("kind") != "code" or backend == "none":
        return "none"
    is_python = PurePosixPath(path).suffix == ".py"
    if backend == "python-ast" or (backend == "auto" and is_python):
        return "python-ast" if is_python else "none"
    if gfy.available() and gfy.supports(path):
        return f"graphify-ast@{gfy.version()}"
    return "none"


def _graphify(record: dict, source_id: str, text: str) -> list[dict]:
    from evo_agents.kg.extract import graphify as gfy

    item_id = record["id"]
    props = record.get("props") or {}
    path = props.get("path") or ""
    result = gfy.extract(text, path)
    unit = item_unit(item_id)
    if result is None:
        return [{"t": "issue", "message": f"{path}: graphify could not extract it", "unit": unit}]
    facts, by_qual = [], {}
    for sym in result["symbols"]:
        sid = f"symbol:{source_id}:{path}::{sym['qualname']}"
        by_qual[sym["qualname"]] = sid
        facts.append(
            node(
                sid,
                "Symbol",
                sym["qualname"],
                [unit],
                props={"kind": sym["kind"], "path": path, "line": sym["line"], "file": item_id},
                where=[[unit, f"line {sym['line']}"]],
            )
        )
        parent = by_qual.get(sym["parent"]) if sym["parent"] else None
        if parent:
            facts.append(edge(sid, "member_of", parent, [unit]))
        else:
            facts.append(edge(item_id, "defines", sid, [unit]))
        facts.append(
            {
                "t": "ident",
                "type": "symbol",
                "key": f"{path}::{sym['qualname']}",
                "name": sym["name"],
                "qualname": sym["qualname"],
                "source": source_id,
                "repo": props.get("repo"),
                "node": sid,
            }
        )
    for call in result["calls"]:
        caller, callee = by_qual.get(call["caller"]), by_qual.get(call["callee"])
        if caller and callee and caller != callee:
            facts.append(
                edge(
                    caller,
                    "calls",
                    callee,
                    [unit],
                    status=call["status"],
                    conf=1.0 if call["status"] == "parsed" else 0.8,
                    where=[[unit, f"line {call['line']}"]],
                )
            )
    for candidates in result["imports"]:
        facts.append(
            ref(item_id, "imports", {"type": "paths", "values": candidates, "repo": props.get("repo")}, [unit])
        )
    return facts


def structure_item(record: dict, source_id: str, text: str | None, code_backend: str = "auto") -> list[dict]:
    kind = record["kind"]
    item_id = record["id"]
    unit = item_unit(item_id)
    if kind in ("manifest", "contracts", "plan") and text is not None:
        data = _yaml(text)
        if not isinstance(data, dict):
            return [{"t": "issue", "message": f"{item_id}: not a YAML mapping", "unit": unit}]
        if kind == "manifest":
            return _manifest(item_id, data, unit, record)
        return {"contracts": _contracts, "plan": _plan}[kind](item_id, data, unit)
    if kind == "code" and text is not None:
        extractor = code_extractor(record, code_backend)
        if extractor == "python-ast":
            return _python(record, source_id, text)
        if extractor.startswith("graphify-ast"):
            return _graphify(record, source_id, text)
    return []
