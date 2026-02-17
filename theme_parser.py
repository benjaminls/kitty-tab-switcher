#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class BorderStyle:
    h: str = "-"
    v: str = "|"
    tl: str = "+"
    tr: str = "+"
    bl: str = "+"
    br: str = "+"


@dataclass
class Theme:
    name: str = "default"
    border: BorderStyle = field(default_factory=BorderStyle)
    border_selected: BorderStyle = field(
        default_factory=lambda: BorderStyle(h="=", v="#", tl="+", tr="+", bl="+", br="+")
    )
    title_align: str = "left"  # left|center|right
    title_padding: int = 1
    title_invert_selected: bool = True
    wrap_title: str = "truncate"  # truncate|clip
    wrap_preview: str = "truncate"  # truncate|clip
    ellipsis: str = "..."
    align: str = "center"  # left|center|right
    vertical_align: str = "center"  # top|center|bottom
    gap: int = 2
    preview_rows: int = 12
    min_preview_rows: int = 6
    min_preview_cols: int = 24
    card_min_width: int = 7
    card_min_height: int = 3
    preview_color_mode: str = "both"  # none|fg|bg|both


def _coerce_value(value: str) -> Any:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in {"'", '"'}:
        # strip surrounding quotes for the minimal parser
        v = v[1:-1]
    if v.lower() in {"true", "false"}:
        return v.lower() == "true"
    try:
        return int(v)
    except Exception:
        pass
    return v


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(0, root)]
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        while stack and indent < stack[-1][0]:
            stack.pop()
        if not stack:
            stack = [(0, root)]
        current = stack[-1][1]
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        key = key.strip()
        rest = rest.strip()
        if rest == "":
            child: dict[str, Any] = {}
            current[key] = child
            stack.append((indent + 2, child))
        else:
            current[key] = _coerce_value(rest)
    return root


def _load_yaml_data(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = f.read()
    try:
        import yaml  # type: ignore
    except Exception:
        return _parse_simple_yaml(data)
    loaded = yaml.safe_load(data)
    return loaded if isinstance(loaded, dict) else {}


def _merge_border(base: BorderStyle, data: dict[str, Any]) -> BorderStyle:
    return BorderStyle(
        h=str(data.get("h", base.h)),
        v=str(data.get("v", base.v)),
        tl=str(data.get("tl", base.tl)),
        tr=str(data.get("tr", base.tr)),
        bl=str(data.get("bl", base.bl)),
        br=str(data.get("br", base.br)),
    )


def load_theme(path: Optional[str]) -> Theme:
    theme = Theme()
    if not path:
        return theme
    data = _load_yaml_data(path)
    if not isinstance(data, dict):
        return theme

    theme.name = str(data.get("name", theme.name))

    border = data.get("border", {})
    if isinstance(border, dict):
        normal = border.get("normal", border.get("default", {}))
        selected = border.get("selected", {})
        if isinstance(normal, dict):
            theme.border = _merge_border(theme.border, normal)
        if isinstance(selected, dict):
            theme.border_selected = _merge_border(theme.border_selected, selected)

    text = data.get("text", {})
    if isinstance(text, dict):
        theme.title_align = str(text.get("title_align", theme.title_align))
        theme.title_padding = int(text.get("title_padding", theme.title_padding))
        theme.title_invert_selected = bool(text.get("title_invert_selected", theme.title_invert_selected))

    wrap = data.get("wrap", {})
    if isinstance(wrap, dict):
        theme.wrap_title = str(wrap.get("title", theme.wrap_title))
        theme.wrap_preview = str(wrap.get("preview", theme.wrap_preview))
        theme.ellipsis = str(wrap.get("ellipsis", theme.ellipsis))

    layout = data.get("layout", {})
    if isinstance(layout, dict):
        theme.align = str(layout.get("align", theme.align))
        theme.vertical_align = str(layout.get("vertical_align", theme.vertical_align))
        theme.gap = int(layout.get("gap", theme.gap))

    size = data.get("size", {})
    if isinstance(size, dict):
        theme.preview_rows = int(size.get("preview_rows", theme.preview_rows))
        theme.min_preview_rows = int(size.get("min_preview_rows", theme.min_preview_rows))
        theme.min_preview_cols = int(size.get("min_preview_cols", theme.min_preview_cols))
        theme.card_min_width = int(size.get("card_min_width", theme.card_min_width))
        theme.card_min_height = int(size.get("card_min_height", theme.card_min_height))

    preview = data.get("preview", {})
    if isinstance(preview, dict):
        theme.preview_color_mode = str(preview.get("color_mode", theme.preview_color_mode))

    return theme
