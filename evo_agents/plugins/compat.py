"""Whether a plugin runs on a release of evo-agents: its ``engines`` range, its ``plugin_api`` and the version of
each extension point it contributes to.

``engines["evo-agents"]`` is a list of clauses separated by commas, each an operator and a version: ``>=``, ``>``,
``<=``, ``<``, ``==`` (``==0.17.*`` takes every 0.17 release), ``!=`` and ``~=`` (``~=0.17.2`` is ``>=0.17.2,
==0.17.*``). A version of evo-agents is dotted numbers with an optional pre-release or development suffix
(``0.17.0rc1``, ``0.17.0.dev3``), which comes before the release it precedes, as PEP 440 orders them; missing numbers
are zeros. As in PEP 440, ``<0.17`` leaves out the pre-releases of 0.17 too. ``plugin_api`` is the major version of
the plugin API a plugin is written against, one of PLUGIN_API_VERSIONS.

Each extension point has a version of its own (``POINT_VERSIONS``), as Backstage versions its extension points: the
keys a contribution to the point takes and the calls the host makes for it. A contribution names the version it is
written against with ``api`` (1 when it names none); a release that does not serve that version of the point does not
run the plugin, so a point can change its contract in a new version while plugins written for the old one keep
running on the releases that serve it.

Plugin versions are MAJOR.MINOR.PATCH with an optional ``-pre-release``, ordered as semantic versions:
``plugin_version_key`` sorts them, a pre-release before its release. Standard library only.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from evo_agents import __version__

PLUGIN_API_VERSIONS = (1,)  # the plugin API majors this release of evo-agents runs
# The extension points, each with the versions of its contract this release serves (docs/plugins.md).
POINT_VERSIONS: dict[str, tuple[int, ...]] = {
    "channels": (1,),
    "routes": (1,),
    "mcp_tools": (1,),
    "jobs": (1,),
    "events": (1,),
    "pages": (1,),
    "agent_packs": (1,),
    "agent_types": (1,),
    "runtimes": (1,),
    "environments": (1,),
    "kg_connectors": (1,),
    "kg_sources": (1,),
    "tables": (1,),
    "loop_rules": (1,),
    "cli": (1,),
    "notice_kinds": (1,),
}
PLUGIN_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z.-]{1,40}))?")
_RELEASE = re.compile(r"(\d+(?:\.\d+)*)(?:\.?(a|b|rc|dev)\.?(\d*))?")
_CLAUSE = re.compile(r"(>=|<=|==|!=|~=|>|<)\s*(\d+(?:\.\d+)*(?:\.\*)?(?:\.?(?:a|b|rc|dev)\.?\d*)?)")
_SUFFIX_RANK = {"dev": 0, "a": 1, "b": 2, "rc": 3}
_RELEASE_RANK = 4  # a release sorts after its development and pre-releases
_WIDTH = 4  # numbers compared, the missing ones zeros


class SpecError(ValueError):
    """An ``engines`` range that is not a list of clauses this module reads."""


def version_key(text: str) -> tuple:
    """A sort key of an evo-agents version such as ``0.16.1``, ``0.17.0rc1`` or ``0.17.0.dev3``; SpecError when it is
    none."""
    found = _RELEASE.fullmatch(text.strip())
    if found is None:
        raise SpecError(f"{text!r} is not a version of evo-agents, such as 0.17.0")
    numbers = tuple(int(part) for part in found[1].split("."))
    numbers = (numbers + (0,) * _WIDTH)[: max(_WIDTH, len(numbers))]
    suffix = found[2]
    rank = _RELEASE_RANK if suffix is None else _SUFFIX_RANK[suffix]
    return (numbers, rank, int(found[3] or 0))


def _wildcard(bound: str) -> Callable[[tuple], bool]:
    """Whether a version's numbers start with those of ``bound`` (``0.17.*``)."""
    prefix = tuple(int(part) for part in bound.split("."))
    return lambda key: key[0][: len(prefix)] == prefix


def _clause(operator: str, bound: str) -> Callable[[tuple], bool]:
    if bound.endswith(".*"):
        if operator not in ("==", "!="):
            raise SpecError(f"{operator}{bound}: a wildcard goes with == or != only")
        inside = _wildcard(bound[:-2])
        return inside if operator == "==" else (lambda key: not inside(key))
    limit = version_key(bound)
    if operator == "~=":
        parts = bound.split(".")
        if len(parts) < 2:
            raise SpecError(f"~={bound}: a compatible release names at least two numbers, such as ~=0.17")
        inside = _wildcard(".".join(parts[:-1]))
        return lambda key: key >= limit and inside(key)
    released = limit[1] == _RELEASE_RANK
    compare = {
        ">=": lambda key: key >= limit,
        ">": lambda key: key > limit,
        "<=": lambda key: key <= limit,
        # <0.17 leaves out 0.17.0rc1 too, a pre-release of the release it excludes (PEP 440)
        "<": lambda key: key < limit and not (released and key[0] == limit[0]),
        "==": lambda key: key == limit,
        "!=": lambda key: key != limit,
    }
    return compare[operator]


def parse_spec(spec: str) -> list[Callable[[tuple], bool]]:
    """The clauses of ``spec`` as predicates on ``version_key``; SpecError naming the clause that is wrong."""
    clauses = [part.strip() for part in spec.split(",")]
    if not any(clauses):
        raise SpecError("an empty range: write clauses such as >=0.17, <0.19")
    parsed = []
    for clause in clauses:
        found = _CLAUSE.fullmatch(clause)
        if found is None:
            raise SpecError(f"{clause!r} is not a clause such as >=0.17 or ==0.17.*")
        parsed.append(_clause(found[1], found[2]))
    return parsed


def satisfies(version: str, spec: str) -> bool:
    """Whether evo-agents ``version`` is in the range ``spec``."""
    key = version_key(version)
    return all(clause(key) for clause in parse_spec(spec))


def point_problem(contributes: dict | None, version: str = __version__) -> str | None:
    """Why the contributions ``contributes`` (a manifest's) name a version of an extension point that evo-agents
    ``version`` does not serve, or None."""
    for point, items in (contributes or {}).items():
        served = POINT_VERSIONS.get(point, ())
        for item in items:
            api = item.get("api", 1)
            if api not in served:
                listed = ", ".join(str(each) for each in served) or "none"
                return (
                    f"its {point} contribution {item.get('name')!r} is written for version {api} of that extension "
                    f"point; evo-agents {version} serves version {listed}"
                )
    return None


def problem(engines: dict, plugin_api: int, version: str = __version__, contributes: dict | None = None) -> str | None:
    """Why a plugin with ``engines``, ``plugin_api`` and the contributions ``contributes`` does not run on evo-agents
    ``version``, or None when it does."""
    if plugin_api not in PLUGIN_API_VERSIONS:
        supported = ", ".join(str(api) for api in PLUGIN_API_VERSIONS)
        return f"it is written for plugin API {plugin_api}; evo-agents {version} runs plugin API {supported}"
    spec = engines.get("evo-agents", "")
    try:
        inside = satisfies(version, spec)
    except SpecError as exc:
        return f"its engines range {spec!r} does not read: {exc}"
    if not inside:
        return f"it runs on evo-agents {spec}, not on {version}"
    return point_problem(contributes, version)


def plugin_version_key(text: str) -> tuple:
    """A sort key of a plugin version, MAJOR.MINOR.PATCH[-pre]; ValueError when it is none."""
    found = PLUGIN_VERSION.fullmatch(text)
    if found is None:
        raise ValueError(f"{text!r} is not a plugin version, MAJOR.MINOR.PATCH with an optional -pre-release")
    numbers = (int(found[1]), int(found[2]), int(found[3]))
    pre = found[4]
    if pre is None:
        return (numbers, 1, ())
    parts = tuple((0, int(part), "") if part.isdigit() else (1, 0, part) for part in pre.split("."))
    return (numbers, 0, parts)
