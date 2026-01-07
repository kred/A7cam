"""
Image Preview Module
Handles RAW thumbnail extraction, JPEG processing, EXIF rotation, and preview caching.
"""
import os
import threading
from typing import Optional, List, Tuple, Callable
from dataclasses import dataclass
from datetime import datetime

try:
    from PIL import Image, ExifTags
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import rawpy
    HAS_RAWPY = True
except ImportError:
    HAS_RAWPY = False

# RAW file extensions
RAW_EXTENSIONS = {'.arw', '.nef', '.cr2', '.cr3', '.rw2', '.raf', '.orf', '.dng', '.pef', '.srw'}
JPEG_EXTENSIONS = {'.jpg', '.jpeg'}

import logging
from pathlib import Path
import platform
import re

logger = logging.getLogger(__name__)


def get_user_pictures_dir() -> str:
    """Return the user's Pictures/Images directory for the current platform.

    Strategy (platform-specific):
      - Windows: use SHGetFolderPathW (CSIDL_MYPICTURES) via ctypes when available
      - Linux: parse ~/.config/user-dirs.dirs for XDG_PICTURES_DIR when present
      - macOS/Other: fall back to ~/Pictures

    Return value is an absolute path string.
    """
    home = Path.home()
    system = platform.system()

    # Windows: try SHGetFolderPathW (CSIDL_MYPICTURES = 0x0027)
    if system == "Windows":
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            res = ctypes.windll.shell32.SHGetFolderPathW(None, 0x0027, None, 0, buf)
            if res == 0 and buf.value:
                return str(Path(buf.value))
        except Exception:
            pass
        # Fallback
        return str(home / "Pictures")

    # Linux: parse XDG user dirs config
    if system == "Linux":
        try:
            cfg = home / ".config" / "user-dirs.dirs"
            if cfg.exists():
                txt = cfg.read_text()
                m = re.search(r'XDG_PICTURES_DIR=(?P<val>.+)$', txt, flags=re.MULTILINE)
                if m:
                    val = m.group('val').strip().strip('"')
                    val = val.replace('$HOME', str(home))
                    p = Path(val).expanduser()
                    return str(p)
        except Exception:
            pass
        return str(home / "Pictures")

    # macOS and other: default to ~/Pictures
    return str(home / "Pictures")


@dataclass
class CachedImage:
    """Represents a cached preview image."""
    filepath: str
    filename: str
    timestamp: float
    is_raw: bool


class ImagePreviewManager:
    """Manages image preview extraction, caching, and navigation."""
    
    def __init__(self, download_dir: Optional[str] = None, max_cache_size: int = 50):
        """
        Initialize the preview manager.
        
        Args:
            download_dir: Directory where downloaded files are stored. If None, defaults
                to the user's Pictures folder (platform-specific) with a `StudioTether`
                subdirectory (e.g., ~/Pictures/StudioTether).
            max_cache_size: Maximum number of images to cache
        """
        # Compute a sensible default download directory when not provided
        if download_dir is None:
            pictures_dir = get_user_pictures_dir()
            download_dir = os.path.join(pictures_dir, "StudioTether")

        self.download_dir = download_dir
        # Ensure the download directory exists
        try:
            os.makedirs(self.download_dir, exist_ok=True)
        except Exception as e:
            logger.warning("Could not create download dir %s: %s", self.download_dir, e)

        self.max_cache_size = max_cache_size
        self._cache: List[CachedImage] = []
        self._cache_lock = threading.Lock()
        self._current_index = -1  # -1 means live view mode
        
        # Callback for when a new image is ready to preview
        self._preview_callback: Optional[Callable[[CachedImage], None]] = None
        
        # Track pending RAW files waiting for their JPEG pair
        self._pending_raw: dict = {}  # filename_base -> (raw_path, timestamp)
        self._pair_timeout = 2.0  # seconds to wait for RAW+JPEG pair
    
    def set_preview_callback(self, callback: Callable[[CachedImage], None]):
        """Set callback invoked when a new preview is ready."""
        self._preview_callback = callback
    
    def cleanup_download_folder(self):
        """Scan the download folder and load any existing JPEGs into the cache.

        IMPORTANT: This no longer deletes files on startup. Existing RAW/JPEG files
        are preserved and any found JPEGs are loaded into the in-memory cache so
        they are available for preview navigation.
        """
        try:
            # Ensure the download directory exists
            if not os.path.exists(self.download_dir):
                try:
                    os.makedirs(self.download_dir, exist_ok=True)
                except Exception as e:
                    logger.warning("Could not create download dir %s: %s", self.download_dir, e)
                    return

            jpeg_files = []
            for filename in os.listdir(self.download_dir):
                filepath = os.path.join(self.download_dir, filename)
                try:
                    if os.path.isfile(filepath):
                        ext = os.path.splitext(filename)[1].lower()
                        # Only consider JPEG files for loading into cache
                        if ext in JPEG_EXTENSIONS:
                            jpeg_files.append((filepath, filename))
                except Exception as e:
                    logger.warning("Cleanup: failed to inspect %s: %s", filename, e)

            # Load existing JPEGs into cache (sorted for deterministic order)
            if jpeg_files:
                logger.info("Loading %d existing JPEG(s) into cache", len(jpeg_files))
                jpeg_files.sort()
                for filepath, filename in jpeg_files:
                    self._load_jpeg_to_cache(filepath, filename)
        except Exception as e:
            logger.warning("Cleanup failed: %s", e)
    
    def _load_jpeg_to_cache(self, filepath: str, filename: str):
        """Load an existing JPEG file into the cache."""
        try:
            # Convert to absolute path for Flet
            filepath = os.path.abspath(filepath)
            
            cached = CachedImage(
                filepath=filepath,
                filename=filename,
                timestamp=os.path.getmtime(filepath),
                is_raw=False
            )
            
            with self._cache_lock:
                self._cache.append(cached)
                self._current_index = len(self._cache) - 1
            
            logger.debug("Cleanup: loaded %s into cache", filename)
            
        except Exception as e:
            logger.warning("Failed to load %s: %s", filename, e)
    
    def process_downloaded_file(self, filepath: str, filetype: str):
        """
        Process a newly downloaded file.
        
        Args:
            filepath: Path to the downloaded file
            filetype: 'arw' for RAW files, 'jpg' for JPEG files
        """
        if not os.path.exists(filepath):
            return
        
        filename = os.path.basename(filepath)
        # Strip 'capt_' prefix if present (silent)
        if filename.lower().startswith('capt_'):
            new_filename = filename[5:]  # Remove 'capt_'
            new_filepath = os.path.join(os.path.dirname(filepath), new_filename)
            try:
                os.rename(filepath, new_filepath)
                filepath = new_filepath
                filename = new_filename
            except Exception as e:
                # Non-fatal; continue with original filename
                logger.warning("ImagePreview: failed to rename: %s", e)
        
        base_name = os.path.splitext(filename)[0]
        ext = os.path.splitext(filename)[1].lower()
        is_raw = ext in RAW_EXTENSIONS
        
        now = datetime.now().timestamp()
        
        # Check if this is part of a RAW+JPEG pair
        if is_raw:
            # Check if JPEG already exists
            jpeg_path = self._find_jpeg_pair(filepath)
            if jpeg_path:

                # JPEG exists, use it and delete RAW
                self._process_jpeg_file(jpeg_path)
                self._safe_delete(filepath)
            else:
                # Immediately process RAW in background (no 2s wait)

                self._pending_raw[base_name] = (filepath, now)
                t = threading.Thread(target=self._process_raw_file, args=(filepath,), daemon=True)
                t.start()
                # Schedule a short-lived pending clear to avoid indefinite entries
                threading.Timer(self._pair_timeout, 
                                lambda bn=base_name: self._clear_pending_raw(bn)).start()
        else:
            # JPEG file
            # If a RAW was pending/processed, prefer the real JPEG: replace cached thumbnail
            if base_name in self._pending_raw:
                raw_path, _ = self._pending_raw.pop(base_name)
                logger.info("ImagePreview: JPEG arrived for %s, updating cache", base_name)
                # Process the incoming JPEG and delete RAW (raw may have been removed already)
                self._process_jpeg_file(filepath)
                self._safe_delete(raw_path)
            else:
                # Standalone JPEG

                self._process_jpeg_file(filepath)
    
    def _check_pending_raw(self, base_name: str):
        """Legacy timer path — delegate to _clear_pending_raw which will schedule processing if needed."""
        self._clear_pending_raw(base_name)

    def _clear_pending_raw(self, base_name: str):
        """Clear a pending RAW entry; if file still exists, schedule processing."""
        if base_name not in self._pending_raw:
            return
        raw_path, _ = self._pending_raw.pop(base_name)
        if os.path.exists(raw_path):
            t = threading.Thread(target=self._process_raw_file, args=(raw_path,), daemon=True)
            t.start()
    
    def _find_jpeg_pair(self, raw_path: str) -> Optional[str]:
        """Find a JPEG file with the same base name as the RAW file."""
        base_name = os.path.splitext(os.path.basename(raw_path))[0]
        dir_path = os.path.dirname(raw_path)
        
        for ext in JPEG_EXTENSIONS:
            jpeg_path = os.path.join(dir_path, base_name + ext)
            if os.path.exists(jpeg_path):
                return jpeg_path
            jpeg_path = os.path.join(dir_path, base_name + ext.upper())
            if os.path.exists(jpeg_path):
                return jpeg_path
        return None
    
    def _process_jpeg_file(self, filepath: str):
        """Process a JPEG file: apply EXIF rotation and cache."""
        try:
            filename = os.path.basename(filepath)
            
            # Save to disk with .jpg extension
            base_name = os.path.splitext(filename)[0]
            jpeg_filename = f"{base_name}.jpg"
            jpeg_filepath = os.path.join(self.download_dir, jpeg_filename)
            # Convert to absolute path for Flet
            jpeg_filepath = os.path.abspath(jpeg_filepath)
            
            if HAS_PIL:
                # Load and auto-rotate based on EXIF
                img = Image.open(filepath)
                img = self._apply_exif_rotation(img)
                
                # Save to disk
                img.save(jpeg_filepath, format='JPEG', quality=90)
            else:
                # Fallback: copy file
                import shutil
                shutil.copy2(filepath, jpeg_filepath)
            
            cached = CachedImage(
                filepath=jpeg_filepath,
                filename=jpeg_filename,
                timestamp=datetime.now().timestamp(),
                is_raw=False
            )
            
            # Replace existing cached thumbnail for this base name or add new
            self._replace_or_add_cached(cached)
            
        except Exception as e:
            logger.warning("Failed to process JPEG %s: %s", filepath, e)
    
    def _process_raw_file(self, filepath: str):
        """Extract embedded JPEG from RAW file and cache."""
        try:
            filename = os.path.basename(filepath)
            jpeg_data = self._extract_jpeg_from_raw(filepath)
            
            if not jpeg_data:
                # No thumbnail extracted
                return
            
            # Save to disk with .jpg extension
            base_name = os.path.splitext(filename)[0]
            jpeg_filename = f"{base_name}.jpg"
            jpeg_filepath = os.path.join(self.download_dir, jpeg_filename)            # Convert to absolute path for Flet
            jpeg_filepath = os.path.abspath(jpeg_filepath)            
            if HAS_PIL:
                # Load extracted JPEG and apply EXIF rotation
                import io
                img = Image.open(io.BytesIO(jpeg_data))
                img = self._apply_exif_rotation(img)
                
                # Save to disk
                img.save(jpeg_filepath, format='JPEG', quality=90)
            else:
                # Save raw JPEG data
                with open(jpeg_filepath, 'wb') as f:
                    f.write(jpeg_data)
            
            # Reference the saved JPEG file
            cached = CachedImage(
                filepath=jpeg_filepath,
                filename=jpeg_filename,
                timestamp=datetime.now().timestamp(),
                is_raw=False  # Now it's a JPEG on disk
            )
            
            # Replace existing cached thumbnail for this base name or add new
            self._replace_or_add_cached(cached)
            # After saving extracted JPEG, delete RAW file
            try:
                self._safe_delete(filepath)
            except Exception as e:
                logger.warning("ImagePreview: failed to delete RAW after extraction: %s", e)
            
        except Exception as e:
            logger.warning("Failed to process RAW %s: %s", filepath, e)
    
    def _extract_jpeg_from_raw(self, filepath: str) -> Optional[bytes]:
        """
        Extract embedded JPEG thumbnail from RAW file using rawpy.
        
        Falls back to manual extraction if rawpy is not available.
        """

        
        # Try rawpy first (preferred method)
        if HAS_RAWPY:
            try:
                with rawpy.imread(filepath) as raw:
                    # Extract the embedded JPEG thumbnail
                    thumb = raw.extract_thumb()
                    if thumb.format == rawpy.ThumbFormat.JPEG:

                        return thumb.data
                    elif thumb.format == rawpy.ThumbFormat.BITMAP:
                        # Convert bitmap to JPEG

                        import io
                        import numpy as np
                        if HAS_PIL:
                            img = Image.fromarray(thumb.data)
                            buffer = io.BytesIO()
                            img.save(buffer, format='JPEG', quality=90)
                            jpeg_data = buffer.getvalue()

                            return jpeg_data
            except Exception as e:
                logger.info("ImagePreview: rawpy extraction failed, falling back to manual method")
        
        # Fallback: manual JPEG extraction
        try:
            with open(filepath, 'rb') as f:
                data = f.read()
            
            jpegs = []
            start = 0
            while True:
                # Find JPEG start marker (FFD8)
                soi = data.find(b'\xff\xd8', start)
                if soi == -1:
                    break
                # Find JPEG end marker (FFD9)
                eoi = data.find(b'\xff\xd9', soi + 2)
                if eoi == -1:
                    break
                jpeg_data = data[soi:eoi + 2]
                # Only consider reasonably sized JPEGs (> 10KB)
                if len(jpeg_data) > 10000:
                    jpegs.append(jpeg_data)
                start = eoi + 2
            
            if jpegs:
                # Return the largest JPEG
                largest = max(jpegs, key=len)

                return largest
            
            logger.debug("ImagePreview: no embedded JPEG found in RAW file")
            return None
            
        except Exception as e:
            logger.warning("ImagePreview: failed to extract JPEG from RAW: %s", e)
            return None
    
    def _apply_exif_rotation(self, img: Image.Image) -> Image.Image:
        """Apply rotation based on EXIF orientation tag."""
        try:
            exif = img.getexif()
            if not exif:
                return img
            
            # Find orientation tag
            orientation_tag = None
            for tag_id, tag_name in ExifTags.TAGS.items():
                if tag_name == 'Orientation':
                    orientation_tag = tag_id
                    break
            
            if orientation_tag is None or orientation_tag not in exif:
                return img
            
            orientation = exif[orientation_tag]
            
            # Apply rotation based on EXIF orientation
            if orientation == 2:
                img = img.transpose(Image.FLIP_LEFT_RIGHT)
            elif orientation == 3:
                img = img.rotate(180, expand=True)
            elif orientation == 4:
                img = img.transpose(Image.FLIP_TOP_BOTTOM)
            elif orientation == 5:
                img = img.transpose(Image.FLIP_LEFT_RIGHT).rotate(90, expand=True)
            elif orientation == 6:
                img = img.rotate(270, expand=True)
            elif orientation == 7:
                img = img.transpose(Image.FLIP_LEFT_RIGHT).rotate(270, expand=True)
            elif orientation == 8:
                img = img.rotate(90, expand=True)
            
            return img
            
        except Exception as e:
            logger.debug("EXIF rotation failed: %s", e)
            return img
    
    def _add_to_cache(self, cached: CachedImage):
        """Add image to cache and notify callback."""
        with self._cache_lock:
            self._cache.append(cached)
            
            # Trim cache if too large
            while len(self._cache) > self.max_cache_size:
                self._cache.pop(0)
            
            # Set current index to newest image
            self._current_index = len(self._cache) - 1
        
        logger.info("ImagePreview: added to cache: %s (cache size: %d)", cached.filename, len(self._cache))
        
        # Notify callback
        if self._preview_callback:
            try:
                self._preview_callback(cached)
            except Exception as e:
                logger.exception("ImagePreview: Preview callback failed")

    def _replace_or_add_cached(self, cached: CachedImage):
        """Replace an existing cached image with same base name or add new one."""
        base_name = os.path.splitext(cached.filename)[0]
        with self._cache_lock:
            for idx, entry in enumerate(self._cache):
                if os.path.splitext(entry.filename)[0] == base_name:
                    self._cache[idx] = cached
                    self._current_index = idx
                    logger.debug("ImagePreview: replaced cached %s at index %d", cached.filename, idx)
                    # Notify callback for replacement
                    if self._preview_callback:
                        try:
                            self._preview_callback(cached)
                        except Exception as e:
                            logger.exception("ImagePreview: Preview callback failed on replace")
                    return
            # Not found — append normally
            self._cache.append(cached)
            while len(self._cache) > self.max_cache_size:
                self._cache.pop(0)
            self._current_index = len(self._cache) - 1
        logger.info("ImagePreview: added to cache: %s (cache size: %d)", cached.filename, len(self._cache))
        # Notify callback for new item
        if self._preview_callback:
            try:
                self._preview_callback(cached)
            except Exception as e:
                logger.exception("ImagePreview: Preview callback failed")

    def _safe_delete(self, filepath: str):
        """Safely delete a file."""
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                logger.debug("Deleted: %s", os.path.basename(filepath))
        except Exception as e:
            logger.warning("Failed to delete %s: %s", filepath, e)
    
    def get_current_preview(self) -> Optional[CachedImage]:
        """Get the currently selected preview image."""
        with self._cache_lock:
            if self._current_index < 0 or self._current_index >= len(self._cache):
                return None
            return self._cache[self._current_index]
    
    def get_latest_preview(self) -> Optional[CachedImage]:
        """Get the most recent preview image."""
        with self._cache_lock:
            if not self._cache:
                return None
            self._current_index = len(self._cache) - 1
            return self._cache[self._current_index]
    
    def navigate_previous(self) -> Optional[CachedImage]:
        """Navigate to previous (older) image in cache. Cycles to newest."""
        with self._cache_lock:
            if not self._cache:
                logger.debug("ImagePreview: navigate_previous called but cache empty")
                return None
            self._current_index -= 1
            if self._current_index < 0:
                self._current_index = len(self._cache) - 1
            logger.debug("ImagePreview: navigate_previous -> index=%d, filename=%s", self._current_index, self._cache[self._current_index].filename)
            return self._cache[self._current_index]
    
    def navigate_next(self) -> Optional[CachedImage]:
        """Navigate to next (newer) image in cache. Cycles to oldest."""
        with self._cache_lock:
            if not self._cache:
                logger.debug("ImagePreview: navigate_next called but cache empty")
                return None
            self._current_index += 1
            if self._current_index >= len(self._cache):
                self._current_index = 0
            logger.debug("ImagePreview: navigate_next -> index=%d, filename=%s", self._current_index, self._cache[self._current_index].filename)
            return self._cache[self._current_index]
    
    def get_cache_info(self) -> Tuple[int, int]:
        """Return (current_index, total_count) for UI display."""
        with self._cache_lock:
            return (self._current_index + 1, len(self._cache))
    
    def has_cached_images(self) -> bool:
        """Check if there are any cached images."""
        with self._cache_lock:
            return len(self._cache) > 0
    
    def reset_to_live_view(self):
        """Reset state to indicate live view mode."""
        self._current_index = -1
