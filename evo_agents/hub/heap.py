"""Giving back the memory a long job freed: CPython and glibc keep freed pages for the process, so a hub process that
built a graph or loaded a snapshot held that memory on top of what it did next (``kg_build``, ``server.kg_health``)."""

from __future__ import annotations

import ctypes
import ctypes.util
import gc


def give_back() -> None:
    """Give the memory freed since back to the system: collect the cycles, then have glibc's malloc return its free
    pages (malloc_trim), which it keeps otherwise. Nothing more where the C library is not glibc."""
    gc.collect()
    found = ctypes.util.find_library("c")
    if found is None:
        return
    trim = getattr(ctypes.CDLL(found), "malloc_trim", None)
    if trim is not None:
        trim(0)
