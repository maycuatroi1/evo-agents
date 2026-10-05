"""A terminal UI for the worker's tests, in place of a runtime's: ``fake_tui.py SESSION NAME [FIRST_PROMPT_LINE]``.

It prints its session and tmux session, and the first line of its prompt when it has one, then reads lines: each one
is echoed as ``echo: <line>``, ``clear`` clears the screen, and ``exit`` ends it with status 0.
"""

import sys


def main() -> int:
    session, name = sys.argv[1], sys.argv[2]
    prompt = sys.argv[3] if len(sys.argv) > 3 else ""
    print(f"fake tui: session {session} in {name}")
    if prompt:
        print(f"fake tui: prompt {prompt}")
    for line in sys.stdin:
        line = line.rstrip("\r\n")
        if line == "exit":
            print("fake tui: bye")
            return 0
        if line == "clear":
            print("\x1b[2J\x1b[H", end="")
            print("fake tui: cleared")
            continue
        print(f"echo: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
