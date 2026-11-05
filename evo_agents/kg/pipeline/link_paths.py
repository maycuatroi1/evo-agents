"""How the link stage (``pipeline.link``) reads a path: the file or directory it names, from the item's directory,
a package root of its repo, another repo, or the tail of one path of the project; and when it names none, whether the
text around it says it is of another tree, a placeholder, or a file the history read (``unseen``)."""

from __future__ import annotations

import posixpath

from evo_agents.kg.pipeline.link_forms import (
    CD,
    LOCKFILE,
    PLAN_FILE,
    REPO_AFTER,
    REPO_BEFORE,
    is_placeholder,
    unique,
)


class PathResolver:
    """The path resolution of a ``Linker``: what it reads of the item it links, which ``Linker`` sets."""

    index: dict
    repo: str | None
    path: str
    base_dir: str
    touched: frozenset[str]
    dated: bool
    fixture: bool

    def _at(self, key: str, as_dir: bool, keep=None) -> tuple[list[dict], bool]:
        """Index entries at one path that ``keep`` accepts, and whether they are directories: files when
        any is accepted, else directories. A path written with a trailing slash only names directories."""
        for table in ("dir",) if as_dir else ("path", "dir"):
            entries = [e for e in self.index[table].get(key, []) if keep is None or keep(e)]
            if entries:
                return entries, table == "dir"
        return [], False

    def path_target(self, path: str, repo: str | None, guess: bool = False) -> tuple[str | None, str]:
        """``_path_target``, and for a module ``x.py`` it finds nothing at, the package ``x/__init__.py`` it became."""
        found = self._path_target(path, repo, guess)
        if found[0] is None and path.endswith(".py") and posixpath.basename(path) != "__init__.py":
            package = self._path_target(f"{path[:-3]}/__init__.py", repo, guess)
            if package[0] is not None:
                return package
        return found

    def unseen(self, path: str, text: str, span: tuple[int, int], step_repo: str | None) -> tuple[str | None, str]:
        """What a path ``path_target`` found dangling names after all, by what the text around it says (the module
        docstring): the file of the tree it is of, ``outside`` for a tree no source reads, a placeholder or a fixture's
        text, ``past`` for a file the history read in a dated item; else still ``dangling``."""
        tree = self.tree_of(text, span) or step_repo
        if tree and tree != self.repo:
            if tree not in self.index["read_repos"]:
                return None, "outside"
            found = self.path_target(path, tree)
            if found[0] is not None:
                return found
        moved = PLAN_FILE.search(path)
        if moved:  # a plan closed or reopened since: plans/active/x.yaml is now plans/completed/x.yaml
            other = "completed" if moved.group(2) == "active" else "active"
            found = self.path_target(f"{path[: moved.start(2)]}{other}/{moved.group(3)}", None)
            if found[0] is not None:
                return found
        if self.fixture or is_placeholder(path) or LOCKFILE.search(path):
            return None, "outside"
        if self.dated and posixpath.normpath(path).lstrip("/") in self.index["past"]:
            return None, "past"
        return None, "dangling"

    def tree_of(self, text: str, span: tuple[int, int]) -> str | None:
        """The tree a path at ``span`` of ``text`` is written of: the last segment of the last ``cd`` before it whose
        subshell is still open, else a repo of harness.yaml named next to it or in the first cell of its table row."""
        before = text[: span[0]]
        line = before[before.rfind("\n") + 1 :].lstrip()
        if line.startswith("|"):
            first = line.split("|")[1].strip().strip("`*").strip()
            if first in self.index["repo"]:
                return first
        moves = list(CD.finditer(before))
        if moves:
            rest = before[moves[-1].end() :]
            if rest.count(")") <= rest.count("("):
                name = posixpath.basename(moves[-1].group(1).rstrip("/"))
                if name and not name.startswith("$"):
                    return name
        for m in (
            REPO_BEFORE.search(text, max(0, span[0] - 60), span[0]),
            REPO_AFTER.match(text[span[1] : span[1] + 60]),
        ):
            if m and m.group(1) in self.index["repo"]:
                return m.group(1)
        return None

    def _path_target(self, path: str, repo: str | None, guess: bool = False) -> tuple[str | None, str]:
        """The file or directory a path names, and how: ``parsed`` relative to this item's directory or with its repo,
        ``resolved`` as written from a repo's root, ``ambiguous`` or ``dangling`` when it finds two or none. With
        ``guess`` (a path in prose), a path not found as written is also tried ``resolved``: relative to a directory
        above this item's, under a package root of a repo (``_package_roots``), then as the end of one file's path,
        in this item's repo before any other; a path from a root (``/x/y.md``), as the end of one file's path in this
        item's repo alone (``_site_path``). A path still not found whose directory no directory of the project ends
        with is ``outside``, as a URL of an unknown host is: it names a file of another tree (``.evo-run/plan.yaml``,
        ``icons/x.svg`` of a vendored package), not a missing file of this one."""
        as_dir = path.endswith("/")
        if not repo and self.base_dir and not path.startswith("/"):
            relative = posixpath.normpath(posixpath.join(self.base_dir, path))
            near, _ = self._at(relative, as_dir, lambda e: e.get("repo") == self.repo)
            if len(near) == 1:
                return near[0]["node"], "parsed"
        key = posixpath.normpath(path).lstrip("/")
        if repo:
            scoped, _ = self._at(key, as_dir, lambda e: e.get("repo") == repo)
            if scoped:
                return unique(scoped), "parsed"
            return None, "dangling"
        same, is_dir = self._at(key, as_dir, lambda e: self.repo and e.get("repo") == self.repo)
        if len(same) == 1:
            return same[0]["node"], "parsed" if is_dir else "resolved"
        entries, is_dir = self._at(key, as_dir)
        if entries:
            target = unique(entries)
            return (target, "parsed" if is_dir else "resolved") if target else (None, "ambiguous")
        if guess and key not in (".", "") and not path.startswith(("./", "../", "//")):
            found = self._site_path(key, as_dir) if path.startswith("/") else self._guess_path(key, as_dir)
            parent = posixpath.dirname(key.rstrip("/"))
            if found[1] == "dangling" and parent and parent not in self.index["dirsuffix"]:
                return None, "outside"  # no directory of the project ends so: a path of another tree
            return found
        return None, "dangling"

    def _site_path(self, key: str, as_dir: bool) -> tuple[str | None, str]:
        """A path written from the root of a site or a docs bundle (``/master-plan/x.md`` for ``docs/master-plan/x.md``
        when the bundle's root is ``docs/``): the one file of this item's repo whose path ends with it, those under a
        directory above this item first (a fixture's copy of the bundle is another site)."""
        if as_dir or not self.repo:
            return None, "dangling"
        found = [e for e in self.index["pathsuffix"].get(key, []) if e.get("repo") == self.repo]
        if not found:
            return None, "dangling"
        above = f"{self.base_dir}/" if self.base_dir else None
        target = unique([e for e in found if above and above.startswith(e["path"][: -len(key)])] or found)
        return (target, "resolved") if target else (None, "ambiguous")

    def _guess_path(self, key: str, as_dir: bool) -> tuple[str | None, str]:
        """A path written short: the one match of the first way that finds any (``path_target``)."""
        found: list[dict] = []
        directory = posixpath.dirname(self.base_dir) if self.base_dir else ""
        while directory and not found:  # the directories above this item's, nearest first
            found, _ = self._at(f"{directory}/{key}", as_dir, lambda e: e.get("repo") == self.repo)
            directory = posixpath.dirname(directory)
        if not found:
            for repo in [self.repo] if self.repo else []:
                for root in self.index["roots"].get(repo, []):
                    found += self._at(f"{root}/{key}", as_dir, lambda e, r=repo: e.get("repo") == r)[0]
        if not found:
            found, _ = self._at_roots(key, as_dir)
        if not found and not as_dir:
            entries = self.index["pathsuffix"].get(key, [])
            found = [e for e in entries if self.repo and e.get("repo") == self.repo] or entries
            if len(found) > 1 and self.touched:  # a commit's message names a file it changed
                found = [e for e in found if e["path"] in self.touched] or found
        if not found:
            return None, "dangling"
        target = unique(found)
        return (target, "resolved") if target else (None, "ambiguous")

    def _at_roots(self, key: str, as_dir: bool) -> tuple[list[dict], bool]:
        """Entries at ``key`` under a package root of any repo other than this item's."""
        found: list[dict] = []
        for repo in self.index["roots"]:
            if repo == self.repo:
                continue
            for root in self.index["roots"].get(repo, []):
                found += self._at(f"{root}/{key}", as_dir, lambda e, r=repo: e.get("repo") == r)[0]
        return found, False
