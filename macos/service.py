#!/usr/bin/env python3
"""Control this project's launchd service without putting secrets in a plist."""

import argparse
import json
import os
import plistlib
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
LABEL = "com.rain.qianji-readonly.upstream"
DOMAIN = f"gui/{os.getuid()}"
TARGET = f"{DOMAIN}/{LABEL}"
RUNNER = ROOT / "macos/run_upstream.py"
PLIST = ROOT / ".private" / f"{LABEL}.plist"
COMPONENT = "upstream"
PROGRAM_ARGS = [str(ROOT / ".venv/bin/python"), str(RUNNER)]


def job():
    result = subprocess.run(["/bin/launchctl", "print", TARGET], capture_output=True, text=True)
    if result.returncode:
        return None
    if str(RUNNER) not in result.stdout:
        raise SystemExit("A different service already uses this label; refusing to change it")
    return result.stdout


def authenticated_boundary():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open("http://127.0.0.1:3000/mcp", timeout=2)
    except urllib.error.HTTPError as error:
        return error.code == 401
    except OSError:
        return None
    return False


def readiness():
    if COMPONENT == "upstream":
        return authenticated_boundary()
    file = ROOT / ".private/tunnel-health.url"
    if not file.exists():
        return False
    base = file.read_text().strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.path:
        raise SystemExit("Refusing a non-loopback tunnel health URL")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base + "/readyz", timeout=2) as response:
            return response.status == 200
    except urllib.error.HTTPError:
        return False
    except OSError:
        return None


def status():
    state = job()
    details = [line.strip() for line in (state or "").splitlines()
               if line.strip().startswith(("state =", "pid =", "last exit code ="))]
    boundary = readiness()
    print(json.dumps({"component": COMPONENT, "service_loaded": state is not None, "details": details,
                      ("unauthenticated_mcp_rejected" if COMPONENT == "upstream" else "tunnel_ready"): boundary,
                      "probe_unavailable": boundary is None,
                      "checks_account_data": False}, ensure_ascii=False, indent=2))


def start():
    os.umask(0o077)
    if job() is None:
        if COMPONENT == "upstream":
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", 3000)) == 0:
                    raise SystemExit("Port 3000 is already occupied; no other service was changed")
        elif not (ROOT / ".private/tunnel-profiles/qianji-readonly.yaml").exists():
            raise SystemExit("Create the private tunnel profile before starting it")
        PLIST.parent.mkdir(mode=0o700, exist_ok=True)
        logs = ROOT / "logs"
        logs.mkdir(mode=0o700, exist_ok=True)
        settings = {
            "Label": LABEL,
            "ProgramArguments": PROGRAM_ARGS,
            "WorkingDirectory": str(ROOT),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "Umask": 0o077,
            "StandardOutPath": str(logs / f"{COMPONENT}.log"),
            "StandardErrorPath": str(logs / f"{COMPONENT}-errors.log"),
            "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TZ": "Asia/Shanghai"},
        }
        PLIST.write_bytes(plistlib.dumps(settings))
        PLIST.chmod(0o600)
        subprocess.run(["/bin/launchctl", "bootstrap", DOMAIN, str(PLIST)], check=True)
    else:
        subprocess.run(["/bin/launchctl", "kickstart", TARGET], check=True)
    for _ in range(150):
        if readiness():
            status()
            return
        time.sleep(0.2)
    status()
    raise SystemExit("Service did not become ready; inspect this project's private local logs")


def stop():
    if job() is not None:
        subprocess.run(["/bin/launchctl", "bootout", TARGET], check=True)
    status()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "stop", "status"])
    parser.add_argument("--component", choices=["upstream", "tunnel"], default="upstream")
    args = parser.parse_args()
    COMPONENT = args.component
    if COMPONENT == "tunnel":
        LABEL = "com.rain.qianji-readonly.tunnel"
        TARGET = f"{DOMAIN}/{LABEL}"
        RUNNER = ROOT / "macos/secure_tunnel.ts"
        PLIST = ROOT / ".private" / f"{LABEL}.plist"
        PROGRAM_ARGS = [str(ROOT / ".runtime/bun/bun"), str(RUNNER), "run"]
    {"start": start, "stop": stop, "status": status}[args.action]()
