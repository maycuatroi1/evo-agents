"""Push notifications: the push devices of members, the senders that push a notification to them through the outbox
of ``evo_agents.hub.server.notifications``, the tiers and quiet hours that decide whether and when a push goes, and the
routes a member adds, lists, removes and tries their devices with. ``docs/notifications.md`` is the design.

The package has five layers, each importing only those below it: ``routes`` (a member's devices, a test push, what a
client needs to subscribe), ``service`` (what the routes do, ``devices``; the tiers and quiet hours, ``tiers``; the
senders of Web Push, ``senders``, and of APNs and FCM, ``native``), ``repository`` (the SQL of the devices and the
preferences), ``models`` (the limits, the tiers' facts and the API's shapes) and ``webpush`` (the crypto of Web Push,
RFC 8291 and RFC 8292, which the models check a subscription with)."""
