"""The claim of a run under an agent profile that is not its project's default (``runs.AGENT_HOME``,
``runs.service.profiles.claim_clause``, ``own_home``): one that runs on Claude Code goes only to a daemon that gives its
agent a config dir of its own, and its claim says ``own_home``; a run under the project's default profile, and one
that runs on another runtime, go to any daemon as before. Each run goes to a node of its agent's pool (plan
agent-dedicated-pools), where the worker is moved for the run of each agent, and takes its model account from the
agent's account pool, so the claim of every run of an agent says ``own_home``, the default agent's too
(``pool_accounts.is_pooled``); the default agent's says ``owner_setup`` as well, so its agent keeps the owner's setup."""

import pytest

from tests.hub import pg

if not pg.DSN:
    pytest.skip(pg.SKIP_REASON, allow_module_level=True)

from evo_agents.hub import runs
from tests.hub import test_credentials_api as credentials_api
from tests.hub.test_credentials_api import CHECKOUTS, worker_of
from tests.hub.test_profile_runs import BOTH, admin, allow, beat, dispatched, profile
from tests.hub.test_runs import add_worker, claim, join_pool, report

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
    profile(client, admin["owner"], "reviewer")
    worker = join_pool(client, worker_of(client, admin["owner"], "mac-mini"), "reviewer")
    beat(client, worker, features=OLD)
    run = dispatched(client, admin["owner"], profile="reviewer")
    assert claim(client, worker) is None
    beat(client, worker, features=FEATURES)
    spec = claim(client, worker)
    assert spec["id"] == run["id"] and spec["runtime"] == "claude-code"
    assert spec["own_home"] is True and spec["profile"]["handle"] == "reviewer"
    assert "owner_setup" not in spec, "an agent of another profile gets none of the owner's setup"


def test_claim_of_a_default_profile_run_needs_no_feature_agent_home_and_says_own_home(client, admin, hub_db):
    worker = worker_of(client, admin["owner"], "mac-mini")
    beat(client, worker, features=OLD)
    plain = dispatched(client, admin["owner"])
    spec = claim(client, worker)
    assert spec["id"] == plain["id"] and spec["own_home"] is True, "its account is its account pool's"
    assert spec["owner_setup"] is True, "the default agent keeps the owner's setup, its login aside"
    done(client, worker, plain["id"])
    beat(client, worker, features=FEATURES)
    again = dispatched(client, admin["owner"], "lumen-kb-docs")
    spec = claim(client, worker)
    assert spec["id"] == again["id"] and spec["own_home"] is True


def test_claim_of_another_profile_run_on_codex_needs_no_feature_agent_home(client, admin, hub_db):
    profile(client, admin["owner"], "reviewer")
    profile(client, admin["owner"], "codex-only", permissions=allow(runtime=["codex"]))
    both = add_worker(client, admin["owner"], "both-box", runtimes=BOTH, checkouts=CHECKOUTS, pool="reviewer")
    beat(client, both, features=OLD)
    asked = dispatched(client, admin["owner"], runtime="codex", profile="reviewer")
    spec = claim(client, both)
    assert (spec["id"], spec["runtime"], spec["own_home"]) == (asked["id"], "codex", True)
    done(client, both, asked["id"])
    # asking any runtime, the claim picks codex, the profile's only one
    join_pool(client, both, "codex-only")
    anyone = dispatched(client, admin["owner"], "lumen-kb-docs", profile="codex-only")
    spec = claim(client, both)
    assert (spec["id"], spec["runtime"]) == (anyone["id"], "codex")
    done(client, both, anyone["id"])
    # asking any runtime under a profile that allows all, the claim would pick claude-code: not for an old daemon
    join_pool(client, both, "reviewer")
    claude = dispatched(client, admin["owner"], profile="reviewer")
    assert claim(client, both) is None
    beat(client, both, features=FEATURES)
    spec = claim(client, both)
    assert (spec["id"], spec["runtime"]) == (claude["id"], "claude-code")
