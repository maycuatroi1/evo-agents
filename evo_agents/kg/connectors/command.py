"""Builtin ``exec`` connector: a command of the source (``command:``) that writes its kg/1 messages to stdout, one JSON
object a line. It gets the project, the source and the cursor in KG_PROJECT, KG_SOURCE and KG_STATE beside the
credentials the source declares, runs in the harness root and is killed after the source's ``timeout``."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Iterator

from evo_agents.kg.connectors.base import Connector
from evo_agents.plugins.connectors import ConnectorError

DEFAULT_TIMEOUT = 1800


class CommandConnector(Connector):
    """Adapter for an external command. A command that cannot start raises ConnectorError to the caller, as an unknown
    connector does; how it ended is in ``returncode`` and ``stderr_tail``."""

    def __iter__(self) -> Iterator[dict]:
        return self.messages()

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self.ctx.env)
        env["KG_PROJECT"] = self.ctx.project
        env["KG_SOURCE"] = json.dumps(self.ctx.source, ensure_ascii=False)
        if self.ctx.cursor is not None:
            env["KG_STATE"] = json.dumps(self.ctx.cursor, ensure_ascii=False)
        return env

    def messages(self) -> Iterator[dict]:
        command = self.ctx.source.get("command") or []
        if not command:
            raise ConnectorError(f"source {self.ctx.source_id!r} has no command")
        cwd = str(self.ctx.harness_root) if self.ctx.harness_root else None
        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=self._env(),
                cwd=cwd,
            )
        except OSError as exc:
            raise ConnectorError(f"cannot start {command[0]!r}: {exc}") from exc
        stdout, stderr = proc.stdout, proc.stderr
        assert stdout is not None and stderr is not None  # both piped

        def drain() -> None:
            for line in stderr:
                self.stderr_tail.append(line.rstrip("\n"))

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        timeout = float(self.ctx.source.get("timeout") or DEFAULT_TIMEOUT)
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        drained = False
        try:
            for line in stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    yield {"type": "_invalid", "raw": line[:200]}
            drained = True
        finally:
            timer.cancel()
            if not drained and proc.poll() is None:
                # The caller stopped reading or asked for a kill: do not leave the connector running.
                proc.kill()
            self.returncode = proc.wait()
            reader.join(timeout=5)
