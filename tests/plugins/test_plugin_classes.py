"""The plugins and the core plugins as classes (plan oop-redesign, criterion a10): a manifest is checked by a validator
of each extension point, held in one registry; each kind of proposal of @builder is a subclass of one Template Method
base; the runtimes and the environments of plugins share one class of plugin name and of declaration, whose JSON is
the one the hub has always sent; the host backs off under a ``RetryPolicy``."""

from __future__ import annotations

from evo_agents.core_plugins.builder import proposals
from evo_agents.core_plugins.builder.kinds import ProposalKind
from evo_agents.infra.retry import RetryPolicy
from evo_agents.plugins import host
from evo_agents.plugins.agent_packs import split_pack
from evo_agents.plugins.compat import POINT_VERSIONS
from evo_agents.plugins.manifest import (
    CORE_ONLY_POINTS,
    SHARED,
    VALIDATORS,
    PointValidator,
    read_manifest,
    validator_of,
)
from evo_agents.plugins.names import PluginDeclaration, PluginName
from evo_agents.plugins.run_environments import ContainerEnvironment, container_environments
from evo_agents.plugins.runtimes import AcpRuntime, acp_runtimes
from tests.plugins.samples import manifest

IMAGE = "ghcr.io/example/box@sha256:" + "a" * 64


def test_plugin_classes_each_extension_point_has_one_validator_in_the_registry():
    assert set(VALIDATORS) <= set(POINT_VERSIONS)
    assert all(isinstance(validator, PointValidator) for validator in VALIDATORS.values())
    assert validator_of("no-such-point") is SHARED
    assert set(CORE_ONLY_POINTS) == {point for point, validator in VALIDATORS.items() if validator.core_only}


def test_plugin_classes_each_kind_of_builder_proposal_is_a_proposal_kind():
    assert set(proposals.KINDS) == set(proposals.PROPOSAL_KINDS)
    for kind in proposals.KINDS.values():
        assert isinstance(kind, ProposalKind)
        for step in ("locate", "check", "apply", "roll_back", "write", "body"):
            assert callable(getattr(kind, step))


def test_plugin_classes_a_plugin_name_reads_and_writes_plugin_slash_name():
    assert PluginName.of("sandbox", "fake") == "sandbox/fake" == str(PluginName("sandbox", "fake"))
    assert PluginName.parse("sandbox/fake") == PluginName("sandbox", "fake")
    assert PluginName.plugin_of("sandbox/fake") == "sandbox"
    for other in ("claude-code", "host", "sandbox/", "/fake", "Sandbox/fake", None, 3):
        assert not PluginName.matches(other) and PluginName.plugin_of(other) is None
    assert split_pack("sandbox/reviewer") == ("sandbox", "reviewer")


def test_plugin_classes_runtimes_and_environments_share_one_declaration_whose_json_is_unchanged():
    read = read_manifest(
        manifest(
            version="0.2.0",
            contributes={
                "runtimes": [{"name": "fake", "acp": {"command": ["fake-acp"]}, "models": ["small"]}],
                "environments": [{"name": "box", "image": IMAGE, "network": "none"}],
            },
        )
    )
    [runtime], [environment] = acp_runtimes(read), container_environments(read)
    assert isinstance(runtime, PluginDeclaration) and isinstance(environment, PluginDeclaration)
    assert runtime.to_json() == {
        "runtime": "sandbox/fake",
        "plugin": "sandbox",
        "version": "0.2.0",
        "title": "sandbox/fake",
        "command": ["fake-acp"],
        "models": ["small"],
        "secrets": [],
    }
    assert environment.to_json() == {
        "environment": "sandbox/box",
        "plugin": "sandbox",
        "version": "0.2.0",
        "title": "sandbox/box",
        "provider": "container",
        "image": IMAGE,
        "network": "none",
        "mounts": [],
    }
    assert AcpRuntime.from_json(runtime.to_json()) == runtime
    assert ContainerEnvironment.from_json(environment.to_json()) == environment


def test_plugin_classes_the_host_backs_off_under_a_retry_policy():
    assert isinstance(host.RESTARTS, RetryPolicy) and host.RESTARTS.tries is None
    delays = [host.RESTARTS.delay(n) for n in range(len(host.BACKOFF) + 2)]
    assert delays == [*host.BACKOFF, host.MAX_BACKOFF, host.MAX_BACKOFF]
