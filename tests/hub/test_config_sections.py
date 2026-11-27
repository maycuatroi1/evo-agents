"""The named sections of the hub's configuration: together they hold each field of HubConfig once, and each section
reads what the flat configuration holds."""

from __future__ import annotations

from collections import Counter
from dataclasses import fields, replace
from pathlib import Path

from evo_agents.hub.config import HubConfig
from evo_agents.hub.config_sections import SECTIONS, Part


def test_the_sections_hold_each_field_of_the_configuration_once() -> None:
    held = Counter(name for section in SECTIONS for name in section.names())
    assert sorted(held) == sorted(field.name for field in fields(HubConfig)), "a field outside every section, or none"
    assert max(held.values()) == 1, "a field in two sections"


def test_each_section_is_an_attribute_of_the_configuration() -> None:
    parts = {part.section for part in vars(HubConfig).values() if isinstance(part, Part)}
    assert parts == set(SECTIONS)


def test_a_section_reads_the_configuration_and_says_what_it_misses() -> None:
    config = replace(HubConfig(dsn="postgresql://hub@db/hub", data_dir=Path("/tmp/hub")), telegram_bot_token="1:x")
    assert config.telegram.telegram_bot_token == "1:x"
    assert config.telegram.missing() == config.telegram_missing() == ["EVO_HUB_TELEGRAM_WEBHOOK_SECRET"]
    assert config.database.dsn == "postgresql://hub@db/hub" and config.server.data_dir == Path("/tmp/hub")
    assert config.blob_store_missing() == [
        "EVO_HUB_S3_ENDPOINT",
        "EVO_HUB_S3_BUCKET",
        "EVO_HUB_S3_ACCESS_KEY_ID",
        "EVO_HUB_S3_SECRET_ACCESS_KEY",
    ]
    assert config.webpush_missing() == ["EVO_HUB_VAPID_PRIVATE_KEY", "EVO_HUB_VAPID_SUBJECT"]
    assert replace(config, public_url="https://hub").webpush_missing() == ["EVO_HUB_VAPID_PRIVATE_KEY"]
