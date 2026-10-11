"""Class hierarchy of one build: each class's bases resolved through the identifier index, the chain a
member is looked up along, and the ``inherits`` and ``overrides`` edges of one item.

The structure stage gives a class its bases as ``via`` lists, the evidence a call carries (see
``extract.xref``). ``resolve`` runs once a build, after the index has learnt every definition: a base links
when the first ``via`` entry that finds a class finds exactly one; a base it cannot find (a class of a
library, a name without evidence) drops out of the chain. A class's chain is its C3 linearization, Python's
method resolution order, which for single inheritance is the class, its parent, the parent's parent and so
on; when the bases admit no such order, the rest follows depth first. ``member`` walks the chain:
``self.m()`` reaches the nearest class that defines ``m``, ``super().m()`` the nearest after the class.

A method overrides, for each base of its class, the nearest method of the same name up that base's chain.
An edge is ``parsed`` when both ends are in one file, else ``resolved``.
"""

from __future__ import annotations

from collections.abc import Callable

from evo_agents.kg.pipeline.stages import edge


def resolve(index: dict, classes: list[dict], resolver: Callable) -> None:
    """Set ``index["bases"]`` (class -> its resolved bases, nearest first) and ``index["mro"]`` (class ->
    its chain) from the ``symbol`` idents of classes with bases. ``resolver(file, repo, path)`` gives an
    object whose ``definitions(kind, value, qual, target)`` resolves one ``via`` entry from that file."""
    contexts: dict[tuple, object] = {}
    bases: dict[str, tuple[str, ...]] = {}
    for f in sorted(classes, key=lambda f: f["node"]):
        key = (f["file"], f.get("repo"), f.get("path"))
        if key not in contexts:
            contexts[key] = resolver(*key)
        target = {"repo": f.get("repo"), "source": f.get("source")}
        found: list[str] = []
        for via in f["bases"]:
            base = _base(contexts[key], via, target, index["classes"])
            if base and base != f["node"] and base not in found:
                found.append(base)
        if found:
            bases[f["node"]] = tuple(found)
    memo: dict[str, tuple[str, ...]] = {}
    index["bases"] = bases
    index["mro"] = {node: _linearize(node, bases, memo, set()) for node in sorted(bases)}


def _base(ctx, via: list[list], target: dict, classes: dict) -> str | None:
    """The one class the first ``via`` entry that finds a class finds; None when it finds two."""
    for kind, value, qual in via:
        found = sorted({d for d in ctx.definitions(kind, value, qual, target) if d in classes})
        if found:
            return found[0] if len(found) == 1 else None
    return None


def _linearize(cls: str, bases: dict, memo: dict, active: set) -> tuple[str, ...]:
    """C3: the class, then a merge of its bases' chains that keeps each chain's order and the bases' own."""
    if cls in memo:
        return memo[cls]
    if cls in active:  # a cycle through names that resolve to each other: stop here
        return (cls,)
    active.add(cls)
    parents = bases.get(cls, ())
    seqs = [list(_linearize(b, bases, memo, active)) for b in parents] + [list(parents)]
    out = [cls]
    while any(seqs):
        head = next((s[0] for s in seqs if s and not any(s[0] in t[1:] for t in seqs)), None)
        if head is None:  # no C3 order: the rest, depth first
            out += [c for s in seqs for c in s]
            break
        out.append(head)
        seqs = [s[1:] if s and s[0] == head else s for s in seqs]
    active.discard(cls)
    memo[cls] = tuple(dict.fromkeys(out))
    return memo[cls]


def find_member(index: dict, cls: str, name: str, skip: bool = False) -> tuple[str, str] | None:
    """The method ``name`` of the nearest class on ``cls``'s chain that defines it, and that class's file;
    ``skip`` starts after ``cls`` itself, as ``super`` does."""
    chain = index["mro"].get(cls, (cls,))
    for c in chain[1:] if skip else chain:
        where = index["classes"].get(c)
        node = index["def"].get(f"{where[0]}::{where[1]}.{name}") if where else None
        if node:
            return node, where[0]
    return None


def item_facts(item_id: str, structure: list[dict], index: dict) -> list[dict]:
    """``inherits`` from each class of one item to its bases, ``overrides`` from each method of those
    classes to the method it overrides up each base's chain."""
    nodes = {f["id"]: f for f in structure if f["t"] == "node"}
    out: dict[tuple, dict] = {}
    for f in structure:
        if f["t"] != "ident" or f["type"] != "symbol" or f["node"] not in nodes:
            continue
        if f.get("kind") == "class":
            for base in index["bases"].get(f["node"], ()):
                _add(out, nodes[f["node"]], "inherits", base, index["classes"][base][0] == item_id)
        elif f.get("kind") == "method":
            owner = index["def"].get(f"{item_id}::{f['qualname'].rpartition('.')[0]}")
            for base in index["bases"].get(owner, ()):
                found = find_member(index, base, f["name"])
                if found and found[0] != f["node"]:
                    _add(out, nodes[f["node"]], "overrides", found[0], found[1] == item_id)
    return [out[k] for k in sorted(out)]


def _add(out: dict, src: dict, rel: str, dst: str, local: bool) -> None:
    key = (src["id"], rel, dst)
    if key not in out:
        out[key] = edge(
            src["id"],
            rel,
            dst,
            src["units"],
            status="parsed" if local else "resolved",
            conf=1.0 if local else 0.8,
            needs=[dst],
            where=src["where"],
        )
