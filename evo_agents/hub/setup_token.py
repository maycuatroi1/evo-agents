"""``claude setup-token`` for ``evo-agents hub account login``: running it unchanged, and reading the token it prints.

``claude setup-token`` is Anthropic's own sign-in of Claude Code: it opens claude.ai in the browser and, once the
member approves there, prints an OAuth token that lives a year, for CLAUDE_CODE_OAUTH_TOKEN. Nothing here calls an
OAuth endpoint, keeps a refresh token or mints anything: ``run`` starts ``claude setup-token`` on a terminal of its
own (a pseudo-terminal), relays its screen to the member and their keys to it, and keeps what it printed in memory
alone, never in a file. Claude Code draws its screen with cursor moves (a space is often a jump to a column), so
``Screen`` replays the output on a grid the size of that terminal, and ``find_token`` reads the token off the grid,
joining it again where the screen's width wrapped it.

The token never reaches the member's screen through the relay: from the first byte of it on, ``Mask`` passes on only
the escape sequences that set or reset the terminal's modes, so the terminal comes back as Claude Code left it. When
the grid holds no token (a sign-in cancelled, a version of Claude Code that prints it otherwise), ``run`` gives None
and the command asks for the token instead, so a change on Anthropic's side never leaves the member stuck.

Standard library only, like the rest of the client. ``pty`` is POSIX; elsewhere ``run`` lets ``claude setup-token``
use the terminal itself and gives None, so the member pastes what it printed.
"""

from __future__ import annotations

import os
import re
import select
import signal
import struct
import subprocess
import sys
import time
import unicodedata
import warnings
from datetime import date, timedelta

ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
LEAD = "sk-ant-"  # how every key and token of Anthropic starts
API_KEY_LEAD = "sk-ant-api"  # an API key of the Anthropic Console, which goes in ANTHROPIC_API_KEY
TOKEN = re.compile(r"sk-ant-[a-z]{2,}[0-9]*-[A-Za-z0-9_-]{20,}")
PART = re.compile(r"[A-Za-z0-9_-]+")
DEFAULT_SIZE = (80, 24)  # columns and rows, when no terminal says its own
LINGER_S = 5.0  # how long claude setup-token may run on once the token is out, before it is stopped
TICK_S = 0.5

# What a terminal reads in the output besides text: CSI sequences (the parameters and the final byte kept), OSC, DCS,
# APC, PM and SOS strings up to BEL or ST, other escapes of one or two characters, and control characters.
ESCAPE = re.compile(
    r"\x1b\[(?P<params>[0-?]*)[ -/]*(?P<final>[@-~])"
    r"|\x1b[\]P_^X][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[ -/]*[0-~]"
    r"|[\x00-\x1f\x7f]"
)
CSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
MODES = re.compile(rb"\x1b\[[?<>=]?[0-9;:]*[hlmu]")  # set and reset a mode, pop the keyboard protocol, colours
RESTORE = b"\x1b[0m\x1b[?25h"


def default_end(today: date) -> date:
    """The day a token of ``claude setup-token`` made ``today`` is taken out of use: a year on, less a day."""
    try:
        later = today.replace(year=today.year + 1)
    except ValueError:  # 29 February
        later = today + timedelta(days=365)
    return later - timedelta(days=1)


def is_api_key(value: str) -> bool:
    return value.startswith(API_KEY_LEAD)


# The screen


class Screen:
    """A terminal's screen of ``cols`` by ``rows`` cells and the rows scrolled off its top, as the output that ran on
    it left them. It reads what places text: printable characters, line feeds, carriage returns, cursor moves, erases,
    insertions and deletions; colours, modes and titles leave it as it is."""

    def __init__(self, cols: int, rows: int):
        self.cols, self.rows = max(cols, 1), max(rows, 1)
        self.history: list[list[str]] = []
        self.grid = [self._blank() for _ in range(self.rows)]
        self.row = self.col = 0
        self.saved = (0, 0)

    def _blank(self) -> list[str]:
        return [" "] * self.cols

    def lines(self) -> list[str]:
        return ["".join(cells).rstrip() for cells in self.history + self.grid]

    def feed(self, text: str) -> None:
        at = 0
        for match in ESCAPE.finditer(text):
            self._print(text[at : match.start()])
            at = match.end()
            if match.group("final") is not None:
                self._csi(match.group("params"), match.group("final"))
            else:
                self._control(match.group())
        self._print(text[at:])

    def _print(self, text: str) -> None:
        for char in text:
            width = 2 if unicodedata.east_asian_width(char) in "WF" else 0 if unicodedata.combining(char) else 1
            if width == 0:
                continue
            if self.col + width > self.cols:  # the right margin: the text goes on at the start of the next row
                self.col = 0
                self._line_feed()
            self.grid[self.row][self.col] = char
            if width == 2 and self.col + 1 < self.cols:
                self.grid[self.row][self.col + 1] = ""
            self.col += width

    def _line_feed(self) -> None:
        if self.row == self.rows - 1:
            self.history.append(self.grid.pop(0))
            self.grid.append(self._blank())
        else:
            self.row += 1

    def _move(self, row: int, col: int) -> None:
        self.row = min(max(row, 0), self.rows - 1)
        self.col = min(max(col, 0), self.cols - 1)

    def _control(self, sequence: str) -> None:
        if sequence == "\r":
            self.col = 0
        elif sequence in ("\n", "\x0b", "\x0c", "\x1bD"):
            self._line_feed()
        elif sequence == "\x1bE":
            self.col = 0
            self._line_feed()
        elif sequence == "\b":
            self.col = max(min(self.col, self.cols - 1) - 1, 0)
        elif sequence == "\t":
            self.col = min((self.col // 8 + 1) * 8, self.cols - 1)
        elif sequence == "\x1b7":
            self.saved = (self.row, self.col)
        elif sequence == "\x1b8":
            self._move(*self.saved)
        elif sequence == "\x1bM":
            self._reverse_index()
        elif sequence == "\x1bc":
            self.grid = [self._blank() for _ in range(self.rows)]
            self._move(0, 0)

    def _reverse_index(self) -> None:
        if self.row == 0:
            self.grid.insert(0, self._blank())
            self.grid.pop()
        else:
            self.row -= 1

    def _csi(self, params: str, final: str) -> None:
        if params[:1] in ("?", "<", ">", "="):  # a private mode or query: nothing on the screen moves
            return
        numbers = [int(part) if part.isdigit() else 0 for part in params.split(";")] if params else []
        first = numbers[0] if numbers else 0
        count = max(first, 1)
        if final in MOVES:
            row, col = MOVES[final](self, count, numbers)
            self._move(row, col)
        elif final == "J":
            self._erase_display(first)
        elif final == "K":
            self._erase_line(first)
        elif final in EDITS:
            method, how = EDITS[final]
            getattr(self, method)(count, how)
        elif final == "s":
            self.saved = (self.row, self.col)
        elif final == "u":
            self._move(*self.saved)

    def _erase_line(self, mode: int) -> None:
        cells, col = self.grid[self.row], min(self.col, self.cols - 1)
        start, end = {0: (col, self.cols), 1: (0, col + 1)}.get(mode, (0, self.cols))
        cells[start:end] = [" "] * (end - start)

    def _erase_display(self, mode: int) -> None:
        if mode in (2, 3):
            self.grid = [self._blank() for _ in range(self.rows)]
            return
        self._erase_line(mode)
        rows = range(self.row + 1, self.rows) if mode == 0 else range(self.row)
        for row in rows:
            self.grid[row] = self._blank()

    def _chars(self, count: int, how: str) -> None:
        cells, col = self.grid[self.row], min(self.col, self.cols - 1)
        if how == "insert":
            cells[col:col] = [" "] * count
        elif how == "delete":
            del cells[col : col + count]
            cells.extend([" "] * count)
        else:
            end = min(col + count, self.cols)
            cells[col:end] = [" "] * (end - col)
        self.grid[self.row] = cells[: self.cols]

    def _lines(self, count: int, insert: bool) -> None:
        count = min(count, self.rows - self.row)
        for _ in range(count):
            if insert:
                self.grid.insert(self.row, self._blank())
                self.grid.pop()
            else:
                self.grid.pop(self.row)
                self.grid.append(self._blank())
        self.col = 0

    def _scroll(self, count: int, up: bool) -> None:
        for _ in range(min(count, self.rows)):
            if up:
                self.history.append(self.grid.pop(0))
                self.grid.append(self._blank())
            else:
                self.grid.insert(0, self._blank())
                self.grid.pop()


def _absolute(numbers: list[int], index: int) -> int:
    """A 1-based position of a CSI sequence, 0-based; 1 when left out or 0."""
    value = numbers[index] if len(numbers) > index else 0
    return max(value, 1) - 1


# Where each CSI sequence that moves the cursor puts it, from (count, parameters).
MOVES = {
    "A": lambda s, n, _: (s.row - n, s.col),
    "B": lambda s, n, _: (s.row + n, s.col),
    "e": lambda s, n, _: (s.row + n, s.col),
    "C": lambda s, n, _: (s.row, s.col + n),
    "a": lambda s, n, _: (s.row, s.col + n),
    "D": lambda s, n, _: (s.row, min(s.col, s.cols - 1) - n),
    "E": lambda s, n, _: (s.row + n, 0),
    "F": lambda s, n, _: (s.row - n, 0),
    "G": lambda s, n, p: (s.row, _absolute(p, 0)),
    "`": lambda s, n, p: (s.row, _absolute(p, 0)),
    "d": lambda s, n, p: (_absolute(p, 0), s.col),
    "H": lambda s, n, p: (_absolute(p, 0), _absolute(p, 1)),
    "f": lambda s, n, p: (_absolute(p, 0), _absolute(p, 1)),
}
# The method of Screen each CSI sequence that edits the screen calls, with its count and this.
EDITS = {
    "@": ("_chars", "insert"),
    "P": ("_chars", "delete"),
    "X": ("_chars", "erase"),
    "L": ("_lines", True),
    "M": ("_lines", False),
    "S": ("_scroll", True),
    "T": ("_scroll", False),
}


def find_token(lines: list[str], cols: int) -> str | None:
    """The token on the rows ``lines`` of a screen ``cols`` wide, joined again across the rows it wrapped onto; None
    when there is none, or what is there is not whole.

    A token that wraps fills its rows up to the same column: the edge of the box it is printed in, which may be the
    right margin (Claude Code 2.1.296 prints it from column 1 up to the margin), or the margin where the terminal
    wrapped it. The next row goes on with it when it holds nothing but token characters, starting where the token's
    first row started (the box) or, when that row reached the margin, at column 0 (the terminal's wrap), and no longer
    than the first row's part; the part that ends short is the last. Token characters alone on the next row anywhere
    else give None, never the first row's part alone: a token cut short would go to the hub and fail every run.
    """
    for at, line in enumerate(lines):
        start = line.find(LEAD)
        if start < 0:
            continue
        first = PART.match(line, start).group()
        end = start + len(first)
        rest = line[end:]
        if rest[:1] not in ("", " "):  # cut short, such as with an ellipsis: not the whole token
            return None
        if rest.strip():  # more text after a space on its row: the token ended there
            return _whole(first)
        indents = {start, 0} if end >= cols else {start}
        parts = [first]
        for following in lines[at + 1 :]:
            part = following.strip()
            if not PART.fullmatch(part):
                break
            indent = len(following) - len(following.lstrip(" "))
            if indent not in indents or indent + len(part) > end:
                return None  # not where a wrap of this token puts its next part, or longer than its rows
            parts.append(part)
            if indent + len(part) < end:
                break
            indents = {indent}
        return _whole("".join(parts))
    return None


def _whole(token: str) -> str | None:
    return token if TOKEN.fullmatch(token) else None


# The relay


class Mask:
    """What of claude setup-token's output reaches the member's screen: all of it up to the first byte of the token,
    then only the sequences that set or reset the terminal's modes."""

    def __init__(self, write):
        self.write = write
        self.masked = False
        self.held = b""
        self.lead = LEAD.encode()

    def feed(self, data: bytes) -> None:
        data = self.held + data
        self.held = b""
        if not self.masked:
            at = data.find(self.lead)
            if at < 0:
                keep = next((n for n in range(len(self.lead) - 1, 0, -1) if data.endswith(self.lead[:n])), 0)
                self.write(data[: len(data) - keep])
                self.held = data[len(data) - keep :]
                return
            self.write(data[:at])
            self.masked = True
            data = data[at:]
        self._modes(data)

    def _modes(self, data: bytes) -> None:
        end = 0
        for match in CSI.finditer(data):
            if MODES.fullmatch(match.group()):
                self.write(match.group())
            end = match.end()
        tail = data.rfind(b"\x1b")
        if tail >= end and len(data) - tail < 64:  # a sequence the next read completes
            self.held = data[tail:]

    def close(self) -> None:
        if self.masked:
            self.write(RESTORE + b"\r\n")
        elif self.held:
            self.write(self.held)
        self.held = b""


def terminal_size() -> tuple[int, int]:
    """(columns, rows) of the member's terminal, from stderr, stdin or stdout; DEFAULT_SIZE when none is one."""
    for fd in (2, 0, 1):
        try:
            size = os.get_terminal_size(fd)
        except OSError:
            continue
        if size.columns > 0 and size.lines > 0:
            return size.columns, size.lines
    return DEFAULT_SIZE


def _writer(stream):
    """A function that writes bytes to ``stream`` at once: to its buffer when it has one, else to its descriptor."""
    buffer = getattr(stream, "buffer", None)

    def write(data: bytes) -> None:
        if not data:
            return
        stream.flush()
        if buffer is not None:
            buffer.write(data)
            buffer.flush()
        else:
            os.write(stream.fileno(), data)

    return write


def run(claude: str, stdin=None, screen=None) -> str | None:
    """Run ``claude setup-token`` (the executable ``claude``) with the member at it, relaying its screen to ``screen``
    (stderr by default) and their keys from ``stdin``; the token it printed, or None when none could be read."""
    stdin = sys.stdin if stdin is None else stdin
    screen = sys.stderr if screen is None else screen
    try:
        import pty
        import termios
        import tty
    except ImportError:  # no pseudo-terminal here: claude setup-token takes the terminal, the member pastes
        subprocess.run([claude, "setup-token"], check=False)
        return None
    sizes = [terminal_size()]
    pid, master = _spawn(pty, termios, claude, sizes[0])
    stdin_fd = stdin.fileno()
    saved = termios.tcgetattr(stdin_fd) if os.isatty(stdin_fd) else None
    resized = _follow_resizes(termios, master, sizes)
    mask = Mask(_writer(screen))
    try:
        if saved is not None:
            tty.setraw(stdin_fd)
        output = _relay(pid, master, stdin_fd, mask)
    finally:
        if saved is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved)
        if resized is not None:
            signal.signal(signal.SIGWINCH, resized)
        mask.close()
        os.close(master)
    shown = Screen(*sizes[-1])  # the size the token was printed at, most likely
    shown.feed(output.decode("utf-8", errors="replace"))
    return find_token(shown.lines(), shown.cols)


def _window(size: tuple[int, int]) -> bytes:
    cols, rows = size
    return struct.pack("HHHH", rows, cols, 0, 0)


def _spawn(pty, termios, claude: str, size: tuple[int, int]) -> tuple[int, int]:
    """claude setup-token in a child process on a new pseudo-terminal of ``size``: (its pid, the master's fd)."""
    import fcntl

    with warnings.catch_warnings():  # the child only sets its window and execs, which is safe with threads around
        warnings.simplefilter("ignore", DeprecationWarning)
        pid, master = pty.fork()
    if pid == 0:
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ, _window(size))
            os.execv(claude, [claude, "setup-token"])
        finally:
            os._exit(127)
    return pid, master


def _follow_resizes(termios, master: int, sizes: list[tuple[int, int]]):
    """Pass each resize of the member's terminal on to the child's, adding the new size to ``sizes``; the handler there
    was before, or None when signals cannot be handled here (not the main thread)."""
    import fcntl

    def resize(signum, frame):
        sizes.append(terminal_size())
        try:
            fcntl.ioctl(master, termios.TIOCSWINSZ, _window(sizes[-1]))
        except OSError:
            pass

    try:
        return signal.signal(signal.SIGWINCH, resize)
    except (ValueError, AttributeError):
        return None


def _relay(pid: int, master: int, stdin_fd: int, mask: Mask) -> bytes:
    """Copy the child's output to ``mask`` and keep it, and the member's keys to the child, until the child is gone;
    stop it LINGER_S after the token came out if it is still there. What it printed, in memory."""
    output = bytearray()
    watched = [master, stdin_fd]
    stop_at, stopped = None, False
    while True:
        ready, _, _ = select.select(watched, [], [], TICK_S)
        if master in ready:
            try:
                data = os.read(master, 65536)
            except OSError:  # EIO: the child closed its terminal
                data = b""
            if not data:
                break
            output += data
            mask.feed(data)
            if mask.masked and stop_at is None:
                stop_at = time.monotonic() + LINGER_S
        if stdin_fd in ready:
            keys = os.read(stdin_fd, 1024)
            if keys:
                _write_all(master, keys)
            else:
                watched.remove(stdin_fd)
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            pid = 0
            _drain(master, output, mask)
            break
        if stop_at is not None and time.monotonic() > stop_at:
            os.kill(pid, signal.SIGKILL if stopped else signal.SIGTERM)
            stop_at, stopped = time.monotonic() + LINGER_S, True
    if pid:
        os.waitpid(pid, 0)
    return bytes(output)


def _drain(master: int, output: bytearray, mask: Mask) -> None:
    """Read what the child left on its terminal before it went."""
    while select.select([master], [], [], 0.1)[0]:
        try:
            data = os.read(master, 65536)
        except OSError:
            return
        if not data:
            return
        output += data
        mask.feed(data)


def _write_all(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data) :]
