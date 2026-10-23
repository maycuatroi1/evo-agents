"""The weeks of a probe for the Playwright stack, as the weekly probe runs leave them in curator_probe_runs.

POST /probes/seed {probe_id, weeks: [{ago, on: [passed, ...], off: [passed, ...]}]} makes the probe's change merged
two weeks ago and the probe three weeks old, so the report lists it in each week since, then writes one done probe run
per week and per runtime and model of the charter's probes (``probe_runs._models``): ``ago`` weeks before the current
ISO week in the charter's time zone, each arm's sessions passing or not as listed. No worker runs probe sessions in the
stack, so the job that queues and reports them is not played here (tests/hub/test_probe_runs.py covers it); this gives
the report and the probe card the rows they read. The answer is ``{weeks: [<Monday>, ...]}``.
"""

from __future__ import annotations

from datetime import timedelta


def seed(dsn: str, body: dict) -> dict:
    import psycopg
    from psycopg.types.json import Jsonb

    from evo_agents.hub import probes
    from evo_agents.hub.server.probe_runs import _models

    probe_id = int(body["probe_id"])
    with psycopg.connect(dsn) as conn:
        probe = conn.execute("SELECT project_id, change_id FROM curator_probes WHERE id = %s", (probe_id,)).fetchone()
        if probe is None or probe[1] is None:
            raise LookupError(f"no probe {probe_id} on a change")
        project_id, change_id = probe
        conn.execute(
            "UPDATE curator_changes SET state = 'merged', merged_at = now() - interval '15 days' WHERE id = %s",
            (change_id,),
        )
        conn.execute("UPDATE curator_probes SET created_at = now() - interval '21 days' WHERE id = %s", (probe_id,))
        charter = conn.execute(
            "SELECT body FROM charters WHERE project_id = %s ORDER BY revision DESC LIMIT 1", (project_id,)
        ).fetchone()
        charter_body = dict(charter[0]) if charter is not None else {}
        zone = (charter_body.get("window") or {}).get("timezone") or "UTC"
        today = conn.execute("SELECT (timezone(%s, now()))::date", (zone,)).fetchone()[0]
        current = probes.week_of(today)
        written = []
        for item in body["weeks"]:
            week = current - timedelta(days=7 * int(item["ago"]))
            sessions = [
                {"arm": arm, "index": index, "passed": bool(passed)}
                for arm in ("on", "off")
                for index, passed in enumerate(item[arm], start=1)
            ]
            for runtime, model in _models(charter_body):
                conn.execute(
                    "INSERT INTO curator_probe_runs (project_id, probe_id, change_id, week, runtime, model, k, state, "
                    "sessions) VALUES (%s, %s, %s, %s, %s, %s, %s, 'done', %s) "
                    "ON CONFLICT (probe_id, week, runtime, model) DO UPDATE SET state = 'done', k = EXCLUDED.k, "
                    "sessions = EXCLUDED.sessions",
                    (project_id, probe_id, change_id, week, runtime, model, len(item["on"]), Jsonb(sessions)),
                )
            written.append(week.isoformat())
        conn.commit()
    return {"weeks": written}
