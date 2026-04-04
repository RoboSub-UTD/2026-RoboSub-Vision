import tkinter as tk
from tkinter import ttk, messagebox
import cv2
import numpy as np
from PIL import Image, ImageTk
import os
import time
import threading
import gi
from pathlib import Path

# Import GStreamer
gi.require_version('Gst', '1.0')
from gi.repository import Gst, GLib

# Initialize GStreamer
Gst.init(None)


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
            
        # Create GStreamer pipeline
        pipeline_str = (
            f'udpsrc port={self.port} caps=application/x-rtp,encoding-name=H264,payload=96 ! '
            'rtph264depay ! avdec_h264 ! videoconvert ! '
            'videoflip method=clockwise ! '
            'video/x-raw,format=BGR ! appsink name=sink emit-signals=true max-buffers=1 drop=true'
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

class CameraCaptureApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Dual RTP Camera Feed")
                
        # Initialize GStreamer video sources
        self.rtp_source1 = GstreamerRTPSource(port=5000)
        self.rtp_source2 = GstreamerRTPSource(port=5001)
        
        # Create directory for saved frames
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        self.output_dir = Path("captured_frames") / f"output_{timestamp}"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Create main frame
        self.main_frame = ttk.Frame(root)
        self.main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Frame for both camera displays side by side
        self.cameras_frame = ttk.Frame(self.main_frame)
        self.cameras_frame.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)  # Remove padding

        # Left camera frame (remove LabelFrame border and padding)
        self.left_cam_frame = ttk.Frame(self.cameras_frame)
        self.left_cam_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=0, pady=0)

        self.cam_label1 = ttk.Label(self.left_cam_frame)
        self.cam_label1.pack(padx=0, pady=0)

        self.status_label1 = ttk.Label(self.left_cam_frame, text="")
        self.status_label1.pack(padx=0, pady=0)

        # Right camera frame (remove LabelFrame border and padding)
        self.right_cam_frame = ttk.Frame(self.cameras_frame)
        self.right_cam_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=0, pady=0)

        self.cam_label2 = ttk.Label(self.right_cam_frame)
        self.cam_label2.pack(padx=0, pady=0)

        self.status_label2 = ttk.Label(self.right_cam_frame, text="")
        self.status_label2.pack(padx=0, pady=0)
        
        # Control panel
        self.control_frame = ttk.Frame(root)
        self.control_frame.pack(fill=tk.X, padx=10, pady=10)
        
        # Capture buttons frame
        self.capture_frame = ttk.Frame(self.control_frame)
        self.capture_frame.pack(side=tk.TOP, fill=tk.X, pady=5)
                
        # Capture button for Feed 1
        self.btn_capture1 = ttk.Button(
            self.capture_frame, 
            text="Capture Feed 1", 
            command=lambda: self.capture_frames(1)
        )
        self.btn_capture1.pack(side=tk.LEFT, padx=5)

        # Capture button for Feed 2
        self.btn_capture2 = ttk.Button(
            self.capture_frame, 
            text="Capture Feed 2", 
            command=lambda: self.capture_frames(2)
        )
        self.btn_capture2.pack(side=tk.LEFT, padx=5)

        # Open captured frame folder
        self.btn_open_capture_folder = ttk.Button(
            self.capture_frame,
            text="Open Capture Folder",
            command=lambda: os.system(f"nautilus {self.output_dir}")
        )
        self.btn_open_capture_folder.pack(side=tk.LEFT, padx=5)

        # Connection frame
        self.connection_frame = ttk.Frame(self.control_frame)
        self.connection_frame.pack(side=tk.TOP, fill=tk.X, pady=5)
        
        # Feed 1 port configuration
        self.port_frame1 = ttk.LabelFrame(self.connection_frame, text="Feed 1")
        self.port_frame1.pack(side=tk.LEFT, padx=10, pady=5)
        
        ttk.Label(self.port_frame1, text="Port:").pack(side=tk.LEFT)
        self.port_var1 = tk.StringVar(value="5000")
        self.port_entry1 = ttk.Entry(self.port_frame1, textvariable=self.port_var1, width=6)
        self.port_entry1.pack(side=tk.LEFT, padx=5)
        
        self.btn_connect1 = ttk.Button(
            self.port_frame1,
            text="Connect",
            command=lambda: self.connect_to_stream(1)
        )
        self.btn_connect1.pack(side=tk.LEFT, padx=5)
        
        # Feed 2 port configuration
        self.port_frame2 = ttk.LabelFrame(self.connection_frame, text="Feed 2")
        self.port_frame2.pack(side=tk.LEFT, padx=10, pady=5)
        
        ttk.Label(self.port_frame2, text="Port:").pack(side=tk.LEFT)
        self.port_var2 = tk.StringVar(value="5001")
        self.port_entry2 = ttk.Entry(self.port_frame2, textvariable=self.port_var2, width=6)
        self.port_entry2.pack(side=tk.LEFT, padx=5)

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

        # Start the RTP sources and update loop
        self.running = True
        self.connect_to_stream(1)  # Start Feed 1 with default port
        self.connect_to_stream(2)  # Start Feed 2 with default port
        self.update_frames()
    
    def connect_to_stream(self, feed_number):
        """Connect to RTP stream on specified port for the given feed"""
        if feed_number == 1:
            rtp_source = self.rtp_source1
            port_var = self.port_var1
            status_label = self.status_label1
        else:
            rtp_source = self.rtp_source2
            port_var = self.port_var2
            status_label = self.status_label2
            
        # Stop current stream if running
        if rtp_source.running:
            rtp_source.stop()
                
        # Get port from entry
        try:
            port = int(port_var.get())
            if port < 1 or port > 65535:
                raise ValueError("Port must be between 1 and 65535")
        except ValueError as e:
            messagebox.showerror("Invalid Port", str(e))
            return
                    
        # Update status
        status_label.config(text=f"Connecting to RTP stream on port {port}...")
        self.root.update()
                
        # Create new RTP source with the specified port
        new_source = GstreamerRTPSource(port=port)
                
        # Start the RTP source
        try:
            new_source.start()
            status_label.config(text=f"Connected to RTP stream on port {port}")
            
            # Update the reference to the source
            if feed_number == 1:
                self.rtp_source1 = new_source
            else:
                self.rtp_source2 = new_source
                
        except Exception as e:
            messagebox.showerror("Connection Error", f"Failed to connect to port {port}: {e}")
            status_label.config(text="Connection failed")

    def process_frame(self, frame):
        if frame is None:
            return None
        return frame

    def capture_frames(self, feed_number):
        """Capture 1 frame processing from specified feed"""
        if feed_number == 1:
            rtp_source = self.rtp_source1
            status_label = self.status_label1
        else:
            rtp_source = self.rtp_source2
            status_label = self.status_label2

        frame = rtp_source.get_frame()
        if frame is None:
            messagebox.showerror("Error", f"No video stream available on Feed {feed_number}")
            return

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = os.path.join(self.output_dir, f"feed{feed_number}_{timestamp}.jpg")
        cv2.imwrite(filename, frame)
        self.root.update()
    
    def update_frames(self):
        """Update both camera feed displays"""
        if self.running:
            # Process Feed 1
            self.update_single_frame(
                self.rtp_source1, 
                self.cam_label1, 
                self.status_label1, 
                self.process_var1.get(),
                "Feed 1"
            )
            
            # Process Feed 2
            self.update_single_frame(
                self.rtp_source2, 
                self.cam_label2, 
                self.status_label2, 
                self.process_var2.get(),
                "Feed 2"
            )
            
            # Schedule the next update
            self.root.after(30, self.update_frames)  # ~30 FPS

    def update_single_frame(self, rtp_source, cam_label, status_label, feed_name):
        """Update a single camera feed display"""
        # Get the current frame from the RTP source
        frame = rtp_source.get_frame()
        
        if frame is not None:            
            # Convert to RGB for display
            display_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            
            # Resize if needed (smaller for dual display)
            if display_frame.shape[1] > 522 or display_frame.shape[0] > 928:
                display_frame = cv2.resize(display_frame, (522, 928))
            
            # Convert to PhotoImage and update display
            img = ImageTk.PhotoImage(image=Image.fromarray(display_frame))
            cam_label.config(image=img)
            cam_label.image = img  # Keep a reference to prevent garbage collection
            
            # Update status to show dimensions
            h, w = frame.shape[:2]
            status_label.config(text=f"{feed_name}: {w}x{h}")
        else:
            # No frame available - only update if not already shown as empty
            attribute_name = f'_no_frame_shown_{feed_name}'
            if not getattr(self, attribute_name, False):
                setattr(self, attribute_name, True)
                cam_label.config(image='')
                status_label.config(text=f"Waiting for {feed_name} stream...")

    def close_app(self):
        """Clean up resources and close the application"""
        self.running = False
        
        # Stop both RTP sources
        if hasattr(self, 'rtp_source1'):
            self.rtp_source1.stop()
            
        if hasattr(self, 'rtp_source2'):
            self.rtp_source2.stop()
            
        self.root.destroy()

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
