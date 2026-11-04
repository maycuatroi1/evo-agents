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
- the answer is memoized by the item's content digest, the model and the mapping, so an unchanged item costs nothing;
- calls, tokens and cost are counted per build, and the step stops at the ceiling (``max_calls``, ``max_tokens``,
  ``max_cost``), leaves the rest to a later build (``coverage["entities"]["skipped"]``) and the next build continues
  from the memo.

A model is one of: ``command``, an argv run on this machine that reads one JSON object on stdin
(``{"text", "node_kinds", "relations", "model"}``) and writes one on stdout
(``{"nodes": [{"kind", "name", "evidence", "conf"}], "edges": [{"src", "rel", "dst", "evidence"}], "usage":
{"input_tokens", "output_tokens", "cost"}}``); or ``url``, an OpenAI-compatible endpoint, reached with the ``openai``
library of the optional extra ``entities`` (``key_env`` names the variable that holds the key, never the key).
Verification of a build never calls a model: it reads the memo only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from urllib.parse import urlparse

from evo_agents.kg.protocol import canonical_json, sha256

VERSION = "1"
STATUS = "extracted"
DEFAULT_CONF = 0.6
DEFAULT_MAX_CHARS = 12_000
DEFAULT_MAX_CALLS = 200
DEFAULT_MAX_TOKENS = 400_000
DEFAULT_MAX_COST = 5.0
COMMAND_TIMEOUT = 300
REFUSED_SAMPLES = 20
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
SLUG = re.compile(r"[^a-z0-9]+")

FIELDS = {
    "items",  # record kinds read; the prose kinds when absent
    "model",  # the name recorded as the evidence of what it stated
    "sink",  # a policy sink of kind llm-backend; absent: the model is local
    "command",  # argv of a local model
    "url",  # an OpenAI-compatible endpoint
    "key_env",  # the variable that holds its key
    "node_kinds",  # the kinds it may state, all in the ontology
    "relations",  # the relations it may state
    "max_calls",
    "max_tokens",
    "max_cost",
    "price",  # {"input": cost per million tokens in, "output": per million out}, for a model that reports no cost
    "max_chars",  # characters of an item sent
}
_INTS = ("max_calls", "max_tokens", "max_chars")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.:/-]{0,79}$")


class EntitiesError(ValueError):
    pass


class BudgetStop(Exception):
    """Raised inside a memoized task before the call that would pass the ceiling: nothing is cached for it."""


def check(value):
    """``value`` as the ``entities`` setting holds it, or EntitiesError saying what is wrong. None or {} turns it off."""
    if value is None or value == {}:
        return None
    if not isinstance(value, dict):
        raise EntitiesError("entities must be a mapping")
    unknown = sorted(set(value) - FIELDS)
    if unknown:
        raise EntitiesError(f"entities has unknown keys {', '.join(unknown)} (known: {', '.join(sorted(FIELDS))})")
    out = dict(value)
    for key in ("items", "node_kinds", "relations"):
        if key in out:
            items = out[key]
            if not isinstance(items, list) or not items or not all(isinstance(i, str) and i for i in items):
                raise EntitiesError(f"entities.{key} must be a list of names")
    if "model" not in out or not isinstance(out["model"], str) or not _NAME.match(out["model"]):
        raise EntitiesError("entities.model must name the model (letters, digits, . _ : / -), it is the evidence")
    if not out.get("node_kinds"):
        raise EntitiesError("entities.node_kinds must list the kinds the model may state")
    if "command" in out:
        c = out["command"]
        if not isinstance(c, list) or not c or not all(isinstance(a, str) and a for a in c):
            raise EntitiesError("entities.command must be an argv list")
    if "command" in out and "url" in out:
        raise EntitiesError("entities names a command or a url, not both")
    if "url" in out:
        url = out["url"]
        if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https") or not urlparse(url).hostname:
            raise EntitiesError("entities.url must be an http(s) url")
        if "sink" not in out and urlparse(url).hostname not in LOCAL_HOSTS:
            raise EntitiesError(
                "entities.url reaches a model that is not local: name the llm-backend sink in entities.sink"
            )
    if "command" not in out and "url" not in out:
        raise EntitiesError("entities needs a command (a local model) or a url")
    if "sink" in out and not (isinstance(out["sink"], str) and out["sink"]):
        raise EntitiesError("entities.sink must be a sink id of the policy")
    if "key_env" in out and not (isinstance(out["key_env"], str) and re.match(r"^[A-Z_][A-Z0-9_]*$", out["key_env"])):
        raise EntitiesError("entities.key_env must be the name of an environment variable")
    for key in _INTS:
        if key in out and (isinstance(out[key], bool) or not isinstance(out[key], int) or out[key] < 1):
            raise EntitiesError(f"entities.{key} must be a positive whole number")
    if "max_cost" in out and (
        isinstance(out["max_cost"], bool) or not isinstance(out["max_cost"], (int, float)) or out["max_cost"] < 0
    ):
        raise EntitiesError("entities.max_cost must be a number of at least 0")
    if "price" in out:
        p = out["price"]
        if (
            not isinstance(p, dict)
            or set(p) - {"input", "output"}
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 for v in p.values())
        ):
            raise EntitiesError("entities.price must be {input, output}: cost per million tokens")
    return out


def enabled(tuning, harness_root=None) -> dict | None:
    """The mapping to run, when the tuning's strategy turns the step on and holds a mapping; else None."""
    from evo_agents.kg import strategy

    if not tuning.entities or "entities" not in strategy.steps_of(tuning.strategy, harness_root):
        return None
    return tuning.entities


def slug(name: str) -> str:
    return SLUG.sub("-", name.lower()).strip("-")[:80]


def entity_id(kind: str, name: str) -> str:
    return f"entity:{kind}:{slug(name)}"


def config_digest(config: dict, node_kinds: list[str], relations: list[str]) -> str:
    keep = {"model": config["model"], "node_kinds": node_kinds, "relations": relations}
    keep["max_chars"] = config.get("max_chars", DEFAULT_MAX_CHARS)
    return sha256(canonical_json({**keep, "version": VERSION}))[:16]


class Spend:
    """What one build used of the ceiling."""

    def __init__(self, config: dict):
        self.max_calls = config.get("max_calls", DEFAULT_MAX_CALLS)
        self.max_tokens = config.get("max_tokens", DEFAULT_MAX_TOKENS)
        self.max_cost = float(config.get("max_cost", DEFAULT_MAX_COST))
        self.price = config.get("price") or {}
        self.calls = self.tokens_in = self.tokens_out = 0
        self.cost = 0.0

    def exhausted(self) -> str | None:
        if self.calls >= self.max_calls:
            return f"max_calls {self.max_calls}"
        if self.tokens_in + self.tokens_out >= self.max_tokens:
            return f"max_tokens {self.max_tokens}"
        if self.cost >= self.max_cost:
            return f"max_cost {self.max_cost:g}"
        return None

    def add(self, usage: dict) -> None:
        tin, tout = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        self.calls += 1
        self.tokens_in += tin
        self.tokens_out += tout
        if usage.get("cost") is not None:
            self.cost += float(usage["cost"])
        else:
            self.cost += tin / 1e6 * float(self.price.get("input", 0)) + tout / 1e6 * float(self.price.get("output", 0))


# -- the models ---------------------------------------------------------------------------------------------------


def _parse_answer(text: str) -> dict:
    """The JSON object in a model's answer (it may sit in a fence or after a sentence)."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise EntitiesError("the model answered without a JSON object")
    try:
        data = json.loads(text[start : end + 1])
    except ValueError as exc:
        raise EntitiesError(f"the model's JSON does not parse: {exc}") from None
    if not isinstance(data, dict):
        raise EntitiesError("the model's answer is not an object")
    return data


def _prompt(text: str, node_kinds: list[str], relations: list[str]) -> str:
    return (
        "Read the text and state the entities and the relations between them that it says outright.\n"
        f"Entity kinds you may use: {', '.join(node_kinds)}.\n"
        f"Relations you may use: {', '.join(relations) or 'none'}.\n"
        'Answer with one JSON object: {"nodes": [{"kind", "name", "evidence", "conf"}], '
        '"edges": [{"src", "rel", "dst", "evidence"}]}, where src and dst are names of your nodes, evidence is a short '
        "quote of the text and conf is between 0 and 1. Leave out what the text does not say.\n\nTEXT:\n" + text
    )


def call_command(config: dict, text: str, node_kinds: list[str], relations: list[str]) -> dict:
    request = {"text": text, "node_kinds": node_kinds, "relations": relations, "model": config["model"]}
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
    if done.returncode != 0:
        raise EntitiesError(f"the model command exited {done.returncode}: {done.stderr.strip()[:200]}")
    return _parse_answer(done.stdout)


def _openai_client(config: dict):
    try:
        import openai
    except ImportError:
        raise EntitiesError("the url model needs the openai library: pip install 'evo-agents[entities]'") from None
    key = os.environ.get(config.get("key_env") or "") if config.get("key_env") else None
    return openai.OpenAI(base_url=config["url"], api_key=key or "none")


CLIENT_FACTORY = _openai_client  # tests replace it


def call_endpoint(config: dict, text: str, node_kinds: list[str], relations: list[str]) -> dict:
    client = CLIENT_FACTORY(config)
    try:
        got = client.chat.completions.create(
            model=config["model"],
            messages=[{"role": "user", "content": _prompt(text, node_kinds, relations)}],
            temperature=0,
        )
    except Exception as exc:  # the library's errors differ by version: a failed call is a failed call
        raise EntitiesError(f"the model endpoint failed: {type(exc).__name__}: {exc}") from None
    data = _parse_answer(got.choices[0].message.content or "")
    usage = getattr(got, "usage", None)
    if usage is not None:
        data["usage"] = {
            "input_tokens": getattr(usage, "prompt_tokens", 0),
            "output_tokens": getattr(usage, "completion_tokens", 0),
        }
    return data


def call_model(config: dict, text: str, node_kinds: list[str], relations: list[str]) -> dict:
    if "command" in config:
        return call_command(config, text, node_kinds, relations)
    return call_endpoint(config, text, node_kinds, relations)


# -- the step -----------------------------------------------------------------------------------------------------


def _clean(answer: dict, node_kinds: set[str], relations: set[str]) -> dict:
    """What of a model's answer is within the kinds and relations allowed: nodes and edges, with the counts dropped."""
    nodes, seen, dropped = [], {}, 0
    for n in answer.get("nodes") or []:
        name = n.get("name") if isinstance(n, dict) else None
        kind = n.get("kind") if isinstance(n, dict) else None
        if not isinstance(name, str) or not name.strip() or kind not in node_kinds or not slug(name):
            dropped += 1
            continue
        name = name.strip()[:200]
        key = (kind, slug(name))
        if key in seen:
            continue
        try:
            conf = min(max(float(n.get("conf", DEFAULT_CONF)), 0.0), 1.0)
        except (TypeError, ValueError):
            conf = DEFAULT_CONF
        seen[key] = {"kind": kind, "name": name, "conf": conf, "evidence": str(n.get("evidence") or "")[:200]}
        nodes.append(seen[key])
    by_name = {}
    for n in nodes:
        by_name.setdefault(slug(n["name"]), n)
    edges = []
    for e in answer.get("edges") or []:
        if not isinstance(e, dict) or e.get("rel") not in relations:
            dropped += 1
            continue
        src, dst = by_name.get(slug(str(e.get("src") or ""))), by_name.get(slug(str(e.get("dst") or "")))
        if src is None or dst is None or src is dst:
            dropped += 1
            continue
        edges.append(
            {
                "src": entity_id(src["kind"], src["name"]),
                "rel": e["rel"],
                "dst": entity_id(dst["kind"], dst["name"]),
                "evidence": str(e.get("evidence") or "")[:200],
            }
        )
    return {"nodes": nodes, "edges": edges, "dropped": dropped}


def run(config, project, records, body, memo, unit_labels, first, kinds, relations_known, *, offline=False):
    """Run the step: ``(facts per item id, coverage)``. ``config`` is the mapping of ``enabled``; ``body(rec)`` the text
    of an item; ``first`` the facts the item's map stage gave (the document node an edge starts from); ``kinds`` and
    ``relations_known`` what the ontology holds (None: everything). ``offline`` reads the memo and calls nothing."""
    from evo_agents.kg.pipeline.stages import edge, item_unit, node
    from evo_agents.kg.profile import class_of

    policy = project.policy
    out: dict[str, list[dict]] = {}
    cov = {
        "enabled": True,
        "model": config["model"],
        "sink": config.get("sink"),
        "local": "command" in config or urlparse(config.get("url") or "").hostname in LOCAL_HOSTS,
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
    }
    allowed_kinds = [k for k in config["node_kinds"] if kinds is None or k in kinds]
    allowed_rels = [r for r in config.get("relations") or [] if relations_known is None or r in relations_known]
    cov["rejected_config"] = sorted(
        (set(config["node_kinds"]) - set(allowed_kinds)) | (set(config.get("relations") or []) - set(allowed_rels))
    )
    sink = config.get("sink")
    sink_problem = None
    if sink is not None:
        if sink not in policy.sinks:
            sink_problem = f"sink {sink!r} is not declared in knowledge.yaml policy.sinks"
        elif policy.sinks[sink].kind != "llm-backend":
            sink_problem = f"sink {sink!r} is of kind {policy.sinks[sink].kind!r}, not llm-backend"
    if not allowed_kinds:
        cov["stopped"] = "no node kind of entities.node_kinds is in the ontology"
        return out, cov
    wanted = set(config.get("items") or [])
    spend = Spend(config)
    digest = config_digest(config, allowed_kinds, allowed_rels)
    max_chars = config.get("max_chars", DEFAULT_MAX_CHARS)
    for rec in records:
        r = rec.record
        if (r["kind"] not in wanted) if wanted else (class_of(r["kind"]) != "prose"):
            continue
        label = unit_labels[item_unit(rec.item_id)]
        if sink_problem or (sink is not None and not policy.allows(label, sink)):
            cov["refused_count"] += 1
            if len(cov["refused"]) < REFUSED_SAMPLES:
                why = sink_problem or (
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

        def task(text=text):
            if offline:
                raise BudgetStop("offline")
            why = spend.exhausted()
            if why:
                raise BudgetStop(why)
            answer = call_model(config, text, allowed_kinds, allowed_rels)
            usage = answer.get("usage") if isinstance(answer.get("usage"), dict) else {}
            spend.add(usage)
            cleaned = _clean(answer, set(allowed_kinds), set(allowed_rels))
            cleaned["usage"] = usage
            return cleaned

        before = memo.misses["entities"]
        try:
            cleaned, _ = memo.run("entities", VERSION, inputs, task)
        except BudgetStop as stop:
            memo.misses["entities"] = before
            cov["skipped"] += 1
            if str(stop) != "offline":
                cov["stopped"] = f"the ceiling of this build was reached ({stop}); the rest waits for a later build"
            continue
        except EntitiesError as exc:
            memo.misses["entities"] = before
            cov["failed"] += 1
            cov.setdefault("errors", [])
            if len(cov["errors"]) < REFUSED_SAMPLES:
                cov["errors"].append({"item": rec.item_id, "error": str(exc)[:200]})
            continue
        if memo.misses["entities"] == before:
            cov["cache_hits"] += 1
        unit = item_unit(rec.item_id)
        facts: list[dict] = []
        has_doc = any(f["t"] == "node" and f["id"] == rec.item_id for f in first.get(rec.item_id, []))
        evidence = {"model": config["model"], "item": rec.item_id}
        for n in cleaned["nodes"]:
            nid = entity_id(n["kind"], n["name"])
            facts.append(
                node(
                    nid,
                    n["kind"],
                    n["name"],
                    [unit],
                    props={**evidence, "evidence": n["evidence"]},
                    status=STATUS,
                    conf=n["conf"],
                )
            )
            if has_doc:
                facts.append(
                    edge(
                        rec.item_id,
                        "mentions",
                        nid,
                        [unit],
                        props={**evidence, "evidence": n["evidence"]},
                        status=STATUS,
                        conf=n["conf"],
                    )
                )
        for e in cleaned["edges"]:
            facts.append(
                edge(
                    e["src"],
                    e["rel"],
                    e["dst"],
                    [unit],
                    props={**evidence, "evidence": e["evidence"]},
                    status=STATUS,
                    conf=DEFAULT_CONF,
                )
            )
        cov["nodes"] += len(cleaned["nodes"])
        cov["edges"] += len(cleaned["edges"])
        cov["rejected"] += cleaned["dropped"]
        out[rec.item_id] = facts
    cov.update(
        calls=spend.calls,
        tokens_in=spend.tokens_in,
        tokens_out=spend.tokens_out,
        cost=round(spend.cost, 6),
    )
    return out, cov


def unfinished(coverage: dict) -> bool:
    """A build whose entity step left items for a later build, or whose calls failed: not the build to skip next time."""
    found = coverage.get("entities") or {}
    return bool(found.get("skipped") or found.get("failed")) and not found.get("offline")


def not_enabled(tuning) -> dict:
    """What coverage says of a project whose tuning holds a mapping but whose strategy does not turn the step on."""
    return {
        "enabled": False,
        "reason": "the strategy of the project does not enable the entities step"
        if tuning.entities
        else "no entities mapping",
    }


__all__ = ["BudgetStop", "EntitiesError", "check", "enabled", "run", "unfinished"]
