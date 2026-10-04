"""parse_iso reads on Python 3.10 what datetime.fromisoformat reads from 3.11 on."""

import sys
from datetime import datetime, timedelta, timezone

import pytest

from evo_agents.isotime import parse_iso

UTC = timezone.utc


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # what the hub writes, which 3.10's fromisoformat rejects
        ("2026-10-04T15:59:41.219905Z", datetime(2026, 10, 4, 15, 59, 41, 219905, tzinfo=UTC)),
        ("2026-10-04T15:59:41Z", datetime(2026, 10, 4, 15, 59, 41, tzinfo=UTC)),
        ("2026-10-04T15:59Z", datetime(2026, 10, 4, 15, 59, tzinfo=UTC)),
        ("2026-10-04 15:59:41.1Z", datetime(2026, 10, 4, 15, 59, 41, 100000, tzinfo=UTC)),
        ("2026-10-04T15:59:41.12+00:00", datetime(2026, 10, 4, 15, 59, 41, 120000, tzinfo=UTC)),
        ("2026-10-04T15:59:41.1234567891Z", datetime(2026, 10, 4, 15, 59, 41, 123456, tzinfo=UTC)),
        # what 3.10 read already
        ("2026-10-04T15:59:41.219905+00:00", datetime(2026, 10, 4, 15, 59, 41, 219905, tzinfo=UTC)),
        (
            "2026-10-04T22:59:41.219+07:00",
            datetime(2026, 10, 4, 22, 59, 41, 219000, tzinfo=timezone(timedelta(hours=7))),
        ),
        ("2026-10-04T15:59:41", datetime(2026, 10, 4, 15, 59, 41)),
        ("2026-10-04", datetime(2026, 10, 4)),
    ],
)
def test_parse_iso_reads_a_trailing_z_and_any_fraction(text, expected):
    parsed = parse_iso(text)
    assert parsed == expected
    assert parsed.utcoffset() == expected.utcoffset()  # a naive value stays naive
    if sys.version_info >= (3, 11):
        assert parsed == datetime.fromisoformat(text)


@pytest.mark.parametrize("text", ["", "Z", "yesterday", "2026-13-01T00:00:00Z", "2026-10-04T15:59:41z", "1700000000"])
def test_parse_iso_rejects_what_fromisoformat_rejects(text):
    with pytest.raises(ValueError):
        parse_iso(text)
