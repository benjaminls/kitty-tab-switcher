#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import unicodedata
from typing import Iterable, Optional


FILL_THRESHOLD = 0.2
COLOR_THRESHOLD = 0.35
COLOR_TOTAL_THRESHOLD = 0.0
COLOR_MODES = ("none", "fg", "bg", "both")


def downsample_mask(lines: list[str], cols: int, rows: int) -> list[list[bool]]:
    if rows <= 0 or cols <= 0:
        return []
    if not lines:
        return [[False for _ in range(cols)] for _ in range(rows)]
    max_len = max(len(line) for line in lines)
    max_len = max(max_len, 1)
    out: list[list[bool]] = []
    src_rows = len(lines)
    for r in range(rows):
        r0 = int(r * src_rows / rows)
        r1 = int((r + 1) * src_rows / rows)
        if r1 <= r0:
            r1 = min(src_rows, r0 + 1)
        row_bits: list[bool] = []
        for c in range(cols):
            c0 = int(c * max_len / cols)
            c1 = int((c + 1) * max_len / cols)
            if c1 <= c0:
                c1 = min(max_len, c0 + 1)
            filled = 0
            total = 0
            for sr in range(r0, r1):
                line = lines[sr].ljust(max_len)
                for sc in range(c0, c1):
                    total += 1
                    if sc < len(line) and line[sc] != " ":
                        filled += 1
            row_bits.append(filled > 0 and filled / max(total, 1) >= FILL_THRESHOLD)
        out.append(row_bits)
    return out


def _ansi_8_color(code: int) -> Optional[tuple[int, int, int]]:
    base = {
        30: (0, 0, 0),
        31: (205, 49, 49),
        32: (13, 188, 121),
        33: (229, 229, 16),
        34: (36, 114, 200),
        35: (188, 63, 188),
        36: (17, 168, 205),
        37: (229, 229, 229),
        90: (102, 102, 102),
        91: (241, 76, 76),
        92: (35, 209, 139),
        93: (245, 245, 67),
        94: (59, 142, 234),
        95: (214, 112, 214),
        96: (41, 184, 219),
        97: (229, 229, 229),
    }
    return base.get(code)


def _ansi_256_color(code: int) -> Optional[tuple[int, int, int]]:
    if code < 0 or code > 255:
        return None
    if code < 16:
        # map to basic colors
        return _ansi_8_color(30 + code) or _ansi_8_color(90 + (code - 8))
    if 16 <= code <= 231:
        code -= 16
        r = (code // 36) % 6
        g = (code // 6) % 6
        b = code % 6
        def level(n: int) -> int:
            return 0 if n == 0 else 55 + n * 40
        return (level(r), level(g), level(b))
    # grayscale 232-255
    gray = 8 + (code - 232) * 10
    return (gray, gray, gray)


def _rgb_from_8bit(code: int) -> Optional[tuple[int, int, int]]:
    return _ansi_8_color(code)


def parse_ansi_lines(lines: list[str]) -> list[list[tuple[str, Optional[tuple[int, int, int]], Optional[tuple[int, int, int]]]]]:
    out: list[list[tuple[str, Optional[tuple[int, int, int]], Optional[tuple[int, int, int]]]]] = []
    cur: list[tuple[str, Optional[tuple[int, int, int]], Optional[tuple[int, int, int]]]] = []
    fg: Optional[tuple[int, int, int]] = None
    bg: Optional[tuple[int, int, int]] = None
    for line in lines:
        i = 0
        # Reset color at the start of each new line to avoid bleed.
        fg = None
        bg = None
        col = 0
        while i < len(line):
            ch = line[i]
            if ch == "\x1b" and i + 1 < len(line) and line[i + 1] == "]":
                # OSC sequence (e.g., hyperlinks). Skip until BEL or ST.
                end_bel = line.find("\x07", i + 2)
                end_st = line.find("\x1b\\", i + 2)
                if end_bel == -1 and end_st == -1:
                    break
                if end_bel == -1:
                    i = end_st + 2
                elif end_st == -1:
                    i = end_bel + 1
                else:
                    i = min(end_bel + 1, end_st + 2)
                continue
            if ch == "\x1b" and i + 1 < len(line) and line[i + 1] == "[":
                end = line.find("m", i + 2)
                if end == -1:
                    i += 1
                    continue
                seq = line[i + 2 : end]
                if seq == "":
                    fg = None
                    bg = None
                    i = end + 1
                    continue
                raw_parts = [p for p in seq.split(";") if p != ""]
                parts: list[str] = []
                for part in raw_parts:
                    parts.extend([p for p in part.split(":") if p != ""])
                if not parts:
                    fg = None
                    bg = None
                    i = end + 1
                    continue
                j = 0
                while j < len(parts):
                    code = parts[j]
                    if code == "0":
                        fg = None
                        bg = None
                    elif code == "39":
                        fg = None
                    elif code == "49":
                        bg = None
                        bg = None
                    elif code == "38":
                        if j + 1 < len(parts) and parts[j + 1] == "2" and j + 4 < len(parts):
                            try:
                                r = int(parts[j + 2])
                                g = int(parts[j + 3])
                                b = int(parts[j + 4])
                                fg = (r, g, b)
                            except Exception:
                                pass
                            j += 4
                        elif j + 1 < len(parts) and parts[j + 1] == "5" and j + 2 < len(parts):
                            try:
                                idx = int(parts[j + 2])
                                fg = _ansi_256_color(idx)
                            except Exception:
                                pass
                            j += 2
                    elif code == "48":
                        if j + 1 < len(parts) and parts[j + 1] == "2" and j + 4 < len(parts):
                            try:
                                r = int(parts[j + 2])
                                g = int(parts[j + 3])
                                b = int(parts[j + 4])
                                bg = (r, g, b)
                            except Exception:
                                pass
                            j += 4
                        elif j + 1 < len(parts) and parts[j + 1] == "5" and j + 2 < len(parts):
                            try:
                                idx = int(parts[j + 2])
                                bg = _ansi_256_color(idx)
                            except Exception:
                                pass
                            j += 2
                    elif code.isdigit():
                        cval = int(code)
                        if 30 <= cval <= 37 or 90 <= cval <= 97:
                            fg = _ansi_8_color(cval)
                        elif 40 <= cval <= 47 or 100 <= cval <= 107:
                            bg = _ansi_8_color(cval - 10)
                        elif 0 <= cval <= 7:
                            # Some terminals emit 0-7 instead of 30-37
                            fg = _rgb_from_8bit(30 + cval)
                    j += 1
                i = end + 1
                continue
            if ch == "\t":
                # Expand tabs to 8-column boundaries.
                spaces = 8 - (col % 8)
                for _ in range(spaces):
                    cur.append((" ", fg, bg))
                col += spaces
                i += 1
                continue
            if ch == "\r":
                col = 0
                i += 1
                continue
            is_private = _is_private_use(ch)
            width = _char_width(ch)
            if width == 0:
                i += 1
                continue
            if is_private:
                # Ignore glyphs from private use; preserve background only to avoid color splotches.
                fg = None
                ch = " "
            cur.append((ch, fg, bg))
            if width == 2:
                # pad the second cell for wide glyphs
                cur.append(("", fg, bg))
                col += 2
            else:
                col += 1
            i += 1
        out.append(cur)
        cur = []
    return out


def _char_width(ch: str) -> int:
    if not ch:
        return 0
    if ch == "\t":
        return 4
    if unicodedata.combining(ch):
        return 0
    # Symbols used by powerline/nerd fonts are typically wide in terminals.
    name = unicodedata.name(ch, "")
    if "POWERLINE" in name:
        return 2
    code = ord(ch)
    # Common private use areas for Nerd Font glyphs; treat as wide.
    if (0xE000 <= code <= 0xF8FF) or (0xF0000 <= code <= 0xFFFFD) or (0x100000 <= code <= 0x10FFFD):
        return 2
    eaw = unicodedata.east_asian_width(ch)
    if eaw in ("W", "F"):
        return 2
    # control chars
    if ord(ch) < 32 or ord(ch) == 127:
        return 0
    return 1


def _is_private_use(ch: str) -> bool:
    if not ch:
        return False
    code = ord(ch)
    return (0xE000 <= code <= 0xF8FF) or (0xF0000 <= code <= 0xFFFFD) or (0x100000 <= code <= 0x10FFFD)


def _content_row_count_cells(lines: list[list[tuple[str, Optional[tuple[int, int, int]], Optional[tuple[int, int, int]]]]]) -> int:
    for idx in range(len(lines) - 1, -1, -1):
        row = lines[idx]
        if any(ch.strip() for ch, _, _ in row):
            return idx + 1
    return min(len(lines), 1)


def _downsample_color(
    lines: list[list[tuple[str, Optional[tuple[int, int, int]], Optional[tuple[int, int, int]]]]],
    cols: int,
    rows: int,
) -> list[list[tuple[bool, Optional[tuple[int, int, int]]]]]:
    if rows <= 0 or cols <= 0:
        return []
    if not lines:
        return [[(False, None) for _ in range(cols)] for _ in range(rows)]
    max_len = max(len(line) for line in lines)
    max_len = max(max_len, 1)
    out: list[list[tuple[bool, Optional[tuple[int, int, int]], Optional[tuple[int, int, int]]]]] = []
    src_rows = len(lines)
    for r in range(rows):
        r0 = int(r * src_rows / rows)
        r1 = int((r + 1) * src_rows / rows)
        if r1 <= r0:
            r1 = min(src_rows, r0 + 1)
        row_bits: list[tuple[bool, Optional[tuple[int, int, int]], Optional[tuple[int, int, int]]]] = []
        for c in range(cols):
            c0 = int(c * max_len / cols)
            c1 = int((c + 1) * max_len / cols)
            if c1 <= c0:
                c1 = min(max_len, c0 + 1)
            filled = 0
            total = 0
            fg_counts: dict[tuple[int, int, int], int] = {}
            bg_counts: dict[tuple[int, int, int], int] = {}
            colored = 0
            bg_filled = 0
            for sr in range(r0, r1):
                line = lines[sr]
                for sc in range(c0, c1):
                    total += 1
                    if sc < len(line):
                        ch, fg, bg = line[sc]
                    else:
                        ch, fg, bg = " ", None, None
                    if ch != " ":
                        filled += 1
                        if fg is not None:
                            fg_counts[fg] = fg_counts.get(fg, 0) + 1
                            colored += 1
                    if bg is not None:
                        bg_filled += 1
                        bg_counts[bg] = bg_counts.get(bg, 0) + 1
            if (filled > 0 and filled / max(total, 1) >= FILL_THRESHOLD) or (bg_filled > 0):
                fg_color = None
                bg_color = None
                if fg_counts and (colored / max(filled, 1)) >= COLOR_THRESHOLD and (colored / max(total, 1)) >= COLOR_TOTAL_THRESHOLD:
                    fg_color = max(fg_counts.items(), key=lambda kv: kv[1])[0]
                if bg_counts:
                    bg_color = max(bg_counts.items(), key=lambda kv: kv[1])[0]
                row_bits.append((True, fg_color, bg_color))
            else:
                row_bits.append((False, None, None))
        out.append(row_bits)
    return out


def _color_to_sgr(color: Optional[tuple[int, int, int]]) -> str:
    if color is None:
        return "\x1b[0m"
    r, g, b = color
    return f"\x1b[48;2;{r};{g};{b}m"


def render_block_preview(lines: list[str], cols: int, rows: int, color_mode: str = "both") -> list[str]:
    # Use two rows of the mask per output row, rendered with block chars.
    if rows <= 0 or cols <= 0:
        return []
    if color_mode not in COLOR_MODES:
        color_mode = "both"
    has_ansi = any("\x1b[" in line for line in lines)
    if has_ansi:
        ansi_lines = parse_ansi_lines(lines)
        content_rows = _content_row_count_cells(ansi_lines)
        effective_rows = min(content_rows, rows * 2)
        mask = _downsample_color(ansi_lines[:content_rows], cols, effective_rows)
        out: list[str] = []
        for r in range(rows):
            upper = mask[r * 2] if r * 2 < len(mask) else [(False, None, None)] * cols
            lower = mask[r * 2 + 1] if r * 2 + 1 < len(mask) else [(False, None, None)] * cols
            row_chars: list[str] = []
            for c in range(cols):
                up, upf, upb = upper[c]
                lo, lof, lob = lower[c]
                if up and lo:
                    ch = "█"
                    fg = upf or lof
                    bg = upb or lob
                    if fg is None and bg is not None:
                        fg = bg
                        bg = None
                elif up:
                    ch = "▀"
                    fg = upf
                    bg = upb
                    if fg is None and bg is not None:
                        fg = bg
                        bg = None
                elif lo:
                    ch = "▄"
                    fg = lof
                    bg = lob
                    if fg is None and bg is not None:
                        fg = bg
                        bg = None
                else:
                    ch = " "
                    fg = None
                    bg = None
                if color_mode != "none":
                    seq = ""
                    if color_mode in ("fg", "both") and fg is not None:
                        seq += f"\x1b[38;2;{fg[0]};{fg[1]};{fg[2]}m"
                    if color_mode in ("bg", "both") and bg is not None:
                        seq += f"\x1b[48;2;{bg[0]};{bg[1]};{bg[2]}m"
                    if seq:
                        row_chars.append(seq)
                        row_chars.append(ch)
                        row_chars.append("\x1b[0m")
                    else:
                        row_chars.append("\x1b[0m")
                        row_chars.append(ch)
                else:
                    row_chars.append("\x1b[0m")
                    row_chars.append(ch)
            out.append("".join(row_chars))
        return out

    content_rows = 0
    for idx in range(len(lines) - 1, -1, -1):
        if lines[idx].strip():
            content_rows = idx + 1
            break
    if content_rows == 0:
        content_rows = min(len(lines), 1)
    effective_rows = min(content_rows, rows * 2)
    mask = downsample_mask(lines[:content_rows], cols, effective_rows)
    out: list[str] = []
    for r in range(rows):
        upper = mask[r * 2] if r * 2 < len(mask) else [False] * cols
        lower = mask[r * 2 + 1] if r * 2 + 1 < len(mask) else [False] * cols
        row_chars: list[str] = []
        for c in range(cols):
            up = upper[c]
            lo = lower[c]
            if up and lo:
                row_chars.append("█")
            elif up:
                row_chars.append("▀")
            elif lo:
                row_chars.append("▄")
            else:
                row_chars.append(" ")
        out.append("".join(row_chars))
    return out


def _read_lines(source: str | None) -> list[str]:
    if source:
        with open(source, "r", encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    return sys.stdin.read().splitlines()


def _write_lines(lines: Iterable[str]) -> None:
    for line in lines:
        sys.stdout.write(line + "\n")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Generate a block preview from text.")
    parser.add_argument("--cols", type=int, required=True)
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--file", type=str, default=None)
    parser.add_argument("--ansi", action="store_true")
    parser.add_argument("--color-mode", type=str, default="both", choices=COLOR_MODES)
    args = parser.parse_args(argv)

    lines = _read_lines(args.file)
    if args.ansi:
        # pass through, render_block_preview will parse
        pass
    preview = render_block_preview(lines, args.cols, args.rows, color_mode=args.color_mode)
    _write_lines(preview)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
