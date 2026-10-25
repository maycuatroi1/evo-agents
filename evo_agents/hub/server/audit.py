"""The audit trail: one row per command that changes something, saying who did it (the user and the token they
used), the action, its target, the project it happened in and when. A target names things, such as a project, a
login, a host or a token id; it never holds content or a credential. Rows are inserted in the same transaction as
the change they describe, so a change that rolls back leaves no row, and never updated (schema 0001 rejects updates).
The hub's worker deletes the rows older than EVO_HUB_AUDIT_DAYS, 365 by default, once a day (hub.prune_audit).

The project (schema 0007) comes from the action and the target unless the caller names it: the first name of the
target for grant, project, plan, kg and blob actions, the project of ``skill:project/<project>/<name>``, and the
project of the memory a memory action names. Actions outside a project (signing in and out, tokens, global skills,
personal memories) have none.
"""

from __future__ import annotations

import re

from sqlalchemy import BigInteger, Insert, String, bindparam, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from evo_agents.hub import tables

LOGIN = "auth.login"
LOGOUT = "auth.logout"
TOKEN_REVOKE = "token.revoke"
# A member's emails belong to no project; a target names the row and the member, "email:<id> user=<login>", never the
# address (evo_agents.hub.server.user_emails).
EMAIL_ADD = "user_email.add"
EMAIL_REMOVE = "user_email.remove"
GRANT_PUT = "grant.put"
GRANT_DELETE = "grant.delete"
PROJECT_REGISTER = "project.register"
PROJECT_UPDATE = "project.update"
# An invite link (evo_agents.hub.server.invites) was made, accepted or revoked; the target names the link and what it
# gives, "invite:<id> kind=<kind> [project=<p> role=<r> max_level=<l>] [for=<login>]", never its token. A join link's
# rows are in its project.
INVITE_CREATE = "invite.create"
INVITE_ACCEPT = "invite.accept"
INVITE_REVOKE = "invite.revoke"
BLOB_COMMIT = "blob.commit"  # the target is the project, never the hashes
KG_CONFIG = "kg.config"  # the knowledge config a project's graph is built with changed; the target is the project
KG_INGEST = "kg.ingest"  # a pushed run became part of the project's corpus; the target is the project, never the run
KG_BUILD = "kg.build"  # a member queued a build; the target is the project
KG_PRUNE = "kg.prune"  # the retention deleted artifacts of a project's older graphs; the target is "<project> keep=<n>"
# An admin deleted old graphs and source blobs of a project that hold a secret (evo_agents.hub.server.kg_scrub); the
# target counts them, "<project> artifacts=<n> blobs=<n>", never a hash or a value.
KG_SCRUB = "kg.scrub"
# A member took every run of a source off the hub (evo_agents.hub.server.kg): "<project> source=<id> runs=<n>".
KG_FORGET = "kg.forget"
# A writer dismissed an item of the review queue (evo_agents.hub.server.kg_review): "<project> item=<fingerprint>".
KG_REVIEW = "kg.review"
# A writer took, accepted, rejected or undid a decision on the graph, or an agent proposed one
# (evo_agents.hub.server.kg_decisions): "<project> decision=<number> action=<action>".
KG_DECISION = "kg.decision"
# Workers belong to no project; a target names the pairing or the worker by id and name, never a code or a token.
WORKER_PAIR = "worker.pair"  # a member created a pairing code: "pairing:<id> name=<worker name>"
WORKER_JOIN = "worker.join"  # a machine joined with a code, as its owner with the new worker token
WORKER_REGISTER = "worker.register"  # a machine registered itself with its machine token
WORKER_DRAIN = "worker.drain"
WORKER_UNDRAIN = "worker.undrain"
WORKER_REVOKE = "worker.revoke"  # the worker and its token are revoked, its runs released
# The owner set, on the web, who may hand the worker runs: "worker:<id> name=<n> owner=<login> dispatch_from=<any|web>"
WORKER_DISPATCH_FROM = "worker.dispatch_from"
# The owner changed the worker's settings; the target names each field that changed, "<field>=<was>-><is>", after
# "worker:<id> name=<n> owner=<login>": "slots=1->4 labels=[]->[gpu,mac] name=old->new", never a token.
WORKER_UPDATE = "worker.update"
# The owner or a hub admin asked the worker's daemon to update itself to nightly now, or cancelled that request:
# "worker:<id> name=<n> owner=<login>" (evo_agents.hub.server.update_now)
WORKER_UPDATE_NOW = "worker.update_now"
WORKER_UPDATE_NOW_CANCEL = "worker.update_now_cancel"
# The owner or a hub admin asked the worker's daemon to install Chromium, or cancelled that request:
# "worker:<id> name=<n> owner=<login>" (evo_agents.hub.server.browser_install)
WORKER_BROWSER_INSTALL = "worker.browser_install"
WORKER_BROWSER_INSTALL_CANCEL = "worker.browser_install_cancel"
# The owner or a hub admin narrowed a shared worker to a unit of the org chart, or took the unit back
# (evo_agents.hub.server.worker_shares): "worker:<id> name=<n> owner=<login> unit=<slug> by=<owner|admin>", then
# " shared=false->true" when sharing turned the worker shared, " shared=true->false" when its last unit went.
WORKER_SHARE = "worker.share"
WORKER_UNSHARE = "worker.unshare"
# Runs happen in a project, which the caller names; a target is "<project>/<plan>#<step> run:<id>" ("<project>/<plan>
# run:<id>" for a plan run), never a prompt or evidence.
RUN_DISPATCH = "run.dispatch"  # a member queued a run of a ready step
RUN_DISPATCH_PLAN = "run.dispatch_plan"  # a member queued a plan run: "<project>/<plan> run:<id>", no step
RUN_DISPATCH_AUTHOR = "run.dispatch_author"  # a member queued an author run: "<project>/author run:<id>", no request
RUN_STEP_REPORT = "run.step_report"  # a plan run's worker wrote a step, as the dispatcher: "... run:<id> status=<s>"
RUN_CANCEL = "run.cancel"  # the owner cancelled a run, or asked its worker to stop it
RUN_APPROVE = "run.approve"  # the owner approved a run in review, and the step is done
RUN_TRIAGE_RERUN = "run.triage_rerun"  # the agent of a triage thread ran a failed run again for its owner
RUN_TRIAGE_INVITE = "run.triage_invite"  # ... or invited the agent of another project the error names into the thread
RUN_TRIAGE_PLAN_EDIT = "run.triage_plan_edit"  # ... or edited verify or what of a pending step, as the owner
RUN_RERUN = "run.rerun"  # the owner queued the step again after a run that ended
RUN_MESSAGE = "run.message"  # the owner sent the run's agent a message; the target names the message, not its text
# The dispatcher of a run or an admin of its project deleted an output of it (evo_agents.hub.server.run_outputs): the
# run's target, then " output:<id>", never its name or its bytes.
RUN_OUTPUT_DELETE = "run.output_delete"
RUN_TAKEOVER = "run.takeover"  # the owner asked to drive the run's agent in a terminal
RUN_HANDBACK = "run.handback"  # the owner asked to let the run's agent go on headless
RUN_FINISH = "run.finish"  # the owner ended the chat of an author run: "<project>/author run:<id>"
# The worker of a chat run added a repo of the project to it, as its agent asked: "<project>/chat run:<id> repo=<name>"
RUN_OPEN_REPO = "run.open_repo"
# Decisions and notifications; a target names the decision or the notifications by id, and an answer's option, never
# the question, the answer's text or a notification's title.
DECISION_ANSWER = "decision.answer"  # the owner answered: "<project>/<plan>[#<step>] decision:<id> run:<id> option=<k>"
NOTIFICATION_READ = "notification.read"  # a member marked notifications read: "notifications:<id>,<id>" or ":all"
# A member turned kinds of notice on or off: "notifications:settings run_outputs=off" (server.output_notices).
NOTIFICATION_SETTINGS = "notification.settings"
# The web terminal of a run; the target names the run as above, and on close the bytes relayed each way and how it
# ended ("... to_worker=<n> to_browser=<n> end=<idle|timeout|browser|worker|protocol|shutdown>"), never the bytes.
TERMINAL_OPEN = "terminal.open"  # the owner opened the run's terminal in a browser
TERMINAL_CLOSE = "terminal.close"
# A member's secrets belong to no one project; the target is the secret's name, never its value.
SECRET_PUT = "secret.put"  # the owner created a secret or replaced it, value included
SECRET_DELETE = "secret.delete"  # the owner deleted it: its sealed value and bindings went, its leases were revoked
# A share of a secret or an account (evo_agents.hub.server.credential_shares), in no project; the target names the share
# and the secret by id, never the secret's name: "share:<id> secret:<id> credential=<class> grantee=<kind>[:<login or
# slug>] projects=<names> level=<level> budget_usd=<n> usage_ceiling_pct=<n> max_running=<n> expires=<when>", each limit
# that is set. share.terms_ack follows share.create for a subscription token: "share:<id> secret:<id> warning=<the
# terms warning, word for word>". share.revoke: "share:<id> secret:<id> by=<owner|admin>".
SHARE_CREATE = "share.create"
SHARE_TERMS_ACK = "share.terms_ack"
SHARE_REVOKE = "share.revoke"
# A member's accounts (docs/credentials.md, section Accounts): an env secret's settings, by its id ("secret:<id>
# weight=<n> priority=<n> paused=<yes|no>"), and the member's policy for a project, in that project ("<project>
# strategy=<s> cooldown=<seconds> failover=<yes|no> max_failovers=<n>", or the project alone when it was dropped).
ACCOUNT_SET = "account.set"  # the owner changed the weight or priority of an account, or paused or resumed it
ACCOUNT_POLICY_PUT = "account.policy-put"  # the member set their policy for the project
ACCOUNT_POLICY_DELETE = "account.policy-delete"  # the member dropped it: the defaults hold again
# A run's account hit its usage limit and the run moved to another one, as its worker (the actor) reported it, in the
# run's project: "<run target> env=<variable> from=secret:<id> to=secret:<id>".
ACCOUNT_FAILOVER = "account.failover"
# A project's agent profiles (evo_agents.hub.server.agent_profiles), in the project; the target is
# "<project>/@<handle> v<version>", with " fields=<names>" for an edit, never a description, instructions or an image.
AGENT_CREATE = "agent.create"  # an admin of the project created an agent profile, at version 1
AGENT_UPDATE = "agent.update"  # an admin changed it, its avatar included: the version it made and the fields changed
AGENT_DELETE = "agent.delete"  # an admin soft-deleted it: it takes no new run
# The accounts of an agent profile (evo_agents.hub.server.agent_accounts), in the project; the target is
# "<project>/@<handle> secret:<id>", with " fields=<names>" for a change, never a name of the account or a value.
AGENT_ACCOUNT_PUT = "agent-account.put"  # an admin gave the agent an account, or replaced the one of that name
AGENT_ACCOUNT_SET = "agent-account.set"  # an admin renewed it or changed its weight, priority, workers, end or pause
AGENT_ACCOUNT_DELETE = "agent-account.delete"  # an admin deleted it: value and bindings went, its leases were revoked
# Browser identities (evo_agents.hub.server.browser_identities, browser_profiles, browser_leases), in the project; the
# target is "<project> browser-identity:<id>", with " version=<n>" for a profile saved or handed out and " run:<id>"
# for a run's, never a domain, a cookie or a key. A run's are in the name of its dispatcher, with its worker's token.
BROWSER_IDENTITY_CREATE = "browser_identity.create"  # a member made one of their own, or an admin one of the project
BROWSER_IDENTITY_DELETE = "browser_identity.delete"  # its owner or an admin deleted it: its keys and blobs went
BROWSER_IDENTITY_SAVE = "browser_identity.save"  # a new version of its profile was sealed; the one before the last went
BROWSER_IDENTITY_LEASE = "browser_identity.lease"  # the worker of a run that holds it was handed its profile
BROWSER_IDENTITY_LOGIN = (
    "browser_identity.login"  # a login session of it was queued: "<project> browser-identity:<id> run:<id>"
)
BROWSER_IDENTITY_DISABLE = "browser_identity.disable"  # its member or an admin disabled it: no run attaches it
BROWSER_IDENTITY_ENABLE = "browser_identity.enable"  # it was enabled again
BROWSER_IDENTITY_DEFAULT = "browser_identity.default"  # an admin made it the identity its agent hands its runs
BROWSER_IDENTITY_NEEDS_LOGIN = "browser_identity.needs_login"  # its profile did not open: it waits for a new login
# The live view of a run's browser (evo_agents.hub.server.browser_view), in the run's project and the name of who
# dispatched it; the target names the run as above, then "start", or "end=<how>" with the screen frames and events
# relayed, never a key, a text, a URL or a frame.
BROWSER_VIEW = "browser.view"  # the dispatcher opened the run's browser in the live view, or the view ended
BROWSER_CONTROL = "browser.control"  # in a view, the dispatcher got the browser to drive (mode human), or gave it back
# Group chats (evo_agents.hub.server.chats); the target is "chat:<id>", then "person:<login>", "agent:@<handle>" or
# "message:<id>", never a title or a message's text. Adding or removing an agent is in the agent's project.
CHAT_CREATE = "chat.create"  # a member created a chat, with the people and agents it started with
CHAT_ADD = "chat.add"  # a member added a person or an agent
CHAT_REMOVE = "chat.remove"  # a member removed one, or left
CHAT_MESSAGE = "chat.message"  # a member sent a message, or the agent of a run did for its dispatcher
# A member set the image of a group chat or took it off: "chat:<id> image:<the first 16 of its SHA-256>|none".
CHAT_IMAGE = "chat.image"
# The hub's store of plugins (evo_agents.hub.server.plugin_store), outside any project; the target is
# "plugin:<name>@<version>", never a manifest, a bundle or a signature.
PLUGIN_PUBLISH = "plugin.publish"  # a hub admin published a signed version, which the hub now holds
PLUGIN_INSTALL = "plugin.install"  # a hub admin installed a version, or installed one again after the hub turned it off
PLUGIN_UPGRADE = "plugin.upgrade"  # a hub admin moved the installed version to a newer one
PLUGIN_REMOVE = "plugin.remove"  # a hub admin removed the installed version; the versions stay in the store
PLUGIN_DISABLE = "plugin.disable"  # the hub turned off an installed version that no longer runs on its release
# The plugin market (evo_agents.hub.server.market), outside any project: "publisher:<id> signer:<signer>" for a
# publisher a hub admin trusted or stopped trusting, "market:<OWNER/REPO>" for a market source added or removed.
PLUGIN_PUBLISHER_ADD = "plugin.publisher.add"
PLUGIN_PUBLISHER_REMOVE = "plugin.publisher.remove"
MARKET_SOURCE_ADD = "market.source.add"
MARKET_SOURCE_REMOVE = "market.source.remove"
# A project admin asked the hub admins to install a plugin of a market, in that project:
# "<project>/market:<source id>/<plugin>".
MARKET_REQUEST = "market.request"
# Plugins in a project (evo_agents.hub.server.plugin_grants), in that project; the target is
# "<project>/plugin:<name>@<version>", then " permissions=<names>" for a grant.
PLUGIN_PROJECT_ENABLE = "plugin.project-enable"  # an admin of the project enabled an installed plugin for it
PLUGIN_GRANT = "plugin.grant"  # an admin granted the permissions the installed version asks for, enabling it or after
PLUGIN_PROJECT_DISABLE = "plugin.project-disable"  # an admin disabled it for the project: its grants went
# Agent types of plugins in a project (evo_agents.hub.server.agent_types), in that project; the target is
# "<project>/plugin:<name> agent-type:<type>", then " @<handle> run-as=<login> workers=<names> daily-usd=<cap>" for an
# enable, or " run:<id>" for a task, whose actor is the member it runs as, with no token: the plugin's token handed it.
AGENT_TYPE_ENABLE = "agent-type.enable"  # an admin of the project enabled it, or changed how; its profile on the first
AGENT_TYPE_DISABLE = "agent-type.disable"  # an admin turned it off: the plugin's tasks are refused
PLUGIN_NOTIFY = (
    "plugin.notify"  # the plugin, granted notifications:send, sent members a notice: who and how many, no text
)
TASK_DISPATCH = "task.dispatch"  # the plugin, granted runs:dispatch, handed it a task: a run of kind task
# A new version of a stored layer of a plugin's config (evo_agents.hub.server.plugin_configs): "plugin:<name> config
# instance v<n>" outside any project, "<project>/plugin:<name> config project v<n>" or "<project>/@<handle>/plugin:
# <name> config agent v<n>" in the project, then " fields=<names>" or " rollback-to=v<n>"; never a value.
PLUGIN_CONFIG = "plugin.config"
# A new version of a layer of the hub's own settings (evo_agents.hub.server.hub_settings): "branding v<n>" or
# "features v<n>" for the hub's, "<project>/features v<n>" in the project, then " fields=<names>" or
# " rollback-to=v<n>"; never a value.
SETTINGS_BRANDING = "settings.branding"
SETTINGS_FEATURES = "settings.features"
# An admin of a project switched the images taken out of tools' results on or off (evo_agents.hub.server.run_outputs):
# "<project>/outputs tool_images=on|off".
SETTINGS_OUTPUTS = "settings.outputs"
# A new revision of a project's autonomy policy (evo_agents.hub.server.autonomy_policies), in the project:
# "<project>/policy revision=<n> keys=<the dotted keys whose value changed>", then " proposal=<id>" when an admin
# approved or rolled back a proposal of @builder, and " copy-of=<n>" for a rollback; never a value.
POLICY_SET = "policy.set"
# A new revision of a goal of a project (evo_agents.hub.server.goals), in the project: "<project>/goal G-<n>
# revision=<n> fields=<the fields whose value changed>", then " proposal=<id>" when an admin approved or rolled back a
# proposal of @builder, " copy-of=<n>" for a rollback, and " charter=<revision>" with no actor when the hub made the
# goal from a goal of the Curator's charter; never a value.
GOAL_SET = "goal.set"
# A new revision of a problem of a project (evo_agents.hub.server.problems), in the project: "<project>/problem P-<n>
# revision=<n> fields=<the fields whose value changed>", then " proposal=<id>" when a writer approved or rolled back a
# proposal of @builder, and " copy-of=<n>" for a rollback; never a value.
PROBLEM_SET = "problem.set"
# Lines of evidence of a problem added or confirmed (evo_agents.hub.server.problem_evidence), in the project:
# "<project>/problem P-<n> evidence=E-<n>[,E-<n>...]" with " added" or " confirmed", then " proposal=<id>" when a writer
# approved a proposal of @builder that carried them, or " removed proposal=<id>" when a rollback took them away; never
# an excerpt.
PROBLEM_EVIDENCE = "problem.evidence"
# A revision of an opportunity of a project written (evo_agents.hub.server.opportunities), in the project:
# "<project>/opportunity O-<n> revision=<n> fields=<the fields whose value changed>", then " proposal=<id>" when a writer
# approved or rolled back a proposal of @builder, and " copy-of=<n>" for a rollback; never a value.
OPPORTUNITY_SET = "opportunity.set"
# Lines of evidence of an opportunity added or confirmed (evo_agents.hub.server.opportunity_evidence), in the project:
# "<project>/opportunity O-<n> evidence=E-<n>[,E-<n>...]" with " added" or " confirmed", then " proposal=<id>" when a
# writer approved a proposal that carried them, or " removed proposal=<id>" for a rollback; never an excerpt.
OPPORTUNITY_EVIDENCE = "opportunity.evidence"
# An intake of a project started or its result put (evo_agents.hub.server.intake), in the project: "<project>/intake
# I-<n>" with " started sources=<count>", or " put candidates=<n> promises=<n> questions=<n> proposals=<ids>
# tasks=<count>"; never a source's name or an excerpt.
INTAKE_START = "intake.start"
INTAKE_PUT = "intake.put"
# The org chart belongs to no project (evo_agents.hub.server.org_store). A target names the unit by slug and the seat
# by id with its holder, then each field that changed as "<field>=<was>-><is>" and the unit's revision after it:
# "unit:<slug> parent=<slug> kind=<kind> revision=1", "seat:<id> unit=<slug> login=<l> role=<role> primary=true->false
# revision=<n>"; never a ceiling's note.
ORG_UNIT_CREATE = "org.unit_create"
ORG_UNIT_EDIT = "org.unit_edit"
ORG_UNIT_ARCHIVE = "org.unit_archive"
ORG_SEAT_ADD = "org.seat_add"
ORG_SEAT_EDIT = "org.seat_edit"
ORG_SEAT_END = "org.seat_end"
# Autonomy levels belong to no project (evo_agents.hub.server.autonomy_store, .org_writes; plan autonomy-levels): a
# ceiling revision of a unit or seat, "ceiling unit:<slug> level=A5->A1[ K5=ask ...][ environments=<e,...>]
# [ workers=<w,...>] revision=<n>", and a revision of a person's own preference, "pref <login> level=A5->A2
# [ K6=deny ...][ reset] revision=<n>"; never a ceiling's note.
AUTONOMY_CEILING_SET = "autonomy.ceiling_set"
AUTONOMY_PREF_SET = "autonomy.pref_set"
# A gate of autonomy, in the run's project, with the run's principal as actor (evo_agents.hub.server.autonomy_gates,
# .lease_autonomy, .route_autonomy): "run:<id> <class> <act> mode=<mode>" when it left an act undone (once for each
# act), "run:<id> <class> <act> decision:<id>" when it asked the principal, "run:<id> <class> <act>" when it acted and
# told the principal; a route refused to a run's credential is "<class> <route> mode=<mode> source=<source> run:<id>".
AUTONOMY_REFUSED = "autonomy.refused"
AUTONOMY_ASKED = "autonomy.asked"
AUTONOMY_NOTIFIED = "autonomy.notified"
# A backup the agent of a run made before deleting data (evo_agents.hub.server.worker_autonomy, the floor S2), in the
# run's project, with the worker's owner as actor: "run:<id> <target> at=<location> made=<ISO time>".
AUTONOMY_BACKUP = "autonomy.backup"
# The ledger S1 of autonomy (evo_agents.hub.server.autonomy_ledger): every decision a gate of the hub made for a run,
# in the run's project, with the run's principal as actor, once for each distinct decision: "run:<id> principal=<login>
# agent=<handle> <class> mode=<mode> result=<acted|asked|refused|allowed|granted|withheld> <act>", "-" for a principal
# or an agent the run has none of.
AUTONOMY_DECIDED = "autonomy.decided"
# The stop S4 and its lift (evo_agents.hub.server.autonomy_stops), in no project, with who stopped or lifted as actor:
# "stop:<id> <login:<login>|unit:<slug>|all> runs=<n cancelled or asked to stop> leases=<n ended>" and "stop:<id>
# <login:<login>|unit:<slug>|all>"; never the reason.
AUTONOMY_STOP = "autonomy.stop"
AUTONOMY_STOP_LIFT = "autonomy.stop.lift"
# The hub's merge queue on GitLab (evo_agents.hub.server.merge_gate, .merge_judge, .merge_turn), in the project, with
# no actor but the policy's admin for a judge run: "<project>/<repo> !<iid> source=<source>[ plan=<id>] run:<id>" when it
# opened the merge request of a run, "<project>/<repo> !<iid> run:<id> first|second model=<model>" when it queued a
# Judge, "<project>/<repo> !<iid> into=<branch> sha=<merged head> before=<the branch's head before>" when it merged one.
MERGE_OPEN = "merge.open"
MERGE_JUDGE = "merge.judge"
MERGE_MERGE = "merge.merge"
# An admin passed the hold of a merge request at a gate that holds it for a person (evo_agents.hub.server.merge_pass),
# in the project: "<project>/<repo> !<iid> gate=<n> sha=<the head it holds> reason=<the admin's reason, cut>".
MERGE_PASS = "merge.pass"
# What follows a merge of the queue (evo_agents.hub.server.post_merge), in the project: the hub ran the PROD deploy job,
# "<project>/<repo> !<iid> job=<name> sha=<commit>[ carries=<!iid,...>]", or the rollback job, "... job=<name>
# to=<commit>", or opened a revert, "<project>/<repo> !<iid> revert=!<iid> branch=<branch>"; an admin approved a PROD
# release, "<project>/<repo> !<iid> approved", asked for a revert, "<project>/<repo> !<iid> revert=asked", or answered
# an audit sample, "<project>/<repo> !<iid> sample=<id> result=<ok|faulty>". Never a note.
MERGE_DEPLOY = "merge.deploy"
MERGE_ROLLBACK = "merge.rollback"
MERGE_REVERT = "merge.revert"
MERGE_RELEASE = "merge.release"
MERGE_SAMPLE = "merge.sample"
# The breaker of a repo of the merge queue (evo_agents.hub.server.merge_breaker), in the project: it tripped,
# "<project>/<repo> cause=<cause> causes=<cause,...> paused=<!iid,...>", or an admin reset it, "<project>/<repo>
# resumed=<!iid,...>".
MERGE_BREAKER = "merge.breaker"
MERGE_RESUME = "merge.resume"
# The leases of a run, in the run's project; the target names the run as above, then the secrets by name, the GitHub
# App's accounts and the repos, never a value: "... run:<id> secrets=<names> github-app=<accounts> repos=<repos>".
CREDENTIAL_LEASE = "credential.lease"  # the worker holding the run asked for its leases
# Leases given back: "... run:<id> leases=<n> secrets=<names> github-app=<tokens> by=<worker|run-<state>|...>",
# by the worker (its actor), or by the hub itself when the run left the held states or its worker was revoked.
CREDENTIAL_REVOKE = "credential.revoke"
# The signals of a project (evo_agents.hub.server.signal_keys), in the project: "<project> key=<name>" for its registry,
# "<project> ingest:<id> name=<name>" for an ingest key; never a token.
SIGNAL_KEY_ADD = "signal.key-add"  # an admin added a key to the registry
SIGNAL_KEY_ARCHIVE = "signal.key-archive"  # an admin archived one: it takes no new signal
SIGNAL_INGEST_CREATE = "signal.ingest-create"  # an admin created an ingest key for a service
SIGNAL_INGEST_REVOKE = "signal.ingest-revoke"  # an admin revoked one

NAMED_FAMILIES = frozenset({"grant", "project", "plan", "kg", "blob"})  # targets that start with the project's name
_FIRST_NAME = re.compile(r"([a-z0-9][a-z0-9-]{0,99})(?:[/ @]|$)")
_PROJECT_SKILL = re.compile(r"skill:project/([a-z0-9][a-z0-9-]{0,99})/")
_MEMORY = re.compile(r"memory:([0-9]{1,18})")


def subject(action: str, target: str) -> tuple[str | None, int | None]:
    """The project an action happened in, as a project name or the id of the memory it changed; (None, None) for
    an action outside any project."""
    family = action.partition(".")[0]
    if family == "memory":
        found = _MEMORY.fullmatch(target)
        return None, int(found[1]) if found else None
    if family == "skill":
        found = _PROJECT_SKILL.match(target)
        return (found[1] if found else None), None
    if family in NAMED_FAMILIES:
        found = _FIRST_NAME.match(target)
        return (found[1] if found else None), None
    return None, None


def _insert(project_id) -> Insert:
    """An audit row of :audit_actor, :audit_token, :audit_action and :audit_target in the project ``project_id``
    gives. The parameters name no column of audit: an INSERT would take a parameter named after a column as the
    value of that column."""
    return insert(tables.audit).values(
        actor_id=bindparam("audit_actor", type_=BigInteger),
        token_id=bindparam("audit_token", type_=BigInteger),
        action=bindparam("audit_action", type_=String),
        target=bindparam("audit_target", type_=String),
        project_id=project_id,
    )


# Built once (docs/hub.md, Data access): the project given by id, by name, as the memory's, or none.
_IN_PROJECT = _insert(bindparam("audit_project_id", type_=BigInteger))
_IN_NAMED = _insert(
    select(tables.projects.c.id).where(tables.projects.c.name == bindparam("audit_project")).scalar_subquery()
)
_OF_MEMORY = _insert(
    select(tables.memories.c.project_id).where(tables.memories.c.id == bindparam("audit_memory")).scalar_subquery()
)


async def record(
    conn: AsyncConnection,
    *,
    actor_id: int | None,
    token_id: int | None,
    action: str,
    target: str,
    project_id: int | None = None,
) -> None:
    row = {"audit_actor": actor_id, "audit_token": token_id, "audit_action": action, "audit_target": target}
    statement = _IN_PROJECT
    if project_id is None:
        project, memory_id = subject(action, target)
        if project is not None:
            statement, row["audit_project"] = _IN_NAMED, project
        elif memory_id is not None:
            statement, row["audit_memory"] = _OF_MEMORY, memory_id
    row["audit_project_id"] = project_id
    await conn.execute(statement, row)


def token_target(token_id: int) -> str:
    return f"token:{token_id}"
