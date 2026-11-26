"""Notifications: the outbox every decision and notice of a plan run goes through, the channels that deliver them, the
notices of the worker, and the routes a member reads them with. ``docs/notifications.md`` is the design and ``runs``
holds the kinds and limits.

The package has four layers, each importing only those below it: ``routes`` (the member's list, count and read, the
worker's notices, a hub admin's link of a member's channel), ``service`` (the Notifier every module notifies with,
``service.notifier.NOTIFIER``; the channels, a Strategy of Channel classes; the job hub.deliver_notifications; the
notices of a run's end; the members' channels of core plugins), ``repository`` (the SQL of the outbox, the list and
the channels) and ``models`` (the limits, the reasons, Outgoing, the exceptions a channel raises and the API's
shapes)."""
