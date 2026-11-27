"""Selling plugins from this hub (plan plugin-market, criterion a7): the seller's key (``service.keys``), its listings
(``service.listings``), orders (``service.orders``), revenue and payouts (``service.revenue``), signals
(``service.signals``), and the public API buying hubs call (``service.api``).

The package has four layers, which lint-imports keeps in this order (contract hub-seller-layers in pyproject.toml):
``routes`` opens a transaction at most and passes the request's parts to ``service``, which checks and decides, and
reads and writes through ``repository``; ``models`` holds the request and response models and the constants. Other
modules import ``service`` and ``models``, never ``routes``.
"""
