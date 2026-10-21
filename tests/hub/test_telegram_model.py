"""What the hub keeps of the Telegram channel's model for itself (``evo_agents.hub.telegram``): the one-time link
codes of members' channels, what may reach a channel outside the hub (``restricted``, ``restricted_for``), and the
absolute URL of a page. No database, no network.

The model of the bot (commands, the data of buttons, the messages of a decision, a proposal and a notice) is the core
plugin telegram's, in the repository evo-agents-core-plugins, whose ``plugin_tests/test_telegram_model.py`` checks it.
"""

import re

import pytest

from evo_agents.hub import telegram as hub_telegram


def test_a_link_code_is_32_characters_a_start_parameter_takes_and_the_hub_keeps_its_hash():
    code = hub_telegram.new_code()
    assert len(code) == hub_telegram.LINK_CODE_CHARS == 32 and re.fullmatch(r"[A-Za-z0-9_-]{32}", code)
    assert hub_telegram.new_code() != code
    digest = hub_telegram.code_hash(code)
    assert re.fullmatch(r"[0-9a-f]{64}", digest) and code not in digest
    assert hub_telegram.LINK_SECONDS == 600


LEVELS = ["public", "internal", "customer", "secret"]  # evo_agents.kg.policy.DEFAULT_LEVELS
ANY = ["any"]
DOMESTIC = ["any", "domestic-only"]


@pytest.mark.parametrize(
    ("clearance", "locations", "restricted"),
    [
        ({"level": "internal"}, ANY, False),
        ({"level": "internal", "location": "any"}, DOMESTIC, False),
        ({"level": "customer", "location": "any"}, DOMESTIC, True),
        ({"level": "internal", "location": "domestic-only"}, DOMESTIC, True),
        ({"level": "customer", "location": "domestic-only"}, DOMESTIC, True),
        (None, ANY, True),  # no hub sink: fail closed
    ],
)
def test_a_project_cleared_for_customer_or_domestic_only_is_restricted(clearance, locations, restricted):
    assert hub_telegram.restricted(clearance, LEVELS, locations) is restricted


@pytest.mark.parametrize(
    ("clearance", "levels", "locations"),
    [
        ({"level": "secret", "location": "any"}, LEVELS, DOMESTIC),  # above customer
        ({"level": "secret"}, LEVELS, ANY),
        ({"level": "top-secret"}, [*LEVELS, "top-secret"], ANY),  # a custom level above customer
        ({"location": "any"}, LEVELS, DOMESTIC),  # no level
        ({"level": None}, LEVELS, ANY),
        ({"level": "galaxy"}, LEVELS, ANY),  # a level the ladder lacks
        ({"level": "Internal"}, LEVELS, ANY),
        ({"level": "internal", "location": "moon"}, LEVELS, DOMESTIC),  # a location the ladder lacks
        ({"level": "internal"}, LEVELS, DOMESTIC),  # no location: it may receive any, domestic-only included
        ({"level": "internal", "location": "on-premises"}, LEVELS, [*DOMESTIC, "on-premises"]),  # stricter still
        ({"level": "confidential"}, ["public", "confidential"], ANY),  # no customer: above the lowest is restricted
        ({"level": "internal", "location": "eu-only"}, LEVELS, ["any", "eu-only"]),  # no domestic-only: the same
        ({"level": "internal"}, None, ANY),  # no ladder at all
        ({"level": "internal"}, [], ANY),
        ("customer", LEVELS, ANY),  # not a clearance
        ([], LEVELS, ANY),
    ],
)
def test_restricted_compares_ranks_on_the_projects_ladder_and_fails_closed(clearance, levels, locations):
    assert hub_telegram.restricted(clearance, levels, locations) is True


@pytest.mark.parametrize(
    ("clearance", "levels", "locations"),
    [
        ({"level": "public"}, LEVELS, ANY),
        ({"level": "internal", "location": "any"}, LEVELS, DOMESTIC),
        ({"level": "public"}, ["public", "confidential"], ANY),  # the lowest rung of a ladder without customer
        ({"level": "internal"}, ["public", "internal", "restricted", "customer"], ANY),
    ],
)
def test_below_customer_and_domestic_only_on_the_ladder_is_not_restricted(clearance, levels, locations):
    assert hub_telegram.restricted(clearance, levels, locations) is False


# A channel of any clearance (restricted_for, the API of core plugins' restricted and prepare)


@pytest.mark.parametrize(
    ("clearance", "level", "location", "kept"),
    [
        ({"level": "internal", "location": "any"}, "internal", None, False),
        ({"level": "public"}, "internal", "any", True),  # a sink without a location may receive any: above any
        ({"level": "internal", "location": "domestic-only"}, "internal", None, False),  # None receives any location
        ({"level": "internal", "location": "domestic-only"}, "internal", "any", True),
        ({"level": "customer", "location": "any"}, "internal", None, True),  # above the channel's level
        ({"level": "customer", "location": "any"}, "customer", None, False),
        ({"level": "secret"}, "secret", None, False),
        (None, "secret", None, True),  # no hub sink
        ({"location": "any"}, "secret", None, True),  # a sink without a level
        ({"level": "galaxy"}, "secret", None, True),  # a level the ladder lacks
        ({"level": "internal"}, "galaxy", None, True),  # a channel's level the ladder lacks
        ({"level": "internal", "location": "moon"}, "internal", None, True),
        ({"level": "internal", "location": "any"}, "internal", "moon", True),
    ],
)
def test_restricted_for_keeps_a_sink_above_the_channels_clearance_and_fails_closed(clearance, level, location, kept):
    assert hub_telegram.restricted_for(clearance, LEVELS, DOMESTIC, level, location) is kept


@pytest.mark.parametrize(
    ("clearance", "levels", "level", "location", "kept"),
    [
        ({"level": "internal", "location": "any"}, LEVELS, "customer", "domestic-only", False),
        ({"level": "customer", "location": "any"}, LEVELS, "customer", "domestic-only", True),  # at the level
        ({"level": "internal", "location": "domestic-only"}, LEVELS, "customer", "domestic-only", True),
        ({"level": "customer"}, LEVELS, "secret", None, False),  # None: any location
        ({"level": "secret"}, LEVELS, "secret", None, True),
        ({"level": "internal", "location": "moon"}, LEVELS, "secret", None, True),  # a location the ladder lacks
        ({"level": "public"}, ["public", "confidential"], "customer", None, False),  # no customer: the lowest rung
        ({"level": "confidential"}, ["public", "confidential"], "customer", None, True),
        (None, LEVELS, "secret", None, True),  # no hub sink
    ],
)
def test_restricted_for_below_keeps_a_sink_at_or_above_the_channels_clearance(clearance, levels, level, location, kept):
    """``Clearance(level, location, below=True)`` of the API of core plugins: what sits under the rungs, not at them;
    ``restricted`` is customer and domestic-only, below."""
    assert hub_telegram.restricted_for(clearance, levels, DOMESTIC, level, location, below=True) is kept


def test_web_url_needs_the_public_url_and_a_path_of_the_hub():
    assert hub_telegram.web_url("https://hub.test/", "/inbox?decision=1") == "https://hub.test/inbox?decision=1"
    assert hub_telegram.web_url(None, "/inbox") is None
    assert hub_telegram.web_url("https://hub.test", "//evil.example") is None
    assert hub_telegram.web_url("https://hub.test", "https://evil.example") is None
