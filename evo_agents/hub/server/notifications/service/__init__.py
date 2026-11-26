"""The service of notifications, the layer under the routes: the Notifier every module tells a member something with
(``notifier``), the channels a notification goes out on (``channels``), the job that sends the outbox
(``deliveries``), a worker's notices (``notices``) and the notices of a run's end (``run_notices``), what a member
reads of their notifications (``reads``), and the members' channels of core plugins (``member_channels``), which a hub
admin may link (``admin_channels``)."""
