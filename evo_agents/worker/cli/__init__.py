"""``evo-agents worker``: this machine as a worker of a hub. Each command is a module of this package.

- ``join --url URL --code CODE`` (``join``): trade a pairing code from the web, or a pool's join token, for a worker
  token. ``register --name NAME --project P ... --slots N`` (``register``): register this machine directly, with the
  machine token of ``evo-agents hub login``.
- ``run`` (``run``): the daemon in the foreground (``daemon``). ``service install|uninstall|status`` (``service``): the
  daemon as a background service of this user.
- ``status`` (``status``): what this machine is to the hub, and what the daemon sees here. ``doctor [--json]``
  (``doctor``): what this machine holds or allows that a worker should not; exit status 2 when a finding is high.
  ``selftest --runtime NAME`` (``selftest``): a runtime's adapter on a tiny prompt.
- ``attach N`` (``attach``): this terminal on the tmux session of run N while a person drives its agent.
- ``drain [--resume]``, ``revoke [--force]`` (``drain``, ``revoke``), ``projects``, ``list`` and ``pool``: the
  owner's controls, with the machine token.
- ``git-credential --run N get|store|erase`` and ``env --run N`` (``git_credential``, ``env``): git's credential
  helper for run N and the variables its leases add to its agent's environment; both need the core package only.
- ``browser install|login|state`` (``browser``): the Chromium of the runs dispatched with a browser.
- ``step``, ``ask``, ``wait``, ``notify``, ``finding``, ``propose``, ``plan``, ``put``, ``chat``, ``repo``,
  ``triage``, ``playbook``, ``output``, ``backup``, ``probe``, ``merge-check``, ``curator``, ``agent``, ``escalate``,
  ``work`` and ``ops``: the commands of a run's agent, a module each (``AgentCommand`` of ``base``). They read the run
  from EVO_RUN_ID, EVO_WORKER_HOME and the run's record there (``base.AgentRunContext``), call the hub with the
  worker's token, and refuse to run outside a run of a kind they serve.

The worker token goes to ``~/.evo/worker/token`` (0600) and is never printed. A failure is one ``error:`` line on
stderr and exit status 69, 75 or 1 (``client.exit_status``); a command lacking the worker extra says how to get it (2).
``run`` on a machine that is not a worker, such as one revoked with ``revoke``, exits ``home.EXIT_REVOKED`` (3, or
what EVO_WORKER_REVOKED_EXIT says), so a service does not start it again and again. ``run`` needs the extra (aiohttp)
and so does ``join``; the others need the core package only.
"""

from __future__ import annotations

import argparse

from evo_agents.clikit import Group
from evo_agents.worker.cli.agent import AGENT
from evo_agents.worker.cli.ask import ASK
from evo_agents.worker.cli.attach import ATTACH
from evo_agents.worker.cli.backup import BACKUP
from evo_agents.worker.cli.browser import BROWSER
from evo_agents.worker.cli.chat import CHAT
from evo_agents.worker.cli.curator import CURATOR
from evo_agents.worker.cli.doctor import DOCTOR
from evo_agents.worker.cli.drain import DRAIN
from evo_agents.worker.cli.env import ENV
from evo_agents.worker.cli.escalate import ESCALATE
from evo_agents.worker.cli.finding import FINDING
from evo_agents.worker.cli.git_credential import GIT_CREDENTIAL
from evo_agents.worker.cli.join import JOIN
from evo_agents.worker.cli.list import LIST
from evo_agents.worker.cli.merge_check import MERGE_CHECK
from evo_agents.worker.cli.notify import NOTIFY
from evo_agents.worker.cli.ops import OPS
from evo_agents.worker.cli.output import OUTPUT
from evo_agents.worker.cli.plan import PLAN
from evo_agents.worker.cli.playbook import PLAYBOOK
from evo_agents.worker.cli.pool import POOL
from evo_agents.worker.cli.probe import PROBE
from evo_agents.worker.cli.projects import PROJECTS
from evo_agents.worker.cli.propose import PROPOSE
from evo_agents.worker.cli.put import PUT
from evo_agents.worker.cli.register import REGISTER
from evo_agents.worker.cli.repo import REPO
from evo_agents.worker.cli.revoke import REVOKE
from evo_agents.worker.cli.run import RUN
from evo_agents.worker.cli.selftest import SELFTEST
from evo_agents.worker.cli.service import SERVICE
from evo_agents.worker.cli.status import STATUS
from evo_agents.worker.cli.step import STEP
from evo_agents.worker.cli.triage import TRIAGE
from evo_agents.worker.cli.wait import WAIT
from evo_agents.worker.cli.work import WORK


def tree() -> Group:
    """The ``worker`` group, its commands in the order ``--help`` lists them."""
    return Group(
        "worker",
        "this machine as a worker of a hub: it runs plan steps the hub hands it (not `hub worker`, the server's job "
        "worker)",
        (
            JOIN,
            REGISTER,
            RUN,
            SERVICE,
            STATUS,
            DOCTOR,
            SELFTEST,
            ATTACH,
            DRAIN,
            REVOKE,
            GIT_CREDENTIAL,
            ENV,
            *(STEP, ASK, WAIT, NOTIFY, FINDING, PROPOSE, PLAN, PUT),
            *(CHAT, REPO, TRIAGE, PLAYBOOK, OUTPUT, BACKUP, PROBE, MERGE_CHECK),
            *(CURATOR, AGENT, ESCALATE, WORK, OPS),
            PROJECTS,
            LIST,
            BROWSER,
            POOL,
        ),
        dest="worker_command",
    )


def register(sub: argparse._SubParsersAction) -> None:
    tree().attach(sub)
