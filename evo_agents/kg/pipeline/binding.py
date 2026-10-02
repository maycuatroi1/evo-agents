"""Binding stage: edges a person declared in ``bindings/*.yaml``, reviewed in git.

    version: 1
    bindings:
      - from: kb-service:src/kb/search.py::search     # any reference the index can resolve
        rel: implements
        to: usecase:KB-01
        note: optional
      - same_as: [usecase:KB-01, "https://wiki.example.test/kb-01"]

A binding that does not resolve is a build error: a declared fact that silently drops is worse than a
build that stops.
"""

from __future__ import annotations

import re

import yaml

from evo_agents.kg.pipeline.link import QUALIFIED_SYMBOL, _unique
from evo_agents.kg.pipeline.stages import edge, item_unit, node, normalize_url

BINDING_VERSION = "1"
REL = re.compile(r"^[a-z][a-z_]*$")


def resolve(value: str, index: dict, code_kinds: dict[str, str]) -> tuple[str | None, dict | None]:
    """Return (node id, node fact to create or None)."""
    value = value.strip()
    if value in index["ids"]:
        return value, None
    prefix, _, rest = value.partition(":")
    if prefix in code_kinds and rest:
        return value, ("create", code_kinds[prefix], rest)
    if value.startswith(("http://", "https://")):
        return index["url"].get(normalize_url(value)), None
    m = QUALIFIED_SYMBOL.fullmatch(value)
    if m:
        entries = index["symbol"].get(f"{m.group(2)}::{m.group(3)}", [])
        if m.group(1):
            entries = [e for e in entries if e.get("repo") == m.group(1)]
        return _unique(entries), None
    if "#" in value:
        return index["step"].get(value), None
    if prefix in index["repo"] and rest:
        entries = [e for e in index["path"].get(rest, []) if e.get("repo") == prefix]
        return _unique(entries), None
    for kind in ("repo", "seam", "plan"):
        if value in index[kind]:
            return index[kind][value], None
    return None, None


def binding_item(record: dict, text: str | None, index: dict) -> list[dict]:
    item_id = record["id"]
    unit = item_unit(item_id)
    path = (record.get("props") or {}).get("path") or item_id
    try:
        data = yaml.safe_load(text or "") or {}
    except yaml.YAMLError as exc:
        return [{"t": "error", "message": f"{path}: invalid YAML: {exc}", "unit": unit}]
    if not isinstance(data, dict):
        return [{"t": "error", "message": f"{path}: expected a mapping with 'bindings'", "unit": unit}]
    code_kinds = {i["kind"].lower(): i["kind"] for i in index.get("identifiers", [])}
    facts: list[dict] = []
    created: set[str] = set()

    def target(value, where: str):
        if not isinstance(value, str) or not value.strip():
            facts.append({"t": "error", "message": f"{path}: {where} must be a non-empty string", "unit": unit})
            return None
        found, create = resolve(value, index, code_kinds)
        if create and found not in created:
            _, kind, code = create
            facts.append(node(found, kind, code, [unit], props={"code": code}, status="declared"))
            created.add(found)
        if found is None:
            facts.append({"t": "error", "message": f"{path}: {where} {value!r} does not resolve", "unit": unit})
        return found

    for i, entry in enumerate(data.get("bindings") or []):
        where = f"bindings[{i}]"
        if not isinstance(entry, dict):
            facts.append({"t": "error", "message": f"{path}: {where} must be a mapping", "unit": unit})
            continue
        if "same_as" in entry:
            refs = entry["same_as"]
            if not isinstance(refs, list) or len(refs) < 2:
                facts.append({"t": "error", "message": f"{path}: {where}.same_as needs two or more refs", "unit": unit})
                continue
            ids = [target(r, f"{where}.same_as[{j}]") for j, r in enumerate(refs)]
            if all(ids):
                for other in ids[1:]:
                    facts.append({"t": "same", "a": ids[0], "b": other, "units": [unit], "where": [[unit, where]]})
            continue
        rel = entry.get("rel")
        if not isinstance(rel, str) or not REL.match(rel):
            facts.append({"t": "error", "message": f"{path}: {where}.rel must be a snake_case name", "unit": unit})
            continue
        src, dst = target(entry.get("from"), f"{where}.from"), target(entry.get("to"), f"{where}.to")
        if src and dst:
            props = {"binding": f"{path}#{i}"}
            if isinstance(entry.get("note"), str):
                props["note"] = entry["note"]
            facts.append(
                edge(src, rel, dst, [unit], props=props, status="declared", needs=[src, dst], where=[[unit, where]])
            )
    return facts
