import pytest

pytest.importorskip("sqlalchemy", reason="the runs listing needs the hub-server extra, evo-ak[hub-server]")

from evo_agents.hub.server.runs.service.listing import plan_progress  # noqa: E402


def test_progress_names_the_step_in_progress():
    plan = {"steps": [{"id": "a", "status": "done"}, {"title": "Build", "status": "in_progress"}, {"id": "c"}]}
    found = plan_progress(plan)
    assert (found.done, found.total, found.percent) == (1, 3, 33)
    assert (found.current_step, found.current_status) == ("Build", "in_progress")


def test_progress_falls_back_to_first_pending_and_ends_complete():
    assert plan_progress({"steps": [{"id": "a", "status": "done"}, {"id": "b"}]}).current_step == "b"
    done = plan_progress({"steps": [{"id": "a", "status": "done"}]})
    assert (done.percent, done.current_step) == (100, None)
    assert plan_progress({"steps": []}) is None
