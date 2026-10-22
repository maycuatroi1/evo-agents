"""The categories of a decision an agent asks, and the autonomy card that opens the prompt of a run (plan
autonomy-levels, criterion a12).

Categories (``DECISION_CATEGORIES``): each class of action that takes ask has its own (``CLASS_CATEGORIES``): K5
``merge``, K6 ``deploy``, K8 ``credential``, K9 ``delete_data`` and ``live_migration``, K10 ``external_send``, K11
``spend_money``, K12 ``access``; ``architecture`` and ``scope`` belong to no class, and every agent of a plan run may ask
them. A run without a snapshot of autonomy (a local run, one queued before schema 0111) tells its agent the fixed list
``PROMPTED_DECISION_CATEGORIES`` instead, as before.

The card (``card``): built by the hub from the run's snapshot (``runs.autonomy``) and its principal when the worker
claims the run, and put first in the prompt of every run with an agent. It names the principal, their role and unit,
the person they report to when they are an intern (who answers the run's decisions), the effective level, and the
classes in deny, in ask with the category to ask them in, and in notify. A run at A5 whose every class acts on its own
gets the floors S2 and S3 alone. An ask a project policy sets (a repo in mode off caps K5, prod_release manual K6) is
the merge queue's and the release's to hold, as the gates read it (``autonomy_gates.mode_of``): the card shows it as
auto, so the agent of an A5 principal still merges, deploys and releases on its own (``ships``).

Standard library only: the worker builds the prompt of a plan run's step from it, and the CLI reads its names.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from evo_agents.hub.autonomy_levels import (
    ACTING,
    ASK,
    ASK_ON_RISK_PATHS,
    AUTO,
    CLASS_NAMES,
    CLASSES,
    CONDITION_TEXT,
    DENY,
    HIGHEST,
    NOTIFY,
    POLICY,
)

DECISION_CATEGORIES = (
    "deploy",
    "delete_data",
    "live_migration",
    "external_send",
    "spend_money",
    "architecture",
    "scope",
    "merge",
    "credential",
    "access",
)
DECISION_CATEGORY_TEXT = {
    "deploy": "deploying or releasing anything to any environment, a checkpoint step that deploys included",
    "delete_data": "deleting data that is not this run's own scratch: database rows, files, buckets, other "
    "people's branches",
    "live_migration": "a migration, backfill or bulk change on real data rather than a test database",
    "external_send": "sending anything to a service outside this machine and the repos' own remotes: mail, chat, "
    "issues, third-party APIs",
    "spend_money": "anything that costs money beyond this runtime's own usage: paid APIs, cloud resources, purchases",
    "architecture": "an architectural choice the plan leaves open",
    "scope": "a question of scope the plan leaves open: adding, dropping or reshaping a step or what it delivers",
    "merge": "merging into a repo's default branch or the target branch of a pull or merge request",
    "credential": "creating, rotating or revoking a credential: a token, a key, a secret, an account's password",
    "access": "changing grants, autonomy ceilings or preferences, policies or agent profiles",
}
# The category to ask a class in; K9 has two, a deletion and a change of real data.
CLASS_CATEGORIES = {
    "K5": ("merge",),
    "K6": ("deploy",),
    "K8": ("credential",),
    "K9": ("delete_data", "live_migration"),
    "K10": ("external_send",),
    "K11": ("spend_money",),
    "K12": ("access",),
}
FREE_CATEGORIES = ("architecture", "scope")  # of no class: any agent of a plan run asks them
# What the prompt of a plan run without a snapshot tells its agent to ask: the categories of before this plan but
# deploy, since merging, deploying and releasing are the agent's (main deploys on every push, deploy.yml).
PROMPTED_DECISION_CATEGORIES = ("delete_data", "live_migration", "external_send", "spend_money", *FREE_CATEGORIES)
FLOORS = (
    "S2: before you delete or overwrite data for good, make a backup, or check that one made within the last 24 hours "
    "exists",
    "S3: never print a credential's value, nor commit, send or log it",
)
HEADING = "# Your autonomy on this run"


@dataclass(frozen=True)
class Who:
    """The principal of a run as the card names them: login, role (lead, member or intern), unit slug, and the login
    of the person they report to (an intern's, who answers the run's decisions; None otherwise)."""

    login: str
    role: str
    unit: str
    reports_to: str | None = None


@dataclass(frozen=True)
class Card:
    """The card's text, the mode each class acts in for the agent, the categories its agent may ask, and what set the
    mode of each class (``Choice.source``: a ceiling, a preference, a policy), which the worker's hook names when it
    refuses a command, and the principal's login, which the run's commits and its hook's events name."""

    text: str
    modes: dict[str, str] = field(default_factory=dict)
    categories: tuple[str, ...] = FREE_CATEGORIES
    sources: dict[str, str] = field(default_factory=dict)
    principal: str | None = None

    @property
    def ships(self) -> bool:
        return ships(self.modes)

    def spec(self) -> dict:
        """As the claim gives it to the worker (``RunSpec.autonomy``)."""
        found = {"card": self.text, "modes": dict(self.modes), "categories": list(self.categories)}
        found |= {"sources": dict(self.sources)} if self.sources else {}
        return found | ({"principal": self.principal} if self.principal else {})


def ships(modes: Mapping[str, str] | None) -> bool:
    """Whether merging, deploying and releasing are the agent's: K5 and K6 auto, or no snapshot at all."""
    return not modes or (modes.get("K5") == AUTO and modes.get("K6") == AUTO)


def agent_mode(held: Mapping) -> str:
    """The mode a class of the snapshot acts in for the agent: an ask of a project policy is the flows' that hold it."""
    if held["mode"] == ASK and str(held.get("source", "")).startswith(POLICY):
        return AUTO
    return held["mode"]


def _named(klass: str, held: Mapping, mode: str) -> str:
    text = f"{klass} {CLASS_NAMES[klass]}"
    condition = held.get("condition") if mode in ACTING else None
    if klass == "K5" and condition == ASK_ON_RISK_PATHS:
        return f"{text} (but ask, category merge, when it touches a risk path of the merge queue)"
    if condition is not None:
        return f"{text} ({CONDITION_TEXT[condition]})"
    return text


def _ask(klass: str, held: Mapping) -> str:
    return f"{_named(klass, held, ASK)} (category {' or '.join(CLASS_CATEGORIES[klass])})"


def _principal(who: Who, level: str) -> str:
    line = f"Principal: {who.login}, {who.role} in unit {who.unit}"
    if who.reports_to:
        line += f", reports to {who.reports_to}, who answers this run's decisions"
    return f"{line}. Effective level: {level}."


def card(snapshot: Mapping, who: Who) -> Card:
    """The card of a run whose autonomy is ``snapshot`` (``autonomy_levels.snapshot``) and whose principal is ``who``."""
    classes = snapshot["classes"]
    modes = {klass: agent_mode(classes[klass]) for klass in CLASSES}
    sources = {klass: str(classes[klass].get("source") or "") for klass in CLASSES}
    floors = ["Floors for everyone, whatever the level:", *(f"- {floor}" for floor in FLOORS)]
    if snapshot["level"] == HIGHEST and all(mode == AUTO for mode in modes.values()):
        lines = [HEADING, _principal(who, HIGHEST) + " Every class of action is yours to do.", *floors]
        return Card("\n".join(lines), modes, sources=sources, principal=who.login)
    by_mode = {mode: [klass for klass in CLASSES if modes[klass] == mode] for mode in (DENY, ASK, NOTIFY, AUTO)}
    asked = [klass for klass in CLASSES if modes[klass] == ASK]
    asked += [
        klass for klass in ("K5",) if modes[klass] in ACTING and classes[klass].get("condition") == ASK_ON_RISK_PATHS
    ]
    lines = [HEADING, _principal(who, snapshot["level"])]
    if by_mode[DENY]:
        lines.append("- Never do (deny): " + "; ".join(_named(k, classes[k], DENY) for k in by_mode[DENY]) + ".")
    if by_mode[ASK]:
        lines.append(
            "- Ask first with `evo-agents worker ask --category CATEGORY` and wait for the answer (ask): "
            + "; ".join(_ask(k, classes[k]) for k in by_mode[ASK])
            + "."
        )
    if by_mode[NOTIFY]:
        named = "; ".join(_named(k, classes[k], NOTIFY) for k in by_mode[NOTIFY])
        lines.append(f"- Do it, and the hub tells the principal (notify): {named}.")
    if by_mode[AUTO]:
        lines.append("- Yours to do (auto): " + "; ".join(_named(k, classes[k], AUTO) for k in by_mode[AUTO]) + ".")
    categories = [category for klass in dict.fromkeys(asked) for category in CLASS_CATEGORIES.get(klass, ())]
    return Card("\n".join([*lines, *floors]), modes, (*categories, *FREE_CATEGORIES), sources, who.login)


def decision_lines(where: str, autonomy: Mapping | None) -> list[str]:
    """The categories of decision the agent of a plan run may ask, and where it writes the others: those the card
    names (``autonomy``, as ``Card.spec`` gives it) and architecture and scope, or PROMPTED_DECISION_CATEGORIES for a
    run without one."""
    if autonomy:
        categories = [name for name in autonomy.get("categories") or FREE_CATEGORIES if name in DECISION_CATEGORY_TEXT]
        head = "- Ask only a decision of one of these categories, the ones your autonomy card above leaves to ask:"
    else:
        categories, head = list(PROMPTED_DECISION_CATEGORIES), "- Ask the run's owner only a decision of one of these:"
    return [
        head,
        *(f"  - {name}: {DECISION_CATEGORY_TEXT[name]}" for name in categories),
        f"  Decide everything else yourself, and write each such choice and why into the evidence of {where}, on a "
        'line that starts with "Decision:".',
    ]


def checkpoint_rule(autonomy: Mapping | None) -> str:
    """The rule of a plan run on its checkpoint steps: merging, deploying and releasing are the agent's only when K5
    and K6 are auto for it."""
    if ships((autonomy or {}).get("modes")):
        return (
            "- A step is ready when its status is pending and every step its depends_on names is done. Do ready steps "
            "until every step is done. Checkpoint steps (a verification, a review, a merge, a deploy, a release) are "
            "yours too, with no question to the owner: merging, deploying and releasing are the agent's."
        )
    return (
        "- A step is ready when its status is pending and every step its depends_on names is done. Do ready steps "
        "until every step is done. Checkpoint steps of a verification or a review are yours; a merge into a default "
        "branch (K5) and a deploy or a release (K6) go as your autonomy card says: never in deny, after an answer in "
        "ask."
    )


def with_card(prompt: str, autonomy: Mapping | None) -> str:
    """``prompt`` opened by the card of ``autonomy`` (``Card.spec``); as it is for a run without one."""
    text = (autonomy or {}).get("card")
    return f"{text}\n\n{prompt}" if text else prompt


def without_card(prompt: str) -> str:
    """``prompt`` without the card ``with_card`` opened it with; as it is when it opens with none."""
    if not prompt.startswith(HEADING):
        return prompt
    _, _, rest = prompt.partition("\n\n")
    return rest
