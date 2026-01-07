"""
Camera Handler Module
Manages camera connection, preview capture, and image processing for supported mirrorless cameras (e.g., Sony A7 series).
"""
import gphoto2 as gp
import cv2
import numpy as np
import base64
import threading
import time
import io
import os

# Optional: Pillow for EXIF parsing
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

import logging
logger = logging.getLogger(__name__)


class CameraHandler:
    """Manages camera connection and frame capture with error handling."""
    
    # Orientation mapping (EXIF-like codes)
    ORIENTATION_NORMAL = 1
    ORIENTATION_180 = 3
    ORIENTATION_270 = 6  # 90 CCW
    ORIENTATION_90 = 8   # 90 CW
    
    def __init__(self):
        """Initialize camera handler with default settings."""
        self.camera = None
        self.context = None
        self.is_streaming = False
        self.lost_device = False
        self.orientation = self.ORIENTATION_NORMAL
        
        # Thread safety
        self.camera_lock = threading.Lock()
        
        # I/O error handling
        self._io_error_counter = 0
        self._io_error_threshold = 5
        self._retry_delays = [0.0, 0.1, 0.5]  # Backoff delays for retries
        # Logging throttle for repeated corrupt frames (seconds)
        self._last_corrupt_frame_log = 0.0
        # Consecutive corrupt frame tracking - to escalate to device lost and stop streaming
        self._consecutive_corrupt_frames = 0
        self._consecutive_corrupt_threshold = 5
        # Throttle captures to avoid hammering the camera/USB bus
        self._min_frame_interval = 0.03  # seconds between preview grabs (30ms = ~33fps max)
        self._last_capture_ts = 0.0

        # Tethering support (background thread)
        # Directory to save incoming tethered files
        self._tether_download_dir = "./downloads"
        # Optional callback invoked when a file is downloaded: callback(path, filetype)
        self._tether_callback = None
        self._tether_running = False
        self._tether_stop_event = None
        self._tether_thread = None

    def connect(self):
        """
        Connect to the camera and initialize.
        
        Returns:
            tuple: (success: bool, message: str)
        """
        try:
            self.context = gp.Context()
            self.camera = gp.Camera()
            self.camera.init(self.context)
            self.lost_device = False
            
            # Get camera model info
            summary = self.camera.get_summary(self.context).text.splitlines()[0]
            return True, f"Connected: {summary}"
        except Exception as e:
            self.lost_device = True
            return False, str(e)

    def get_frame_base64(self):
        """
        Capture a preview frame and return it as a base64-encoded JPEG.
        
        Implements retry logic with exponential backoff for transient I/O errors.
        
        Returns:
            str: Base64-encoded JPEG string, or None on error
        """
        if not self.camera:
            return None

        file_data = self._capture_preview_with_retry()
        if not file_data:
            return None

        return self._process_frame_to_base64(file_data)

    def _capture_preview_with_retry(self):
        """
        Capture preview with retry logic for I/O busy errors.
        
        Returns:
            bytes: Preview image data, or None on failure
        """
        for attempt, delay in enumerate(self._retry_delays, start=1):
            try:
                # Enforce a minimal interval between captures to reduce truncated frames
                now = time.time()
                elapsed = now - self._last_capture_ts
                if elapsed < self._min_frame_interval:
                    time.sleep(self._min_frame_interval - elapsed)

                with self.camera_lock:
                    # Poll for any pending camera events (file downloads, etc.) first
                    # This is quick and non-blocking (0ms timeout)
                    self._poll_events_unlocked()
                    
                    camera_file = gp.check_result(gp.gp_camera_capture_preview(self.camera))
                    file_data = gp.check_result(gp.gp_file_get_data_and_size(camera_file))
                    # CRITICAL: Copy the memoryview to bytes immediately to avoid corruption
                    # The memoryview may reference camera internal buffers that get reused
                    if isinstance(file_data, memoryview):
                        file_data = bytes(file_data)
                
                # Process any queued downloads outside the lock
                self._process_pending_downloads()
                
                # Success - reset error counter
                self._io_error_counter = 0
                self._last_capture_ts = time.time()
                return file_data
                
            except Exception as e:
                if self._handle_capture_error(e, attempt, len(self._retry_delays)):
                    time.sleep(delay)
                    continue
                return None
        
        # Exceeded retries
        if self._io_error_counter >= self._io_error_threshold:
            logger.warning("Too many I/O busy errors (-110); marking device lost")
            self.lost_device = True
        return None

    def _handle_capture_error(self, error, attempt, max_attempts):
        """
        Handle errors during preview capture.
        
        Returns:
            bool: True if should retry, False otherwise
        """
        err_str = str(error)
        error_code = getattr(error, "code", None)
        
        # I/O busy error (transient)
        if "-110" in err_str or "I/O in progress" in err_str or error_code == -110:
            self._io_error_counter += 1
            logger.debug("I/O busy (-110) during preview capture, attempt %d/%d", attempt, max_attempts)
            return True
        
        # USB disconnect error (permanent)
        if "-52" in err_str or "Could not find the requested device" in err_str:
            logger.error("Device lost on USB: %s", err_str)
            self.lost_device = True
            try:
                self.release()
            except Exception:
                pass
            # Notify GUI/watcher about disconnect
            try:
                if hasattr(self, "_disconnect_callback") and self._disconnect_callback:
                    self._disconnect_callback(False, err_str)
            except Exception:
                pass
            return False

        # Treat unspecified or fatal errors as disconnects as well
        if "-1" in err_str or "Unspecified error" in err_str:
            logger.exception("Fatal device error detected: %s — marking device lost", err_str)
            self.lost_device = True
            try:
                self.release()
            except Exception:
                pass
            # Notify GUI/watcher about disconnect
            try:
                if hasattr(self, "_disconnect_callback") and self._disconnect_callback:
                    self._disconnect_callback(False, err_str)
            except Exception:
                pass
            return False

        # Other errors
        logger.error("Frame capture error: %s", err_str)
        return False

    @staticmethod
    def _trim_to_eoi(file_data):
        """Best-effort JPEG normalization: ensure SOI and trim at the last EOI.

        Some cameras (or transports) append padding/metadata after the EOI marker,
        which causes strict EOI checks to fail even though the image decodes fine.
        This routine keeps data from SOI to the last EOI if both markers exist.
        Returns trimmed bytes or None if markers are missing/too short.
        
        Note: Memoryview conversion happens in _capture_preview_with_retry to
        prevent buffer reuse corruption.
        """
        if not isinstance(file_data, (bytes, bytearray)):
            return None
        if len(file_data) < 4:
            return None

        # Require a JPEG SOI at the start
        if not file_data.startswith(b"\xff\xd8"):
            return None

        # Find the last End Of Image marker and trim anything after it
        eoi_idx = file_data.rfind(b"\xff\xd9")
        if eoi_idx == -1 or eoi_idx < 2:
            return None

        return file_data[: eoi_idx + 2]

    def _process_frame_to_base64(self, file_data):
        """
        Process raw preview data to base64 JPEG with rotation applied.
        
        Args:
            file_data: Raw JPEG bytes from camera (already converted from memoryview)
            
        Returns:
            str: Base64-encoded JPEG, or None on error
        """
        try:
            # Normalize JPEG by trimming to the last EOI; skip if markers are missing
            normalized = self._trim_to_eoi(file_data)
            if not normalized:
                self._io_error_counter += 1
                self._consecutive_corrupt_frames += 1
                now = time.time()
                if now - self._last_corrupt_frame_log > 1.0:
                    logger.warning("Incomplete or corrupted JPEG frame received; skipping frame")
                    self._last_corrupt_frame_log = now
                return None
            file_data = normalized

            # Fast path: if no rotation needed, skip decode/encode cycle
            if self.orientation == self.ORIENTATION_NORMAL:
                self._io_error_counter = 0
                self._consecutive_corrupt_frames = 0
                b64_str = base64.b64encode(file_data).decode('utf-8')
                return f"data:image/jpeg;base64,{b64_str}"

            # Slow path: decode, rotate, re-encode
            data = np.frombuffer(file_data, dtype=np.uint8)
            img = cv2.imdecode(data, cv2.IMREAD_COLOR)
            
            if img is None:
                self._io_error_counter += 1
                self._consecutive_corrupt_frames += 1
                logger.warning("Failed to decode preview image")
                return None
            
            # Apply rotation
            img = self._apply_rotation(img)
            
            # Compress to JPEG (quality 85 for better quality when rotating)
            success, buffer = cv2.imencode('.jpg', img, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            if not success:
                logger.warning("Failed to encode frame to JPEG")
                return None
            
            # Convert to base64
            # reset intermittent error counter on success
            self._io_error_counter = 0
            self._consecutive_corrupt_frames = 0
            # Return base64 string with data URI prefix for Flet Image control
            b64_str = base64.b64encode(buffer).decode('utf-8')
            return f"data:image/jpeg;base64,{b64_str}"
            
        except Exception as e:
            err_str = str(e)
            # If we see device-level errors during processing, mark device lost and release
            if "-52" in err_str or "Could not find the requested device" in err_str:
                logger.error("Device lost during frame processing: %s", err_str)
                self.lost_device = True
                try:
                    self.release()
                except Exception:
                    pass
                try:
                    if hasattr(self, "_disconnect_callback") and self._disconnect_callback:
                        self._disconnect_callback(False, err_str)
                except Exception:
                    pass
                return None
            if "-1" in err_str or "Unspecified error" in err_str:
                logger.exception("Fatal device error during frame processing: %s", err_str)
                self.lost_device = True
                try:
                    self.release()
                except Exception:
                    pass
                try:
                    if hasattr(self, "_disconnect_callback") and self._disconnect_callback:
                        self._disconnect_callback(False, err_str)
                except Exception:
                    pass
                return None
            logger.exception("Frame processing error")
            return None

    def _apply_rotation(self, img):
        """
        Apply rotation to image based on current orientation setting.
        
        Args:
            img: OpenCV image (numpy array)
            
        Returns:
            np.ndarray: Rotated image
        """
        if self.orientation == self.ORIENTATION_180:
            return cv2.rotate(img, cv2.ROTATE_180)
        elif self.orientation == self.ORIENTATION_270:
            return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
        elif self.orientation == self.ORIENTATION_90:
            return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
        return img

    def set_orientation(self, orientation_code):
        """
        Set manual rotation orientation.
        
        Args:
            orientation_code: One of ORIENTATION_* constants
        """
        if orientation_code in (self.ORIENTATION_NORMAL, self.ORIENTATION_90, 
                               self.ORIENTATION_180, self.ORIENTATION_270):
            self.orientation = orientation_code

    def release(self):
        """Release camera resources and reset state."""
        # Ensure tethering is stopped when releasing the device
        try:
            self.stop_tether()
        except Exception:
            pass

        if self.camera:
            # Try graceful exit
            try:
                gp.check_result(gp.gp_camera_exit(self.camera))
            except Exception:
                try:
                    self.camera.exit(self.context)
                except Exception:
                    pass
            
            # Attempt to unref if available (may be no-op)
            self._safe_unref(gp, 'gp_camera_unref', self.camera)
        
        # Unref context if available
        if self.context:
            self._safe_unref(gp, 'gp_context_unref', self.context)
        
        # Reset state
        self.camera = None
        self.context = None
        self.is_streaming = False
        self.lost_device = False
        self.orientation = self.ORIENTATION_NORMAL
        self._io_error_counter = 0
        self._last_capture_ts = 0.0

    # --- Camera availability watcher ---
    def start_watch(self, callback=None, poll_interval: float = 2.0):
        """Start background watcher that polls for camera availability.

        Args:
            callback: Optional callable(success: bool, message: str) invoked when camera connects.
            poll_interval: Seconds between polls.
        """
        if getattr(self, "_watch_running", False):
            return
        self._watch_callback = callback
        self._watch_stop_event = threading.Event()
        self._watch_thread = threading.Thread(target=self._watch_loop, args=(poll_interval,), daemon=True)
        self._watch_thread.start()
        self._watch_running = True

    def set_disconnect_callback(self, callback):
        """Register a callback that is called when the device is detected as lost.

        The callback signature is callback(success: bool, message: str).
        """
        self._disconnect_callback = callback


    def stop_watch(self):
        """Stop the background watcher."""
        if getattr(self, "_watch_running", False) and hasattr(self, "_watch_stop_event"):
            self._watch_stop_event.set()
            try:
                self._watch_thread.join(timeout=1.0)
            except Exception:
                pass
            self._watch_running = False

    # --- Tethering support (inline event polling) ---
    def start_tether(self, callback=None, poll_interval: float = 0.5):
        """Enable tethering. Events are polled inline during preview capture.

        callback(path, filetype) will be invoked when a file is saved locally.
        """
        self._tether_callback = callback
        self._tether_running = True
        logger.info("Tether: enabled (download dir=%s)", self._tether_download_dir)

    def stop_tether(self):
        """Disable tethering."""
        self._tether_running = False

    def set_tether_callback(self, callback):
        """Register a callback invoked when tethered files are saved locally."""
        self._tether_callback = callback

    def _poll_events_unlocked(self):
        """Poll for camera events without blocking. Must be called with camera_lock held.
        
        Downloads any new files detected via FILE_ADDED events.
        """
        if not getattr(self, '_tether_running', False):
            return
        if self.camera is None:
            return
        
        # Drain all pending events with 0ms timeout (non-blocking)
        try:
            while True:
                evt = self.camera.wait_for_event(0)  # 0ms = immediate return
                if isinstance(evt, (tuple, list)) and len(evt) >= 2:
                    event_type, event_data = evt[0], evt[1]
                else:
                    break
                
                # Timeout means no more events
                if event_type == gp.GP_EVENT_TIMEOUT:
                    break
                
                # Handle FILE_ADDED
                if event_type == gp.GP_EVENT_FILE_ADDED:
                    folder = getattr(event_data, 'folder', None)
                    filename = getattr(event_data, 'name', None)
                    if not folder or not filename:
                        if isinstance(event_data, (tuple, list)) and len(event_data) >= 2:
                            folder = folder or event_data[0]
                            filename = filename or event_data[1]
                    
                    if filename:
                        # Queue download (will be processed after releasing lock)
                        if not hasattr(self, '_pending_downloads'):
                            self._pending_downloads = []
                        self._pending_downloads.append((folder, filename))
        except Exception:
            pass

    def _process_pending_downloads(self):
        """Download any files queued by _poll_events_unlocked. Called outside the lock."""
        if not hasattr(self, '_pending_downloads') or not self._pending_downloads:
            return
        
        downloads = self._pending_downloads
        self._pending_downloads = []
        
        for folder, filename in downloads:
            logger.info("Tether: FILE_ADDED -> folder=%s, filename=%s", folder, filename)
            ext = os.path.splitext(filename)[1].lower()
            filetype = 'arw' if ext in ('.arw', '.nef', '.cr2', '.rw2', '.raf') else 'jpg'
            
            path = self._download_file(folder, filename)
            if path and self._tether_callback:
                try:
                    logger.debug("Tether: invoking callback for %s", path)
                    self._tether_callback(path, filetype)
                except Exception as e:
                    logger.exception("Tether: callback failed")
            elif path:
                logger.warning("Tether: WARNING - no callback registered for %s", path)

    def _download_file(self, folder, filename):
        """Download a single file from camera to the tether download directory.

        Returns the local path on success or None on failure.
        """
        try:
            os.makedirs(self._tether_download_dir, exist_ok=True)
        except Exception:
            pass
        target_path = os.path.join(self._tether_download_dir, filename)
        try:
            with self.camera_lock:
                # Create a CameraFile object to receive the data
                camera_file = gp.CameraFile()
                gp.check_result(
                    gp.gp_camera_file_get(self.camera, folder, filename, gp.GP_FILE_TYPE_NORMAL, camera_file, self.context)
                )
                gp.check_result(gp.gp_file_save(camera_file, target_path))

            logger.info("Tether: downloaded %s -> %s", filename, target_path)
            return target_path
        except Exception as e:
            logger.exception("Tether download failed for %s", filename)
            return None


    def _watch_loop(self, poll_interval: float):
        """Internal loop that attempts to connect when camera is not present."""
        while not getattr(self, "_watch_stop_event", threading.Event()).is_set():
            try:
                if self.camera is None or self.lost_device:
                    success, msg = self.connect()
                    if success:
                        # Notify via callback if provided
                        try:
                            if self._watch_callback:
                                self._watch_callback(success, msg)
                        except Exception:
                            pass
                        # Auto-start tethering when camera is detected
                        try:
                            if not getattr(self, '_tether_running', False):
                                self.start_tether()
                        except Exception:
                            pass
                time.sleep(poll_interval)
            except Exception:
                time.sleep(poll_interval)

    @staticmethod
    def _safe_unref(gp_module, func_name, obj):
        """Safely call unref function if it exists."""
        try:
            if hasattr(gp_module, func_name):
                getattr(gp_module, func_name)(obj)
        except Exception:
            pass
