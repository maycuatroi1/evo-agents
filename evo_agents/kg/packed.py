"""Values held packed until read: pickled and compressed, a fresh copy at each read.

A build holds the facts of every item between its stages: the map and structure facts until the link stage has
read the index made of them and the facts loop has taken them, the link facts until that loop. As Python objects
(a dict and its strings for each fact) they were most of a build's memory: on a corpus of some 25,000 items, about
1.5 GB of a 3.2 GB peak, against some 50 MB packed. A stage reads each item's facts once or twice, so a read costs
little next to what the build does with them.
"""

from __future__ import annotations

import pickle
import zlib
from collections.abc import Iterator, Mapping

LEVEL = 1  # zlib's fastest: about a tenth of the pickle's size


class Packed(Mapping):
    """A mapping whose values are kept pickled and compressed; each read unpacks a new copy, so a reader that changes
    what it got changes nothing held."""

    def __init__(self) -> None:
        self._held: dict[str, bytes] = {}

    def __setitem__(self, key: str, value) -> None:
        self._held[key] = zlib.compress(pickle.dumps(value, pickle.HIGHEST_PROTOCOL), LEVEL)

    def __getitem__(self, key: str):
        return pickle.loads(zlib.decompress(self._held[key]))

    def __iter__(self) -> Iterator[str]:
        return iter(self._held)

    def __len__(self) -> int:
        return len(self._held)

    def size(self) -> int:
        """The bytes its values take packed."""
        return sum(len(v) for v in self._held.values())
