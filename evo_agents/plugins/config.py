"""The config of a plugin as layers (seam plugin-v1): the defaults of the config schema its manifest gives, then the
hub's own layer (instance), a project's, an agent profile's of that project and a run's. Each layer sets some fields;
a field takes its value from the highest layer that sets it, whole (a layer that sets an object replaces it, it does
not merge into it).

- ``fields``: the fields a config schema declares, and which of them hold a secret (``"x-secret": true``).
- ``check_values``: what is wrong with the values one layer sets: a field the schema does not declare, a value its
  field refuses, a secret where none may go (a run's layer), a secret that is no string. No message ever holds a
  secret's value.
- ``merge``: the effective config of a stack of layers, each field with its value (none for a secret), the layer and
  the version of that layer that set it; what is wrong with it (a field the schema requires that no layer sets, a field
  set that the schema no longer declares, or no longer takes); and its digest.
- ``digest``: the SHA-256 of the effective config in canonical JSON, a secret counted by the fingerprint the hub gives
  its sealed value (``Layer.secrets``), never by the value, so that the digest changes with a secret and gives nothing
  of it away.

Standard library only, besides ``evo_agents.schema``.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from evo_agents.schema import errors, validate

LAYERS = ("default", "instance", "project", "agent", "run")  # lowest first: a later layer wins
STORED_LAYERS = ("instance", "project", "agent")  # the layers the hub keeps in versions
SECRET = "x-secret"
MAX_LAYER_BYTES = 64 * 1024  # the values one layer sets, as JSON
MAX_SECRET_CHARS = 16 * 1024
DIGEST_PREFIX = "sha256:"
NO_ECHO = "the hub does not repeat a secret's value"


class ConfigError(ValueError):
    """Values one layer may not set: ``problems`` says each thing that is wrong, with the field."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def fields(schema: Mapping | None) -> dict[str, bool]:
    """name -> whether it holds a secret, for each field ``schema`` (a manifest's ``config``) declares."""
    properties = (schema or {}).get("properties") or {}
    return {name: bool(isinstance(spec, Mapping) and spec.get(SECRET) is True) for name, spec in properties.items()}


def defaults(schema: Mapping | None) -> dict:
    """The ``default`` of each field that gives one, secrets left out: the lowest layer."""
    properties = (schema or {}).get("properties") or {}
    secret = fields(schema)
    return {
        name: copy.deepcopy(spec["default"])
        for name, spec in properties.items()
        if isinstance(spec, Mapping) and "default" in spec and not secret[name]
    }


def _partial(schema: Mapping) -> dict:
    """``schema`` checking only the fields given: what it requires, and what it says of fields it does not declare,
    are the effective config's business."""
    return {key: value for key, value in schema.items() if key not in ("required", "additionalProperties")}


def _issues(schema: Mapping, values: Mapping) -> list[str]:
    try:
        return [f"{issue.path}: {issue.message}" for issue in errors(validate(dict(values), _partial(schema)))]
    except (ValueError, KeyError, TypeError) as exc:  # a construct evo_agents.schema does not read
        return [f"the config schema holds what the hub cannot check ({exc}); fix the plugin's manifest"]


def _secret_problem(schema: Mapping, name: str, value) -> str | None:
    if not isinstance(value, str) or not value:
        return f"{name}: a secret's value is a string that is not empty"
    if len(value) > MAX_SECRET_CHARS:
        return f"{name}: a secret's value is at most {MAX_SECRET_CHARS} characters"
    if _issues(schema, {name: value}):
        return f"{name}: the value does not fit the field's schema ({NO_ECHO})"
    return None


def check_values(schema: Mapping | None, values: Mapping, *, secrets: bool = True) -> list[str]:
    """What is wrong with the values one layer sets, each problem naming its field; ``secrets`` False for a layer
    that may hold no secret (a run's). Empty when nothing is."""
    if not values:
        return []
    if not schema:
        return ["the plugin declares no config: its manifest has no config schema"]
    declared = fields(schema)
    problems, plain = [], {}
    for name, value in values.items():
        if name not in declared:
            known = ", ".join(sorted(declared)) or "none"
            problems.append(f"{name}: not a field of the plugin's config (its fields: {known})")
        elif not declared[name]:
            plain[name] = value
        elif not secrets:
            problems.append(f"{name}: a secret, which a run's layer may not set; set it on the hub or the project")
        elif (problem := _secret_problem(schema, name, value)) is not None:
            problems.append(problem)
    problems.extend(_issues(schema, plain))
    if len(json.dumps(plain, ensure_ascii=False).encode()) > MAX_LAYER_BYTES:
        problems.append(f"the values of one layer are at most {MAX_LAYER_BYTES // 1024} KiB as JSON")
    return problems


@dataclass(frozen=True)
class Layer:
    """One layer of a stack: its name (one of LAYERS), the plain values it sets, the secrets it sets by the
    fingerprint of their sealed values, and the version of the layer (None for the defaults and a run's)."""

    name: str
    values: Mapping = field(default_factory=dict)
    secrets: Mapping[str, str] = field(default_factory=dict)
    version: int | None = None


@dataclass(frozen=True)
class Field:
    """One field of an effective config: its value (None for a secret), whether it is a secret, and the layer and
    version that set it."""

    value: object
    secret: bool
    layer: str
    version: int | None
    fingerprint: str | None = None


@dataclass(frozen=True)
class Effective:
    fields: dict[str, Field]
    problems: tuple[str, ...]
    digest: str


def _where(layer: Layer) -> str:
    return layer.name if layer.version is None else f"{layer.name} v{layer.version}"


def _placed(schema: Mapping | None, layers: list[Layer]) -> tuple[dict[str, Field], list[str]]:
    declared = fields(schema)
    placed: dict[str, Field] = {}
    problems: list[str] = []
    for layer in layers:
        given = [(name, value, False) for name, value in layer.values.items()]
        given += [(name, fingerprint, True) for name, fingerprint in layer.secrets.items()]
        for name, value, secret in given:
            if name not in declared:
                problems.append(f"{name}: set at {_where(layer)}, and the config has no such field now")
            elif declared[name] != secret:
                kind = "a secret" if declared[name] else "not a secret"
                problems.append(f"{name}: set at {_where(layer)}, and the field is {kind} now")
            elif secret:
                placed[name] = Field(None, True, layer.name, layer.version, value)
            else:
                placed[name] = Field(value, False, layer.name, layer.version)
    return placed, problems


def merge(schema: Mapping | None, layers: list[Layer]) -> Effective:
    """The effective config of ``layers`` (lowest first, the defaults of ``schema`` below them all)."""
    stack = [Layer("default", defaults(schema)), *layers]
    placed, problems = _placed(schema, stack)
    plain = {name: item.value for name, item in placed.items() if not item.secret}
    problems.extend(f"{problem} (as the layers set it)" for problem in _issues(schema or {}, plain))
    for name in (schema or {}).get("required") or []:
        if name not in placed:
            problems.append(f"{name}: required, and no layer sets it")
    return Effective(placed, tuple(problems), digest(placed))


def digest(placed: Mapping[str, Field]) -> str:
    """sha256:<hex> of the effective config: each field's value, a secret's fingerprint in its place."""
    canonical = {
        name: {"secret": item.fingerprint} if item.secret else {"value": item.value}
        for name, item in sorted(placed.items())
    }
    text = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return DIGEST_PREFIX + hashlib.sha256(text.encode()).hexdigest()
