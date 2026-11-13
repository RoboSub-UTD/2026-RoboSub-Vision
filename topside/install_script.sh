#!/bin/bash

set -euo pipefail

# RoboSub Vision — topside install script
# Supports: Ubuntu/Debian, Arch Linux, AlmaLinux/RHEL 10 (and Rocky/Fedora)
# Requires: Python 3.12 (AlmaLinux 10's system Python; use pyenv elsewhere if yours is newer)
# Python packages go into ./.venv; run the app with ./run.sh

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

elif [[ "${ID:-}" == "almalinux" ]] || [[ "${ID:-}" == "rhel" ]] || [[ "${ID:-}" == "rocky" ]] || [[ "${ID:-}" == "fedora" ]] || [[ "${ID_LIKE:-}" == *"rhel"* ]]; then
    echo "Detected RHEL-based distribution ($PRETTY_NAME). Installing system dependencies..."
    if [[ "${ID:-}" != "fedora" ]]; then
        # CRB + EPEL, then RPM Fusion free for gstreamer1-libav (avdec_h264); RHEL doesn't ship it
        $SUDO dnf install -y dnf-plugins-core epel-release
        $SUDO dnf config-manager --set-enabled crb
        if ! rpm -q rpmfusion-free-release >/dev/null 2>&1; then
            $SUDO dnf install -y --nogpgcheck \
                "https://mirrors.rpmfusion.org/free/el/rpmfusion-free-release-$(rpm -E %rhel).noarch.rpm"
        fi
    elif ! rpm -q rpmfusion-free-release >/dev/null 2>&1; then
        $SUDO dnf install -y \
            "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm"
    fi
    $SUDO dnf install -y \
        python3 \
        python3-devel \
        python3-pip \
        gcc \
        pkgconf-pkg-config \
        gobject-introspection-devel \
        cairo-devel \
        cairo-gobject-devel \
        gstreamer1 \
        gstreamer1-plugins-base \
        gstreamer1-plugins-good \
        gstreamer1-plugins-bad-free \
        gstreamer1-plugins-ugly-free \
        tcpdump iw   # video_check.py (stream analysis), Wi-Fi power-save control
    # H.264 decoder: gst-libav from RPM Fusion; OpenH264 as a fallback
    $SUDO dnf install -y gstreamer1-libav || \
        $SUDO dnf install -y gstreamer1-plugin-openh264 || \
        echo "WARNING: no H.264 decoder package installed; video feeds will fall back to decodebin"
    # Qt 6 needs libxcb-cursor if it ever falls back to X11/XWayland
    $SUDO dnf install -y xcb-util-cursor || true

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

# ── Virtual environment ───────────────────────────────────────────────────────
# Keeps pip packages out of the system Python (required on Ubuntu 24.04+, cleaner everywhere).
# --system-site-packages is NOT used; PyGObject is built into the venv against system GStreamer.

if [ -z "${VIRTUAL_ENV:-}" ]; then
    if [ ! -d "$SCRIPT_DIR/.venv" ]; then
        echo "Creating virtual environment in $SCRIPT_DIR/.venv ..."
        python3 -m venv "$SCRIPT_DIR/.venv"
    fi
    PY="$SCRIPT_DIR/.venv/bin/python"
else
    PY="python"
fi
"$PY" -m pip install --upgrade pip

# ── Python packages ───────────────────────────────────────────────────────────

echo "Installing Python packages..."
"$PY" -m pip install -r "$SCRIPT_DIR/requirements.txt"

# ultralytics depends on the full opencv-python, which bundles its own Qt and can break
# PySide6 windows. Keep only the headless build.
if "$PY" -m pip show opencv-python >/dev/null 2>&1; then
    echo "Replacing opencv-python with opencv-python-headless (avoids Qt conflict with PySide6)..."
    "$PY" -m pip uninstall -y opencv-python
    "$PY" -m pip install --force-reinstall --no-deps "opencv-python-headless>=4.10.0"
fi

# ── Firewall ──────────────────────────────────────────────────────────────────

if command -v ufw >/dev/null 2>&1; then
    UFW_STATUS=$($SUDO ufw status | head -1)
    if [[ "$UFW_STATUS" == *"active"* ]]; then
        echo "UFW is active. Opening required ports..."
        $SUDO ufw allow 5000/udp   # UC-684 camera 1
        $SUDO ufw allow 5001/udp   # UC-684 camera 2
        echo "Ports 5000/udp, 5001/udp opened."
    fi
fi

if command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active --quiet firewalld; then
    echo "firewalld is active. Opening required ports..."
    $SUDO firewall-cmd --permanent --add-port=5000-5001/udp   # UC-684 cameras 1 and 2 (RTP/H.264)
    $SUDO firewall-cmd --reload
    echo "Ports 5000-5001/udp opened."
fi

# ── Larger UDP receive buffer (1080p MJPEG video arrives in big bursts) ───────
if [[ "$(uname)" == "Linux" ]]; then
    echo "net.core.rmem_max = 8388608" | $SUDO tee /etc/sysctl.d/90-rov-video.conf >/dev/null
    $SUDO sysctl -q -p /etc/sysctl.d/90-rov-video.conf || true
fi

# ── Verify GStreamer bindings ─────────────────────────────────────────────────

echo "Verifying GStreamer Python bindings..."
"$PY" -c "
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstApp', '1.0')
from gi.repository import Gst, GstApp
Gst.init(None)
print('GStreamer:', Gst.version_string())
print('GstApp OK')
dec = next((n for n in ('avdec_h264', 'nvh264dec', 'openh264dec') if Gst.ElementFactory.find(n)), None)
print('H.264 decoder:', dec or 'NONE (install gstreamer1-libav / gstreamer1.0-libav)')
"

echo "Checking PyTorch / CUDA for YOLO..."
"$PY" -c "
import torch
print('PyTorch', torch.__version__, '| CUDA available:', torch.cuda.is_available(),
      '|', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'YOLO will run on CPU')
" || echo "PyTorch check failed (YOLO will be disabled)"

echo ""
echo "Installation complete."
echo "Run: ./run.sh   (or .venv/bin/python interface.py)"