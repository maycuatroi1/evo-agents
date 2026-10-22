"""The autonomy card and the categories of a decision (``evo_agents.hub.autonomy_card``), as criterion a12 of plan
autonomy-levels names them: the prompt of a run with an agent opens with a card built from the run's snapshot, short at
A5, and the card replaces the fixed list of categories of a plan run's prompt; each class that takes ask has its
categories of decision, a category belongs to one class. The claim's side: test_autonomy_level_routing."""

from __future__ import annotations

from evo_agents.hub import autonomy_card, autonomy_levels, runs
from evo_agents.hub.autonomy_card import CLASS_CATEGORIES, FREE_CATEGORIES, HEADING, Who
from evo_agents.hub.autonomy_levels import NO_ASK, Source, effective, pref_source, snapshot

TAKEN = "2026-10-22T00:00:00+00:00"
LENA, IVAN = Who("lena", "lead", "lab"), Who("ivan", "intern", "intern-2026", reports_to="lena")
SHIPS = "merging, deploying and releasing are the agent's"


def of(*sources: Source) -> dict:
    return snapshot(effective(sources), TAKEN)


def test_autonomy_level_card_at_a5_names_the_principal_and_the_floors_s2_and_s3_alone():
    made = autonomy_card.card(of(pref_source("lena", None)), LENA)
    lines = made.text.splitlines()
    assert lines[0] == HEADING
    assert lines[1] == "Principal: lena, lead in unit lab. Effective level: A5. Every class of action is yours to do."
    assert lines[2:] == ["Floors for everyone, whatever the level:", *(f"- {floor}" for floor in autonomy_card.FLOORS)]
    assert [line[2:4] for line in lines if line.startswith("- S")] == ["S2", "S3"]
    assert made.ships and made.categories == FREE_CATEGORIES
    assert set(made.modes.values()) == {"auto"}


def test_autonomy_level_card_of_an_intern_names_their_mentor_and_each_class_in_deny_ask_and_notify():
    made = autonomy_card.card(of(pref_source("ivan", "A2")), IVAN)
    text = made.text
    assert "Principal: ivan, intern in unit intern-2026, reports to lena, who answers this run's decisions." in text
    assert "Effective level: A2." in text
    deny = next(line for line in text.splitlines() if line.startswith("- Never do (deny): "))
    for klass in ("K8", "K9", "K11", "K12"):
        assert f"{klass} {autonomy_levels.CLASS_NAMES[klass]}" in deny, klass
    asked = next(line for line in text.splitlines() if line.startswith("- Ask first with `evo-agents worker ask"))
    assert "K6" in asked and "(category deploy)" in asked and "(category external_send)" in asked
    notify = next(line for line in text.splitlines() if "(notify)" in line)
    assert "K5" in notify and "but ask, category merge, when it touches a risk path" in notify
    assert made.categories == ("deploy", "external_send", "merge", *FREE_CATEGORIES)
    assert not made.ships, "K5 notify and K6 ask: merging and deploying are not the agent's alone"
    spec = {"card": text, "modes": made.modes, "categories": list(made.categories), "sources": made.sources}
    assert made.spec() == spec and set(made.sources) == set(autonomy_levels.CLASSES)


def test_autonomy_level_card_shows_an_ask_of_a_project_policy_as_auto():
    policy = autonomy_levels.policy_source("demo", "app", autonomy_levels.REPO_OFF, autonomy_levels.RELEASE_MANUAL)
    made = autonomy_card.card(of(pref_source("lena", None), policy), LENA)
    assert made.modes["K5"] == made.modes["K6"] == "auto" and made.ships
    assert "Every class of action is yours to do." in made.text


def test_autonomy_level_card_replaces_the_fixed_categories_of_a_plan_runs_prompt():
    plan = {"id": "fleet", "steps": [{"id": 1, "what": "do it", "status": "pending"}]}
    repos = [{"name": "app", "branch": "main"}]
    bare = runs.build_plan_prompt(plan, repos)
    assert not bare.startswith(HEADING) and SHIPS in bare
    for category in runs.PROMPTED_DECISION_CATEGORIES:
        assert f"\n  - {category}: " in bare, category
    spec = autonomy_card.card(of(pref_source("ivan", "A2")), IVAN).spec()
    carded = runs.build_plan_prompt(plan, repos, None, spec)
    assert carded.startswith(spec["card"] + "\n\n")
    assert SHIPS not in carded and "(K5) and a deploy or a release (K6) go as your autonomy card says" in carded
    listed = [line[4:].split(":")[0] for line in carded.splitlines() if line.startswith("  - ")]
    assert listed == ["deploy", "external_send", "merge", "architecture", "scope"]
    short = autonomy_card.card(of(pref_source("lena", None)), LENA).spec()
    at_a5 = runs.build_plan_prompt(plan, repos, None, short)
    assert at_a5.startswith(HEADING) and SHIPS in at_a5
    step = runs.build_step_session_prompt(plan, plan["steps"][0], repos, autonomy=spec)
    assert step.startswith(spec["card"])
    assert autonomy_card.with_card("the prompt", None) == "the prompt"


def test_autonomy_level_categories_one_class_each_and_every_class_that_takes_ask_has_its_own():
    takes_ask = [klass for klass in autonomy_levels.CLASSES if klass not in NO_ASK]
    assert sorted(CLASS_CATEGORIES, key=autonomy_levels.CLASSES.index) == takes_ask
    named = [category for categories in CLASS_CATEGORIES.values() for category in categories]
    assert len(named) == len(set(named)), "a category belongs to one class"
    assert set(named) | set(FREE_CATEGORIES) == set(runs.DECISION_CATEGORIES)
    assert (CLASS_CATEGORIES["K5"], CLASS_CATEGORIES["K8"], CLASS_CATEGORIES["K12"]) == (
        ("merge",),
        ("credential",),
        ("access",),
    )
    assert CLASS_CATEGORIES["K6"] == ("deploy",) and CLASS_CATEGORIES["K9"] == ("delete_data", "live_migration")
    assert (CLASS_CATEGORIES["K10"], CLASS_CATEGORIES["K11"]) == (("external_send",), ("spend_money",))
    assert set(runs.DECISION_CATEGORIES) <= set(runs.ASKED_DECISION_CATEGORIES), "worker ask takes them"
    assert set(runs.ASKED_DECISION_CATEGORIES) <= set(runs.SHOWN_DECISION_CATEGORIES)
