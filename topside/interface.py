"""
RoboSub Vision — topside interface (Linux, PySide6 + PyGObject).
Requires Python 3.12 with PyGObject and GStreamer bindings.
"""

import os
import sys
import socket
import threading
import time
from pathlib import Path

import cv2
import numpy as np

try:
    from ultralytics import YOLO as _YOLO
    YOLO_OK = True
except ImportError:
    YOLO_OK = False
    print("[warn] ultralytics not installed — YOLO inference disabled")
import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")
from gi.repository import Gst, GLib, GstApp

from PySide6.QtCore    import Qt, QTimer, QObject, Signal, QUrl
from PySide6.QtGui     import (QImage, QPixmap, QFont, QColor,
                                QPalette, QDesktopServices)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel, QPushButton,
    QLineEdit, QHBoxLayout, QVBoxLayout, QGroupBox, QMessageBox,
    QStatusBar, QSizePolicy, QFrame,
)

try:
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    import pygame
    pygame.init()
    pygame.joystick.init()
    PYGAME_OK = True
except Exception:
    PYGAME_OK = False

Gst.init(None)

CAPTURE_BUTTON = 1  # PS4 Circle



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
                    verbose=False, device=0)
                self._running = True
                self._worker  = threading.Thread(target=self._worker_loop, daemon=True)
                self._worker.start()
                print(f"[YOLO] model loaded: {model_path}")
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
                    frame, verbose=False, device=0, conf=0.4, iou=0.5)
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

    def __init__(self, port=5000):
        self.port         = port
        self.frame        = None
        self.frame_time   = None   # monotonic time when frame arrived
        self.lock         = threading.Lock()
        self.running      = False
        self.pipeline     = None
        self._loop        = None
        self._loop_thread = None
        self._sink        = None

    def start(self):
        if self.running:
            return
        pipe_str = (
            f"udpsrc port={self.port} "
            f"caps=application/x-rtp,encoding-name=H264,payload=96 ! "
            f"rtph264depay ! avdec_h264 ! videoconvert ! "
            f"video/x-raw,format=BGR ! "
            f"appsink name=sink emit-signals=false max-buffers=1 drop=true sync=false"
        )
        self.pipeline = Gst.parse_launch(pipe_str)
        self._sink    = self.pipeline.get_by_name("sink")
        self.pipeline.set_state(Gst.State.PLAYING)
        self._loop        = GLib.MainLoop()
        self._loop_thread = threading.Thread(target=self._loop.run, daemon=True)
        self._loop_thread.start()
        self.running      = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        print(f"[GStreamer] port {self.port} started")

    def _poll_loop(self):
        while self.running:
            sample = self._sink.try_pull_sample(Gst.SECOND)
            if sample is None:
                continue
            arrived  = time.monotonic()
            buf      = sample.get_buffer()
            caps     = sample.get_caps()
            s        = caps.get_structure(0)
            w        = s.get_value("width")
            h        = s.get_value("height")
            ok, info = buf.map(Gst.MapFlags.READ)
            if not ok:
                continue
            frame = np.frombuffer(info.data, dtype=np.uint8).reshape((h, w, 3)).copy()
            with self.lock:
                self.frame      = frame
                self.frame_time = arrived
            buf.unmap(info)

    def get_frame(self):
        with self.lock:
            if self.frame is None:
                return None, None
            return self.frame.copy(), self.frame_time

    def stop(self):
        if not self.running:
            return
        self.running = False
        if self._loop and self._loop.is_running():
            self._loop.quit()
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=2.0)
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
        print(f"[GStreamer] port {self.port} stopped")

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

# ── Pi Camera photo client ────────────────────────────────────────────────────

class PhotoClient(QObject):
    photo_received = Signal(bytes)
    status_changed = Signal(str)
    error_occurred = Signal(str)

    def trigger(self, host, port):
        threading.Thread(
            target=self._fetch, args=(host, port), daemon=True).start()

    def _fetch(self, host, port):
        self.status_changed.emit("Capturing…")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10.0)
            sock.connect((host, port))
            sock.sendall(b"CAPTURE")
            raw = b""
            while len(raw) < 4:
                chunk = sock.recv(4 - len(raw))
                if not chunk:
                    raise ConnectionError("Connection closed")
                raw += chunk
            length = int.from_bytes(raw, "big")
            if length == 0:
                raise RuntimeError("Pi Camera not available on ROV")
            data = b""
            while len(data) < length:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
            sock.close()
            self.status_changed.emit(f"Received  ·  {length:,} bytes")
            self.photo_received.emit(data)
        except Exception as exc:
            self.error_occurred.emit(str(exc))
            self.status_changed.emit("Capture failed")

# ── Pi Camera photo panel ─────────────────────────────────────────────────────

class PhotoPanel(QWidget):

    def __init__(self, output_dir, parent=None):
        super().__init__(parent)
        self.output_dir = output_dir / "photos"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.client = PhotoClient()
        self.client.photo_received.connect(self._on_photo)
        self.client.status_changed.connect(self._on_status)
        self.client.error_occurred.connect(
            lambda msg: QMessageBox.critical(self, "Capture Error", msg))
        self._last_pixmap = None
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        header = QLabel("Pi Camera Module 3")
        header.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        header.setStyleSheet("color:#00bfff; letter-spacing:1px;")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(header)

        self.preview = QLabel("No photo yet\nPress  ○  or  Capture")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.preview.setMinimumSize(280, 200)
        self.preview.setStyleSheet(
            "background:#080808; color:#333; border-radius:4px; font-size:12px;")
        layout.addWidget(self.preview, stretch=1)

        self.info_label = QLabel("—")
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.info_label.setStyleSheet("color:#555; font-size:11px;")
        layout.addWidget(self.info_label)

        grp = QGroupBox("Connection")
        grp.setStyleSheet(_group_style())
        gl = QVBoxLayout(grp)
        row = QHBoxLayout()
        row.addWidget(_label("ROV IP:"))
        self.ip_entry = _line_edit("192.168.2.2", 110)
        row.addWidget(self.ip_entry)
        row.addWidget(_label("Port:"))
        self.port_entry = _line_edit("5002", 50)
        row.addWidget(self.port_entry)
        gl.addLayout(row)
        self.btn_capture = QPushButton("📷  Capture  (○ button)")
        self.btn_capture.setStyleSheet(
            "QPushButton { background:#1a2a1a; color:#8f8; border:1px solid #3a5a3a;"
            "border-radius:4px; padding:5px 10px; font-size:12px; }"
            "QPushButton:hover { background:#223a22; border-color:#5a8a5a; }"
            "QPushButton:pressed { background:#111; }")
        self.btn_capture.clicked.connect(self.trigger_capture)
        gl.addWidget(self.btn_capture)
        layout.addWidget(grp)

        self.ctrl_label = QLabel("🎮  No controller detected")
        self.ctrl_label.setStyleSheet("color:#444; font-size:11px;")
        self.ctrl_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.ctrl_label)

        self.status_label = QLabel("Ready")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("color:#666; font-size:11px;")
        layout.addWidget(self.status_label)

    def trigger_capture(self):
        host = self.ip_entry.text().strip()
        try:
            port = int(self.port_entry.text())
        except ValueError:
            port = 5002
        self.client.trigger(host, port)

    def set_controller_name(self, name):
        if name:
            self.ctrl_label.setText(f"🎮  {name}  (○ = capture)")
            self.ctrl_label.setStyleSheet("color:#8f8; font-size:11px;")
        else:
            self.ctrl_label.setText("🎮  No controller detected")
            self.ctrl_label.setStyleSheet("color:#444; font-size:11px;")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._last_pixmap:
            self._show_pixmap(self._last_pixmap)

    def _on_photo(self, jpeg_bytes):
        arr = np.frombuffer(jpeg_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            QMessageBox.critical(self, "Decode Error",
                                 "Could not decode received image.")
            return
        ts   = time.strftime("%Y%m%d_%H%M%S")
        path = self.output_dir / f"picam3_{ts}.jpg"
        cv2.imwrite(str(path), img)
        h, w = img.shape[:2]
        qt   = QImage(img.data, w, h, 3 * w, QImage.Format.Format_BGR888)
        pix  = QPixmap.fromImage(qt)
        self._last_pixmap = pix
        self._show_pixmap(pix)
        self.info_label.setText(
            f"{w}×{h}  ·  {len(jpeg_bytes)/1024:.0f} KB  ·  {ts}")

    def _show_pixmap(self, pix):
        scaled = pix.scaled(self.preview.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        self.preview.setPixmap(scaled)

    def _on_status(self, msg):
        self.status_label.setText(msg)

# ── Main window ───────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("RoboSub Vision")
        self.resize(1400, 860)
        ts = time.strftime("%Y%m%d_%H%M%S")
        self.output_dir = Path("captured_frames") / f"output_{ts}"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._joystick          = None
        self.preprocessing_enabled = False
        model_path = Path(__file__).parent / "crab.pt"
        self.yolo = YOLOEngine(str(model_path)) if YOLO_OK else None
        self._apply_dark_theme()
        self._build_ui()

        self.display_timer = QTimer(self)
        self.display_timer.timeout.connect(self._tick_display)
        self.display_timer.start(33)

        if PYGAME_OK:
            self.ctrl_timer = QTimer(self)
            self.ctrl_timer.timeout.connect(self._tick_controller)
            self.ctrl_timer.start(16)

        self.feed1.connect()

    def _apply_dark_theme(self):
        p = QPalette()
        dark, mid = QColor("#0f0f0f"), QColor("#1a1a1a")
        text, acc = QColor("#d0d0d0"), QColor("#00bfff")
        p.setColor(QPalette.ColorRole.Window,          dark)
        p.setColor(QPalette.ColorRole.WindowText,      text)
        p.setColor(QPalette.ColorRole.Base,            mid)
        p.setColor(QPalette.ColorRole.AlternateBase,   dark)
        p.setColor(QPalette.ColorRole.Text,            text)
        p.setColor(QPalette.ColorRole.Button,          mid)
        p.setColor(QPalette.ColorRole.ButtonText,      text)
        p.setColor(QPalette.ColorRole.Highlight,       acc)
        p.setColor(QPalette.ColorRole.HighlightedText, QColor("#000"))
        self.setPalette(p)
        self.setStyleSheet(
            "QPushButton { background:#1e1e1e; color:#ccc; border:1px solid #333;"
            "border-radius:4px; padding:4px 10px; font-size:12px; }"
            "QPushButton:hover { background:#252525; border-color:#555; color:#fff; }"
            "QPushButton:pressed { background:#111; }"
            "QLineEdit { background:#111; color:#ddd; border:1px solid #333;"
            "border-radius:3px; padding:2px 4px; font-size:12px; }")

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
        hdr.addWidget(_button("📁  Open Capture Folder",
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
        self.feed1 = FeedPanel(1, 5000, self.output_dir, yolo_engine=self.yolo, main_window=self)
        self.feed2 = FeedPanel(2, 5001, self.output_dir, yolo_engine=self.yolo, main_window=self)
        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setStyleSheet("color:#222;")
        self.photo_panel = PhotoPanel(self.output_dir)
        self.photo_panel.setFixedWidth(320)
        content.addWidget(self.feed1, stretch=1)
        content.addWidget(self.feed2, stretch=1)
        content.addWidget(divider)
        content.addWidget(self.photo_panel)
        root.addLayout(content, stretch=1)

        sb = QStatusBar()
        sb.setStyleSheet("color:#444; font-size:11px;")
        self.setStatusBar(sb)
        sb.showMessage(f"Output → {self.output_dir.resolve()}")

    def _tick_display(self):
        self.feed1.update_display()
        self.feed2.update_display()

    def _tick_controller(self):
        if not PYGAME_OK:
            return
        pygame.event.pump()
        count = pygame.joystick.get_count()
        if self._joystick is None and count > 0:
            self._joystick = pygame.joystick.Joystick(0)
            self._joystick.init()
            self.photo_panel.set_controller_name(self._joystick.get_name())
        elif self._joystick is not None and count == 0:
            self._joystick = None
            self.photo_panel.set_controller_name(None)
        for event in pygame.event.get():
            if event.type == pygame.JOYBUTTONDOWN:
                if event.button == CAPTURE_BUTTON:
                    self.photo_panel.trigger_capture()

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
        if PYGAME_OK and hasattr(self, "ctrl_timer"):
            self.ctrl_timer.stop()
        if self.yolo:
            self.yolo.stop()
        self.feed1.stop()
        self.feed2.stop()
        if PYGAME_OK:
            pygame.quit()
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
    app = QApplication(sys.argv)
    app.setApplicationName("RoboSub Vision")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())