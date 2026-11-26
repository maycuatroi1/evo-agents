"""The outbound webhooks of a project: the routes its admin keeps them with, and the job hub.deliver_webhooks that
sends their outbox (``evo_agents.hub.webhook_events`` has the events, the signature and the backoff; ``docs/hub.md``,
section "Outbound webhooks", is the design).

The package has four layers, each importing only those below it: ``routes`` (list, add, change, rotate the secret
of, delete a webhook, and list its deliveries), ``service`` (the admin's check, the sealed secret and the writes,
``hooks``; the job, ``delivery``), ``repository`` (the SQL of project_webhooks and webhook_deliveries) and ``models``
(the limits and the API's shapes)."""
