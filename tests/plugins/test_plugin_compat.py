"""Whether a plugin runs on a release: the clauses of an ``engines`` range, pre-releases before their release as PEP
440 orders them, the plugin API, and plugin versions ordered as semantic versions. Where packaging is installed, the
ranges agree with its specifiers."""

from __future__ import annotations

import pytest

from evo_agents import __version__
from evo_agents.plugins.compat import (
    PLUGIN_API_VERSIONS,
    SpecError,
    parse_spec,
    plugin_version_key,
    problem,
    satisfies,
    version_key,
)

RANGES = [
    (">=0.16", "0.16.0", True),
    (">=0.16", "0.15.9", False),
    (">=0.16, <0.17", "0.16.9", True),
    (">=0.16, <0.17", "0.17.0", False),
    (">=0.16,<0.17", "0.17.0rc1", False),
    ("<0.17", "0.17.0rc1", False),
    ("<0.17", "0.17.0.dev3", False),
    ("<0.17", "0.16.9rc1", True),
    ("<0.17.0rc2", "0.17.0rc1", True),
    (">=0.17.0rc1", "0.17.0rc2", True),
    (">=0.17.0rc1", "0.17.0b1", False),
    (">0.16", "0.16.0", False),
    (">0.16", "0.16.1", True),
    ("<=0.16.1", "0.16.1", True),
    ("==0.16.1", "0.16.1", True),
    ("==0.16", "0.16.0", True),
    ("==0.16.*", "0.16.4", True),
    ("==0.16.*", "0.17.0", False),
    ("!=0.16.*", "0.16.4", False),
    ("!=0.16.2", "0.16.1", True),
    ("~=0.16.1", "0.16.9", True),
    ("~=0.16.1", "0.17.0", False),
    ("~=0.16.1", "0.16.0", False),
    ("~=0.16", "0.99.0", True),
    ("~=0.16", "1.0.0", False),
]


@pytest.mark.parametrize(("spec", "version", "inside"), RANGES)
def test_plugin_compat_engines_ranges(spec, version, inside):
    assert satisfies(version, spec) is inside


@pytest.mark.parametrize(("spec", "version", "inside"), RANGES)
def test_plugin_compat_engines_ranges_agree_with_packaging(spec, version, inside):
    specifiers = pytest.importorskip("packaging.specifiers")
    assert specifiers.SpecifierSet(spec).contains(version, prereleases=True) is inside


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("", "an empty range"),
        (" , ", "an empty range"),
        ("0.16", "'0.16' is not a clause"),
        (">=0.16 <0.17", "is not a clause"),
        ("=>0.16", "is not a clause"),
        (">=0.16.*", "a wildcard goes with == or != only"),
        ("~=1", "a compatible release names at least two numbers"),
    ],
)
def test_plugin_compat_ranges_that_do_not_read_say_why(spec, message):
    with pytest.raises(SpecError, match=message):
        parse_spec(spec)


def test_plugin_compat_orders_releases_after_their_pre_releases():
    order = ["0.16.0.dev1", "0.16.0a1", "0.16.0b2", "0.16.0rc1", "0.16.0", "0.16.0.1", "0.16.1", "0.17"]
    assert sorted(order, key=version_key) == order
    assert version_key("0.16") == version_key("0.16.0")
    with pytest.raises(SpecError, match="is not a version of evo-agents"):
        version_key("latest")


def test_plugin_compat_names_the_plugin_api_and_the_range_a_plugin_misses():
    assert PLUGIN_API_VERSIONS == (1,)
    assert problem({"evo-agents": ">=0.16"}, 1, "0.16.1") is None
    assert problem({"evo-agents": f"=={__version__}"}, 1) is None  # this release by default
    assert problem({"evo-agents": ">=0.16"}, 2, "0.16.1") == (
        "it is written for plugin API 2; evo-agents 0.16.1 runs plugin API 1"
    )
    assert problem({"evo-agents": ">=0.16, <0.17"}, 1, "0.17.0") == "it runs on evo-agents >=0.16, <0.17, not on 0.17.0"
    assert "does not read" in problem({"evo-agents": "soon"}, 1, "0.16.1")


def test_plugin_compat_orders_plugin_versions_as_semantic_versions():
    order = [
        "0.9.0",
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0",
        "1.2.0",
    ]
    assert sorted(reversed(order), key=plugin_version_key) == order
    assert plugin_version_key("10.0.0") > plugin_version_key("9.9.9")
    for wrong in ("1.0", "01.0.0", "1.0.0+build", "v1.0.0"):
        with pytest.raises(ValueError, match="is not a plugin version"):
            plugin_version_key(wrong)
