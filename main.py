"""
Sony A7 III Live View Monitor
Main application entry point.
"""
import flet as ft
import logging
import os
from camera_handler import CameraHandler
from gui import LiveViewGUI

# Configure logging early; allow runtime override via A7CAM_LOG_LEVEL (e.g., DEBUG)
level_name = os.environ.get('A7CAM_LOG_LEVEL', 'INFO').upper()
level = getattr(logging, level_name, logging.INFO)
log_format = '%(asctime)s %(levelname)s:%(name)s: %(message)s'
logging.basicConfig(level=level, format=log_format)
if os.environ.get('A7CAM_LOG_FILE'):
    fh = logging.FileHandler(os.environ['A7CAM_LOG_FILE'])
    fh.setFormatter(logging.Formatter(log_format))
    logging.getLogger().addHandler(fh)

# Security/verbosity filter: scrub large base64 data URIs from log messages to avoid leaking
# sensitive or noisy binary data into logs (e.g., live frame data:image/...;base64,AAA...)
import re
_base64_datauri_re = re.compile(r"(data:image\/[^\s;]+;base64,)[A-Za-z0-9+/=\s]+", re.IGNORECASE)

class Base64ScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Format the message once to capture args
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        new_msg = _base64_datauri_re.sub(r"\1<BASE64_SNIPPED>", msg)
        if new_msg != msg:
            # Replace the record message with sanitized version and clear args
            record.msg = new_msg
            record.args = ()
        return True

logging.getLogger().addFilter(Base64ScrubFilter())

# Also wrap handlers with a sanitizing formatter to ensure base64 URIs are scrubbed
class SanitizingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        s = super().format(record)
        return _base64_datauri_re.sub(r"\1<BASE64_SNIPPED>", s)

for h in logging.getLogger().handlers:
    try:
        h.setFormatter(SanitizingFormatter(log_format))
    except Exception:
        pass

logger = logging.getLogger(__name__)
logger.info("Starting application")


def main(page: ft.Page):
    """
    Main application function.
    
    Args:
        page: Flet page object
    """
    # Initialize camera handler
    camera = CameraHandler()
    
    # Initialize and build GUI
    gui = LiveViewGUI(camera)
    gui.build(page)


if __name__ == "__main__":
    # Launch Flet application
    ft.run(main, view=ft.AppView.FLET_APP)
