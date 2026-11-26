"""What the summary push of Today (``digests.service.today``) sends and when: who is away, and the notice."""

from __future__ import annotations

from datetime import timedelta

AWAY = timedelta(hours=8)  # a member seen on the hub more recently gets no summary push
NOTICE = "today"  # runs.NOTICE_KINDS: the summary push
TITLE = "Today on the hub"
TODAY = "/today"  # the web's Today, which was Home until the chat home took "/"
