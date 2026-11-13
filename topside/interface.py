"""
RoboSub Vision — topside interface (Linux, PySide6 + PyGObject).
Requires Python 3.12 with PyGObject and GStreamer bindings.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageTk
import os
import time
import threading
import gi
from pathlib import Path

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GLib, GstApp

from PySide6.QtCore    import Qt, QTimer, QObject, Signal, QUrl, QFileSystemWatcher, QSettings, QSize
from PySide6.QtGui     import (QImage, QPixmap, QFont, QColor,
                                QPalette, QDesktopServices, QImageReader)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton,
    QLineEdit, QHBoxLayout, QVBoxLayout, QGroupBox, QMessageBox,
    QStatusBar, QSizePolicy, QFrame, QFileDialog,
)


# Initialize GStreamer
Gst.init(None)


def _pick_h264_decoder() -> str:
    """First available H.264 decoder. avdec_h264 (gst-libav) isn't shipped by every distro
    (AlmaLinux/RHEL need RPM Fusion), so fall back to NVIDIA, OpenH264, then decodebin."""
    for name in ("avdec_h264", "nvh264dec", "openh264dec"):
        if Gst.ElementFactory.find(name):
            return name
    return "decodebin"


def _pick_yolo_device():
    """CUDA GPU 0 if PyTorch can see one, otherwise CPU (so YOLO still loads without NVIDIA)."""
    try:
        import torch
        if torch.cuda.is_available():
            return 0
    except Exception:
        pass
    return "cpu"


H264_DECODER = _pick_h264_decoder()
YOLO_DEVICE  = _pick_yolo_device() if YOLO_OK else "cpu"
# Must match ROV_VIDEO_CODEC on the ROV's video sender (rov-onboard video/video.env):
#   h264  (default) RTP/H.264, payload 96
#   mjpeg           RTP/JPEG, payload 26: the camera's JPEG frames, not re-encoded on the Pi
VIDEO_CODEC = os.environ.get("ROV_VIDEO_CODEC", "h264").strip().lower()
if VIDEO_CODEC not in ("h264", "mjpeg"):
    print(f"[GStreamer] unknown ROV_VIDEO_CODEC={VIDEO_CODEC!r}; using h264")
    VIDEO_CODEC = "h264"
print(f"[GStreamer] video codec: {VIDEO_CODEC}")
# The ROV has one pilot camera (UDP 5000). ROV_VIDEO_FEEDS=2 adds a second panel on UDP 5001.
try:
    VIDEO_FEEDS = max(1, min(2, int(os.environ.get("ROV_VIDEO_FEEDS", "1"))))
except ValueError:
    VIDEO_FEEDS = 1
print(f"[GStreamer] H.264 decoder: {H264_DECODER}")
if H264_DECODER == "decodebin":
    print("[GStreamer] warning: no dedicated H.264 decoder found; install gstreamer1-libav (RPM Fusion) "
          "or gstreamer1.0-libav (Debian/Ubuntu)")




# ── Underwater image preprocessing ───────────────────────────────────────────

def preprocess_underwater(frame: np.ndarray) -> np.ndarray:
    """
    Correct for underwater imaging conditions on laminated crab images:
      - Red channel boost  → counteracts water blue/green absorption
      - CLAHE on L channel → reduces laminate glare, improves contrast
    """
    # Red channel boost (BGR order: index 2 = red)
    f = frame.astype(np.float32)
    f[:, :, 2] = np.clip(f[:, :, 2] * 1.4, 0, 255)  # red
    f[:, :, 1] = np.clip(f[:, :, 1] * 1.1, 0, 255)  # green (slight)
    frame = f.astype(np.uint8)

    # CLAHE on luminance to reduce glare and boost contrast
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l = clahe.apply(l)
    return cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)

# ── YOLO inference engine ─────────────────────────────────────────────────────

# Per-class colors (BGR for OpenCV)
CRAB_COLORS = {
    "European Green crab": (0,   200,  80),
    "Native Jonah crab":   (0,   160, 255),
    "Native Rock crab":    (80,  80,  255),
}
DEFAULT_COLOR = (200, 200, 200)

class YOLOEngine:
    """crab.pt
    Runs YOLO26 inference in a single persistent worker thread.
    Uses a queue so frames are dropped if inference can't keep up,
    preventing RAM from climbing.
    """

    def __init__(self, model_path: str):
        self.enabled    = False
        self._model     = None
        self._lock      = threading.Lock()
        self._detections: dict[int, list] = {}
        self._queue     = {}  # feed_number → latest frame (dict acts as size-1 queue)
        self._queue_lock = threading.Lock()
        self._worker    = None
        self._running   = False

        if YOLO_OK:
            try:
                self._model = _YOLO(model_path)
                import numpy as _np
                self._model.predict(
                    _np.zeros((640, 640, 3), dtype=_np.uint8),
                    verbose=False, device=YOLO_DEVICE)
                self._running = True
                self._worker  = threading.Thread(target=self._worker_loop, daemon=True)
                self._worker.start()
                print(f"[YOLO] model loaded: {model_path} (device: {YOLO_DEVICE})")
            except Exception as exc:
                print(f"[YOLO] failed to load model: {exc}")
                self._model = None

    def is_available(self):
        return self._model is not None

    def infer_async(self, feed_number: int, frame, preprocess: bool = False):
        """Drop latest frame into the queue — worker picks it up when ready."""
        if not self.enabled or self._model is None:
            return
        with self._queue_lock:
            self._queue[feed_number] = (frame.copy(), preprocess)

    def _worker_loop(self):
        """Single persistent thread — processes one frame at a time."""
        while self._running:
            item = None
            with self._queue_lock:
                if self._queue:
                    feed_number, item = next(iter(self._queue.items()))
                    del self._queue[feed_number]
            if item is None:
                threading.Event().wait(0.01)  # 10ms sleep when idle
                continue
            frame, preprocess = item
            try:
                if preprocess:
                    frame = preprocess_underwater(frame)
                results = self._model.predict(
                    frame, verbose=False, device=YOLO_DEVICE, conf=0.4, iou=0.5)
                dets = []
                for r in results:
                    for box in r.boxes:
                        x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                        conf  = float(box.conf[0])
                        label = self._model.names[int(box.cls[0])]
                        dets.append(((x1, y1, x2, y2), conf, label))
                with self._lock:
                    self._detections[feed_number] = dets
            except Exception as exc:
                print(f"[YOLO] inference error: {exc}")

    def stop(self):
        self._running = False

    def draw(self, feed_number: int, frame):
        """Draw bounding boxes for European Green crabs only. Shows count on screen."""
        with self._lock:
            dets = self._detections.get(feed_number, [])
        green_count = 0
        for (x1, y1, x2, y2), conf, label in dets:
            if label != "European Green crab":
                continue
            green_count += 1
            color = CRAB_COLORS.get(label, DEFAULT_COLOR)
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            text  = f"Green Crab {conf:.0%}"
            (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
            cv2.rectangle(frame, (x1, y1 - th - 6), (x1 + tw + 4, y1), color, -1)
            cv2.putText(frame, text, (x1 + 2, y1 - 3),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1, cv2.LINE_AA)
        # Display count in top-left corner
        count_text = f"European Green Crabs: {green_count}"
        (tw, th), _ = cv2.getTextSize(count_text, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 2)
        cv2.rectangle(frame, (8, 8), (tw + 16, th + 20), (0, 0, 0), -1)
        cv2.putText(frame, count_text, (12, th + 12),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 80), 2, cv2.LINE_AA)
        return frame

# ── GStreamer RTP source ───────────────────────────────────────────────────────

class GstreamerRTPSource:
    """Class to handle GStreamer RTP video source"""
    def __init__(self, port=5000):
        self.port = port
        self.frame = None
        self.lock = threading.Lock()
        self.running = False
        self.pipeline = None
        self.loop = None
        self.loop_thread = None
        
    def on_new_sample(self, sink):
        """Callback for new video samples"""
        sample = sink.emit("pull-sample")
        if not sample:
            return Gst.FlowReturn.ERROR
            
        buf = sample.get_buffer()
        caps = sample.get_caps()
        
        structure = caps.get_structure(0)
        width = structure.get_value("width")
        height = structure.get_value("height")
        
        success, map_info = buf.map(Gst.MapFlags.READ)
        if not success:
            return Gst.FlowReturn.ERROR
            
        # Create a copy of the numpy array to avoid issues when buffer is unmapped
        # Reshape based on frame dimensions
        new_frame = np.frombuffer(map_info.data, dtype=np.uint8).reshape((height, width, 3)).copy()
        
        # Update the current frame thread-safely
        with self.lock:
            self.frame = new_frame
            
        # Unmap buffer
        buf.unmap(map_info)
        return Gst.FlowReturn.OK
    
    def start(self):
        """Start the GStreamer pipeline"""
        if self.running:
            return
        if VIDEO_CODEC == "mjpeg":
            depay = (f"udpsrc port={self.port} "
                     f"caps=application/x-rtp,media=video,clock-rate=90000,encoding-name=JPEG,payload=26 ! "
                     f"rtpjpegdepay ! jpegdec ! ")
        else:
            depay = (f"udpsrc port={self.port} "
                     f"caps=application/x-rtp,encoding-name=H264,payload=96 ! "
                     f"rtph264depay ! {H264_DECODER} ! ")
        pipe_str = (
            depay +
            f"videoconvert ! "
            f"video/x-raw,format=BGR ! "
            f"appsink name=sink emit-signals=false max-buffers=1 drop=true sync=false"
        )
        
        self.pipeline = Gst.parse_launch(pipeline_str)
        appsink = self.pipeline.get_by_name("sink")
        appsink.connect("new-sample", self.on_new_sample)
        
        # Start the pipeline
        self.pipeline.set_state(Gst.State.PLAYING)
        
        # Create and start GLib main loop in a separate thread
        self.loop = GLib.MainLoop()
        self.loop_thread = threading.Thread(target=self.loop.run)
        self.loop_thread.daemon = True
        self.loop_thread.start()
        
        self.running = True
        print(f"GStreamer RTP source started on port {self.port}")
    
    def get_frame(self):
        """Get the current frame thread-safely"""
        with self.lock:
            if self.frame is not None:
                return self.frame.copy()
            return None
    
    def stop(self):
        """Stop the GStreamer pipeline"""
        if not self.running:
            return
            
        # Quit the GLib main loop
        if self.loop and self.loop.is_running():
            self.loop.quit()
            
        # Wait for the loop thread to finish
        if self.loop_thread and self.loop_thread.is_alive():
            self.loop_thread.join(timeout=1.0)
            
        # Stop the pipeline
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            
        self.running = False
        print(f"GStreamer RTP source stopped on port {self.port}")

# ── UC-684 feed panel ─────────────────────────────────────────────────────────

class FeedPanel(QWidget):

    def __init__(self, feed_number, default_port, output_dir, yolo_engine=None, main_window=None, parent=None):
        super().__init__(parent)
        self.feed_number   = feed_number
        self.output_dir    = output_dir
        self.rtp_source    = None
        self.yolo_engine   = yolo_engine
        self.main_window   = main_window
        self._fps_times    = []
        self._last_frame_t = None
        self._frametimes   = []
        # Fisheye dewarp maps — built once on first frame
        self._map1         = None
        self._map2         = None
        self._dewarp_size  = None
        self._build_ui(default_port)

    def _build_dewarp_maps(self, w: int, h: int):
        """Build fisheye undistortion maps for the UC-684 at given resolution."""
        # Scale calibration from 640×480 base
        fx = fy = 522 * (w / 640)
        cx, cy = w / 2, h / 2
        K = np.array([[fx, 0., cx],
                      [0., fy, cy],
                      [0., 0.,  1.]], dtype=np.float32)
        D = np.array([-0.2, 0.02, 0.0, 0.0], dtype=np.float32)
        new_K = cv2.fisheye.estimateNewCameraMatrixForUndistortRectify(
            K, D, (w, h), np.eye(3), balance=0.05)
        self._map1, self._map2 = cv2.fisheye.initUndistortRectifyMap(
            K, D, np.eye(3), new_K, (w, h), cv2.CV_16SC2)
        self._dewarp_size = (w, h)
        print(f"[Feed {self.feed_number}] dewarp maps built for {w}x{h}")

    def _build_ui(self, default_port):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.video_label = QLabel(f"Feed {self.feed_number}\nWaiting for stream…")
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setMinimumSize(300, 300)
        self.video_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.video_label.setStyleSheet(
            "background:#0a0a0a; color:#444; border-radius:4px;")
        layout.addWidget(self.video_label, stretch=1)

        self.status_label = QLabel("Disconnected")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("color:#555; font-size:11px;")
        layout.addWidget(self.status_label)

        group = QGroupBox(f"Feed {self.feed_number}  ·  UC-684")
        group.setStyleSheet(_group_style())
        gl = QHBoxLayout(group)
        gl.addWidget(_label("Port:"))
        self.port_entry = _line_edit(str(default_port), 56)
        gl.addWidget(self.port_entry)
        gl.addWidget(_button("Connect", self.connect))
        gl.addWidget(_button("Capture Frame", self.capture_frame))
        layout.addWidget(group)

    def connect(self):
        try:
            port = int(self.port_entry.text())
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            QMessageBox.critical(self, "Invalid Port",
                                 "Enter a port between 1 and 65535.")
            return
        if self.rtp_source and self.rtp_source.running:
            self.rtp_source.stop()
        self.rtp_source = GstreamerRTPSource(port=port)
        self.status_label.setText(f"Connecting on port {port}…")
        QApplication.processEvents()
        try:
            self.rtp_source.start()
            self.status_label.setText(f"Connected  ·  port {port}")
        except Exception as exc:
            QMessageBox.critical(self, "Connection Error", str(exc))
            self.status_label.setText("Connection failed")
            self.rtp_source = None

    def capture_frame(self):
        if not self.rtp_source:
            QMessageBox.warning(self, "Not Connected",
                                f"Feed {self.feed_number} not connected.")
            return
        frame, _ = self.rtp_source.get_frame()
        if frame is None:
            QMessageBox.warning(self, "No Frame",
                                f"No video on Feed {self.feed_number} yet.")
            return
        ts   = time.strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"feed{self.feed_number}_{ts}.jpg"
        cv2.imwrite(str(path), frame)
        self.status_label.setText(f"Saved → {path.name}")

    def update_display(self):
        if not self.rtp_source:
            return
        frame, frame_time = self.rtp_source.get_frame()
        if frame is None:
            return

        now = time.monotonic()

        # Build dewarp maps on first frame or resolution change
        h0, w0 = frame.shape[:2]
        if self._dewarp_size != (w0, h0):
            self._build_dewarp_maps(w0, h0)

        # Apply fisheye correction
        frame = cv2.remap(frame, self._map1, self._map2,
                          interpolation=cv2.INTER_LINEAR)

        # FPS
        self._fps_times.append(now)
        self._fps_times = [t for t in self._fps_times if now - t <= 1.0]
        fps = len(self._fps_times)

        # Frametime between successive frames
        if frame_time and self._last_frame_t and frame_time != self._last_frame_t:
            self._frametimes.append((frame_time - self._last_frame_t) * 1000)
            if len(self._frametimes) > 30:
                self._frametimes.pop(0)
        if frame_time and frame_time != self._last_frame_t:
            self._last_frame_t = frame_time

        avg_ft     = sum(self._frametimes) / len(self._frametimes) if self._frametimes else 0
        display_lag = (now - frame_time) * 1000 if frame_time else 0

        # Submit frame for async inference
        if self.yolo_engine:
            preprocess = bool(self.main_window and self.main_window.preprocessing_enabled)
            self.yolo_engine.infer_async(self.feed_number, frame, preprocess=preprocess)
            if self.yolo_engine.enabled:
                frame = self.yolo_engine.draw(self.feed_number, frame)

        lw, lh = self.video_label.width(), self.video_label.height()
        h, w   = frame.shape[:2]
        scale  = min(lw / w, lh / h, 1.0)
        dw, dh = max(1, int(w * scale)), max(1, int(h * scale))
        frame  = cv2.resize(frame, (dw, dh), interpolation=cv2.INTER_LINEAR)
        h2, w2, _ = frame.shape
        qt_img = QImage(frame.data, w2, h2, 3 * w2, QImage.Format.Format_BGR888)
        self.video_label.setPixmap(QPixmap.fromImage(qt_img))
        self.status_label.setText(
            f"{w}×{h}  ·  {fps} fps  ·  ft {avg_ft:.1f}ms  ·  lag {display_lag:.0f}ms  ·  port {self.rtp_source.port}"
        )

    def stop(self):
        if self.rtp_source:
            self.rtp_source.stop()

# ── Photogrammetry captures (saved by the ROS stack) ─────────────────────────
#
# Stills are captured and saved by ROS: Circle on the DS4 -> joy_to_mavlink calls
# /photogrammetry/capture on the Pi -> /photogrammetry/image -> photogrammetry_saver
# writes the original JPEG (EXIF intact) plus a .json with depth/heading into its
# captures folder. This panel only watches that folder and shows each new photo.

CAPTURE_EXTS = (".jpg", ".jpeg")


def list_capture_files(folder: Path) -> list[Path]:
    """Finished capture JPEGs in `folder`, oldest first. Ignores .tmp files mid-write."""
    try:
        files = [f for f in folder.iterdir()
                 if f.is_file() and f.suffix.lower() in CAPTURE_EXTS]
    except OSError:
        return []
    return sorted(files, key=lambda f: (f.stat().st_mtime, f.name))


def read_sidecar(photo: Path) -> dict:
    """Metadata JSON written next to a capture by photogrammetry_saver ({} if missing/bad)."""
    try:
        return json.loads(photo.with_suffix(".json").read_text())
    except (OSError, ValueError):
        return {}


def default_captures_dir(home: Path | None = None, env: dict | None = None,
                         saved: str | None = None) -> Path:
    """Where the ROS photogrammetry_saver writes: $ROV_CAPTURES_DIR, the last folder chosen
    in this app, then the usual rov-topside checkouts, then ~/rov_captures."""
    home = home or Path.home()
    env = os.environ if env is None else env
    if env.get("ROV_CAPTURES_DIR"):
        return Path(env["ROV_CAPTURES_DIR"]).expanduser()
    if saved:
        return Path(saved)
    candidates = [home / "rov_topside_ws" / "captures"]
    candidates += sorted(home.glob("Downloads/rov-topside*/captures"))
    candidates += sorted(home.glob("rov-topside*/captures"))
    candidates.append(home / "rov_captures")
    for c in candidates:
        if c.is_dir():
            return c
    return candidates[0]


def describe_capture(photo: Path, width: int, height: int, meta: dict) -> str:
    parts = [f"{width}×{height}", f"{photo.stat().st_size / 1e6:.1f} MB"]
    if meta.get("depth_m") is not None:
        parts.append(f"depth {meta['depth_m']:.2f} m")
    if meta.get("heading_deg") is not None:
        parts.append(f"hdg {meta['heading_deg']:.0f}°")
    return "  ·  ".join(parts)


class PhotoPanel(QWidget):

    PREVIEW_DECODE = QSize(960, 720)   # decode JPEGs scaled down; plenty for a 320 px panel

    def __init__(self, parent=None):
        super().__init__(parent)
        self._settings      = QSettings("RoboSub", "Vision")
        self._dir: Path | None = None
        self._seen: set[str] = set()
        self._session_count = 0
        self._last_pixmap   = None

        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(lambda _p: self._scan_timer.start())
        self._scan_timer = QTimer(self)             # debounce bursts of directory events
        self._scan_timer.setSingleShot(True)
        self._scan_timer.setInterval(250)
        self._scan_timer.timeout.connect(self._scan)
        self._poll_timer = QTimer(self)             # safety net: missed events, folder created later
        self._poll_timer.timeout.connect(self._poll)
        self._poll_timer.start(2000)
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(lambda: self._set_preview_border("#080808"))

        self._build_ui()
        self.set_folder(default_captures_dir(saved=self._settings.value("captures_dir", None)),
                        remember=False)

        # Left camera frame (remove LabelFrame border and padding)
        self.left_cam_frame = ttk.Frame(self.cameras_frame)
        self.left_cam_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=0, pady=0)

        header = QLabel("Photogrammetry")
        header.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        header.setStyleSheet("color:#00bfff; letter-spacing:1px;")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(header)

        self.preview = QLabel("No photo yet\nPress  ○  on the controller")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.preview.setMinimumSize(280, 200)
        self._set_preview_border("#080808")
        layout.addWidget(self.preview, stretch=1)

        self.info_label = QLabel("—")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.info_label.setWordWrap(True)
        self.info_label.setStyleSheet("color:#555; font-size:11px;")
        layout.addWidget(self.info_label)

        self.count_label = QLabel("")
        self.count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.count_label.setStyleSheet("color:#8f8; font-size:12px;")
        layout.addWidget(self.count_label)

        grp = QGroupBox("Captures folder (ROS photogrammetry_saver)")
        grp.setStyleSheet(_group_style())
        gl = QVBoxLayout(grp)
        self.folder_label = QLabel("—")
        self.folder_label.setWordWrap(True)
        self.folder_label.setStyleSheet("color:#aaa; font-size:11px;")
        gl.addWidget(self.folder_label)
        row = QHBoxLayout()
        row.addWidget(_button("Choose…", self._choose_folder))
        row.addWidget(_button("Open", self._open_folder))
        gl.addLayout(row)
        layout.addWidget(grp)

        hint = QLabel("○ triggers capture through the ROS stack")
        hint.setStyleSheet("color:#444; font-size:11px;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("color:#666; font-size:11px;")
        layout.addWidget(self.status_label)

    # ── folder handling ──
    def set_folder(self, folder: Path, remember: bool = True):
        folder = Path(folder).expanduser()
        if self._watcher.directories():
            self._watcher.removePaths(self._watcher.directories())
        self._dir = folder
        self._session_count = 0
        if remember:
            self._settings.setValue("captures_dir", str(folder))
        self.folder_label.setText(str(folder))
        existing = list_capture_files(folder)
        self._seen = {str(f) for f in existing}
        if folder.is_dir():
            self._watcher.addPath(str(folder))
            self.status_label.setText("Watching for new captures")
        else:
            self.status_label.setText("Folder not found yet (start the ROS topside), or Choose…")
        if existing:
            self._show_photo(existing[-1], flash=False)
        else:
            self._last_pixmap = None
            self.preview.setText("No photo yet\nPress  ○  on the controller")
            self.info_label.setText("—")
        self._update_count()

    def _choose_folder(self):
        start = str(self._dir) if self._dir and self._dir.is_dir() else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "ROS captures folder", start)
        if chosen:
            self.set_folder(Path(chosen))

    def _open_folder(self):
        if self._dir and self._dir.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._dir.resolve())))

    def _poll(self):
        if self._dir is None:
            return
        if self._dir.is_dir() and str(self._dir) not in self._watcher.directories():
            self._watcher.addPath(str(self._dir))
            self.status_label.setText("Watching for new captures")
        self._scan()

    def _scan(self):
        if self._dir is None:
            return
        newest = None
        for f in list_capture_files(self._dir):
            key = str(f)
            if key in self._seen:
                continue
            reader = QImageReader(key)
            if not reader.canRead():          # still being written; try again next scan
                continue
            self._seen.add(key)
            self._session_count += 1
            newest = f
        if newest is not None:
            self._show_photo(newest, flash=True)
        self._update_count()

    # ── display ──
    def _show_photo(self, photo: Path, flash: bool):
        reader = QImageReader(str(photo))
        reader.setAutoTransform(True)
        full = reader.size()
        if full.isValid():
            reader.setScaledSize(full.scaled(self.PREVIEW_DECODE, Qt.AspectRatioMode.KeepAspectRatio))
        img = reader.read()
        if img.isNull():
            self.status_label.setText(f"Could not read {photo.name}")
            return
        self._last_pixmap = QPixmap.fromImage(img)
        self._show_pixmap(self._last_pixmap)
        meta = read_sidecar(photo)
        self.info_label.setText(f"{photo.name}\n{describe_capture(photo, full.width(), full.height(), meta)}")
        if flash:
            self.status_label.setText(f"New capture  ·  {time.strftime('%H:%M:%S')}")
            self._set_preview_border("#3f3")
            self._flash_timer.start(700)

    def _update_count(self):
        total = len(self._seen)
        self.count_label.setText(f"{total} in folder  ·  {self._session_count} this session")

    def _set_preview_border(self, color):
        self.preview.setStyleSheet(
            f"background:#080808; color:#333; border-radius:4px; font-size:12px;"
            f"border:2px solid {color};")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._last_pixmap:
            self._show_pixmap(self._last_pixmap)

    def _show_pixmap(self, pix):
        scaled = pix.scaled(self.preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.preview.setPixmap(scaled)

# ── Main window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("RoboSub Vision")
        self.resize(1400, 860)
        ts = time.strftime("%Y%m%d_%H%M%S")
        self.output_dir = Path("captured_frames") / f"output_{ts}"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.preprocessing_enabled = False
        model_path = Path(__file__).parent / "crab.pt"
        self.yolo = YOLOEngine(str(model_path)) if YOLO_OK else None
        self._apply_dark_theme()
        self._build_ui()

        # Capture button for Feed 2
        self.btn_capture2 = ttk.Button(
            self.capture_frame, 
            text="Capture Feed 2", 
            command=lambda: self.capture_frames(2)
        )
        self.btn_capture2.pack(side=tk.LEFT, padx=5)

        self.feed1.connect()

        self.last_frame_hash1 = None
        self.last_frame_hash2 = None
        
        self.btn_connect2 = ttk.Button(
            self.port_frame2,
            text="Connect",
            command=lambda: self.connect_to_stream(2)
        )
        self.btn_connect2.pack(side=tk.LEFT, padx=5)
        
        # View mode frame
        self.view_frame = ttk.Frame(self.control_frame)
        self.view_frame.pack(side=tk.TOP, fill=tk.X, pady=5)
        
        # Process checkboxes
        self.process_var1 = tk.BooleanVar(value=False)
        self.chk_process1 = ttk.Checkbutton(
            self.view_frame,
            text="Show Feed 1",
            variable=self.process_var1
        )
        self.chk_process1.pack(side=tk.LEFT, padx=20)
        
        self.process_var2 = tk.BooleanVar(value=False)
        self.chk_process2 = ttk.Checkbutton(
            self.view_frame,
            text="Show Feed 2",
            variable=self.process_var2
        )
        self.chk_process2.pack(side=tk.LEFT, padx=20)
        
        # Exit button
        self.btn_exit = ttk.Button(
            self.view_frame, 
            text="Exit", 
            command=self.close_app
        )
        self.btn_exit.pack(side=tk.RIGHT, padx=5)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        hdr = QHBoxLayout()
        title = QLabel("RoboSub Vision")
        title.setFont(QFont("Courier New", 14, QFont.Weight.Bold))
        title.setStyleSheet("color:#00bfff; letter-spacing:2px;")
        hdr.addWidget(title)
        hdr.addStretch()
        hdr.addWidget(_button("📁  Open Frame Folder",
            lambda: QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self.output_dir.resolve())))))

        self.btn_yolo = QPushButton("🦀  YOLO  OFF")
        self.btn_yolo.setCheckable(True)
        self.btn_yolo.setEnabled(self.yolo is not None and self.yolo.is_available())
        self.btn_yolo.setStyleSheet(
            "QPushButton { background:#1a1a2a; color:#666; border:1px solid #333;"
            "border-radius:4px; padding:4px 12px; font-size:12px; }"
            "QPushButton:checked { background:#1a2a1a; color:#8f8; border-color:#3a5a3a; }"
            "QPushButton:hover { border-color:#555; }")
        self.btn_yolo.toggled.connect(self._toggle_yolo)
        hdr.addWidget(self.btn_yolo)

        self.btn_preprocess = QPushButton("🌊  Preprocess  OFF")
        self.btn_preprocess.setCheckable(True)
        self.btn_preprocess.setStyleSheet(
            "QPushButton { background:#1a1a2a; color:#666; border:1px solid #333;"
            "border-radius:4px; padding:4px 12px; font-size:12px; }"
            "QPushButton:checked { background:#1a2a2a; color:#4dd; border-color:#2a5a5a; }"
            "QPushButton:hover { border-color:#555; }")
        self.btn_preprocess.toggled.connect(self._toggle_preprocess)
        hdr.addWidget(self.btn_preprocess)
        hdr.addWidget(_button("Exit", self.close))
        root.addLayout(hdr)

        content = QHBoxLayout()
        content.setSpacing(8)
        self.feeds = [FeedPanel(i + 1, 5000 + i, self.output_dir, yolo_engine=self.yolo, main_window=self)
                      for i in range(VIDEO_FEEDS)]
        self.feed1 = self.feeds[0]
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setStyleSheet("color:#222;")
        self.photo_panel = PhotoPanel()
        self.photo_panel.setFixedWidth(320)
        for feed in self.feeds:
            content.addWidget(feed, stretch=1)
        content.addWidget(divider)
        content.addWidget(self.photo_panel)
        root.addLayout(content, stretch=1)

        sb = QStatusBar()
        sb.setStyleSheet("color:#444; font-size:11px;")
        self.setStatusBar(sb)
        sb.showMessage(f"Output → {self.output_dir.resolve()}")

    def _tick_display(self):
        for feed in self.feeds:
            feed.update_display()

    def _toggle_yolo(self, checked: bool):
        if self.yolo:
            self.yolo.enabled = checked
            self.btn_yolo.setText("🦀  YOLO  ON" if checked else "🦀  YOLO  OFF")

    def _toggle_preprocess(self, checked: bool):
        self.preprocessing_enabled = checked
        self.btn_preprocess.setText(
            "🌊  Preprocess  ON" if checked else "🌊  Preprocess  OFF")

    def closeEvent(self, event):
        self.display_timer.stop()
        if self.yolo:
            self.yolo.stop()
        for feed in self.feeds:
            feed.stop()
        event.accept()

# ── Helpers ───────────────────────────────────────────────────────────────────

def _group_style():
    return ("QGroupBox { color:#888; font-size:11px; border:1px solid #2a2a2a;"
            "border-radius:4px; margin-top:8px; padding-top:4px; }"
            "QGroupBox::title { subcontrol-origin:margin; left:8px; }")

def _label(text):
    lbl = QLabel(text)
    lbl.setStyleSheet("color:#777; font-size:12px;")
    return lbl

def _line_edit(text, width):
    w = QLineEdit(text)
    w.setFixedWidth(width)
    return w

def _button(text, slot=None):
    btn = QPushButton(text)
    if slot:
        btn.clicked.connect(slot)
    return btn

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Create Tkinter window
    root = tk.Tk()
    root.geometry("1100x900")
    
    # Create app
    app = CameraCaptureApp(root)
    
    # Set up window close handler
    root.protocol("WM_DELETE_WINDOW", app.close_app)
    
    print("Entering main loop!")
    
    # Start Tkinter event loop

    root.mainloop()
