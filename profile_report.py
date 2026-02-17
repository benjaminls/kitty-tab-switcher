#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Any


DEFAULT_PATH = os.path.expanduser("~/.cache/kitty-tab-switcher-profile.jsonl")


def _read_records(path: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def _pick_current(records: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, int]:
    if not records:
        return None, -1
    for idx in range(len(records) - 1, -1, -1):
        if records[idx].get("type") == "final":
            return records[idx], idx
    return records[-1], len(records) - 1


def _fmt_ts(ts: Any) -> str:
    try:
        return dt.datetime.fromtimestamp(float(ts)).isoformat(timespec="seconds")
    except Exception:
        return "unknown"


def _to_float(v: Any) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def _to_int(v: Any) -> int:
    try:
        return int(v)
    except Exception:
        return 0


def _print_table(sections: dict[str, dict[str, Any]], top: int) -> None:
    rows: list[tuple[str, dict[str, Any]]] = sorted(
        sections.items(),
        key=lambda kv: _to_float(kv[1].get("total_ms", 0.0)),
        reverse=True,
    )
    if top > 0:
        rows = rows[:top]
    total_all = sum(_to_float(v.get("total_ms", 0.0)) for _, v in rows) or 1.0
    print(f"{'section':28} {'count':>8} {'total_ms':>10} {'avg_ms':>9} {'p50':>8} {'p95':>8} {'max':>8} {'pct':>7}")
    for name, s in rows:
        count = _to_int(s.get("count", 0))
        total_ms = _to_float(s.get("total_ms", 0.0))
        avg_ms = _to_float(s.get("avg_ms", 0.0))
        p50 = _to_float(s.get("p50_ms", 0.0))
        p95 = _to_float(s.get("p95_ms", 0.0))
        max_ms = _to_float(s.get("max_ms", 0.0))
        pct = (total_ms / total_all) * 100.0
        print(f"{name:28} {count:8d} {total_ms:10.3f} {avg_ms:9.3f} {p50:8.3f} {p95:8.3f} {max_ms:8.3f} {pct:6.1f}%")


def _print_delta(current: dict[str, Any], previous: dict[str, Any] | None, top: int) -> None:
    if previous is None:
        print("\nDelta view unavailable (need at least 2 records).")
        return
    cur = current.get("sections", {}) if isinstance(current.get("sections"), dict) else {}
    prev = previous.get("sections", {}) if isinstance(previous.get("sections"), dict) else {}
    names = set(cur) | set(prev)
    deltas: list[tuple[str, int, float]] = []
    for n in names:
        c = cur.get(n, {})
        p = prev.get(n, {})
        dc = max(0, _to_int(c.get("count", 0)) - _to_int(p.get("count", 0)))
        dt_ms = max(0.0, _to_float(c.get("total_ms", 0.0)) - _to_float(p.get("total_ms", 0.0)))
        if dc == 0 and dt_ms == 0:
            continue
        deltas.append((n, dc, dt_ms))
    deltas.sort(key=lambda t: t[2], reverse=True)
    if top > 0:
        deltas = deltas[:top]
    if not deltas:
        print("\nNo per-interval deltas detected.")
        return
    total = sum(t[2] for t in deltas) or 1.0
    print("\nTop interval deltas (current - previous):")
    print(f"{'section':28} {'d_count':>8} {'d_total_ms':>11} {'pct':>7}")
    for name, dc, dt_ms in deltas:
        pct = (dt_ms / total) * 100.0
        print(f"{name:28} {dc:8d} {dt_ms:11.3f} {pct:6.1f}%")


def _print_info() -> None:
    print("How to read profiler output")
    print("")
    print("Record model:")
    print("- Each JSONL line is a profiler record emitted by `SectionProfiler.flush()` in `tab_switcher.py`.")
    print("- `type=snapshot` records are periodic cumulative summaries since profiler start.")
    print("- `type=final` is the same cumulative summary written at shutdown.")
    print("- Values are cumulative, not per-interval, unless you use the delta table.")
    print("")
    print("Columns in main table:")
    print("- `section`: timed code region name from `tab_switcher.py`.")
    print("- `count`: number of completed timed samples for that section.")
    print("- `total_ms`: sum of all sample durations for that section.")
    print("- `avg_ms`: arithmetic mean duration (`total_ms / count`).")
    print("- `p50`: median sample duration.")
    print("- `p95`: 95th percentile sample duration.")
    print("- `max`: maximum single-sample duration.")
    print("- `pct`: section share of displayed rows' `total_ms` (not whole-process wall time).")
    print("")
    print("Delta table:")
    print("- `d_count`: `count(current) - count(previous)` for each section.")
    print("- `d_total_ms`: `total_ms(current) - total_ms(previous)` for each section.")
    print("- Delta approximates latest interval cost because records are cumulative.")
    print("")
    print("Known sections (from current instrumentation):")
    print("- `loop.select_wait`: time inside `select.select()` wait.")
    print("- `loop.mod_poll`: modifier polling call (`mod_query()`).")
    print("- `loop.read_key`: key decode/read path.")
    print("- `event.handle`: per-event state/update logic.")
    print("- `draw.total`: full draw pass.")
    print("- `preview.fetch.remote`: remote-control `get-text` fetch time.")
    print("- `preview.render.block`: `render_block_preview()` time.")
    print("- `cache.save.preview`: preview cache file write time.")
    print("- `cache.save.state`: MRU cache file write time.")
    print("")
    print("Interpretation tips:")
    print("- High `loop.select_wait` is expected idle time, not CPU overhead.")
    print("- Focus on `p95` and `max` for stutter, not only `avg_ms`.")
    print("- Use deltas while reproducing one interaction pattern to isolate new work.")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Summarize kitty tab switcher profiler JSONL logs.")
    parser.add_argument("path", nargs="?", default=DEFAULT_PATH, help=f"Profiler JSONL path (default: {DEFAULT_PATH})")
    parser.add_argument("--top", type=int, default=20, help="How many sections to show (default: 20)")
    parser.add_argument("--json", action="store_true", help="Print selected record as JSON")
    parser.add_argument("--no-delta", action="store_true", help="Disable interval-delta section")
    parser.add_argument("--info", action="store_true", help="Explain columns, sections, and interpretation")
    args = parser.parse_args(argv)

    if args.info:
        _print_info()
        return 0

    records = _read_records(args.path)
    if not records:
        print(f"No profiler records found at: {args.path}")
        return 1

    current, idx = _pick_current(records)
    if current is None:
        print("No valid profiler record found.")
        return 1
    previous = records[idx - 1] if idx > 0 else None

    print(f"path: {args.path}")
    print(f"records: {len(records)}")
    print(f"selected: type={current.get('type', 'unknown')} ts={_fmt_ts(current.get('ts'))}")
    if previous is not None:
        print(f"previous: type={previous.get('type', 'unknown')} ts={_fmt_ts(previous.get('ts'))}")

    sections = current.get("sections", {})
    if not isinstance(sections, dict) or not sections:
        print("Selected record has no section data.")
        return 1

    if args.json:
        print(json.dumps(current, indent=2, sort_keys=True))
        return 0

    print("")
    _print_table(sections, args.top)
    if not args.no_delta:
        _print_delta(current, previous, args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
