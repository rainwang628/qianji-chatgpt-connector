#!/bin/zsh
set -eu
QIANJI_PROJECT_DIR="${0:A:h}"
exec "$QIANJI_PROJECT_DIR/.venv/bin/python" "$QIANJI_PROJECT_DIR/macos/service.py" start --component tunnel
