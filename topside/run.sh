#!/bin/bash
# Launch the vision interface with the project's virtual environment (created by install_script.sh).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
# Video format from the ROV; must match ROV_VIDEO_CODEC in the ROV's video.env (h264, camera-h264 or mjpeg)
export ROV_VIDEO_CODEC="${ROV_VIDEO_CODEC:-mjpeg}"
if [ -x .venv/bin/python ]; then
    exec .venv/bin/python interface.py "$@"
fi
exec python3 interface.py "$@"
