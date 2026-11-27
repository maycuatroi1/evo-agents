"""Hub administration and the hub-wide settings around it: users and grants, the audit trail and every user's tokens,
the features, the branding and a project's DLP policy. The routes stay in the flat modules ``admin``,
``admin_console``, ``features``, ``branding`` and ``dlp_policy``; each handler passes the request's parts and the
app's engine to ``service``, which opens the transaction, checks and decides; ``repository`` holds the SQL and
``models`` the API's shapes and limits (contract hub-hub_admin-layers in pyproject.toml)."""
