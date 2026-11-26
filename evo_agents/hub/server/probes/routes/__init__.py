"""The routes layer of the Curator's probes: the HTTP routes under /v1/projects and the probe routes of the worker
protocol under /v1/worker. Each opens the transaction, passes the request's parts to the service and logs what
happened."""
