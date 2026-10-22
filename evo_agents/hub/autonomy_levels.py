"""The autonomy levels of the hub (plan autonomy-levels, criteria a1 to a3): twelve classes of action K1 to K12, four
modes, six levels A0 to A5 that give each class a mode, and the effective mode of each class for a run, the strictest
of the sources that bear on it.

Classes (``CLASSES``): K1 read code, documents, the graph and logs within the grants; K2 edit and run commands in the
run's workspace; K3 commit and push the run's branch, never forced; K4 open a PR or MR, comment, label; K5 merge into
the default or target branch; K6 deploy, publish, tag a release; K7 use a credential that writes or reaches a real
environment; K8 create, rotate, revoke a credential; K9 delete or overwrite data for good; K10 send out on a person's
behalf (mail, chat, an outside issue, a third-party API); K11 spend money beyond the runtime's allowance; K12 change
grants, ceilings, policies, profiles.

Modes (``MODES``), strictest first: deny < ask (open a decision and wait) < notify (act, then send a notice) < auto (act;
the ledger records it). K1 to K4 and K7 never take ask (``NO_ASK``): a run cannot wait on a person to read, edit, push
its own branch, open a PR, or hold a credential.

Levels (``PRESETS``), the table "Mức dựng sẵn" of the design report. A cell is a mode with an optional condition
(``Cell``): K5 at A2 is notify outside the merge queue's risk paths and ask on them (``ASK_ON_RISK_PATHS``); K7 at A1
is the credentials that push the run's branch alone (``RUN_BRANCH``), at A2 those and the secrets labelled for a test
environment (``TEST_SECRETS``); K11 acts within the budget alone (``WITHIN_BUDGET``) and K12 never above the writer's own
level (``NOT_ABOVE_OWN``) in every mode that acts, which those two classes always carry (``INHERENT``).

The effective mode (``effective``) takes a list of ``Source``: each names itself, may set a level and override classes
(``Kn`` to ``mode`` or ``mode:condition``), and may list the environments and the workers it allows (empty: no limit).
Each class gets the strictest mode among the sources that set it, from the first of them in the list on a tie (the
caller lists them nearest first); the strictest condition of them all goes with it while the mode acts. A class no
source sets is auto. The level is the lowest level a source sets, the first on a tie, A5 from the last source when
none sets one: the ceiling of the org chart (``evo_agents.hub.org.lowest``) is this with sources that set levels alone.
The environments and the workers are the intersection of the lists the sources give; None means no limit.

Standard library only: the CLI reads its names, and the server builds its sources from the tables.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

LEVELS = ("A0", "A1", "A2", "A3", "A4", "A5")
HIGHEST = LEVELS[-1]
DENY, ASK, NOTIFY, AUTO = MODES = ("deny", "ask", "notify", "auto")
ACTING = (NOTIFY, AUTO)  # the modes a run acts in, the ones a condition holds for
CLASSES = tuple(f"K{n}" for n in range(1, 13))
CLASS_NAMES = {
    "K1": "read code, documents, the graph and logs within the grants",
    "K2": "edit and run commands in the run's workspace",
    "K3": "commit and push the run's branch, never forced",
    "K4": "open a pull or merge request, comment, label",
    "K5": "merge into the default or target branch",
    "K6": "deploy, publish, tag a release",
    "K7": "use a credential that writes or reaches a real environment",
    "K8": "create, rotate or revoke a credential",
    "K9": "delete or overwrite data for good",
    "K10": "send out on a person's behalf",
    "K11": "spend money beyond the runtime's allowance",
    "K12": "change grants, ceilings, policies or profiles",
}
NO_ASK = frozenset({"K1", "K2", "K3", "K4", "K7"})

ASK_ON_RISK_PATHS = "ask_on_risk_paths"
RUN_BRANCH = "run_branch"
TEST_SECRETS = "test_secrets"
WITHIN_BUDGET = "within_budget"
NOT_ABOVE_OWN = "not_above_own"
# The conditions a class takes, strictest first; no condition is looser than any of them.
CONDITIONS = {"K5": (ASK_ON_RISK_PATHS,), "K7": (RUN_BRANCH, TEST_SECRETS)}
# The conditions a class carries in every mode that acts, whatever set it.
INHERENT = {"K11": WITHIN_BUDGET, "K12": NOT_ABOVE_OWN}
CONDITION_TEXT = {
    ASK_ON_RISK_PATHS: "asks on the merge queue's risk paths",
    RUN_BRANCH: "only the credentials that push the run's branch",
    TEST_SECRETS: "only the run's branch and the secrets labelled for a test environment",
    WITHIN_BUDGET: "within the budget",
    NOT_ABOVE_OWN: "never above the writer's own level",
}
INSTANCE = "instance"
# What the instance holds for every principal whatever the mode: the floors S1 to S5 of the design, none a step of
# approval. K9 runs only once the run recorded a backup made within 24 hours (S2).
FLOORS = {"K9": "floor S2: a backup made within 24 hours comes first"}


def rank(level: str) -> int:
    """The place of ``level`` on the ladder, A0 being 0."""
    return LEVELS.index(level)


def mode_rank(mode: str) -> int:
    """The place of ``mode``, deny being 0."""
    return MODES.index(mode)


@dataclass(frozen=True)
class Cell:
    """The mode of one class, with the condition it acts under; None without one."""

    mode: str
    condition: str | None = None

    def text(self) -> str:
        return self.mode if self.condition is None else f"{self.mode}:{self.condition}"


def cell(klass: str, mode: str, condition: str | None = None) -> Cell:
    """The cell of ``klass``: a condition only while the mode acts, the class's inherent one when it has one."""
    if mode not in ACTING:
        return Cell(mode)
    return Cell(mode, INHERENT.get(klass, condition))


def _row(*modes: str) -> dict[str, Cell]:
    row = {}
    for klass, given in zip(CLASSES, modes, strict=True):
        mode, _, condition = given.partition(":")
        row[klass] = cell(klass, mode, condition or None)
    return row


_ACTS = (AUTO,) * 4  # K1 to K4
PRESETS: dict[str, dict[str, Cell]] = {
    "A0": _row(AUTO, *(DENY,) * 11),
    "A1": _row(*_ACTS, ASK, DENY, f"{AUTO}:{RUN_BRANCH}", DENY, DENY, DENY, DENY, DENY),
    "A2": _row(*_ACTS, f"{NOTIFY}:{ASK_ON_RISK_PATHS}", ASK, f"{AUTO}:{TEST_SECRETS}", DENY, DENY, ASK, DENY, DENY),
    "A3": _row(*_ACTS, AUTO, NOTIFY, AUTO, ASK, ASK, ASK, ASK, DENY),
    "A4": _row(*_ACTS, AUTO, AUTO, AUTO, NOTIFY, NOTIFY, NOTIFY, NOTIFY, ASK),
    "A5": _row(*(AUTO,) * 12),
}


# Overrides of one class


def mode_problem(klass: str, text: str) -> str | None:
    """Why ``text`` (``mode`` or ``mode:condition``) is no mode of ``klass``; None when it is one."""
    if klass not in CLASSES:
        return f"{klass} is no class; the classes are K1 to K12"
    mode, _, condition = text.partition(":")
    if mode not in MODES:
        return f"{klass}={text}: the modes are {', '.join(MODES)}"
    if mode == ASK and klass in NO_ASK:
        return f"{klass} never takes ask; K1 to K4 and K7 take deny, notify or auto"
    if condition:
        if condition not in CONDITIONS.get(klass, ()):
            taken = CONDITIONS.get(klass)
            return f"{klass} takes no condition {condition}" + (f"; it takes {', '.join(taken)}" if taken else "")
        if mode not in ACTING:
            return f"{klass}={text}: a condition goes with notify or auto"
    return None


def parse_override(text: str) -> tuple[str, str]:
    """``Kn=mode[:condition]`` as (class, mode text); ValueError naming the problem."""
    klass, sep, mode = text.partition("=")
    klass = klass.strip().upper()
    mode = mode.strip().lower()
    if not sep or not mode:
        raise ValueError(f"{text!r} is not Kn=mode")
    problem = mode_problem(klass, mode)
    if problem is not None:
        raise ValueError(problem)
    return klass, mode


def overrides_problem(overrides: Mapping[str, str]) -> str | None:
    """The first problem of a map of class overrides; None when every one is right."""
    for klass, text in overrides.items():
        problem = mode_problem(klass, text) if isinstance(text, str) else f"{klass}: a mode is a string"
        if problem is not None:
            return problem
    return None


def cells(level: str | None, overrides: Mapping[str, str] | None = None) -> dict[str, Cell]:
    """Every class of ``level`` (A5 without one), each override in its place."""
    found = dict(PRESETS[level or HIGHEST])
    for klass, text in (overrides or {}).items():
        mode, _, condition = text.partition(":")
        found[klass] = cell(klass, mode, condition or None)
    return found


def condition_rank(klass: str, condition: str | None) -> int:
    """The place of ``condition`` among those of ``klass``, strictest 0; no condition is the loosest."""
    taken = CONDITIONS.get(klass, ())
    return taken.index(condition) if condition in taken else len(taken)


def looser(klass: str, a: Cell, b: Cell) -> bool:
    """Whether ``a`` allows more than ``b`` does: a looser mode, or the same acting mode under a looser condition."""
    if mode_rank(a.mode) != mode_rank(b.mode):
        return mode_rank(a.mode) > mode_rank(b.mode)
    return a.mode in ACTING and condition_rank(klass, a.condition) > condition_rank(klass, b.condition)


# The effective mode


@dataclass(frozen=True)
class Source:
    """One source of the effective mode: its name (``pref:<login>``, ``seat:<id>``, ``unit:<slug>``,
    ``policy:<project>/<repo>``, ``credential_share``, ``instance``), the level it sets and its class overrides, the
    environments and workers it allows (empty: no limit), and a line saying what it is."""

    name: str
    level: str | None = None
    overrides: Mapping[str, str] = field(default_factory=dict)
    environments: tuple[str, ...] = ()
    workers: tuple[str, ...] = ()
    detail: str = ""

    def sets(self, klass: str) -> bool:
        return self.level is not None or klass in self.overrides

    def cell(self, klass: str) -> Cell:
        text = self.overrides.get(klass)
        if text is None:
            return PRESETS[self.level or HIGHEST][klass]
        mode, _, condition = text.partition(":")
        return cell(klass, mode, condition or None)

    def why(self, klass: str) -> str:
        """How this source sets ``klass``."""
        said = f"{klass}={self.overrides[klass]}" if klass in self.overrides else f"level {self.level}"
        return f"{said} of {self.name}" + (f" ({self.detail})" if self.detail else "")


@dataclass(frozen=True)
class Choice:
    """The effective mode of one class, the source that sets it, and why."""

    mode: str
    source: str
    detail: str
    condition: str | None = None


def refusal(klass: str, mode: str, choice: Choice) -> str:
    """The sentence of a refusal of the hub's gates: the class, the mode and the source that sets it."""
    return f"{klass} ({CLASS_NAMES[klass]}) is {mode} for the run's principal, set by {choice.source}" + (
        f" ({choice.detail})" if choice.detail else ""
    )


@dataclass(frozen=True)
class Reached:
    level: str
    level_source: str
    classes: dict[str, Choice]
    environments: tuple[str, ...] | None  # None: no limit
    workers: tuple[str, ...] | None

    def cell(self, klass: str) -> Cell:
        choice = self.classes[klass]
        return Cell(choice.mode, choice.condition)


def _level(sources: list[Source]) -> tuple[str, str]:
    found: tuple[str, str] | None = None
    for source in sources:
        if source.level is not None and (found is None or rank(source.level) < rank(found[0])):
            found = (source.level, source.name)
    return found if found is not None else (HIGHEST, sources[-1].name if sources else INSTANCE)


def _choice(klass: str, sources: list[Source]) -> Choice:
    setting = [source for source in sources if source.sets(klass)]
    if not setting:
        last = sources[-1].name if sources else INSTANCE
        return Choice(AUTO, last, "no source limits it", INHERENT.get(klass))
    strictest = min(setting, key=lambda source: mode_rank(source.cell(klass).mode))
    mode = strictest.cell(klass).mode
    detail = strictest.why(klass)
    condition = None
    if mode in ACTING:
        held = min(setting, key=lambda source: condition_rank(klass, source.cell(klass).condition))
        condition = INHERENT.get(klass, held.cell(klass).condition)
        if condition is not None:
            detail += f"; {CONDITION_TEXT[condition]}"
            if held is not strictest and klass not in INHERENT:
                detail += f" from {held.name}"
    if klass in FLOORS and mode in ACTING:
        detail += f"; {FLOORS[klass]}"
    return Choice(mode, strictest.name, detail, condition)


def _places(lists: Iterable[tuple[str, ...]]) -> tuple[str, ...] | None:
    found: set[str] | None = None
    for given in lists:
        if given:
            found = set(given) if found is None else found & set(given)
    return None if found is None else tuple(sorted(found))


def effective(sources: Iterable[Source]) -> Reached:
    """The effective level, the mode of each class with its source, and the environments and workers allowed."""
    listed = list(sources)
    level, level_source = _level(listed)
    return Reached(
        level=level,
        level_source=level_source,
        classes={klass: _choice(klass, listed) for klass in CLASSES},
        environments=_places(source.environments for source in listed),
        workers=_places(source.workers for source in listed),
    )


# The sources outside the org chart, in the order ``evo_agents.hub.org.autonomy_sources`` lists them

CREDENTIAL_SHARE = "credential_share"
REPO_OFF = "off"  # repos.<repo>.mode of the project policy (evo_agents.hub.autonomy.MODES)
RELEASE_MANUAL = "manual"  # repos.<repo>.prod_release


def pref_source(login: str, level: str | None, overrides: Mapping[str, str] | None = None) -> Source:
    """Source 1, the principal's own preference; A5 for one who set none."""
    if level is None:
        return Source(f"pref:{login}", HIGHEST, detail="no preference set")
    return Source(f"pref:{login}", level, dict(overrides or {}), detail="their own preference")


POLICY = "policy:"  # the prefix of the name of source 4


def policy_source(project: str, repo: str, mode: str, prod_release: str) -> Source:
    """Source 4, the project policy of one repo: mode off caps K5 at ask, prod_release manual caps K6 at ask."""
    overrides = {"K5": ASK} if mode == REPO_OFF else {}
    if prod_release == RELEASE_MANUAL:
        overrides["K6"] = ASK
    return Source(f"{POLICY}{project}/{repo}", None, overrides, detail=f"mode {mode}, prod_release {prod_release}")


def credential_source(shares: Iterable[tuple[int, str | None]] = ()) -> Source:
    """Source 6, the credentials shared with the run (credential_shares.level of each share it is leased under, as
    (share id, level)): the lowest level among them; empty when none carries a level."""
    held = sorted((rank(level), share_id, level) for share_id, level in shares if level is not None)
    if not held:
        return Source(CREDENTIAL_SHARE, detail="no shared credential limits it")
    _, share_id, level = held[0]
    return Source(CREDENTIAL_SHARE, level, detail=f"share {share_id} of a credential the run is leased")


def instance_source() -> Source:
    """Source 7, the instance: it limits no class; the floors S1 to S5 hold whatever the mode."""
    return Source(INSTANCE, detail="the floors S1 to S5 hold for everyone")


def escalation(klass_cells: Mapping[str, Cell], own: Reached) -> str | None:
    """The first class ``klass_cells`` allows more of than ``own`` does, as a sentence; None when there is none."""
    for klass in CLASSES:
        given, held = klass_cells[klass], own.cell(klass)
        if looser(klass, given, held):
            choice = own.classes[klass]
            return f"{klass}={given.text()} lies above their own effective mode {held.text()} (from {choice.source})"
    return None


# The snapshot of a run (criteria a4, a5 and a7): what the hub took at dispatch, and the strictest of two


def _stricter_choice(klass: str, a: Choice, b: Choice) -> Choice:
    first, other = (b, a) if mode_rank(b.mode) < mode_rank(a.mode) else (a, b)
    if first.mode not in ACTING or other.mode not in ACTING:
        return first
    if condition_rank(klass, other.condition) < condition_rank(klass, first.condition):
        held = CONDITION_TEXT[other.condition]
        return replace(first, condition=other.condition, detail=f"{first.detail}; {held} from {other.source}")
    return first


def _both(a: tuple[str, ...] | None, b: tuple[str, ...] | None) -> tuple[str, ...] | None:
    if a is None or b is None:
        return b if a is None else a
    return tuple(sorted(set(a) & set(b)))


def stricter(a: Reached, b: Reached) -> Reached:
    """The strictest of ``a`` and ``b``: each class at the stricter mode, ``a``'s on a tie, under the stricter
    condition of the two while it acts; the lower level; the environments and the workers both allow."""
    level, level_source = (b.level, b.level_source) if rank(b.level) < rank(a.level) else (a.level, a.level_source)
    return Reached(
        level=level,
        level_source=level_source,
        classes={klass: _stricter_choice(klass, a.classes[klass], b.classes[klass]) for klass in CLASSES},
        environments=_both(a.environments, b.environments),
        workers=_both(a.workers, b.workers),
    )


def restricted(reached: Reached) -> bool:
    """Whether a run of ``reached`` is held to a worker that applies the modes (criterion a7): a class below auto
    that a source other than the project policy sets. The policy's caps of K5 and K6 are the hub's own, the merge
    queue and the release cycle hold them, and a repo is off by default: they keep no run from any worker."""
    return any(choice.mode != AUTO and not choice.source.startswith(POLICY) for choice in reached.classes.values())


def snapshot(reached: Reached, taken_at: str, also: Iterable[str] = ()) -> dict:
    """``reached`` as runs.autonomy holds it: the level and its source, each class's mode, source, detail and
    condition, the environments and the workers (null: no limit), when it was taken, and the logins of the persons
    besides the principal whose autonomy bounds it (``also``, an agent type's run_as)."""
    return {
        "level": reached.level,
        "level_source": reached.level_source,
        "classes": {
            klass: {"mode": c.mode, "source": c.source, "detail": c.detail, "condition": c.condition}
            for klass, c in reached.classes.items()
        },
        "environments": None if reached.environments is None else list(reached.environments),
        "workers": None if reached.workers is None else list(reached.workers),
        "taken_at": taken_at,
        "also": list(also),
    }


def from_snapshot(body: Mapping) -> Reached:
    """The ``Reached`` of a snapshot ``snapshot`` wrote."""
    places = {key: None if body.get(key) is None else tuple(body[key]) for key in ("environments", "workers")}
    return Reached(
        level=body["level"],
        level_source=body["level_source"],
        classes={
            klass: Choice(held["mode"], held["source"], held["detail"], held.get("condition"))
            for klass, held in body["classes"].items()
        },
        **places,
    )
