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


# Chat runs (evo_agents.hub.chat_runs)


def _message(number: int, body: str = "", author: str = "owner") -> dict:
    return {"id": number, "author": author, "at": "2026-10-14 08:00 UTC", "body": body or f"message {number}"}


def test_a_chat_run_snapshot_keeps_the_first_message_and_leaves_out_the_oldest_first():
    from evo_agents.hub import chat_runs

    root = _message(50, "The wait helper sleeps 5 seconds.")
    before = [_message(number) for number in range(1, 15)]  # 14: the 10 latest are shown, the rest counted
    thread = [_message(number, "x" * 1000) for number in range(51, 81)]  # 30 of 1000 bytes each: over 12 KB
    blocks = chat_runs.snapshot(root, thread, before)
    text = "\n".join(blocks)
    assert len(text.encode()) <= chat_runs.SNAPSHOT_BYTES
    assert "[#50] owner, 2026-10-14 08:00 UTC:\nThe wait helper sleeps 5 seconds." in text, "the first message stays"
    assert blocks[0].startswith("[") and blocks[0].endswith("older message(s) of the chat are left out of this prompt]")
    left = int(blocks[0][1:].split()[0])
    shown = [block for block in blocks if block.startswith("[#")]
    assert left + len(shown) == len(before) + len(thread) + 1, "every message is shown or counted"
    assert "[#80]" in text and "[#1]" not in text, "the latest stay, the oldest go first"
    small = chat_runs.snapshot(None, [], [_message(3), _message(4)])
    assert small == ["## The chat before the thread", *(chat_runs.render_message(_message(n)) for n in (3, 4))]


def test_a_chat_run_prompt_has_the_rules_the_repos_and_the_message_whole():
    from evo_agents.hub import chat_runs

    trigger = _message(60, "@reviewer replace the sleep with a poll\n" + "detail " * 2000)
    prompt = chat_runs.build_chat_prompt(
        agent={"handle": "reviewer", "name": "Reviewer", "project": "evo-agents", "description": "Reviews diffs."},
        chat={"id": 7, "title": "Release talk", "people": ["owner", "someone-else"], "agents": ["reviewer", "fixer"]},
        trigger=trigger,
        login="owner",
        hop=1,
        branch="chat/41",
        budget_usd=5.0,
        blocks=chat_runs.snapshot(None, [], [_message(1, "earlier")]),
        repos=[{"repo": "evo-agents", "branch": "chat/41"}, {"repo": "agent-skills", "branch": "chat/41"}],
        worktrees={"agent-skills": "agent-skills-2"},
        left_out=["evo-cli"],
    )
    assert prompt.startswith("You are @reviewer (Reviewer), an agent of project evo-agents on the evo-agents hub, in")
    assert (
        "Reviews diffs." in prompt and "People of the chat: owner, someone-else. Agents: @reviewer, @fixer." in prompt
    )
    assert "- evo-agents: evo-agents/\n- agent-skills: agent-skills-2/" in prompt
    assert "Repos of the project this worker has no checkout of: evo-cli." in prompt
    for command in ("post", "react", "create", "mention"):
        assert f"evo-agents worker chat {command}" in prompt
    assert "at hop 2 of at most 3, never to yourself (@reviewer)" in prompt and "$5.00 for 24 hours" in prompt
    assert prompt.index("# The chat, shortened") < prompt.index("# The message to answer")
    assert len(prompt.encode()) <= runs.MAX_PROMPT_BYTES and "detail detail" in prompt


def test_chat_run_rules_of_hops_budgets_branches_and_the_inbox():
    from evo_agents.hub import chat_runs

    assert chat_runs.hop_problem(2, False) is None and chat_runs.hop_problem(3, False) is None
    assert "hop 4 from the person's message" in chat_runs.hop_problem(4, False)
    assert chat_runs.hop_problem(2, True) == "an agent does not mention itself: it goes on in its own session"
    assert chat_runs.run_budget(4.25) == {"max_usd": 4.25, "max_seconds": chat_runs.TIME_CAP_SECONDS}
    assert chat_runs.run_budget(-1) == {"max_usd": 0.0, "max_seconds": chat_runs.TIME_CAP_SECONDS}
    assert chat_runs.branch_of(41) == "chat/41" and chat_runs.is_chat_branch("chat/41")
    assert not any(chat_runs.is_chat_branch(name) for name in ("chat/", "chat/x", "chat/41/a", "main", None))
    text = chat_runs.inbox_text("owner", 9, "  also the retries \n" + "y" * 10_000)
    assert text.startswith("owner wrote in the thread (message #9):\nalso the retries")
    assert len(text.encode()) <= runs.MAX_MESSAGE_BYTES
    assert chat_runs.pull_text("evo-agents", "chat/41", "https://x/pull/1", pull=True, opened=True).startswith(
        "Opened a draft pull request of evo-agents from chat/41"
    )
    assert "did not open its draft pull request: GitHub said no" in chat_runs.pull_text(
        "evo-agents", "chat/41", None, pull=False, opened=False, problem="GitHub said no"
    )


@pytest.mark.parametrize(
    ("texts", "named"),
    [
        (["look at evo-agents too"], ["evo-agents"]),
        (["EVO-AGENTS and Notes."], ["evo-agents", "notes"]),
        (["the evo-agents-harness says so"], ["evo-agents-harness"]),  # not evo-agents, which it starts with
        (["clone maycuatroi1/evo-agents.git", None], ["evo-agents"]),
        (["my-notes and notes_old and anotes"], []),
        (["", "m1.kb fails"], []),
    ],
)
def test_a_chat_run_names_a_repo_as_a_name_of_its_own(texts, named):
    from evo_agents.hub import chat_runs

    names = ["evo-agents-harness", "evo-agents", "lumen-kb-docs", "notes"]
    assert chat_runs.named_repos(names, texts) == named


def test_a_lazy_chat_run_ranks_the_repos_open_then_named_then_the_others_within_its_bytes():
    from evo_agents.hub import chat_runs

    names = ["harness", *(f"repo-{n:02}" for n in range(60))]
    listing = chat_runs.ranked_repos(names, ["harness", "repo-40"], {"harness", "repo-40", "repo-07"}, ["repo-07"])
    assert [item["repo"] for item in listing[:4]] == ["harness", "repo-40", "repo-07", "repo-00"]
    assert listing[:3] == [
        {"repo": "harness", "open": True, "checkout": True, "named": False},
        {"repo": "repo-40", "open": True, "checkout": True, "named": False},
        {"repo": "repo-07", "open": False, "checkout": True, "named": True},
    ]
    assert len(listing) == len(names) and listing[3] == {
        "repo": "repo-00",
        "open": False,
        "checkout": False,
        "named": False,
    }
    trigger = _message(60, "@reviewer look at repo-07\n" + "detail " * 5000)
    prompt = chat_runs.build_chat_prompt(
        agent={"handle": "reviewer", "name": "Reviewer", "project": "evo-agents"},
        chat={"id": 7, "title": "Release talk", "people": ["owner"], "agents": ["reviewer"]},
        trigger=trigger,
        login="owner",
        hop=1,
        branch="chat/41",
        budget_usd=5.0,
        blocks=[],
        listing=listing,
        harness="harness",
    )
    assert len(prompt.encode()) <= runs.MAX_PROMPT_BYTES
    start = prompt.index("Repositories of the project")
    shown = prompt[prompt.index("\n", start) + 1 : prompt.index("\n\n", start)]
    assert len(shown.encode()) <= chat_runs.REPOS_BYTES and "repo-59" not in shown  # the last ones left out first
    assert shown.startswith("- harness: open, checked out on this worker: its worktree is harness/")
    assert (
        "- repo-07: not open, checked out on this worker, named in the chat: `evo-agents worker repo open repo-07`"
        in shown
    )
    assert "The harness.yaml of harness says what each repo of the project is for." in prompt
