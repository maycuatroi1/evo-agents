"""The rows of the core plugins crm and marketing for the Playwright stack, as their jobs would leave them after a few
weeks, written straight to the stack's database: the stack runs no job to find contacts on GitHub, no gate and no
channel to post on.

POST /growth/seed {project, owner, spent, long} writes, for a project that enabled both plugins and whose admin is
``owner``:

- crm: four contacts labelled with the lowest level of the project's ladder, one per stage (lead, invited,
  activated, retained), each with the stages it reached and a touchpoint from where it came; ``long`` makes the lead's
  login 39 characters, the longest GitHub takes.
- marketing: four channel accounts: github (project, autonomous), x (project, its breaker open on two failures),
  linkedin (project, browser, its login session expired) and facebook_group (``owner``'s, draft_only); a post of each
  outcome of the gate (posted, a facebook_group draft, failed with the deny-list's reason, proposed); and the month's
  budget with ``spent`` dollars spent (12.5 by default; the default cap is 50, so 50 spends it).

The answer is ``{contacts: [logins], channels: {channel: id}, posts: {state: id}}``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

STAGES = ("lead", "invited", "activated", "retained")
LONG_LOGIN = "a-very-long-github-login-that-never-wrap"[:39]


def seed(dsn: str, body: dict) -> dict:
    import psycopg
    from psycopg.types.json import Jsonb

    project, owner = body["project"], body["owner"]
    spent = float(body.get("spent", 12.5))
    now = datetime.now(UTC)
    ago = lambda days: now - timedelta(days=days)  # noqa: E731
    with psycopg.connect(dsn, autocommit=False) as conn:
        found = conn.execute("SELECT id, levels FROM projects WHERE name = %s", (project,)).fetchone()
        user = conn.execute("SELECT id FROM users WHERE login = %s", (owner,)).fetchone()
        if found is None or user is None:
            raise LookupError(f"no project {project} or no user {owner}")
        pid, uid = found[0], user[0]
        # The lowest level of the project's ladder, so every member reads the contacts (the job writes the project's
        # default label, which a project registered without one makes its highest).
        label = Jsonb({"level": found[1][0], "projects": [project]})

        lead = LONG_LOGIN if body.get("long") else "ana-lead"
        people = [
            (lead, "lead", "issue"),
            ("binh-invited", "invited", "invite_request"),
            ("chi-active", "activated", "invite_link"),
            ("dung-kept", "retained", "grant"),
        ]
        for index, (login, stage, source) in enumerate(people):
            first = ago(20 - index)
            contact = conn.execute(
                "INSERT INTO crm_contacts (project_id, login, profile_url, source, stage, label, first_seen_at, stage_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (pid, login, f"https://github.com/{login}", source, stage, label, first, ago(10 - index)),
            ).fetchone()[0]
            for reached in STAGES[: STAGES.index(stage) + 1]:
                conn.execute(
                    "INSERT INTO crm_stage_history (contact_id, stage, reached_at) VALUES (%s, %s, %s)",
                    (contact, reached, first + timedelta(days=STAGES.index(reached))),
                )
            conn.execute(
                "INSERT INTO crm_touchpoints (contact_id, kind, ref, url, happened_at) VALUES (%s, %s, %s, %s, %s)",
                (
                    contact,
                    source,
                    f"{source}:{index + 1}",
                    f"https://github.com/acme/app/issues/{index + 1}" if source == "issue" else None,
                    first,
                ),
            )

        def channel(name: str, account: str, kind: str, **extra) -> int:
            columns = {
                "project_id": pid,
                "channel": name,
                "account": account,
                "account_kind": kind,
                "created_by": uid,
                **extra,
            }
            names = ", ".join(columns)
            marks = ", ".join(["%s"] * len(columns))
            return conn.execute(
                f"INSERT INTO marketing_channels ({names}) VALUES ({marks}) RETURNING id", tuple(columns.values())
            ).fetchone()[0]

        channels = {
            "github": channel("github", "acme/app", "project", mode="autonomous", autonomous_since=ago(14)),
            "x": channel(
                "x",
                "acme",
                "project",
                mode="autonomous",
                autonomous_since=ago(14),
                paused_at=ago(1),
                paused_reason="Two failures in a row",
            ),
            "linkedin": channel("linkedin", "acme-page", "project", adapter="browser", session_state="stale"),
            "facebook_group": channel(
                "facebook_group",
                f"{owner}-group",
                "personal",
                owner_id=uid,
                adapter="browser",
                target="https://www.facebook.com/groups/devs",
            ),
        }

        def post(name: str, state: str, text: str, **extra) -> int:
            columns = {
                "project_id": pid,
                "channel_id": channels[name],
                "channel": name,
                "account": "",
                "body": text,
                "state": state,
                "proposed_by": uid,
                **extra,
            }
            columns["account"] = conn.execute(
                "SELECT account FROM marketing_channels WHERE id = %s", (channels[name],)
            ).fetchone()[0]
            names = ", ".join(columns)
            marks = ", ".join(["%s"] * len(columns))
            return conn.execute(
                f"INSERT INTO marketing_posts ({names}) VALUES ({marks}) RETURNING id", tuple(columns.values())
            ).fetchone()[0]

        docs = "https://evo-agents.dev/docs"
        posts = {
            "posted": post(
                "github",
                "posted",
                f"Release notes of 0.38: plans run in parallel. Docs: {docs}",
                posted_url="https://github.com/acme/app/discussions/7",
                posted_at=ago(2),
                cost_usd=0.02,
                links=Jsonb([docs]),
            ),
            "draft": post(
                "facebook_group",
                "draft",
                f"Chào cả nhóm, bản 0.38 của evo-agents chạy kế hoạch song song. Tài liệu: {docs}",
                links=Jsonb([docs]),
            ),
            "failed": post(
                "x", "failed", "Better than the other tool, trust us.", reason="deny-list: a comparison with no source"
            ),
            "proposed": post("github", "proposed", f"How the gate checks a post before it goes out: {docs}"),
        }
        month = f"{now.year:04d}-{now.month:02d}"
        conn.execute(
            "INSERT INTO marketing_budget (project_id, month, reserved_usd, spent_usd) VALUES (%s, %s, 0, %s) "
            "ON CONFLICT (project_id, month) DO UPDATE SET spent_usd = EXCLUDED.spent_usd",
            (pid, month, spent),
        )
        conn.commit()
    return {"contacts": [login for login, _, _ in people], "channels": channels, "posts": posts}
