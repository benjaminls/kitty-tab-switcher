#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import io
import os
import subprocess
import sys
from typing import Optional

from preview_capture import get_text_from_kitty
from preview_gen import parse_ansi_lines


def _load_font(size: int, font_path: Optional[str] = None):
    try:
        from PIL import ImageFont  # type: ignore
    except Exception:
        return None
    if font_path:
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            pass
    for name in ("Menlo.ttc", "Monaco.ttf", "SFMono-Regular.otf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def render_image(lines: list[str], cols: int, rows: int, font_size: int, font_path: Optional[str] = None) -> bytes:
    try:
        from PIL import Image, ImageDraw  # type: ignore
    except Exception as exc:
        raise RuntimeError("Pillow is required to render images") from exc

    cells = parse_ansi_lines(lines)
    if rows <= 0:
        rows = len(cells)
    if cols <= 0:
        cols = max((len(row) for row in cells), default=0)
    rows = min(rows, len(cells)) if cells else 0
    cols = min(cols, max((len(row) for row in cells), default=0)) if cols else 0
    if rows <= 0 or cols <= 0:
        rows = max(1, rows)
        cols = max(1, cols)

    font = _load_font(font_size, font_path=font_path)
    # Estimate cell size
    if font is not None:
        cell_w = int(font_size * 0.6)
        cell_h = int(font_size * 1.2)
    else:
        cell_w = 8
        cell_h = 16

    img = Image.new("RGB", (cols * cell_w, rows * cell_h), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    for r in range(rows):
        if r >= len(cells):
            break
        row = cells[r]
        for c in range(cols):
            if c >= len(row):
                break
            ch, fg, bg = row[c]
            x = c * cell_w
            y = r * cell_h
            if bg is not None:
                draw.rectangle([x, y, x + cell_w, y + cell_h], fill=bg)
            if not ch:
                continue
            if fg is None:
                fg = (230, 230, 230)
            draw.text((x, y), ch, fill=fg, font=font)

    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _icat_inline(png_bytes: bytes) -> None:
    p = subprocess.Popen(["kitty", "+kitten", "icat", "--stdin"], stdin=subprocess.PIPE)
    if p.stdin is not None:
        p.stdin.write(png_bytes)
        p.stdin.close()
    p.wait()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Render a colored image preview from kitty text.")
    parser.add_argument("--window-id", type=int, default=None)
    parser.add_argument("--listen-on", type=str, default=None)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--cols", type=int, default=0)
    parser.add_argument("--rows", type=int, default=0)
    parser.add_argument("--font-size", type=int, default=12)
    parser.add_argument("--font", type=str, default=None)
    parser.add_argument("--out", type=str, default=None)
    parser.add_argument("--inline", action="store_true")
    args = parser.parse_args(argv)

    listen_on = args.listen_on or os.environ.get("KITTY_LISTEN_ON")
    code, text = get_text_from_kitty(args.window_id, listen_on, ansi=True, timeout=args.timeout)
    if code != 0:
        sys.stderr.write(text + "\n")
        return code

    font_path = args.font or os.environ.get("KTS_PREVIEW_FONT")
    png_bytes = render_image(text.splitlines(), args.cols, args.rows, args.font_size, font_path=font_path)
    if args.inline:
        _icat_inline(png_bytes)
        return 0
    if args.out:
        with open(args.out, "wb") as f:
            f.write(png_bytes)
        return 0
    # default: emit base64 PNG to stdout
    sys.stdout.write(base64.b64encode(png_bytes).decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
