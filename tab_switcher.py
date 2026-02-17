#!/usr/bin/env python3
from __future__ import annotations

import json
import ctypes
import os
import select
import sys
import termios
import time
import tty
import traceback
import socket
try:
    from kitty import fast_data_types as _fdt  # type: ignore
except Exception:
    _fdt = None
from typing import Any, Callable, Iterable

from kittens.tui.handler import kitten_ui
from preview_gen import render_block_preview
from preview_capture import get_text_lines_from_rc
from theme_parser import Theme, load_theme


PREVIEW_COLS = 40
PREVIEW_ROWS = 12  # logical rows (will be rendered as block rows)
MIN_PREVIEW_COLS = 24
MIN_PREVIEW_ROWS = 6
MIN_TITLE_COLS = 20
MAX_VISIBLE_CARDS = 7
PREVIEW_NEIGHBORS = 1
PREVIEW_COLOR_MODE = "both"  # none|fg|bg|both
THEME_ENV_VAR = "KTS_THEME"
CACHE_FILENAME = "kitty-tab-switcher.json"
PREVIEW_CACHE_FILENAME = "kitty-tab-switcher-previews.json"
PREVIEW_REFRESH_SECS = 0.5
SWITCHER_TITLE = "KTS_SWITCHER"
DEBUG_ENV = "KTS_DEBUG"
DEBUG_PATH_ENV = "KTS_DEBUG_PATH"
DEFAULT_DEBUG_PATH = os.path.expanduser("~/.cache/kitty-tab-switcher-debug.log")

CSI = "\x1b["
KEYBOARD_MODE_PUSH = f"{CSI}>1u"
KEYBOARD_MODE_POP = f"{CSI}<u"
# Enable disambiguation + report events + report all keys
KEYBOARD_ENHANCE_FLAGS = 1 | 2 | 8
KEYBOARD_MODE_SET = f"{CSI}={KEYBOARD_ENHANCE_FLAGS};1u"

TAB_CODE = 9
ESC_CODE = 27
CTRL_KEY_CODES = {57442, 57448}
MARKER_KEY_CODE = 57387  # f24 from kitty send-key
MARKER_SEQ = "\x1b[9999u"


class TabInfo:
    def __init__(
        self,
        id: int,
        title: str,
        window_id: int,
        is_active: bool,
        last_focused: float | None = None,
    ) -> None:
        self.id = id
        self.title = title
        self.window_id = window_id
        self.is_active = is_active
        self.last_focused = last_focused


class StateStore:
    def __init__(self, os_window_id: int) -> None:
        self.os_window_id = os_window_id
        self.pid = int(os.environ.get("KITTY_PID", "0") or "0")
        cache_dir = os.environ.get(
            "KITTY_CACHE_DIRECTORY",
            os.path.expanduser("~/.cache"),
        )
        self.path = os.path.join(cache_dir, CACHE_FILENAME)

    def load(self) -> dict[int, float]:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return {}
        pid_key = str(self.pid)
        if pid_key not in data:
            return {}
        raw = data.get(pid_key, {}).get(str(self.os_window_id), {})
        if isinstance(raw, list):
            now = time.time()
            out: dict[int, float] = {}
            for idx, tid in enumerate(raw):
                try:
                    out[int(tid)] = now - idx
                except Exception:
                    continue
            return out
        if isinstance(raw, dict):
            out = {}
            for k, v in raw.items():
                try:
                    out[int(k)] = float(v)
                except Exception:
                    continue
            return out
        return {}

    def save(self, last_used: dict[int, float]) -> None:
        data: dict[str, Any] = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        pid_key = str(self.pid)
        osw_key = str(self.os_window_id)
        data.setdefault(pid_key, {})
        data[pid_key][osw_key] = {str(k): float(v) for k, v in last_used.items()}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        try:
            ordered = sorted(last_used.items(), key=lambda kv: kv[1], reverse=True)
            os.environ["KTS_MRU"] = ",".join(str(k) for k, _ in ordered)
        except Exception:
            pass


class PreviewStore:
    def __init__(self, os_window_id: int) -> None:
        self.os_window_id = os_window_id
        self.pid = int(os.environ.get("KITTY_PID", "0") or "0")
        cache_dir = os.environ.get(
            "KITTY_CACHE_DIRECTORY",
            os.path.expanduser("~/.cache"),
        )
        self.path = os.path.join(cache_dir, PREVIEW_CACHE_FILENAME)

    def load(self) -> tuple[dict[int, list[str]], dict[int, float]]:
        if not os.path.exists(self.path):
            return {}, {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return {}, {}
        pid_key = str(self.pid)
        osw_key = str(self.os_window_id)
        raw = data.get(pid_key, {}).get(osw_key, {})
        out: dict[int, list[str]] = {}
        ts_out: dict[int, float] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    tid = int(k)
                except Exception:
                    continue
                if isinstance(v, dict):
                    lines = v.get("lines")
                    ts = v.get("ts")
                else:
                    lines = v
                    ts = None
                if isinstance(lines, list) and all(isinstance(s, str) for s in lines):
                    out[tid] = lines
                    if isinstance(ts, (int, float)):
                        ts_out[tid] = float(ts)
        return out, ts_out

    def save(self, previews: dict[int, list[str]], timestamps: dict[int, float]) -> None:
        data: dict[str, Any] = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        pid_key = str(self.pid)
        osw_key = str(self.os_window_id)
        data.setdefault(pid_key, {})
        payload: dict[str, Any] = {}
        now = time.time()
        for k, v in previews.items():
            ts = timestamps.get(k, now)
            payload[str(k)] = {"lines": v, "ts": ts}
        data[pid_key][osw_key] = payload
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)


class KeyEvent:
    def __init__(self, key_code: int, mods: int = 1, event_type: int = 1) -> None:
        self.key_code = key_code
        self.mods = mods
        self.event_type = event_type

    @property
    def shift(self) -> bool:
        return bool((self.mods - 1) & 1)

    @property
    def ctrl(self) -> bool:
        return bool((self.mods - 1) & 4)


class RawSwitcher:
    def __init__(
        self,
        tabs: list[TabInfo],
        mru: dict[int, float],
        os_window_id: int,
        direction: int,
        remote_control: Callable[..., Any],
        command_server: "CommandServer | None",
        theme: Theme,
    ) -> None:
        self.tabs = tabs
        self.os_window_id = os_window_id
        self.remote_control = remote_control
        self.command_server = command_server
        self.theme = theme
        self.state = StateStore(os_window_id)
        self.preview_state = PreviewStore(os_window_id)
        self.preview_cache: dict[int, list[str]] = {}
        self.preview_cache_ts: dict[int, float] = {}
        cached, cached_ts = self.preview_state.load()
        self.preview_cache.update(cached)
        self.preview_cache_ts.update(cached_ts)
        self.last_used = self._reconcile_mru(mru)
        self.original_tab_id = self._active_tab_id()
        if self.original_tab_id is not None:
            self.last_used[self.original_tab_id] = time.time()
        self.mru_order = [t.id for t in self.tabs]
        self.selected_index = self._initial_index(direction)
        log(
            "init_state",
            direction=direction,
            original=self.original_tab_id,
            active=self._active_tab_id(),
            tabs=[t.id for t in self.tabs],
            mru_order=self.mru_order,
            selected_index=self.selected_index,
            selected_tab=self._current_tab_id(),
        )
        self.last_draw = 0.0
        self.ctrl_down = False
        self.start_time = time.time()
        self.saw_tab_event = False
        self.ctrl_seen = False
        self.mod_query, self.ctrl_mask = resolve_mod_query()
        self.last_input_time = time.time()
        self.poll_ctrl_seen = False
        self.any_key_event = False
        self.ctrl_up_streak = 0
        self.marker_seen = False
        self.marker_sent = False
        self.initial_mods_checked = False
        self.initial_ctrl_down = False

    def run(self) -> None:
        log("switcher.run start", tabs=len(self.tabs), selected=self.selected_index)
        self._send_marker()
        self._check_initial_mods()
        self.draw()
        self._schedule_preview_fetch()
        fd = sys.stdin.fileno()
        while True:
            rlist = [fd]
            if self.command_server is not None:
                rlist.append(self.command_server.fileno())
            r, _, _ = select.select(rlist, [], [], 0.05)
            if not r:
                if self._drain_preview_queue():
                    self.draw()
                if self.mod_query is not None and self.ctrl_mask is not None:
                    mods = self.mod_query()
                    log("mods_poll", mods=mods)
                    if mods is not None:
                        if ctrl_is_down(mods, self.ctrl_mask):
                            self.poll_ctrl_seen = True
                            self.ctrl_down = True
                            self.ctrl_up_streak = 0
                        else:
                            self.ctrl_up_streak += 1
                            if self.poll_ctrl_seen and self.ctrl_up_streak >= 2:
                                log("ctrl_poll_commit", seen=self.poll_ctrl_seen, streak=self.ctrl_up_streak)
                                self.commit()
                                return
                if (self.marker_seen and not self.ctrl_down and not self.saw_tab_event
                        and (time.time() - self.start_time) > 0.15):
                    log("marker_no_ctrl_timeout_commit", elapsed=time.time() - self.start_time)
                    self.commit()
                    return
                # If we know ctrl was not down at launch and nothing else happened, close quickly.
                if (not self.marker_sent
                        and self.initial_mods_checked and not self.initial_ctrl_down and not self.saw_tab_event
                        and not self.marker_seen and not self.ctrl_seen
                        and (time.time() - self.start_time) > 0.08):
                    log("initial_ctrl_up_commit", elapsed=time.time() - self.start_time)
                    self.commit()
                    return
                if (self.marker_sent and not self.any_key_event and not self.marker_seen
                        and (time.time() - self.start_time) > 0.2):
                    log("no_marker_timeout_commit", elapsed=time.time() - self.start_time)
                    self.commit()
                    return
                # Fallback: if we never see a tab event, don't let the UI stick.
                if (self.any_key_event and not self.saw_tab_event and not self.ctrl_down
                        and (time.time() - self.start_time) > 0.2):
                    log("no_tab_timeout_commit", elapsed=time.time() - self.start_time, ctrl_down=self.ctrl_down)
                    self.commit()
                    return
            if self.command_server is not None and self.command_server.fileno() in r:
                cmd = self.command_server.recv()
                if cmd in {"next", "prev"}:
                    self._move(-1 if cmd == "prev" else 1)
                    log("move_cmd", cmd=cmd, selected=self.selected_index)
                    self.draw(force=True)
            if fd not in r:
                continue
            ev = read_key(fd)
            if ev is None:
                continue
            self.any_key_event = True
            self.last_input_time = time.time()
            try:
                log("key", code=ev.key_code, mods=ev.mods, event=ev.event_type)
                log("key_state", ctrl_down=self.ctrl_down, saw_tab=self.saw_tab_event)
                if ev.key_code == MARKER_KEY_CODE and ev.event_type == 1:
                    self.marker_seen = True
                    self.ctrl_down = True
                    self.ctrl_seen = True
                    log("marker_seen")
                    continue
                if ev.key_code == MARKER_KEY_CODE and ev.event_type == 3:
                    self.ctrl_down = False
                    log("marker_release")
                    continue
                if ev.key_code in CTRL_KEY_CODES and ev.event_type == 1:
                    self.ctrl_down = True
                    self.ctrl_seen = True
                if ev.key_code in CTRL_KEY_CODES and ev.event_type == 3:
                    self.ctrl_down = False
                if ev.key_code in CTRL_KEY_CODES and ev.event_type == 3:
                    if self._should_commit_on_ctrl_release():
                        log("ctrl_release_commit")
                        log(
                            "ctrl_release_state",
                            selected_index=self.selected_index,
                            selected_tab=self._current_tab_id(),
                            tabs=[t.id for t in self.tabs],
                        )
                        self.commit()
                        return
                    log("ctrl_release_ignored", elapsed=time.time() - self.start_time, saw_tab=self.saw_tab_event)
                if ev.key_code == ESC_CODE and ev.event_type == 1:
                    self.cancel()
                    return
                if ev.key_code == TAB_CODE and ev.event_type in (1, 2):
                    log(
                        "tab_key",
                        event_type=ev.event_type,
                        mods=ev.mods,
                        ctrl=ev.ctrl,
                        shift=ev.shift,
                        selected_index=self.selected_index,
                        selected_tab=self._current_tab_id(),
                    )
                    self.saw_tab_event = True
                    if ev.ctrl:
                        self.ctrl_down = True
                    self._move(-1 if ev.shift else 1)
                    log(
                        "tab_move",
                        event_type=ev.event_type,
                        selected_index=self.selected_index,
                        selected_tab=self._current_tab_id(),
                    )
                    log("move", selected=self.selected_index)
                    self.draw(force=True)
                    self._schedule_preview_fetch()
                    continue
                # Legacy shift-tab
                if ev.key_code == -TAB_CODE:
                    self.saw_tab_event = True
                    self._move(-1)
                    log("move", selected=self.selected_index)
                    self.draw(force=True)
                    self._schedule_preview_fetch()
                    continue
                if ev.key_code == ord("q") and ev.event_type == 1:
                    log("quit_q")
                    self.cancel()
                    return
            except Exception as exc:
                log("loop_exception", error=repr(exc))
                log("loop_traceback", trace=traceback.format_exc())
                raise

    def draw(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last_draw < 0.016:
            return
        self.last_draw = now
        rows, cols = screen_size()
        log("draw", rows=rows, cols=cols, tabs=len(self.tabs), selected=self.selected_index)
        write("\x1b[?25l")
        write("\x1b[2J\x1b[H")
        if debug_enabled():
            write(f"\x1b[1;1H[DEBUG] size={rows}x{cols}")
        if not self.tabs:
            write("\x1b[HNo tabs")
            flush()
            return

        layout = self._compute_layout(rows, cols)
        cards = self._visible_cards(layout["max_cards"])
        card_w = layout["card_w"]
        card_h = layout["card_h"]
        total_w = len(cards) * card_w + (len(cards) - 1) * 2
        if self.theme.align == "left":
            start_x = 1
        elif self.theme.align == "right":
            start_x = max(1, cols - total_w + 1)
        else:
            start_x = max(1, (cols - total_w) // 2 + 1)
        if self.theme.vertical_align == "top":
            start_y = 1
        elif self.theme.vertical_align == "bottom":
            start_y = max(1, rows - card_h + 1)
        else:
            start_y = max(1, (rows - card_h) // 2 + 1)

        for idx, tab in enumerate(cards):
            x = start_x + idx * (card_w + 2)
            y = start_y
            selected = self._current_tab_id() == tab.id
            if self._preview_stale(tab.id):
                if not hasattr(self, "preview_queue"):
                    self.preview_queue = []
                if tab.id not in self.preview_queue:
                    self.preview_queue.append(tab.id)
            log("card", idx=idx, tab_id=tab.id, title=tab.title, x=x, y=y, selected=selected)
            self._draw_card(x, y, card_w, card_h, tab, selected, rows, cols, layout)

        write("\x1b[?25l")
        flush()

    def commit(self) -> None:
        tab_id = self._current_tab_id()
        if tab_id is None:
            self.cancel()
            return
        log("commit", tab_id=tab_id)
        self._update_mru(tab_id)
        self._focus_tab(tab_id)

    def cancel(self) -> None:
        return

    def _visible_cards(self, max_cards: int) -> list[TabInfo]:
        if len(self.tabs) <= max_cards:
            return self.tabs
        half = max_cards // 2
        start = max(0, self.selected_index - half)
        end = min(len(self.tabs), start + max_cards)
        start = max(0, end - max_cards)
        return self.tabs[start:end]

    def _draw_card(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        tab: TabInfo,
        selected: bool,
        rows: int,
        cols: int,
        layout: dict[str, Any],
    ) -> None:
        border = self.theme.border_selected if selected else self.theme.border
        title = self._format_title(tab.title, w - 4)

        def write_at(row: int, col: int, text: str, invert: bool = False) -> None:
            if row < 1 or row > rows or col < 1 or col > cols:
                return
            max_len = cols - col + 1
            if max_len <= 0:
                return
            if "\x1b" not in text and len(text) > max_len:
                text = text[:max_len]
            if invert:
                write(f"\x1b[{row};{col}H\x1b[7m{text}\x1b[0m")
            else:
                write(f"\x1b[{row};{col}H{text}")

        top = border.tl + border.h * (w - 2) + border.tr
        mid = border.v + " " * (w - 2) + border.v
        bottom = border.bl + border.h * (w - 2) + border.br

        write_at(y + 1, x + 1, top, selected)
        write_at(y + 2, x + 1, mid, selected)
        write_at(y + 2, x + 3, title, selected and self.theme.title_invert_selected)
        if not layout["title_only"]:
            for r in range(layout["preview_rows"]):
                write_at(y + 3 + r, x + 1, mid, selected)
        write_at(y + h, x + 1, bottom, selected)

        if not layout["title_only"]:
            raw_lines = self.preview_cache.get(tab.id) or []
            preview = render_block_preview(
                raw_lines,
                layout["preview_cols"],
                layout["preview_rows"],
                color_mode=self.theme.preview_color_mode,
            )
            for r, line in enumerate(preview[: layout["preview_rows"]]):
                write_at(y + 3 + r, x + 3, self._wrap_preview_line(line, layout["preview_cols"]), False)

    def _should_commit_on_ctrl_release(self) -> bool:
        elapsed = time.time() - self.start_time
        if self.saw_tab_event:
            return True
        # ignore early ctrl release triggered by the invocation shortcut
        return elapsed > 0.2

    def _compute_layout(self, rows: int, cols: int) -> dict[str, Any]:
        gap = self.theme.gap
        preview_rows = self.theme.preview_rows
        title_only = False

        max_height = max(1, rows - 2)
        max_width = max(1, cols - 2)
        target_cards = max(1, len(self.tabs))

        # Fit height
        while preview_rows + 3 > max_height and preview_rows > self.theme.min_preview_rows:
            preview_rows -= 1

        def total_width(card_w: int, cards: int, gap_size: int) -> int:
            return cards * card_w + max(0, cards - 1) * gap_size

        # Ensure all cards fit by adapting gap and width
        for gap_try in (gap, 1, 0):
            gap = gap_try
            available = max_width - gap * max(0, target_cards - 1)
            if available <= 0:
                continue
            card_w = max(self.theme.card_min_width, available // target_cards)
            if total_width(card_w, target_cards, gap) <= max_width:
                break
        else:
            gap = 0
            card_w = max(self.theme.card_min_width, max_width // target_cards)

        preview_cols = max(0, card_w - 4)
        card_h = preview_rows + 3

        if preview_cols < self.theme.min_preview_cols or card_h > max_height or card_w < self.theme.card_min_width:
            title_only = True
            preview_cols = 0
            preview_rows = 0
            card_h = max(self.theme.card_min_height, 3)
            if target_cards > 0:
                available = max_width - gap * max(0, target_cards - 1)
                card_w = max(self.theme.card_min_width, available // target_cards)

        max_cards = target_cards

        return {
            "preview_cols": preview_cols,
            "preview_rows": preview_rows,
            "card_w": card_w,
            "card_h": card_h,
            "max_cards": max_cards,
            "title_only": title_only,
        }

    def _truncate(self, text: str, max_len: int, ellipsis: str | None = None) -> str:
        if max_len <= 0:
            return ""
        if len(text) <= max_len:
            return text
        if max_len <= 3:
            return text[:max_len]
        ell = ellipsis if ellipsis is not None else "..."
        if len(ell) >= max_len:
            return text[:max_len]
        return text[: max_len - len(ell)] + ell

    def _truncate_ansi(self, text: str, max_len: int) -> str:
        if max_len <= 0:
            return ""
        if "\x1b" not in text:
            return self._truncate(text, max_len)
        out: list[str] = []
        visible = 0
        i = 0
        while i < len(text) and visible < max_len:
            ch = text[i]
            if ch == "\x1b":
                end = text.find("m", i + 1)
                if end == -1:
                    break
                out.append(text[i:end + 1])
                i = end + 1
                continue
            out.append(ch)
            visible += 1
            i += 1
        out.append("\x1b[0m")
        return "".join(out)

    def _wrap_preview_line(self, text: str, max_len: int) -> str:
        if self.theme.wrap_preview == "clip":
            line = self._truncate_ansi(text, max_len)
        else:
            line = self._truncate_ansi(text, max_len)
        return f"\x1b[0m{line}\x1b[0m"

    def _format_title(self, text: str, width: int) -> str:
        if width <= 0:
            return ""
        pad = max(0, self.theme.title_padding)
        inner = max(0, width - pad * 2)
        if self.theme.wrap_title == "clip":
            content = text[:inner]
        else:
            content = self._truncate(text, inner, self.theme.ellipsis)
        if self.theme.title_align == "center":
            content = content.center(inner)
        elif self.theme.title_align == "right":
            content = content.rjust(inner)
        else:
            content = content.ljust(inner)
        return (" " * pad) + content + (" " * pad)

    def _move(self, delta: int) -> None:
        if not self.tabs:
            return
        before_idx = self.selected_index
        before_id = self._current_tab_id()
        self.selected_index = (self.selected_index + delta) % len(self.tabs)
        after_id = self._current_tab_id()
        log("move_detail", delta=delta, before_idx=before_idx, after_idx=self.selected_index, before_id=before_id, after_id=after_id)
        self._ensure_preview_cache()

    def _current_tab_id(self) -> int | None:
        if not self.tabs:
            return None
        return self.tabs[self.selected_index].id

    def _active_tab_id(self) -> int | None:
        for tab in self.tabs:
            if tab.is_active:
                return tab.id
        return self.tabs[0].id if self.tabs else None

    def _initial_index(self, direction: int) -> int:
        if not self.tabs:
            return 0
        if direction >= 0:
            return 1 % len(self.tabs)
        return (len(self.tabs) - 1) % len(self.tabs)

    def _reconcile_mru(self, last_used: dict[int, float]) -> dict[int, float]:
        base_tabs = list(self.tabs)
        base_order = [t.id for t in base_tabs]
        base_rank = {tid: idx for idx, tid in enumerate(base_order)}
        by_id = {t.id: t for t in base_tabs}

        # Prefer kitty-provided focus timestamps if available
        if any(t.last_focused is not None for t in self.tabs):
            now = time.time()
            out: dict[int, float] = {}
            for t in self.tabs:
                if t.last_focused is not None:
                    out[t.id] = t.last_focused
                else:
                    out[t.id] = 0.0
            active_id = self._active_tab_id()
            if active_id is not None:
                out[active_id] = max(out.get(active_id, 0.0), now)
            ordered = sorted(
                out.items(),
                key=lambda kv: (-kv[1], base_rank.get(kv[0], 10**9)),
            )
            log("mru_order", ordered=[k for k, _ in ordered], source="last_focused")
            self.tabs = [by_id[tid] for tid, _ in ordered if tid in by_id]
            return {tid: out[tid] for tid in base_order}

        now = time.time()
        out: dict[int, float] = {}
        for tid in base_order:
            if tid in last_used:
                out[tid] = last_used[tid]
            if tid not in out:
                out[tid] = 0.0
        active_id = self._active_tab_id()
        if active_id is not None:
            out[active_id] = max(out.get(active_id, 0.0), now)
        ordered = sorted(
            out.items(),
            key=lambda kv: (-kv[1], base_rank.get(kv[0], 10**9)),
        )
        log("mru_order", ordered=[k for k, _ in ordered], source="cache")
        self.tabs = [by_id[tid] for tid, _ in ordered if tid in by_id]
        return out

    def _tab_by_id(self, tab_id: int) -> TabInfo | None:
        for tab in self.tabs:
            if tab.id == tab_id:
                return tab
        return None

    def _ensure_preview_cache(self) -> None:
        if not self.tabs:
            return
        indices = {
            self.selected_index,
            (self.selected_index - 1) % len(self.tabs),
            (self.selected_index + 1) % len(self.tabs),
        }
        for idx in indices:
            tab = self.tabs[idx]
            if tab.id not in self.preview_cache:
                log("preview_fetch", tab_id=tab.id, window_id=tab.window_id)
                self.preview_cache[tab.id] = self._fetch_preview(tab)
                self.preview_cache_ts[tab.id] = time.time()
                self.preview_state.save(self.preview_cache, self.preview_cache_ts)

    def _schedule_preview_fetch(self) -> None:
        if not self.tabs:
            return
        if not hasattr(self, "preview_queue"):
            self.preview_queue: list[int] = []
        targets = [
            self.selected_index,
            (self.selected_index - 1) % len(self.tabs),
            (self.selected_index + 1) % len(self.tabs),
        ]
        for idx in targets:
            tab_id = self.tabs[idx].id
            if not self._preview_stale(tab_id):
                continue
            if tab_id not in self.preview_queue:
                self.preview_queue.append(tab_id)
        log("preview_queue", queued=self.preview_queue)

    def _preview_stale(self, tab_id: int) -> bool:
        if tab_id not in self.preview_cache:
            return True
        ts = self.preview_cache_ts.get(tab_id, 0.0)
        return (time.time() - ts) >= PREVIEW_REFRESH_SECS

    def _drain_preview_queue(self) -> bool:
        queue = getattr(self, "preview_queue", [])
        if not queue:
            return False
        tab_id = queue.pop(0)
        tab = self._tab_by_id(tab_id)
        if tab is not None and self._preview_stale(tab.id):
            log("preview_refresh", tab_id=tab.id, window_id=tab.window_id)
            self.preview_cache[tab.id] = self._fetch_preview(tab)
            self.preview_cache_ts[tab.id] = time.time()
            self.preview_state.save(self.preview_cache, self.preview_cache_ts)
            return True
        return False

    def _fetch_preview(self, tab: TabInfo) -> list[str]:
        try:
            lines = get_text_lines_from_rc(self.remote_control, tab.window_id, ansi=True)
            if not lines:
                log("preview_error", tab_id=tab.id, returncode=1)
                return [" " * PREVIEW_COLS] * PREVIEW_ROWS
        except Exception as exc:
            log("preview_exception", tab_id=tab.id, error=repr(exc))
            return [" " * PREVIEW_COLS] * PREVIEW_ROWS
        log("preview_lines", tab_id=tab.id, lines=len(lines))
        return lines

    def _focus_tab(self, tab_id: int) -> None:
        self.remote_control(
            [
                "focus-tab",
                "--match",
                f"id:{tab_id}",
                "--no-response",
            ],
            capture_output=True,
        )

    def _update_mru(self, tab_id: int) -> None:
        base = [tid for tid in self.mru_order if tid not in {tab_id, self.original_tab_id}]
        new_order = [tab_id]
        if self.original_tab_id is not None and self.original_tab_id != tab_id:
            new_order.append(self.original_tab_id)
        new_order.extend(base)
        self.mru_order = new_order
        now = time.time()
        self.last_used = {tid: now - (idx * 0.001) for idx, tid in enumerate(self.mru_order)}
        log("mru_commit", ordered=self.mru_order, committed=tab_id, original=self.original_tab_id)
        self.state.save(self.last_used)

    def _send_marker(self) -> None:
        try:
            win_id = os.environ.get("KITTY_WINDOW_ID")
            if not win_id:
                return
            cp = self.remote_control(
                [
                    "send-key",
                    "--match",
                    f"id:{win_id}",
                    "f24",
                ],
                capture_output=True,
            )
            self.marker_sent = (cp.returncode == 0)
            log("marker_send", sent=self.marker_sent, returncode=cp.returncode)
        except Exception as exc:
            log("marker_send_error", error=repr(exc))

    def _check_initial_mods(self) -> None:
        if self.mod_query is None or self.ctrl_mask is None:
            return
        try:
            mods = self.mod_query()
        except Exception:
            mods = None
        if mods is None:
            return
        self.initial_mods_checked = True
        self.initial_ctrl_down = ctrl_is_down(mods, self.ctrl_mask)
        log("initial_mods", mods=mods, ctrl_down=self.initial_ctrl_down)


def _parse_switcher_args(args: list[str]) -> tuple[int, str | None]:
    direction = 1
    theme_path: str | None = None
    it = iter(args)
    for raw in it:
        arg = raw.strip()
        low = arg.lower()
        if low in {"prev", "previous", "left", "-1"}:
            direction = -1
            continue
        if low in {"next", "right", "1"}:
            direction = 1
            continue
        if low.startswith("--theme="):
            theme_path = arg.split("=", 1)[1]
            continue
        if low == "--theme":
            theme_path = next(it, None)
            continue
    return direction, theme_path


@kitten_ui(allow_remote_control=True)
def main(args: list[str]) -> str:
    direction, theme_path = _parse_switcher_args(args)
    if theme_path is None:
        theme_path = os.environ.get(THEME_ENV_VAR)
    theme = load_theme(theme_path)
    log("theme_loaded", path=theme_path, name=theme.name)
    rc = main.remote_control
    os_window_id, tabs = list_tabs(rc)
    if not tabs:
        return ""
    cmd = "prev" if direction < 0 else "next"
    if try_send_command(os_window_id, cmd):
        log("command_sent", cmd=cmd, os_window_id=os_window_id)
        return ""
    mru = StateStore(os_window_id).load()
    server = CommandServer(os_window_id)
    switcher = RawSwitcher(tabs, mru, os_window_id, direction, rc, server, theme)
    try:
        run_in_raw_mode(switcher)
    finally:
        server.close()
    return ""


def run_in_raw_mode(switcher: RawSwitcher) -> None:
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        set_window_title(SWITCHER_TITLE)
        log(
            "raw_mode_enter",
            stdin_isatty=sys.stdin.isatty(),
            stdout_isatty=sys.stdout.isatty(),
            size_in=terminal_size_safe(sys.stdin.fileno()),
            size_out=terminal_size_safe(sys.stdout.fileno()),
        )
        enter_alternate_screen()
        enter_keyboard_mode()
        switcher.run()
    finally:
        exit_keyboard_mode()
        exit_alternate_screen()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        log("raw_mode_exit")
        write("\x1b[?25h\x1b[0m")
        flush()


def enter_keyboard_mode() -> None:
    write(KEYBOARD_MODE_PUSH)
    write(KEYBOARD_MODE_SET)
    flush()


def exit_keyboard_mode() -> None:
    write(KEYBOARD_MODE_POP)
    flush()


def enter_alternate_screen() -> None:
    write("\x1b[?1049h")
    flush()


def exit_alternate_screen() -> None:
    write("\x1b[?1049l")
    flush()


def set_window_title(title: str) -> None:
    write(f"\x1b]2;{title}\x07")
    flush()


def read_key(fd: int) -> KeyEvent | None:
    try:
        first = os.read(fd, 1)
    except OSError:
        return None
    if not first:
        return None
    if debug_enabled():
        log("raw_byte", b=first.hex())
    if first == b"\t":
        return KeyEvent(TAB_CODE, 1, 1)
    if first != b"\x1b":
        return KeyEvent(first[0], 1, 1)

    nxt = read_with_timeout(fd, 0.01)
    if not nxt:
        if debug_enabled():
            log("raw_seq", seq="ESC")
        return KeyEvent(ESC_CODE, 1, 1)
    if nxt != b"[":
        if debug_enabled():
            log("raw_seq", seq=("ESC" + nxt.hex()))
        return KeyEvent(ESC_CODE, 1, 1)

    buf = bytearray(b"\x1b[")
    while True:
        ch = read_with_timeout(fd, 0.01)
        if not ch:
            break
        buf.extend(ch)
        if ch and ch[0] >= 0x40 and ch[0] <= 0x7e:
            break
        if len(buf) > 64:
            break

    if debug_enabled():
        log("raw_seq", seq=buf.hex())

    if buf.endswith(b"Z"):
        return KeyEvent(-TAB_CODE, 2, 1)
    if buf.endswith(b"I"):
        return KeyEvent(TAB_CODE, 1, 1)
    if buf.endswith(b"~"):
        # Try to interpret known sequences
        try:
            params = buf[2:-1].decode("ascii", "ignore")
        except Exception:
            params = ""
        if params.strip() == "9":
            return KeyEvent(TAB_CODE, 1, 1)
        if params.strip() == "24":
            return KeyEvent(MARKER_KEY_CODE, 1, 1)
    if not buf.endswith(b"u"):
        return KeyEvent(ESC_CODE, 1, 1)

    return parse_csi_u(bytes(buf))


def parse_csi_u(buf: bytes) -> KeyEvent | None:
    try:
        payload = buf[2:-1].decode("ascii", "ignore")
    except Exception:
        return None
    if not payload:
        return None
    parts = payload.split(";")
    key_field = parts[0]
    key_code = int(key_field.split(":")[0] or 0)
    mods = 1
    event_type = 1
    if len(parts) > 1 and parts[1] != "":
        mod_field = parts[1]
        if ":" in mod_field:
            mods_str, ev_str = mod_field.split(":", 1)
            mods = int(mods_str) if mods_str else 1
            event_type = int(ev_str) if ev_str else 1
        else:
            mods = int(mod_field)
    return KeyEvent(key_code, mods, event_type)


def read_with_timeout(fd: int, timeout: float) -> bytes:
    r, _, _ = select.select([fd], [], [], timeout)
    if not r:
        return b""
    return os.read(fd, 1)


def screen_size() -> tuple[int, int]:
    size_out = terminal_size_safe(sys.stdout.fileno())
    size_in = terminal_size_safe(sys.stdin.fileno())
    if size_out:
        return size_out
    if size_in:
        return size_in
    return 24, 80


def terminal_size_safe(fd: int) -> tuple[int, int] | None:
    try:
        size = os.get_terminal_size(fd)
        return size.lines, size.columns
    except OSError:
        return None


def write(text: str) -> None:
    sys.stdout.write(text)


def flush() -> None:
    sys.stdout.flush()


def list_tabs(remote_control: Callable[..., Any]) -> tuple[int, list[TabInfo]]:
    try:
        cp = remote_control(["ls"], capture_output=True)
        if cp.returncode != 0:
            return 0, []
        raw = cp.stdout
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        data = json.loads(raw)
    except Exception:
        return 0, []

    current_window_id = int(os.environ.get("KITTY_WINDOW_ID", "0") or "0")
    log("list_tabs", current_window_id=current_window_id)
    for os_window in data:
        tabs = os_window.get("tabs", [])
        if debug_enabled():
            log("os_window_keys", keys=sorted(os_window.keys()))
        history = os_window.get("active_window_history") or []
        for tab in tabs:
            for win in tab.get("windows", []):
                if int(win.get("id", -1)) == current_window_id:
                    log("list_tabs_match", os_window_id=os_window.get("id", 0), tabs=len(tabs))
                    return os_window.get("id", 0), parse_tabs(tabs, tab, history, current_window_id)
    if data:
        os_window = data[0]
        tabs = os_window.get("tabs", [])
        history = os_window.get("active_window_history") or []
        log("list_tabs_fallback", os_window_id=os_window.get("id", 0), tabs=len(tabs))
        return os_window.get("id", 0), parse_tabs(tabs, None, history, current_window_id)
    return 0, []


def parse_tabs(
    tabs: Iterable[dict[str, Any]],
    active_tab: dict[str, Any] | None,
    os_window_history: Iterable[Any] | None,
    current_window_id: int,
) -> list[TabInfo]:
    active_tab_id = None
    if active_tab is not None:
        active_tab_id = active_tab.get("id")
    history_positions: dict[int, int] = {}
    if os_window_history:
        for idx, win_id in enumerate(os_window_history):
            try:
                history_positions[int(win_id)] = idx
            except Exception:
                continue
    parsed: list[TabInfo] = []
    for tab in tabs:
        tab_id = int(tab.get("id", 0))
        title = tab.get("title") or "Untitled"
        if debug_enabled():
            log("tab_keys", id=tab_id, keys=sorted(tab.keys()))
        last_focused = None
        windows = tab.get("windows", [])
        if history_positions and windows:
            max_idx = None
            for win in windows:
                try:
                    wid = int(win.get("id", 0))
                except Exception:
                    continue
                if wid in history_positions:
                    if max_idx is None or history_positions[wid] > max_idx:
                        max_idx = history_positions[wid]
            if max_idx is not None:
                last_focused = float(max_idx)
        for key in ("last_focused", "last_active", "last_activated", "last_activity"):
            val = tab.get(key)
            if isinstance(val, (int, float)):
                last_focused = float(val)
                break
        window_id = 0
        reason = "none"
        history = tab.get("active_window_history") or []
        if history:
            for wid in history:
                try:
                    cand = int(wid)
                except Exception:
                    continue
                if cand == current_window_id:
                    continue
                window_id = cand
                reason = "history"
                break
        if window_id == 0:
            for win in windows:
                if win.get("is_focused") or win.get("is_active") or win.get("active"):
                    try:
                        cand = int(win.get("id", 0))
                    except Exception:
                        continue
                    if cand == current_window_id:
                        continue
                    window_id = cand
                    reason = "active"
                    break
        if window_id == 0:
            for win in windows:
                try:
                    cand = int(win.get("id", 0))
                except Exception:
                    continue
                if cand == current_window_id:
                    continue
                window_id = cand
                reason = "first"
                break
        if window_id == 0 and windows:
            window_id = int(windows[0].get("id", 0))
            reason = "fallback_current"
        if debug_enabled():
            log("tab_window_pick", tab_id=tab_id, window_id=window_id, reason=reason)
        is_active = tab_id == active_tab_id or bool(tab.get("is_active") or tab.get("active"))
        parsed.append(
            TabInfo(
                id=tab_id,
                title=title,
                window_id=window_id,
                is_active=is_active,
                last_focused=last_focused,
            )
        )
    log("parse_tabs", count=len(parsed))
    return parsed


def command_socket_path(os_window_id: int) -> str:
    base = "/tmp"
    return os.path.join(base, f"kitty-tab-switcher-{os_window_id}.sock")


class CommandServer:
    def __init__(self, os_window_id: int) -> None:
        self.path = command_socket_path(os_window_id)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            self.sock.bind(self.path)
        except OSError:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            self.sock.bind(self.path)
        log("command_server_bind", path=self.path)

    def fileno(self) -> int:
        return self.sock.fileno()

    def recv(self) -> str:
        try:
            data, _ = self.sock.recvfrom(32)
        except OSError:
            return ""
        return data.decode("utf-8", "ignore")

    def close(self) -> None:
        try:
            self.sock.close()
        finally:
            try:
                os.unlink(self.path)
            except OSError:
                pass


def try_send_command(os_window_id: int, cmd: str) -> bool:
    path = command_socket_path(os_window_id)
    if not os.path.exists(path):
        log("command_send_missing", path=path, cmd=cmd)
        return False
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.sendto(cmd.encode("utf-8"), path)
        sock.close()
        log("command_send_ok", path=path, cmd=cmd)
        return True
    except OSError:
        log("command_send_error", path=path, cmd=cmd)
        return False


def debug_enabled() -> bool:
    return os.environ.get(DEBUG_ENV, "").strip() not in {"", "0", "false", "False"}


def debug_path() -> str:
    return os.environ.get(DEBUG_PATH_ENV, DEFAULT_DEBUG_PATH)


def log(message: str, **fields: Any) -> None:
    if not debug_enabled():
        return
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        payload = " ".join(f"{k}={fields[k]!r}" for k in sorted(fields))
        line = f"[{ts}] {message} {payload}\n"
        path = debug_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def resolve_mod_query() -> tuple[Callable[[], int | None] | None, int | None]:
    global _fdt
    if _fdt is None:
        try:
            from kitty import fast_data_types as _maybe_fdt  # type: ignore
            _fdt = _maybe_fdt
        except Exception as exc:
            log("mods_import_error", error=repr(exc))
            return None, None
    ctrl_mask = getattr(_fdt, "GLFW_MOD_CONTROL", None)
    if ctrl_mask is None:
        ctrl_mask = 4
    candidates = (
        "get_key_mods",
        "get_mods",
        "get_modifiers",
        "current_mods",
        "modifiers",
        "get_mod_state",
        "get_mods_state",
        "key_mods",
        "mods_state",
        "current_key_mods",
        "get_keyboard_mods",
    )
    for name in candidates:
        fn = getattr(_fdt, name, None)
        if callable(fn):
            def _wrap(fn=fn) -> int | None:
                try:
                    return int(fn())
                except Exception:
                    return None
            log("mods_fn", name=name, ctrl_mask=ctrl_mask)
            return _wrap, int(ctrl_mask)
    # Try boss-based fallback if available
    boss = getattr(_fdt, "get_boss", None)
    if callable(boss):
        try:
            b = boss()
        except Exception:
            b = None
        if b is not None:
            for name in ("get_key_mods", "get_mods", "key_mods", "mods"):
                fn = getattr(b, name, None)
                if callable(fn):
                    def _wrap(fn=fn) -> int | None:
                        try:
                            return int(fn())
                        except Exception:
                            return None
                    log("mods_fn", name=f"boss.{name}", ctrl_mask=ctrl_mask)
                    return _wrap, int(ctrl_mask)
    if debug_enabled():
        try:
            names = [n for n in dir(_fdt) if "mod" in n.lower()]
        except Exception:
            names = []
        log("mods_unavailable", candidates=names)
    # macOS side-channel: query ctrl key state via CoreGraphics
    if sys.platform == "darwin":
        try:
            cg = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            cg.CGEventSourceKeyState.argtypes = [ctypes.c_int, ctypes.c_int]
            cg.CGEventSourceKeyState.restype = ctypes.c_bool
            kVK_Control = 0x3B
            kVK_RightControl = 0x3E
            def _mac_mods() -> int | None:
                try:
                    down = bool(cg.CGEventSourceKeyState(0, kVK_Control) or cg.CGEventSourceKeyState(0, kVK_RightControl))
                    return 4 if down else 0
                except Exception:
                    return None
            log("mods_fn", name="macos_cg", ctrl_mask=ctrl_mask)
            return _mac_mods, int(ctrl_mask)
        except Exception as exc:
            log("mods_import_error", error=repr(exc))
    return None, None


def ctrl_is_down(mods: int, ctrl_mask: int) -> bool:
    # Try both GLFW-style mask and kitty keyboard protocol style (1+mods)
    if mods & ctrl_mask:
        return True
    if mods > 0 and ((mods - 1) & 4):
        return True
    return False


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
