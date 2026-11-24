"""The CSR indexes of a snapshot's partitions (``evo_agents.kg.snapshot.Partition``), made with numpy: one sort of
each partition's edges by (node, other end, edge) instead of a sort of a Python tuple per edge. Without numpy
``index`` answers None and the snapshot makes them in pure Python; both give the same arrays."""

from __future__ import annotations

from array import array

try:
    import numpy as _np
except ImportError:  # the snapshot then makes its indexes in pure Python
    _np = None


def _array(values) -> array:
    """``values`` (a numpy array of integers below 2**32) as an ``array('I')``."""
    assert _np is not None
    out = array("I")
    out.frombytes(values.astype(_np.uint32).tobytes())
    return out


def _side(count: int, node, other, edges) -> tuple[array, array]:
    """(start, edges) of the CSR index of the edges ``edges`` by their end ``node``, each node's by ``other`` end,
    then by edge number."""
    assert _np is not None
    order = _np.lexsort((edges, other, node))
    start = _np.zeros(count + 1, dtype=_np.int64)
    _np.cumsum(_np.bincount(node, minlength=count), out=start[1:])
    return _array(start), _array(edges[order])


def index(count: int, src: array, dst: array, groups) -> dict | None:
    """Per (relation, status) of ``groups`` (its edge numbers), (out_start, out_edges, in_start, in_edges) over
    ``count`` nodes, as ``snapshot.partitions_of`` makes them; None without numpy."""
    if _np is None:
        return None
    s = _np.frombuffer(src, dtype=_np.uint32).astype(_np.int64) if len(src) else _np.zeros(0, dtype=_np.int64)
    d = _np.frombuffer(dst, dtype=_np.uint32).astype(_np.int64) if len(dst) else _np.zeros(0, dtype=_np.int64)
    out = {}
    for key, edges in sorted(groups.items()):
        e = _np.asarray(edges, dtype=_np.int64)
        out[key] = (*_side(count, s[e], d[e], e), *_side(count, d[e], s[e], e))
    return out
