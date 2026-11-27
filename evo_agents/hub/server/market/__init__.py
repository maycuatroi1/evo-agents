"""The plugin market on the hub (plan plugin-market): the markets a hub admin adds and what every member reads of them
(``service.market``), installing a free plugin from a market or a GitHub release and a project admin's request for one
(``service.install``), and buying a paid plugin from its seller (``service.licenses``).

The package has four layers, which lint-imports keeps in this order (contract hub-market-layers in pyproject.toml):
``routes`` opens a transaction at most and passes the request's parts to ``service``, which checks and decides, and
reads and writes through ``repository``; ``models`` holds the request and response models and the constants. Other
modules import ``service`` and ``models``, never ``routes``.
"""
