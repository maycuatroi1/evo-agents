"""Browser identities: their profiles, the leases of a run's, their logins and the live view of a run's browser.

The package has four layers, routes over service over repository over models (the contract hub-browser-layers in
pyproject.toml): a route opens the transaction and calls the service, which decides and refuses with the domain errors
(``base.errors``); the repository holds every query, and models the shapes, the limits and the route class that
bounds an uploaded archive.
"""
