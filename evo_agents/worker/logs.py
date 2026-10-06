"""The daemon's log: one JSON object per line in ``~/.evo/worker/worker.log`` (mode 0600, rotated at 10 MiB, five
old files kept), and on stderr while it runs in the foreground.

Lines go through the hub's ``JsonFormatter``, which removes worker tokens (``evw_...``), machine tokens, bearer
credentials, pairing codes and the signatures of presigned URLs; the token the daemon holds is registered as a secret
too, so it is masked wherever it shows up, and so is the value of every lease a run gets while a run holds it
(``mask``, which ``credentials`` calls as each lease arrives, after ``configure``, and ``unmask`` once every run that
held it gave it back). The daemon never logs the text of an agent's events
or of a message from the owner: those go to the hub only. Standard library only.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

from evo_agents.hub.log import QUIET_LOGGERS, JsonFormatter, register_secret, unregister_secret

FILE_MODE = 0o600
MAX_BYTES = 10 * 1024 * 1024
BACKUPS = 5
QUIET = {**QUIET_LOGGERS, "aiohttp.access": logging.WARNING, "asyncio": logging.WARNING}


class _WorkerFile(logging.handlers.RotatingFileHandler):
    """worker.log, created 0600 whatever the umask, as is each file rotation starts."""

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
        os.fchmod(fd, FILE_MODE)
        return os.fdopen(fd, "a", encoding=self.encoding or "utf-8", errors=self.errors)


class _WorkerStream(logging.StreamHandler):
    """Marks the stderr handler this module installed."""


def mask(*values: str) -> None:
    """Mask ``values`` in every log line from now on, whichever handler writes it: the leases of a run, which come
    after ``configure``."""
    for value in values:
        register_secret(value)


def unmask(*values: str) -> None:
    """Undo one ``mask`` of each of ``values``: the leases a run gave back, once no other run holds them."""
    for value in values:
        unregister_secret(value)


def configure(log_path: Path, *, level: str = "INFO", stderr: bool = True, secrets=()) -> None:
    """Send the process's log records to ``log_path`` (and stderr) as JSON lines, with ``secrets`` masked, and every
    value given to ``mask`` later."""
    mask(*secrets)
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, _WorkerFile | _WorkerStream):
            root.removeHandler(handler)
            handler.close()
    formatter = JsonFormatter()
    file_handler = _WorkerFile(log_path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    if stderr:
        stream = _WorkerStream(sys.stderr)
        stream.setFormatter(formatter)
        root.addHandler(stream)
    root.setLevel(level)
    for name, quiet in QUIET.items():
        logging.getLogger(name).setLevel(quiet)
    logging.captureWarnings(True)
