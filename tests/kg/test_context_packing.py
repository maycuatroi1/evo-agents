"""kg_context's budgeted subgraph (``evo_agents.kg.context_pack``): the greedy of marginal gain per cost, the best
single item beside it, soft hub penalty on bridges, and the discount of what a session was already given."""

import math

from evo_agents.kg import context_pack as cp
from tests.kg.hub_graph import hub_session
from tests.kg.test_serve import built_wider


def value(items, ids, given=None, weights=None):
    objective = cp.Objective(given, weights)
    by_id = {i.id: i for i in items}
    return sum(objective.add(by_id[i]) for i in ids)


def test_greedy_takes_the_best_gain_per_cost_and_keeps_within_budget():
    items = [cp.Item("a", 10, 1.0), cp.Item("b", 2, 0.5), cp.Item("c", 2, 0.4), cp.Item("d", 2, 0.45)]
    chosen = cp.pack_subgraph(items, 6)
    assert chosen == ["b", "d", "c"]
    assert sum(i.cost for i in items if i.id in chosen) <= 6


def test_the_best_single_item_beats_a_greedy_of_crumbs():
    items = [cp.Item("big", 100, 50.0), cp.Item("tiny", 1, 1.0)]
    assert cp.pack_subgraph(items, 100) == ["big"]  # density picks tiny first; the single item is worth more


def test_facet_coverage_is_submodular_so_a_second_node_of_one_facet_adds_less():
    a = cp.Item("a", 1, 0.0, frozenset({"f"}))
    b = cp.Item("b", 1, 0.0, frozenset({"f"}))
    objective = cp.Objective()
    first = objective.add(a)
    assert objective.gain(b) < first
    assert objective.gain(b) == objective.gain(b)  # gains do not move while the set does not
    c = cp.Item("c", 1, 0.0, frozenset({"g"}))
    assert objective.gain(c) == first  # another facet is as new as the first was


def test_greedy_is_within_the_bound_of_the_best_set_by_brute_force():
    items = [
        cp.Item("a", 3, 0.9, frozenset({"x"})),
        cp.Item("b", 2, 0.5, frozenset({"x", "y"})),
        cp.Item("c", 2, 0.4, frozenset({"y"})),
        cp.Item("d", 4, 1.1, frozenset({"z"})),
        cp.Item("e", 1, 0.2, frozenset({"z"})),
    ]
    budget = 6
    best = 0.0
    for mask in range(1 << len(items)):
        picked = [items[i] for i in range(len(items)) if mask >> i & 1]
        if sum(i.cost for i in picked) <= budget:
            best = max(best, value(items, [i.id for i in picked]))
    got = value(items, cp.pack_subgraph(items, budget))
    assert got >= (1 - 1 / math.e) / 2 * best


def test_a_node_given_before_is_discounted_hard_and_a_new_one_wins():
    items = [cp.Item("old", 2, 1.0), cp.Item("new", 2, 0.5)]
    assert cp.pack_subgraph(items, 2) == ["old"]
    assert cp.pack_subgraph(items, 2, given={"old": 1}) == ["new"]
    assert cp.repeat_discount(0) == 1.0 and cp.repeat_discount(2) < cp.repeat_discount(1) < 1.0


def test_forced_seeds_cost_nothing_but_cover_their_facets():
    items = [cp.Item("n1", 1, 0.0, frozenset({"s"})), cp.Item("n2", 1, 0.0, frozenset({"t"}))]
    forced = [cp.Item("seed", 99, 1.0, frozenset({"s"}))]
    assert cp.pack_subgraph(items, 1, forced=forced) == ["n2"]


def test_a_bridge_goes_around_a_hub_when_a_cheap_path_exists_and_through_it_when_not():
    edges = {"s1": ["hub", "m"], "m": ["s1", "s2"], "hub": ["s1", "s2", "x"], "s2": ["m", "hub"], "x": ["hub"]}
    degree = {"s1": 2, "s2": 2, "m": 2, "hub": 900, "x": 1}

    def near(u):
        return [(o, f"{u}-{o}") for o in edges[u]]

    nodes, via = cp.bridge(["s1", "s2"], near, degree.get, 20.0)
    assert nodes == ["m"] and via == ["s1-m", "m-s2"]
    edges["m"] = ["s1"]
    edges["s2"] = ["hub"]
    nodes, _ = cp.bridge(["s1", "s2"], near, degree.get, 20.0)
    assert nodes == ["hub"]  # no other way joins them: a hub is dear, not forbidden
    assert cp.bridge(["s1", "s2"], near, degree.get, 3.0) == ([], [])  # the prize is not worth a path that dear


def test_step_cost_is_the_log_of_two_plus_degree():
    assert cp.step_cost(0) == math.log(2) and cp.step_cost(98) == math.log(100)


def test_kg_context_gives_other_nodes_the_second_time_in_a_session_but_the_same_on_a_retry(tmp_path, kg_env):
    project = built_wider(tmp_path, kg_env)
    session = hub_session(project, "agent")
    args = {"query": "search", "budget_tokens": 700, "hops": 3}
    first = session.call("kg_context", args)["structuredContent"]
    retry = session.call("kg_context", args)["structuredContent"]
    assert retry == first  # the same request again reads what it read
    other = session.call("kg_context", {**args, "budget_tokens": 800})["structuredContent"]
    named = {n["id"] for n in first["nodes"]} - set(first["seeds"])
    again = {n["id"] for n in other["nodes"]} - set(other["seeds"])
    assert named and again - named  # nodes already given give way to new ones


def test_kg_context_stays_within_its_budget_and_connects_the_seeds(tmp_path, kg_env):
    project = built_wider(tmp_path, kg_env)
    session = hub_session(project, "agent")
    result = session.call("kg_context", {"query": "search", "budget_tokens": 600, "hops": 2})
    data = result["structuredContent"]
    assert len(result["content"][0]["text"]) <= 600 * 3 + 200
    assert set(data["seeds"]) <= {n["id"] for n in data["nodes"]}
