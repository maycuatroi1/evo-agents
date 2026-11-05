"""The ``entities`` mapping of the build step ``evo_agents.kg.entities``: its check, its ceiling and what a model is asked.

``check`` reads the setting and says what is wrong with it, ``enabled`` gives the mapping a strategy turns on,
``Spend`` counts one build's calls, tokens and cost against the ceiling, and ``prompt`` and ``parse_answer`` are the
question a model gets and the reading of its answer. ``evo_agents.kg.entities`` imports each name it uses from here, so
callers keep reading them there.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from evo_agents.kg import claude_code

DEFAULT_MAX_CHARS = 12_000
DEFAULT_MAX_CALLS = 200
DEFAULT_MAX_TOKENS = 400_000
DEFAULT_MAX_COST = 5.0
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
STYLES = ("closed", "lightrag")
OTHER = "Other"  # the kind of a lightrag entity whose type is none of the mapping's node kinds

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
    "synonym_threshold",  # the cosine from which two entities are synonyms, when the graph has vectors (entity_links)
    "jobs",  # calls at once (default 1); not part of what is memoized
    "style",  # what the model is asked: closed (the default) or lightrag (``prompt``)
    "reasoning",  # of a url model, sent as the request's ``reasoning`` (OpenRouter's): {"enabled": false} turns it off
    "merge",  # whether the model merges entities and summarizes descriptions (entity_merge); default: lightrag only
}
_INTS = ("max_calls", "max_tokens", "max_chars", "jobs")
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.:/-]{0,79}$")


class EntitiesError(ValueError):
    """A call that failed, or an answer that does not read; ``usage`` is what the call reported it used, when it did."""

    def __init__(self, message: str, usage: dict | None = None):
        super().__init__(message)
        self.usage = usage


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
    _check_names(out)
    _check_command(out)
    _check_url(out)
    _check_access(out)
    _check_ceiling(out)
    _check_tuning(out)
    return out


def _check_names(out: dict) -> None:
    """The lists of names, the model's name and the node kinds."""
    for key in ("items", "node_kinds", "relations"):
        if key in out:
            items = out[key]
            if not isinstance(items, list) or not items or not all(isinstance(i, str) and i for i in items):
                raise EntitiesError(f"entities.{key} must be a list of names")
    if "model" not in out or not isinstance(out["model"], str) or not _NAME.match(out["model"]):
        raise EntitiesError("entities.model must name the model (letters, digits, . _ : / -), it is the evidence")
    if not out.get("node_kinds"):
        raise EntitiesError("entities.node_kinds must list the kinds the model may state")
    if out.get("style", "closed") not in STYLES:
        raise EntitiesError(f"entities.style must be one of {', '.join(STYLES)}")
    if out.get("style") == "lightrag" and not out.get("relations"):
        raise EntitiesError("entities.relations must name the relation a lightrag style stores its relations as")


def _check_command(out: dict) -> None:
    if "command" in out:
        c = out["command"]
        if not isinstance(c, list) or not c or not all(isinstance(a, str) and a for a in c):
            raise EntitiesError("entities.command must be an argv list")
    if "command" in out and "url" in out:
        raise EntitiesError("entities names a command or a url, not both")
    if "command" in out and claude_code.uses(out["command"]) and "sink" not in out:
        raise EntitiesError(
            "entities.command runs Claude Code, which sends the text to Anthropic: name the llm-backend sink in "
            "entities.sink"
        )


def _check_url(out: dict) -> None:
    if "url" in out:
        url = out["url"]
        if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https") or not urlparse(url).hostname:
            raise EntitiesError("entities.url must be an http(s) url")
        if "sink" not in out and urlparse(url).hostname not in LOCAL_HOSTS:
            raise EntitiesError(
                "entities.url reaches a model that is not local: name the llm-backend sink in entities.sink"
            )


def _check_access(out: dict) -> None:
    """A model to reach, the sink it is reached through and the variable of its key."""
    if "command" not in out and "url" not in out:
        raise EntitiesError("entities needs a command (a local model) or a url")
    if "sink" in out and not (isinstance(out["sink"], str) and out["sink"]):
        raise EntitiesError("entities.sink must be a sink id of the policy")
    if "key_env" in out and not (isinstance(out["key_env"], str) and re.match(r"^[A-Z_][A-Z0-9_]*$", out["key_env"])):
        raise EntitiesError("entities.key_env must be the name of an environment variable")


def _check_ceiling(out: dict) -> None:
    for key in _INTS:
        if key in out and (isinstance(out[key], bool) or not isinstance(out[key], int) or out[key] < 1):
            raise EntitiesError(f"entities.{key} must be a positive whole number")
    if "max_cost" in out and (
        isinstance(out["max_cost"], bool) or not isinstance(out["max_cost"], (int, float)) or out["max_cost"] < 0
    ):
        raise EntitiesError("entities.max_cost must be a number of at least 0")


def _check_tuning(out: dict) -> None:
    """The price of a model that reports no cost, the cosine of a synonym and the reasoning of a url model."""
    if "reasoning" in out and (not isinstance(out["reasoning"], dict) or "url" not in out):
        raise EntitiesError("entities.reasoning must be a mapping, sent to a url model as its reasoning")
    if "price" in out:
        p = out["price"]
        if (
            not isinstance(p, dict)
            or set(p) - {"input", "output"}
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 for v in p.values())
        ):
            raise EntitiesError("entities.price must be {input, output}: cost per million tokens")
    if "merge" in out and not isinstance(out["merge"], bool):
        raise EntitiesError("entities.merge must be true or false")
    if "synonym_threshold" in out:
        t = out["synonym_threshold"]
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t <= 1:
            raise EntitiesError("entities.synonym_threshold must be a cosine above 0 and at most 1")


def enabled(tuning, harness_root=None) -> dict | None:
    """The mapping to run, when the tuning's strategy turns the step on and holds a mapping; else None."""
    from evo_agents.kg import strategy

    if not tuning.entities or "entities" not in strategy.steps_of(tuning.strategy, harness_root):
        return None
    return tuning.entities


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


def estimate(text: str, node_kinds: list[str], relations: list[str], style: str = "closed") -> dict:
    """The usage of a call that reported none, such as one that failed or timed out: the prompt's tokens in (4
    characters a token), none out."""
    return {"input_tokens": -(-len(prompt(text, node_kinds, relations, style)) // 4), "output_tokens": 0}


# -- what a model is asked ---------------------------------------------------------------------------------------


def parse_answer(text: str) -> dict:
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


def prompt(text: str, node_kinds: list[str], relations: list[str], style: str | None = "closed") -> str:
    """What a model is asked of ``text``: the entities of ``node_kinds`` and the ``relations`` it says outright; in the
    lightrag style, entities of open types with a description, and relations with keywords, a description and a
    strength."""
    if style == "lightrag":
        return (
            "Read the text and list the entities it names and the relations between them that it states.\n"
            f"Entity types, a suggestion: {', '.join(k for k in node_kinds if k != OTHER)}; use Other when none fits.\n"
            'Answer with one JSON object, nothing else: {"nodes": [{"name", "type", "description", "evidence"}], '
            '"edges": [{"src", "dst", "keywords", "description", "strength", "evidence"}]}. name: as the text writes '
            "it. description: one short sentence of what the text says of it, not repeating the evidence. src and "
            "dst: names of your nodes. keywords: two or three words of what ties them. strength: 1 to 10, how "
            "strongly the text ties them. evidence: the text's own words, at most 8. No line breaks or indentation. "
            'A node may add "aliases": other names the text gives it. '
            "Keep the entities the text's facts are about; leave out what it does not say.\n\nTEXT:\n" + text
        )
    return (
        "Read the text and state the entities and the relations between them that it says outright.\n"
        f"Entity kinds you may use: {', '.join(node_kinds)}.\n"
        f"Relations you may use: {', '.join(relations) or 'none'}.\n"
        'Answer with one JSON object: {"nodes": [{"kind", "name", "evidence", "conf"}], '
        '"edges": [{"src", "rel", "dst", "evidence"}]}, where src and dst are names of your nodes, evidence is a short '
        'quote of the text and conf is between 0 and 1. A node may add "aliases": the other names the text gives it. '
        "Leave out what the text does not say.\n\nTEXT:\n" + text
    )
