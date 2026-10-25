"""The tables the hub's chain of migrations made and a core plugin now keeps: ``0086`` made pm_sends, ``0124`` to
``0129`` the tables of crm and marketing. The revisions stay in the hub's chain, so every hub, old or new, has these
tables before the plugin's own chain adopts them (``evo_agents.hub.plugin_schema.adopt_tables``); the hub's
autogenerate (``env.py``) and ``tests/hub/test_schema_metadata.py`` leave them out, as they leave out the tables of the
installed core plugins. A copy of names, never an import of the plugins' tables."""

PLUGIN_OWNED_TABLES = frozenset(
    {
        "pm_sends",
        "crm_contacts",
        "crm_touchpoints",
        "crm_stage_history",
        "crm_cursors",
        "crm_deletions",
        "crm_invites_sent",
        "marketing_budget",
        "marketing_channels",
        "marketing_denylist",
        "marketing_metrics",
        "marketing_posts",
        "marketing_replies",
        "marketing_state",
    }
)
