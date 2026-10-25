"""What is wrong with a document, as the hub checks it when it is imported, saved and submitted.

Errors that refuse a save (``doc.SAVE_CHECKS``): a code that does not match its kind's pattern, a code twice in the
document. ``writes`` adds those only the hub's tables tell: a code another item of the project holds or held, a code
changed, a coded item dropped by a member who is not an admin. Errors that block submitting a version: a business rule
outside every use case, a use case without an actor or another field the config requires (``required_use_case_fields``),
a link to an item the version does not have or a screen that is none. Warnings only show: a business rule whose code
does not match its use case's, a business rule naming a step its use case does not have, and what reading the Markdown
noted (a heading that skips a level, one the template does not name, coded rows kept as text).
"""

from __future__ import annotations

import re

from evo_agents.hub.server.srs.doc import ERROR, WARNING, Document, Item, Problem
from evo_agents.hub.server.srs.template import Template, code_key

USE_CASE_BLOCK = re.compile(r"UC ([A-Z]+)-(\d+)")
RULE_BLOCK = re.compile(r"BR ([A-Z]+)(\d+)-\d+")


def _problem(level: str, check: str, message: str, item: Item) -> Problem:
    return Problem(level, check, message, code=item.code, item=item.key, line=item.line)


def code_problems(doc: Document, template: Template) -> list[Problem]:
    """Codes that do not match their kind's pattern, and codes the document holds twice (aliases included)."""
    found: list[Problem] = []
    seen: dict[str, Item] = {}
    for item in doc.items:
        if item.code:
            wrong = template.code_problem(item.kind, item.code)
            if wrong:
                found.append(_problem(ERROR, "code_pattern", wrong, item))
        for code in item.codes():
            first = seen.setdefault(code_key(code), item)
            if first is not item:
                where = f" (line {first.line})" if first.line else ""
                found.append(_problem(ERROR, "code_duplicate", f"{code!r} is the code of another item{where}", item))
    return found


def _use_case_of(doc: Document, index: int) -> Item | None:
    return next((doc.items[at] for at in doc.ancestors(index) if doc.items[at].kind == "use_case"), None)


def _rule_problems(doc: Document, index: int) -> list[Problem]:
    rule = doc.items[index]
    use_case = _use_case_of(doc, index)
    if use_case is None:
        return [_problem(ERROR, "br_outside_uc", f"business rule {rule.code} is under no use case", rule)]
    found = []
    block = USE_CASE_BLOCK.fullmatch(use_case.code or "")
    own = RULE_BLOCK.fullmatch(rule.code or "")
    if block and own and (block[1], int(block[2])) != (own[1], int(own[2])):
        expected = f"BR {block[1]}{int(block[2])}-<n>"
        message = f"business rule {rule.code} is under {use_case.code}, whose rules are {expected}"
        found.append(_problem(WARNING, "br_code_mismatch", message, rule))
    steps = {str(step.get("step", "")).strip() for step in use_case.fields.get("steps") or [] if isinstance(step, dict)}
    unknown = [step for step in rule.fields.get("steps") or [] if steps and step not in steps]
    if unknown:
        message = f"business rule {rule.code} names step {', '.join(unknown)}, which {use_case.code} does not have"
        found.append(_problem(WARNING, "br_step_unknown", message, rule))
    return found


def _ref_problems(doc: Document, item: Item, kinds: dict[str, str]) -> list[Problem]:
    found = []
    for rel, target in item.refs:
        if target not in kinds:
            found.append(_problem(ERROR, "ref_unknown", f"a link ({rel}) names an item this version lacks", item))
        elif rel == "uses_screen" and kinds[target] != "screen":
            found.append(_problem(ERROR, "ref_unknown", f"a link uses_screen names a {kinds[target]}", item))
    return found


def _filled(item: Item, field: str) -> bool:
    return bool(str(item.fields.get(field) or "").strip())


def _field_problems(item: Item, template: Template) -> list[Problem]:
    """The fields the template requires (its actor always) that a use case leaves empty."""
    found: list[Problem] = []
    for field in template.required:
        if _filled(item, field):
            continue
        if field == "actor":
            found.append(_problem(ERROR, "uc_no_actor", f"use case {item.code} names no actor", item))
        else:
            name = field.replace("_", "-").capitalize()
            found.append(_problem(ERROR, "uc_field_missing", f"use case {item.code} has no {name}", item))
    return found


def rule_problems(doc: Document, template: Template) -> list[Problem]:
    """Business rules outside use cases or out of step with theirs, use cases without a field the template requires,
    and broken links."""
    found: list[Problem] = []
    kinds = {item.key: item.kind for item in doc.items if item.key}
    for index, item in enumerate(doc.items):
        if item.kind == "business_rule":
            found.extend(_rule_problems(doc, index))
        elif item.kind == "use_case":
            found.extend(_field_problems(item, template))
        found.extend(_ref_problems(doc, item, kinds))
    return found


def problems(doc: Document, template: Template) -> list[Problem]:
    """Everything wrong with ``doc`` that the document alone tells, what reading it noted first."""
    return [*doc.notes, *code_problems(doc, template), *rule_problems(doc, template)]


def blocking(found: list[Problem]) -> list[Problem]:
    """The errors among ``found``: a version with one is not submitted."""
    return [problem for problem in found if problem.level == ERROR]


__all__ = ["blocking", "code_problems", "problems", "rule_problems"]
