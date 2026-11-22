#!/usr/bin/env python3
"""Daily Qianji cache refresh through the local connector.

Plain script: imports the connector's read-only bridge and calls the upstream
`refresh_cache` tool over loopback. No bot, no model, no tokens.

Run: .venv/bin/python scripts/sync_daily.py
Exit: 0 refreshed (or already running), 1 failed with the failure written to the log.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qianji_chatgpt import BridgeError, ReadOnlyBridge, Settings  # noqa: E402

DB_PATH = Path(os.environ.get("QIANJI_DB_PATH", str(ROOT / ".private" / "qianji.db")))
LOG_PATH = Path(os.environ.get("QIANJI_SYNC_LOG", str(ROOT / "logs" / "sync-daily.log")))
LOCK_PATH = LOG_PATH.with_name("sync-daily.lock")
# Whole-run guard; the bridge itself enforces its own per-call timeout (105s).
RUN_TIMEOUT_SECONDS = float(os.environ.get("QIANJI_SYNC_TIMEOUT", "900"))


def iso(epoch: object) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(int(epoch)).astimezone().isoformat(timespec="seconds")


def snapshot() -> dict:
    """Read local cache stats without touching the writer (read-only URI)."""
    uri = f"file:{DB_PATH}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=15) as conn:
        row = conn.execute("SELECT COUNT(*), MIN(time), MAX(time) FROM bills").fetchone()
        cursors_row = conn.execute("SELECT lasttimes_json FROM sync_state LIMIT 1").fetchone()
    cursors: dict = {}
    if cursors_row:
        try:
            raw = json.loads(cursors_row[0])
            if isinstance(raw, dict):
                cursors = {str(k): iso(v) for k, v in raw.items()}
        except (TypeError, ValueError):
            cursors = {"raw": str(cursors_row[0])[:200]}
    return {
        "bill_count": int(row[0]),
        "earliest_bill_time": iso(row[1]),
        "latest_bill_time": iso(row[2]),
        "sync_cursors": cursors,
    }


def payload_of(result: object) -> dict:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        return dict(structured)
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return {"raw_text": text[:1000]}
        if isinstance(parsed, dict):
            return parsed
        return {"raw": parsed}
    return {}


async def call_refresh() -> object:
    os.environ.setdefault("QIANJI_READ_ONLY", "1")
    bridge = ReadOnlyBridge(Settings.from_env())
    async with asyncio.timeout(RUN_TIMEOUT_SECONDS):
        return await bridge.call_tool("refresh_cache", {})


def emit(record: dict, pretty: bool = False) -> None:
    line = json.dumps(record, ensure_ascii=False)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(json.dumps(record, ensure_ascii=False, indent=2) if pretty else line, flush=True)


def main() -> int:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = LOCK_PATH.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        emit({
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "ok": False,
            "skipped": "another sync is still running",
        })
        return 0

    started = time.time()
    record: dict = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "ok": False,
        "db": str(DB_PATH),
    }

    try:
        record["before"] = snapshot()
    except (sqlite3.Error, OSError) as exc:
        record["before"] = None
        record["before_error"] = f"{type(exc).__name__}: {exc}"

    result = None
    try:
        result = asyncio.run(call_refresh())
        upstream = payload_of(result)
        if getattr(result, "isError", False):
            text = ""
            for item in getattr(result, "content", None) or []:
                text = getattr(item, "text", "") or text
            record["error"] = text.strip() or "上游返回错误，未给出原因"
        else:
            record["upstream"] = upstream
    except BridgeError as exc:
        record["error"] = f"BridgeError: {exc}"
    except asyncio.TimeoutError:
        record["error"] = f"TimeoutError: 运行超过 {RUN_TIMEOUT_SECONDS:.0f}s，未确认同步结果"
    except Exception as exc:  # keep the failure visible instead of dying silently
        record["error"] = f"{type(exc).__name__}: {exc}"

    try:
        record["after"] = snapshot()
    except (sqlite3.Error, OSError) as exc:
        record["after"] = None
        record["after_error"] = f"{type(exc).__name__}: {exc}"

    before, after = record.get("before"), record.get("after")
    if before and after:
        record["delta_bills"] = after["bill_count"] - before["bill_count"]
    record["duration_s"] = round(time.time() - started, 2)
    record["ok"] = "error" not in record and record.get("after") is not None
    if not record["ok"] and "error" not in record:
        record["error"] = "本地缓存读取失败，无法确认同步结果"

    emit(record, pretty=True)
    if not record["ok"]:
        print(f"失败原因：{record.get('error', '未知')}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
