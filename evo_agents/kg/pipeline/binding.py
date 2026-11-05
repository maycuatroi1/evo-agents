"""Binding stage: edges a person declared in ``bindings/*.yaml``, reviewed in git.

    version: 1
    bindings:
      - from: billing:src/billing/invoice.py::total   # any reference the index can resolve
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

from evo_agents.kg.pipeline.link_forms import QUALIFIED_SYMBOL, unique
from evo_agents.kg.pipeline.stages import edge, item_unit, node, normalize_url

BINDING_VERSION = "1"
REL = re.compile(r"^[a-z][a-z_]*$")


def resolve(value: str, index: dict, code_kinds: dict[str, str]) -> tuple[str | None, tuple[str, str, str] | None]:
    """Return (node id, ``("create", kind, code)`` of a node to create or None)."""
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
        return unique(entries), None
    if "#" in value:
        return index["step"].get(value), None
    if prefix in index["repo"] and rest:
        entries = [e for e in index["path"].get(rest, []) if e.get("repo") == prefix]
        return unique(entries), None
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
    targets = _Targets(index, path, unit)
    for i, entry in enumerate(data.get("bindings") or []):
        targets.bind(entry, i)
    return targets.facts


class _Targets:
    """The facts of one bindings file, in order: a node for each code a binding declares, an error for each ref that
    does not resolve or entry that is malformed, and the edges and ``same`` facts of the entries."""

    def __init__(self, index: dict, path: str, unit: str):
        self.index, self.path, self.unit = index, path, unit
        self.code_kinds = {i["kind"].lower(): i["kind"] for i in index.get("identifiers", [])}
        self.facts: list[dict] = []
        self.created: set[str] = set()

    def error(self, message: str) -> None:
        self.facts.append({"t": "error", "message": f"{self.path}: {message}", "unit": self.unit})

    def target(self, value, where: str) -> str | None:
        if not isinstance(value, str) or not value.strip():
            self.error(f"{where} must be a non-empty string")
            return None
        found, create = resolve(value, self.index, self.code_kinds)
        if create and found is not None and found not in self.created:
            _, kind, code = create
            self.facts.append(node(found, kind, code, [self.unit], props={"code": code}, status="declared"))
            self.created.add(found)
        if found is None:
            self.error(f"{where} {value!r} does not resolve")
        return found

    def bind(self, entry, i: int) -> None:
        """The facts of ``bindings[i]``: a ``same_as`` list, or an edge ``from`` -> ``to``."""
        where, unit = f"bindings[{i}]", self.unit
        if not isinstance(entry, dict):
            self.error(f"{where} must be a mapping")
            return
        if "same_as" in entry:
            self.same(entry["same_as"], where)
            return
        rel = entry.get("rel")
        if not isinstance(rel, str) or not REL.match(rel):
            self.error(f"{where}.rel must be a snake_case name")
            return
        src, dst = self.target(entry.get("from"), f"{where}.from"), self.target(entry.get("to"), f"{where}.to")
        if src and dst:
            props = {"binding": f"{self.path}#{i}"}
            if isinstance(entry.get("note"), str):
                props["note"] = entry["note"]
            self.facts.append(
                edge(src, rel, dst, [unit], props=props, status="declared", needs=[src, dst], where=[[unit, where]])
            )

    def same(self, refs, where: str) -> None:
        if not isinstance(refs, list) or len(refs) < 2:
            self.error(f"{where}.same_as needs two or more refs")
            return
        ids = [self.target(r, f"{where}.same_as[{j}]") for j, r in enumerate(refs)]
        if all(ids):
            for other in ids[1:]:
                self.facts.append(
                    {"t": "same", "a": ids[0], "b": other, "units": [self.unit], "where": [[self.unit, where]]}
                )
