"""The claim of a run under an agent profile that is not its project's default (``runs.AGENT_HOME``,
``runs.service.profiles.claim_clause``, ``own_home``): one that runs on Claude Code goes only to a daemon that gives its
agent a config dir of its own, and its claim says ``own_home``; a run under the project's default profile, and one
that runs on another runtime, go to any daemon as before."""

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from evo_agents.hub import runs
from tests.hub import test_credentials_api as credentials_api
from tests.hub.test_credentials_api import CHECKOUTS, worker_of
from tests.hub.test_profile_runs import BOTH, admin, allow, beat, dispatched, profile
from tests.hub.test_runs import add_worker, claim, report

app_key, config, hub, client = (
    credentials_api.app_key,
    credentials_api.config,
    credentials_api.hub,
    credentials_api.client,
)
__all__ = ["admin"]

OLD = [feature for feature in runs.WORKER_FEATURES if feature != runs.AGENT_HOME]  # a daemon from before the feature
FEATURES = list(runs.WORKER_FEATURES)


def done(client, worker, run_id: int) -> None:
    assert report(client, worker, run_id, "failed", error="done").status_code == 200


def test_claim_of_a_claude_run_of_another_profile_needs_the_feature_agent_home(client, admin, hub_db):
    worker = worker_of(client, admin["owner"], "mac-mini")
    beat(client, worker, features=OLD)
    profile(client, admin["owner"], "reviewer")
    run = dispatched(client, admin["owner"], profile="reviewer")
    assert claim(client, worker) is None
    beat(client, worker, features=FEATURES)
    spec = claim(client, worker)
    assert spec["id"] == run["id"] and spec["runtime"] == "claude-code"
    assert spec["own_home"] is True and spec["profile"]["handle"] == "reviewer"


def test_claim_of_a_default_profile_run_needs_no_feature_agent_home_and_has_no_own_home(client, admin, hub_db):
    worker = worker_of(client, admin["owner"], "mac-mini")
    beat(client, worker, features=OLD)
    plain = dispatched(client, admin["owner"])
    spec = claim(client, worker)
    assert spec["id"] == plain["id"] and spec["own_home"] is False
    done(client, worker, plain["id"])
    beat(client, worker, features=FEATURES)
    again = dispatched(client, admin["owner"], "lumen-kb-docs")
    spec = claim(client, worker)
    assert spec["id"] == again["id"] and spec["own_home"] is False


def test_claim_of_another_profile_run_on_codex_needs_no_feature_agent_home(client, admin, hub_db):
    both = add_worker(client, admin["owner"], "both-box", runtimes=BOTH, checkouts=CHECKOUTS)
    beat(client, both, features=OLD)
    profile(client, admin["owner"], "reviewer")
    profile(client, admin["owner"], "codex-only", permissions=allow(runtime=["codex"]))
    asked = dispatched(client, admin["owner"], runtime="codex", profile="reviewer")
    spec = claim(client, both)
    assert (spec["id"], spec["runtime"], spec["own_home"]) == (asked["id"], "codex", True)
    done(client, both, asked["id"])
    # asking any runtime, the claim picks codex, the profile's only one
    anyone = dispatched(client, admin["owner"], "lumen-kb-docs", profile="codex-only")
    spec = claim(client, both)
    assert (spec["id"], spec["runtime"]) == (anyone["id"], "codex")
    done(client, both, anyone["id"])
    # asking any runtime under a profile that allows all, the claim would pick claude-code: not for an old daemon
    claude = dispatched(client, admin["owner"], profile="reviewer")
    assert claim(client, both) is None
    beat(client, both, features=FEATURES)
    spec = claim(client, both)
    assert (spec["id"], spec["runtime"]) == (claude["id"], "claude-code")
