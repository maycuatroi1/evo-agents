"""What one link task reads of the identifier index.

A link task notes what it reads of the index (``Reads``), and the build memoizes it by those entries rather than by
the whole index (``Memo.run_traced``): a changed file links again the items that looked up one of its names or
paths, not every item of the project.
"""

from __future__ import annotations

from evo_agents.kg.protocol import canonical_json, sha256

# Tables a link task reads whole: small, seldom changed, and looked up once per word of prose, so noting each key
# would make the list of what a task read longer than its output.
WHOLE_TABLES = frozenset({"repo", "seam", "plan", "identifiers"})
WHOLE, KEY = 0, 1  # how a task read a table: whole, or one key of it
NONE_KEY = "\0none"  # the key None, as the list of what a task read writes it


class Reads:
    """The identifier index as one link task sees it, noting what the task reads: one key of a table (absent keys
    too), or a whole table of WHOLE_TABLES. ``keys()`` lists them for ``Memo.run_traced``; ``Held`` digests what
    they hold."""

    def __init__(self, index: dict):
        self._index = index
        self._keys: set[tuple] = set()

    def __getitem__(self, table: str) -> _Table:
        return _Table(self, table, self._index[table])

    def get(self, table: str, default=None):
        return _Table(self, table, self._index[table]) if table in self._index else default

    def note(self, table: str, key=None) -> None:
        self._keys.add((table, WHOLE, "") if key is None or table in WHOLE_TABLES else (table, KEY, key))

    def keys(self) -> list[list]:
        return [list(k) for k in sorted(self._keys, key=canonical_json)]


class _Table:
    """One table of a ``Reads``: every lookup is noted before it is answered as the table answers it."""

    __slots__ = ("_name", "_reads", "_table")

    def __init__(self, reads: Reads, name: str, table):
        self._reads, self._name, self._table = reads, name, table

    def _key(self, key):
        self._reads.note(self._name, NONE_KEY if key is None else key)
        return key

    def get(self, key, default=None):
        return self._table.get(self._key(key), default)

    def __getitem__(self, key):
        return self._table[self._key(key)]

    def __contains__(self, key) -> bool:
        return self._key(key) in self._table

    def __iter__(self):
        self._reads.note(self._name)
        return iter(self._table)

    def __len__(self) -> int:
        self._reads.note(self._name)
        return len(self._table)


class Held:
    """What the entries of one build's identifier index hold, each digested once: ``digest(keys)`` is the ``reads``
    input of a traced link task that read ``keys`` (``Reads.keys``)."""

    def __init__(self, index: dict):
        self.index = index
        self._cache: dict[tuple, str] = {}

    def digest(self, keys: list[list]) -> str:
        return sha256(canonical_json([self._one(*k) for k in keys]))

    def _one(self, table: str, how: int, key: str) -> str:
        found = self._cache.get((table, how, key))
        if found is None:
            held, real = self.index.get(table), None if key == NONE_KEY else key
            if how == WHOLE:
                value = ["whole", _plain(held)]
            elif isinstance(held, (set, frozenset)):
                value = ["member", real in held]
            else:
                value = ["value", _plain(held[real])] if held is not None and real in held else ["absent"]
            found = sha256(canonical_json([table, how, key, value]))
            self._cache[(table, how, key)] = found
        return found


def _plain(value):
    """``value`` as canonical JSON can write it: sets sorted, tuples as lists, dict keys as they are."""
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value
