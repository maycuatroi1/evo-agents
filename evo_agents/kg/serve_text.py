"""How the answers of ``kg serve`` name a node: its line in the text, the label a result prints once, and its JSON in
the structured result."""

from __future__ import annotations

import re
from collections import Counter

from evo_agents.kg.policy import Label
from evo_agents.kg.refs import named_by, readable
from evo_agents.kg.serve_view import ANSWERS_NOTHING, SessionView
from evo_agents.kg.text import slugify

KIND_PROPS = {"ChatMessage": ("chat", "thread")}  # the props a line of that kind names too; never a text
SPAN = re.compile(r"lines? (\d+)(?:-(\d+))?")


class SessionText(SessionView):
    """The lines and JSON of the nodes and edges a session's answers show."""

    def _line(self, n: dict, shared: str | None = None, passage: str | None = None, status: str | None = None) -> str:
        """One node as the text shows it: its readable id, its kind, its name unless the id ends with it, its status,
        its label unless it is ``shared`` (the one the result prints once), and the path when the id does not hold
        it, the status and number of a plan step, the chat and thread of a chat message (KIND_PROPS), and where in
        its file it is (``lines``): the lines of a section, the first and last line of a symbol, or ``passage``, the
        lines a search matched best (``Store.matched``), with the node's own after ``of``. Never its URI: kg_node
        gives that. With ``status``, the status the result prints once too (kg_search), the line is briefer: its
        node's status only when it is another, no name of a section its anchor spells, no number of a step its id
        holds, and no parentheses when they would hold nothing."""
        ref, props = readable(n["id"]), n["props"]
        brief = status is not None
        named = named_by(ref, n["name"]) or (
            brief and n["kind"] == "Section" and bool(n["name"]) and slugify(n["name"]) == props.get("anchor")
        )
        head = f"[{ref}] {n['kind']}" + ("" if named else f" {n['name']!r}")
        extra = [
            f"{key}={props[key]}"
            for key in ("path", "status", "number", *KIND_PROPS.get(n["kind"], ()))
            if props.get(key) not in (None, "")
            and not (key == "path" and str(props[key]) in ref)
            and not (brief and key == "number" and ref.endswith(f"step:{props[key]}"))
        ]
        where = lines(props)
        if passage:
            where = f"{passage} of {where.split(' ', 1)[1]}" if where.startswith("lines ") else passage
        if where:
            extra.append(where)
        label = self.describe(n["label"])
        if brief:
            told = [part for part, same in ((n["status"], n["status"] == status), (label, label == shared)) if not same]
            state = f" ({', '.join(told)})" if told else ""
        else:
            state = f" ({n['status']})" if label == shared else f" ({n['status']}, {label})"
        return f"{head}{state}" + (f" {' '.join(extra)}" if extra else "")

    def _labels(self, nodes) -> tuple[str | None, list[str]]:
        """The label a result prints once, under its first line: the label of most of ``nodes`` (the first seen of
        those as common). A line names its node's label only when it is another. None and no line without nodes."""
        counts = Counter(self.describe(n["label"]) for n in nodes)
        if not counts:
            return None, []
        shared = counts.most_common(1)[0][0]
        unless = "" if len(counts) == 1 else " unless its line names another"
        return shared, [f"label of every node{unless}: {shared}"]

    def _node_json(self, n: dict) -> dict:
        self._saw(n["label"])  # every node in a structured result is revealed
        assert self.policy is not None, ANSWERS_NOTHING
        label = self.policy.describe(Label.of(n["label"]))  # level, location, integrity; audience when it has one
        return {
            "id": n["id"],
            "kind": n["kind"],
            "name": n["name"],
            "status": n["status"],
            "conf": n["conf"],
            "label": label,
            "props": n["props"],
            "aliases": n["aliases"],
        }

    def _edge_line(self, e: dict) -> str:
        return f"  [{readable(e['src'])}] -{e['rel']}-> [{readable(e['dst'])}] ({e['status']})"

    @staticmethod
    def _step(parent: str, e: dict) -> str:
        arrow = f"-{e['rel']}->" if e["src"] == parent else f"<-{e['rel']}-"
        return f"{arrow} ({e['status']})"


def lines(props: dict) -> str:
    """Where a node is in its file, as its text shows it: ``lines A-B`` of a section (its ``span``) or of a symbol (its
    ``line`` and ``end_line``), ``line A`` of a symbol read before symbols had an end; "" for any other node."""
    span = props.get("span")
    if isinstance(span, str) and SPAN.fullmatch(span):
        return span
    first, last = props.get("line"), props.get("end_line")
    if not isinstance(first, int) or isinstance(first, bool) or first < 1:
        return ""
    if isinstance(last, int) and not isinstance(last, bool) and last > first:
        return f"lines {first}-{last}"
    return f"line {first}"
