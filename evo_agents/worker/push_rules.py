"""What a kind of run may push (``gitops.check_push``), each kind's rule a class of its own (``run_kinds.Kind.pushes``).

- ``PushesNothing``: a review run, a judge run, an author run, a replay run, a history run, a probe run, a question run
  read and push nothing at all.
- ``OwnBranches``: a chat run pushes only its branch ``chat/<run>``, a task run its branch ``task/<run>`` or the branch
  its task names for the repo, a Builder of the Curator a branch ``curator/...`` and a release run a branch
  ``release/<version>``: never a default branch.
- ``PlanPushes``: a plan run pushes a default branch only when its plan names exactly that branch for the repo.
- ``StepPushes``: a run of one step, and a run of any other kind, never pushes a default branch; the push of a run of
  one step replays its commits on a branch the remote moved on.

A push goes to refs/heads/ alone (``gitops.send``), so no rule ever lets a tag through.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass

from evo_agents.worker.gitops import PROTECTED, PushRefused


class PushRule:
    """Whether a push of a kind of run to ``branch`` goes to a default branch of the repo (one of ``protected``): False
    when it does not, True when it does and the rule lets it; PushRefused otherwise."""

    replays = False  # a push to a branch the remote moved on replays the run's commits on its tip (``git_replay``)

    def check(
        self, kind: str, branch: str, protected: Collection[str], plan_branch: str | None, named_branch: str | None
    ) -> bool:
        raise NotImplementedError


class PushesNothing(PushRule):
    def check(self, kind, branch, protected, plan_branch, named_branch) -> bool:
        raise PushRefused(f"a {kind} run reads and pushes nothing")


@dataclass(frozen=True)
class OwnBranches(PushRule):
    """Only branches of its own, never a default branch; ``named`` also lets the branch its task names for the repo."""

    who: str  # who pushes them, "a chat run"
    whose: str  # whose branches they are, "a chat run"
    form: str  # their form, "chat/<run>"
    test: Callable[[], Callable[[str], bool]]  # the test of a branch, imported once a push asks it
    named: bool = False

    def check(self, kind, branch, protected, plan_branch, named_branch) -> bool:
        if branch in protected or branch in PROTECTED:
            raise PushRefused(f"{branch} is a default branch: {self.who} never pushes it")
        if self.named and named_branch is not None and branch == named_branch:
            return False  # the branch the task names for the repo
        if not self.test()(branch):
            raise PushRefused(f"{branch} is not a branch of {self.whose}: {self.who} pushes {self.form} alone")
        return False


class PlanPushes(PushRule):
    def check(self, kind, branch, protected, plan_branch, named_branch) -> bool:
        if branch not in protected:
            return False
        if plan_branch is not None and branch == plan_branch:
            return True
        raise PushRefused(
            f"{branch} is a default branch of the repo and the plan does not name it for the repo: the worker pushes "
            "a default branch only when the plan names it"
        )


@dataclass(frozen=True)
class StepPushes(PushRule):
    replays: bool = False

    def check(self, kind, branch, protected, plan_branch, named_branch) -> bool:
        if branch not in protected:
            return False
        raise PushRefused(f"{branch} is a default branch: the worker never pushes it in a run of one step")


def _chat_test():
    from evo_agents.hub.chat_runs import is_chat_branch

    return is_chat_branch


def _task_test():
    from evo_agents.hub.task_runs import is_task_branch

    return is_task_branch


def _curator_test():
    from evo_agents.hub.judge import is_curator_branch

    return is_curator_branch


def _release_test():
    from evo_agents.hub.release_runs import is_release_branch

    return is_release_branch


NOTHING = PushesNothing()
CHAT = OwnBranches("a chat run", "a chat run", "chat/<run>", _chat_test)
TASK = OwnBranches("a task run", "a task run", "task/<run>", _task_test, named=True)
CURATOR = OwnBranches("a run of the Curator", "the Curator", "curator/...", _curator_test)
RELEASE = OwnBranches("a release run", "a release", "release/<version>", _release_test)
PLAN = PlanPushes()
STEP = StepPushes(replays=True)
OTHER = StepPushes()  # a kind no other rule names
