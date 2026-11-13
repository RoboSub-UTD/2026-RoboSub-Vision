# ROV vision (onboard): camera sender

Streams the UC-684 USB camera from the Raspberry Pi 5 / CM5 to the topside vision interface
(`../topside`) over RTP on UDP 5000 (MJPEG by default). Not ROS: it uses the system Python and GStreamer, so a
ROS problem can't take the pilot's video down. Photos for photogrammetry go through ROS
(rov-onboard `rov_photogrammetry`), not this folder.

## Setup (AlmaLinux 10; Raspberry Pi OS also works)

```bash
./install_script.sh              # GStreamer, encoders/parsers, video group, Wi-Fi power saving off
./install_script.sh --service    # same, plus stream automatically on boot
```
Log out and back in once afterwards.

## Run

```bash
./run.sh --list                  # find the camera, show its formats
./run.sh                         # stream to the topside (settings in video.env)
./run.sh --host 10.6.18.143      # Wi-Fi testing: send to the laptop's Wi-Fi IP
./run.sh --dry-run               # print the GStreamer pipeline
```
Same usage as the original script also works: `python3 stream.py 192.168.1.69`.

On the topside: `./run.sh` in `../topside`. The video panel listens on UDP 5000.

## Settings (`video.env`)

| Variable | In the file | |
|---|---|---|
| `ROV_TOPSIDE_IP` | `10.6.18.143` | the laptop: its Wi-Fi IP for testing, `192.168.1.69` on the tether |
| `ROV_CAMERAS` | `auto` | first USB camera → 5000; or pin `DEVICE:PORT` from `--list` |
| `ROV_VIDEO_CODEC` | `mjpeg` | `mjpeg` sends the camera's JPEG frames (best picture, ~7% CPU); `camera-h264` the camera's own H.264 (~1.4 Mbit/s, softer); `h264` re-encodes with x264 (CPU heavy, not recommended on the Pi 5). The topside's `run.sh` must match (defaults to mjpeg) |
| `ROV_VIDEO_SIZE` | `1920x1080@30` | the camera also does `1280x720@30` |
| `ROV_JPEG_QUALITY` | `0` | mjpeg only: 0 = camera's frames (~45 Mbit/s at any size). 1-100 re-compresses on the Pi; `1280x720@30` + `70` ≈ 17 Mbit/s for a weak link |
| `ROV_CAMERA_CONTROLS` | `exposure_dynamic_framerate=0,power_line_frequency=2` | applied at every camera start: steady 30 fps, 60 Hz anti-flicker |
| `ROV_VIDEO_BITRATE` | `12000` | kbit/s, `h264` mode only |

Only one sender can use the camera at a time; a second copy exits and says so (stop the service
first: `sudo systemctl stop rov-video`).

## Lag spikes or stutter

Run both while the camera streams, then compare:

```bash
./pi_check.sh 60                                   # on the Pi: Wi-Fi signal/retries/power saving, CPU, USB, camera
python3 video_check.py capture --seconds 60        # on the laptop, in ../topside (interface running)
```
`video_check.py` splits the problem into packet loss, network delay spikes, uneven frames from the ROV,
and laptop buffer overflows, and writes a timeline chart. It needs tcpdump (`sudo dnf install -y tcpdump`).

## Note

rov-onboard has the same sender (`video/rov_video_sender.py`) with its own `rov-video.service`.
Install the service from one place only; both use the same name, so the last install wins.
