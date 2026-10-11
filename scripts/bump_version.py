import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path("evo_agents") / "__init__.py"
RELEASE = re.compile(r"\d+\.\d+\.\d+")


def current_version(root: Path) -> str:
    match = re.search(r'^__version__ = "([^"]+)"$', (root / SOURCE).read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise SystemExit(f"{SOURCE.as_posix()} sets no __version__")
    return match.group(1)


def targets(root: Path) -> list[tuple[Path, str]]:
    found = [(SOURCE, '__version__ = "')]
    for plugin in sorted(path for path in (root / "plugins").iterdir() if path.is_dir()):
        base = plugin.relative_to(root)
        found += [
            (base / ".claude-plugin" / "plugin.json", '"version": "'),
            (base / ".mcp.json", "evo-ak=="),
            (base / "hooks" / "hooks.json", "evo-ak=="),
        ]
    return found


def bump(root: Path, new: str) -> list[tuple[Path, int]]:
    if not RELEASE.fullmatch(new):
        raise SystemExit(f"{new} is not a release version, X.Y.Z")
    old = current_version(root)
    if new == old:
        raise SystemExit(f"the version is already {old}")
    edits = []
    for path, prefix in targets(root):
        pattern = re.compile(re.escape(prefix + old) + r"(?![\w.])")
        text, count = pattern.subn(prefix + new, (root / path).read_bytes().decode("utf-8"))
        if not count:
            raise SystemExit(f"{path.as_posix()} does not name {old}")
        edits.append((path, text, count))
    for path, text, _ in edits:
        (root / path).write_bytes(text.encode("utf-8"))
    return [(path, count) for path, _, count in edits]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set the release version in every file that names it.")
    parser.add_argument("version", help="the new version, X.Y.Z")
    parser.add_argument("--root", type=Path, default=ROOT, help="the checkout to change (default: this one)")
    args = parser.parse_args(argv)
    old = current_version(args.root)
    for path, count in bump(args.root, args.version):
        print(f"{path.as_posix()}: {count}")
    print(f"{old} -> {args.version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
