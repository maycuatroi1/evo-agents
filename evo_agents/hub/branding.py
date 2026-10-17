"""The branding of a hub (criterion a11 of plan hub-agent-plugins): the product's name, a logo and a favicon, and the
colours of the tokens of web/DESIGN.md that a client may set, each for the light and the dark theme.

A field left unset keeps what the web has without branding: its own name, the mark, the favicon, and the colours of
web/DESIGN.md (DEFAULTS, copies of web/src/app/globals.css). Only the four tokens of TOKENS may change, never a raw
colour, and only to colours that keep the contrast web/DESIGN.md asks of them (CHECKS, WCAG 2.2 AA): text at least
4.5:1 on each ground it is drawn on, the selected nav item's icon at least 3:1. ``problems`` checks one write: the
fields it sets, then the colours the branding would have after it, each theme whole (a token left unset counts with
its default), so a brand colour that fails on the default soft ground is refused before it is saved.

The logo and the favicon are images in the blob store (``evo_agents.hub.server.branding``); a write of the fields
sets neither, only the upload route does, and unsetting one goes back to the mark and the favicon of the web.

Standard library only.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping

FIELDS = ("name", "colors", "logo", "favicon")
ASSETS = ("logo", "favicon")  # set by uploading an image, never by a write of the fields
THEMES = ("light", "dark")
TOKENS = ("brand", "brand-hover", "brand-soft", "on-brand")
MAX_NAME = 60
HEX = re.compile(r"#[0-9a-fA-F]{6}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# The tokens CHECKS reads, as web/src/app/globals.css sets them (the Colour section of web/DESIGN.md).
DEFAULTS = {
    "light": {
        "canvas": "#f5f6f8",
        "surface": "#ffffff",
        "surface-selected": "#e9edfd",
        "brand": "#2f4bd8",
        "brand-hover": "#263fba",
        "brand-soft": "#ebeefd",
        "on-brand": "#ffffff",
    },
    "dark": {
        "canvas": "#0a0c10",
        "surface": "#111419",
        "surface-selected": "#1a2246",
        "brand": "#8c9dff",
        "brand-hover": "#a3b0ff",
        "brand-soft": "#19204a",
        "on-brand": "#0a0c10",
    },
}

# (foreground, ground, least ratio, what draws it): the pairs web/DESIGN.md measures for the tokens of TOKENS.
CHECKS = (
    ("brand", "surface", 4.5, "links and the running pill on a card"),
    ("brand", "canvas", 4.5, "links on the page"),
    ("brand", "brand-soft", 4.5, "the running pill and brand tags"),
    ("brand", "surface-selected", 3.0, "the icon of the selected nav item"),
    ("brand-hover", "surface", 4.5, "a link under the pointer"),
    ("on-brand", "brand", 4.5, "glyphs on a brand fill"),
)


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(color: str) -> float:
    """The relative luminance of ``#rrggbb`` (WCAG 2.2)."""
    red, green, blue = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def contrast(first: str, second: str) -> float:
    """The contrast ratio of two ``#rrggbb`` colours, from 1 to 21."""
    lighter, darker = sorted((luminance(first), luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def palette(theme: str, colors: Mapping | None) -> dict[str, str]:
    """The tokens CHECKS reads in ``theme``: the defaults, with what ``colors`` sets for that theme over them."""
    return {**DEFAULTS[theme], **((colors or {}).get(theme) or {})}


def contrast_problems(colors: Mapping | None) -> list[str]:
    """Each pair of CHECKS under its least ratio, in either theme, with the colours and the ratio it has."""
    found = []
    for theme in THEMES:
        tokens = palette(theme, colors)
        for front, ground, least, what in CHECKS:
            ratio = contrast(tokens[front], tokens[ground])
            if ratio < least:
                shown = math.floor(ratio * 100) / 100
                found.append(
                    f"colors.{theme}: {front} {tokens[front]} on {ground} {tokens[ground]} is {shown}:1, under the "
                    f"{least}:1 web/DESIGN.md asks for {what}"
                )
    return found


def _color_problems(colors) -> list[str]:
    if not isinstance(colors, Mapping):
        return ["colors: an object of light and dark, each of tokens to #rrggbb colours"]
    found = [
        f"colors.{theme}: not a theme; the themes are {', '.join(THEMES)}" for theme in colors if theme not in THEMES
    ]
    for theme in THEMES:
        tokens = colors.get(theme, {})
        if not isinstance(tokens, Mapping):
            found.append(f"colors.{theme}: an object of tokens to #rrggbb colours")
            continue
        for token, value in tokens.items():
            if token not in TOKENS:
                found.append(f"colors.{theme}.{token}: not a token a hub may set; those are {', '.join(TOKENS)}")
            elif not isinstance(value, str) or not HEX.fullmatch(value):
                found.append(f"colors.{theme}.{token}: a colour as #rrggbb")
    return found


def _name_problems(name) -> list[str]:
    if not isinstance(name, str) or not name.strip():
        return ["name: the product's name, a string that is not blank"]
    if len(name) > MAX_NAME or _CONTROL.search(name) or name != name.strip():
        return [f"name: at most {MAX_NAME} characters, no control character and no space at either end"]
    return []


def normalized(values: Mapping) -> dict:
    """``values`` with each colour in lower case, as the hub keeps it."""
    colors = values.get("colors")
    if not isinstance(colors, Mapping) or _color_problems(colors):
        return dict(values)
    lowered = {theme: {token: color.lower() for token, color in tokens.items()} for theme, tokens in colors.items()}
    return {**values, "colors": lowered}


def problems(values: Mapping, after: Mapping) -> list[str]:
    """What is wrong with ``values``, the fields one write sets, then with the colours of ``after``, the branding
    once the write is made: a field that is not one of FIELDS, an image set without its upload, a name or a colour
    that does not fit, and each pair of CHECKS under its ratio."""
    found = []
    for name, value in values.items():
        if name not in FIELDS:
            found.append(f"{name}: not a field of the branding, whose fields are {', '.join(FIELDS)}")
        elif name in ASSETS:
            found.append(f"{name}: an image, set by PUT /v1/admin/branding/{name} with its bytes")
        elif name == "name":
            found.extend(_name_problems(value))
        else:
            found.extend(_color_problems(value))
    if found:
        return found
    return contrast_problems(after.get("colors"))
