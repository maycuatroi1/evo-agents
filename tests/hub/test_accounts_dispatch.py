"""The account a member picks by hand for a run (``evo_agents.hub.server.runs.service.accounts``): ``account`` on a
dispatch of steps, a plan run and an author run, and on a rerun.

The checks the claude-account-rotation plan names for its second step: the run takes the account picked and keeps it,
the pick moves no rotation, a rerun keeps the account picked for the run it reruns unless it names another one or
asks the rules; a name that is no env secret of the dispatcher's is 404, one not bound to the project, bound only to
other workers, paused or past its end is 422 saying why, and nothing is dispatched; audit rows name the account by its
secret's id. A body without ``account`` dispatches as before (``test_accounts_api``, ``test_credentials_api``)."""

from datetime import timedelta

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from evo_agents.hub import runs, tables
from tests.hub import test_credentials_api as credentials_api
from tests.hub.live import sql
from tests.hub.test_accounts_api import VAR, account_of, change, consecutive, taken_how, three
from tests.hub.test_credentials_api import (
    CHECKOUTS,
    PLAN,
    PROJECT,
    STEPS,
    audit_rows,
    env_secret,
    git_secret,
    give_back,
    sample,
    secret_ids,
    worker_of,
)
from tests.hub.test_runs import add_worker, claim, count_runs, report

app_key, config, hub, client = (
    credentials_api.app_key,
    credentials_api.config,
    credentials_api.hub,
    credentials_api.client,
)

HARNESS = "evo-agents-harness"


def dispatch(client, headers, repo: str, worker: dict | None, **extra):
    body = {"plan_id": PLAN, "steps": [STEPS[repo]], **extra}
    if worker is not None:
        body["worker_id"] = worker["id"]
    return client.post(f"/v1/projects/{PROJECT}/runs", json=body, headers=headers)


def dispatched(client, headers, repo: str, worker: dict, **extra) -> int:
    """A run of the step on ``repo``, pinned to ``worker`` and claimed by it."""
    response = dispatch(client, headers, repo, worker, **extra)
    assert response.status_code == 201, response.text
    run_id = response.json()[0]["id"]
    assert claim(client, worker)["id"] == run_id
    return run_id


def rerun(client, headers, run_id: int, body: dict | None = None):
    path = f"/v1/projects/{PROJECT}/runs/{run_id}/rerun"
    return client.post(path, headers=headers) if body is None else client.post(path, json=body, headers=headers)


def ended(client, worker: dict, run_id: int) -> None:
    assert report(client, worker, run_id, "failed", error="done").status_code == 200


def rerun_claimed(client, headers, worker: dict, run_id: int, body: dict | None = None) -> int:
    response = rerun(client, headers, run_id, body)
    assert response.status_code == 201, response.text
    new_id = response.json()["id"]
    assert claim(client, worker)["id"] == new_id
    return new_id


def all_runs(db) -> int:
    return count_runs(db)[0][0]


# Dispatching with an account


def test_a_dispatch_with_an_account_takes_it_keeps_it_and_moves_no_rotation(client, hub, hub_db):
    three(client, hub["owner"])
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = dispatched(client, hub["owner"], "evo-agents", worker, account="claude-b")
    assert taken_how(hub_db, run_id) == [(VAR, "claude-b", "manual")]
    assert account_of(client, worker, run_id) == "claude-b"
    assert give_back(client, worker["headers"], run_id).status_code == 200
    assert account_of(client, worker, run_id) == "claude-b"
    assert taken_how(hub_db, run_id) == [(VAR, "claude-b", "manual")]
    # the audit row names the account by its secret's id, never its name
    target = audit_rows(hub_db, "run.dispatch")[-1][0]
    assert target.endswith(f" account=secret:{secret_ids(hub_db, 'claude-b')}") and "claude-b" not in target
    ended(client, worker, run_id)
    # a run picked by hand moved no turn: the rules still start at the first account
    assert consecutive(client, hub["owner"], worker, 2) == ["claude-a", "claude-b"]


def test_an_account_picked_by_hand_is_kept_even_when_paused_or_cooling_since(client, hub, hub_db):
    three(client, hub["owner"])
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = dispatched(client, hub["owner"], "evo-agents", worker, account="claude-c")
    assert change(client, hub["owner"], "claude-c", paused=True).status_code == 200
    assert account_of(client, worker, run_id) == "claude-c"


def test_a_plan_run_dispatch_with_an_account_takes_it(client, hub, hub_db):
    three(client, hub["owner"])
    worker = worker_of(client, hub["owner"], "mac-mini")
    body = {"plan_id": PLAN, "worker_id": worker["id"], "account": "claude-c"}
    response = client.post(f"/v1/projects/{PROJECT}/plan-runs", json=body, headers=hub["owner"])
    assert response.status_code == 201, response.text
    run_id = response.json()["id"]
    assert claim(client, worker)["id"] == run_id
    assert account_of(client, worker, run_id) == "claude-c"
    assert taken_how(hub_db, run_id) == [(VAR, "claude-c", "manual")]
    assert audit_rows(hub_db, "run.dispatch_plan")[-1][0].endswith(f" account=secret:{secret_ids(hub_db, 'claude-c')}")


def test_an_author_run_dispatch_with_an_account_takes_it(client, hub, hub_db):
    three(client, hub["owner"])
    checkouts = {**CHECKOUTS, f"{PROJECT}/{HARNESS}": {"path": f"/src/{HARNESS}", "branch": "main"}}
    worker = add_worker(client, hub["owner"], "mac-mini", checkouts=checkouts)
    beat = {"runtimes": worker["runtimes"], "checkouts": checkouts, "free_slots": 1, "runs": []}
    response = client.post(
        "/v1/worker/heartbeat", json={**beat, "run_kinds": list(runs.RUN_KINDS)}, headers=worker["headers"]
    )
    assert response.status_code == 200, response.text
    body = {"request": "Write a plan.", "worker_id": worker["id"], "account": "claude-a"}
    response = client.post(f"/v1/projects/{PROJECT}/author-runs", json=body, headers=hub["owner"])
    assert response.status_code == 201, response.text
    assert taken_how(hub_db, response.json()["id"]) == [(VAR, "claude-a", "manual")]
    body["account"] = "nobody"
    response = client.post(f"/v1/projects/{PROJECT}/author-runs", json=body, headers=hub["owner"])
    assert response.status_code == 404, response.text


def test_an_account_dispatch_refuses_a_name_that_is_not_the_dispatchers_env_secret_bound_there(client, hub, hub_db):
    three(client, hub["owner"])
    env_secret(client, hub["other"], "theirs", VAR, sample())
    git_secret(client, hub["owner"], "gitlab", "https://gitlab.example.org/team", sample())
    box, desk = worker_of(client, hub["owner"], "box"), worker_of(client, hub["owner"], "desk")
    env_secret(client, hub["owner"], "box-only", VAR, sample(), workers=["box"])
    env_secret(client, hub["owner"], "unbound", VAR, sample())
    s, b = tables.secrets, tables.secret_bindings
    unbound = select(s.c.id).where(s.c.name == "unbound").scalar_subquery()
    sql(hub_db, delete(b).where(b.c.secret_id == unbound))
    env_secret(client, hub["owner"], "stale", VAR, sample())
    sql(hub_db, update(s).values(expires_at=func.now() - timedelta(minutes=1)).where(s.c.name == "stale"))
    assert change(client, hub["owner"], "claude-c", paused=True).status_code == 200
    before = all_runs(hub_db)

    for name in ("nobody", "theirs", "gitlab"):
        response = dispatch(client, hub["owner"], "evo-agents", desk, account=name)
        assert response.status_code == 404, response.text
        assert f"you have no env secret named {name}" in response.json()["message"]
    refusals = {
        "unbound": (desk, f"account unbound is not bound to project {PROJECT}"),
        "box-only": (desk, "on worker box only, not on worker desk"),
        "stale": (desk, "account stale ended at"),
        "claude-c": (desk, "account claude-c is paused"),
    }
    for name, (worker, why) in refusals.items():
        response = dispatch(client, hub["owner"], "evo-agents", worker, account=name)
        assert response.status_code == 422, response.text
        assert why in response.json()["message"] and "nothing was dispatched" in response.json()["message"]
    response = dispatch(client, hub["owner"], "evo-agents", None, account="box-only")
    assert response.status_code == 422 and "the run is not pinned to a worker" in response.json()["message"]
    response = dispatch(client, hub["owner"], "evo-agents", None, account="Not A Name")
    assert response.status_code == 422, response.text
    assert all_runs(hub_db) == before
    # pinned to the worker it is bound to, it goes
    run_id = dispatched(client, hub["owner"], "evo-agents", box, account="box-only")
    assert account_of(client, box, run_id) == "box-only"


def test_an_oauth_account_with_a_lapsed_access_token_is_pickable_and_a_candidate_unlike_a_setup_token(
    client, hub, hub_db
):
    three(client, hub["owner"])
    desk = worker_of(client, hub["owner"], "desk")
    env_secret(client, hub["owner"], "oauth-old", VAR, sample())
    env_secret(client, hub["owner"], "token-old", VAR, sample())
    s, a = tables.secrets, tables.accounts
    lapsed = func.now() - timedelta(minutes=1)
    sql(hub_db, update(s).values(expires_at=lapsed).where(s.c.name.in_(("oauth-old", "token-old"))))
    oauth = select(s.c.id).where(s.c.name == "oauth-old").scalar_subquery()
    sealed = {"oauth_sealed": b"sealed", "oauth_nonce": b"nonce", "oauth_key_id": "k1"}
    upsert = insert(a).values(secret_id=oauth, **sealed)
    sql(hub_db, upsert.on_conflict_do_update(index_elements=[a.c.secret_id], set_=sealed))

    response = dispatch(client, hub["owner"], "evo-agents", desk, account="token-old")
    assert response.status_code == 422 and "account token-old ended at" in response.json()["message"]
    # the pick is accepted; its tokens are fake here, so the lease itself is covered by test_account_oauth_refresh
    run_id = dispatched(client, hub["owner"], "evo-agents", desk, account="oauth-old")
    ended(client, desk, run_id)
    path = f"/v1/projects/{PROJECT}/runs/{run_id}/rerun-candidates"
    answer = client.get(path, headers=hub["owner"]).json()
    states = {c["name"]: c["state"] for c in answer["accounts"]}
    assert states["oauth-old"] == "ready" and states["token-old"] == "expired"
    (candidate,) = (w for w in answer["workers"] if w["worker"] == "desk")
    assert "oauth-old" in candidate["accounts"] and "token-old" not in candidate["accounts"]


# Rerunning


def test_a_rerun_keeps_the_account_picked_for_the_run_unless_it_names_another_or_asks_the_rules(client, hub, hub_db):
    three(client, hub["owner"])
    worker = worker_of(client, hub["owner"], "mac-mini")
    first = dispatched(client, hub["owner"], "evo-agents", worker, account="claude-c")
    ended(client, worker, first)

    kept = rerun_claimed(client, hub["owner"], worker, first)
    assert taken_how(hub_db, kept) == [(VAR, "claude-c", "manual")]
    assert account_of(client, worker, kept) == "claude-c"
    assert audit_rows(hub_db, "run.rerun")[-1][0].endswith(f" account=secret:{secret_ids(hub_db, 'claude-c')}")
    ended(client, worker, kept)

    other = rerun_claimed(client, hub["owner"], worker, kept, {"account": "claude-b"})
    assert account_of(client, worker, other) == "claude-b"
    assert taken_how(hub_db, other) == [(VAR, "claude-b", "manual")]
    ended(client, worker, other)

    by_rule = rerun_claimed(client, hub["owner"], worker, other, {"account_by_rule": True})
    assert account_of(client, worker, by_rule) == "claude-a"
    assert taken_how(hub_db, by_rule) == [(VAR, "claude-a", "pool")]
    ended(client, worker, by_rule)

    # a run whose account the rules picked is rerun with the rules again
    again = rerun_claimed(client, hub["owner"], worker, by_rule)
    assert taken_how(hub_db, again) == []
    assert account_of(client, worker, again) == "claude-b"
    assert taken_how(hub_db, again) == [(VAR, "claude-b", "pool")]


def test_a_rerun_says_why_when_its_account_cannot_go_and_refuses_both_choices(client, hub, hub_db):
    three(client, hub["owner"])
    worker = worker_of(client, hub["owner"], "mac-mini")
    first = dispatched(client, hub["owner"], "evo-agents", worker, account="claude-c")
    ended(client, worker, first)
    before = all_runs(hub_db)

    response = rerun(client, hub["owner"], first, {"account": "claude-a", "account_by_rule": True})
    assert response.status_code == 422, response.text
    response = rerun(client, hub["owner"], first, {"account": "nobody"})
    assert response.status_code == 404, response.text
    assert change(client, hub["owner"], "claude-c", paused=True).status_code == 200
    response = rerun(client, hub["owner"], first)
    assert response.status_code == 422, response.text
    detail = response.json()["message"]
    assert f"run {first} took its account for {VAR} by hand" in detail and "account claude-c is paused" in detail
    assert all_runs(hub_db) == before
    # asking the rules, it goes
    rerun_claimed(client, hub["owner"], worker, first, {"account_by_rule": True})


def test_rerun_candidates_list_the_accounts_and_drop_a_worker_with_none_ready(client, hub, hub_db):
    three(client, hub["owner"])
    worker = worker_of(client, hub["owner"], "mac-mini")
    run_id = dispatched(client, hub["owner"], "evo-agents", worker, account="claude-a")
    ended(client, worker, run_id)
    path = f"/v1/projects/{PROJECT}/runs/{run_id}/rerun-candidates"
    assert change(client, hub["owner"], "claude-b", paused=True).status_code == 200
    answer = client.get(path, headers=hub["owner"]).json()
    states = {a["name"]: (a["state"], a["workers"], a["used_by_run"]) for a in answer["accounts"]}
    assert states == {
        "claude-a": ("ready", ["any"], True),
        "claude-b": ("paused", ["any"], False),
        "claude-c": ("ready", ["any"], False),
    }
    (candidate,) = (w for w in answer["workers"] if w["worker"] == "mac-mini")
    assert candidate["fit"] and candidate["accounts"] == ["claude-a", "claude-c"]
    for name in ("claude-a", "claude-c"):
        assert change(client, hub["owner"], name, paused=True).status_code == 200
    answer = client.get(path, headers=hub["owner"]).json()
    (candidate,) = (w for w in answer["workers"] if w["worker"] == "mac-mini")
    assert not candidate["fit"] and "has no account of yours that is ready for this project" in candidate["reasons"]
