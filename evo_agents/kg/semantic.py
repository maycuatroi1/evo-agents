"""Search by meaning next to BM25: a multilingual embedding model run on this machine (the hub's or a worker's; the API backend
below is opt-in and sends texts to OpenRouter), a vector for each row of the full-text index that has a body, and the two rankings fused.

A project chooses, ``off`` by default (``Settings``): ``off`` writes no vector and searches by words alone; ``index``
writes the vectors with each build, but ``kg_search`` still ranks by words unless the caller asks for both (``kg eval
--search hybrid``, the KG optimize loop), so the two are compared on one build; ``on`` writes them and ``kg_search``
ranks by both. On this machine the choice is ``<kg home>/<project>/semantic.json`` (``evo-agents kg semantic``); on the
hub it is the project's row of kg_semantic, which the build reads (``evo_agents.hub.kg_build``).

What is embedded is what the full-text index holds for a row (``evo_agents.kg.search``): its name, its path and its
body, cut at EMBED_CHARS, for every row with a body: a node's own row, and each passage of a long section or file,
which already starts with its document's name and the headings above it. The index folds ``đ`` to ``d``, and the
query is folded the same way. Rows without a body (most symbols, directories, repos) get no vector: their names are
what BM25 finds them by.

The vectors sit in the graph file, in two tables outside the store's schema version and its content hash, found by
existence like the asked index: ``node_vec`` (one row a full-text row, keyed as ``node_fts_rows``, with its node, its
kind, its audience and its vector as float16) and ``node_vec_meta`` (the model, the dimension, the mode, the digest).
A reader of before ignores them; a graph without them, or a reader without the model, searches by words as before.
Embedding is the slow part, so each vector is also kept in ``vectors.sqlite`` next to the graph, by model and digest
of its text: a build embeds only the rows whose text changed since.

``Store.search`` fuses the two rankings by reciprocal rank fusion (Cormack et al., 2009): a node scores
1 / (RRF_K + its rank by words) plus SEMANTIC_WEIGHT / (RRF_K + its rank by meaning), each over the nodes the list holds,
the ranking by meaning weighed like the one by words (``search.weight``: background down, named nodes up) and holding
the SEMANTIC_POOL nearest. The model is loaded once a process (``embedder``), through fastembed, the optional extra
``evo-ak[semantic]``; without it ``index`` and ``on`` write nothing and search by words (docs/kg-semantic.md).
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from evo_agents.kg import search as text_search
from evo_agents.kg.protocol import canonical_json, sha256

MODES = ("off", "index", "on")
DEFAULT_MODEL = "BAAI/bge-m3"
SETTINGS_FILE = "semantic.json"
CACHE_FILE = "vectors.sqlite"
EMBED_CHARS = 2_000  # of a row's text, about the 512 tokens the model reads; a long body has passages for the rest
BATCH = 16
RRF_K = 60
SEMANTIC_WEIGHT = 0.5  # tuned on the tune split of the evo-agents questions: 0.25 to 5 move hit@5 by under a point
SEMANTIC_POOL = 100  # the nearest nodes the ranking by meaning holds
VEC_TABLES = ("node_vec", "node_vec_meta")
VEC_SCHEMA = """
CREATE TABLE node_vec (
    key TEXT PRIMARY KEY, node_id TEXT NOT NULL, kind TEXT NOT NULL, aud TEXT, digest TEXT NOT NULL, vec BLOB NOT NULL
) STRICT;
CREATE TABLE node_vec_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) STRICT;
"""
# The models this code knows how to load and what they are. BGE-M3: dense vectors of 1,024, over 100 languages, read
# through its ONNX export, CLS pooling, normalized. Any other name is read as a model fastembed itself lists.
KNOWN_MODELS = {
    "BAAI/bge-m3": {"dim": 1024, "pooling": "CLS", "file": "onnx/model.onnx", "extra": ["onnx/model.onnx_data"]},
}


class SemanticUnavailable(RuntimeError):
    """The model cannot be loaded here: fastembed or numpy is missing, or the model is not downloaded."""


# -- what a project chose -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    mode: str = "off"
    model: str = DEFAULT_MODEL

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"semantic mode {self.mode!r} is not one of {', '.join(MODES)}")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("semantic model must be a model name")

    @property
    def indexes(self) -> bool:
        return self.mode != "off"

    def to_json(self) -> dict:
        return {"mode": self.mode, "model": self.model}

    @classmethod
    def of(cls, data) -> Settings:
        data = data if isinstance(data, dict) else {}
        return cls(str(data.get("mode") or "off"), str(data.get("model") or DEFAULT_MODEL))


def read_settings(root: Path) -> Settings:
    """The choice of the project whose kg home is ``root``; off when it made none."""
    path = Path(root) / SETTINGS_FILE
    try:
        return Settings.of(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return Settings()
    except (ValueError, OSError) as exc:
        raise ValueError(f"{path}: {exc}") from None


def write_settings(root: Path, settings: Settings) -> Path:
    path = Path(root) / SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings.to_json(), indent=1) + "\n", encoding="utf-8")
    return path


# -- the model ----------------------------------------------------------------------------------------------------


class Embedder:
    """Vectors of texts, unit length. ``embed`` takes the texts of rows, ``query`` one query."""

    name: str
    dim: int

    def embed(self, texts: Sequence[str]) -> list:
        raise NotImplementedError

    def query(self, text: str):
        return self.embed([text])[0]


class FastEmbedder(Embedder):
    def __init__(self, name: str, download: bool):
        try:
            from fastembed import TextEmbedding
        except ImportError:
            raise SemanticUnavailable("fastembed is not installed: pip install 'evo-ak[semantic]'") from None
        known = KNOWN_MODELS.get(name)
        if known is not None and name not in {m["model"] for m in TextEmbedding.list_supported_models()}:
            from fastembed.common.model_description import ModelSource, PoolingType

            TextEmbedding.add_custom_model(
                model=name,
                pooling=getattr(PoolingType, known["pooling"]),
                normalization=True,
                sources=ModelSource(hf=name),
                dim=known["dim"],
                model_file=known["file"],
                additional_files=list(known["extra"]),
            )
        try:
            self.model = TextEmbedding(name, cache_dir=str(model_dir()), local_files_only=not download)
        except Exception as exc:  # fastembed raises ValueError, OSError or its hub's errors for a missing model
            raise SemanticUnavailable(f"model {name} cannot be loaded from {model_dir()}: {exc}") from None
        self.name = name
        self.dim = known["dim"] if known else len(self.query("x"))

    def embed(self, texts: Sequence[str]) -> list:
        return [v for v in self.model.embed(list(texts), batch_size=BATCH)]

    def query(self, text: str):
        return next(iter(self.model.query_embed(text)))


OPENROUTER_URL = "https://openrouter.ai/api/v1/embeddings"
OPENROUTER_MODELS = {"BAAI/bge-m3": "baai/bge-m3"}
OPENROUTER_BATCH = 64
OPENROUTER_USD_PER_MTOKEN = 0.01  # baai/bge-m3 on OpenRouter, measured 2026-10-29 (provider Parasail)
OPENROUTER_MAX_USD = 55.0  # a run that would pass this stops; the owner decides past 60
BACKEND_ENV = "EVO_KG_EMBEDDINGS"  # "openrouter" turns the API backend on; unset (default) is the local fastembed model


def spend_ledger() -> Path:
    """Where the API backend adds up what it spent: EVO_KG_EMBED_LEDGER, else next to the models (JSON lines)."""
    return Path(os.environ.get("EVO_KG_EMBED_LEDGER") or model_dir() / "openrouter-spend.jsonl").expanduser()


def spent_usd(ledger: Path | None = None) -> float:
    path = ledger or spend_ledger()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 0.0
    return sum(json.loads(line).get("usd", 0.0) for line in lines if line.strip())


class OpenRouterEmbedder(Embedder):
    """The same BGE-M3 vectors through OpenRouter's embeddings API (key in OPENROUTER_API_KEY, never written down):
    opt-in with EVO_KG_EMBEDDINGS=openrouter, for a host with no CPU time or GPU for the local model. Texts leave the
    machine, so it is not the default. Every call adds its tokens and cost to ``spend_ledger`` and stops at
    OPENROUTER_MAX_USD."""

    def __init__(self, name: str):
        if name not in OPENROUTER_MODELS:
            raise SemanticUnavailable(f"model {name} is not served by the API backend: {', '.join(OPENROUTER_MODELS)}")
        self.key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        if not self.key:
            raise SemanticUnavailable("OPENROUTER_API_KEY is not set")
        self.name, self.remote, self.dim = name, OPENROUTER_MODELS[name], KNOWN_MODELS[name]["dim"]
        self.provider: str | None = None

    def _post(self, texts: Sequence[str]) -> dict:
        import time
        import urllib.error
        import urllib.request

        body = json.dumps({"model": self.remote, "input": list(texts), "encoding_format": "float"}).encode()
        request = urllib.request.Request(
            OPENROUTER_URL,
            body,
            {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json", "X-Title": "evo-agents kg"},
        )
        last = ""
        for attempt in range(6):
            try:
                with urllib.request.urlopen(request, timeout=120) as reply:
                    data = json.loads(reply.read())
                if isinstance(data.get("data"), list) and len(data["data"]) == len(texts):
                    return data
                last = str(data.get("error") or "answer without data")[:200]
            except urllib.error.HTTPError as exc:
                last = f"HTTP {exc.code}"
                if exc.code not in (408, 429, 500, 502, 503, 504):
                    raise SemanticUnavailable(f"OpenRouter embeddings: {last}") from None
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last = type(exc).__name__
            time.sleep(min(2**attempt, 30))
        raise SemanticUnavailable(f"OpenRouter embeddings: {last} after 6 tries")

    def embed(self, texts: Sequence[str]) -> list:
        ledger = spend_ledger()
        out: list = []
        for start in range(0, len(texts), OPENROUTER_BATCH):
            part = [t or " " for t in texts[start : start + OPENROUTER_BATCH]]
            if spent_usd(ledger) >= OPENROUTER_MAX_USD:
                raise SemanticUnavailable(f"OpenRouter spend reached {OPENROUTER_MAX_USD} USD ({ledger}): stop and ask")
            data = self._post(part)
            usage = data.get("usage") or {}
            cost = usage.get("cost")
            usd = (
                float(cost)
                if isinstance(cost, (int, float))
                else usage.get("total_tokens", 0) * OPENROUTER_USD_PER_MTOKEN / 1e6
            )
            self.provider = data.get("provider") or self.provider
            ledger.parent.mkdir(parents=True, exist_ok=True)
            row = {"tokens": usage.get("total_tokens", 0), "usd": usd, "provider": self.provider, "inputs": len(part)}
            with ledger.open("a", encoding="utf-8") as f:  # one short append: whole lines even from several processes
                f.write(json.dumps(row) + "\n")
            ordered = sorted(data["data"], key=lambda item: item.get("index", 0))
            out += [self._unit(item["embedding"]) for item in ordered]
        return out

    @staticmethod
    def _unit(vector):
        np = _numpy()
        v = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(v))
        return v / norm if norm else v


def model_dir() -> Path:
    """Where models are downloaded and read: EVO_KG_MODEL_DIR, else ``~/.cache/evo-agents/models``."""
    return Path(os.environ.get("EVO_KG_MODEL_DIR") or "~/.cache/evo-agents/models").expanduser()


# Factories by model name ahead of fastembed's: a test, or a host with a model of its own, puts one here.
FACTORIES: dict[str, Callable[[], Embedder]] = {}
_LOADED: dict[str, Embedder] = {}
_FAILED: dict[str, str] = {}
_LOCK = threading.Lock()


def embedder(name: str, *, download: bool = False) -> Embedder:
    """The model ``name``, loaded once a process. ``download`` fetches it when it is not in ``model_dir`` (a build
    may; a search never waits on a download). Raises SemanticUnavailable."""
    with _LOCK:
        if name in _LOADED:
            return _LOADED[name]
        if name in FACTORIES:
            _LOADED[name] = FACTORIES[name]()
            return _LOADED[name]
        if name in _FAILED and not download:
            raise SemanticUnavailable(_FAILED[name])
        try:
            if os.environ.get(BACKEND_ENV, "").strip().lower() == "openrouter":
                _LOADED[name] = OpenRouterEmbedder(name)
            else:
                _LOADED[name] = FastEmbedder(name, download)
        except SemanticUnavailable as exc:
            _FAILED[name] = str(exc)
            raise
        _FAILED.pop(name, None)
        return _LOADED[name]


def forget_models() -> None:
    """Drop the loaded models and the failures remembered (tests)."""
    with _LOCK:
        _LOADED.clear()
        _FAILED.clear()
        _MATRICES.clear()


def _numpy():
    try:
        import numpy
    except ImportError:
        raise SemanticUnavailable("numpy is not installed: pip install 'evo-ak[semantic]'") from None
    return numpy


# -- the text of a row --------------------------------------------------------------------------------------------


def embed_text(name: str, path: str, body: str) -> str:
    """What is embedded of a full-text row: the first line of its name, its path, its body, cut at EMBED_CHARS."""
    first = str(name or "").split("\n", 1)[0]
    head = "\n".join(p for p in (first, path or "") if p)
    return f"{head}\n{body}"[:EMBED_CHARS]


def query_text(query: str) -> str:
    return text_search.fold(query)


def text_digest(model: str, text: str) -> str:
    return sha256(canonical_json([model, text]))


def index_rows(db: sqlite3.Connection) -> list[tuple[str, str, str, str | None, str]]:
    """(key, node id, kind, audience, text) of every full-text row with a body, by key."""
    out = []
    for table, aud in (("node_fts", "NULL"), ("node_fts_aud", "f.aud")):
        flag = 0 if table == "node_fts" else 1
        out += db.execute(
            f"SELECT r.node_id, f.node_id, f.kind, {aud}, f.name, f.path, f.body FROM node_fts_rows r"
            f" JOIN {table} f ON f.rowid = r.fts_rowid WHERE r.aud = ? AND length(trim(f.body)) > 0",
            (flag,),
        ).fetchall()
    return sorted((key, nid, kind, aud, embed_text(name, path, body)) for key, nid, kind, aud, name, path, body in out)


# -- the cache of vectors -----------------------------------------------------------------------------------------


class Cache:
    """Vectors by (model, digest of the text), in ``vectors.sqlite`` next to a graph: only rows whose text changed are
    embedded again."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS vec (model TEXT NOT NULL, digest TEXT NOT NULL, vec BLOB NOT NULL,"
            " PRIMARY KEY (model, digest)) STRICT, WITHOUT ROWID"
        )

    def get(self, model: str, digests: list[str]) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for i in range(0, len(digests), 500):
            chunk = digests[i : i + 500]
            marks = ", ".join("?" for _ in chunk)
            sql = f"SELECT digest, vec FROM vec WHERE model = ? AND digest IN ({marks})"
            out.update(self.db.execute(sql, (model, *chunk)).fetchall())
        return out

    def put(self, model: str, rows: Iterable[tuple[str, bytes]]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO vec VALUES (?, ?, ?)", [(model, d, v) for d, v in rows])
        self.db.commit()

    def keep(self, model: str, digests: set[str]) -> None:
        """Forget the vectors of ``model`` no row has now."""
        held = [d for (d,) in self.db.execute("SELECT digest FROM vec WHERE model = ?", (model,))]
        self.db.executemany(
            "DELETE FROM vec WHERE model = ? AND digest = ?", [(model, d) for d in held if d not in digests]
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()


# -- writing the vectors into a graph -----------------------------------------------------------------------------


@dataclass
class Indexed:
    """What ``index`` did: the mode written, the rows with a vector, how many were embedded now, and the digest
    (None when the graph holds no vector); ``error`` when the model could not be loaded."""

    mode: str
    rows: int = 0
    embedded: int = 0
    digest: str | None = None
    error: str | None = None

    def line(self) -> str:
        if self.error:
            return f"semantic {self.mode}: no vectors written, search stays by words: {self.error}"
        if self.mode == "off":
            return "semantic off: no vectors"
        return f"semantic {self.mode}: {self.rows} rows with a vector, {self.embedded} embedded now"


def has_vectors(db: sqlite3.Connection) -> bool:
    marks = ", ".join("?" for _ in VEC_TABLES)
    found = db.execute(f"SELECT count(*) FROM sqlite_master WHERE name IN ({marks})", VEC_TABLES).fetchone()[0]
    return found == len(VEC_TABLES)


def drop(db: sqlite3.Connection) -> None:
    for table in VEC_TABLES:
        db.execute(f"DROP TABLE IF EXISTS {table}")
    db.commit()


def index(
    db: sqlite3.Connection,
    settings: Settings,
    cache_path: Path | None,
    *,
    download: bool = True,
    progress: Callable[[int, int], None] | None = None,
) -> Indexed:
    """Make the graph in ``db`` hold the vectors ``settings`` asks for: none when off (the tables go), else one per
    full-text row with a body, embedded only when ``cache_path`` lacks it. A model that cannot be loaded leaves the
    graph without vectors, as off, and says why."""
    if not settings.indexes:
        drop(db)
        return Indexed("off")
    rows = index_rows(db)
    digests = [text_digest(settings.model, text) for *_, text in rows]
    digest = sha256(canonical_json([settings.model, [[r[0], d] for r, d in zip(rows, digests, strict=True)]]))
    held_info = info(db)
    if held_info is not None and held_info.digest == digest:  # the graph holds these vectors already
        db.execute("UPDATE node_vec_meta SET value = ? WHERE key = 'mode'", (settings.mode,))
        db.commit()
        return Indexed(settings.mode, len(rows), 0, digest)
    cache = Cache(cache_path) if cache_path is not None else None
    try:
        np = _numpy()
        held = cache.get(settings.model, sorted(set(digests))) if cache else {}
        texts = {d: t for d, (*_, t) in zip(digests, rows, strict=True) if d not in held}
        todo = sorted(texts.items(), key=lambda x: (len(x[1]), x[0]))
        model = embedder(settings.model, download=download) if todo else None
        dim = model.dim if model else None
        done = 0
        for start in range(0, len(todo), BATCH * 8):  # by length, so a batch pads little; kept as it goes
            part = todo[start : start + BATCH * 8]
            vectors = model.embed([t for _, t in part])
            fresh = [(d, np.asarray(v, dtype=np.float16).tobytes()) for (d, _), v in zip(part, vectors, strict=True)]
            held.update(fresh)
            if cache:
                cache.put(settings.model, fresh)
            done += len(part)
            if progress:
                progress(done, len(todo))
        if cache:
            cache.keep(settings.model, set(digests))
    except SemanticUnavailable as exc:
        drop(db)
        return Indexed(settings.mode, error=str(exc))
    finally:
        if cache:
            cache.close()
    if dim is None:  # every vector came from the cache
        dim = KNOWN_MODELS.get(settings.model, {}).get("dim") or (len(next(iter(held.values()))) // 2 if held else 0)
    drop(db)
    db.executescript(VEC_SCHEMA)
    db.executemany(
        "INSERT INTO node_vec VALUES (?, ?, ?, ?, ?, ?)",
        [(key, nid, kind, aud, d, held[d]) for (key, nid, kind, aud, _), d in zip(rows, digests, strict=True)],
    )
    meta = {"model": settings.model, "dim": str(dim), "mode": settings.mode, "digest": digest}
    db.executemany("INSERT INTO node_vec_meta VALUES (?, ?)", sorted(meta.items()))
    db.commit()
    return Indexed(settings.mode, len(rows), len(todo), digest)


def index_file(
    path: Path, settings: Settings, cache_path: Path | None = None, *, leave_wal: bool = False, **kw
) -> Indexed:
    """``index`` on the graph file at ``path``, the cache next to it by default; ``leave_wal`` puts the file out of
    WAL mode again after, as the hub keeps its graphs."""
    db = sqlite3.connect(path)
    try:
        found = index(db, settings, cache_path or path.with_name(CACHE_FILE), **kw)
        if leave_wal:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("PRAGMA journal_mode=DELETE")
        return found
    finally:
        db.close()


# -- reading them -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Info:
    model: str
    dim: int
    mode: str
    digest: str


def info(db: sqlite3.Connection) -> Info | None:
    """What the graph's vectors are, None when it has none."""
    if not has_vectors(db):
        return None
    meta = dict(db.execute("SELECT key, value FROM node_vec_meta").fetchall())
    try:
        return Info(meta["model"], int(meta["dim"]), meta["mode"], meta["digest"])
    except (KeyError, ValueError):
        return None


@dataclass
class Matrix:
    node_ids: list[str]
    kinds: list[str]
    auds: list[str | None]
    vectors: object  # numpy array, rows x dim, float32


_MATRICES: dict[tuple, Matrix] = {}
MAX_MATRICES = 4


def matrix(db: sqlite3.Connection, found: Info, key: tuple | None = None) -> Matrix:
    """The graph's vectors as one matrix, kept for the process under ``key`` (the file and its digest) when given: a
    hub call opens the graph again each time."""
    np = _numpy()
    if key is not None and key in _MATRICES:
        return _MATRICES[key]
    rows = db.execute("SELECT node_id, kind, aud, vec FROM node_vec ORDER BY key").fetchall()
    vectors = np.frombuffer(b"".join(r[3] for r in rows), dtype=np.float16).reshape(len(rows), found.dim)
    out = Matrix([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows], vectors.astype(np.float32))
    if key is not None:
        with _LOCK:
            while len(_MATRICES) >= MAX_MATRICES:
                _MATRICES.pop(next(iter(_MATRICES)))
            _MATRICES[key] = out
    return out


def nearest(
    m: Matrix, vector, limit: int, kinds: Sequence[str], seen: Callable[[str], bool]
) -> list[tuple[str, float]]:
    """(node id, cosine) of the ``limit`` nodes nearest ``vector``, a node at its best row, of ``kinds`` when given,
    rows with an audience only when ``seen`` says the reader may read it."""
    np = _numpy()
    if not m.node_ids:
        return []
    q = np.asarray(vector, dtype=np.float32)
    scores = m.vectors @ q
    order = np.argsort(-scores, kind="stable")
    wanted = set(kinds or ())
    seen_aud: dict[str, bool] = {}
    out: dict[str, float] = {}
    for i in order:
        if len(out) >= limit:
            break
        nid = m.node_ids[i]
        if nid in out or (wanted and m.kinds[i] not in wanted):
            continue
        aud = m.auds[i]
        if aud is not None:
            if aud not in seen_aud:
                seen_aud[aud] = seen(aud)
            if not seen_aud[aud]:
                continue
        out[nid] = float(scores[i])
    return list(out.items())


def fuse(lists: Sequence[tuple[float, list[str]]], k: float = RRF_K) -> list[str]:
    """Reciprocal rank fusion of ranked lists of ids, each with its weight: by the sum of weight / (k + rank), rank
    from 1; ties by the first list's order, then by id."""
    scores: dict[str, float] = {}
    first: dict[str, int] = {}
    for n, (weight, ids) in enumerate(lists):
        for rank, nid in enumerate(ids, start=1):
            scores[nid] = scores.get(nid, 0.0) + weight / (k + rank)
            if n == 0:
                first.setdefault(nid, rank)
    return sorted(scores, key=lambda nid: (-scores[nid], first.get(nid, 1 << 30), nid))


def query_vector(store, query: str, want: bool | None):
    """The vector of ``query`` for ``store``'s graph, or None to search by words: when the graph has no vectors,
    ``want`` is False, or None and the graph's mode is not on, or the model is not here."""
    found = store.vector_info()
    if found is None or want is False or (want is None and found.mode != "on") or not query.strip():
        return None
    try:
        return embedder(found.model).query(query_text(query))
    except SemanticUnavailable:
        return None
