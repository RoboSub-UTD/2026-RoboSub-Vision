# ROV vision (onboard): camera sender

Streams the UC-684 USB camera from the Raspberry Pi 5 / CM5 to the topside vision interface
(`../topside`) as RTP/H.264 on UDP 5000. Not ROS: it uses the system Python and GStreamer, so a
ROS problem can't take the pilot's video down. Photos for photogrammetry go through ROS
(rov-onboard `rov_photogrammetry`), not this folder.

## Setup (AlmaLinux 10; Raspberry Pi OS also works)

```bash
./install_script.sh              # GStreamer, H.264 encoder, video group
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

| Variable | Default | |
|---|---|---|
| `ROV_TOPSIDE_IP` | `192.168.1.69` | laptop on the tether |
| `ROV_CAMERAS` | `auto` | first USB camera → 5000; or pin `DEVICE:PORT[:WxH@FPS]` from `--list` |
| `ROV_VIDEO_CODEC` | `h264` | `mjpeg` sends the camera's JPEGs without re-encoding (topside needs `ROV_VIDEO_CODEC=mjpeg` too) |
| `ROV_VIDEO_BITRATE` | `12000` | kbit/s, H.264 only |
| `ROV_VIDEO_SIZE` | `1920x1080@30` | lower to `1280x720@30` if the Pi's CPU is too busy |

The Pi 5 has no hardware H.264 encoder, so the stream is encoded in software. Check `top` while
the ROS stack is running too. If CPU is tight, lower the size or use MJPEG.

## Note

rov-onboard has the same sender (`video/rov_video_sender.py`) with its own `rov-video.service`.
Install the service from one place only; both use the same name, so the last install wins.
