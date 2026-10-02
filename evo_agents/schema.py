"""A small JSON Schema validator.

The core depends only on the standard library and PyYAML, so this module implements the subset of
JSON Schema the harness and protocol schemas use: type, enum, const, pattern, required, properties,
additionalProperties, items, minItems, minimum, maximum, minLength, anyOf, oneOf and local $ref.

Two extensions keep readers lenient while writers stay strict: ``"x-unknown": "warn"`` on an object
schema reports keys outside ``properties`` as warnings instead of errors, and ``"x-severity":
"warning"`` downgrades every issue found under that schema node.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


@dataclass(frozen=True)
class Issue:
    path: str
    message: str
    severity: str = "error"  # error | warning

    def __str__(self) -> str:
        where = self.path or "<root>"
        return f"{self.severity}: {where}: {self.message}"


def _is_type(value, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    expected = _TYPES.get(name)
    if expected is None:
        raise ValueError(f"unsupported schema type {name!r}")
    return isinstance(value, expected)


def _join(path: str, key) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else str(key)


class Validator:
    def __init__(self, schema: dict):
        self.root = schema

    def _resolve(self, ref: str) -> dict:
        if not ref.startswith("#/"):
            raise ValueError(f"only local $ref is supported, got {ref!r}")
        node = self.root
        for part in ref[2:].split("/"):
            node = node[part]
        return node

    def validate(self, value) -> list[Issue]:
        issues: list[Issue] = []
        self._check(value, self.root, "", issues)
        return issues

    def _check(self, value, schema: dict, path: str, issues: list[Issue]) -> None:
        if schema.get("x-severity") == "warning":
            local: list[Issue] = []
            self._check_node(value, schema, path, local)
            issues.extend(Issue(i.path, i.message, "warning") for i in local)
            return
        self._check_node(value, schema, path, issues)

    def _check_node(self, value, schema: dict, path: str, issues: list[Issue]) -> None:
        if "$ref" in schema:
            self._check(value, self._resolve(schema["$ref"]), path, issues)
            return

        if "anyOf" in schema or "oneOf" in schema:
            options = schema.get("anyOf") or schema.get("oneOf")
            matches = [opt for opt in options if not self._errors_only(value, opt, path)]
            if not matches:
                issues.append(Issue(path, "does not match any allowed form"))
                return
            if "oneOf" in schema and len(matches) > 1:
                issues.append(Issue(path, "matches more than one form"))
                return
            # Re-run the matching option to collect its warnings.
            self._check(value, matches[0], path, issues)

        types = schema.get("type")
        if types is not None:
            names = types if isinstance(types, list) else [types]
            if not any(_is_type(value, name) for name in names):
                issues.append(Issue(path, f"expected {' or '.join(names)}, got {type(value).__name__}"))
                return

        if "const" in schema and value != schema["const"]:
            issues.append(Issue(path, f"must equal {schema['const']!r}"))
        if "enum" in schema and value not in schema["enum"]:
            allowed = ", ".join(repr(v) for v in schema["enum"])
            issues.append(Issue(path, f"must be one of {allowed}, got {value!r}"))

        if isinstance(value, str):
            if "pattern" in schema and not re.search(schema["pattern"], value):
                issues.append(Issue(path, f"does not match pattern {schema['pattern']!r}"))
            if "minLength" in schema and len(value) < schema["minLength"]:
                issues.append(Issue(path, f"shorter than {schema['minLength']} characters"))

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in schema and value < schema["minimum"]:
                issues.append(Issue(path, f"must be >= {schema['minimum']}"))
            if "maximum" in schema and value > schema["maximum"]:
                issues.append(Issue(path, f"must be <= {schema['maximum']}"))

        if isinstance(value, list):
            if "minItems" in schema and len(value) < schema["minItems"]:
                issues.append(Issue(path, f"needs at least {schema['minItems']} item(s)"))
            if "items" in schema:
                for i, item in enumerate(value):
                    self._check(item, schema["items"], _join(path, i), issues)

        if isinstance(value, dict):
            for key in schema.get("required", []):
                if key not in value:
                    issues.append(Issue(_join(path, key), "is required"))
            props = schema.get("properties", {})
            extra = schema.get("additionalProperties", True)
            for key, item in value.items():
                if key in props:
                    self._check(item, props[key], _join(path, key), issues)
                elif isinstance(extra, dict):
                    self._check(item, extra, _join(path, key), issues)
                elif extra is False:
                    severity = "warning" if schema.get("x-unknown") == "warn" else "error"
                    issues.append(Issue(_join(path, key), "unknown key", severity))

    def _errors_only(self, value, schema: dict, path: str) -> list[Issue]:
        found: list[Issue] = []
        self._check(value, schema, path, found)
        return [i for i in found if i.severity == "error"]


def validate(value, schema: dict) -> list[Issue]:
    return Validator(schema).validate(value)


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]
