"""The facts of the per-item stages, as the dicts the memo holds (``pipeline.stages``): the units an item owns, and
the node, edge and reference facts made of them."""

from __future__ import annotations


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
