"""The registry of the hub's routers, listed by hand in the order ``app.create_app`` includes them.

The order is part of the contract: Starlette matches the first route whose path fits, so a router placed before
another wins the paths both could take (each such router says which one it beats), and the OpenAPI document lists the
paths in this order (``tests/hub/test_contract.py`` holds it). Each group imports its modules when it is read, as
``create_app`` did, so the server's modules import in the same order. The routes of plugins, which ``create_app``
includes between ``curator`` and ``websockets``, are not here: ``plugins.routes.extension`` includes them itself.
"""

from __future__ import annotations

from fastapi import APIRouter

from evo_agents.hub.server import (
    admin,
    app_sessions,
    auth,
    blobs,
    health,
    invites,
    projects,
    tokens,
    user_emails,
    web_auth,
)
from evo_agents.hub.server.loop import routes as loop_routes  # GET .../loop, POST .../loop/resume
from evo_agents.hub.server.memories.routes import restore_router as restore_routes
from evo_agents.hub.server.notifications import routes as notifications
from evo_agents.hub.server.systems.routes import traces as system_trace_views

Routers = tuple[APIRouter, ...]


def accounts() -> Routers:
    from evo_agents.hub.server import skills
    from evo_agents.hub.server.memories import routes as memory_routes
    from evo_agents.hub.server.plans import routes as plans

    return (
        health.router,
        auth.router,
        web_auth.router,
        app_sessions.router,  # POST /v1/auth/app-session
        app_sessions.admin_router,  # /v1/admin/auth-apps
        tokens.router,
        user_emails.router,
        admin.router,
        notifications.admin_router,  # /v1/admin/users/{login}/channels/{kind}
        invites.router,  # /v1/admin/invites
        invites.accept_router,  # POST /v1/invites/accept
        projects.router,
        blobs.router,
        restore_routes,  # POST /v1/memories/{id}/restore
        plans.router,
        memory_routes.router,
        skills.router,
        skills.worker_router,  # the skill sync of worker pool nodes
    )


def kg() -> Routers:
    from evo_agents.hub.server import (
        admin_console,
        kg_answers,
        kg_branches,
        kg_config,
        kg_entities,
        kg_entities_setup,
        kg_entity_map,
        kg_entity_map_run,
        kg_eval_sets,
        kg_graphs,
        kg_grep,
        kg_init,
        kg_map,
        kg_optimize,
        kg_pipeline,
        kg_questions,
        kg_review,
        kg_settings,
        kg_upload_relay,
        kg_uploads,
        kg_web,
        run_cause,
    )
    from evo_agents.hub.server import kg as kg_routes
    from evo_agents.hub.server.kg_decisions import routes as kg_decisions

    return (
        kg_routes.router,
        kg_config.router,
        kg_grep.router,
        kg_web.router,
        kg_map.router,
        kg_review.router,
        kg_settings.router,
        kg_graphs.router,
        kg_branches.router,
        kg_entities_setup.router,
        kg_optimize.router,
        kg_init.router,
        kg_init.admin_router,
        kg_questions.router,
        kg_uploads.router,
        kg_pipeline.router,
        kg_answers.router,
        kg_upload_relay.router,
        kg_eval_sets.router,
        kg_decisions.router,
        kg_entities.router,
        kg_entity_map.router,
        kg_entity_map_run.router,
        run_cause.router,
        admin_console.router,
    )


def runs() -> Routers:
    from evo_agents.hub.server import run_events, tool_stats
    from evo_agents.hub.server import runs as run_routes
    from evo_agents.hub.server.chats.routes import author as author_chat
    from evo_agents.hub.server.workers.routes import join as worker_join
    from evo_agents.hub.server.workers.routes import pools as worker_pools
    from evo_agents.hub.server.workers.routes import workers

    return (
        workers.router,
        worker_pools.router,
        worker_join.worker_router,
        run_routes.router,
        run_routes.worker_router,
        run_events.router,
        run_events.stream_router,
        run_events.worker_router,
        author_chat.router,
        author_chat.worker_router,
        tool_stats.router,
    )


def run_reads() -> Routers:
    from evo_agents.hub.server import (
        dlp_policy,
        kg_worker,
        local_runs,
        overview,
        today,
    )
    from evo_agents.hub.server.attachments import routes as attachments
    from evo_agents.hub.server.curator.routes import charter as curator_routes
    from evo_agents.hub.server.decisions import routes as decisions
    from evo_agents.hub.server.doc_versions import routes as doc_versions
    from evo_agents.hub.server.member_tasks import routes as member_tasks
    from evo_agents.hub.server.outputs import routes as outputs
    from evo_agents.hub.server.proposals.routes import reads as proposal_reads
    from evo_agents.hub.server.proposals.routes import record as proposals
    from evo_agents.hub.server.push import routes as push
    from evo_agents.hub.server.releases.routes import claims as release_claims
    from evo_agents.hub.server.releases.routes import releases
    from evo_agents.hub.server.webhooks import routes as webhooks

    return (
        kg_worker.worker_router,  # the graph pushes of the worker on duty for a night shift
        decisions.router,  # reading and answering decisions
        decisions.worker_router,
        local_runs.router,
        local_runs.batch_router,
        notifications.router,  # a member reads their notifications
        notifications.worker_router,
        outputs.notice_router,  # the notices a member turned off
        push.router,  # a member's push devices (evo_agents.hub.server.push)
        push.config_router,  # what a client needs to subscribe, without a sign-in
        overview.router,
        today.router,  # what waits for a member and what changed since they last looked
        curator_routes.router,
        proposals.router,
        proposals.worker_router,
        proposal_reads.router,  # reading findings and proposals, and answering one
        releases.router,  # the daily release of a project (evo_agents.hub.releases)
        release_claims.worker_router,  # the branch a release run pushed (evo_agents.hub.release_runs)
        outputs.router,  # the outputs of runs (evo_agents.hub.outputs) and a project's switch of tool images
        outputs.worker_router,
        outputs.content_router,  # their bytes
        outputs.chat_router,
        attachments.router,  # the files people attach to messages (evo_agents.hub.attachments)
        attachments.chat_router,
        attachments.worker_router,
        doc_versions.chat_router,  # the versions of a document in a chat or a run
        doc_versions.router,
        member_tasks.router,  # tasks a member hands an agent with no Git (task runs with no agent type)
        webhooks.router,  # a project's outbound webhooks (evo_agents.hub.webhook_events)
        dlp_policy.router,  # a project's data loss prevention policy (evo_agents.hub.dlp)
    )


def curator() -> Routers:
    from evo_agents.hub.server import (
        build_now,
        earlier,
        kg_health,
        ledger,
        org,
        transcripts,
    )
    from evo_agents.hub.server.autonomy.routes import levels as autonomy_routes
    from evo_agents.hub.server.autonomy.routes import page as autonomy_page
    from evo_agents.hub.server.autonomy.routes import stops as autonomy_stops
    from evo_agents.hub.server.changes import routes as change_routes
    from evo_agents.hub.server.digests import routes as digests
    from evo_agents.hub.server.evolve import routes as evolve
    from evo_agents.hub.server.forge.routes import protection
    from evo_agents.hub.server.history import routes as history_routes
    from evo_agents.hub.server.merge.routes import merged_work
    from evo_agents.hub.server.playbook import routes as playbook_routes
    from evo_agents.hub.server.probes.routes import probes as probe_routes
    from evo_agents.hub.server.probes.routes import report as probe_report
    from evo_agents.hub.server.probes.routes import runs as probe_runs
    from evo_agents.hub.server.proposal_plans import routes as proposal_plans
    from evo_agents.hub.server.replays import routes as replay_routes
    from evo_agents.hub.server.workers.routes import doctor as worker_doctor
    from evo_agents.hub.server.workers.routes import shares as worker_shares

    return (
        change_routes.router,
        build_now.router,  # Run plan on a Curator's plan: its Builder now
        protection.router,
        proposal_plans.router,
        change_routes.worker_router,
        ledger.router,
        digests.router,
        transcripts.router,
        kg_health.router,
        replay_routes.router,
        replay_routes.worker_router,
        history_routes.router,
        history_routes.worker_router,
        history_routes.search_router,
        earlier.router,
        playbook_routes.router,
        playbook_routes.worker_router,
        probe_routes.router,  # the Curator's probes (plan correction-recurrence)
        probe_routes.worker_router,
        probe_runs.worker_router,  # the probe runs' sessions (plan correction-recurrence)
        probe_report.router,  # and their weekly report
        evolve.router,  # the evolutions of a harness or an agent (plan pareto-curator-evolution)
        evolve.worker_router,  # and what their runs read and post
        org.router,  # the org chart (plan org-chart)
        autonomy_routes.router,  # the autonomy levels (plan autonomy-levels)
        autonomy_stops.router,  # and their stop S4
        autonomy_page.router,  # what the web's autonomy pages read
        worker_shares.router,  # shared workers narrowed to units (plan credential-sharing)
        worker_doctor.worker_router,  # the doctor reports of workers, which narrowing to units needs
        merged_work.worker_router,  # the ended runs whose work merged, whose worktrees a worker removes at once
    )


def websockets() -> Routers:
    """The two ends of a run's web terminal and of the live view of its browser."""
    from evo_agents.hub.server import terminal
    from evo_agents.hub.server.browser.routes import view as browser_view

    return (terminal.router, terminal.worker_router, browser_view.router, browser_view.worker_router)


def members() -> Routers:
    from evo_agents.hub.server import (
        claude_sign_in,
        rate_limits,
        run_leases,
        run_waits,
        task_dispatch,
    )
    from evo_agents.hub.server.accounts.routes import accounts as account_routes
    from evo_agents.hub.server.accounts.routes import pools as account_pools
    from evo_agents.hub.server.accounts.routes import views as account_views
    from evo_agents.hub.server.agent_accounts.routes import accounts as agent_accounts
    from evo_agents.hub.server.agent_accounts.routes import provider_accounts as agent_provider_accounts
    from evo_agents.hub.server.agent_accounts.routes import views as agent_account_views
    from evo_agents.hub.server.agents.routes import agent_types
    from evo_agents.hub.server.agents.routes import asks as agent_asks
    from evo_agents.hub.server.agents.routes import packs as agent_packs
    from evo_agents.hub.server.agents.routes import profiles as agent_profiles
    from evo_agents.hub.server.agents.routes import reads as agent_reads
    from evo_agents.hub.server.autonomy.routes import worker as worker_autonomy
    from evo_agents.hub.server.browser.routes import asks as browser_asks
    from evo_agents.hub.server.browser.routes import identities as browser_identities
    from evo_agents.hub.server.browser.routes import leases as browser_leases
    from evo_agents.hub.server.browser.routes import local as browser_local
    from evo_agents.hub.server.browser.routes import logins as browser_logins
    from evo_agents.hub.server.chats.routes import agents as chat_agents
    from evo_agents.hub.server.chats.routes import bridges as chat_bridges
    from evo_agents.hub.server.chats.routes import chats
    from evo_agents.hub.server.chats.routes import direct as chat_direct
    from evo_agents.hub.server.chats.routes import events as chat_events
    from evo_agents.hub.server.chats.routes import home as chat_home
    from evo_agents.hub.server.chats.routes import images as chat_images
    from evo_agents.hub.server.chats.routes import labels as chat_labels
    from evo_agents.hub.server.chats.routes import members as chat_members
    from evo_agents.hub.server.chats.routes import people as chat_people
    from evo_agents.hub.server.chats.routes import read as chat_read
    from evo_agents.hub.server.credentials.routes import leases as credential_leases
    from evo_agents.hub.server.credentials.routes import secrets as secret_routes
    from evo_agents.hub.server.credentials.routes import shares as credential_shares
    from evo_agents.hub.server.cron import routes as cron_jobs
    from evo_agents.hub.server.curator.routes import threads as curator_thread_routes
    from evo_agents.hub.server.ops import routes as ops_routes
    from evo_agents.hub.server.orchestrator import routes as orchestrator_routes
    from evo_agents.hub.server.plan_run_choice import routes as plan_run_choice
    from evo_agents.hub.server.plan_systems import routes as plan_systems
    from evo_agents.hub.server.plugin_api.routes import chats as plugin_chats_api
    from evo_agents.hub.server.plugin_api.routes import docs as plugin_docs_api
    from evo_agents.hub.server.plugin_api.routes import records as plugin_records_api
    from evo_agents.hub.server.plugin_api.routes import runs as plugin_runs_api
    from evo_agents.hub.server.plugin_api.routes import srs as plugin_srs_api
    from evo_agents.hub.server.plugin_api.routes import tasks as plugin_tasks_api
    from evo_agents.hub.server.plugins.routes import notify as plugin_notify
    from evo_agents.hub.server.systems.routes import ops as system_ops
    from evo_agents.hub.server.systems.routes import systems as agent_systems
    from evo_agents.hub.server.triage import routes as triage_routes
    from evo_agents.hub.server.work_cards import routes as work_cards

    return (
        secret_routes.router,
        credential_shares.router,  # the shares of secrets and accounts
        run_leases.router,
        credential_leases.worker_router,
        claude_sign_in.router,  # before accounts.router: /v1/accounts/claude-sign-in is no account's name
        account_routes.router,
        account_views.router,
        rate_limits.worker_router,
        worker_autonomy.worker_router,
        agent_profiles.router,
        system_trace_views.stream_router,  # before its router, whose /traces/{id} would take /traces/stream
        system_trace_views.router,  # the traces of agent systems (0218)
        agent_systems.router,  # the agent systems of a project (0217)
        system_ops.router,  # the default system of a project and the ops of a system (0240)
        cron_jobs.router,  # the cron jobs of a project (0243)
        agent_accounts.router,
        account_pools.router,
        agent_provider_accounts.router,
        agent_account_views.router,
        agent_packs.router,
        agent_types.router,
        browser_identities.router,
        browser_logins.router,
        browser_asks.worker_router,
        run_waits.worker_router,
        browser_leases.worker_router,
        browser_leases.upload_router,
        browser_local.router,
        browser_local.upload_router,
        task_dispatch.router,
        plugin_notify.router,
        plugin_records_api.backend,
        plugin_records_api.members,
        plugin_tasks_api.router,  # tasks:write, under .../board/ apart from task_dispatch's .../tasks/{task}
        plugin_docs_api.router,  # docs:write
        plugin_runs_api.router,  # the summary and outputs of the plugin's task runs, with runs:dispatch
        plugin_srs_api.router,  # srs:read
        plugin_chats_api.router,  # chats:write and chats:read, in the chats of the project's agents
        task_dispatch.worker_router,
        chat_people.router,  # before chats.routes.chats.router, whose GET /{chat_id} would take /people
        chats.router,
        chat_direct.router,
        plan_systems.router,  # a plan handed to a system's entry agent, in the caller's direct chat with it
        plan_run_choice.router,  # a member's last choice in the Run plan dialog of a project
        chat_home.router,  # the agents a member may chat with, for the chat home of the web
        chat_members.router,
        chat_images.router,
        chat_labels.router,
        chat_bridges.router,
        work_cards.router,  # the work card of a work chat
        chat_events.router,
        chat_events.stream_router,
        chat_agents.worker_router,
        agent_asks.worker_router,  # a chat run's agent asks an agent of another project
        agent_reads.worker_router,  # and reads another project's knowledge graph and memories
        chat_read.worker_router,
        triage_routes.worker_router,
        orchestrator_routes.worker_router,  # the Orchestrator of a system acts in its threads; agents escalate
        ops_routes.worker_router,  # and the Orchestrator of a default system in its ops threads
        work_cards.worker_router,  # the entry agent of a system sets the work of its trace
        curator_thread_routes.worker_router,  # the agent of a proposal's thread answers it
    )


def store() -> Routers:
    """The plugin store, the markets and sellers, a project's admin and plugins, the hub's branding and features, the
    model catalog and gateway, signals, goals, problems, opportunities, intake and merges."""
    from evo_agents.hub.server import (
        branding,
        context_answer_reuse,
        features,
        kg_shared_pool_api,
        model_catalog,
        project_admin,
        project_archive,
        runnable_models,
    )
    from evo_agents.hub.server.accounts.routes import provider_accounts
    from evo_agents.hub.server.autonomy.routes import policies as autonomy_policies
    from evo_agents.hub.server.gateway.routes import proxy as gateway_proxy
    from evo_agents.hub.server.goals import routes as goals
    from evo_agents.hub.server.intake import routes as intake
    from evo_agents.hub.server.market.routes import install as market_install
    from evo_agents.hub.server.market.routes import licenses as market_licenses
    from evo_agents.hub.server.market.routes import market
    from evo_agents.hub.server.merge.routes import check as merge_check
    from evo_agents.hub.server.merge.routes import judge as merge_judge
    from evo_agents.hub.server.merge.routes import view as merge_view
    from evo_agents.hub.server.opportunities import routes as opportunities
    from evo_agents.hub.server.opportunity_evidence import routes as opportunity_evidence
    from evo_agents.hub.server.plugins.routes import commands as plugin_command_list
    from evo_agents.hub.server.plugins.routes import host as plugin_host
    from evo_agents.hub.server.plugins.routes import projects as project_plugins
    from evo_agents.hub.server.plugins.routes import publishers as plugin_publishers
    from evo_agents.hub.server.plugins.routes import store as plugins
    from evo_agents.hub.server.plugins.routes import ui as plugin_ui
    from evo_agents.hub.server.problem_evidence import routes as problem_evidence
    from evo_agents.hub.server.problems import routes as problems
    from evo_agents.hub.server.seller.routes import api as seller_api
    from evo_agents.hub.server.seller.routes import seller
    from evo_agents.hub.server.signals import routes as signals

    return (
        market_install.github_router,  # before plugins.router, whose routes take /v1/plugins/{name}
        plugins.router,
        plugin_publishers.router,
        market.sources_router,  # before market.router, whose /{source}/{plugin} would take /sources/{id}
        market_licenses.router,  # before market.router, whose /{source} would take /hub and /licenses
        market_install.router,
        market.router,
        seller.router,
        seller_api.router,
        project_admin.admin_router,  # /v1/admin/projects/{p}: self-managed state
        project_admin.router,  # a self-managed project's admins: grants, invites, plugins
        project_archive.router,  # /v1/projects/{p}/archive and /unarchive
        project_plugins.router,
        plugin_ui.router,
        plugin_command_list.router,
        plugin_host.router,
        branding.router,
        branding.admin_router,
        features.router,
        features.admin_router,
        kg_shared_pool_api.router,  # /v1/admin/kg-pool: the shared KG pool
        model_catalog.router,
        provider_accounts.router,
        runnable_models.router,
        gateway_proxy.router,  # /v1/messages and the like: a run's token to the model gateway
        signals.push_router,  # a project's signals: an ingest key's batches, and the members' routes
        signals.router,
        context_answer_reuse.router,  # GET .../context-answers: the answers loaded by week
        signals.keys_router,
        autonomy_policies.router,
        goals.router,
        problems.router,
        problem_evidence.router,
        opportunities.router,
        opportunity_evidence.router,
        intake.router,
        loop_routes.router,
        merge_view.router,
        merge_judge.worker_router,
        merge_check.worker_router,
    )


# The groups in the order create_app includes them; the routes of plugins go between EARLY and LATE.
EARLY = (accounts, kg, runs, run_reads, curator)
LATE = (websockets, members, store)
