"""The evo-agents hub: one server that keeps memories, plans, skills and knowledge graphs for a team.

The server (``hub serve``, ``hub migrate``) needs the hub-server extra and Postgres. ``cli``, ``cli_client``,
``client``, ``config`` and ``log`` use the standard library only, so ``evo-agents`` and the client commands
(``hub login``, ``whoami``, ``token``, ``admin``) work on a core install; everything else in this package
imports the extra at module level and is loaded only by the commands that need it.
"""
