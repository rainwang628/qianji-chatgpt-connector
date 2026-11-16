#!/usr/bin/env python3
"""Keep the one-time login ticket in a private local redirect, outside logs."""

import asyncio
import html
import json
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from qianji_chatgpt import ReadOnlyBridge, Settings


async def prepare():
    logging.disable(logging.CRITICAL)
    result = await ReadOnlyBridge(Settings.from_env()).call_tool("connect_qianji", {})
    data = result.structuredContent or {}
    url = data.get("url", "")
    parsed = urlsplit(url)
    if result.isError or parsed.scheme != "http" or parsed.netloc != "127.0.0.1:3000" or parsed.path != "/connect":
        raise SystemExit("Unable to prepare a safe local login link")
    os.umask(0o077)
    page = ROOT / ".private/login.html"
    page.write_text('<!doctype html><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
                    '<meta http-equiv="refresh" content="0;url=' + html.escape(url, quote=True) + '">'
                    '<title>打开钱迹本机登录页</title><p>正在打开本机钱迹登录页…</p>', encoding="utf-8")
    page.chmod(0o600)
    print(json.dumps({"local_login_file": str(page), "expires_at": data.get("expiresAt")}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(prepare())
