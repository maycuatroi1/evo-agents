"""The surface of the API of core plugins as JSON, and the rule that says which version a change of it takes.

``surface`` describes each name of ``__all__`` but API_VERSION: a function by its signature (each parameter's name,
kind, annotation as written and default), a dataclass by its fields, a pydantic model by its fields, a TypedDict by
its keys, an error by its bases, a class by its attributes and methods, an ``Annotated`` alias by what it annotates, a
constant by its value.
``tests/plugins/golden/core-plugin-api.json`` holds the surface of the version the hub serves, and
``tests/plugins/test_core_plugin_api_surface.py`` compares the two with ``required_version``: a surface that only adds
(a name, a keyword with a default, a field with a default, a key that is not required, a value of a Literal) takes the
next minor version; one that removes or changes anything the next major version, minor 0.

Private to ``core_plugin_api``: ``python -m evo_agents.hub.server.core_plugin_api surface`` prints it.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import typing
from typing import Annotated

API = "evo_agents.hub.server.core_plugin_api"
LEFT_OUT = ("API_VERSION",)  # the version is the surface's own key, not one of its names


def _default(value) -> str | None:
    return None if value is inspect.Parameter.empty else repr(value)


def _annotation(value) -> str:
    if value is inspect.Parameter.empty:
        return ""
    if isinstance(value, typing.ForwardRef):
        return value.__forward_arg__
    if isinstance(value, str) or (isinstance(value, type) and not typing.get_args(value)):
        return value if isinstance(value, str) else value.__name__
    return repr(value).replace("typing.", "")


def _signature(function) -> dict:
    found = inspect.signature(function)
    params = [
        {
            "name": param.name,
            "kind": param.kind.name.lower(),
            "annotation": _annotation(param.annotation),
            "default": _default(param.default),
        }
        for param in found.parameters.values()
    ]
    return {"params": params, "returns": _annotation(found.return_annotation)}


def _metadata(item) -> str:
    """A metadata object of an ``Annotated`` as its kind and its settings: Depends(web_session), Path(pattern=...)."""
    if hasattr(item, "dependency"):
        return f"Depends({getattr(item.dependency, '__name__', '?')})"
    settings = [
        f"{key}={value!r}"
        for part in getattr(item, "metadata", []) or []
        for key, value in sorted(vars(part).items())
        if value is not None
    ]
    return f"{type(item).__name__}({', '.join(settings)})"


def _class(value: type) -> dict:
    own = {name: member for name, member in vars(value).items() if not name.startswith("_") or name == "__init__"}
    methods = {name: _signature(member) for name, member in sorted(own.items()) if inspect.isfunction(member)}
    attributes = {name: repr(member) for name, member in sorted(own.items()) if not callable(member)}
    return {"methods": methods, "attributes": attributes}


def describe(value) -> dict:
    """What one name of the surface is, as JSON."""
    if typing.get_origin(value) is Annotated:
        annotated, *metadata = typing.get_args(value)
        return {"kind": "alias", "annotated": _annotation(annotated), "metadata": [_metadata(item) for item in metadata]}
    if isinstance(value, type) and issubclass(value, BaseException):
        found = {"kind": "error", "bases": [base.__name__ for base in value.__bases__]}
        if "__init__" in vars(value):
            found["init"] = _signature(vars(value)["__init__"])
        return found
    if isinstance(value, type) and typing.is_typeddict(value):
        hints = value.__annotations__
        return {
            "kind": "typeddict",
            "keys": {name: _annotation(hint) for name, hint in sorted(hints.items())},
            "required": sorted(value.__required_keys__),
        }
    if isinstance(value, type) and hasattr(value, "model_fields"):  # a pydantic model, such as ErrorBody
        fields = {
            name: {"annotation": _annotation(item.annotation), "required": item.is_required()}
            for name, item in value.model_fields.items()
        }
        return {"kind": "model", "fields": fields}
    if isinstance(value, type) and dataclasses.is_dataclass(value):
        fields = [
            {
                "name": item.name,
                "annotation": _annotation(item.type),
                "default": None if item.default is dataclasses.MISSING else repr(item.default),
            }
            for item in dataclasses.fields(value)
        ]
        return {"kind": "dataclass", "frozen": value.__dataclass_params__.frozen, "fields": fields}
    if isinstance(value, type):
        return {"kind": "class", "bases": [base.__name__ for base in value.__bases__], **_class(value)}
    if inspect.iscoroutinefunction(value):
        return {"kind": "async function", **_signature(value)}
    if inspect.isfunction(value):
        return {"kind": "function", **_signature(value)}
    return {"kind": "constant", "value": repr(value)}


def surface() -> dict:
    """The surface of the API: its version and each name of its ``__all__`` but API_VERSION, described."""
    api = importlib.import_module(API)
    names = {name: describe(getattr(api, name)) for name in sorted(api.__all__) if name not in LEFT_OUT}
    return {"api_version": list(api.API_VERSION), "names": names}


# The rule of versions


def _widens(old: str, new: str) -> bool:
    """Whether annotation ``new`` is ``old``, or ``old`` with more values in its Literals."""
    if old == new:
        return True
    try:
        return _wider(ast.parse(old, mode="eval").body, ast.parse(new, mode="eval").body)
    except SyntaxError:
        return False


def _literal_values(node) -> list | None:
    if not (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "Literal"):
        return None
    items = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
    if not all(isinstance(item, ast.Constant) for item in items):
        return None
    return [item.value for item in items]


def _wider(old, new) -> bool:
    before, after = _literal_values(old), _literal_values(new)
    if before is not None and after is not None:
        return set(before) <= set(after)
    if type(old) is not type(new):
        return False
    for name, value in ast.iter_fields(old):
        other = getattr(new, name)
        if isinstance(value, list):
            if not isinstance(other, list) or len(value) != len(other):
                return False
            if not all(_wider(a, b) if isinstance(a, ast.AST) else a == b for a, b in zip(value, other, strict=True)):
                return False
        elif isinstance(value, ast.AST):
            if not _wider(value, other):
                return False
        elif value != other:
            return False
    return True


def _same_or_wider(old: dict, new: dict) -> bool:
    keys = set(old) | set(new)
    return all(
        _widens(old.get(key, ""), new.get(key, "")) if key == "annotation" else old.get(key) == new.get(key)
        for key in keys
    )


def _adds(old: list[dict], new: list[dict]) -> bool:
    """Whether the list ``new`` (of parameters or fields) is ``old``, each the same or wider, then items with a
    default."""
    if len(new) < len(old) or [item["name"] for item in old] != [item["name"] for item in new[: len(old)]]:
        return False
    if not all(_same_or_wider(a, b) for a, b in zip(old, new, strict=False)):
        return False
    return all(item.get("default") is not None for item in new[len(old) :])


def _signature_adds(old: dict, new: dict) -> bool:
    return _widens(old.get("returns", ""), new.get("returns", "")) and _adds(old["params"], new["params"])


def additive(old: dict, new: dict) -> bool:
    """Whether the description ``new`` of a name only adds to ``old``."""
    if old.get("kind") != new.get("kind"):
        return False
    kind = old["kind"]
    if kind in ("function", "async function"):
        return _signature_adds(old, new)
    if kind == "dataclass":
        return old["frozen"] == new["frozen"] and _adds(old["fields"], new["fields"])
    if kind == "model":
        old_fields, new_fields = old["fields"], new["fields"]
        kept = all(name in new_fields and new_fields[name] == item for name, item in old_fields.items())
        return kept and not any(item["required"] for name, item in new_fields.items() if name not in old_fields)
    if kind == "typeddict":
        kept = all(_widens(hint, new["keys"].get(name, "")) for name, hint in old["keys"].items())
        return kept and set(new["required"]) == set(old["required"])
    if kind == "class":
        same = old["bases"] == new["bases"] and old["attributes"].items() <= new["attributes"].items()
        kept = old["methods"].items()
        return same and all(name in new["methods"] and _signature_adds(sig, new["methods"][name]) for name, sig in kept)
    return old == new


def changes(golden: dict, current: dict) -> dict[str, list[str]]:
    """The names ``current`` adds to ``golden``, removes, changes, and widens (changes by adding only)."""
    before, after = golden["names"], current["names"]
    changed = [name for name in sorted(set(before) & set(after)) if before[name] != after[name]]
    return {
        "added": sorted(set(after) - set(before)),
        "removed": sorted(set(before) - set(after)),
        "changed": [name for name in changed if not additive(before[name], after[name])],
        "widened": [name for name in changed if additive(before[name], after[name])],
    }


def required_version(golden: dict, current: dict) -> tuple[int, int]:
    """The version ``current`` takes against ``golden``: the same with no change, the next minor when it only adds,
    the next major, minor 0, when it removes or changes anything."""
    major, minor = golden["api_version"]
    found = changes(golden, current)
    if found["removed"] or found["changed"]:
        return major + 1, 0
    if found["added"] or found["widened"]:
        return major, minor + 1
    return major, minor


__all__ = ["additive", "changes", "describe", "required_version", "surface"]
