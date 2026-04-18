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
  gstreamer1.0-libav python3-dev

sudo pip3 install -r rov/requirements.txt --break-system-packages
```

`picamera2` is pre-installed on Raspberry Pi OS — no additional install needed.

### Running

```bash
sudo python3 stream.py <topside_ip>
```

Find your topside IP with `ip addr show`. Example: `sudo python3 stream.py 192.168.2.1`

**Optional flags:**
```bash
sudo python3 stream.py <topside_ip> --photo-port 5002
```

### Second camera

When both UC-684s are connected, check their device nodes:
```bash
ls /dev/video*
```

Then uncomment the `t1` lines in `stream.py` and update the index if needed.

---

## Topside Setup

### Python version requirement

**Python 3.12 is required.** PyGObject's GStreamer bindings are broken on Python 3.14 (current Arch default). Use pyenv if your system Python is 3.14+:

```bash
pyenv install 3.12.9
pyenv local 3.12.9
python -m venv .venv
```

### Firewall

Open required ports before running — **this is required even if UFW reports inactive:**

```bash
sudo ufw allow 5000/udp   # UC-684 camera 1
sudo ufw allow 5001/udp   # UC-684 camera 2
sudo ufw allow 5002/tcp   # Pi Camera stills
```

The install script handles this automatically if UFW is active.

---

### Linux — Ubuntu / Debian

```bash
sudo apt install \
  gstreamer1.0-tools gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
  gstreamer1.0-libav \
  libgirepository-2.0-dev libcairo2-dev \
  pkg-config python3-dev python3-gi python3-gst-1.0

pip install -r topside/requirements.txt
```

Or use the install script:
```bash
bash topside/install_script.sh
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

pip install -r topside/requirements.txt
```

### macOS

```bash
brew install gstreamer gst-plugins-good gst-plugins-bad \
  gst-plugins-ugly gst-libav gobject-introspection

pip install -r topside/requirements.txt
```

### Windows

Not recommended — PyGObject cannot be pip-installed on Windows. If required:

1. Install [GStreamer](https://gstreamer.freedesktop.org/download/) (MSVC 64-bit runtime + development)
2. Add `C:\gstreamer\1.0\msvc_x86_64\bin` to your system PATH
3. Install [Python 3.12](https://python.org)
4. `pip install PySide6 opencv-python numpy pygame ultralytics`

Video streaming will not work without a separate PyGObject installation.

---

### Running (topside)

Activate your venv, place `crab.pt` in the `topside/` folder, then:

```bash
source .venv/bin/activate.fish   # Fish
# or: source .venv/bin/activate  # Bash/Zsh

python interface.py
```

---

## YOLO Crab Detection

Detects European Green Crabs for MATE 2026 Task 2.1 (Mitigate Invasive Species). Only Green Crabs receive bounding boxes and are counted — Rock and Jonah crabs are intentionally excluded per competition rules.

To retrain the model:

```bash
# Export dataset from Roboflow as YOLOv8 format, extract to crab-dataset/
yolo detect train data=crab-dataset/data.yaml model=yolo26n.pt epochs=100 imgsz=640 device=0
cp runs/detect/train/weights/best.pt topside/crab.pt
```

---

## Controls

| Control | Action |
|---------|--------|
| 🦀 YOLO button | Toggle crab detection on/off |
| 🌊 Preprocess button | Toggle underwater color correction (red boost + CLAHE) |
| PS4 Circle (○) | Trigger Pi Camera Module 3 still capture |
| Capture Frame button | Save current video frame to disk |

Captured frames saved to `topside/captured_frames/output_<timestamp>/`.

---

## Hardware

| Component | Details |
|-----------|---------|
| ROV computer | Raspberry Pi 4B (testing) / CM5 (competition) |
| Streaming cameras | Blue Robotics UC-684 × 2 (MJPEG, 1080p @ 30fps) |
| Photo camera | Raspberry Pi Camera Module 3 (12MP stills) |
| Controller | PS4 DualShock 4 |
| Tether | Gigabit ethernet tether → router |