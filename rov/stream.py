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
  - camera-h264 mode (--codec camera-h264): for cameras that output H.264 themselves (many
    Arducam USB cameras, including the UC-684, have a second H.264 video node). The Pi only
    repackages it, so it costs almost no CPU and needs no x264. The topside needs no change.
  - Optional MJPEG mode (--codec mjpeg): forwards the camera's JPEG frames without re-encoding.
    The Pi 5 has no hardware H.264 encoder, so this costs almost no CPU, but uses much more
    tether bandwidth. The topside must be started with ROV_VIDEO_CODEC=mjpeg too.
  - The Camera Module 3 photo server (TCP 5002) is gone: photos now go through ROS
    (rov_photogrammetry -> topside photogrammetry_saver -> captures folder).

Settings come from command-line flags or the environment (video.env, loaded by run.sh and the service):
  ROV_TOPSIDE_IP     topside address                         (default 192.168.1.69)
  ROV_CAMERAS        "auto" (first USB camera -> UDP 5000) or "DEVICE[:PORT[:WxH[@FPS]]] ..."
  ROV_VIDEO_CODEC    h264 | camera-h264 | mjpeg               (default h264)
  ROV_VIDEO_BITRATE  H.264 bitrate in kbit/s                  (default 12000)
  ROV_VIDEO_SIZE     default WxH@FPS for each camera          (default 1920x1080@30)
  ROV_VIDEO_ENCODER  auto | x264enc | openh264enc             (default auto)
  ROV_JPEG_QUALITY   mjpeg only: re-compress on the Pi at this quality, 1-100 (default 0 = camera's frames)
  ROV_CAMERA_CONTROLS  v4l2 controls applied each time the camera starts (they reset on replug/reboot)
                     (default exposure_dynamic_framerate=0,power_line_frequency=2: steady 30 fps in dim
                     water, no 60 Hz flicker under US lights; empty = leave the camera alone)

Examples:
  python3 stream.py --list              # show cameras and their by-path names
  python3 stream.py --dry-run           # print the GStreamer pipelines
  python3 stream.py                     # stream (auto: first USB camera -> UDP 5000)
  python3 stream.py --host 10.6.18.143 --cam /dev/v4l/by-path/...-video-index0:5000
"""

from __future__ import annotations

import argparse
import fcntl
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
MAX_RESTART_DELAY_S = 30.0
QUICK_FAIL_S = 5.0          # a run shorter than this counts as a failed start
LOCK_FILE = "/tmp/rov-video.lock"
_SIZE_RE = re.compile(r"^(\d+)x(\d+)(?:@(\d+))?$")


def log(msg: str) -> None:
    try:
        print(time.strftime("%H:%M:%S"), msg, flush=True)
    except (BrokenPipeError, ValueError):   # stdout closed (e.g. piped into head); keep streaming
        pass


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


CODECS = ("h264", "camera-h264", "mjpeg")
_FOURCC_RE = re.compile(r"\[\d+\]:\s*'(\w{3,4})\s*'")


def usb_camera_paths(by_path_dir: str = "/dev/v4l/by-path") -> list[str]:
    """First video node of each USB camera, in a stable order (by USB port). Skips the Pi's
    built-in video nodes (ISP, HEVC decoder)."""
    return sorted(glob.glob(os.path.join(by_path_dir, "*usb*-video-index0")))


def usb_video_nodes(by_path_dir: str = "/dev/v4l/by-path") -> list[str]:
    """Every USB video node (a camera can have several: MJPEG, H.264, metadata)."""
    return sorted(glob.glob(os.path.join(by_path_dir, "*usb*-video-index*")))


def node_formats(dev: str) -> set[str] | None:
    """Pixel formats a video node can capture (e.g. {'MJPG', 'YUYV'} or {'H264'}).
    None if v4l2-ctl isn't installed or the node can't be read."""
    try:
        out = subprocess.run(["v4l2-ctl", "-d", dev, "--list-formats"], capture_output=True,
                             text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return set(_FOURCC_RE.findall(out.stdout))


def auto_camera_node(codec: str, by_path_dir: str = "/dev/v4l/by-path", formats=node_formats) -> str | None:
    """The USB node that provides what `codec` needs: H.264 for camera-h264, MJPEG otherwise.
    Falls back to the first camera's first node if formats can't be read."""
    want = "H264" if codec == "camera-h264" else "MJPG"
    readable = False
    for node in usb_video_nodes(by_path_dir):
        f = formats(node)
        if f is None:
            continue
        readable = True
        if want in f:
            return node
    if readable and codec == "camera-h264":
        return None          # formats were readable and nothing offers H.264
    first = usb_camera_paths(by_path_dir)
    return first[0] if first else None


def parse_cameras(spec: str, default_size: str, by_path_dir: str = "/dev/v4l/by-path",
                  codec: str = "h264", formats=node_formats) -> list[Camera]:
    w0, h0, fps0 = parse_size(default_size)
    fps0 = fps0 or 30
    if spec.strip().lower() in ("", "auto"):
        node = auto_camera_node(codec, by_path_dir, formats)   # the ROV has one pilot camera
        return [Camera(node, FIRST_PORT, w0, h0, fps0)] if node else []
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
                   bitrate_kbps: int = 12000, keyint: int = 15, h264parse: bool = True,
                   jpegparse: bool = True, jpeg_quality: int = 0) -> list[str]:
    caps = f"width={cam.width},height={cam.height},framerate={cam.fps}/1"
    src = ["v4l2src", f"device={cam.device}", "do-timestamp=true", "!",
           f"video/x-h264,{caps}" if codec == "camera-h264" else f"image/jpeg,{caps}", "!"]
    sink = ["udpsink", f"host={host}", f"port={cam.port}", "sync=false"]
    if codec == "camera-h264":
        # The camera already encodes H.264; just packetize it (same RTP format the topside expects)
        # h264parse (gstreamer1-plugins-bad-free) is optional: rtph264pay takes the camera's
        # byte-stream directly
        body = (["h264parse", "!"] if h264parse else []) + \
               ["rtph264pay", "config-interval=-1", "pt=96", "!"]
    elif codec == "mjpeg":
        # If the network stalls (Wi-Fi), drop stale frames here instead of sending a backlog late.
        # Safe for MJPEG: every frame stands alone.
        # jpegparse trims each frame to its real end (EOI). Some UVC cameras pad every MJPEG frame
        # to a fixed size, and rtpjpegpay would send that padding over the network.
        if jpeg_quality:
            # Re-compress on the Pi: the UC-684's own JPEGs are ~170 kB (~40 Mbit/s) at any resolution,
            # too much for Wi-Fi. Costs some CPU; frames stay independent (no smearing after a loss).
            body = ["jpegdec", "!", "queue", "max-size-buffers=1", "leaky=downstream", "!",
                    "jpegenc", f"quality={jpeg_quality}", "!", "rtpjpegpay", "pt=26", "!"]
        else:
            body = (["jpegparse", "!"] if jpegparse else []) + \
                   ["queue", "max-size-buffers=1", "leaky=downstream", "!", "rtpjpegpay", "pt=26", "!"]
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
        raise ValueError(f"codec must be one of {', '.join(CODECS)}, not {codec!r}")
    return ["gst-launch-1.0", *src, *body, *sink]


# ------------------------------------------------------------------- streaming
class CameraStreamer(threading.Thread):
    """Runs one gst-launch per camera and restarts it if it exits (unplugged, error)."""

    def __init__(self, cam: Camera, argv: list[str], stop: threading.Event, controls: str = ""):
        super().__init__(name=cam.label, daemon=True)
        self.cam, self.argv, self.stop, self.controls = cam, argv, stop, controls
        self.proc: subprocess.Popen | None = None
        self._controls_warned = False

    def apply_controls(self) -> None:
        cmd = controls_command(self.cam.device, self.controls)
        if not cmd:
            return
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            ok, err = r.returncode == 0, (r.stderr or r.stdout).strip()
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            ok, err = False, str(e)
        if not ok and not self._controls_warned:
            log(f"[{self.cam.label}] couldn't set camera controls ({self.controls}): {err}")
            self._controls_warned = True

    def run(self) -> None:
        waiting_logged = False
        delay = RESTART_DELAY_S
        last_error = None
        while not self.stop.is_set():
            if not os.path.exists(self.cam.device):
                if not waiting_logged:
                    log(f"[{self.cam.label}] waiting for {self.cam.device}")
                    waiting_logged = True
                self.stop.wait(RESTART_DELAY_S)
                continue
            waiting_logged = False
            quiet = last_error is not None   # same failure again: don't repeat the whole GStreamer dump
            if not quiet:
                log(f"[{self.cam.label}] {self.cam.width}x{self.cam.height}@{self.cam.fps} "
                    f"{os.path.basename(self.cam.device)} -> udp:{self.cam.port}")
            self.apply_controls()
            started = time.monotonic()
            self.proc = subprocess.Popen(self.argv, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, bufsize=1)
            assert self.proc.stdout is not None
            first_error, held = None, []
            for line in self.proc.stdout:
                line = line.strip()
                if not line or line.startswith(("Setting pipeline", "Pipeline is", "New clock",
                                                "Redistribute latency", "Prerolled")):
                    continue
                if first_error is None and (line.startswith("ERROR") or "erroneous pipeline" in line):
                    first_error = line
                if quiet and time.monotonic() - started < QUICK_FAIL_S:
                    held.append(line)            # print only if this run turns out to be different
                    continue
                if held:
                    for h in held:
                        log(f"[{self.cam.label}] {h}")
                    held = []
                quiet = False
                log(f"[{self.cam.label}] {line}")
            code = self.proc.wait()
            if self.stop.is_set():
                break
            ran = time.monotonic() - started
            if ran < QUICK_FAIL_S:
                if held and first_error != last_error:
                    for h in held:
                        log(f"[{self.cam.label}] {h}")
                if first_error and "busy" in first_error.lower() and first_error != last_error:
                    dev = os.path.realpath(self.cam.device)
                    log(f"[{self.cam.label}] The camera is in use by another program. Find it with: "
                        f"sudo fuser -v {dev}   (often rov-video.service: sudo systemctl stop rov-video)")
                last_error = first_error or f"exit {code}"
                log(f"[{self.cam.label}] failed to start ({first_error or f'exit {code}'}); "
                    f"retrying in {delay:.0f}s")
                self.stop.wait(delay)
                delay = min(delay * 2, MAX_RESTART_DELAY_S)
            else:
                log(f"[{self.cam.label}] gst-launch exited ({code}) after {ran:.0f}s; "
                    f"restarting in {RESTART_DELAY_S:.0f}s")
                last_error, delay = None, RESTART_DELAY_S
                self.stop.wait(RESTART_DELAY_S)

    def terminate(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


_lock_handle = None


def acquire_lock(path: str = LOCK_FILE):
    """Only one sender at a time (a manual run and rov-video.service would fight over the camera)."""
    global _lock_handle
    try:
        fh = open(path, "a+")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return None
    except OSError:
        return True   # can't create the lock file: don't block streaming over it
    _lock_handle = fh
    return fh


def controls_command(device: str, controls: str) -> list[str] | None:
    """v4l2-ctl command for a comma-separated control list, or None if there's nothing to set."""
    controls = ",".join(c.strip() for c in controls.replace(" ", ",").split(",") if c.strip())
    if not controls or not shutil.which("v4l2-ctl"):
        return None
    return ["v4l2-ctl", "-d", device, f"--set-ctrl={controls}"]


def list_cameras() -> int:
    nodes = usb_video_nodes()
    if not nodes:
        print("No USB cameras found under /dev/v4l/by-path (plugged in? try: ls /dev/v4l/by-path)")
        return 1
    have_ctl = shutil.which("v4l2-ctl") is not None
    print("USB video nodes:")
    with_formats = []
    for node in nodes:
        f = node_formats(node) if have_ctl else None
        desc = "formats unknown (install v4l-utils)" if f is None else (
            ", ".join(sorted(f)) if f else "no video formats (metadata node)")
        print(f"  {node}\n     -> {os.path.realpath(node)}   [{desc}]")
        if f:
            with_formats.append((node, f))
    for codec in ("h264", "camera-h264"):
        node = auto_camera_node(codec)
        print(f"\nauto with ROV_VIDEO_CODEC={codec}: {node or 'no suitable node'}")
    if any("H264" in f for _, f in with_formats):
        print("\nThis camera outputs H.264 itself: ROV_VIDEO_CODEC=camera-h264 saves the Pi's CPU (no x264).")
    for node, _ in with_formats:
        print(f"\n--- {os.path.realpath(node)} (look for 1920x1080 30 fps) ---")
        subprocess.run(["v4l2-ctl", "--list-formats-ext", "-d", node])
    return 0


def main(argv: list[str] | None = None) -> int:
    env = os.environ
    ap = argparse.ArgumentParser(description="Stream the ROV's USB camera to the topside (RTP/UDP).")
    ap.add_argument("host_ip", nargs="?", help="topside IP (same as --host; same usage as the original stream.py)")
    ap.add_argument("--host", default=env.get("ROV_TOPSIDE_IP", "192.168.1.69"))
    ap.add_argument("--cam", action="append", metavar="DEV[:PORT[:WxH[@FPS]]]",
                    help="camera to stream (repeatable); default: ROV_CAMERAS or auto")
    ap.add_argument("--codec", choices=CODECS, default=env.get("ROV_VIDEO_CODEC", "h264"))
    ap.add_argument("--bitrate", type=int, default=int(env.get("ROV_VIDEO_BITRATE", "12000")),
                    help="H.264 bitrate, kbit/s (default 12000)")
    ap.add_argument("--size", default=env.get("ROV_VIDEO_SIZE", "1920x1080@30"))
    ap.add_argument("--encoder", default=env.get("ROV_VIDEO_ENCODER", "auto"))
    ap.add_argument("--jpeg-quality", type=int, default=int(env.get("ROV_JPEG_QUALITY", "0") or 0),
                    help="mjpeg only: re-compress on the Pi at this JPEG quality (1-100; 0 = send the camera's frames)")
    ap.add_argument("--controls", default=env.get("ROV_CAMERA_CONTROLS",
                                                  "exposure_dynamic_framerate=0,power_line_frequency=2"),
                    help="v4l2 camera controls to apply at start ('' = none)")
    ap.add_argument("--list", action="store_true", help="list cameras and exit")
    ap.add_argument("--dry-run", action="store_true", help="print the pipelines and exit")
    args = ap.parse_args(argv)

    if args.list:
        return list_cameras()
    if not args.dry_run:
        lock = acquire_lock()
        if lock is None:
            log("Another copy of the video sender is already running and holding the camera.")
            log("If it's the boot service: sudo systemctl stop rov-video   "
                "(or watch it: journalctl -u rov-video -f)")
            log("Otherwise find it with: pgrep -af 'stream.py|rov_video_sender'")
            return 1
    if not shutil.which("gst-launch-1.0") and not args.dry_run:
        log("gst-launch-1.0 not found. Run ./install_script.sh (installs GStreamer).")
        return 1

    host = args.host_ip or args.host
    spec = " ".join(args.cam) if args.cam else env.get("ROV_CAMERAS", "auto")
    cams = parse_cameras(spec, args.size, codec=args.codec)
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
    use_parser = args.dry_run or element_exists("h264parse")
    use_jpegparse = args.dry_run or element_exists("jpegparse")
    if args.codec == "mjpeg" and not use_jpegparse:
        log("Note: jpegparse isn't installed (sudo dnf install -y gstreamer1-plugins-bad-free); "
            "MJPEG frames are sent exactly as the camera delivers them, including any padding.")
    pipelines = [(c, build_pipeline(c, host, args.codec, encoder, args.bitrate, h264parse=use_parser,
                                    jpegparse=use_jpegparse, jpeg_quality=args.jpeg_quality))
                 for c in cams]

    if args.dry_run:
        for c, p in pipelines:
            print(f"# {c.label}\n{' '.join(p)}\n")
        return 0

    log(f"Sending {len(cams)} camera(s) to {host}, codec {args.codec}"
        + (f" ({encoder}, {args.bitrate} kbit/s)" if args.codec == "h264" else "")
        + (f" (re-compressed at JPEG quality {args.jpeg_quality})" if args.codec == "mjpeg" and args.jpeg_quality else ""))
    stop = threading.Event()
    streamers = [CameraStreamer(c, p, stop, args.controls) for c, p in pipelines]

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
