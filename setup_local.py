#!/usr/bin/env python3
"""Create private local credentials; never print them or contact Qianji."""

import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parent
private = root / ".private"
private.mkdir(mode=0o700, exist_ok=True)
os.chmod(private, 0o700)
pat_path = private / "upstream.pat"
env_path = private / "upstream.env"
if pat_path.exists():
    pat = pat_path.read_text(encoding="utf-8").strip()
    if not pat.startswith("mt_pat_") or len(pat) < 39 or any(c.isspace() for c in pat):
        raise SystemExit("现有凭据格式无效，已停止。未覆盖任何文件。")
else:
    pat = "mt_pat_" + secrets.token_hex(32)
    with pat_path.open("x", encoding="utf-8") as stream:
        os.chmod(pat_path, 0o600)
        stream.write(pat + "\n")
expected = f"QIANJI_MCP_ADMIN_PAT={pat}\nQIANJI_MCP_PUBLIC_URL=http://127.0.0.1:3000\n"
if env_path.exists():
    if env_path.read_text(encoding="utf-8") != expected:
        raise SystemExit("容器凭据文件与连接凭据不一致，已停止。未覆盖现有文件。")
else:
    with env_path.open("x", encoding="utf-8") as stream:
        os.chmod(env_path, 0o600)
        stream.write(expected)
os.chmod(pat_path, 0o600)
os.chmod(env_path, 0o600)
print("本机配置已就绪。凭据未显示；下一步启动容器并运行 --check。")
