"""A merge queue on GitLab for the Playwright stack: the rows the hub's job hub.merge_queue would leave in a project
after a few passes, written straight to the stack's database, since no GitLab answers the stack.

POST /merge-queue/seed {project, repo, other, admin, notify} writes, for repo ``repo`` of ``project``: a merge request
in its turn (merging, rebased once, judged by both Judges at a high risk), one ready, one blocked at gate 3, one merged
and waiting for its PROD release by hand, one done and drawn for an audit sample that waits for an answer, one done
whose sample ``admin`` answered ok, and the breaker of ``repo`` armed after ``admin`` reset it once; for repo
``other``, a merge request the breaker paused and the breaker tripped by a failed UAT check. With ``notify``, the
member of that login gets the Inbox notices the hub sends its admins: the audit sample, the PROD release and the
tripped breaker. The answer names the ids a test needs: ``requests`` (iid -> queue id), ``sample`` and ``answered``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

SHA = "4f2c9a1d0b7e6f5a4c3b2a1908f7e6d5c4b3a291"
BEFORE = "9d8c7b6a5f4e3d2c1b0a99887766554433221100"
AFTER = "1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d"
FORGE = "https://gitlab.example.org/northwind"

DIMENSIONS = {
    "work": {"verdict": "pass", "reason": "The diff exports invoices as the plan's steps 2 and 3 ask."},
    "description": {"verdict": "pass", "reason": "The description names the export and its columns."},
    "tests": {"verdict": "pass", "reason": "tests/test_export.py covers the new columns."},
    "scope": {"verdict": "pass", "reason": "Nothing beyond the export changed."},
    "safety": {"verdict": "pass", "reason": "No secret, permission or data loss."},
}
RISK_DIMENSIONS = {
    "migration": {"verdict": "pass", "reason": "The migration adds a column with a default and can be undone."},
    "rollback": {"verdict": "pass", "reason": "The image before it reads the table as it was."},
}


def _verdict(model: str, dimensions: dict, run_id=None) -> dict:
    return {"run_id": run_id, "model": model, "agent": "pass", "dimensions": dimensions, "failures": [], "passed": True}


def seed(dsn: str, body: dict) -> dict:
    import psycopg
    from psycopg.types.json import Jsonb

    project, repo, other = body["project"], body.get("repo", "shop"), body.get("other", "billing")
    now = datetime.now(UTC)
    ago = lambda minutes: now - timedelta(minutes=minutes)  # noqa: E731
    with psycopg.connect(dsn, autocommit=False) as conn:
        found = conn.execute("SELECT id FROM projects WHERE name = %s", (project,)).fetchone()
        if found is None:
            raise LookupError(f"no project {project}")
        project_id = found[0]
        user = lambda login: (  # noqa: E731
            conn.execute("SELECT id FROM users WHERE login = %s", (login,)).fetchone() or (None,)
        )[0]
        admin_id = user(body["admin"]) if body.get("admin") else None

        def row(iid: int, state: str, **values) -> int:
            columns = {
                "project_id": project_id,
                "repo": repo,
                "iid": iid,
                "source": "label",
                "target_branch": "main",
                "source_branch": f"evo/change-{iid}",
                "title": None,
                "web_url": f"{FORGE}/{values.get('repo', repo)}/-/merge_requests/{iid}",
                "state": state,
                "head_sha": SHA,
                "created_at": ago(240 - iid),
                "updated_at": ago(60 - iid % 50),
                **values,
            }
            for key in ("verdict", "risk", "steps"):
                if key in columns:
                    columns[key] = Jsonb(columns[key])
            names = ", ".join(columns)
            marks = ", ".join(["%s"] * len(columns))
            query = f"INSERT INTO merge_queue ({names}) VALUES ({marks}) RETURNING id"
            return conn.execute(query, tuple(columns.values())).fetchone()[0]

        def event(queue_id: int, kind: str, minutes: int, **values) -> None:
            detail = Jsonb(values.pop("detail", {}))
            conn.execute(
                "INSERT INTO merge_events (project_id, queue_id, kind, gate, passed, reason, sha, detail, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    project_id,
                    queue_id,
                    kind,
                    values.get("gate"),
                    values.get("passed"),
                    values.get("reason"),
                    values.get("sha"),
                    detail,
                    ago(minutes),
                ),
            )

        ids: dict[int, int] = {}
        ids[41] = row(
            41,
            "merging",
            title="Add the invoice export",
            source="plan",
            plan_id="invoice-export",
            steps=["2", "3"],
            profile="@builder",
            model="claude-sonnet-4-5",
            before_sha=BEFORE,
            rebases=1,
            queued_at=ago(50),
            risk={"high": True, "reasons": [f"{repo}:migrations/0042_invoice.sql is a risk path (migrations and SQL)"]},
            verdict={
                "fingerprint": "ab12",
                "first": _verdict("claude-sonnet-4-5", DIMENSIONS),
                "second": _verdict("claude-opus-4-1", RISK_DIMENSIONS),
            },
        )
        event(ids[41], "queued", 58, reason="labelled by the plan's run")
        for gate, name in enumerate(("repo checked", "pipeline", "repo of the plan step", "score hacking")):
            event(ids[41], "gate", 57 - gate, gate=gate, passed=True, reason=f"{name}: passed", sha=SHA)
        event(ids[41], "judged", 52, passed=True, reason="the first Judge passed every dimension", sha=SHA)
        event(ids[41], "rebased", 20, reason=f"rebased onto main at {BEFORE[:12]}", sha=SHA)
        event(ids[41], "pipeline", 10, reason="the pipeline at the new head is running", sha=SHA)
        ids[42] = row(42, "ready", title="Fix the cart total rounding", source="label", queued_at=ago(30))
        event(ids[42], "queued", 30, reason="every gate passed: it waits for its turn")
        reason = "gate 3 (score hacking): the diff deletes tests/test_cart.py::test_total, which held it for a person"
        ids[43] = row(43, "blocked", title="Speed up the cart page", source="task", reason=reason)
        event(ids[43], "gate", 25, gate=3, passed=False, reason=reason, sha=SHA)
        release = "its PROD release waits for an admin to approve it (prod_release manual)"
        ids[40] = row(
            40,
            "releasing",
            title="Show the invoice number on receipts",
            source="builder",
            profile="@builder",
            reason=release,
            merged_at=ago(90),
            before_sha=BEFORE,
            after_sha=AFTER,
            uat_result="passed",
            uat_at=ago(70),
        )
        event(ids[40], "merged", 90, passed=True, reason=f"merged into main as {AFTER[:12]}", sha=AFTER)
        event(ids[40], "uat_check", 70, passed=True, reason="the job uat-check passed", sha=AFTER)
        ids[39] = row(
            39,
            "done",
            title="Rename the export menu",
            merged_at=ago(200),
            after_sha=AFTER,
            uat_result="passed",
            prod_result="passed",
        )
        ids[37] = row(
            37,
            "done",
            title="Log the export size",
            merged_at=ago(400),
            after_sha=AFTER,
            uat_result="passed",
            prod_result="passed",
        )
        paused = "waits: the repo's breaker tripped"
        ids[7] = row(7, "paused", repo=other, title="Retry failed card charges", reason=paused, queued_at=ago(15))

        def sample(queue_id: int, result=None) -> int:
            answered = admin_id if result else None
            return conn.execute(
                "INSERT INTO merge_samples (project_id, queue_id, result, note, answered_by, answered_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                (project_id, queue_id, result, None, answered, now if result else None),
            ).fetchone()[0]

        waiting, answered = sample(ids[39]), sample(ids[37], "ok")
        event(ids[39], "sampled", 199, reason="drawn for an audit sample (audit.sample_rate 0.1)", sha=AFTER)
        conn.execute(
            "INSERT INTO merge_breakers (project_id, repo, state, trips, resumed_by, resumed_at) "
            "VALUES (%s, %s, 'armed', 1, %s, %s)",
            (project_id, repo, admin_id, ago(600)),
        )
        tripped = "the UAT check of !6 failed after a retry: the job uat-check exited 1"
        conn.execute(
            "INSERT INTO merge_breakers (project_id, repo, state, cause, reason, tripped_at, trips) "
            "VALUES (%s, %s, 'tripped', 'uat_check', %s, %s, 1)",
            (project_id, other, tripped, ago(12)),
        )
        if body.get("notify"):
            notified = user(body["notify"])
            link = f"/p/{project}/merge-queue"
            notices = [
                (
                    "merge_default_branch",
                    f"Audit sample: check !39 of {repo}, merged into main",
                    {
                        "repo": repo,
                        "branch": "main",
                        "commits": [AFTER],
                        "queue_id": ids[39],
                        "ask": "sample",
                        "sample_id": waiting,
                    },
                ),
                (
                    "merge_default_branch",
                    f"PROD release of {repo} waits for an admin: !40, merged into main",
                    {"repo": repo, "branch": "main", "commits": [AFTER], "queue_id": ids[40], "ask": "release"},
                ),
                (
                    "merge_paused",
                    f"The merge queue of {other} stopped: a UAT check failed",
                    {"repo": other, "branch": "main", "cause": "uat_check", "causes": ["uat_check"], "paused": [7]},
                ),
            ]
            for notice_kind, title, details in notices:
                conn.execute(
                    "INSERT INTO notifications (user_id, kind, notice_kind, project_id, title, body, details, link) "
                    "VALUES (%s, 'notice', %s, %s, %s, %s, %s, %s)",
                    (notified, notice_kind, project_id, title, title + ".", Jsonb(details), link),
                )
        conn.commit()
    return {"requests": {str(iid): queue_id for iid, queue_id in ids.items()}, "sample": waiting, "answered": answered}
