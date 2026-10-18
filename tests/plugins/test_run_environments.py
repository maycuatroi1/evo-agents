"""The run environments a plugin declares (``evo_agents.plugins.run_environments``): a container image pinned by its
digest, its network and its mounts, read from the manifest, registered as <plugin>/<name> beside host, and sent to a
worker as a declaration it reads back whole or refuses."""

from __future__ import annotations

import pytest

from evo_agents.plugins.manifest import ManifestError, is_per_project, read_manifest
from evo_agents.plugins.run_environments import (
    HOST,
    ContainerEnvironment,
    Mount,
    container_environments,
    environment_name,
    is_environment,
    is_plugin_environment,
    plugin_of,
    target_problem,
)
from tests.plugins.samples import manifest

DIGEST = "sha256:" + "0123456789abcdef" * 4
BOX = {
    "name": "box",
    "title": "Agent box",
    "image": f"ghcr.io/acme/agent-box:1.2@{DIGEST}",
    "network": "outbound",
    "mounts": [
        {"type": "tmpfs", "target": "/scratch", "size_mb": 256},
        {"type": "volume", "name": "npm-cache", "target": "/cache/npm", "read_only": False},
    ],
}


def _read(*environments: dict, **changes):
    return read_manifest(manifest(contributes={"environments": list(environments)}, **changes))


def test_plugin_environment_reads_and_registers_as_plugin_slash_name():
    bare = {"name": "bare", "image": f"busybox@{DIGEST}", "network": "none"}
    found = container_environments(_read(BOX, bare, version="0.3.0"))
    assert found == [
        ContainerEnvironment(
            environment="sandbox/box",
            plugin="sandbox",
            version="0.3.0",
            title="Agent box",
            image=f"ghcr.io/acme/agent-box:1.2@{DIGEST}",
            network="outbound",
            mounts=(Mount("tmpfs", "/scratch", size_mb=256), Mount("volume", "/cache/npm", name="npm-cache")),
        ),
        ContainerEnvironment("sandbox/bare", "sandbox", "0.3.0", "sandbox/bare", f"busybox@{DIGEST}", "none"),
    ]
    box = found[0]
    assert box.provider == "container" and box.volume_name(box.mounts[1]) == "evo-sandbox-npm-cache"
    assert box.to_json()["provider"] == "container"
    assert ContainerEnvironment.from_json(box.to_json()) == box
    assert ContainerEnvironment.from_json(found[1].to_json()) == found[1]


def test_plugin_environment_names_are_namespaced_by_the_plugin():
    assert environment_name("sandbox", "box") == "sandbox/box"
    assert is_plugin_environment("sandbox/box") and is_plugin_environment("srs-docs/node_22")
    assert is_environment(HOST) and is_environment("sandbox/box")
    for name in ("host/", "container", "sandbox/", "/box", "Sandbox/box", "a/b/c", None, 3):
        assert not is_plugin_environment(name), name
        assert not is_environment(name), name
    assert plugin_of("sandbox/box") == "sandbox" and plugin_of(HOST) is None


def test_plugin_environment_makes_a_core_plugin_one_a_project_enables():
    core = manifest(tier="core", permissions=None, config=None, contributes={"environments": [BOX]})
    assert read_manifest(core).per_project() and is_per_project(core)


@pytest.mark.parametrize(
    ("change", "said"),
    [
        ({"image": "ghcr.io/acme/agent-box:1.2"}, "does not match pattern"),  # a tag alone moves
        ({"image": f"ghcr.io/acme/box@sha256:{'0' * 63}"}, "does not match pattern"),
        ({"image": f"GHCR.io/acme/box@{DIGEST}"}, "does not match pattern"),
        ({"network": "host"}, "must be one of 'none', 'outbound'"),
        ({"network": None}, "network: is required"),
        ({"privileged": True}, "privileged: unknown key"),
        ({"mounts": [{"type": "bind", "target": "/x"}]}, "must be one of 'tmpfs', 'volume'"),
        ({"mounts": [{"type": "tmpfs", "target": "/x", "source": "/Users/me"}]}, "source: unknown key"),
        ({"mounts": [{"type": "tmpfs", "target": "relative"}]}, "does not match pattern"),
        ({"mounts": [{"type": "tmpfs", "target": "/a:b"}]}, "does not match pattern"),
        ({"mounts": [{"type": "volume", "target": "/x"}]}, "a volume names itself (name)"),
        ({"mounts": [{"type": "volume", "name": "c", "target": "/x", "size_mb": 4}]}, "only a tmpfs has a size"),
        ({"mounts": [{"type": "tmpfs", "name": "c", "target": "/x"}]}, "only a volume has a name"),
        ({"mounts": [{"type": "tmpfs", "target": "/x"}, {"type": "tmpfs", "target": "/x"}]}, "listed twice"),
        ({"mounts": [{"type": "tmpfs", "target": "/a/../b"}]}, "not a normalized absolute path"),
        ({"mounts": [{"type": "tmpfs", "target": "/a/"}]}, "not a normalized absolute path"),
        ({"mounts": [{"type": "tmpfs", "target": "//a"}]}, "not a normalized absolute path"),
        ({"mounts": [{"type": "tmpfs", "target": "/"}]}, "is /, or one of /proc, /sys, /dev"),
        ({"mounts": [{"type": "tmpfs", "target": "/proc/self"}]}, "is /, or one of /proc, /sys, /dev"),
        ({"mounts": [{"type": "tmpfs", "target": f"/m{n}"} for n in range(9)]}, "mounts: at most 8 items"),
    ],
)
def test_plugin_environment_out_of_its_shape_is_refused_saying_where(change, said):
    item = {key: value for key, value in {**BOX, **change}.items() if value is not None}
    with pytest.raises(ManifestError) as refused:
        _read(item)
    assert any(said in problem and "environments[0]" in problem for problem in refused.value.problems), refused.value


def test_plugin_environment_names_are_unique_in_the_plugin():
    with pytest.raises(ManifestError, match="'box' is contributed twice"):
        _read(BOX, BOX)


@pytest.mark.parametrize(
    ("change", "said"),
    [
        ({"environment": "sandbox/box", "plugin": "other"}, "is not <plugin>/<name> of plugin 'other'"),
        ({"environment": HOST}, "is not <plugin>/<name>"),
        ({"provider": "host"}, "is a container, not 'host'"),
        ({"version": ""}, "names the plugin's version"),
        ({"title": "two\nlines"}, "title is one line"),
        ({"image": "busybox:latest"}, "is not an image pinned by its digest"),
        ({"network": "host"}, "network is one of none, outbound"),
        ({"mounts": [{"type": "bind", "target": "/x"}]}, "whose type is one of tmpfs, volume"),
        ({"mounts": [{"type": "tmpfs", "target": "/sys"}]}, "is one of /proc, /sys, /dev or under it"),
        ({"mounts": [{"type": "tmpfs", "target": "/a/./b"}]}, "not a normalized absolute path"),
        ({"mounts": [{"type": "volume", "target": "/x", "name": "Bad"}]}, "a volume names itself"),
        ({"mounts": [{"type": "tmpfs", "target": "/x", "size_mb": 0}]}, "size is 1 to 65536 MiB"),
        ({"mounts": [{"type": "tmpfs", "target": "/x", "read_only": "yes"}]}, "read_only is true or false"),
        ({"mounts": [{"type": "tmpfs", "target": "/x"}] * 2}, "mounts each target once"),
        ({"mounts": [{"type": "tmpfs", "target": f"/m{n}"} for n in range(9)]}, "at most 8 mounts"),
    ],
)
def test_plugin_environment_declaration_a_worker_cannot_trust_is_refused(change, said):
    (box,) = container_environments(_read(BOX))
    with pytest.raises(ValueError, match=None) as refused:
        ContainerEnvironment.from_json({**box.to_json(), **change})
    assert said in str(refused.value)
    with pytest.raises(ValueError):
        ContainerEnvironment.from_json(["not", "an", "object"])


def test_plugin_environment_target_problem_names_each_reason():
    assert target_problem("/workspace/cache") is None
    for target in ("", "x", "/", "/a//b", "/a/.", "/dev", "/dev/shm", "/a,b", "/a\nb", None):
        assert target_problem(target), target
