"""The evo-agents hub: one server that keeps memories, plans, skills and knowledge graphs for a team.

The server (``hub serve``, ``hub migrate``) needs the hub-server extra and Postgres. ``access``, ``cli``,
``cli_client``, ``cli_memory``, ``client``, ``config``, ``log``, ``memory``, ``registration`` and ``registry`` need
nothing beyond the core package, so ``evo-agents`` and the client commands (``hub login``, ``whoami``, ``token``,
``admin``, ``project``, ``registry``, ``memory``) work on a core install; everything else in this package imports the
extra at module level and is loaded only by the commands that need it.
"""
