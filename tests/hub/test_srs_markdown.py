"""The core plugin srs reads and writes Markdown (``evo_agents.core_plugins.srs.markdown``) and checks a document
(``checks``), without the hub: the sample SRS of tests/hub/fixtures/srs, written for these tests and holding no content
of any client, imports to the items its headings and tables name, with the same codes, and exports back to its source
(criterion a2 of plan srs-tasks-plugins). Each check finds what it is for.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from evo_agents.core_plugins.srs import checks, markdown
from evo_agents.core_plugins.srs.diff import diff
from evo_agents.core_plugins.srs.template import TemplateError, code_key, template_of

SAMPLE = Path(__file__).parent / "fixtures" / "srs" / "demo-shop-srs.md"
CODE_LINE = re.compile(r"\b(?:UC [A-Z]+-\d+|BR [A-Z]+\d+-\d+)\b")


def sample() -> str:
    return SAMPLE.read_text(encoding="utf-8")


def parsed(text: str | None = None, **patterns):
    return markdown.parse(sample() if text is None else text, template_of(patterns=patterns or None))


def codes(doc, kind: str) -> list[str]:
    return [item.code for item in doc.items if item.kind == kind]


def test_srs_sample_imports_to_the_items_its_headings_and_tables_name():
    doc = parsed()
    assert codes(doc, "use_case") == ["UC ORD-1", "UC ORD-2", "UC RET-1"]
    assert codes(doc, "business_rule") == ["BR ORD1-1", "BR ORD1-2", "BR ORD2-1", "BR RET1-1"]
    assert codes(doc, "cbr") == ["CBR 1", "CBR 2"]
    assert codes(doc, "nfr") == ["NFR-SEC-01", "NFR-PERF-01"]
    assert [item.title for item in doc.items if item.kind == "screen"] == ["Cart", "Checkout"]
    kinds = {item.title: item.kind for item in doc.items if item.level is not None}
    assert kinds["Approval Page"] == "approval_page" and kinds["Revision History"] == "revision_history"
    assert kinds["1.2. Definitions, Acronyms and Abbreviations"] == "glossary"
    assert markdown.title_of(doc) == "Demo Shop: Order Service"
    assert doc.notes == []


def test_srs_use_case_fields_aliases_and_steps_come_from_its_tables():
    doc = parsed()
    order = next(item for item in doc.items if item.code == "UC ORD-1")
    assert order.title == "Place an order" and order.level == 4
    assert order.aliases == ["UC-ORD-001", "ORD-01"]
    assert order.fields["actor"] == "Customer" and order.fields["pre_condition"] == "The cart holds at least one line"
    assert [step["step"] for step in order.fields["steps"]] == ["1", "2", "3"]
    assert order.fields["steps"][1]["system"] == "Checks the cart (BR ORD1-1)"
    retries = next(item for item in doc.items if item.code == "BR ORD2-1")
    assert retries.fields["steps"] == ["2", "2.1"] and retries.fields["step_label"] == "Steps (2, 2.1)"
    assert doc.items[retries.parent].code == "UC ORD-2"
    window = next(item for item in doc.items if item.code == "BR RET1-1")
    assert window.fields["steps"] == [] and window.fields["step_label"] is None
    security = next(item for item in doc.items if item.code == "NFR-SEC-01")
    assert security.fields["group"] == "SEC" and security.title == "Card data never reaches the shop's servers"
    assert doc.items[security.parent].title == "5. Non-Functional Requirements"


def test_srs_a_heading_inside_a_fenced_block_is_text():
    doc = parsed()
    retries = next(item for item in doc.items if item.code == "BR ORD2-1")
    assert "# declined: insufficient funds" in retries.body
    assert not any(item.title.startswith("declined") for item in doc.items)


def test_srs_export_gives_the_source_back_with_the_same_codes():
    source = sample()
    doc = parsed(source)
    assert markdown.render(doc, anchors=False) == markdown.normalize(source)
    exported = markdown.render(doc)
    assert markdown.normalize(exported, anchors=False) == markdown.normalize(source)
    again = parsed(exported)
    assert [(item.kind, item.code, item.title) for item in again.items] == [
        (item.kind, item.code, item.title) for item in doc.items
    ]
    assert sorted(CODE_LINE.findall(exported)) == sorted(CODE_LINE.findall(source))


def test_srs_screen_codes_written_after_the_heading_are_read_back():
    doc = parsed()
    for number, item in enumerate((item for item in doc.items if item.kind == "screen"), start=1):
        item.code = f"SCR-{number}"
    exported = markdown.render(doc)
    assert "### Screen: Cart\n<!-- srs: SCR-1 -->\n\n| Component | Behaviour |" in exported
    again = parsed(exported)
    assert codes(again, "screen") == ["SCR-1", "SCR-2"]
    assert markdown.render(again, anchors=False) == markdown.normalize(sample())


def test_srs_the_sample_has_no_problem():
    assert checks.problems(parsed(), template_of()) == []


def test_srs_checks_find_what_refuses_a_save_and_what_blocks_a_submit():
    text = sample()
    text = text.replace("| Actor | Customer |\n| Trigger | The customer presses", "| Trigger | The customer presses")
    text = text.replace(
        "##### Step (3): BR ORD1-2 – Stock reservation", "##### Step (9): BR ORD2-7 – Stock reservation"
    )
    text = text.replace("#### BR RET1-1 – Return window", "## BR RET1-1 – Return window")
    text = text.replace("| CBR 2 |", "| CBR 1 |")
    text = text.replace("### UC RET-1: Return a book", "### UC ret-1: Return a book")
    found = {(problem.check, problem.code) for problem in checks.problems(parsed(text), template_of())}
    assert ("uc_no_actor", "UC ORD-1") in found
    assert ("br_code_mismatch", "BR ORD2-7") in found and ("br_step_unknown", "BR ORD2-7") in found
    assert ("br_outside_uc", "BR RET1-1") in found
    assert ("code_duplicate", "CBR 1") in found
    assert ("code_pattern", "UC ret-1") in found
    problems = checks.problems(parsed(text), template_of())
    refusing = {problem.check for problem in problems if problem.refuses_save()}
    assert refusing == {"code_duplicate", "code_pattern"}
    assert {problem.check for problem in checks.blocking(problems)} >= {"uc_no_actor", "br_outside_uc"}


def test_srs_reading_notes_skipped_levels_unknown_headings_and_rows_left_as_text():
    text = "# 1. Scope\n\n### Deep\n\ntext\n\n# Something else\n\n# 5. NFR\n\n| ID | R |\n|---|---|\n"
    text += "| NFR-SEC-01 | a |\n\nbetween\n\n| NFR-SEC-02 | b |\n"
    doc = parsed(text)
    assert {problem.check for problem in doc.notes} == {"heading_jump", "heading_unknown", "rows_as_text"}
    assert codes(doc, "nfr") == ["NFR-SEC-01"]
    assert markdown.render(doc) == markdown.normalize(text)


def test_srs_code_patterns_come_from_the_config_and_codes_compare_normalized():
    custom = parsed(use_case=r"^UC [A-Z]+-\d{3}$")
    problems = checks.code_problems(custom, template_of(patterns={"use_case": r"^UC [A-Z]+-\d{3}$"}))
    assert {problem.code for problem in problems} == {"UC ORD-1", "UC ORD-2", "UC RET-1"}
    with pytest.raises(TemplateError, match="code_patterns.use_case"):
        template_of(patterns={"use_case": "("})
    with pytest.raises(TemplateError, match="no template"):
        template_of("lms-srs")
    assert code_key("UC  ord-1") == code_key("uc ORD-1") == "uc ord-1"


def test_srs_diff_lists_added_removed_changed_and_moved_items():
    old = parsed()
    for index, item in enumerate(old.items):
        item.key = f"k{index}"
    new = parsed()
    for index, item in enumerate(new.items):
        item.key = f"k{index}"
    cart = next(item for item in new.items if item.code == "BR ORD1-1")
    cart.body = cart.body.replace("50 lines", "60 lines")
    gone = next(index for index, item in enumerate(new.items) if item.code == "CBR 2")
    del new.items[gone]
    for item in new.items:
        item.parent = item.parent - 1 if item.parent is not None and item.parent > gone else item.parent
    new.items[-1].key = "fresh"
    checkout = next(item for item in new.items if item.title == "Checkout")
    checkout.level = 2
    found = {(change.code or change.title_to or change.title_from, change.change) for change in diff(old, new)}
    assert found == {
        ("BR ORD1-1", "changed"),
        ("CBR 2", "removed"),
        ("7. Open Items", "added"),
        ("7. Open Items", "removed"),
        ("Checkout", "changed"),
    }
    changed = next(change for change in diff(old, new) if change.change == "changed")
    assert "-- A cart holds at most 50 lines and 99 of one SKU." in changed.lines
    assert "+- A cart holds at most 60 lines and 99 of one SKU." in changed.lines
