#!/usr/bin/env python3
"""
ROV camera video sender: streams the UC-684 USB camera to the topside over RTP/UDP.

Rewrite of the original stream.py. Not a ROS node on purpose: video goes straight to the topside
vision interface (interface.py listens on UDP 5000), so a ROS problem can't take
the pilot's video down with it.

What changed from the original stream.py:
  - Runs on AlmaLinux 10 (system GStreamer from dnf, stdlib-only Python, no picamera2/OpenCV).
  - The camera is found by USB port (/dev/v4l/by-path), not /dev/video0, so it doesn't break
    when the Pi's own video nodes (ISP, HEVC decoder) shift the numbering. One camera by default;
    more can be listed in ROV_CAMERAS (each on its own UDP port).
  - A camera that is unplugged or crashes is restarted; GStreamer errors are logged, not hidden.
  - Forces 4:2:0 (I420) before the encoder. UVC cameras usually send 4:2:2 JPEG, which made
    x264enc produce High 4:2:2 H.264 that many hardware decoders can't play.
  - Encoder is picked from what's installed: x264enc (RPM Fusion), then openh264enc.
  - Optional MJPEG mode (--codec mjpeg): forwards the camera's JPEG frames without re-encoding.
    The Pi 5 has no hardware H.264 encoder, so this costs almost no CPU, but uses much more
    tether bandwidth. The topside must be started with ROV_VIDEO_CODEC=mjpeg too.
  - The Camera Module 3 photo server (TCP 5002) is gone: photos now go through ROS
    (rov_photogrammetry -> topside photogrammetry_saver -> captures folder).

Settings come from command-line flags or the environment (video.env, loaded by run.sh and the service):
  ROV_TOPSIDE_IP     topside address                         (default 192.168.1.69)
  ROV_CAMERAS        "auto" (first USB camera -> UDP 5000) or "DEVICE[:PORT[:WxH[@FPS]]] ..."
  ROV_VIDEO_CODEC    h264 | mjpeg                             (default h264)
  ROV_VIDEO_BITRATE  H.264 bitrate in kbit/s                  (default 12000)
  ROV_VIDEO_SIZE     default WxH@FPS for each camera          (default 1920x1080@30)
  ROV_VIDEO_ENCODER  auto | x264enc | openh264enc             (default auto)

Examples:
  python3 stream.py --list              # show cameras and their by-path names
  python3 stream.py --dry-run           # print the GStreamer pipelines
  python3 stream.py                     # stream (auto: first USB camera -> UDP 5000)
  python3 stream.py --host 10.6.18.143 --cam /dev/v4l/by-path/...-video-index0:5000
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

FIRST_PORT = 5000
RESTART_DELAY_S = 2.0
_SIZE_RE = re.compile(r"^(\d+)x(\d+)(?:@(\d+))?$")


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# --------------------------------------------------------------------- cameras
@dataclass
class Camera:
    device: str          # stable path, e.g. /dev/v4l/by-path/platform-...-usb-0:1:1.0-video-index0
    port: int
    width: int
    height: int
    fps: int

    @property
    def label(self) -> str:
        return f"cam:{self.port}"


def parse_size(text: str) -> tuple[int, int, int | None]:
    m = _SIZE_RE.match(text.strip())
    if not m:
        raise ValueError(f"bad size {text!r}; expected WxH or WxH@FPS, e.g. 1920x1080@30")
    return int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None


def usb_camera_paths(by_path_dir: str = "/dev/v4l/by-path") -> list[str]:
    """USB capture nodes in a stable order (by USB port). Skips the Pi's built-in video nodes and
    the metadata nodes (index1) that every UVC camera also creates."""
    return sorted(glob.glob(os.path.join(by_path_dir, "*usb*-video-index0")))


def parse_cameras(spec: str, default_size: str, by_path_dir: str = "/dev/v4l/by-path") -> list[Camera]:
    w0, h0, fps0 = parse_size(default_size)
    fps0 = fps0 or 30
    if spec.strip().lower() in ("", "auto"):
        return [Camera(dev, FIRST_PORT + i, w0, h0, fps0)
                for i, dev in enumerate(usb_camera_paths(by_path_dir)[:1])]   # the ROV has one pilot camera
    cams = []
    for i, item in enumerate(spec.split()):
        # Split from the right: by-path names contain ':' themselves (usb-0:1:1.0)
        size = None
        parts = item.rsplit(":", 2)
        if len(parts) == 3 and _SIZE_RE.match(parts[2]) and parts[1].isdigit():
            dev, port, size = parts[0], int(parts[1]), parts[2]
        else:
            head, _, tail = item.rpartition(":")
            if head and tail.isdigit():   # device paths end in video-index0 or videoN, never ":digits"
                dev, port = head, int(tail)
            else:
                dev, port = item, FIRST_PORT + i
        w, h, fps = parse_size(size) if size else (w0, h0, None)
        cams.append(Camera(dev, port, w, h, fps or fps0))
    return cams


# ------------------------------------------------------------------- pipelines
def element_exists(name: str) -> bool:
    try:
        return subprocess.run(["gst-inspect-1.0", "--exists", name],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except FileNotFoundError:
        return False


def pick_encoder(preference: str = "auto", exists=element_exists) -> str | None:
    if preference != "auto":
        return preference if exists(preference) else None
    for name in ("x264enc", "openh264enc"):
        if exists(name):
            return name
    return None


def encoder_args(encoder: str, bitrate_kbps: int, keyint: int) -> list[str]:
    if encoder == "x264enc":
        # Same settings as the original stream.py
        return ["x264enc", "tune=zerolatency", "speed-preset=ultrafast",
                f"bitrate={bitrate_kbps}", f"key-int-max={keyint}"]
    if encoder == "openh264enc":
        return ["openh264enc", f"bitrate={bitrate_kbps * 1000}", f"gop-size={keyint}"]
    raise ValueError(f"unsupported encoder {encoder!r}")


def build_pipeline(cam: Camera, host: str, codec: str, encoder: str | None,
                   bitrate_kbps: int = 12000, keyint: int = 15) -> list[str]:
    src = ["v4l2src", f"device={cam.device}", "do-timestamp=true", "!",
           f"image/jpeg,width={cam.width},height={cam.height},framerate={cam.fps}/1", "!"]
    sink = ["udpsink", f"host={host}", f"port={cam.port}", "sync=false"]
    if codec == "mjpeg":
        body = ["rtpjpegpay", "pt=26", "!"]
    elif codec == "h264":
        if not encoder:
            raise RuntimeError("no H.264 encoder installed (x264enc or openh264enc); "
                               "run ./install_script.sh, or use --codec mjpeg")
        body = ["jpegdec", "!", "videoconvert", "!", "video/x-raw,format=I420", "!",
                # decode and encode run in separate threads; drop frames rather than add latency
                "queue", "max-size-buffers=2", "leaky=downstream", "!",
                *encoder_args(encoder, bitrate_kbps, keyint), "!",
                "rtph264pay", "config-interval=-1", "pt=96", "!"]
    else:
        raise ValueError(f"codec must be h264 or mjpeg, not {codec!r}")
    return ["gst-launch-1.0", *src, *body, *sink]


# ------------------------------------------------------------------- streaming
class CameraStreamer(threading.Thread):
    """Runs one gst-launch per camera and restarts it if it exits (unplugged, error)."""

    def __init__(self, cam: Camera, argv: list[str], stop: threading.Event):
        super().__init__(name=cam.label, daemon=True)
        self.cam, self.argv, self.stop = cam, argv, stop
        self.proc: subprocess.Popen | None = None

    def run(self) -> None:
        waiting_logged = False
        while not self.stop.is_set():
            if not os.path.exists(self.cam.device):
                if not waiting_logged:
                    log(f"[{self.cam.label}] waiting for {self.cam.device}")
                    waiting_logged = True
                self.stop.wait(RESTART_DELAY_S)
                continue
            waiting_logged = False
            log(f"[{self.cam.label}] {self.cam.width}x{self.cam.height}@{self.cam.fps} "
                f"{os.path.basename(self.cam.device)} -> udp:{self.cam.port}")
            self.proc = subprocess.Popen(self.argv, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, bufsize=1)
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                line = line.strip()
                if line and not line.startswith(("Setting pipeline", "Pipeline is", "New clock",
                                                 "Redistribute latency", "Prerolled")):
                    log(f"[{self.cam.label}] {line}")
            code = self.proc.wait()
            if not self.stop.is_set():
                log(f"[{self.cam.label}] gst-launch exited ({code}); restarting in {RESTART_DELAY_S:.0f}s")
                self.stop.wait(RESTART_DELAY_S)

    def terminate(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def list_cameras() -> int:
    paths = usb_camera_paths()
    if not paths:
        print("No USB cameras found under /dev/v4l/by-path (plugged in? try: ls /dev/v4l/by-path)")
        return 1
    print("USB cameras (auto mode streams the first one to udp 5000):")
    for i, p in enumerate(paths):
        target = os.path.realpath(p)
        print(f"  {i}: {p}\n     -> {target}")
    if shutil.which("v4l2-ctl"):
        print("\nFormats of the first camera (look for MJPG 1920x1080 30 fps):")
        subprocess.run(["v4l2-ctl", "--list-formats-ext", "-d", paths[0]])
    return 0


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    ap = argparse.ArgumentParser(description="Stream the ROV's USB camera to the topside (RTP/UDP).")
    ap.add_argument("host_ip", nargs="?", help="topside IP (same as --host; same usage as the original stream.py)")
    ap.add_argument("--host", default=env.get("ROV_TOPSIDE_IP", "192.168.1.69"))
    ap.add_argument("--cam", action="append", metavar="DEV[:PORT[:WxH[@FPS]]]",
                    help="camera to stream (repeatable); default: ROV_CAMERAS or auto")
    ap.add_argument("--codec", choices=("h264", "mjpeg"), default=env.get("ROV_VIDEO_CODEC", "h264"))
    ap.add_argument("--bitrate", type=int, default=int(env.get("ROV_VIDEO_BITRATE", "12000")),
                    help="H.264 bitrate, kbit/s (default 12000)")
    ap.add_argument("--size", default=env.get("ROV_VIDEO_SIZE", "1920x1080@30"))
    ap.add_argument("--encoder", default=env.get("ROV_VIDEO_ENCODER", "auto"))
    ap.add_argument("--list", action="store_true", help="list cameras and exit")
    ap.add_argument("--dry-run", action="store_true", help="print the pipelines and exit")
    args = ap.parse_args(argv)

    if args.list:
        return list_cameras()
    if not shutil.which("gst-launch-1.0") and not args.dry_run:
        log("gst-launch-1.0 not found. Run ./install_script.sh (installs GStreamer).")
        return 1

    host = args.host_ip or args.host
    spec = " ".join(args.cam) if args.cam else env.get("ROV_CAMERAS", "auto")
    cams = parse_cameras(spec, args.size)
    if not cams:
        log("No USB camera found (auto). Plug it in, or set ROV_CAMERAS / --cam. Retrying in 5 s...")
        # Under systemd, exiting non-zero lets Restart= try again; don't spin
        time.sleep(5)
        return 1

    encoder = None
    if args.codec == "h264":
        if args.dry_run:
            encoder = args.encoder if args.encoder != "auto" else "x264enc"
        else:
            encoder = pick_encoder(args.encoder)
    pipelines = [(c, build_pipeline(c, host, args.codec, encoder, args.bitrate)) for c in cams]

    if args.dry_run:
        for c, p in pipelines:
            print(f"# {c.label}\n{' '.join(p)}\n")
        return 0

    log(f"Sending {len(cams)} camera(s) to {host}, codec {args.codec}"
        + (f" ({encoder}, {args.bitrate} kbit/s)" if args.codec == "h264" else ""))
    stop = threading.Event()
    streamers = [CameraStreamer(c, p, stop) for c, p in pipelines]

    def shutdown(signum, _frame):
        log(f"Stopping (signal {signum})")
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    for s in streamers:
        s.start()
    while not stop.is_set():
        stop.wait(1)
    for s in streamers:
        s.terminate()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError) as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
