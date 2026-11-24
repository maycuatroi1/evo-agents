"""A text index (``evo_agents.kg.snapshot_text.TextIndex``) after a delta (``TextIndex.updated``): what
``TextIndex.build`` makes of the rows then, made from the old index's parts. The rows kept come in runs between the
rows dropped and those added, and a run of rows is a run of each table's rows too: each run is copied as one slice,
and only the rows added are cut into tokens (``Table.updated``)."""

from __future__ import annotations

import bisect
from array import array
from pathlib import PurePosixPath

from evo_agents.kg.snapshot_text import Row, TextIndex, tri_texts

TABLES = ("public", "audience", "tri")


def _starts(index: TextIndex) -> dict[str, array]:
    """Per table, per row of ``index`` (and one past the last), how many rows before it the table holds: a run of
    rows ``a`` to ``b`` holds that table's rows ``starts[a]`` to ``starts[b]``. Kept on the index."""
    found = index.__dict__.get("_starts")
    if found is None:
        found = {t: array("I", [0]) for t in TABLES}
        public, audience, tri = (found[t] for t in TABLES)
        p = a = t = 0
        for r in index.rows:
            if r.aud is None:
                p += 1
                t += r.own
            else:
                a += 1
            public.append(p)
            audience.append(a)
            tri.append(t)
        index.__dict__["_starts"] = found
    return found


def _fresh(keyed) -> list[tuple[Row, tuple]]:
    out = []
    for key, row in keyed:
        out.append((Row(key, row[0], row[1], row[2] or "", row[3] or "", next(iter(row[5:6]), None)), row))
    out.sort(key=lambda x: x[0].key)
    return out


def _events(index: TextIndex, keys: list[str], gone, fresh: list) -> list[tuple[int, int, object]]:
    """The rows dropped and added, by where they are among the old rows: (position, 1, None) for a row dropped,
    (position, 0, (row, raw)) for one added before the old row at position. Raises ValueError for a key held
    twice."""
    out: list[tuple[int, int, object]] = []
    for k in gone:
        i = bisect.bisect_left(keys, k)
        if i < len(keys) and keys[i] == k:
            out.append((i, 1, None))
    for n, item in enumerate(fresh):
        k = item[0].key
        i = bisect.bisect_left(keys, k)
        if (i < len(keys) and keys[i] == k and k not in gone) or (n and fresh[n - 1][0].key == k):
            raise ValueError("a full-text key held twice")
        out.append((i, 0, item))
    out.sort(key=lambda e: (e[0], e[1]))
    return out


class _Parts:
    """The new index's rows and, per table, its texts and each row's number in the old table (-1 for a new one)."""

    def __init__(self, index: TextIndex):
        self.index, self.starts = index, _starts(index)
        self.rows: list[Row] = []
        self.texts: dict[str, list] = {t: [] for t in TABLES}
        self.old: dict[str, array] = {t: array("q") for t in TABLES}
        self.listed: dict[str, list[Row]] = {t: [] for t in TABLES}  # public_rows, audience_rows, tri_rows

    def run(self, a: int, b: int) -> None:
        """The old rows ``a`` to ``b``, kept."""
        if b <= a:
            return
        index = self.index
        self.rows += index.rows[a:b]
        for t, listed in (("public", index.public_rows), ("audience", index.audience_rows), ("tri", index.tri_rows)):
            lo, hi = self.starts[t][a], self.starts[t][b]
            if hi > lo:
                self.texts[t] += getattr(index, t).rows[lo:hi]
                self.old[t].extend(range(lo, hi))
                self.listed[t] += listed[lo:hi]

    def added(self, r: Row, raw: tuple) -> None:
        self.rows.append(r)
        table = "public" if r.aud is None else "audience"
        self._put(table, r, (r.name, r.path, raw[4] or ""))
        if r.aud is None and r.own:
            self._put("tri", r, tri_texts(r))

    def _put(self, table: str, r: Row, texts: tuple) -> None:
        self.texts[table].append(texts)
        self.old[table].append(-1)
        self.listed[table].append(r)


def updated(index: TextIndex, keyed, gone: frozenset[str], code: dict[str, str], code_gone) -> TextIndex:
    """``TextIndex.updated``."""
    keys = index.__dict__.get("_keys")
    if keys is None:
        keys = index.__dict__["_keys"] = [r.key for r in index.rows]
    fresh = _fresh(keyed)
    events = _events(index, keys, gone, fresh)
    parts = _Parts(index)
    at = 0
    for position, dropped, item in events:
        parts.run(at, position)
        at = max(at, position)
        if dropped:
            at = position + 1
        else:
            parts.added(*item)  # type: ignore[misc]
    parts.run(at, len(index.rows))
    tables = {}
    for t in TABLES:
        old = parts.old[t]
        same = len(old) == len(getattr(index, t).rows) and all(o >= 0 for o in old)
        tables[t] = getattr(index, t) if same else getattr(index, t).updated(parts.texts[t], old)
    code_table, code_ids = _code(index, code, code_gone)
    out = TextIndex.__new__(TextIndex)
    out.rows = parts.rows
    out.public_rows, out.audience_rows, out.tri_rows = (parts.listed[t] for t in TABLES)
    out.public, out.audience, out.tri = (tables[t] for t in TABLES)
    out.code, out.code_ids = code_table, code_ids
    out.own, out.files = _named(index, events, gone)
    out._kinds = {}
    out.__dict__["_keys"] = [r.key for r in out.rows] if events else keys
    return out


def _named(index: TextIndex, events, gone) -> tuple[dict, dict]:
    """(``own``, ``files``) of the new index: the old ones without the rows dropped, with those added."""
    if not events:
        return index.own, index.files
    dropped = [index.rows[p] for p, d, _ in events if d]
    own = dict(index.own)
    files = {w: list(rows) for w, rows in index.files.items()}
    for r in dropped:
        if r.aud is None and r.own and own.get(r.node_id) is r:
            del own[r.node_id]
        for word in _file_words(r):
            held = files.get(word)
            if held is not None and r in held:
                held[:] = [x for x in held if x is not r]
                if not held:
                    del files[word]
    for _, d, item in events:
        if d:
            continue
        r = item[0]  # type: ignore[index]
        if r.aud is None and r.own:
            own[r.node_id] = r
        for word in _file_words(r):
            held = files.setdefault(word, [])
            bisect.insort(held, r, key=lambda x: x.key)
    return own, files


def _file_words(r: Row) -> set[str]:
    if r.aud is not None or r.kind != "File" or not r.path:
        return set()
    pure = PurePosixPath(r.path)
    return {pure.name, pure.stem}


def _code(index: TextIndex, code: dict[str, str], gone) -> tuple:
    """(the code table, its file node ids) with the files ``gone`` dropped and ``code`` set."""
    if not code and not gone:
        return index.code, index.code_ids
    held = {nid: i for i, nid in enumerate(index.code_ids)}
    code_ids = tuple(sorted((held.keys() - set(gone)) | code.keys()))
    texts, old = [], []
    for nid in code_ids:
        texts.append((code[nid],) if nid in code else index.code.rows[held[nid]])
        old.append(-1 if nid in code else held[nid])
    return index.code.updated(texts, old), code_ids
