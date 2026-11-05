"""What a project's knowledge graph is tuned by, in one place: the semantic mode and model, the weight of the ranking by
meaning in the fusion, the BM25 weights of the name, path and body columns, the length from which a body is also indexed
as passages and the length of a passage, the characters of a row that are embedded, and the name of the strategy the
project uses (``evo_agents.kg.strategies``), the entities step's mapping, and the walk that ranks kg_search and
kg_context by Personalized PageRank (``walk``, ``evo_agents.kg.ppr``; off unless set).

A project that set nothing gets the constants the code has always used, so it ranks as before. ``Tuning`` holds the
values in effect; a settings file holds only what differs from the default. Two kinds of key:

- query keys (``semantic_weight``, ``bm25_name``, ``bm25_path``, ``bm25_body``, ``walk``, ``semantic_mode`` between
  index and on) change how ``kg_search`` reads a graph that exists: they take effect at once and can be measured on the newest build;
- build keys (``passage_from``, ``passage_chars``, ``embed_chars``, ``semantic_model``, ``strategy``, and
  ``semantic_mode`` to or from off) change what a build writes: they take effect with the next build, and measuring one
  needs that build (``rebuild_keys``).

On this machine the file is ``<kg home>/<project>/settings.json``; ``semantic.json`` stays the home of the semantic mode
and model and is read with it. On the hub the values are the project's row of kg_settings, and its semantic mode and
model are its row of kg_semantic (``evo_agents.hub.kg_settings``). Standard library only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields, replace
from pathlib import Path

from evo_agents.kg import search as text_search
from evo_agents.kg import semantic

SETTINGS_FILE = "settings.json"
HISTORY_FILE = "settings-history.jsonl"
STRATEGY_PATTERN = r"^[a-z][a-z0-9-]{0,39}$"


@dataclass(frozen=True)
class Tuning:
    semantic_mode: str = "off"
    semantic_model: str = semantic.DEFAULT_MODEL
    semantic_weight: float = semantic.SEMANTIC_WEIGHT
    bm25_name: float = text_search.WEIGHTS[2]
    bm25_path: float = text_search.WEIGHTS[3]
    bm25_body: float = text_search.WEIGHTS[4]
    passage_from: int = text_search.PASSAGE_FROM
    passage_chars: int = text_search.PASSAGE_CHARS
    embed_chars: int = semantic.EMBED_CHARS
    strategy: str | None = None
    entities: dict | None = (
        None  # the entities step's mapping (evo_agents.kg.entities); runs only under a strategy that enables it
    )
    walk: dict | None = (
        None  # the Personalized PageRank walk of kg_search and kg_context (evo_agents.kg.ppr); None: off
    )

    @property
    def bm25(self) -> tuple[float, float, float, float, float]:
        """The weights of node_fts's columns: node_id, kind, name, path, body."""
        return (0.0, 0.0, self.bm25_name, self.bm25_path, self.bm25_body)

    def semantic_settings(self) -> semantic.Settings:
        return semantic.Settings(self.semantic_mode, self.semantic_model, self.embed_chars)

    def to_json(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def overrides(self) -> dict:
        """The keys whose value is not the default."""
        base = Tuning()
        return {k: v for k, v in self.to_json().items() if v != getattr(base, k)}


DEFAULT = Tuning()
BUILD_KEYS = frozenset({"passage_from", "passage_chars", "embed_chars", "semantic_model", "strategy", "entities"})
SEMANTIC_KEYS = frozenset({"semantic_mode", "semantic_model"})  # kept where semantic mode and model always were
KEYS = tuple(f.name for f in fields(Tuning))
_FLOATS = {"semantic_weight": (0.0, 20.0), "bm25_name": (0.0, 50.0), "bm25_path": (0.0, 50.0), "bm25_body": (0.0, 50.0)}
_INTS = {"passage_from": (500, 200_000), "passage_chars": (200, 20_000), "embed_chars": (200, 8_000)}


class SettingsError(ValueError):
    pass


def _semantic_mode(value):
    if value not in semantic.MODES:
        raise SettingsError(f"semantic_mode {value!r} is not one of {', '.join(semantic.MODES)}")
    return value


def _semantic_model(value):
    if not isinstance(value, str) or not value.strip():
        raise SettingsError("semantic_model must be a model name")
    return value.strip()


def _entities(value):
    from evo_agents.kg import entities

    try:
        return entities.check(value)
    except entities.EntitiesError as exc:
        raise SettingsError(str(exc)) from None


def _walk(value):
    from evo_agents.kg import ppr

    try:
        return ppr.check(value)
    except ppr.WalkError as exc:
        raise SettingsError(str(exc)) from None


def _strategy(value):
    import re

    if value is None or value == "":
        return None
    if not isinstance(value, str) or not re.match(STRATEGY_PATTERN, value):
        raise SettingsError(f"strategy {value!r} must be a lowercase name of letters, digits and hyphens")
    return value


_CHECKS = {
    "semantic_mode": _semantic_mode,
    "semantic_model": _semantic_model,
    "entities": _entities,
    "walk": _walk,
    "strategy": _strategy,
}


def _number(key: str, value):
    """``value`` as the number ``key`` holds, in its range."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise SettingsError(f"{key} must be a number")
    try:
        number = float(value)
    except ValueError:
        raise SettingsError(f"{key} {value!r} is not a number") from None
    low, high = _FLOATS.get(key) or _INTS[key]
    if not low <= number <= high:
        raise SettingsError(f"{key} {value} is outside {low:g} to {high:g}")
    if key in _INTS:
        if number != int(number):
            raise SettingsError(f"{key} {value} must be a whole number")
        return int(number)
    return number


def _check(key: str, value):
    """``value`` as the type ``key`` holds, or SettingsError saying what is wrong."""
    if key not in KEYS:
        raise SettingsError(f"{key} is not a KG setting: {', '.join(KEYS)}")
    check = _CHECKS.get(key)
    return check(value) if check else _number(key, value)


def parse_value(key: str, text: str):
    """The value of ``--set KEY=VALUE``: json when it reads as json (numbers, null), else the text itself."""
    try:
        value = json.loads(text)
    except ValueError:
        value = text
    return _check(key, value)


def of(data: dict | None, base: Tuning = DEFAULT) -> Tuning:
    """``base`` with the keys of ``data`` set; SettingsError for an unknown key or a value out of range."""
    if data is None:
        return base
    if not isinstance(data, dict):
        raise SettingsError("settings must be an object of key: value")
    return replace(base, **{k: _check(k, v) for k, v in data.items()})


def rebuild_keys(before: Tuning, after: Tuning) -> list[str]:
    """The keys that differ between ``before`` and ``after`` and need a new build to take effect (semantic_mode counts
    when it moves to or from off: vectors are written or dropped by a build)."""
    changed = [k for k in KEYS if getattr(before, k) != getattr(after, k)]
    out = [k for k in changed if k in BUILD_KEYS]
    if "semantic_mode" in changed and "off" in (before.semantic_mode, after.semantic_mode):
        out.append("semantic_mode")
    return out


def diff(before: Tuning, after: Tuning) -> dict:
    return {
        k: {"from": getattr(before, k), "to": getattr(after, k)}
        for k in KEYS
        if getattr(before, k) != getattr(after, k)
    }


# -- on this machine ----------------------------------------------------------------------------------------------


def read_file(root: Path) -> dict:
    """The overrides the project's settings.json holds, {} without one."""
    path = Path(root) / SETTINGS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:
        raise SettingsError(f"{path}: {exc}") from None
    if not isinstance(data, dict):
        raise SettingsError(f"{path}: expected an object")
    return data


def read(root: Path) -> Tuning:
    """The project's tuning: the defaults, then semantic.json (mode and model), then settings.json. A value that is not
    valid is an error naming the file."""
    base = DEFAULT
    try:
        chosen = semantic.read_settings(root)
    except ValueError as exc:
        raise SettingsError(str(exc)) from None
    base = replace(base, semantic_mode=chosen.mode, semantic_model=chosen.model)
    found = read_file(root)
    try:
        return of(found, base)
    except SettingsError as exc:
        raise SettingsError(f"{Path(root) / SETTINGS_FILE}: {exc}") from None


def write(root: Path, tuning: Tuning, *, reason: str | None = None, figures: dict | None = None) -> Tuning:
    """Keep ``tuning``: the semantic mode and model in semantic.json, the other overrides in settings.json, and a line of
    settings-history.jsonl with what the file held before, the reason and the figures cited."""
    import time

    root = Path(root)
    before = read(root)
    root.mkdir(parents=True, exist_ok=True)
    semantic.write_settings(root, tuning.semantic_settings())
    rest = {k: v for k, v in tuning.overrides().items() if k not in SEMANTIC_KEYS}
    path = root / SETTINGS_FILE
    if rest:
        path.write_text(json.dumps(rest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    else:
        path.unlink(missing_ok=True)
    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "before": before.to_json(),
        "after": tuning.to_json(),
        "reason": reason,
        "figures": figures,
    }
    with (root / HISTORY_FILE).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return tuning


def history(root: Path) -> list[dict]:
    try:
        lines = (Path(root) / HISTORY_FILE).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


def undo(root: Path) -> Tuning | None:
    """Put back what the last change replaced (the change itself is a line of history, so a second undo undoes the
    undo); None when nothing was ever changed."""
    found = history(root)
    if not found:
        return None
    return write(root, of(found[-1]["before"]), reason="undo")


# -- one run under another tuning ---------------------------------------------------------------------------------

_OVERRIDE: list[Tuning] = []


def override_of(project=None) -> Tuning | None:
    """The tuning a run asked for (``overriding``), which every store opened meanwhile uses in place of the project's."""
    return _OVERRIDE[-1] if _OVERRIDE else None


class overriding:
    """``with overriding(overrides):`` stores opened by project read search settings from ``overrides`` (a dict of
    keys, or None for none) on top of the defaults, whatever the project's files hold: how ``kg bench run --settings``
    and the hub's measure ask a candidate. Build keys cannot be tried this way (``rebuild_keys``)."""

    def __init__(self, overrides: dict | None):
        self.tuning = None if not overrides else of(overrides)

    def __enter__(self):
        if self.tuning is not None:
            _OVERRIDE.append(self.tuning)
        return self.tuning

    def __exit__(self, *exc):
        if self.tuning is not None:
            _OVERRIDE.pop()
