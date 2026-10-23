"""The pure parts of the core plugin crm (``evo_agents.core_plugins.crm.rules`` and ``.github``): who is a contact
in what GitHub sends, and how a stage follows from the facts. No Postgres and no network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_agents.core_plugins.crm import github, rules
from evo_agents.core_plugins.crm.rules import Facts

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def day(n: int) -> datetime:
    return T0 + timedelta(days=n)


def issue(number: int, login: str, association: str = "NONE", **extra) -> dict:
    return {
        "number": number,
        "title": extra.pop("title", "A question"),
        "html_url": f"https://github.com/o/r/issues/{number}",
        "user": {"login": login, "type": extra.pop("type", "User")},
        "author_association": association,
        "created_at": "2026-01-02T03:04:05Z",
        "updated_at": "2026-01-03T00:00:00Z",
        **extra,
    }


def test_crm_login_of_reads_a_github_login_in_lower_case():
    assert rules.login_of("@Alice-B") == "alice-b"
    assert rules.login_of(" bao ") == "bao"
    assert rules.login_of("not a login") is None and rules.login_of("-bad") is None and rules.login_of("") is None
    assert rules.profile_url("alice") == "https://github.com/alice"


def test_crm_stages_follow_the_facts_and_never_run_backwards_in_time():
    assert list(rules.reached(Facts(day(0)))) == ["lead"]
    assert list(rules.reached(Facts(day(0), invited_at=day(1)))) == ["lead", "invited"]
    full = rules.reached(Facts(day(0), day(1), day(2), day(25)))
    assert list(full) == ["lead", "invited", "activated", "retained"] and full["retained"] == day(25)
    # a run without a grant or an invite implies both, at the run's time; the lead is never later than any of them
    implied = rules.reached(Facts(day(5), None, day(2)))
    assert implied["invited"] == implied["activated"] == day(2) and implied["lead"] == day(2)
    assert rules.stage_of(full) == "retained"


def test_crm_the_same_facts_give_the_same_stages_and_a_stored_stage_never_goes_down():
    facts = Facts(day(0), day(1), day(2))
    assert rules.reached(facts) == rules.reached(facts)
    times = rules.reached(facts)
    assert rules.advance("lead", times) == "activated"
    assert rules.advance("retained", times) == "retained"


def test_crm_the_retention_window_is_day_21_up_to_day_28_after_the_first_run():
    start, end = rules.window(day(10))
    assert (start, end) == (day(31), day(38))


def test_crm_an_author_who_is_no_member_is_a_contact_and_a_member_a_bot_and_a_reply_are_not():
    items = [
        issue(1, "Alice"),
        issue(2, "owner", "OWNER"),
        issue(3, "mate", "MEMBER"),
        issue(4, "helper", "COLLABORATOR"),
        issue(5, "dependabot", type="Bot"),
        issue(6, "Bao", "CONTRIBUTOR", pull_request={"url": "x"}),
        issue(7, "Chi", title="Invite: please add me"),
        issue(8, "not a login!"),
        {"title": "no number", "user": {"login": "dan"}},
    ]
    found = github.parse_issues(items)
    assert [(t.login, t.kind, t.ref) for t in found] == [
        ("alice", "issue", "issue#1"),
        ("bao", "pull_request", "pr#6"),
        ("chi", "invite_request", "issue#7"),
    ]
    assert found[0].happened_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert found[0].updated_at == datetime(2026, 1, 3, tzinfo=UTC) and found[0].url.endswith("/issues/1")


def test_crm_a_discussion_author_is_a_contact_unless_a_member_or_a_bot():
    def node(number: int, login: str, association: str = "NONE", kind: str = "User") -> dict:
        return {
            "number": number,
            "url": f"https://github.com/o/r/discussions/{number}",
            "createdAt": "2026-02-01T00:00:00Z",
            "updatedAt": "2026-02-02T00:00:00Z",
            "author": {"login": login, "__typename": kind},
            "authorAssociation": association,
        }

    nodes = [node(1, "Eve"), node(2, "owner", "OWNER"), node(3, "robot", kind="Bot"), {"number": 4, "author": None}]
    assert [(t.login, t.kind, t.ref) for t in github.parse_discussions(nodes)] == [
        ("eve", "discussion", "discussion#1")
    ]
