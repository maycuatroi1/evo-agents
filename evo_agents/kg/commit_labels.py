"""Which commits of a repo's history are questions, and which files answer them (the filter of ``kg rank train``'s git
labels and of ``kg eval-questions --from-commits``).

A commit's subject and body is the question; the files it changed that the graph holds are the answer. A commit of a
bot, with fewer than ``min_words`` words once pull request numbers and ``Signed-off-by`` style trailers are taken out,
or changing more than ``max_files`` files once lock files and generated files are left out, is no question: it says
too little or names a sweep, not one place. The thresholds were chosen on the tune part of the bench (docs/kg-bench.md).
"""

from __future__ import annotations

import re

MIN_WORDS = 4  # a commit message of fewer words says too little to be a question (chosen on tune: docs/kg-bench.md)
MAX_FILES = 8  # a commit changing more files than this is a sweep, not one place (chosen on tune)
BOTS = re.compile(r"\[bot\]|dependabot|renovate|pre-commit-ci|github-actions|greenkeeper", re.I)
GENERATED = re.compile(
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|uv\.lock|Pipfile\.lock|Cargo\.lock|go\.sum|"
    r"composer\.lock|Gemfile\.lock|.*\.min\.(js|css)|.*\.map|.*_pb2\.py|.*\.pb\.go|.*\.snap|CHANGELOG[^/]*|CHANGES[^/]*|"
    r"NEWS[^/]*|HISTORY[^/]*)$|(^|/)(dist|build|vendor|node_modules|third_party|changelog\.d|newsfragments|"
    r"changes|doc/whatsnew|docs/release)/",
    re.I,
)
PR_REF = re.compile(r"\(#\d+\)|#\d+")


def commit_label(commit: dict, held: set[str], min_words: int, max_files: int) -> tuple[str, frozenset[str]] | None:
    """(question, answer files) of a commit, or None when it is no label (see the module)."""
    if BOTS.search(commit["author"]):
        return None
    text = PR_REF.sub(" ", commit["message"])
    text = "\n".join(line for line in text.splitlines() if not re.match(r"^\s*[\w-]+-by:", line, re.I))
    if len(re.findall(r"\w+", text)) < min_words:
        return None
    changed = [f for f in commit["files"] if not GENERATED.search(f)]
    if not changed or len(changed) > max_files:
        return None
    gold = frozenset(f for f in changed if f in held)
    return (text.strip(), gold) if gold else None
