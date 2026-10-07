"""datetime.fromisoformat reads every ISO 8601 timestamp the package exchanges, as it does from Python 3.11 on.

The hub writes each timestamp with a trailing ``Z`` and up to six digits of a second; the package reads them with
``datetime.fromisoformat`` and needs Python 3.11 or later for that (Python 3.10 rejected both).
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # what the hub writes
        ("2026-10-04T15:59:41.219905Z", datetime(2026, 10, 4, 15, 59, 41, 219905, tzinfo=UTC)),
        ("2026-10-04T15:59:41Z", datetime(2026, 10, 4, 15, 59, 41, tzinfo=UTC)),
        ("2026-10-04T15:59Z", datetime(2026, 10, 4, 15, 59, tzinfo=UTC)),
        ("2026-10-04 15:59:41.1Z", datetime(2026, 10, 4, 15, 59, 41, 100000, tzinfo=UTC)),
        ("2026-10-04T15:59:41.12+00:00", datetime(2026, 10, 4, 15, 59, 41, 120000, tzinfo=UTC)),
        ("2026-10-04T15:59:41.1234567891Z", datetime(2026, 10, 4, 15, 59, 41, 123456, tzinfo=UTC)),
        # offsets, and values without one
        ("2026-10-04T15:59:41.219905+00:00", datetime(2026, 10, 4, 15, 59, 41, 219905, tzinfo=UTC)),
        (
            "2026-10-04T22:59:41.219+07:00",
            datetime(2026, 10, 4, 22, 59, 41, 219000, tzinfo=timezone(timedelta(hours=7))),
        ),
        ("2026-10-04T15:59:41", datetime(2026, 10, 4, 15, 59, 41)),
        ("2026-10-04", datetime(2026, 10, 4)),
    ],
)
def test_fromisoformat_reads_a_trailing_z_and_any_fraction(text, expected):
    parsed = datetime.fromisoformat(text)
    assert parsed == expected
    assert parsed.utcoffset() == expected.utcoffset()  # a naive value stays naive


@pytest.mark.parametrize("text", ["", "Z", "yesterday", "2026-13-01T00:00:00Z", "2026-10-04T15:59:41z", "1700000000"])
def test_fromisoformat_rejects_what_is_not_a_timestamp(text):
    with pytest.raises(ValueError):
        datetime.fromisoformat(text)
