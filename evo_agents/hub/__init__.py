"""The evo-agents hub: one server that keeps memories, plans, skills and knowledge graphs for a team.

The server side needs the hub-server extra and imports it at module level: the FastAPI app in ``server``, and
``blobs`` (the S3 blob store), ``db``, ``tables`` (the SQLAlchemy metadata), ``jobs``, ``migrate``, ``worker``
(``hub worker``), ``kg_build``, ``kg_prune``, ``blob_gc``, ``kg_graph`` and ``kg_web``. These are loaded only by the
commands that run the server (``hub serve``, ``hub worker``, ``hub migrate``, ``hub openapi``, ``hub contract
print``). ``kg_graph`` and ``kg_web`` (the graph cache and the reads from it) need only ``blobs`` of that extra: the
Postgres query that lists the built graphs is ``built_graphs`` in ``server.kg``.

Every other module needs only the core package, so ``evo-agents`` and the client commands work on a core install:
``cli`` and ``cli_client`` (the ``hub`` commands and sign-in), ``client`` (HTTP to the hub), ``config`` and ``log``,
``access``, ``registration`` and ``registry`` (projects), ``plans``, ``plan_cli``, ``plan_diff`` and ``mirror``
(plans and their copies in a harness), ``memory`` and ``cli_memory``, ``skills``, ``skill_sync`` and ``cli_skills``,
``kg_cli``, ``kg_push`` and ``kg_ingest`` (knowledge graphs), ``mcp_proxy`` and ``mcp_tools`` (``hub mcp``),
``hooks`` (the evo-hub plugin's hooks), ``openapi`` and ``contract`` (the API document and the command line
contract, ``hub contract check`` included).

The ``server`` package is the API, one module per area: ``app`` (the application and its lifespan), ``security``
(machine tokens, web sessions, CSRF), ``auth`` and ``web_auth`` (sign-in from the CLI and from the browser),
``github``, ``tokens``, ``admin`` (grants, users, row counts) and ``admin_console`` (the web's admin area: the audit
trail and every user's tokens), ``audit``, ``projects``, ``memories``, ``plans``, ``skills``, ``blobs``, ``kg`` and
``kg_web`` (graph pushes, builds and tools, and the web's graph pages), ``mcp`` (/mcp), ``health`` and ``errors``.
``migrations`` holds the Alembic revisions ``migrate`` applies. docs/hub.md describes the hub as a whole.
"""
