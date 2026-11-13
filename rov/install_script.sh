#!/bin/bash
# One-time setup for the ROV camera sender (stream.py) on the Raspberry Pi 5 / CM5.
#
#   ./install_script.sh             # GStreamer + H.264 encoder + camera access
#   ./install_script.sh --service   # also stream on every boot (rov-video.service)
#
# AlmaLinux 10 (primary) and Raspberry Pi OS / Debian (fallback). Run as your normal user.
# Re-running is safe.

set -eo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE=false
for arg in "$@"; do
    case "$arg" in
        --service) SERVICE=true ;;
        *) echo "Unknown option: $arg"; exit 1 ;;
    esac
done
[[ $EUID -eq 0 ]] && { echo "Run as your normal user, not root/sudo. The script uses sudo itself."; exit 1; }

# shellcheck disable=SC1091
. /etc/os-release
echo "Host: ${PRETTY_NAME:-unknown} ($(uname -m))"

if command -v dnf >/dev/null 2>&1; then
    # ── AlmaLinux / RHEL ──
    sudo dnf install -y python3 gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good v4l-utils
    # x264enc is in gstreamer1-plugins-ugly from RPM Fusion (needs EPEL + CRB); OpenH264 is the fallback
    if ! gst-inspect-1.0 --exists x264enc 2>/dev/null; then
        sudo dnf install -y dnf-plugins-core epel-release || true
        sudo dnf config-manager --set-enabled crb || true
        if ! rpm -q rpmfusion-free-release >/dev/null 2>&1; then
            sudo dnf install -y \
                "https://mirrors.rpmfusion.org/free/el/rpmfusion-free-release-$(rpm -E %rhel).noarch.rpm" || true
        fi
        sudo dnf install -y gstreamer1-plugins-ugly || sudo dnf install -y gstreamer1-plugin-openh264 || true
    fi
elif command -v apt-get >/dev/null 2>&1; then
    # ── Raspberry Pi OS / Debian ──
    sudo apt-get update
    sudo apt-get install -y python3 gstreamer1.0-tools gstreamer1.0-plugins-base \
        gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly v4l-utils
else
    echo "Unsupported OS: install GStreamer (good + ugly plugins) and v4l-utils manually."
    exit 1
fi

if gst-inspect-1.0 --exists x264enc 2>/dev/null; then
    echo "H.264 encoder: x264enc"
elif gst-inspect-1.0 --exists openh264enc 2>/dev/null; then
    echo "H.264 encoder: openh264enc"
else
    echo "WARNING: no H.264 encoder found. Set ROV_VIDEO_CODEC=mjpeg in video.env,"
    echo "         and start the topside interface with ROV_VIDEO_CODEC=mjpeg ./run.sh"
fi

echo "Adding $USER to the video group (camera access; takes effect after logging out and back in)"
sudo usermod -aG video "$USER"
chmod +x "$SCRIPT_DIR/run.sh" "$SCRIPT_DIR/stream.py"

if $SERVICE; then
    if systemctl list-unit-files rov-video.service >/dev/null 2>&1 \
       && grep -q "rov_video_sender.py" /etc/systemd/system/rov-video.service 2>/dev/null; then
        echo "Note: replacing the rov-video.service installed by rov-onboard (only one sender can use the camera)."
    fi
    sudo tee /etc/systemd/system/rov-video.service >/dev/null <<UNIT
[Unit]
Description=ROV camera video sender (UC-684 -> topside RTP/UDP 5000)
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$USER
SupplementaryGroups=video
WorkingDirectory=$SCRIPT_DIR
EnvironmentFile=-$SCRIPT_DIR/video.env
ExecStart=/usr/bin/python3 $SCRIPT_DIR/stream.py
# Video yields CPU to flight control (joy_to_mavlink, MAVROS) when the Pi is busy
Nice=5
CPUWeight=50
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable rov-video.service
    echo "  Start now:  sudo systemctl start rov-video"
    echo "  Logs:       journalctl -u rov-video -f"
fi

cat <<EOF

Done. Next:
  1. Log out and back in once (video group).
  2. ./run.sh --list         # camera found? supports MJPG 1920x1080 30 fps?
  3. ./run.sh                # streams to ROV_TOPSIDE_IP in video.env (default 192.168.1.69)
     Wi-Fi testing:  ./run.sh --host <laptop Wi-Fi IP>
EOF
