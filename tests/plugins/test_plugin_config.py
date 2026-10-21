"""A plugin's config as layers (``evo_agents.plugins.config``): what one layer may set, checked against the plugin's
config schema without ever repeating a secret; the effective config of a stack of layers, each field from the highest
layer that sets it, with what is wrong with it; and a digest that follows the values and the sealed secrets, never a
secret's value."""

from __future__ import annotations

from evo_agents.plugins import config
from evo_agents.plugins.config import Layer, check_values, merge
from evo_agents.plugins.manifest import PERMISSION_TEXT, PERMISSIONS

SCHEMA = {
    "type": "object",
    "properties": {
        "greeting": {"type": "string", "default": "hello", "maxLength": 20},
        "retries": {"type": "integer", "minimum": 0, "maximum": 5, "default": 1},
        "channel": {"enum": ["general", "ops"]},
        "token": {"type": "string", "x-secret": True, "pattern": "^tok-"},
        "options": {"type": "object"},
    },
    "required": ["greeting", "token"],
}


def test_plugin_config_fields_defaults_and_permission_texts():
    assert config.fields(SCHEMA) == {
        "greeting": False,
        "retries": False,
        "channel": False,
        "token": True,
        "options": False,
    }
    assert config.defaults(SCHEMA) == {"greeting": "hello", "retries": 1}
    assert config.fields(None) == {} and config.defaults(None) == {}
    assert set(PERMISSION_TEXT) == set(PERMISSIONS)


def test_plugin_config_check_values_names_each_field_and_never_repeats_a_secret():
    assert check_values(SCHEMA, {"greeting": "hi", "retries": 3, "token": "tok-abc"}) == []
    assert check_values(SCHEMA, {}) == []
    problems = check_values(SCHEMA, {"retries": 9, "channel": "random", "colour": "red", "token": "nope-1234"})
    assert "colour: not a field of the plugin's config (its fields: channel, greeting, options, retries, token)" in (
        problems
    )
    assert "retries: must be <= 5" in problems
    assert any(p.startswith("channel: must be one of") for p in problems)
    assert "token: the value does not fit the field's schema (the hub does not repeat a secret's value)" in problems
    assert not any("nope-1234" in problem for problem in problems)
    assert check_values(SCHEMA, {"token": 12}) == ["token: a secret's value is a string that is not empty"]
    assert check_values(SCHEMA, {"token": "tok-1"}, secrets=False) == [
        "token: a secret, which a run's layer may not set; set it on the hub or the project"
    ]
    assert check_values(None, {"greeting": "hi"}) == [
        "the plugin declares no config: its manifest has no config schema"
    ]
    big = check_values(SCHEMA, {"options": {"blob": "x" * (config.MAX_LAYER_BYTES + 1)}})
    assert big == ["the values of one layer are at most 64 KiB as JSON"]
    odd = {"type": "object", "properties": {"when": {"type": "date"}}}
    assert check_values(odd, {"when": "today"})[0].startswith("the config schema holds what the hub cannot check")


def test_plugin_config_merge_takes_each_field_from_the_highest_layer_that_sets_it():
    layers = [
        Layer("instance", {"greeting": "hub", "retries": 2}, {"token": "fp-instance"}, 3),
        Layer("project", {"greeting": "project"}, {}, 1),
        Layer("agent", {"channel": "ops"}, {"token": "fp-agent"}, 2),
        Layer("run", {"retries": 0}),
    ]
    effective = merge(SCHEMA, layers)
    assert effective.problems == ()
    found = {name: (item.value, item.layer, item.version) for name, item in effective.fields.items()}
    assert found == {
        "greeting": ("project", "project", 1),
        "retries": (0, "run", None),
        "channel": ("ops", "agent", 2),
        "token": (None, "agent", 2),
    }
    assert effective.fields["token"].secret and effective.fields["token"].fingerprint == "fp-agent"
    assert effective.digest.startswith("sha256:") and len(effective.digest) == len("sha256:") + 64

    bare = merge(SCHEMA, [])
    assert {name: item.layer for name, item in bare.fields.items()} == {"greeting": "default", "retries": "default"}
    assert bare.problems == ("token: required, and no layer sets it",)


def test_plugin_config_digest_follows_values_and_sealed_secrets_and_the_problems_after_a_schema_change():
    base = [Layer("project", {"greeting": "hi"}, {"token": "fp-1"}, 1)]
    same = merge(SCHEMA, [Layer("project", {"greeting": "hi"}, {"token": "fp-1"}, 7)])
    assert merge(SCHEMA, base).digest == same.digest  # the version that set a value is no part of it
    assert merge(SCHEMA, [Layer("project", {"greeting": "hi"}, {"token": "fp-2"}, 1)]).digest != same.digest
    assert merge(SCHEMA, [Layer("project", {"greeting": "ho"}, {"token": "fp-1"}, 1)]).digest != same.digest

    newer = {"type": "object", "properties": {"greeting": {"type": "integer"}, "token": {"type": "string"}}}
    changed = merge(newer, base)
    assert "token: set at project v1, and the field is not a secret now" in changed.problems
    assert "greeting: expected integer, got str (as the layers set it)" in changed.problems
    gone = merge({"type": "object", "properties": {}}, base)
    assert "greeting: set at project v1, and the config has no such field now" in gone.problems
    assert gone.fields == {}
