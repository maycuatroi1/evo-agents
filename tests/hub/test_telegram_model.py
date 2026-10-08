"""The Telegram channel's model (``evo_agents.hub.telegram``): link codes, the data of buttons, what a restricted
project's message holds, and the messages of a decision, a proposal and a notice. No database, no network."""

import re

import pytest

from evo_agents.hub import telegram

OPTIONS = [
    {"key": "sqlite", "label": "Keep SQLite"},
    {"key": "postgres", "label": "Move to Postgres", "description": "One more service.", "recommended": True},
]


def test_a_link_code_is_32_characters_a_start_parameter_takes_and_the_hub_keeps_its_hash():
    code = telegram.new_code()
    assert len(code) == telegram.LINK_CODE_CHARS == 32 and re.fullmatch(r"[A-Za-z0-9_-]{32}", code)
    assert telegram.new_code() != code
    digest = telegram.code_hash(code)
    assert re.fullmatch(r"[0-9a-f]{64}", digest) and code not in digest
    assert telegram.link_url("evo_hub_bot", code) == f"https://t.me/evo_hub_bot?start={code}"
    assert telegram.LINK_SECONDS == 600


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("/start " + "a" * 32, ("start", "a" * 32)),
        ("/start@evo_hub_bot " + "B-_" + "c" * 29, ("start", "B-_" + "c" * 29)),
        ("/stop", ("stop", None)),
        ("/stop@evo_hub_bot", ("stop", None)),
        ("/start", ("start", None)),
        ("hello", None),
        ("", None),
        (None, None),
    ],
)
def test_commands_are_read_with_or_without_the_bots_name(text, found):
    assert telegram.command(text) == found


def test_only_a_code_of_the_right_shape_is_a_start_code():
    assert telegram.start_code("/start " + "x" * 32) == "x" * 32
    for text in ("/start", "/start " + "x" * 31, "/start " + "x" * 33, "/start " + "x" * 31 + "!", "/stop " + "x" * 32):
        assert telegram.start_code(text) is None


def test_button_data_names_the_item_and_the_choice_within_64_bytes_and_nothing_else_parses():
    data = telegram.decision_callback(123, "postgres")
    assert data == "d:123:postgres"
    assert telegram.parse_callback(data) == telegram.Callback("decision", 123, "postgres")
    longest = telegram.decision_callback(10**17, "k" * 32)
    assert len(longest.encode()) <= telegram.MAX_CALLBACK_BYTES
    assert telegram.parse_callback(telegram.proposal_callback(7, "defer")) == telegram.Callback("proposal", 7, "defer")
    for data in ("p:7:merge", "d:0:a", "x:1:a", "d:1:", "d:1:a b", None, 12, "d:1:" + "a" * 33):
        assert telegram.parse_callback(data) is None


@pytest.mark.parametrize(
    ("clearance", "restricted"),
    [
        ({"level": "internal"}, False),
        ({"level": "internal", "location": "any"}, False),
        ({"level": "customer", "location": "any"}, True),
        ({"level": "internal", "location": "domestic-only"}, True),
        ({"level": "customer", "location": "domestic-only"}, True),
        (None, True),  # no hub sink: fail closed
    ],
)
def test_a_project_cleared_for_customer_or_domestic_only_is_restricted(clearance, restricted):
    assert telegram.restricted(clearance) is restricted


def test_a_restricted_message_holds_the_project_the_kind_and_the_link_only():
    url = "https://hub.test/inbox?decision=5"
    message = telegram.minimal_message("meridai", "decision", None, url, "/inbox?decision=5")
    assert message.text == f"<b>meridai</b>: A decision waits for your answer.\n\n{url}"
    assert message.buttons == [[{"text": "Open on the hub", "url": url}]]
    brief = telegram.minimal_message("meridai", "notice", "curator_brief", None, "/p/meridai/curator")
    assert brief.text == "<b>meridai</b>: Morning brief.\n\n/p/meridai/curator" and brief.buttons == []


def test_a_decision_has_a_button_per_option_and_one_for_the_web_and_escapes_what_the_agent_wrote():
    message = telegram.decision_message(
        decision_id=9,
        project="evo-agents",
        run_id=4,
        plan_id="fleet",
        step_key="2",
        category="architecture",
        question="Use <b>Postgres</b> & drop SQLite?",
        options=OPTIONS,
        url="https://hub.test/inbox?decision=9",
        link="/inbox?decision=9",
    )
    assert "Run #4 of evo-agents asks a decision</b> (architecture, plan fleet, step 2)" in message.text
    assert "Use &lt;b&gt;Postgres&lt;/b&gt; &amp; drop SQLite?" in message.text
    assert "postgres: Move to Postgres (recommended)" in message.text
    assert "One more service." not in message.text  # an option's description stays on the hub
    assert message.buttons == [
        [{"text": "Keep SQLite", "callback_data": "d:9:sqlite"}],
        [{"text": "Move to Postgres (recommended)", "callback_data": "d:9:postgres"}],
        [{"text": "Open on the hub", "url": "https://hub.test/inbox?decision=9"}],
    ]
    assert message.reply_markup() == {"inline_keyboard": message.buttons}
    long = telegram.decision_message(
        decision_id=9,
        project="p",
        run_id=None,
        plan_id=None,
        step_key=None,
        category="scope",
        question="q" * 5000,
        options=OPTIONS,
        url=None,
        link=None,
    )
    assert len(long.text) <= telegram.MAX_TEXT_CHARS and "q" * (telegram.QUESTION_CHARS - 3) + "..." in long.text


def test_a_proposal_has_accept_reject_defer_and_the_web_and_no_summary():
    message = telegram.proposal_message(
        proposal_id=3,
        project="evo-agents",
        title="A wait helper instead of sleep and tail",
        tier=2,
        kind="feature",
        lens="environment",
        evidence=4,
        url="https://hub.test/inbox?proposal=3",
        link="/inbox?proposal=3",
    )
    assert "Proposal #3 of evo-agents" in message.text and "tier 2, kind feature, lens environment" in message.text
    assert [button["callback_data"] for button in message.buttons[0]] == ["p:3:accept", "p:3:reject", "p:3:defer"]
    assert message.buttons[1] == [{"text": "Open on the hub", "url": "https://hub.test/inbox?proposal=3"}]


def test_a_notice_keeps_the_bold_of_its_body_and_escapes_the_rest():
    message = telegram.notice_message(
        project="p",
        notice_kind="curator_brief",
        title="t",
        body="**Night of 2026-10-08** <x> & **",
        url=None,
        link=None,
    )
    assert message.text.endswith("<b>Night of 2026-10-08</b> &lt;x&gt; &amp; **")


def test_a_notice_names_its_kind_and_cuts_its_body():
    message = telegram.notice_message(
        project="evo-agents", notice_kind="curator_brief", title="Morning brief", body="x" * 9000, url=None, link="/p"
    )
    assert message.text.startswith("<b>Morning brief</b> in evo-agents\nMorning brief\n\n")
    assert len(message.text) <= telegram.MAX_TEXT_CHARS and message.buttons == []


def test_an_answered_message_keeps_its_web_button_only():
    markup = {
        "inline_keyboard": [
            [{"text": "Keep SQLite", "callback_data": "d:9:sqlite"}],
            [{"text": "Open on the hub", "url": "https://hub.test/inbox?decision=9"}],
        ]
    }
    assert telegram.url_buttons(markup) == [[{"text": "Open on the hub", "url": "https://hub.test/inbox?decision=9"}]]
    assert telegram.url_buttons(None) == [] and telegram.url_buttons({"inline_keyboard": "no"}) == []
    assert telegram.answered_text("Pick <one>", "Answered by owner: Keep SQLite") == (
        "Pick &lt;one&gt;\n\n<b>Answered by owner: Keep SQLite</b>"
    )


def test_web_url_needs_the_public_url_and_a_path_of_the_hub():
    assert telegram.web_url("https://hub.test/", "/inbox?decision=1") == "https://hub.test/inbox?decision=1"
    assert telegram.web_url(None, "/inbox") is None
    assert telegram.web_url("https://hub.test", "//evil.example") is None
    assert telegram.web_url("https://hub.test", "https://evil.example") is None
