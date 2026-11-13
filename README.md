# ROV Vision (AlmaLinux 10)

| Folder | Runs on | What it does |
|---|---|---|
| `topside/` | Topside laptop | Vision interface: live camera feed (UDP 5000), YOLO crab detection, photo panel for ROS captures |
| `rov/` | Raspberry Pi 5 / CM5 | Camera sender: streams the UC-684 USB camera to the laptop (RTP over UDP) |

Video does not go through ROS, so a ROS problem can't take the pilot's video down.

## Setup

```bash
# Pi
cd rov && ./install_script.sh --service     # GStreamer, camera access, Wi-Fi power saving off, start on boot
# edit rov/video.env: ROV_TOPSIDE_IP (Wi-Fi testing: laptop's Wi-Fi IP; tether: 192.168.1.69)

# Laptop
cd topside && ./install_script.sh && ./run.sh
```
Log out and back in once on the Pi after installing (video group).

## Recommended settings (`rov/video.env`)

| | Size | Codec | `ROV_JPEG_QUALITY` |
|---|---|---|---|
| Tether (competition) | `1920x1080@30` | `mjpeg` | `0` (camera's own frames, ~45 Mbit/s, ~7% CPU) |
| Wi-Fi testing | `1920x1080@30` | `mjpeg` | `0` (clean once the laptop isn't background-scanning) |
| Weak link | `1280x720@30` | `mjpeg` | `70` (~17 Mbit/s, ~25% of a core) |

The topside's `run.sh` defaults to `ROV_VIDEO_CODEC=mjpeg`; it must match the Pi.

## Lag spikes or stutter

Run both while streaming and compare:
```bash
./pi_check.sh 60                                  # Pi (rov/): Wi-Fi, CPU, temperature, USB, camera settings
python3 video_check.py capture --seconds 60       # laptop (topside/), with the interface running
```
Lessons from testing: the camera disconnects if its USB plug is loose (secure it); Wi-Fi power saving and the
laptop's periodic Wi-Fi scans (GNOME Location Services / Settings Wi-Fi page) cause regular lag spikes;
the Pi 5 needs active cooling (it reached 85 C while streaming).
