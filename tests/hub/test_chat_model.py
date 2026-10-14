"""The rules of group chats that need no hub (``evo_agents.hub.chats``): which names a message mentions, what one
emoji is, and which reaction a run of an agent puts on the message that started it as it moves."""

import pytest

from evo_agents.hub import chats, runs


@pytest.mark.parametrize(
    ("text", "names"),
    [
        ("@reviewer please look", ["reviewer"]),
        ("@Someone-Else and @REVIEWER", ["someone-else", "reviewer"]),
        ("@reviewer, then @reviewer again, then @evo-agents.", ["reviewer", "evo-agents"]),
        ("(@reviewer) [@other] @last!", ["reviewer", "other", "last"]),
        ("mail owner@example.org or a@b", []),  # an e-mail address is no mention
        ("see https://github.com/@someone and x.@y and a-@b and @@twice", []),
        ("`@reviewer` and ``code @x``", []),  # code spans
        ("```\n@reviewer in a fence\n```\nafter @other", ["other"]),
        ("~~~python\n@decorator\n~~~", []),
        ("```\nnever closed @reviewer", []),
        ("@-dash @_under @9lives", ["9lives"]),
        ("no mention at all", []),
    ],
)
def test_chat_mention_names_are_read_as_the_parser_rules_say(text, names):
    assert chats.mention_names(text) == names


def test_a_chat_message_mentions_at_most_twenty_names():
    text = " ".join(f"@agent-{n}" for n in range(30))
    assert chats.mention_names(text) == [f"agent-{n}" for n in range(chats.MAX_MENTIONS)]


@pytest.mark.parametrize(
    "emoji",
    [
        "\U0001f44d",  # thumbs up
        "\U0001f44d\U0001f3fd",  # with a skin tone
        "❤️",  # heart with the emoji presentation
        "\U0001f468‍\U0001f469‍\U0001f467",  # a family joined with ZWJ
        "\U0001f1fb\U0001f1f3",  # a flag of two regional indicators
        "1️⃣",  # a keycap
        chats.EYES,
        chats.CHECK,
        chats.CROSS,
    ],
)
def test_a_chat_reaction_takes_one_emoji(emoji):
    assert chats.emoji_problem(emoji) is None


@pytest.mark.parametrize(
    "text",
    ["", "ok", "1", "#", " \U0001f44d", "\U0001f44d ", "<b>", "‮\U0001f44d", "\U0001f44d" * 17, "‍"],
)
def test_a_chat_reaction_refuses_what_is_not_an_emoji(text):
    assert chats.emoji_problem(text) is not None


def test_chat_status_reactions_follow_the_state_of_a_run_by_rule():
    assert chats.status_reaction("leased") == (chats.EYES, ())
    assert chats.status_reaction("done") == (chats.CHECK, (chats.EYES,))
    assert chats.status_reaction("failed") == (chats.CROSS, (chats.EYES,))
    assert chats.status_reaction("cancelled") == (chats.CROSS, (chats.EYES,))
    for state in ("queued", "running", "waiting", "parked", "lost", "review"):
        assert chats.status_reaction(state) is None
    assert set(chats.STATUS_REACTIONS) <= set(runs.RUN_STATES)


def test_chat_notices_are_kinds_of_notice_and_link_to_the_chat():
    assert {chats.NOTICE_MENTION, chats.NOTICE_ADDED} <= set(runs.NOTICE_KINDS)
    assert chats.chat_link(7) == "/chats/7"
    assert chats.chat_link(7, 42) == "/chats/7?message=42"
