"""One line of proof for each acceptance criterion a step names (step 4 of the super-board-parity-2 plan).

A step whose ``acceptance`` names criteria of the plan's (``[a4, a9]``) is reported done with one proof per
criterion: a test, a command or ``n/a`` with a reason. Its prompts state the criteria in the plan's words and how to
report them (``golden/step-prompts-acceptance.json``, a new golden beside ``step-prompts-0.3.0.json``, which a step
without acceptance still matches); the plan's step keeps the proofs; and a Judge reads the criterion with the locator
of its proof, never the Builder's reason. Without Postgres or a server."""

import copy
import json
from pathlib import Path

import pytest

from evo_agents.harness import load_schema, load_yaml
from evo_agents.hub import judge, merge_gate, proofs, record_rule
from evo_agents.hub.plans import step_key
from evo_agents.hub.runs import build_prompt, build_step_session_prompt, plan_repo
from evo_agents.hub.server.runs.models import StepProof, StepReport
from evo_agents.schema import errors, validate

FIXTURE = Path(__file__).parent / "fixtures" / "runs" / "ready-steps.yaml"
GOLDEN = Path(__file__).parent / "golden" / "step-prompts-acceptance.json"
GOLDEN_030 = Path(__file__).parent / "golden" / "step-prompts-0.3.0.json"
A4 = "Each criterion of the step has a line of proof when it is reported done."
A9 = "The contract golden is regenerated."
SECRET_REASON = "trust me, nothing to see here"


def plan_with_acceptance() -> dict:
    plan = load_yaml(FIXTURE)
    plan["acceptance"] = [
        {"id": "a4", "criterion": A4, "verify": "pytest -k acceptance_proof"},
        {"id": "a9", "criterion": A9, "verify": "python -m evo_agents hub contract print", "invariant": True},
        {"id": "a12", "criterion": "Not named by the step."},
    ]
    target = step(plan, 2)
    target["acceptance"] = ["a4", "a9", "a77"]  # a77 is not a criterion of the plan: no proof for it
    return plan


def step(plan: dict, key) -> dict:
    return next(s for i, s in enumerate(plan["steps"]) if step_key(s, i) == str(key))


def acceptance_prompts() -> dict[str, str]:
    plan = plan_with_acceptance()
    target = step(plan, 2)
    return {
        "step-run": build_prompt(plan, target, plan_repo(plan, target)),
        "step-session": build_step_session_prompt(plan, target, plan.get("repos") or [], {}),
    }


# Parsing a proof


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("a4=tests/hub/test_x.py::test_y", {"id": "a4", "kind": "test", "locator": "tests/hub/test_x.py::test_y"}),
        ("a4=tests/a.py::Case::test_b[1-x]", {"id": "a4", "kind": "test", "locator": "tests/a.py::Case::test_b[1-x]"}),
        (
            "a9=git diff --exit-code tests/golden",
            {"id": "a9", "kind": "command", "locator": "git diff --exit-code tests/golden"},
        ),
        (
            "a9 = n/a: an invariant this step leaves alone",
            {"id": "a9", "kind": "n/a", "reason": "an invariant this step leaves alone"},
        ),
    ],
)
def test_acceptance_proof_parses_a_test_a_command_and_n_a(given, expected):
    assert proofs.parse(given) == expected


@pytest.mark.parametrize("given", ["a4", "x1=pytest", "a4=", "a4=n/a", "a4=n/a:  ", "a4=" + "x" * 2001])
def test_acceptance_proof_refuses_a_line_that_is_not_one(given):
    with pytest.raises(proofs.ProofProblem):
        proofs.parse(given)


def test_acceptance_proof_refuses_two_lines_for_one_criterion():
    with pytest.raises(proofs.ProofProblem, match="a4 has more than one proof"):
        proofs.parse_all(["a4=pytest", "a4=n/a: twice"])


def test_acceptance_proof_from_a_result_file():
    assert proofs.from_result({"summary": "s"}) is None
    assert proofs.from_result({"proofs": {"a4": "pytest -q", "a9": "n/a: untouched"}}) == [
        {"id": "a4", "kind": "command", "locator": "pytest -q"},
        {"id": "a9", "kind": "n/a", "reason": "untouched"},
    ]
    for bad in ({"proofs": ["a4=pytest"]}, {"proofs": {"a4": 1}}, {"proofs": {"step": "pytest"}}):
        with pytest.raises(proofs.ProofProblem):
            proofs.from_result(bad)


# The criteria a step names


def test_acceptance_proof_criteria_are_the_plans_ids_the_step_names():
    plan = plan_with_acceptance()
    assert proofs.criteria(plan, step(plan, 2)) == [("a4", A4), ("a9", A9)]
    assert proofs.criteria(plan, step(plan, 3)) == []
    sentences = {"acceptance": ["The page loads in a second.", "a4 and a4 again"]}
    assert proofs.criteria(plan, sentences) == [("a4", A4)]
    assert proofs.criteria({"steps": []}, step(plan, 2)) == []


def test_acceptance_proof_a_missing_line_is_refused_and_named():
    wanted = [("a4", A4), ("a9", A9)]
    why = proofs.problem(wanted, [proofs.parse("a4=pytest -q")])
    assert why and "1 has none" in why and f"a9: {A9}" in why and "a4:" not in why
    why = proofs.problem(wanted, [])
    assert why and "2 has none" in why and "a4: " in why and "a9: " in why
    assert proofs.problem(wanted, proofs.parse_all(["a4=pytest", "a9=n/a: untouched"])) is None


def test_acceptance_proof_of_a_criterion_the_step_does_not_name_is_refused():
    assert "not a12" in proofs.problem([("a4", A4)], proofs.parse_all(["a4=pytest", "a12=pytest"]))
    assert "names no criterion" in proofs.problem([], [proofs.parse("a4=pytest")])
    assert proofs.problem([], []) is None


def test_acceptance_proof_of_a_test_must_be_in_the_worktree(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_it():\n    pass\n", encoding="utf-8")
    held = proofs.parse("a4=tests/test_a.py::test_it[1]")
    assert proofs.missing_test(held, tmp_path) is None
    assert "no test named test_other" in proofs.missing_test(proofs.parse("a4=tests/test_a.py::test_other"), tmp_path)
    assert "not a file" in proofs.missing_test(proofs.parse("a4=tests/test_b.py::test_it"), tmp_path)
    assert "not a file" in proofs.missing_test(proofs.parse("a4=../outside.py::test_it"), tmp_path)
    assert proofs.missing_test(proofs.parse("a4=pytest -q"), tmp_path) is None


# Where the proofs are kept


def test_acceptance_proof_the_plan_schema_keeps_proofs_on_a_step():
    plan = plan_with_acceptance()
    target = step(plan, 2)
    target["status"] = "done"
    target["evidence"] = "Free text stays."
    target["proofs"] = proofs.stored(proofs.parse_all(["a4=tests/x.py::test_y", "a9=n/a: untouched"]))
    assert errors(validate(plan, load_schema("plan"))) == []
    target["proofs"] = [{"id": "a4", "kind": "guess", "locator": "x"}]
    assert errors(validate(plan, load_schema("plan")))
    target["proofs"] = [{"id": "a4", "kind": "test", "locator": "x", "prose": "y"}]
    assert errors(validate(plan, load_schema("plan")))


def test_acceptance_proof_a_step_report_takes_them_structured():
    report = StepReport.model_validate(
        {"status": "done", "proofs": [{"id": "a4", "kind": "test", "locator": "t.py::test_x"}]}
    )
    assert report.proofs and report.proofs[0].locator == "t.py::test_x"
    assert StepReport.model_validate({"status": "done", "evidence": "older worker"}).proofs is None
    for bad in (
        {"id": "a4", "kind": "n/a"},
        {"id": "a4", "kind": "command"},
        {"id": "step", "kind": "command", "locator": "x"},
        {"id": "a4", "kind": "command", "locator": "two\nlines"},
    ):
        with pytest.raises(ValueError):
            StepProof.model_validate(bad)


# What the prompts say


def test_acceptance_proof_prompts_state_the_criteria_and_how_to_report():
    """Changed on purpose against step-prompts-0.3.0: a new golden holds the prompts of a step with acceptance."""
    made = acceptance_prompts()
    assert made == json.loads(GOLDEN.read_text(encoding="utf-8"))
    for prompt in made.values():
        assert "## Acceptance criteria of the step" in prompt
        assert f"- a4: {A4}" in prompt and f"- a9: {A9}" in prompt
        assert "a12" not in prompt and "a77" not in prompt
    assert '"proofs"' in made["step-run"] and proofs.result_how() in made["step-run"]
    assert proofs.worker_how() in made["step-session"]


def test_acceptance_proof_a_step_without_acceptance_keeps_the_0_3_0_prompt():
    golden = json.loads(GOLDEN_030.read_text(encoding="utf-8"))
    plan = plan_with_acceptance()
    target = step(plan, 3)
    assert "Acceptance criteria" not in build_prompt(plan, target, plan_repo(plan, target))
    assert all("Acceptance criteria" not in prompt for prompt in golden.values())


def test_acceptance_proof_the_record_rule_tells_a_plan_run_how_to_report():
    rule = record_rule.record_rule("4", "the step")
    assert "--proof ID=VALUE" in rule and "path::test_name" in rule and "n/a: why" in rule


# What a Judge reads


def test_acceptance_proof_the_judge_reads_the_criterion_and_the_locator_alone():
    plan = plan_with_acceptance()
    target = copy.deepcopy(step(plan, 2))
    target["evidence"] = "The Builder's prose about how well it went."
    target["proofs"] = [
        {"id": "a4", "kind": "test", "locator": "tests/hub/test_acceptance_proof.py::test_x"},
        {"id": "a9", "kind": "n/a", "reason": SECRET_REASON},
    ]
    text = merge_gate._step_text(target, plan)
    assert f"- a4: {A4}" in text and f"- a9: {A9}" in text
    assert "Proof: test tests/hub/test_acceptance_proof.py::test_x" in text
    assert "Proof: n/a (the Builder says it does not apply)" in text
    assert SECRET_REASON not in text and "Builder's prose" not in text


def test_acceptance_proof_the_judge_still_reads_a_step_of_sentences_as_written():
    text = merge_gate._step_text({"id": 1, "title": "t", "acceptance": ["The page loads."]}, {})
    assert "Acceptance:\n- The page loads." in text


def test_acceptance_proof_the_curator_judge_reads_the_criterion_and_the_locator_alone():
    plan = plan_with_acceptance()
    step(plan, 2)["proofs"] = [
        {"id": "a4", "kind": "command", "locator": "pytest -q -k acceptance_proof"},
        {"id": "a9", "kind": "n/a", "reason": SECRET_REASON},
    ]
    proposal = {"id": 7, "title": "t", "kind": "fix", "tier": 1}
    prompt = judge.build_judge_prompt(
        "p", proposal, plan, "evo-agents", branch="b", base="main", head="abc", pr_url=None
    )
    assert f"- a4: {A4}" in prompt and "Proof: command `pytest -q -k acceptance_proof`" in prompt
    assert SECRET_REASON not in prompt
