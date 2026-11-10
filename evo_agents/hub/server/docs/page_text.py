"""The text of an HTML version of a document, for the project's knowledge graph (``evo_agents.hub.server.docs.kg``).

The graph reads a document as Markdown text with a fragment per heading. An HTML page gives it the text a reader sees
without running a script: every text node outside ``<script>``, ``<style>``, ``<template>``, ``<noscript>`` and
``<head>`` (which ``<body>`` ends even when it is not closed), the text of an inline SVG included, one block element
per paragraph, and each ``<h1>`` to ``<h6>`` as a Markdown heading of its level, so ``split_markdown`` cuts the page
where its headings are. Text a script writes when the page runs is not read:
a page that wants the graph to find a phrase keeps it in its markup. Standard library only.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

SKIPPED = frozenset({"script", "style", "template", "head", "noscript"})
BLOCKS = frozenset(
    {
        "address", "article", "aside", "blockquote", "br", "dd", "details", "div", "dl", "dt", "figcaption", "figure",
        "footer", "form", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section", "summary", "table", "tbody",
        "td", "tfoot", "th", "thead", "tr", "ul", "text", "title", "g",
    }
)  # fmt: skip
HEADINGS = {f"h{level}": level for level in range(1, 7)}
SPACES = re.compile(r"\s+")  # Unicode whitespace, a no-break space among it


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skipping: list[str] = []
        self.blocks: list[str] = []
        self.current: list[str] = []
        self.heading: int | None = None

    def _close(self) -> None:
        text = SPACES.sub(" ", "".join(self.current)).strip()
        self.current = []
        if not text:
            return
        if self.heading is not None:
            text = f"{'#' * self.heading} {text}"
        self.blocks.append(text)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "body":
            self.skipping = [skipped for skipped in self.skipping if skipped != "head"]
        if tag in SKIPPED:
            self.skipping.append(tag)
            return
        if self.skipping:
            return
        if tag in HEADINGS:
            self._close()
            self.heading = HEADINGS[tag]
        elif tag in BLOCKS:
            self._close()

    def handle_endtag(self, tag: str) -> None:
        if self.skipping:
            if tag == self.skipping[-1]:
                self.skipping.pop()
            return
        if tag in HEADINGS:
            self._close()
            self.heading = None
        elif tag in BLOCKS:
            self._close()

    def handle_startendtag(self, tag: str, attrs) -> None:
        if not self.skipping and tag in BLOCKS:
            self._close()

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.current.append(data)


def page_text(html: str) -> str:
    """The text of HTML page ``html`` as Markdown (see the module); empty when it shows none without a script."""
    parser = _Text()
    parser.feed(html)
    parser.close()
    parser._close()
    return "\n\n".join(parser.blocks)


__all__ = ["page_text"]
