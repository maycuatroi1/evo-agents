"""The lens product of the Curator's review (``review.PRODUCT``), as criterion a9 of plan loop-engine names it: the
review run reads the project's signals, the claims of its canvases and its goals, writes proposals of claims through
``canvas__propose`` of the plugin canvas and proposals of a change of the autonomy policy through ``builder__propose``
of kind policy, and changes no claim, baseline, goal or policy itself. The lens takes its turn with the other lenses;
only a review run whose night looks through it proposes, and a project without the plugin canvas proposes no claim.
And schema 0094. The agent on the worker is a stand-in: the test calls the tools with the worker's token and the run,
as its MCP client would. The product of the canvas, Northwind Pantry, is made up.
"""

from __future__ import annotations

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

import psycopg
from sqlalchemy import func, insert, literal, select, update

from evo_agents.hub import tables
from evo_agents.hub.migrate import migrate
from tests.hub.live import sql
from tests.hub.test_ba_agent import agent_of, enable, tool
from tests.hub.test_builder import notices
from tests.hub.test_canvas_plugin import SLUG, edit, made, problem
from tests.hub.test_chats import OPEN_SINKS
from tests.hub.test_curator import NIGHT, OWNER, PROJECT, night  # noqa: F401 (night is a fixture)
from tests.hub.test_migrate import ALL, move_to
from tests.hub.test_plans import registration
from tests.hub.test_plugin_points import tool_names
from tests.hub.test_review_runs import collected, finding, proposed, review  # noqa: F401 (review is a fixture)
from tests.hub.test_runs import claim, moved, web_client  # noqa: F401 (web_client is a fixture)
from tests.hub.test_signals import key, push

PRODUCT_NIGHT = ["product_goals", "product", "kg_health"]
OTHER_NIGHT = ["tool_errors", "environment", "cost"]
RATE = "pantry.reorder_rate"
CARD = {"hypothesis": "Owners reorder weekly when the app reminds them", "test": "Remind ten shops for a month"}
PROPOSALS = f"/v1/x/builder/{PROJECT}/proposals"


def held(review, lenses: list[str]) -> dict:  # noqa: F811
    """The review run of ``review``'s night, its night's lenses set to ``lenses`` before its worker claims it (the
    prompt is built at the claim), running: its spec."""
    assert collected(review.client, NIGHT)["review"] == 1
    cf = tables.curator_figures
    ((row_id, figures),) = sql(review.db, select(cf.c.id, cf.c.figures).where(cf.c.run_id.is_not(None)))
    sql(review.db, update(cf).where(cf.c.id == row_id).values(figures={**figures, "lenses": lenses}))
    spec = claim(review.client, review.worker)
    assert spec["kind"] == "review"
    moved(review.client, review.worker, spec["id"], "running")
    return spec


def night_figures(review) -> dict:  # noqa: F811
    cf = tables.curator_figures
    return sql(review.db, select(cf.c.figures).where(cf.c.run_id.is_not(None)))[0][0]


def open_sinks(review) -> None:  # noqa: F811
    client, headers = review.client, review.headers
    registered = client.put(f"/v1/projects/{PROJECT}", json=registration(OPEN_SINKS), headers=headers["admin"])
    assert registered.status_code == 200, registered.text


@pytest.fixture
def product(review):  # noqa: F811
    """review's project read through open sinks, with the plugins canvas and builder enabled, owner's canvas of
    Northwind Pantry with one claim, and the signal key pantry.reorder_rate with one value."""
    client, headers = review.client, review.headers
    open_sinks(review)
    enable(client, headers["owner"], "canvas")
    enable(client, headers["owner"], "builder", ["chats:read"])
    made(client, headers["owner"], description="Stock reordering for corner shops")
    edit(client, headers["owner"], problem(), reason="the first claim")
    key(client, headers["owner"], RATE, "number", unit="ratio")
    push(client, headers["owner"], {"key": RATE, "value": 0.25})
    return review


def canvas_proposals(db) -> list[tuple]:
    p = tables.canvas.proposals
    return sql(db, select(p.c.kind, p.c.status, p.c.actor, p.c.run_id, p.c.fields["text"].astext).order_by(p.c.id))


def claim_count(db) -> int:
    return sql(db, select(func.count()).select_from(tables.canvas.claims))[0][0]


# The night's figures and the prompt


def test_product_lens_figures_hold_goals_signals_and_canvases_and_the_prompt_says_what_it_proposes(product):
    spec = held(product, PRODUCT_NIGHT)
    found = night_figures(product)["product"]
    (goal,) = found["goals"]
    assert (goal["goal"], goal["metric"], goal["key"], goal["canvas"]) == ("G-1", None, None, None)
    assert goal["statement"] == "Run the plans the owner approved while the owner sleeps."
    (signal,) = found["signals"]
    assert {name: signal[name] for name in ("key", "kind", "unit", "values", "min", "max", "newest")} == {
        "key": RATE,
        "kind": "number",
        "unit": "ratio",
        "values": 1,
        "min": 0.25,
        "max": 0.25,
        "newest": 0.25,
    }
    assert found["canvases"] == [
        {"canvas": SLUG, "claims": {"assumption": 1}, "overdue": 0, "open_proposals": 0, "baseline": None}
    ]
    prompt = spec["prompt"]
    assert "- product: the product through its numbers" in prompt and "# The lens product" in prompt
    for words in (
        "canvas__propose",
        "base_rev",
        "Never propose a claim as validated",
        "builder__propose of kind policy",
    ):
        assert words in prompt, words
    assert prompt.index('"product"') < prompt.index('"tools"')  # the lens's figure first on its night
    assert "propose no claim tonight" not in prompt and "propose no change of its autonomy policy" not in prompt


def test_product_lens_leaves_claims_out_of_a_project_without_the_plugin_canvas(review):  # noqa: F811
    open_sinks(review)
    spec = held(review, PRODUCT_NIGHT)
    assert night_figures(review)["product"]["canvases"] is None
    prompt = spec["prompt"]
    assert f"Project {PROJECT} does not enable the plugin canvas: propose no claim tonight." in prompt
    assert f"Project {PROJECT} does not enable the plugin builder: propose no change" in prompt
    agent = agent_of(review.worker, spec["id"])
    assert "canvas__propose" not in tool_names(review.client, agent)
    text, error = tool(
        review.client, agent, "canvas__propose", canvas=SLUG, proposals=[problem()], reason="x", synthetic=False
    )
    assert error and "no tool canvas__propose" in text


def test_product_lens_prompt_is_absent_on_a_night_of_other_lenses(product):
    spec = held(product, OTHER_NIGHT)
    assert "# The lens product" not in spec["prompt"] and "builder__propose" not in spec["prompt"]


# Claims


def test_product_lens_proposes_claims_through_canvas_and_changes_none(product):
    run_id = held(product, PRODUCT_NIGHT)["id"]
    client, db, agent = product.client, product.db, agent_of(product.worker, run_id)
    assert {"canvas__propose", "canvas__read", "canvas__signals", "builder__propose"} <= set(tool_names(client, agent))
    read, error = tool(client, agent, "canvas__read", canvas=SLUG)
    assert not error and "CL-1" in read
    signals, error = tool(client, agent, "canvas__signals")
    assert not error and RATE in signals
    claims = claim_count(db)
    new = {"cell": "solution", "text": "A weekly reminder to reorder", "test_card": CARD, "signal_keys": [RATE]}
    change = {"claim": "CL-1", "base_rev": 1, "confidence": "high"}
    text, error = tool(
        client, agent, "canvas__propose", canvas=SLUG, proposals=[new, change], reason="reorder rate", synthetic=False
    )
    assert not error, text
    assert text.startswith(f"2 proposal(s) of canvas {SLUG}, open for members to accept or reject")
    assert canvas_proposals(db) == [
        ("create", "open", "agent", run_id, "A weekly reminder to reorder"),
        ("change", "open", "agent", run_id, None),
    ]
    assert claim_count(db) == claims
    # it never writes a claim, adds evidence, nor proposes one as validated
    edited, error = tool(client, agent, "canvas__edit", canvas=SLUG, changes=[change], reason="x")
    assert error and "only the agent of a chat run of @strategist" in edited
    added, error = tool(
        client,
        agent,
        "canvas__evidence",
        canvas=SLUG,
        claim="CL-1",
        kind="market",
        direction="supports",
        summary="x",
        synthetic=False,
    )
    assert error and "calls canvas__evidence" in added
    validated = {"claim": "CL-1", "base_rev": 1, "status": "validated"}
    text, error = tool(
        client, agent, "canvas__propose", canvas=SLUG, proposals=[validated], reason="x", synthetic=False
    )
    assert error and "bad arguments" in text
    stale = {"claim": "CL-1", "base_rev": 7, "text": "Shops run out between deliveries"}
    text, error = tool(client, agent, "canvas__propose", canvas=SLUG, proposals=[stale], reason="x", synthetic=False)
    assert error and "CL-1 is at rev 1" in text
    assert len(canvas_proposals(db)) == 2 and claim_count(db) == claims


def test_product_lens_review_run_of_another_night_proposes_nothing(product):
    run_id = held(product, OTHER_NIGHT)["id"]
    client, db, agent = product.client, product.db, agent_of(product.worker, run_id)
    new = {"cell": "solution", "text": "A weekly reminder to reorder", "test_card": CARD}
    text, error = tool(client, agent, "canvas__propose", canvas=SLUG, proposals=[new], reason="x", synthetic=False)
    assert error and f"review run #{run_id} does not look through the lens product tonight" in text
    text, error = tool(
        client, agent, "builder__propose", kind="policy", summary="x", set={"breaker.judge_misses_7d": 1}
    )
    assert error and f"review run #{run_id} does not look through the lens product tonight" in text
    assert canvas_proposals(db) == [] and sql(db, select(tables.builder_proposals.c.id)) == []


# The policy


def test_product_lens_proposes_a_change_of_the_policy_that_an_admin_decides(product):
    run_id = held(product, PRODUCT_NIGHT)["id"]
    client, db, agent, headers = product.client, product.db, agent_of(product.worker, run_id), product.headers
    text, error = tool(
        client,
        agent,
        "builder__propose",
        kind="policy",
        summary="Trip the judge breaker sooner: G-1 waits on runs the judge misses",
        set={"breaker.judge_misses_7d": 1},
    )
    assert not error, text
    proposal_id = int(text.split()[1])
    assert text.startswith(f"Proposal {proposal_id} waits for an admin of project {PROJECT}")
    b, u = tables.builder_proposals, tables.users
    query = select(b.c.kind, b.c.state, b.c.run_id, b.c.chat_id, b.c.thread_root_id, u.c.login).join_from(
        b, u, u.c.id == b.c.proposed_by
    )
    assert sql(db, query) == [("policy", "pending", run_id, None, None, OWNER)]
    assert notices(db) == [(OWNER, proposal_id, "policy")]
    n = tables.notifications
    links = sql(db, select(n.c.link, n.c.title).where(n.c.notice_kind == "builder_proposal"))
    assert links == [(f"/p/{PROJECT}/runs/{run_id}", f"The Curator proposes a change of policy in {PROJECT}")]
    # no other kind, and never a goal
    for kind, extra in (
        ("goal", {"set": {"statement": "Sell more"}}),
        ("features", {"target": {"layer": "project"}, "set": {"chat": False}}),
    ):
        text, error = tool(client, agent, "builder__propose", kind=kind, summary="x", **extra)
        assert error and "proposes a change of the autonomy policy alone" in text
    # the run never answers it; an admin does, and the hub writes the policy's next revision
    refused = client.post(f"{PROPOSALS}/{proposal_id}/answer", json={"answer": "approve"}, headers=agent)
    assert refused.status_code in (403, 404)
    shown = client.get(f"{PROPOSALS}/{proposal_id}", headers=headers["owner"]).json()
    assert (shown["run_id"], shown["link"], shown["proposed_by"]) == (run_id, None, OWNER)
    approved = client.post(f"{PROPOSALS}/{proposal_id}/answer", json={"answer": "approve"}, headers=headers["owner"])
    assert approved.status_code == 200, approved.text
    assert (approved.json()["state"], approved.json()["applied_version"]) == ("applied", 1)
    policy = client.get(f"/v1/projects/{PROJECT}/policy", headers=headers["owner"]).json()
    assert policy["revision"] == 1 and policy["policy"]["breaker"]["judge_misses_7d"] == 1


def test_product_lens_proposes_for_a_night_shift_owner_who_still_writes(product):
    run_id = held(product, PRODUCT_NIGHT)["id"]
    client, agent, headers = product.client, agent_of(product.worker, run_id), product.headers
    lowered = client.put(
        f"/v1/admin/projects/{PROJECT}/grants/{OWNER}",
        json={"role": "reader", "max_level": "internal"},
        headers=headers["admin"],
    )
    assert lowered.status_code == 200, lowered.text
    text, error = tool(
        client, agent, "builder__propose", kind="policy", summary="x", set={"breaker.judge_misses_7d": 1}
    )
    assert error and f"the Curator proposes for a writer of project {PROJECT}" in text
    new = {"cell": "solution", "text": "A weekly reminder to reorder", "test_card": CARD}
    text, error = tool(client, agent, "canvas__propose", canvas=SLUG, proposals=[new], reason="x", synthetic=False)
    assert error and canvas_proposals(product.db) == []


# Schema 0094


def test_product_lens_schema_0092_takes_the_lens_and_going_back_deletes_what_it_wrote(review):  # noqa: F811
    run_id = held(review, PRODUCT_NIGHT)["id"]
    client, worker, db = review.client, review.worker, review.db
    evidence = [{"kind": "run", "run_id": run_id, "seq": 1}]
    for lens in ("product", "cost"):
        made_one = finding(client, worker, run_id, lens=lens, title=f"a finding of {lens}", evidence=evidence)
        assert made_one.status_code == 201, made_one.text
    proposed(client, worker, run_id, lens="product", evidence=evidence)

    move_to(db, "0093", down=True)
    assert sql(db, select(tables.findings.c.lens)) == [("cost",)]
    assert sql(db, select(func.count()).select_from(tables.proposals))[0][0] == 0
    f = tables.findings
    copied = select(
        f.c.project_id, f.c.run_id, literal("product"), f.c.severity, literal("again"), f.c.evidence, f.c.label
    )
    names = ("project_id", "run_id", "lens", "severity", "title", "evidence", "label")
    with pytest.raises(psycopg.errors.CheckViolation, match="findings_lens_check"):
        sql(db, insert(f).from_select(names, copied.where(f.c.lens == "cost")))
    assert migrate(db.dsn).applied == ALL[ALL.index("0094") :]
    again = finding(client, worker, run_id, lens="product", title="again", evidence=evidence)
    assert again.status_code == 201, again.text
