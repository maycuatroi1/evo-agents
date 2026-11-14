"""The band of a worker's free disk (``ops_events.disk_band``): low under its reserve, and once low, low until it has
``DISK_HEADROOM_GB`` over it, so a drive that hovers at the reserve line writes one ``worker.health``, not one per
crossing (Curator findings #164, #166)."""

from __future__ import annotations

from evo_agents.hub import ops_events


def _report(free: float, reserve: float = 20.0) -> dict:
    return {"disk": {"free_gb": free, "free_pct": 5.0, "reserve_gb": reserve}, "logins": {"claude-code": "ok"}}


def test_a_low_disk_stays_low_until_the_headroom_and_writes_no_flap():
    stored = None
    bands, states = [], []
    for free in (30.0, 19.0, 21.0, 19.5, 24.9, 25.0, 21.0, 19.0):
        before = stored
        stored = ops_events.with_band(_report(free), before)
        bands.append(stored["disk"]["band"])
        states.append(ops_events.health_state(stored) != ops_events.health_state(before))
    assert bands == ["ok", "low", "low", "low", "low", "ok", "ok", "low"]
    assert states == [True, True, False, False, False, True, False, True], "one event per real change of band"


def test_a_report_with_no_band_stored_reads_as_before_and_one_with_no_disk_has_none():
    assert ops_events.disk_band(_report(19.0)) == "low" and ops_events.disk_band(_report(21.0)) == "ok"
    assert ops_events.disk_band({"logins": {}}) is None
    assert ops_events.with_band({"logins": {}}, None) == {"logins": {}}
    assert ops_events.with_band(None, _report(10.0)) is None
