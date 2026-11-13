#!/bin/bash
# Stream the ROV camera to the topside. Settings: video.env (flags override, e.g. ./run.sh --host 10.6.18.143)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/video.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/video.env"
    set +a
fi
exec python3 "$SCRIPT_DIR/stream.py" "$@"
