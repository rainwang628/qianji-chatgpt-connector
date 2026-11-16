#!/usr/bin/env python3
"""Run the pinned upstream on loopback with private files and no shell secrets."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / ".private"
os.umask(0o077)
PRIVATE.mkdir(mode=0o700, exist_ok=True)
PRIVATE.chmod(0o700)
credentials = PRIVATE / "upstream.env"
if credentials.stat().st_mode & 0o077:
    raise SystemExit("Refusing credentials readable by another user")
values = dict(line.split("=", 1) for line in credentials.read_text().splitlines() if line)
pat = values.get("QIANJI_MCP_ADMIN_PAT", "")
if not pat.startswith("mt_pat_") or len(pat) < 39 or any(c.isspace() for c in pat):
    raise SystemExit("Invalid private upstream credential")

env = os.environ.copy()
for key in list(env):
    if key.startswith("QIANJI_"):
        del env[key]
env.update({
    "QIANJI_MCP_ADMIN_PAT": pat,
    "QIANJI_MCP_HOST": "127.0.0.1",
    "QIANJI_MCP_PORT": "3000",
    "QIANJI_MCP_PUBLIC_URL": "http://127.0.0.1:3000",
    "QIANJI_MCP_DATABASE_PATH": str(PRIVATE / "qianji.db"),
    "QIANJI_MCP_API_SERVER": "https://api.qianjiapp.com",
})
bun = ROOT / ".runtime/bun/bun"
os.chdir(ROOT / "vendor/qianji-mcp")
os.execve(bun, [str(bun), "src/http-server.ts"], env)
