# RoboSub Vision — 2026 Hydromeda Camera Feed

Live dual-camera streaming from the ROV with YOLO-based crab detection for the 2026 MATE ROV Competition.

## Architecture

| Component | Device | Description |
|-----------|--------|-------------|
| `rov/stream.py` | Raspberry Pi 4B / CM5 | Streams UC-684 cameras via RTP H264, serves Pi Camera stills over TCP |
| `topside/interface.py` | Topside laptop | PySide6 GUI with dual video feeds, YOLO26 crab detection, PS4 controller |

---

## ROV Setup (Raspberry Pi)

### Prerequisites

```bash
sudo apt install gstreamer1.0-tools gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
macOS:

sudo pip3 install opencv-python numpy --break-system-packages
```

`picamera2` is pre-installed on Raspberry Pi OS. No additional install needed.

### Running

```bash
sudo python3 stream.py <topside_ip>
```

Find your topside IP with `ip addr show` — look for your network interface (e.g. `192.168.2.1`).

**Optional flags:**
```bash
sudo python3 stream.py <topside_ip> --photo-port 5002
```

### Firewall (topside)

Open the required ports on the topside machine before running:

```bash
sudo ufw allow 5000/udp   # UC-684 camera 1
sudo ufw allow 5001/udp   # UC-684 camera 2
sudo ufw allow 5002/tcp   # Pi Camera stills
```

---

## Topside Setup

### Python version requirement

**Python 3.12 is required.** PyGObject's GStreamer bindings are broken on Python 3.14 (current Arch default). Use pyenv if your system Python is 3.14+:

```bash
pyenv install 3.12.9
pyenv local 3.12.9
python -m venv .venv
```

---

### Linux — Ubuntu / Debian

```bash
sudo apt install \
  gstreamer1.0-tools gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  libgirepository-2.0-dev libcairo2-dev \
  pkg-config python3-dev python3-gi python3-gst-1.0

pip install PySide6 opencv-python numpy pygame PyGObject ultralytics
```

### Linux — Arch

```bash
sudo pacman -S gstreamer gst-plugins-good gst-plugins-bad \
  gst-plugins-ugly gst-libav python-gobject gst-python pyenv

pyenv install 3.12.9
pyenv local 3.12.9
python -m venv .venv
source .venv/bin/activate.fish   # Fish shell
# or: source .venv/bin/activate  # Bash/Zsh

pip install PySide6 opencv-python numpy pygame PyGObject ultralytics
```

### macOS

```bash
brew install gstreamer gst-plugins-good gst-plugins-bad \
  gst-plugins-ugly gst-libav gobject-introspection

pip install PySide6 opencv-python numpy pygame PyGObject ultralytics
```

### Windows

Windows is not recommended for this setup due to PyGObject compatibility issues. If required:

1. Install [GStreamer](https://gstreamer.freedesktop.org/download/) (MSVC 64-bit runtime + development)
2. Add `C:\gstreamer\1.0\msvc_x86_64\bin` to your system PATH
3. Install [Python 3.12](https://python.org)
4. `pip install PySide6 opencv-python numpy pygame ultralytics`

> **Note:** PyGObject (required for GStreamer Python bindings) cannot be pip-installed on Windows. The application will run but video streaming will not work without a separate PyGObject installation.

---

### Running (topside)

Activate your venv first:

```bash
source .venv/bin/activate.fish   # Fish
source .venv/bin/activate        # Bash/Zsh
```

Place your trained YOLO model at `topside/crab.pt`, then:

```bash
python interface.py
```

---

## YOLO Crab Detection

The interface includes YOLO26-based detection for the MATE 2026 Task 2.1 (Mitigate Invasive Species). Only **European Green Crabs** are boxed and counted, per competition rules.

To train or retrain the model:

```bash
# Export dataset from Roboflow as YOLOv8 format, extract to crab-dataset/
yolo detect train data=crab-dataset/data.yaml model=yolo26n.pt epochs=100 imgsz=640 device=0
cp runs/detect/train/weights/best.pt crab.pt
```

---

## Controls

| Control | Action |
|---------|--------|
| 🦀 YOLO button | Toggle crab detection on/off |
| 🌊 Preprocess button | Toggle underwater color correction |
| PS4 Circle (○) | Trigger Pi Camera still capture |
| Capture Frame button | Save current video frame to disk |

Captured frames are saved to `topside/captured_frames/output_<timestamp>/`.

---

## Hardware

| Component | Details |
|-----------|---------|
| ROV computer | Raspberry Pi 4B (testing) / CM5 (competition) |
| Streaming cameras | Blue Robotics UC-684 × 2 (MJPEG, 1080p @ 30fps) |
| Photo camera | Raspberry Pi Camera Module 3 (12MP stills) |
| Controller | PS4 DualShock 4 |
| Tether | Gigabit ethernet tether → router |