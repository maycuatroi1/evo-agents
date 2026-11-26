"""The repository layer of notifications: every SQL statement on notifications, notification_deliveries,
notification_channels and channel_link_codes, with SQLAlchemy Core on ``evo_agents.hub.tables``. ``outbox`` stores a
notification with its deliveries and moves a delivery through the job; ``listing`` reads a member's notifications;
``channels`` keeps the members' channels and their link codes and reads what a channel may send; ``plans`` reads the
plan a run's end reports on. It checks no role and decides nothing: that is the service's."""
