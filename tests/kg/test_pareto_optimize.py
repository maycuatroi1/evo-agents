"""The KG optimize loop's estimates and gate: clipped IPS over the 5% exploration channel (``evo_agents.kg.rank``) and
the Pareto choice of a configuration by a paired bootstrap on the holdout (``evo_agents.hub.kg_optimize``)."""

import random

import pytest

from evo_agents.hub import kg_optimize as opt
from evo_agents.kg import explore, rank


def label(rank_: int, p: float, clicked: bool, grep: bool = False, cid: str = "n1") -> dict:
    return {
        "results": [{"id": "x", "rank": 1}, {"id": cid, "rank": rank_, "explored": rank_, "p": p}],
        "clicks": [{"id": cid, "rank": rank_}] if clicked else [],
        "grep_after": grep,
    }


def test_samples_read_the_chance_the_explorer_logged_and_a_grep_after_is_no_reward():
    labels = [label(4, 0.01, True), label(5, 0.01, True, grep=True), {"results": [{"id": "x", "rank": 1}]}]
    assert rank.explore_samples(labels) == [(1.0, 0.01, 4), (0.0, 0.01, 5)]
    marked = explore.MARK.format(rank=4, p=0.0125, ref="n1")
    assert explore.marked(marked) == (4, 0.0125, "n1")


def test_clipped_ips_caps_a_rare_draw_and_its_weights_follow_the_inverse_chance():
    rare = [(1.0, 1e-6, 4)] + [(0.0, 0.05, 4)] * 9
    clipped = rank.clipped_ips(rare, clip=0.02)
    assert clipped["clipped"] == 1 and clipped["n"] == 10
    assert clipped["ips"] == pytest.approx(50.0 / 10)  # weight 1 / 0.02, not 1e6
    assert 0 < clipped["snips"] < 1 and clipped["ess"] < 10
    assert rank.clipped_ips([])["ips"] is None


def test_ips_is_unbiased_for_the_chance_when_nothing_is_clipped():
    rng = random.Random(1)
    true = 0.3  # the candidate is opened this often, wherever it goes
    samples = []
    for _ in range(20000):
        p = rng.choice((0.05, 0.1, 0.2))
        if rng.random() < p:  # explored with chance p: only then is it observed
            samples.append((1.0 if rng.random() < true else 0.0, p, 4))
    # an observed sample stands for 1 / p of its kind: the weighted mean over the draws is the true reward
    assert rank.clipped_ips(samples, clip=0.01)["snips"] == pytest.approx(true, abs=0.03)


def series(rng, n, hit, tokens, ms):
    return {
        "hit@5": [1.0 if rng.random() < hit else 0.0 for _ in range(n)],
        "tokens_per_file": [rng.gauss(tokens, 20) for _ in range(n)],
        "p95_ms": [abs(rng.gauss(ms, 4)) for _ in range(n)],
    }


def paired(base, hit_gain=0.0, token_gain=0.0, ms_gain=0.0, seed=2):
    """``base`` with each question moved a little the same way: a paired change the noise of the questions hides from
    an unpaired test."""
    rng = random.Random(seed)
    out = {k: list(v) for k, v in base.items()}
    for i in range(len(out["hit@5"])):
        if hit_gain > 0 and out["hit@5"][i] == 0 and rng.random() < hit_gain:
            out["hit@5"][i] = 1.0
        if hit_gain < 0 and out["hit@5"][i] == 1 and rng.random() < -hit_gain:
            out["hit@5"][i] = 0.0
        out["tokens_per_file"][i] -= token_gain
        out["p95_ms"][i] -= ms_gain
    return out


def test_a_candidate_better_on_one_axis_and_no_worse_on_the_others_is_chosen():
    base = series(random.Random(0), 200, 0.6, 400, 20)
    runs = {"now": base, "better": paired(base, hit_gain=0.5), "same": paired(base)}
    found = opt.pareto_select("now", runs, samples=400)
    assert found["choice"] == "better" and found["changed"]
    assert "better" in found["front"] and found["rejected"] == {}


def test_a_candidate_that_degrades_any_axis_beyond_the_noise_is_never_applied():
    base = series(random.Random(0), 200, 0.6, 400, 20)
    # much faster and cheaper, but it loses hits: the front would take it, the gate does not
    runs = {"now": base, "fast": paired(base, hit_gain=-0.4, token_gain=60, ms_gain=8)}
    found = opt.pareto_select("now", runs, samples=400)
    assert found["choice"] == "now" and not found["changed"]
    assert "hit@5" in found["rejected"]["fast"]


def test_a_difference_inside_the_noise_keeps_the_current_configuration():
    base = series(random.Random(3), 60, 0.6, 400, 20)
    runs = {"now": base, "twin": paired(base, hit_gain=0.02, seed=5)}
    assert opt.pareto_select("now", runs, samples=400)["choice"] == "now"


def test_the_bootstrap_is_paired_and_seeded_and_refuses_other_questions():
    base = series(random.Random(0), 50, 0.5, 300, 15)
    other = paired(base, token_gain=10)
    first = opt.paired_bootstrap(base, other, samples=100)
    assert first == opt.paired_bootstrap(base, other, samples=100)
    assert (
        first["tokens_per_file"]["delta"] == pytest.approx(10.0, abs=1e-6) and first["tokens_per_file"]["better"] == 1.0
    )
    short = {k: v[:10] for k, v in other.items()}
    with pytest.raises(ValueError):
        opt.paired_bootstrap(base, short)
