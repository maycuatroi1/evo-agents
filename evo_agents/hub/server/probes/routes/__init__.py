"""The routes layer of the Curator's probes: the HTTP routes under /v1/projects and the probe routes of the worker
protocol under /v1/worker. Each passes the request's parts and the app's engine to the service, which opens the
transaction (``UnitOfWork``), decides and logs what happened."""
