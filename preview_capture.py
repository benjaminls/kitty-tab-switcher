#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
import json
import sys
from typing import Any, Callable


def get_text_lines_from_rc(remote_control: Callable[..., Any], window_id: int, ansi: bool = False) -> list[str]:
    cp = remote_control(
        [
            "get-text",
            "--match",
            f"id:{window_id}",
            "--extent",
            "screen",
            "--ansi" if ansi else "",
        ],
        capture_output=True,
    )
    if cp.returncode != 0:
        return []
    raw = cp.stdout
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "replace")
    else:
        text = str(raw)
    return text.splitlines()


def _run_kitty_get_text(window_id: int, listen_on: str | None, timeout: float, ansi: bool) -> tuple[int, str]:
    cmd = [
        "kitty",
        "@",
        "--to",
        listen_on or "",
        "get-text",
        "--match",
        f"id:{window_id}",
        "--extent",
        "screen",
        "--ansi" if ansi else "",
    ]
    if not listen_on:
        # remove empty --to if not provided
        cmd = [c for c in cmd if c]
    cmd = [c for c in cmd if c]
    try:
        cp = subprocess.run(cmd, check=False, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "kitty not found in PATH"
    except subprocess.TimeoutExpired:
        return 124, "kitty @ get-text timed out"
    if cp.returncode != 0:
        err = cp.stderr.decode("utf-8", "replace") if isinstance(cp.stderr, bytes) else str(cp.stderr)
        return cp.returncode, err
    out = cp.stdout.decode("utf-8", "replace") if isinstance(cp.stdout, bytes) else str(cp.stdout)
    return 0, out


def get_text_from_kitty(
    window_id: int | None,
    listen_on: str | None,
    ansi: bool = False,
    timeout: float = 2.0,
) -> tuple[int, str]:
    if listen_on is None:
        listen_on = _probe_listen_on(timeout)
    else:
        code, _ = _run_kitty_ls(listen_on, timeout)
        if code != 0:
            listen_on = _probe_listen_on(timeout)
    if window_id is None or window_id == 0:
        picked = _pick_active_window_id(listen_on, timeout)
        if picked is None:
            return 2, "No window id provided and unable to determine active window."
        window_id = picked
    if not _window_id_exists(window_id, listen_on, timeout):
        picked = _pick_active_window_id(listen_on, timeout)
        if picked is None:
            return 2, "No matching windows and unable to determine active window."
        window_id = picked
    return _run_kitty_get_text(window_id, listen_on, timeout, ansi)




def _probe_listen_on(timeout: float) -> str | None:
    try:
        entries = [p for p in os.listdir("/tmp") if p.startswith("kitty-")]
    except Exception:
        return None
    for name in sorted(entries, reverse=True):
        path = f"unix:/tmp/{name}"
        code, _ = _run_kitty_ls(path, timeout)
        if code == 0:
            return path
    return None


def _run_kitty_ls(listen_on: str | None, timeout: float) -> tuple[int, str]:
    cmd = ["kitty", "@", "--to", listen_on or "", "ls"]
    if not listen_on:
        cmd = [c for c in cmd if c]
    try:
        cp = subprocess.run(cmd, check=False, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return 127, "kitty not found in PATH"
    except subprocess.TimeoutExpired:
        return 124, "kitty @ ls timed out"
    if cp.returncode != 0:
        err = cp.stderr.decode("utf-8", "replace") if isinstance(cp.stderr, bytes) else str(cp.stderr)
        return cp.returncode, err
    out = cp.stdout.decode("utf-8", "replace") if isinstance(cp.stdout, bytes) else str(cp.stdout)
    return 0, out


def _pick_active_window_id(listen_on: str | None, timeout: float) -> int | None:
    code, out = _run_kitty_ls(listen_on, timeout)
    if code != 0:
        return None
    try:
        data = json.loads(out)
    except Exception:
        return None
    for osw in data:
        if not osw.get("is_active"):
            continue
        tabs = osw.get("tabs", [])
        for tab in tabs:
            if not tab.get("is_active"):
                continue
            windows = tab.get("windows", [])
            for win in windows:
                if win.get("is_active") or win.get("is_focused"):
                    try:
                        return int(win.get("id", 0))
                    except Exception:
                        pass
            if windows:
                try:
                    return int(windows[0].get("id", 0))
                except Exception:
                    pass
    # fallback: first window anywhere
    for osw in data:
        tabs = osw.get("tabs", [])
        for tab in tabs:
            windows = tab.get("windows", [])
            if windows:
                try:
                    return int(windows[0].get("id", 0))
                except Exception:
                    pass
    return None


def _window_id_exists(window_id: int, listen_on: str | None, timeout: float) -> bool:
    code, out = _run_kitty_ls(listen_on, timeout)
    if code != 0:
        return False
    try:
        data = json.loads(out)
    except Exception:
        return False
    for osw in data:
        for tab in osw.get("tabs", []):
            for win in tab.get("windows", []):
                try:
                    if int(win.get("id", 0)) == window_id:
                        return True
                except Exception:
                    continue
    return False


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Capture raw text from a kitty window.")
    parser.add_argument("--window-id", type=int, default=None)
    parser.add_argument("--listen-on", type=str, default=None)
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--out", type=str, default=None)
    parser.add_argument("--ansi", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    window_id = args.window_id
    if window_id is None:
        try:
            window_id = int(os.environ.get("KITTY_WINDOW_ID", "0") or "0")
        except ValueError:
            window_id = 0
    listen_on = args.listen_on or os.environ.get("KITTY_LISTEN_ON")
    if window_id and not _window_id_exists(window_id, listen_on, args.timeout):
        window_id = 0
    if not window_id:
        picked = _pick_active_window_id(listen_on, args.timeout)
        if picked is None:
            sys.stderr.write("No window id provided and unable to determine active window.\n")
            return 2
        window_id = picked

    if args.debug:
        sys.stderr.write(f"using window_id={window_id}\\n")
    code, text = _run_kitty_get_text(window_id, listen_on, args.timeout, args.ansi)
    if code != 0:
        # If the window id is stale, try picking an active one.
        if "No matching windows" in text:
            picked = _pick_active_window_id(listen_on, args.timeout)
            if picked and picked != window_id:
                code, text = _run_kitty_get_text(picked, listen_on, args.timeout, args.ansi)
                if code == 0:
                    window_id = picked
                else:
                    sys.stderr.write(text + "\n")
                    return code
            else:
                sys.stderr.write(text + "\n")
                return code
        else:
            sys.stderr.write(text + "\n")
            return code
        return code

    if args.debug:
        sys.stderr.write(f"captured_bytes={len(text)}\\n")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
