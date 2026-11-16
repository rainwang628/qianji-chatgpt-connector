#!/bin/zsh
set -eu
QIANJI_PROJECT_DIR="${0:A:h}"
"$QIANJI_PROJECT_DIR/.venv/bin/python" "$QIANJI_PROJECT_DIR/macos/service.py" start
if [[ -f "$QIANJI_PROJECT_DIR/.private/tunnel-profiles/qianji-readonly.yaml" ]]; then
  exec "$QIANJI_PROJECT_DIR/.venv/bin/python" "$QIANJI_PROJECT_DIR/macos/service.py" start --component tunnel
fi
