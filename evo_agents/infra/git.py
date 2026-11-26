"""Git run in one directory, with typed errors, in a blocking and an asyncio flavour (docs/structure.md, section
Shared infrastructure).

``Git(directory)`` runs ``git -C directory ARGS``; ``Git(None)`` runs git where the process is. Each call returns a
``GitResult`` (exit code, stdout, stderr), or raises: ``GitTimeout`` when git gave no answer within the timeout,
``GitUnavailable`` when it could not start, ``GitFailed`` when it exited other than 0 and the call checks. Callers
word their own messages from the error's fields, so each keeps saying what it said before.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Generic, Literal, NamedTuple, TypeVar, overload

T = TypeVar("T", str, bytes)
NO_PROMPT = {"GIT_TERMINAL_PROMPT": "0"}  # git asks no one for a credential: it fails instead


class GitError(Exception):
    """Git did not do what it was asked: ``args`` are its words after ``git -C directory``."""

    def __init__(self, message: str, args: tuple[str, ...] = (), directory: Path | None = None):
        super().__init__(message)
        self.words = args
        self.directory = directory

    @property
    def command(self) -> str:
        """The subcommand, as messages name it (``fetch``)."""
        return self.words[0] if self.words else ""


class GitTimeout(GitError):
    """Git gave no answer within ``timeout`` seconds, and was killed."""

    def __init__(self, args: tuple[str, ...], directory: Path | None, timeout: float, detail: str = ""):
        super().__init__(detail or f"git {args[0] if args else ''} gave no answer within {timeout:g}s", args, directory)
        self.timeout = timeout


class GitUnavailable(GitError):
    """Git could not start: it is not installed, or the directory it runs in is not there (``cause``)."""

    def __init__(self, args: tuple[str, ...], directory: Path | None, cause: OSError):
        super().__init__(str(cause), args, directory)
        self.cause = cause


class GitFailed(GitError):
    """Git exited ``returncode``, other than 0; ``stderr`` and ``stdout`` are what it printed."""

    def __init__(self, result: GitResult, args: tuple[str, ...], directory: Path | None):
        said = result.stderr if isinstance(result.stderr, str) else result.stderr.decode("utf-8", "replace")
        super().__init__(f"git {args[0] if args else ''} failed ({result.returncode}): {said.strip()}", args, directory)
        self.result = result

    @property
    def returncode(self) -> int:
        return self.result.returncode

    @property
    def stdout(self):
        return self.result.stdout

    @property
    def stderr(self):
        return self.result.stderr


class GitResult(NamedTuple, Generic[T]):
    """What git did: unpacks as ``(returncode, stdout, stderr)``."""

    returncode: int
    stdout: T
    stderr: T

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Git:
    """Git in ``directory`` (``-C``; None for the process's own directory), with a default ``timeout`` in seconds (None
    waits for ever) and ``env`` laid over the process's environment, or in its place when ``inherit`` is False.

    With ``chdir`` git starts in ``directory`` instead of being given ``-C``: a directory that is not there is then
    GitUnavailable rather than git's own exit 128.

    Text is decoded with ``encoding`` (None: the locale's, as ``text=True`` does) and ``errors``; ``binary=True`` on a
    call returns bytes."""

    def __init__(
        self,
        directory: str | os.PathLike | None = None,
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        inherit: bool = True,
        encoding: str | None = "utf-8",
        errors: str = "replace",
        chdir: bool = False,
    ):
        self.chdir = chdir
        self.directory = Path(directory) if directory is not None else None
        self.timeout = timeout
        self.env = dict(env) if env is not None else None
        self.inherit = inherit
        self.encoding = encoding
        self.errors = errors

    def __repr__(self) -> str:
        return f"Git({str(self.directory) if self.directory else ''!r})"

    def fail(self, error: GitError) -> BaseException:
        """What a call raises for ``error``: the error itself here; a subclass words it as its callers expect (a
        Template Method hook). ``read``, ``value`` and ``ok`` catch only GitError, so what it returns otherwise
        reaches their callers."""
        return error

    def argv(self, args: tuple[str, ...]) -> list[str]:
        here = self.directory is not None and not self.chdir
        return ["git", *(["-C", str(self.directory)] if here else []), *args]

    def environment(self, env: Mapping[str, str] | None = None) -> dict[str, str] | None:
        """The environment git runs in: the process's (or none of it), then this Git's ``env``, then the call's."""
        if self.env is None and env is None and self.inherit:
            return None
        merged = dict(os.environ) if self.inherit else {}
        merged.update(self.env or {})
        merged.update(env or {})
        return merged

    # -- blocking --------------------------------------------------------------------------------------------------

    @overload
    def run(
        self,
        *args: str,
        check: bool = ...,
        timeout: float | None = ...,
        env: Mapping[str, str] | None = ...,
        binary: Literal[False] = False,
        input: str | None = ...,
    ) -> GitResult[str]: ...
    @overload
    def run(
        self,
        *args: str,
        check: bool = ...,
        timeout: float | None = ...,
        env: Mapping[str, str] | None = ...,
        binary: Literal[True],
        input: bytes | None = ...,
    ) -> GitResult[bytes]: ...
    def run(
        self,
        *args: str,
        check: bool = True,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        binary: bool = False,
        input: str | bytes | None = None,
    ) -> GitResult:
        """``git ARGS``, waited for; GitFailed when it exits other than 0 and ``check``."""
        wait = self.timeout if timeout is None else timeout
        text = {} if binary else {"text": True, "encoding": self.encoding, "errors": self.errors}
        try:
            done = subprocess.run(
                self.argv(args),
                capture_output=True,
                timeout=wait,
                env=self.environment(env),
                input=input,
                cwd=self.directory if self.chdir else None,
                check=False,
                **text,
            )
        except subprocess.TimeoutExpired as exc:
            raise self.fail(GitTimeout(args, self.directory, wait or 0.0, str(exc))) from None
        except OSError as exc:
            raise self.fail(GitUnavailable(args, self.directory, exc)) from None
        result = GitResult(done.returncode, done.stdout, done.stderr)
        if check and not result.ok:
            raise self.fail(GitFailed(result, args, self.directory))
        return result

    def out(self, *args: str, timeout: float | None = None, env: Mapping[str, str] | None = None) -> str:
        """What ``git ARGS`` prints; GitError when it fails."""
        return self.run(*args, timeout=timeout, env=env).stdout

    def read(self, *args: str, timeout: float | None = None, env: Mapping[str, str] | None = None) -> str | None:
        """What ``git ARGS`` prints, or None when it fails in any way (a read whose failure means "not there")."""
        try:
            return self.run(*args, timeout=timeout, env=env).stdout
        except GitError:
            return None

    def value(self, *args: str, timeout: float | None = None, env: Mapping[str, str] | None = None) -> str | None:
        """What ``git ARGS`` prints, stripped; None when it fails in any way or prints nothing (a ref, a config key)."""
        text = (self.read(*args, timeout=timeout, env=env) or "").strip()
        return text or None

    def ok(self, *args: str, timeout: float | None = None, env: Mapping[str, str] | None = None) -> bool:
        """Whether ``git ARGS`` exits 0; False when it cannot run or gives no answer."""
        try:
            return self.run(*args, check=False, timeout=timeout, env=env).ok
        except GitError:
            return False

    # -- asyncio ---------------------------------------------------------------------------------------------------

    async def arun(
        self,
        *args: str,
        check: bool = True,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        binary: bool = False,
    ) -> GitResult:
        """``git ARGS`` as a subprocess of the event loop, in a session of its own and with no stdin; killed when it
        gives no answer within the timeout (GitTimeout). Decoded like ``run``, or bytes with ``binary``."""
        wait = self.timeout if timeout is None else timeout
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.argv(args),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self.environment(env),
                cwd=self.directory if self.chdir else None,
                start_new_session=True,
            )
        except OSError as exc:
            raise self.fail(GitUnavailable(args, self.directory, exc)) from None
        try:
            out, err = await asyncio.wait_for(proc.communicate(), wait)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise self.fail(GitTimeout(args, self.directory, wait or 0.0)) from None
        code = proc.returncode if proc.returncode is not None else -1
        if binary:
            result: GitResult = GitResult(code, out, err)
        else:
            encoding = self.encoding or "utf-8"
            result = GitResult(code, out.decode(encoding, self.errors), err.decode(encoding, self.errors))
        if check and not result.ok:
            raise self.fail(GitFailed(result, args, self.directory))
        return result

    async def aout(
        self, *args: str, timeout: float | None = None, env: Mapping[str, str] | None = None, binary: bool = False
    ):
        """What ``git ARGS`` prints, from ``arun``; GitError when it fails."""
        return (await self.arun(*args, timeout=timeout, env=env, binary=binary)).stdout
