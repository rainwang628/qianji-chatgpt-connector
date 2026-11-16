#!/usr/bin/env python3
"""Human-only, hidden terminal input; the key travels to Keychain over stdin."""

import getpass
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if not sys.stdin.isatty():
    raise SystemExit("请由本人在本机 Terminal 中运行；拒绝通过非交互输入接收密钥。")
print("请输入刚为钱迹隧道创建的 OpenAI 运行密钥。输入不会显示，也不写入文件。")
key = getpass.getpass("OpenAI runtime API key（隐藏输入）: ").strip()
if not key.startswith("sk-") or len(key) < 20 or any(c.isspace() for c in key):
    raise SystemExit("密钥格式无效，未保存。")
result = subprocess.run([str(ROOT / ".runtime/bun/bun"), str(ROOT / "macos/secure_tunnel.ts"), "store"],
                        input=key, text=True, capture_output=True)
key = ""
if result.returncode:
    raise SystemExit("保存到钥匙串失败。请检查 macOS 的钥匙串授权；没有显示或落盘密钥。")
print("密钥已保存到 macOS 钥匙串；可以回到对话继续验证隧道。")
