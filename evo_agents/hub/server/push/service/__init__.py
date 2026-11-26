"""The service of push notifications, the layer under the routes: what the routes do with a member's devices
(``devices``), the tiers and quiet hours of members (``tiers``), and the senders the outbox hands a delivery to a
device to: Web Push (``senders``), APNs and FCM (``native``)."""
