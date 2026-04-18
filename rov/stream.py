import io
import json
import socket
import subprocess
import threading
import time
from argparse import ArgumentParser
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / "config.json"


def autodetect_topside_ip() -> str:
    """
    Resolve topside IP in order of priority:
      1. config.json next to stream.py  (set this before a dive)
      2. Default gateway from routing table
      3. Hardcoded fallback 192.168.2.1
    """
    # 1. Config file
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            ip  = cfg.get("topside_ip", "").strip()
            if ip:
                print(f"[stream] topside IP from config.json: {ip}")
                return ip
        except Exception as exc:
            print(f"[stream] could not read config.json: {exc}")

    # 2. Default gateway
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True)
        for line in result.stdout.splitlines():
            if "default via" in line:
                gateway = line.split()[2]
                print(f"[stream] topside IP from gateway: {gateway}")
                return gateway
    except Exception:
        pass

    # 3. Fallback
    print("[stream] using fallback topside IP: 192.168.2.1")
    return "192.168.2.1"

try:
    from picamera2 import Picamera2
    PICAMERA_AVAILABLE = True
except ImportError:
    PICAMERA_AVAILABLE = False
    print("[warn] picamera2 not found — photo server disabled")

# ── CLI args ──────────────────────────────────────────────────────────────────

parser = ArgumentParser()
parser.add_argument("host_ip", nargs="?", default=None,
                    help="IP address of the topside computer (autodetected if omitted)")
parser.add_argument("--photo-port", type=int, default=5002,
                    help="TCP port for the Pi Camera photo server (default: 5002)")
args = parser.parse_args()
host_ip    = args.host_ip or autodetect_topside_ip()
photo_port = args.photo_port

# ── UC-684 GStreamer streaming ────────────────────────────────────────────────

def make_gst_process(cam_index: int, w: int, h: int, port: int) -> subprocess.Popen:
    return subprocess.Popen([
        "gst-launch-1.0",
        "v4l2src", f"device=/dev/video{cam_index}", "!",
        f"image/jpeg,width={w},height={h},framerate=30/1", "!",
        "jpegdec", "!",
        "videoconvert", "!",
        "x264enc", "tune=zerolatency", "speed-preset=ultrafast",
        "bitrate=12000", "key-int-max=5", "!",
        "rtph264pay", "config-interval=-1", "pt=96", "!",
        "udpsink", f"host={host_ip}", f"port={port}",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stream_camera(cam_index: int, port: int, name: str):
    w, h = 1920, 1080
    print(f"[{name}] streaming {w}x{h} → {host_ip}:{port}")
    proc = make_gst_process(cam_index, w, h, port)
    proc.wait()

# ── Pi Camera Module 3 photo server ──────────────────────────────────────────

picam2 = None


def init_picamera() -> bool:
    """Initialise the Pi Camera Module 3 for full-resolution still capture."""
    global picam2
    if not PICAMERA_AVAILABLE:
        return False
    try:
        picam2 = Picamera2()
        # 4608×2592 = native 12 MP resolution of Camera Module 3
        cfg = picam2.create_still_configuration(
            main={"size": (4608, 2592)},
            display=None,
            buffer_count=1,
        )
        picam2.configure(cfg)
        picam2.start()
        time.sleep(2)  # allow auto-exposure / white-balance to settle
        print(f"[PiCam3] ready  (4608×2592, port {photo_port})")
        return True
    except Exception as exc:
        print(f"[PiCam3] init failed: {exc}")
        picam2 = None
        return False


def handle_photo_client(conn: socket.socket, addr):
    """
    Protocol (one round-trip per connection):
      ← "CAPTURE"          (from topside)
      → <uint32-BE length> (4 bytes)
      → <JPEG bytes>       (length bytes)
    If the camera is unavailable, length is sent as 0.
    """
    try:
        cmd = conn.recv(16).decode(errors="ignore").strip()
        if cmd != "CAPTURE":
            print(f"[PiCam3] unknown command from {addr}: {cmd!r}")
            return

        if picam2 is None:
            print(f"[PiCam3] capture requested but camera unavailable")
            conn.sendall((0).to_bytes(4, "big"))
            return

        print(f"[PiCam3] capturing for {addr} …")
        buf = io.BytesIO()
        picam2.capture_file(buf, format="jpeg")
        jpeg = buf.getvalue()

        conn.sendall(len(jpeg).to_bytes(4, "big"))
        conn.sendall(jpeg)
        print(f"[PiCam3] sent {len(jpeg):,} bytes to {addr}")

    except Exception as exc:
        print(f"[PiCam3] error handling {addr}: {exc}")
    finally:
        conn.close()


def run_photo_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", photo_port))
    srv.listen(5)
    print(f"[PiCam3] TCP photo server listening on :{photo_port}")
    while True:
        conn, addr = srv.accept()
        threading.Thread(
            target=handle_photo_client,
            args=(conn, addr),
            daemon=True,
        ).start()

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Pi Camera Module 3 — initialise then start TCP server
    init_picamera()
    threading.Thread(target=run_photo_server, daemon=True).start()

    # UC-684 cameras — stream in parallel
    # Uncomment t1 lines when second camera is connected
    t0 = threading.Thread(target=stream_camera, args=(0, 5000, "UC-684 #0"))
    # t1 = threading.Thread(target=stream_camera, args=(2, 5001, "UC-684 #1"))
    t0.start()
    # t1.start()
    t0.join()
    # t1.join()