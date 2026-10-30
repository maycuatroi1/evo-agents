"""A merge approval bound to a head and a scope (``evo_agents.hub.approval_heads``, plan super-board-parity-2,
criterion a3): the scope's hash, when an approval holds, and what a decision of category merge takes. These tests need
no Postgres; ``test_approval_heads_routes`` has the routes and the gate."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from evo_agents.hub import approval_heads
from evo_agents.hub.server.decision_models import DecisionAsk

HEAD, NEW_HEAD = "a" * 40, "b" * 40
FILES = ["app/models.py", "app/views.py"]
OPTIONS = [{"key": "merge", "label": "Merge it"}, {"key": "wait", "label": "Not yet"}]


def test_approval_head_scope_hashes_files_migrations_and_needs_you_lines():
    scope = approval_heads.scope_of(FILES, "Adds a view.")
    assert approval_heads.SCOPE.match(scope)
    assert approval_heads.scope_of([*reversed(FILES), FILES[0]], "Adds a view.") == scope, "order and repeats"
    assert approval_heads.scope_of(FILES, "Adds a view, with more words.") == scope, "prose is not scope"
    assert approval_heads.scope_of([*FILES, "app/forms.py"], "Adds a view.") != scope
    assert approval_heads.scope_of([*FILES, "hub/migrations/versions/0009_x.py"]) != approval_heads.scope_of(FILES)
    assert approval_heads.scope_of(FILES, "Adds a view.\nneeds-you: rotate the key") != scope
    assert approval_heads.migrations(["db/schema.sql", "alembic/versions/1.py", "app/x.py"]) == [
        "alembic/versions/1.py",
        "db/schema.sql",
    ]
    assert approval_heads.needs_you("a\n - [ ] Needs you: approve the bill\nb") == ["- [ ] Needs you: approve the bill"]


def test_approval_head_holds_for_its_head_and_scope_alone():
    scope = approval_heads.scope_of(FILES)
    kept = approval_heads.approval(HEAD.upper(), scope, "acme/app#12")
    assert kept == {"pull": "acme/app#12", "head": HEAD, "scope": scope}
    assert approval_heads.holds(kept, HEAD, scope)
    assert not approval_heads.holds(kept, NEW_HEAD, scope)
    assert not approval_heads.holds(kept, HEAD, approval_heads.scope_of([*FILES, "app/forms.py"]))
    assert not approval_heads.holds(kept, None, None), "a head that could not be read opens no bound approval"
    assert approval_heads.stale(kept, NEW_HEAD, scope) == (
        f"it was given for head {HEAD[:12]}, and the head is {NEW_HEAD[:12]} now"
    )
    assert "scope" in approval_heads.stale(kept, HEAD, None)
    assert approval_heads.stale(kept, HEAD, scope) == ""


def test_approval_head_a_decision_without_one_holds_as_before():
    assert approval_heads.holds(None, None, None) and approval_heads.holds({}, NEW_HEAD, "x")
    assert approval_heads.approval(None, "x") is None and approval_heads.line(None) == ""


def test_approval_head_a_merge_decision_carries_it_into_its_context():
    scope = approval_heads.scope_of(FILES)
    asked = DecisionAsk(
        category="merge",
        question="Merge #12 of acme/app?",
        context="It adds a view.",
        options=OPTIONS,
        approval={"pull": "acme/app#12", "head": HEAD, "scope": scope},
    )
    assert asked.stored_extras()["approval"] == {"pull": "acme/app#12", "head": HEAD, "scope": scope}
    context = asked.stored_context()
    assert context.startswith("It adds a view.\n\nThis answer holds for head aaaaaaaaaaaa of acme/app#12")
    assert "asks again" in context
    plain = DecisionAsk(category="merge", question="Merge?", options=OPTIONS)
    assert plain.stored_extras()["approval"] is None and plain.stored_context() is None


@pytest.mark.parametrize(
    "approval",
    [
        {"head": "not-a-sha"},
        {"head": HEAD, "scope": "md5:abc"},
        {"head": HEAD, "pull": "acme/app#0"},
    ],
)
def test_approval_head_a_malformed_one_is_refused(approval):
    with pytest.raises(ValidationError):
        DecisionAsk(category="merge", question="Merge?", options=OPTIONS, approval=approval)


def test_approval_head_binds_a_merge_decision_alone():
    with pytest.raises(ValidationError, match="an approval binds a decision of category merge"):
        DecisionAsk(category="deploy", question="Deploy?", options=OPTIONS, approval={"head": HEAD})
