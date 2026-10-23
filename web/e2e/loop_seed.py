"""The work items of a project's Loop for the Playwright stack: the rows the Loop's tick would leave after a few weeks
of a goal, written straight to the stack's database, since the stack runs no worker to tick it.

POST /loop/seed {project, released, held, tripped} writes, for goal G-1, claim CL-1 of canvas northwind, version 1 of
SRS document demo-shop and task 1 of the project (made through the API before): a brainstorm done that proposed two
claims and one running; the chain of plan ``released`` (a use case done, its build done with the frame task, its
deliver done after its merge request merged, with the ledger of the build); the build of UC ORD-4 done with plan ``held``,
whose deliver waits for a person, its merge request blocked at gate 3; a build of UC ORD-2 queued for want of a worker; a use case
waiting for its SRS version's review; and a build cancelled when the Loop was turned off. With ``tripped`` the Loop's
breaker trips. With ``problem`` (a code, P-<number>, of a problem G-1 answers), every work item goes from that problem,
as rules/2 stamps them, and a discover of CL-1 runs. The answer names the ids a test needs (``items``: name -> work item
id, ``merges``: plan -> queue id).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

FORGE = "https://gitlab.example.org/northwind/api/-/merge_requests"
WORKER_WHY = "no live worker northwind-mini (the policy's loop.worker) serves the project"


def seed(dsn: str, body: dict) -> dict:
    import psycopg
    from psycopg.types.json import Jsonb

    project, released, held = body["project"], body["released"], body["held"]
    now = datetime.now(UTC)
    ago = lambda hours: now - timedelta(hours=hours)  # noqa: E731
    with psycopg.connect(dsn, autocommit=False) as conn:
        found = conn.execute("SELECT id FROM projects WHERE name = %s", (project,)).fetchone()
        if found is None:
            raise LookupError(f"no project {project}")
        pid = found[0]

        def one(query: str, *args) -> int:
            row = conn.execute(query, (pid, *args)).fetchone()
            if row is None:
                raise LookupError(f"{query} {args}: nothing in project {project}")
            return row[0]

        goal = one("SELECT id FROM goals WHERE project_id = %s AND number = %s", 1)
        claim = one("SELECT id FROM canvas_claims WHERE project_id = %s AND number = %s", 1)
        document = one("SELECT id FROM srs_documents WHERE project_id = %s AND slug = %s", "demo-shop")
        task = one("SELECT id FROM tasks_items WHERE project_id = %s AND number = %s", 1)
        code = body.get("problem")
        problem = one("SELECT id FROM problems WHERE project_id = %s AND number = %s", int(code[2:])) if code else None

        def item(kind: str, key: str, state: str, hours: float, reason: str | None = None, **values) -> int:
            cycle = "business" if kind in ("brainstorm", "use_case", "discover") else "development"
            columns = {"project_id": pid, "kind": kind, "cycle": cycle, "key": key, "goal_id": goal, "state": state}
            if problem is not None:
                columns["problem_id"] = problem
            columns |= {"reason": reason, "engine": "rules/1", "created_at": ago(hours), "updated_at": ago(hours / 2)}
            if state in ("done", "cancelled"):
                columns["finished_at"] = ago(hours / 2)
            columns |= values
            columns["detail"] = Jsonb(columns.get("detail") or {})
            names = ", ".join(columns)
            marks = ", ".join(["%s"] * len(columns))
            query = f"INSERT INTO loop_items ({names}) VALUES ({marks}) RETURNING id"
            return conn.execute(query, tuple(columns.values())).fetchone()[0]

        def ledger(item_id: int, kind: str, state: str, hours: float, reason: str | None = None, **detail) -> None:
            conn.execute(
                "INSERT INTO loop_item_events (item_id, kind, state, reason, detail, at) VALUES (%s, %s, %s, %s, %s, %s)",
                (item_id, kind, state, reason, Jsonb(detail), ago(hours)),
            )

        def merge(plan: str, iid: int, state: str, hours: float, **values) -> int:
            columns = {"project_id": pid, "repo": "api", "iid": iid, "source": "plan", "plan_id": plan}
            columns |= {"target_branch": "main", "source_branch": f"evo/{plan}", "state": state}
            columns |= {"web_url": f"{FORGE}/{iid}", "created_at": ago(hours), "updated_at": ago(hours / 2), **values}
            names = ", ".join(columns)
            marks = ", ".join(["%s"] * len(columns))
            query = f"INSERT INTO merge_queue ({names}) VALUES ({marks}) RETURNING id"
            return conn.execute(query, tuple(columns.values())).fetchone()[0]

        ancestors = {"claim_id": claim, "srs_document_id": document, "srs_version": 1}
        items = {
            "brainstorm_done": item(
                "brainstorm", "brainstorm:G-1:r1", "done", 400, owner="@strategist", detail={"proposals": 2}
            ),
            "brainstorm": item("brainstorm", "brainstorm:G-1:2026-W43", "running", 3, owner="@strategist"),
        }
        items["use_case"] = item(
            "use_case",
            "use_case:northwind:CL-1:b1",
            "done",
            300,
            claim_id=claim,
            owner="@ba",
            detail={"document": "demo-shop", "version": 1, "baseline": 1},
        )
        merged_at = ago(20)
        items["build"] = item(
            "build",
            "build:srs:demo-shop:v1:UC ORD-1",
            "done",
            200,
            use_case="UC ORD-1",
            task_id=task,
            plan_id=released,
            parent_id=items["use_case"],
            attempts=1,
            detail={"plan": released, "task": 1},
            **ancestors,
        )
        ledger(items["build"], "created", "watching", 200, "auto_tasks")
        ledger(items["build"], "waiting", "watching", 190, "split", task=1)
        ledger(items["build"], "ready", "queued", 180)
        ledger(items["build"], "dispatched", "running", 170)
        ledger(items["build"], "done", "done", 100, plan=released)
        items["deliver"] = item(
            "deliver",
            f"deliver:{released}",
            "done",
            90,
            use_case="UC ORD-1",
            task_id=task,
            plan_id=released,
            parent_id=items["build"],
            attempts=1,
            detail={"released_at": merged_at.isoformat(), "after_sha": ["1a2b3c4d5e6f7081"]},
            **ancestors,
        )
        merges = {released: merge(released, 41, "done", 60, merged_at=merged_at, after_sha="1a2b3c4d5e6f7081")}
        merges[held] = merge(held, 42, "blocked", 10, reason="gate 3: the pipeline of the target branch is red")
        items["build_held"] = item(
            "build",
            "build:srs:demo-shop:v1:UC ORD-4",
            "done",
            60,
            use_case="UC ORD-4",
            plan_id=held,
            parent_id=items["use_case"],
            attempts=1,
            detail={"plan": held},
            **ancestors,
        )
        items["held"] = item(
            "deliver",
            f"deliver:{held}",
            "waiting_human",
            30,
            "merge_blocked",
            use_case="UC ORD-4",
            plan_id=held,
            parent_id=items["build_held"],
            attempts=1,
            detail={"merges": [merges[held]]},
            **ancestors,
        )
        items["queued"] = item(
            "build",
            "build:srs:demo-shop:v1:UC ORD-2",
            "queued",
            12,
            "worker",
            use_case="UC ORD-2",
            parent_id=items["use_case"],
            detail={"worker": "northwind-mini", "why": WORKER_WHY},
            **ancestors,
        )
        items["review"] = item(
            "use_case",
            "use_case:northwind:CL-1:b2",
            "waiting_human",
            8,
            "srs_review",
            claim_id=claim,
            owner="@ba",
            detail={"srs": {"document": "demo-shop", "version": 1}},
        )
        items["cancelled"] = item(
            "build",
            "build:srs:demo-shop:v1:UC ORD-3",
            "cancelled",
            500,
            "loop_off",
            use_case="UC ORD-3",
            parent_id=items["use_case"],
            **ancestors,
        )
        if problem is not None:
            items["discover"] = item(
                "discover",
                "discover:northwind:CL-1:b1",
                "running",
                2,
                owner="@strategist",
                claim_id=claim,
                engine="rules/2",
            )
        if body.get("tripped"):
            conn.execute(
                "UPDATE loop_states SET breaker = 'tripped', breaker_reason = %s, tripped_at = now() "
                "WHERE project_id = %s",
                ("31 work items in an hour, over the cap of 30", pid),
            )
        conn.commit()
    return {"items": items, "merges": merges}
