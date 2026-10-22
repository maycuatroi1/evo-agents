"""A notice of an isolated plugin for the Playwright stack, as ``server.plugin_notify`` leaves it in the Inbox.

POST /plugin-notice/seed {project, login, plugin, topic, title, body, link} writes one unread notification of kind
notice, notice_kind ``plugin_notice``, whose details name the plugin, its version and the topic, for the member of
``login``. No plugin runs in the stack, so the route that sends it is not played here (tests/hub/test_plugin_notify.py
covers it); this gives the Inbox the row it reads. The answer is ``{id}``.
"""

from __future__ import annotations


def seed(dsn: str, body: dict) -> dict:
    import psycopg
    from psycopg.types.json import Jsonb

    with psycopg.connect(dsn) as conn:
        project = conn.execute("SELECT id FROM projects WHERE name = %s", (body["project"],)).fetchone()
        user = conn.execute("SELECT id FROM users WHERE login = %s", (body["login"],)).fetchone()
        if project is None or user is None:
            raise LookupError(f"no project {body['project']} or no user {body['login']}")
        details = {"plugin": body["plugin"], "version": "0.4.0", "topic": body["topic"], "key": None}
        row = conn.execute(
            "INSERT INTO notifications (user_id, kind, notice_kind, project_id, title, body, details, link) "
            "VALUES (%s, 'notice', 'plugin_notice', %s, %s, %s, %s, %s) RETURNING id",
            (user[0], project[0], body["title"], body["body"], Jsonb(details), body.get("link")),
        ).fetchone()
        conn.commit()
    return {"id": row[0]}
