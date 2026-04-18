#!/bin/bash

set -euo pipefail

# RoboSub Vision — topside install script
# Supports: Ubuntu/Debian, Arch Linux
# Requires: Python 3.12 (use pyenv if your system Python is newer)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Privilege check ───────────────────────────────────────────────────────────

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    if ! command -v sudo >/dev/null 2>&1; then
        echo "This script needs root privileges but sudo is not installed."
        echo "Please run as root."
        exit 1
    fi
    echo "Requesting admin privileges (sudo)..."
    sudo -v
    SUDO="sudo"
fi

# ── Detect distro ─────────────────────────────────────────────────────────────

if [ -f /etc/os-release ]; then
    . /etc/os-release
else
    echo "Cannot detect Linux distribution: /etc/os-release not found"
    exit 1
fi

# ── System dependencies ───────────────────────────────────────────────────────

if [[ "${ID:-}" == "arch" ]] || [[ "${ID_LIKE:-}" == *"arch"* ]]; then
    echo "Detected Arch-based distribution. Installing system dependencies..."
    $SUDO pacman -Syu --noconfirm
    $SUDO pacman -S --needed --noconfirm \
        python \
        python-gobject \
        gobject-introspection \
        gstreamer \
        gst-python \
        gst-plugins-base \
        gst-plugins-good \
        gst-plugins-bad \
        gst-plugins-ugly \
        gst-libav \
        pyenv

    echo ""
    echo "NOTE: Your system Python may be 3.14+ which breaks PyGObject."
    echo "If you see GStreamer signal errors, install Python 3.12 via pyenv:"
    echo "  pyenv install 3.12.9 && pyenv local 3.12.9"
    echo ""

elif [[ "${ID:-}" == "ubuntu" ]] || [[ "${ID:-}" == "debian" ]] || [[ "${ID_LIKE:-}" == *"debian"* ]]; then
    echo "Detected Debian/Ubuntu-based distribution. Installing system dependencies..."
    $SUDO apt update && $SUDO apt upgrade -y
    $SUDO apt install -y \
        python3-pip \
        python3-dev \
        python3-gi \
        python3-gi-cairo \
        python3-gst-1.0 \
        gir1.2-gtk-3.0 \
        libgirepository-2.0-dev \
        libcairo2-dev \
        pkg-config \
        gstreamer1.0-tools \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly \
        gstreamer1.0-libav \
        libgstreamer1.0-dev \
        libgstreamer-plugins-base1.0-dev

else
    echo "Unsupported distribution: ${ID:-unknown}"
    echo "Please install GStreamer and Python build dependencies manually."
    echo "See README.md for instructions."
    exit 1
fi

# ── Python version check ──────────────────────────────────────────────────────

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_MAJOR=$(echo "$PYTHON_VERSION" | cut -d. -f1)
PYTHON_MINOR=$(echo "$PYTHON_VERSION" | cut -d. -f2)

if [ "$PYTHON_MAJOR" -eq 3 ] && [ "$PYTHON_MINOR" -ge 14 ]; then
    echo ""
    echo "WARNING: Python $PYTHON_VERSION detected."
    echo "PyGObject GStreamer bindings are broken on Python 3.14+."
    echo "Please use Python 3.12 via pyenv before continuing:"
    echo ""
    echo "  pyenv install 3.12.9"
    echo "  pyenv local 3.12.9"
    echo "  python -m venv .venv"
    echo "  source .venv/bin/activate  # or .venv/bin/activate.fish"
    echo ""
    echo "Then re-run this script."
    exit 1
fi

echo "Python $PYTHON_VERSION — OK"

# ── Python packages ───────────────────────────────────────────────────────────

echo "Installing Python packages..."
python3 -m pip install -r "$SCRIPT_DIR/requirements.txt"

# ── Firewall ──────────────────────────────────────────────────────────────────

if command -v ufw >/dev/null 2>&1; then
    UFW_STATUS=$($SUDO ufw status | head -1)
    if [[ "$UFW_STATUS" == *"active"* ]]; then
        echo "UFW is active. Opening required ports..."
        $SUDO ufw allow 5000/udp   # UC-684 camera 1
        $SUDO ufw allow 5001/udp   # UC-684 camera 2
        $SUDO ufw allow 5002/tcp   # Pi Camera stills
        echo "Ports 5000/udp, 5001/udp, 5002/tcp opened."
    fi
fi

# ── Verify GStreamer bindings ─────────────────────────────────────────────────

echo "Verifying GStreamer Python bindings..."
python3 -c "
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstApp', '1.0')
from gi.repository import Gst, GstApp
Gst.init(None)
print('GStreamer:', Gst.version_string())
print('GstApp OK')
"

echo ""
echo "Installation complete."
echo "run python3 interface.py"