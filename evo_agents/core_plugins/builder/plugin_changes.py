"""The proposals of @builder of an action (plan plugin-market, criterion a8): a plugin a member asked @builder to make
for an industry, installing its release on the hub, and listing it on a market. ``proposals`` calls these for the three
kinds, as it calls ``opportunity_changes`` for an opportunity.

Their target is empty and their fields are in ``set``; none unsets a field, and none is versioned: proposing keeps
version 0, and approving does the action once, as applied version 1, which never rolls back.

- ``plugin`` (an admin of the project decides it): ``name`` (the plugin's id), ``industry``, ``need``, ``owner`` (of
  the GitHub repo), ``visibility`` (public, private) and ``price`` (``free``, or USD per hub per month); ``repo`` (the
  repo's name, the plugin's id when left out) and ``categories`` optional. Approving hands @builder the task in the
  proposal's thread, for the member who approved it: create the repo from the template, write and test the plugin,
  push it so its CI signs and releases it, add the repo to the project, then propose its install and its listing.
- ``plugin_install`` (a hub admin decides it): ``repo`` (OWNER/REPO), ``tag`` and ``signer`` optional. Approving
  downloads the release, trusts its signer for its publisher when the hub does not yet, and installs it as
  ``evo-agents hub plugin install --github`` does (``market_install.install_github``).
- ``plugin_listing`` (an admin of the project decides it): ``repo``, ``tag``, ``price``, ``industries``, and
  ``categories``, ``seller`` (paid) and ``index`` (OWNER/REPO of the market, the default one when left out) optional.
  Approving hands @builder the task in the thread: for a free plugin, a pull request that adds its entry to the index;
  for a paid one, the listing and the bundle on the seller's hub with the member's account.

@builder itself never publishes, installs or sets a secret: the CI of the repo publishes, the hub installs once a hub
admin approves, and the member sets the secrets of the CI the run names.
"""

from __future__ import annotations

import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.core_plugins.builder.errors import NOTHING, ProposalError
from evo_agents.hub import tables
from evo_agents.hub.credentials import dispatch_credential
from evo_agents.hub.server.chat_access import chat_access
from evo_agents.hub.server.chat_runs import Hand, hand_mentions, hub_reply
from evo_agents.hub.server.chat_store import insert_mentions
from evo_agents.hub.server.chats import resolve_mentions
from evo_agents.hub.server.market_install import GitHubInstallIn, MarketInstalled
from evo_agents.hub.server.projects import ProjectAccess
from evo_agents.hub.server.security import Principal
from evo_agents.hub.tables_settings import PROPOSAL_KINDS
from evo_agents.plugins.market import DEFAULT_SOURCE

PLUGIN, PLUGIN_INSTALL, PLUGIN_LISTING = PROPOSAL_KINDS[-3:]
ACTIONS = (PLUGIN, PLUGIN_INSTALL, PLUGIN_LISTING)
RUNS = (PLUGIN, PLUGIN_LISTING)  # approving one hands @builder a task in its thread
DONE = 1  # the applied version of an action
TEMPLATE = "maycuatroi1/evo-plugin-template"  # the GitHub template a new plugin's repo is made from
FIRST_TAG = "v0.1.0"
PLUGIN_ID = re.compile(r"[a-z][a-z0-9-]{0,62}")  # a manifest's id
SLUG = re.compile(r"[a-z][a-z0-9-]{0,31}")  # an industry or a category of plugin-market-v1
OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")  # a GitHub user or organisation
REPO_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")
REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
TAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
SIGNER = re.compile(r"(sigstore|ed25519):\S{1,600}")
SELLER = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
VISIBILITIES = ("public", "private")
FREE = "free"
MAX_USD = 100_000  # plugin-market-v1, price.usd_per_hub_month
MAX_NEED_CHARS = 2000
MAX_SLUGS = 10
MAX_OUTCOME_CHARS = 2000  # builder_proposals.outcome (0160)
FIELDS = {  # kind -> (the fields it needs, those it may set)
    PLUGIN: (("name", "industry", "need", "owner", "visibility", "price"), ("repo", "categories")),
    PLUGIN_INSTALL: (("repo",), ("tag", "signer")),
    PLUGIN_LISTING: (("repo", "tag", "price", "industries"), ("categories", "seller", "index")),
}

Install = Callable[[Principal, GitHubInstallIn], Awaitable[MarketInstalled]]


@dataclass(frozen=True)
class Done:
    """What approving an action did: the chat run of @builder it handed the task to, and what to say of it."""

    outcome: str
    run_id: int | None = None


# Checking


def _price(value: object) -> tuple[str | float | None, str | None]:
    if value == FREE:
        return FREE, None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None, f"price is {FREE!r} or the USD a hub pays each month, such as 49.5"
    if not 0.01 <= value <= MAX_USD or abs(round(value * 100) - value * 100) > 1e-6:
        return None, f"price: 0.01 to {MAX_USD} USD, with at most two decimals"
    return round(float(value), 2), None


def _slugs(name: str, value: object, *, needed: bool) -> str | None:
    if not isinstance(value, list) or not all(isinstance(item, str) and SLUG.fullmatch(item) for item in value):
        return f"{name}: a list of slugs such as retail or field-service"
    if needed and not value:
        return f"{name}: at least one"
    if len(value) > MAX_SLUGS or len(set(value)) != len(value):
        return f"{name}: at most {MAX_SLUGS}, each once"
    return None


def _named(name: str, value: object, pattern: re.Pattern, shape: str) -> str | None:
    return None if isinstance(value, str) and pattern.fullmatch(value) else f"{name}: {shape}"


def _field_problem(kind: str, name: str, value: object) -> str | None:
    if name == "repo" and kind != PLUGIN:
        return _named(name, value, REPO, "OWNER/REPO on GitHub")
    if name == "price":
        return _price(value)[1]
    if name in ("categories", "industries"):
        return _slugs(name, value, needed=name == "industries")
    if name == "industry":
        return _named(name, value, SLUG, "a slug such as retail or field-service")
    if name == "need":
        text = " ".join(str(value).split()) if isinstance(value, str) else ""
        return None if 0 < len(text) <= MAX_NEED_CHARS else f"need: 1 to {MAX_NEED_CHARS} characters"
    if name == "visibility":
        return None if value in VISIBILITIES else f"visibility is one of {', '.join(VISIBILITIES)}"
    shapes = {
        "name": (PLUGIN_ID, "a plugin's id: lower case letters, digits and -, starting with a letter"),
        "owner": (OWNER, "a GitHub user or organisation"),
        "repo": (REPO_NAME, "the repo's name on GitHub"),
        "tag": (TAG, "a release's tag, such as v0.1.0"),
        "signer": (SIGNER, "sigstore:OWNER/REPO/.github/workflows/FILE.yml or ed25519:KEY"),
        "seller": (SELLER, "the id of a seller of the market"),
        "index": (REPO, "OWNER/REPO of the market's index"),
    }
    pattern, shape = shapes[name]
    return _named(name, value, pattern, shape)


def field_problems(kind: str, sets: object, unset: object) -> list[str]:
    """What is wrong with the fields a proposal of action ``kind`` sets and unsets."""
    if not isinstance(sets, dict) or unset:
        return [f"a {kind} proposal sets its fields and unsets none"]
    needed, allowed = FIELDS[kind]
    problems = [f"{name}: a {kind} proposal needs it" for name in needed if name not in sets]
    extra = sorted(set(sets) - set(needed) - set(allowed))
    if extra:
        problems.append(f"{', '.join(extra)}: a {kind} proposal sets {', '.join(needed + allowed)}")
    for name in [name for name in (*needed, *allowed) if name in sets]:
        problem = _field_problem(kind, name, sets[name])
        problems += [problem] if problem else []
    if kind == PLUGIN_LISTING and "seller" in sets and sets.get("price") == FREE:
        problems.append("seller: a free plugin is sold by no seller")
    return problems


def values(sets: Mapping) -> dict:
    """The fields as the hub keeps them: the need on one line, the price rounded to cents."""
    kept = dict(sets)
    if "need" in kept:
        kept["need"] = " ".join(kept["need"].split())
    if "price" in kept:
        kept["price"] = _price(kept["price"])[0]
    return kept


def repo_of(sets: Mapping) -> str:
    """OWNER/REPO of the repo a ``plugin`` proposal makes."""
    return f"{sets['owner']}/{sets.get('repo') or sets['name']}"


def title(kind: str, sets: Mapping, project: str) -> str:
    """What a proposal of action ``kind`` changes, for its message in the thread."""
    if kind == PLUGIN:
        return f"a new plugin {sets.get('name')} for project {project}, in the repo {repo_of(sets)}"
    if kind == PLUGIN_INSTALL:
        return f"the plugins installed on this hub, from the release of {sets.get('repo')}"
    return f"the market {sets.get('index') or DEFAULT_SOURCE}, with the plugin of {sets.get('repo')}"


async def check(conn: AsyncConnection, access: ProjectAccess, kind: str, sets: Mapping) -> None:
    """ProposalError (409) when the hub already has what a ``plugin`` proposal would make."""
    if kind != PLUGIN:
        return
    p, r = tables.plugins, tables.project_repos
    name, repo = sets["name"], sets.get("repo") or sets["name"]
    if (await conn.execute(select(p.c.id).where(p.c.name == name))).first() is not None:
        raise ProposalError(409, f"this hub has a plugin {name} already: name the new one otherwise; {NOTHING}")
    taken = select(r.c.name).where(r.c.project_id == access.project_id, func.lower(r.c.name) == repo.lower())
    if (await conn.execute(taken)).first() is not None:
        raise ProposalError(409, f"project {access.name} has a repo {repo} already: name the new one otherwise; {NOTHING}")


# Approving


def _said(text: str) -> str:
    return " ".join(text.split())[:MAX_OUTCOME_CHARS]


def task_text(proposal_id: int, kind: str, sets: Mapping, project: str, approver: str, handle: str) -> str:
    """The hub's message in the thread that hands @``handle`` the task of approved proposal ``proposal_id``."""
    head = f"@{handle} {approver} approved proposal {proposal_id}."
    if kind == PLUGIN:
        repo, price = repo_of(sets), sets["price"]
        paid = price != FREE
        categories = ", ".join(sets.get("categories") or ()) or "none"
        return (
            f"{head} Make the plugin {sets['name']} for the industry {sets['industry']}: {sets['need']}\n\n"
            f"1. `gh repo create {repo} --template {TEMPLATE} --{sets['visibility']} --clone`, then rename the "
            "template's plugin as its `docs/rename.md` says: the id, the publisher and the signer.\n"
            "2. Write the plugin for that need: the manifest `evo-plugin.json`, the backend, the pages with the hub's "
            "UI kit, and their tests.\n"
            "3. `evo-agents plugin test` until it passes, then push `main`.\n"
            f"4. Tag `{FIRST_TAG}` and push the tag: the repo's CI signs the bundle and makes the GitHub Release"
            + (
                f"; the plugin is sold at {price} USD per hub per month, so ask {approver} to set the variable "
                "EVO_SELLER_PUBLISH=true and the token of the environment seller, which the job publish-seller uploads "
                "with"
                if paid
                else ""
            )
            + ". You set no secret yourself.\n"
            f"5. Add the repo to project {project} (the harness's harness.yaml, then `evo-agents hub project register`), "
            "so the runs after this one open it.\n"
            f"6. Propose with builder__propose, each for {approver} to approve: kind plugin_install (repo {repo}, tag "
            f"{FIRST_TAG}), and kind plugin_listing (repo {repo}, tag {FIRST_TAG}, price "
            f"{price if paid else FREE}, industries [{sets['industry']}], categories [{categories}]). You install and "
            "list nothing yourself."
        )
    repo, tag, price = sets["repo"], sets["tag"], sets["price"]
    industries = ", ".join(sets["industries"])
    categories = ", ".join(sets.get("categories") or ()) or "none"
    if price == FREE:
        index = sets.get("index") or DEFAULT_SOURCE
        return (
            f"{head} List the free plugin of {repo}, release {tag}, on the market {index}: fork it and open a pull "
            f"request that adds the plugin's entry to its market.json (plugin-market-v1: the id, name and description "
            f"of the release's manifest, industries [{industries}], categories [{categories}], repo {repo}, the "
            "publisher and its signer, tier isolated, price free), as its CONTRIBUTING.md asks, with "
            f"`evo-agents plugin market-check market.json --releases` passing. Tell {approver} its link; you merge "
            "nothing."
        )
    seller = f" for the seller {sets['seller']}" if sets.get("seller") else ""
    return (
        f"{head} List the plugin of {repo}, release {tag}, for sale at {price} USD per hub per month{seller}, on the "
        f"seller's hub with {approver}'s account: `evo-agents hub market listing add PLUGIN --publisher PUBLISHER "
        f"--signer SIGNER --usd {price}`, then `evo-agents hub market bundle upload PLUGIN FILE --signature FILE` with "
        f"the bundle and signature of release {tag}. Industries [{industries}], categories [{categories}]. If the "
        f"account holds no seller grant there, say so to {approver} and stop."
    )


async def _builder_handle(conn: AsyncConnection, run_id: int | None) -> str:
    r, f = tables.runs, tables.agent_profiles
    query = select(f.c.handle).join_from(r, f, f.c.id == r.c.profile_id).where(r.c.id == run_id)
    handle = None if run_id is None else (await conn.execute(query)).scalar_one_or_none()
    if handle is None:
        raise ProposalError(409, "the chat run of @builder this proposal came from is gone: propose it again")
    return handle


async def ready(conn: AsyncConnection, user: Principal, row) -> None:
    """ProposalError (409), the proposal left pending, when approving ``row`` cannot hand its task in its thread:
    the thread is gone, or ``user`` is not in its chat."""
    if row.kind not in RUNS:
        return
    if row.chat_id is None or row.thread_root_id is None:
        raise ProposalError(409, f"the thread of proposal {row.id} is gone: ask @builder to propose it again")
    try:
        await chat_access(conn, user, row.chat_id)
    except HTTPException:
        raise ProposalError(
            409,
            f"approving proposal {row.id} hands @builder its task in its thread for {user.login}, who is not in that "
            "chat: join it, or answer `approve` there",
        ) from None


async def _hand(conn: AsyncConnection, user: Principal, access: ProjectAccess, row, sets: Mapping) -> Done:
    handle = await _builder_handle(conn, row.run_id)
    chat = await chat_access(conn, user, row.chat_id)
    text = task_text(row.id, row.kind, sets, access.name, user.login, handle)
    message_id = await hub_reply(conn, row.chat_id, row.thread_root_id, text)
    kept, _, _ = await resolve_mentions(conn, chat, user, [handle])
    via = dispatch_credential(user.kind)
    hand = Hand(chat, message_id, row.thread_root_id, None, user.user_id, user.login, via, 1, "the hub", text)
    await hand_mentions(conn, hand, kept)
    refused = [mention["refusal"] for mention in kept if mention["refusal"]]
    if refused:
        raise ProposalError(409, "; ".join(refused))
    await insert_mentions(conn, message_id, kept)
    run_id = next((mention.get("run_id") for mention in kept if mention.get("run_id")), None)
    doing = "make the plugin" if row.kind == PLUGIN else "list the plugin"
    return Done(_said(f"@{handle} took the task to {doing} in the thread, as run #{run_id}"), run_id)


async def apply(
    conn: AsyncConnection, user: Principal, access: ProjectAccess, row, sets: Mapping, install: Install | None
) -> Done:
    """Do approved action ``row`` once, for ``user``: hand @builder its task, or install the release."""
    if row.kind in RUNS:
        if row.kind == PLUGIN:
            await check(conn, access, row.kind, sets)
        return await _hand(conn, user, access, row, sets)
    if install is None:
        raise ProposalError(503, f"this hub cannot install a plugin from here: approve it with the route; {NOTHING}")
    try:
        body = GitHubInstallIn(repo=sets["repo"], tag=sets.get("tag"), signer=sets.get("signer"), trust_publisher=True)
    except ValidationError as exc:
        raise ProposalError(422, f"{exc.errors()[0]['msg']}; {NOTHING}") from None
    done = await install(user, body)
    plugin = done.plugin
    trusted = " and trusts its signer for its publisher from now on" if done.trusted_publisher else ""
    return Done(_said(f"the hub installed plugin {plugin.name} {plugin.installed_version}{trusted}"))
