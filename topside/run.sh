#!/bin/bash
# Launch the vision interface with the project's virtual environment (created by install_script.sh).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
if [ -x .venv/bin/python ]; then
    exec .venv/bin/python interface.py "$@"
fi
exec python3 interface.py "$@"
