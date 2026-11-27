"""The limits of the live view of a run's browser (``service.view``): how long a hello, an idle view and a view may
take, the decision that opens a run in human mode, and the errors of an unavailable database. The layers read them
from this module when they use them, so a test may change one."""

from __future__ import annotations

import psycopg
from sqlalchemy import exc as sa_exc

HELLO_SECONDS = 10.0  # the viewer's first message, its CSRF value, comes within this
IDLE_SECONDS = 15 * 60  # a view without a frame either way for this long is closed
MAX_SECONDS = 4 * 60 * 60  # a view is closed this long after it opened, busy or not
LOGIN_CATEGORY = "login"  # a run waiting on an open decision of this category opens in human mode
DATABASE_ERRORS = (psycopg.OperationalError, sa_exc.OperationalError, OSError)  # PoolTimeout is an OperationalError
