"""The build step that reads prose with a model and states the entities and relations in it (definition a14).

It is optional and off. A project turns it on with a strategy whose ``steps`` name ``entities`` and whose settings
carry an ``entities`` mapping (``evo_agents.kg.strategy``); a project without that strategy never calls a model, whatever
its settings hold (a11).

What it does for each item of the kinds the mapping chooses (the prose kinds when it names none):

- the item is sent to the model only when the model is local (a command on this machine) or reached through a sink the
  project declared in its policy as ``llm-backend`` whose clearance covers the item's label; otherwise the item is left
  out and ``coverage["entities"]["refused"]`` says why (a17);
- the model's answer is cut down to the node kinds and relations the mapping lists and the ontology holds; what it
  invented outside them is counted, not stored. Each node and edge gets the status ``extracted`` (never ``parsed``) and
  carries the model's name as evidence, and an edge ``mentions`` joins the document to each entity;
- the answers of all items are read together (``evo_agents.kg.entity_links``): names of one entity are one node, an
  entity that names exactly one Symbol or File gets an edge ``refers_to`` to it, and, when the graph has vectors, two
  near entities of one kind an edge ``synonym_of``; coverage counts the names ``merged``, the ``synonyms``, the
  ``code_links`` and the names ``code_ambiguous``;
- the answer is memoized by the item's content digest, the model and the mapping, so an unchanged item costs nothing;
- calls, tokens and cost are counted per build, and the step stops at the ceiling (``max_calls``, ``max_tokens``,
  ``max_cost``), leaves the rest to a later build (``coverage["entities"]["skipped"]``) and the next build continues
  from the memo. With ``jobs`` above 1, that many calls run at once (``_prefetch``): each answer is memoized and
  committed as it comes, so a build that is killed keeps what it was told, and the ceiling may be passed by the calls
  already running when the token or cost ceiling is reached (at most ``jobs - 1``).

The mapping's ``style`` says what the model is asked (``prompt``). ``closed``, the default, asks for the node kinds and
relations of the mapping, each node with a conf. ``lightrag`` asks as LightRAG does: each entity with its name, a type
taken from ``node_kinds`` as a suggestion (one outside them is kept as ``Other``, its own word in ``props.type``) and a
description; each relation between two of them with keywords, a description and a strength from 1 to 10, stored as the
first relation of the mapping. Each node and edge keeps its item and the model's short quote; in the lightrag style
also, when the quote is found in the text sent, the ``line`` of the item it starts on.

A model is one of: ``command``, an argv run on this machine that reads one JSON object on stdin
(``{"text", "node_kinds", "relations", "model"}``, and ``"style"`` when it is not ``closed``) and writes one on stdout
(``{"nodes": [{"kind", "name", "evidence", "conf"}], "edges": [{"src", "rel", "dst", "evidence"}], "usage":
{"input_tokens", "output_tokens", "cost"}}``); or ``url``, an OpenAI-compatible endpoint, reached with the ``openai``
library of the optional extra ``entities`` (``key_env`` names the variable that holds the key, never the key). The
key is read from ``keys``, a mapping of variable to value the caller holds in memory (the hub's build opens it from a
hub secret, ``evo_agents.hub.server.kg_entity_key``), else from the environment; a remote url whose ``key_env`` has no
value stops the step before any call (the rest waits for a later build), and an error of the endpoint is kept with the
key cut out, so the key reaches no coverage, log or prompt.
A command that exits STOP_EXIT (a login refused, a limit reached) stops the step at that item: the rest is skipped and
waits for a later build. Verification of a build never calls a model: it reads the memo only.

The command ``evo-agents kg claude-entities`` (``evo_agents.kg.claude_code``) asks Claude Code with the subscription
login. It sends the text off the machine, so its mapping must name an ``llm-backend`` sink, and it is not local. On a
machine without ``claude`` (the hub's build container) the step calls nothing: each item its memo lacks is deferred,
counted in ``coverage["entities"]["deferred"]`` and, when the build gives an ``outbox``, put there as a request
``{"key", "item", "text", "node_kinds", "relations", "model"}``; a build given an ``inbox`` (key: the answer of a run
that had ``claude``) takes the answer from it instead of calling, and memoizes it as its own.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections import defaultdict
from urllib.parse import urlparse

from evo_agents.kg import claude_code
from evo_agents.kg import entity_links as links
from evo_agents.kg import entity_merge as merge
from evo_agents.kg.entity_mapping import (
    DEFAULT_MAX_CHARS,
    LOCAL_HOSTS,
    OTHER,
    EntitiesError,
    Spend,
    check,
    enabled,
    estimate,
    parse_answer,
    prompt,
)
from evo_agents.kg.memo import memo_key
from evo_agents.kg.protocol import canonical_json, sha256

VERSION = "1"
STATUS = "extracted"
DEFAULT_CONF = 0.6
COMMAND_TIMEOUT = 300
REFUSED_SAMPLES = 20
SLUG = re.compile(r"[^a-z0-9]+")
MAX_ALIASES = 10
TYPE_WORDS = {"person": "Actor", "people": "Actor", "location": "Place"}  # LightRAG's words for kinds named otherwise
MAX_DESCRIPTION = 600
NODE_EXTRAS = ("description", "type", "line")  # what a lightrag node keeps in its props besides the quote
EDGE_EXTRAS = ("description", "keywords", "strength", "line")
STOP_EXIT = claude_code.STOP_EXIT  # a command's exit status that stops the step at the item


class BudgetStop(Exception):
    """Raised inside a memoized task before the call that would pass the ceiling: nothing is cached for it."""


class ModelStop(EntitiesError):
    """The model command exited STOP_EXIT: its login was refused or a limit was reached. No later item is sent in this
    build."""


def slug(name: str) -> str:
    """The key ``entity_links`` folds a name to: accents and đ folded, so "Lê Văn Tâm" and "Lý Văn Tâm" differ."""
    return links.key(name)


def _old_slug(name: str) -> str:
    """The slug edges of answers memoized before ``slug`` folded accents were written with."""
    return SLUG.sub("-", name.lower()).strip("-")[:80]


def entity_id(kind: str, name: str) -> str:
    return f"entity:{kind}:{slug(name)}"


def style_of(config: dict) -> str:
    return config.get("style") or "closed"


def config_digest(config: dict, node_kinds: list[str], relations: list[str]) -> str:
    keep = {"model": config["model"], "node_kinds": node_kinds, "relations": relations}
    keep["max_chars"] = config.get("max_chars", DEFAULT_MAX_CHARS)
    if style_of(config) != "closed":  # a closed mapping keeps the digest, and the memo, it had before styles
        keep["style"] = style_of(config)
    if config.get("reasoning"):
        keep["reasoning"] = config["reasoning"]
    return sha256(canonical_json({**keep, "version": VERSION}))[:16]


# -- the models ---------------------------------------------------------------------------------------------------


def call_command(config: dict, text: str, node_kinds: list[str], relations: list[str]) -> dict:
    request = {"text": text, "node_kinds": node_kinds, "relations": relations, "model": config["model"]}
    if style_of(config) != "closed":
        request["style"] = style_of(config)
    return _run_command(config, request)


def _run_command(config: dict, request: dict) -> dict:
    try:
        done = subprocess.run(
            config["command"],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EntitiesError(f"the model command failed: {type(exc).__name__}: {exc}") from None
    if done.returncode == STOP_EXIT:
        try:
            said = json.loads(done.stdout or "{}")
        except ValueError:
            said = {}
        why = said.get("error") if isinstance(said, dict) else None
        raise ModelStop(str(why or done.stderr.strip() or "the model command asked to stop")[:200])
    if done.returncode != 0:
        raise EntitiesError(f"the model command exited {done.returncode}: {done.stderr.strip()[:200]}")
    return parse_answer(done.stdout)


def key_of(config: dict, keys: dict | None = None) -> str | None:
    """The key of a url model: ``keys[key_env]``, else the variable ``key_env`` of the environment; None without one."""
    name = config.get("key_env")
    if not name:
        return None
    found = (keys or {}).get(name) or os.environ.get(name) or ""
    return found.strip() or None


def redact(text: str, key: str | None) -> str:
    """``text`` with ``key`` cut out, for an error that may quote what was sent."""
    return text.replace(key, "[key]") if key else text


def _openai_client(config: dict, key: str | None = None):
    try:
        import openai
    except ImportError:
        raise EntitiesError("the url model needs the openai library: pip install 'evo-agents[entities]'") from None
    return openai.OpenAI(base_url=config["url"], api_key=key or "none")


CLIENT_FACTORY = _openai_client  # tests replace it


def call_endpoint(config: dict, text: str, node_kinds: list[str], relations: list[str], keys=None) -> dict:
    return _complete(config, prompt(text, node_kinds, relations, style_of(config)), keys)


def _complete(config: dict, asked: str, keys=None) -> dict:
    key = key_of(config, keys)
    client = CLIENT_FACTORY(config, key)
    try:
        got = client.chat.completions.create(
            model=config["model"],
            messages=[{"role": "user", "content": asked}],
            temperature=0,
            **({"extra_body": {"reasoning": config["reasoning"]}} if config.get("reasoning") else {}),
        )
    except Exception as exc:  # the library's errors differ by version: a failed call is a failed call
        raise EntitiesError(redact(f"the model endpoint failed: {type(exc).__name__}: {exc}", key)) from None
    usage = getattr(got, "usage", None)
    used = None
    if usage is not None:
        used = {
            "input_tokens": getattr(usage, "prompt_tokens", 0),
            "output_tokens": getattr(usage, "completion_tokens", 0),
        }
    try:
        data = parse_answer(got.choices[0].message.content or "")
    except EntitiesError as exc:
        raise EntitiesError(str(exc), used) from None
    if used is not None:
        data["usage"] = used
    return data


def call_model(config: dict, text: str, node_kinds: list[str], relations: list[str], keys=None) -> dict:
    if "command" in config:
        return call_command(config, text, node_kinds, relations)
    return call_endpoint(config, text, node_kinds, relations, keys)


def call_task(config: dict, task: str, asked: str, keys=None) -> dict:
    """The answer of the model to a ``task`` of ``entity_merge`` (merge or summary), ``asked`` its whole prompt. A
    command gets ``{"task", "text": asked, "node_kinds": [], "relations": [], "model"}`` and answers with the object the
    prompt asks for and its usage."""
    if "command" in config:
        request = {"task": task, "text": asked, "node_kinds": [], "relations": [], "model": config["model"]}
        return _run_command(config, request)
    return _complete(config, asked, keys)


# -- the step -----------------------------------------------------------------------------------------------------


def _kind_of(said, node_kinds: set[str]) -> tuple[str | None, str | None]:
    """The node kind of a lightrag entity's type, with the type when it is not that kind's name once folded: a kind
    matched by its folded name or by TYPE_WORDS, else OTHER (None when the mapping has no OTHER)."""
    word = said.strip()[:80] if isinstance(said, str) else ""
    folded = links.key(word).replace("-", "")
    for k in sorted(node_kinds):
        if links.key(k).replace("-", "") == folded:
            return k, None
    k = TYPE_WORDS.get(folded)
    if k in node_kinds:
        return k, word
    return (OTHER, word or None) if OTHER in node_kinds else (None, None)


def _line(text: str | None, quote: str) -> int | None:
    """The line of ``text`` (from 1) the model's ``quote`` starts on; None when the text does not hold it."""
    words = quote.split()
    if not text or not words:
        return None
    found = re.search(r"\s+".join(map(re.escape, words[:8])), text)
    return text.count("\n", 0, found.start()) + 1 if found else None


def _extras(said: dict, keys: tuple[str, ...]) -> dict:
    """The description, keywords and strength of a lightrag node or edge, those it gave in a readable form."""
    out = {}
    if "description" in keys and isinstance(said.get("description"), str) and said["description"].strip():
        out["description"] = said["description"].strip()[:MAX_DESCRIPTION]
    if "keywords" in keys:
        words = said.get("keywords")
        words = ", ".join(w for w in words if isinstance(w, str)) if isinstance(words, list) else words
        if isinstance(words, str) and words.strip():
            out["keywords"] = words.strip()[:200]
    if "strength" in keys:
        try:
            out["strength"] = min(max(float(said.get("strength")), 0.0), 10.0)
        except (TypeError, ValueError):
            out["strength"] = 1.0
    return out


def _clean(
    answer: dict, node_kinds: set[str], relations: set[str], style: str = "closed", text: str | None = None
) -> dict:
    """What of a model's answer is within the kinds and relations allowed: nodes and edges, with the counts dropped.
    Names one item gives twice, folded as ``entity_links.key`` folds them, are one node; an edge may name a node by
    one of its aliases. In the lightrag style a node's kind comes from its type (``_kind_of``), every edge is the
    first of ``relations`` in order, and ``text``, the text sent, gives each quote its line."""
    lightrag = style == "lightrag"
    nodes, seen, dropped = [], {}, 0
    for n in answer.get("nodes") or []:
        name = n.get("name") if isinstance(n, dict) else None
        kind, said_type = (None, None)
        if lightrag and isinstance(n, dict):
            kind, said_type = _kind_of(n.get("type") or n.get("kind"), node_kinds)
        elif isinstance(n, dict):
            kind = n.get("kind")
        if not isinstance(name, str) or not name.strip() or kind not in node_kinds or not links.key(name):
            dropped += 1
            continue
        name = name.strip()[:200]
        key = (kind, links.key(name))
        if key in seen:
            continue
        aliases = _aliases(n, key[1])
        seen[key] = {"kind": kind, "name": name, "conf": _conf(n), "evidence": str(n.get("evidence") or "")[:200]}
        if aliases:
            seen[key]["aliases"] = aliases
        if lightrag:
            seen[key].update(_extras(n, ("description",)), **({"type": said_type} if said_type else {}))
            _add_line(seen[key], text)
        nodes.append(seen[key])
    by_name = {}
    for n in nodes:
        for name in (n["name"], *n.get("aliases", ())):
            by_name.setdefault(links.key(name), n)
    edges = []
    stored = relations[0] if lightrag and isinstance(relations, list) and relations else None
    for e in answer.get("edges") or []:
        if lightrag and isinstance(e, dict) and stored:
            e = {**e, "rel": stored}
        if not isinstance(e, dict) or e.get("rel") not in relations:
            dropped += 1
            continue
        src, dst = by_name.get(links.key(str(e.get("src") or ""))), by_name.get(links.key(str(e.get("dst") or "")))
        if src is None or dst is None or src is dst:
            dropped += 1
            continue
        stated = {
            "src": entity_id(src["kind"], src["name"]),
            "rel": e["rel"],
            "dst": entity_id(dst["kind"], dst["name"]),
            "evidence": str(e.get("evidence") or "")[:200],
        }
        if lightrag:
            stated.update(_extras(e, ("description", "keywords", "strength")))
            _add_line(stated, text)
        edges.append(stated)
    return {"nodes": nodes, "edges": edges, "dropped": dropped}


def _conf(said: dict) -> float:
    """The confidence a node gives, between 0 and 1; DEFAULT_CONF when it gives none that reads."""
    try:
        return min(max(float(said.get("conf", DEFAULT_CONF)), 0.0), 1.0)
    except (TypeError, ValueError):
        return DEFAULT_CONF


def _aliases(said: dict, key: str) -> list[str]:
    """The other names a node gives, those that fold to a key other than its own ``key``, at most MAX_ALIASES."""
    raw = said.get("aliases") if isinstance(said.get("aliases"), list) else []
    return sorted({a.strip()[:200] for a in raw if isinstance(a, str) and links.key(a) and links.key(a) != key})[
        :MAX_ALIASES
    ]


def _add_line(stated: dict, text: str | None) -> None:
    line = _line(text, stated["evidence"])
    if line is not None:
        stated["line"] = line


def run(
    config,
    project,
    records,
    body,
    memo,
    unit_labels,
    first,
    kinds,
    relations_known,
    *,
    offline=False,
    semantic_settings=None,
    outbox=None,
    inbox=None,
    keys=None,
):
    """Run the step: ``(facts per item id, coverage)``. ``config`` is the mapping of ``enabled``; ``body(rec)`` the text
    of an item; ``first`` the facts the item's map stage gave (the document node an edge starts from, the code an
    entity may name); ``kinds`` and ``relations_known`` what the ontology holds (None: everything). ``offline`` reads
    the memo and calls nothing. ``semantic_settings`` are the project's (``kg.semantic``): with vectors, near entities
    get synonym edges (``entity_links``). ``outbox``, a list, gets the requests of the items deferred on a machine
    without ``claude``; ``inbox`` maps a request's key to the answer a run that had ``claude`` gave. ``keys`` maps a
    variable named by ``key_env`` to its value, held in memory only (``key_of``)."""
    from evo_agents.kg.pipeline.stages import item_unit

    out: dict[str, list[dict]] = {}
    cov = _coverage(config, offline)
    allowed_kinds = [k for k in config["node_kinds"] if kinds is None or k in kinds]
    allowed_rels = [r for r in config.get("relations") or [] if relations_known is None or r in relations_known]
    cov["rejected_config"] = sorted(
        (set(config["node_kinds"]) - set(allowed_kinds)) | (set(config.get("relations") or []) - set(allowed_rels))
    )
    if not allowed_kinds:
        cov["stopped"] = "no node kind of entities.node_kinds is in the ontology"
        return out, cov
    step = _Step(config, memo, cov, allowed_kinds, allowed_rels, offline=offline, outbox=outbox, inbox=inbox or {})
    step.keys, step.policy = keys, project.policy
    if "url" in config and not cov["local"] and config.get("key_env") and key_of(config, keys) is None:
        step.halted.append(f"no key: {config['key_env']} is not set on this machine nor given by a hub secret")
    digest = config_digest(config, allowed_kinds, allowed_rels)
    todo = _todo(config, project.policy, records, body, unit_labels, cov, digest)
    jobs = int(config.get("jobs", 1))
    ready: dict[str, dict | EntitiesError] = {}
    if jobs > 1 and not offline and not step.defer and not step.halted:
        ready = _prefetch(
            config, todo, memo, step.inbox, step.spend, step.halted, allowed_kinds, allowed_rels, jobs, keys
        )
    answers: list[tuple] = []  # item, unit, has a document node, the answer, how public, its label
    for rec, label, text, inputs, key in todo:
        cleaned = step.answer(rec.item_id, text, inputs, key, ready)
        if cleaned is None:
            continue
        has_doc = any(f["t"] == "node" and f["id"] == rec.item_id for f in first.get(rec.item_id, []))
        answers.append((rec.item_id, item_unit(rec.item_id), has_doc, cleaned, _rank(label), label))
        cov["nodes"] += len(cleaned["nodes"])
        cov["edges"] += len(cleaned["edges"])
        cov["rejected"] += cleaned["dropped"]
    _assemble(config, answers, first, out, cov, project, memo, semantic_settings, offline, step, (records, body))
    spend = step.spend
    cov.update(calls=spend.calls, tokens_in=spend.tokens_in, tokens_out=spend.tokens_out, cost=round(spend.cost, 6))
    return out, cov


def _coverage(config: dict, offline: bool) -> dict:
    """The coverage of a step that has done nothing yet."""
    return {
        "enabled": True,
        "model": config["model"],
        "sink": config.get("sink"),
        "local": ("command" in config and not claude_code.uses(config["command"]))
        or urlparse(config.get("url") or "").hostname in LOCAL_HOSTS,
        "calls": 0,
        "cache_hits": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "cost": 0.0,
        "stopped": None,
        "skipped": 0,
        "failed": 0,
        "refused": [],
        "refused_count": 0,
        "nodes": 0,
        "edges": 0,
        "rejected": 0,
        "offline": offline,
        "deferred": 0,
        "answered_elsewhere": 0,
    }


def sink_problem(policy, sink: str | None) -> str | None:
    """Why the mapping's sink cannot take any item: not declared, or not an llm-backend."""
    if sink is None:
        return None
    if sink not in policy.sinks:
        return f"sink {sink!r} is not declared in knowledge.yaml policy.sinks"
    if policy.sinks[sink].kind != "llm-backend":
        return f"sink {sink!r} is of kind {policy.sinks[sink].kind!r}, not llm-backend"
    return None


def _todo(config: dict, policy, records, body, unit_labels, cov: dict, digest: str) -> list[tuple]:
    """(rec, label, text, inputs, key) of every item that is sent or read from the memo: of the kinds the mapping
    chooses, with text, and of a label the sink may take (the others counted in ``cov["refused"]``)."""
    from evo_agents.kg.pipeline.stages import item_unit
    from evo_agents.kg.profile import class_of

    sink = config.get("sink")
    refusal = sink_problem(policy, sink)
    wanted = set(config.get("items") or [])
    max_chars = config.get("max_chars", DEFAULT_MAX_CHARS)
    todo = []
    for rec in records:
        r = rec.record
        if (r["kind"] not in wanted) if wanted else (class_of(r["kind"]) != "prose"):
            continue
        label = unit_labels[item_unit(rec.item_id)]
        if refusal or (sink is not None and not policy.allows(label, sink)):
            cov["refused_count"] += 1
            if len(cov["refused"]) < REFUSED_SAMPLES:
                why = refusal or (
                    f"the label of the item (level {policy.level_name(label.level)}, location "
                    f"{policy.location_name(label.location)}) is above the clearance of sink {sink!r}"
                )
                cov["refused"].append({"item": rec.item_id, "reason": why})
            continue
        text = body(rec)
        if not text or not text.strip():
            continue
        text = text[:max_chars]
        inputs = {
            "item": rec.item_id,
            "hash": r["hash"],
            "text": hashlib.sha256(text.encode()).hexdigest(),
            "config": digest,
        }
        todo.append((rec, label, text, inputs, memo_key("entities", VERSION, inputs)))
    return todo


class _Step:
    """The answers of one build's step, item by item: from the memo, the inbox or a call, within the ceiling."""

    def __init__(self, config, memo, cov: dict, kinds: list[str], rels: list[str], *, offline, outbox, inbox: dict):
        self.config, self.memo, self.cov, self.kinds, self.rels = config, memo, cov, kinds, rels
        self.offline, self.outbox, self.inbox = offline, outbox, inbox
        self.spend = Spend(config)
        self.defer = "command" in config and claude_code.uses(config["command"]) and not claude_code.available()
        self.halted: list[str] = []  # why a command asked to stop: no item after it is sent
        self.keys: dict | None = None  # the keys of a url model, held in memory (``key_of``)
        self.policy = None  # the project's policy: the sink of a task of entity_merge (``allowed``)

    def answer(self, item: str, text: str, inputs: dict, key: str, ready: dict) -> dict | None:
        """The cleaned answer of an item, or None when it is skipped or its call failed (counted in coverage)."""
        memo, cov = self.memo, self.cov
        before = memo.misses["entities"]
        fresh = key in ready  # answered (and memoized) by _prefetch: a call of this build, not a cache hit
        try:
            if fresh:
                found = ready[key]
                if isinstance(found, EntitiesError):
                    raise found
                cleaned = found
            else:
                cleaned, _ = memo.run("entities", VERSION, inputs, lambda: self.ask(text, key, item), commit=True)
        except BudgetStop as stop:
            memo.misses["entities"] = before
            self.skipped(str(stop))
            return None
        except EntitiesError as exc:
            memo.misses["entities"] = before
            cov["failed"] += 1
            cov.setdefault("errors", [])
            if len(cov["errors"]) < REFUSED_SAMPLES:
                cov["errors"].append({"item": item, "error": str(exc)[:200]})
            return None
        if memo.misses["entities"] == before and not fresh:
            cov["cache_hits"] += 1
        return cleaned

    def skipped(self, why: str) -> None:
        cov = self.cov
        cov["skipped"] += 1
        if why == "halted":
            cov["stopped"] = f"the model stopped the step ({self.halted[0]}); the rest waits for a later build"
        elif why == "deferred":
            cov["stopped"] = (
                f"claude is not installed on this machine: {cov['deferred']} item(s) wait for a run that has it"
            )
        elif why != "offline":
            cov["stopped"] = f"the ceiling of this build was reached ({why}); the rest waits for a later build"

    def ask(self, text: str, key: str, item: str) -> dict:
        """The memo's task for an item it lacks: the inbox's answer, else a call to the model. BudgetStop when offline,
        halted, deferred or at the ceiling; a call that fails counts toward the ceiling all the same."""
        if self.offline:
            raise BudgetStop("offline")
        given = self.inbox.get(key)
        if isinstance(given, dict):
            self.cov["answered_elsewhere"] += 1
            cleaned = _clean(given, set(self.kinds), self.rels, style_of(self.config), text)
            cleaned["usage"] = given.get("usage") if isinstance(given.get("usage"), dict) else {}
            return cleaned
        if self.halted:
            raise BudgetStop("halted")
        if self.defer:
            self.cov["deferred"] += 1
            if self.outbox is not None:
                request = {"text": text, "node_kinds": self.kinds, "relations": self.rels}
                if style_of(self.config) != "closed":
                    request["style"] = style_of(self.config)
                self.outbox.append({"key": key, "item": item, **request, "model": self.config["model"]})
            raise BudgetStop("deferred")
        why = self.spend.exhausted()
        if why:
            raise BudgetStop(why)
        try:
            answer = call_model(self.config, text, self.kinds, self.rels, self.keys)
        except ModelStop as stop:
            self.halted.append(str(stop))
            raise BudgetStop("halted") from None
        except EntitiesError as exc:  # a failed call, a timeout or JSON that does not read was paid all the same
            used = exc.usage if isinstance(exc.usage, dict) else None
            self.spend.add(used or estimate(text, self.kinds, self.rels, style_of(self.config)))
            raise
        given = answer.get("usage")
        usage = given if isinstance(given, dict) else {}
        self.spend.add(usage)
        cleaned = _clean(answer, set(self.kinds), self.rels, style_of(self.config), text)
        cleaned["usage"] = usage
        return cleaned

    # -- the tasks of entity_merge: one call for many items, within the same ceiling and sink -----------------------

    def allowed(self, label) -> bool:
        """Whether text of an item of ``label`` may go to the model: a local one, or a sink that clears the label."""
        sink = self.config.get("sink")
        if sink is None:
            return True
        policy = self.policy
        return policy is not None and sink_problem(policy, sink) is None and policy.allows(label, sink)

    def tally(self, what: str, key: str, n: int = 1) -> None:
        self.cov.setdefault(what, {})
        self.cov[what][key] = self.cov[what].get(key, 0) + n

    def task(self, task: str, asked: str, key: str) -> dict | None:
        """The model's answer to a task (``call_task``): the inbox's, else a call; None when offline, halted, deferred
        (put in the outbox), at the ceiling or failed. The caller memoizes what it reads of it."""
        if self.offline:
            return None
        given = self.inbox.get(key)
        if isinstance(given, dict):
            self.cov["answered_elsewhere"] += 1
            return given
        if self.halted:
            return None
        if self.defer:
            if self.outbox is not None:
                request = {"key": key, "item": "", "task": task, "text": asked, "node_kinds": [], "relations": []}
                self.outbox.append({**request, "model": self.config["model"]})
            return None
        why = self.spend.exhausted()
        if why:
            self.cov["stopped"] = f"the ceiling of this build was reached ({why}); the rest waits for a later build"
            return None
        try:
            answer = call_task(self.config, task, asked, self.keys)
        except ModelStop as stop:
            self.halted.append(str(stop))
            self.cov["stopped"] = f"the model stopped the step ({stop}); the rest waits for a later build"
            return None
        except EntitiesError as exc:
            used = exc.usage if isinstance(exc.usage, dict) else None
            self.spend.add(used or {"input_tokens": -(-len(asked) // 4), "output_tokens": 0})
            errors = self.cov.setdefault("errors", [])
            if len(errors) < REFUSED_SAMPLES:
                errors.append({"item": f"({task})", "error": str(exc)[:200]})
            return None
        given = answer.get("usage")
        self.spend.add(given if isinstance(given, dict) else {})
        return answer

    def merges(self, cands: list[tuple[str, str, str]], seen: dict) -> list[tuple[str, str, str, dict]]:
        """(a, b, why, {"same", "reason"}) of the candidate pairs the memo or the model decided, MERGE_BATCH a call;
        the others are counted ``pending`` (or ``refused`` by the sink) and wait for a later build."""
        model, out, todo = self.config["model"], [], []
        for a, b, why in cands:
            inputs = merge.pair_inputs(seen[a], seen[b], model)
            held = _recall(self.memo, merge.MERGE_STAGE, merge.MERGE_VERSION, inputs)
            if held is not None:
                out.append((a, b, why, held))
            elif not (self.allowed(seen[a].label) and self.allowed(seen[b].label)):
                self.tally("merge", "refused")
            else:
                todo.append((a, b, why, inputs))
        for start in range(0, len(todo), merge.MERGE_BATCH):
            chunk = todo[start : start + merge.MERGE_BATCH]
            key = memo_key(
                merge.MERGE_STAGE, merge.MERGE_VERSION, {"batch": sha256(canonical_json([c[3] for c in chunk]))}
            )
            got = self.task("merge", merge.merge_prompt([(seen[a], seen[b]) for a, b, _, _ in chunk]), key)
            found = merge.read_decisions(got, len(chunk)) if got else {}
            for i, (a, b, why, inputs) in enumerate(chunk, 1):
                if i not in found:
                    self.tally("merge", "pending")
                    continue
                self.memo.run(merge.MERGE_STAGE, merge.MERGE_VERSION, inputs, lambda d=found[i]: d)
                self.tally("merge", "asked")
                out.append((a, b, why, found[i]))
            self.memo.commit()
        return out

    def summary(self, subject: str, fragments: list[str], label) -> str | None:
        """The model's summary of ``fragments``, descriptions of ``subject`` by items of ``label``: from the memo, else
        a call; None when it waits for a later build or the sink refuses the label."""
        inputs = merge.summary_inputs(subject, fragments, self.config["model"])
        held = _recall(self.memo, merge.SUMMARY_STAGE, merge.SUMMARY_VERSION, inputs)
        if held is not None:
            return held.get("summary")
        if not self.allowed(label):
            self.tally("summaries", "refused")
            return None
        key = memo_key(merge.SUMMARY_STAGE, merge.SUMMARY_VERSION, inputs)
        got = self.task("summary", merge.summary_prompt(subject, fragments), key)
        text = merge.read_summary(got) if got else None
        if text is None:
            self.tally("summaries", "pending")
            return None
        self.memo.run(merge.SUMMARY_STAGE, merge.SUMMARY_VERSION, inputs, lambda: {"summary": text}, commit=True)
        self.tally("summaries", "made")
        return text


class _Miss(Exception):
    pass


def _recall(memo, stage: str, version: str, inputs: dict):
    """What the memo holds for a task, or None; a miss is not counted."""
    before = memo.misses[stage]

    def missing():
        raise _Miss

    try:
        found, _ = memo.run(stage, version, inputs, missing)
    except _Miss:
        memo.misses[stage] = before
        return None
    return found


def _prefetch(config, todo, memo, inbox, spend, halted, kinds, rels, jobs, keys=None) -> dict:
    """Ask the model ``jobs`` items at a time for the items of ``todo`` the memo and the inbox lack, in their order;
    each answer is memoized and committed as it comes. Returns the answer, or the EntitiesError, by memo key. It stops
    sending at the ceiling of ``spend`` and at the first ModelStop (appended to ``halted``); the items it did not send
    are left to the step's loop, which skips them the same way."""
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    have = {row[0] for row in memo.db.execute("SELECT key FROM memo WHERE stage = 'entities'")}
    pending = iter([t for t in todo if t[4] not in have and not isinstance(inbox.get(t[4]), dict)])
    out: dict = {}
    running: dict = {}

    def take(future) -> None:
        _rec, _label, text, inputs, key = running.pop(future)
        try:
            answer = future.result()
        except ModelStop as stop:
            halted.append(str(stop))
            return
        except EntitiesError as exc:
            out[key] = exc
            return
        usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
        spend.add(usage)
        cleaned = _clean(answer, set(kinds), rels, style_of(config), text)
        cleaned["usage"] = usage
        memo.run("entities", VERSION, inputs, lambda: cleaned)
        memo.commit()
        out[key] = cleaned

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        while True:
            while len(running) < jobs and not halted and not spend.exhausted():
                if spend.calls + len(running) >= spend.max_calls:  # the calls running count against max_calls
                    break
                item = next(pending, None)
                if item is None:
                    break
                running[pool.submit(call_model, config, item[2], kinds, rels, keys)] = item
            if not running:
                break
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                take(future)
    return out


def _rank(label) -> tuple:
    """How public a label is, lower more: what picks the name of a node several items state (``entity_links``)."""
    return (label.level, label.location, len(label.audience))


def _assemble(config, answers, first, out, cov, project, memo, semantic_settings, offline, step=None, source=None):
    """The facts of every item's answer, its entities folded into one node each, joined to code and to synonyms; with
    ``step`` and the mapping's ``merge`` on, also merged by the model, the relations and descriptions of each label
    read together (``entity_merge``). ``source`` is (records, body), where the decisions of the build are read."""
    from evo_agents.kg.pipeline.stages import edge, node

    stated = [
        links.Stated(n["kind"], n["name"], tuple(n.get("aliases") or ()), n["conf"], a[0], a[4])
        for a in answers
        for n in a[3]["nodes"]
    ]
    groups = links.group(stated)
    threshold = config.get("synonym_threshold", links.DEFAULT_SYNONYM_THRESHOLD)
    cache = project.root / links.VECTOR_CACHE if getattr(project, "root", None) is not None else None
    pairs, status = links.synonyms(groups, semantic_settings, memo, cache, threshold, offline=offline)
    merging = step is not None and merge.merge_on(config)
    made: list[tuple[str, dict, list[str]]] = []  # the MERGE_REL edges: (item, the edge's ends and props, units)
    if merging:
        before = groups
        groups, made = _merge(config, answers, stated, groups, pairs, step, source)
        pairs = _remap_pairs(pairs, before, groups)
    index, code_names = links.code_index(first)
    code: dict[str, tuple[str, float, str]] = {}  # entity id -> (code id, conf, the name that found it)
    ambiguous = 0
    for nid in sorted(groups.name):
        target, name, unclear = links.code_link(groups.names[nid], index)
        ambiguous += unclear
        if target is not None:
            exact = name.strip().strip("`").removesuffix("()") in (code_names.get(target), target)
            code[nid] = (target, round(groups.conf[nid] * (1.0 if exact else links.CASE_CONF), 4), name)
    near: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    for a, b, cos in pairs:
        near[a].append((a, b, cos))
        near[b].append((a, b, cos))
    said = [_said(config, a, groups) for a in answers]
    shared = _shared(said, groups, step) if merging else {}
    for item, unit, has_doc, label, nodes, edges in said:
        facts: list[dict] = []
        for nid, props, conf in nodes:
            kept = {**props, **shared.get((nid, label), {})}
            facts.append(node(nid, groups.kind[nid], groups.name[nid], [unit], props=kept, status=STATUS, conf=conf))
            if has_doc:
                facts.append(edge(item, "mentions", nid, [unit], props=kept, status=STATUS, conf=conf))
        for (src, rel, dst), props in edges:
            kept = {**props, **shared.get(((src, rel, dst), label), {})}
            facts.append(edge(src, rel, dst, [unit], props=kept, status=STATUS, conf=DEFAULT_CONF))
        evidence = {"model": config["model"], "item": item}
        for nid in dict.fromkeys(n[0] for n in nodes):
            if nid in code:
                target, conf, name = code[nid]
                props = {**evidence, "name": name}
                facts.append(edge(nid, links.CODE_REL, target, [unit], props=props, status=STATUS, conf=conf))
            for x, y, cos in near.get(nid, ()):
                props = {"model": semantic_settings.model, "cosine": cos}
                facts.append(edge(x, links.SYNONYM_REL, y, [unit], props=props, status=STATUS, conf=cos))
        out[item] = facts
    for item, (src, dst, props), units in made:
        out.setdefault(item, []).append(edge(src, merge.MERGE_REL, dst, units, props=props, status=STATUS))
    cov.update(
        distinct=len(groups.name),
        merged=groups.merged,
        synonyms=len(pairs),
        synonym_status=status,
        code_links=len(code),
        code_ambiguous=ambiguous,
    )


def _said(config: dict, answer: tuple, groups) -> tuple:
    """(item, unit, has a document node, label, [(node id, props, conf)], [((src, rel, dst), props)]) of one answer
    once its names are folded; in the lightrag style a relation's ends are in order, as LightRAG's have no direction."""
    item, unit, has_doc, cleaned, _r, label = answer
    evidence = {"model": config["model"], "item": item}
    old_ids: dict[str, str] = {}
    nodes = []
    for n in cleaned["nodes"]:
        nid = groups.id_of(n["kind"], n["name"])
        old_ids.setdefault(entity_id(n["kind"], n["name"]), nid)
        props = {**evidence, "evidence": n["evidence"], **{k: n[k] for k in NODE_EXTRAS if k in n}}
        nodes.append((nid, props, n["conf"]))
    for n in cleaned["nodes"]:  # the edges of an answer memoized before ``slug`` folded accents
        old_ids.setdefault(f"entity:{n['kind']}:{_old_slug(n['name'])}", groups.id_of(n["kind"], n["name"]))
    edges = []
    for e in cleaned["edges"]:
        src, dst = old_ids.get(e["src"]), old_ids.get(e["dst"])
        if src is None or dst is None or src == dst:
            continue
        if style_of(config) == "lightrag" and dst < src:
            src, dst = dst, src
        props = {**evidence, "evidence": e["evidence"], **{k: e[k] for k in EDGE_EXTRAS if k in e}}
        edges.append(((src, e["rel"], dst), props))
    return item, unit, has_doc, label, nodes, edges


def _merge(config, answers, stated, groups, pairs, step, source) -> tuple:
    """The groups once the pairs the model calls one are folded, but those a decision undid, and the MERGE_REL edge of
    each merge: (item, (the node that stays, the document of the item of the node folded in, props), units)."""
    seen: dict[str, merge.Seen] = {}  # node id -> what the model is shown of it: its most public item's
    for item, _unit, _doc, cleaned, _r, label in sorted(answers, key=lambda a: (a[4], a[0])):
        for n in cleaned["nodes"]:
            nid = groups.id_of(n["kind"], n["name"])
            if nid not in seen:
                desc = n.get("description") or ""
                seen[nid] = merge.Seen(groups.kind[nid], groups.name[nid], desc, item, label)
    docs = {a[0] for a in answers if a[2]}
    cands = merge.candidates(groups, pairs)
    decided = step.merges(cands, seen)
    same = [d for d in decided if d[3]["same"]]
    step.tally("merge", "candidates", len(cands))
    step.tally("merge", "same", len(same))

    def joins(chosen):
        return [
            ((groups.kind[a], links.key(groups.name[a])), (groups.kind[b], links.key(groups.name[b])))
            for a, b, _, _ in chosen
        ]

    def edges_of(chosen, folded) -> list[tuple]:
        found = []
        for a, b, why, d in chosen:
            keep = folded.id_of(groups.kind[a], groups.name[a])
            units = sorted({f"{seen[a].item}#", f"{seen[b].item}#"})
            for side, other in ((a, b), (b, a)):
                if side == keep or seen[side].item not in docs:
                    continue
                props = {
                    "model": config["model"],
                    "name": seen[side].name,
                    "kind": seen[side].kind,
                    "with": seen[other].name,
                    "why": why,
                    "reason": d.get("reason") or "",
                }
                found.append((seen[side].item, (keep, seen[side].item, props), units))
        return found

    folded = links.group(stated, joins(same))
    made = edges_of(same, folded)
    undid = merge.undone(*source) if source else set()
    if undid:
        off = {i for i, (_item, (keep, doc, _p), _u) in enumerate(made) if (keep, doc) in undid}
        if off:
            back = {(m[1][0], m[1][1]) for i, m in enumerate(made) if i in off}
            stay = [d for d in same if not any(e[1][:2] in back for e in edges_of([d], folded))]
            undone = [m for i, m in enumerate(made) if i in off]
            for _item, (_keep, _doc, props), _units in undone:
                props["undone"] = True
            step.tally("merge", "undone", len(same) - len(stay))
            folded = links.group(stated, joins(stay))
            made = edges_of(stay, folded) + undone
    return folded, made


def _remap_pairs(pairs: list, before, after) -> list:
    """The synonym pairs of the groups ``before`` as ids of ``after``; a pair folded into one node is gone."""
    out = set()
    for a, b, cos in pairs:
        x = after.id_of(before.kind[a], before.name[a])
        y = after.id_of(before.kind[b], before.name[b])
        if x and y and x != y:
            out.add((min(x, y), max(x, y), cos))
    return [list(p) for p in sorted(out)]


def _shared(said: list[tuple], groups, step) -> dict:
    """What every item of one label shares of a node or a relation, by (node id or (src, rel, dst), label): the
    ``summary`` of their descriptions, or the ``descriptions`` while there is none; of a relation also the sum of its
    strengths, ``weight``, and its ``keywords``, each once (``entity_merge``)."""
    nodes: dict[tuple, list[str]] = defaultdict(list)
    rels: dict[tuple, list[dict]] = defaultdict(list)
    for _item, _unit, _doc, label, ns, es in said:
        for nid, props, _ in ns:
            if props.get("description"):
                nodes[(nid, label)].append(props["description"])
        for key, props in es:
            rels[(key, label)].append(props)
    out: dict[tuple, dict] = {}
    for (nid, label), found in nodes.items():
        subject = f'the {groups.kind[nid]} "{groups.name[nid]}"'
        out[(nid, label)] = _described(step, subject, found, label)
    for (key, label), stated in rels.items():
        subject = f'the relation between "{groups.name.get(key[0], key[0])}" and "{groups.name.get(key[2], key[2])}"'
        shared = _described(step, subject, [p["description"] for p in stated if p.get("description")], label)
        strengths = [p["strength"] for p in stated if isinstance(p.get("strength"), (int, float))]
        if strengths:
            shared["weight"] = round(sum(strengths), 4)
        words = merge.merged_keywords([p["keywords"] for p in stated if p.get("keywords")])
        if words and len(stated) > 1:
            shared["keywords"] = words
        if len(stated) > 1:
            shared["sources"] = len(stated)
        out[(key, label)] = shared
    return {k: v for k, v in out.items() if v}


def _described(step, subject: str, said: list[str], label) -> dict:
    fragments = sorted(set(said))
    if len(fragments) < 2 and not (fragments and merge.needs_summary(fragments)):
        return {}
    if merge.needs_summary(fragments):
        text = step.summary(subject, fragments, label)
        if text:
            step.tally("summaries", "count")
            return {"summary": text, "sources": len(said)}
    return {"descriptions": fragments[: merge.MAX_DESCRIPTIONS], "sources": len(said)}


def unfinished(coverage: dict) -> bool:
    """A build whose entity step left items, merges or summaries for a later build, or whose calls failed: not the
    build to skip next time."""
    found = coverage.get("entities") or {}
    left = (found.get("merge") or {}).get("pending") or (found.get("summaries") or {}).get("pending")
    return bool(found.get("skipped") or found.get("failed") or left) and not found.get("offline")


def not_enabled(tuning) -> dict:
    """What coverage says of a project whose tuning holds a mapping but whose strategy does not turn the step on."""
    return {
        "enabled": False,
        "reason": "the strategy of the project does not enable the entities step"
        if tuning.entities
        else "no entities mapping",
    }


__all__ = ["BudgetStop", "EntitiesError", "check", "enabled", "run", "unfinished"]
