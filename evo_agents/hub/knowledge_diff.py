"""What a change does to a project's knowledge.yaml, key by key, for the Curator's tier rules (``evo_agents.hub.tiers``)
and its Judge. Pure functions over the file's text at both ends of a pull request, read with PyYAML as the harness and
``kg`` read it (``evo_agents.harness.parse_yaml``), so the hub, the worker and the tests share them.

A change that edits nothing but SOURCE_KEYS (``ref``, ``fetch``, ``refresh``, ``include``, ``exclude``, ``allow``) of
git and harness sources the file has at both ends is a change of sources: ``KnowledgeDiff.others`` is empty, and the
tier rules give a proposal of kind ``kg_source`` tier 0 for it. Everything else is named in ``others``, one line each,
and keeps the tier the file has as code: a file added, deleted or that does not read as a mapping; a top-level key
changed (``policy`` and its sinks, ``identifiers``, ``version``, ``project``, ``ontology``, ``bindings``); a source
added, removed or moved; a source of another connector (an exec command) changed in any way; another key of a source changed
(``label``, ``connector``, ``repo``, ``path``, ``branch``, ``pulls``, ``credentials``, ``command``, ``config`` and the
rest); more than MAX_SOURCES sources edited at once.

A Judge checks such a change with ``judge_checks``: ``evo-agents harness validate`` in the worktree of the branch
(JUDGE_VALIDATE), then a trial sync of each source it edits (``trial_name``), run by the worker
(``evo_agents.worker.runner.judge_knowledge``); their results travel with the verify results of the plan, under those
names, and the hub passes the change only when each of them ran and exited 0.
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

from evo_agents.harness import parse_yaml
from evo_agents.hub.tiers import SOURCE_CONNECTORS, SOURCE_KEYS

MAX_SOURCES = 10  # sources one change of tier 0 edits, each synced on trial by its Judge
JUDGE_VALIDATE = "evo-agents harness validate"
MAX_LINE_CHARS = 300
_ABSENT = object()


@dataclass(frozen=True)
class KnowledgeDiff:
    """What a change does to knowledge.yaml: the sources whose SOURCE_KEYS it edits (in the file's order), each edit as
    ``source <id>: <key>``, and what else it changes, one line each (none for a change of sources alone)."""

    sources: tuple[str, ...] = ()
    edits: tuple[str, ...] = ()
    others: tuple[str, ...] = ()

    @property
    def sources_only(self) -> bool:
        return not self.others

    def to_json(self) -> dict:
        return {"sources": list(self.sources), "edits": list(self.edits), "others": list(self.others)}

    @classmethod
    def from_json(cls, data) -> KnowledgeDiff | None:
        if not isinstance(data, dict):
            return None
        lists = [data.get(key) for key in ("sources", "edits", "others")]
        if not all(isinstance(found, list) and all(isinstance(item, str) for item in found) for found in lists):
            return None
        return cls(*(tuple(found) for found in lists))


def _line(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_LINE_CHARS else text[: MAX_LINE_CHARS - 3] + "..."


def _read(text: str | None, side: str) -> tuple[dict | None, str | None]:
    """(the file as a mapping, None) or (None, why it is not one) of ``text``, the file at ``side``."""
    if text is None:
        return None, f"knowledge.yaml is not there {side}"
    try:
        data = parse_yaml(text)
    except yaml.YAMLError:
        return None, f"knowledge.yaml does not read as YAML {side}"
    if not isinstance(data, dict):
        return None, f"knowledge.yaml is not a mapping {side}"
    return data, None


def _top_level(before: dict, after: dict) -> list[str]:
    """What changed besides ``sources``: a key, or the keys of a mapping such as ``policy`` (its ``sinks``)."""
    found = []
    for key in sorted(set(before) | set(after)):
        if key == "sources":
            continue
        old, new = before.get(key, _ABSENT), after.get(key, _ABSENT)
        if old == new:
            continue
        if isinstance(old, dict) and isinstance(new, dict):
            inner = sorted(name for name in set(old) | set(new) if old.get(name, _ABSENT) != new.get(name, _ABSENT))
            found += [f"knowledge.yaml changes {key}.{name}" for name in inner]
        else:
            found.append(f"knowledge.yaml changes {key}")
    return found


def _sources(data: dict, side: str) -> tuple[dict[str, dict], list[str], list[str]]:
    """(each source by id, the ids in order, what is wrong with the list) of ``data`` at ``side``."""
    listed = data.get("sources")
    if not isinstance(listed, list):
        return {}, [], [f"knowledge.yaml has no list of sources {side}"]
    by_id: dict[str, dict] = {}
    order, wrong = [], []
    for item in listed:
        sid = item.get("id") if isinstance(item, dict) else None
        if not isinstance(sid, str):
            wrong.append(f"knowledge.yaml has a source without an id {side}")
        elif sid in by_id:
            wrong.append(f"knowledge.yaml names source {sid} twice {side}")
        else:
            by_id[sid] = item
            order.append(sid)
    return by_id, order, wrong


def _source(sid: str, old: dict, new: dict) -> tuple[list[str], list[str]]:
    """(the edits of SOURCE_KEYS, the other changes) of source ``sid`` from ``old`` to ``new``."""
    connector = old.get("connector")
    changed = sorted(key for key in set(old) | set(new) if old.get(key, _ABSENT) != new.get(key, _ABSENT))
    if not changed:
        return [], []
    if connector != new.get("connector"):
        return [], [f"source {sid} changes its connector"]
    if connector not in SOURCE_CONNECTORS:
        return [], [f"source {sid} is read by connector {connector}, not git or harness, and the change edits it"]
    edits = [f"source {sid}: {key}" for key in changed if key in SOURCE_KEYS]
    others = [f"source {sid} changes its {key}" for key in changed if key not in SOURCE_KEYS]
    return edits, others


def classify(before: str | None, after: str | None) -> KnowledgeDiff:
    """What the change from ``before`` to ``after`` (the file's text at the merge base and at the head; None where it
    is not there) does to knowledge.yaml; see the module's docstring."""
    old, why_old = _read(before, "before the change")
    new, why_new = _read(after, "after the change")
    if old is None or new is None:
        return KnowledgeDiff(others=tuple(why for why in (why_old, why_new) if why))
    others = _top_level(old, new)
    old_sources, old_order, wrong_old = _sources(old, "before the change")
    new_sources, new_order, wrong_new = _sources(new, "after the change")
    others += wrong_old + wrong_new
    others += [f"knowledge.yaml adds source {sid}" for sid in new_order if sid not in old_sources]
    others += [f"knowledge.yaml removes source {sid}" for sid in old_order if sid not in new_sources]
    kept_old = [sid for sid in old_order if sid in new_sources]
    if kept_old != [sid for sid in new_order if sid in old_sources]:
        others.append("knowledge.yaml moves its sources")
    sources, edits = [], []
    for sid in new_order:
        if sid not in old_sources:
            continue
        found, other = _source(sid, old_sources[sid], new_sources[sid])
        edits += found
        others += other
        if found:
            sources.append(sid)
    if len(sources) > MAX_SOURCES:
        others.append(f"the change edits {len(sources)} sources, more than the {MAX_SOURCES} a Judge syncs on trial")
    return KnowledgeDiff(tuple(sources), tuple(edits), tuple(_line(item) for item in others))


def trial_name(source: str) -> str:
    """The name the trial sync of ``source`` carries among a Judge's verify results."""
    return f"evo-agents kg sync --source {source} (a trial sync of the branch's knowledge.yaml on the worker)"


def judge_checks(sources: list[str] | tuple[str, ...]) -> list[str]:
    """The checks a Judge runs for a change of knowledge.yaml that edits ``sources``, in their order."""
    return [JUDGE_VALIDATE, *(trial_name(source) for source in sources)]
