# StudioTether 🎥

**Short description:** A lightweight desktop live-view tethering utility for mirrorless cameras (supports Sony mirrorless cameras such as the A7 series). It uses libgphoto2 and Flet to show camera previews, manage tethered downloads, and preview captured images.

## Demo

![Live view demo](img/a7cam.gif)

*A short screencast showing the live preview, preview navigation and overlay guides.*

---

## ⚡ Features

- Live view preview from mirrorless cameras via libgphoto2 (supports Sony A7 series)
- Automatic camera watcher and tether download handling
- Embedded RAW thumbnail extraction (uses `rawpy` when available)
- EXIF-aware preview rotation (uses `Pillow` when available)
- Simple preview cache and navigation (previous/next)
- Composition guides and rotation/duration controls in the UI

---

## 🔧 Requirements

- Python 3.11+ (project tested with Python 3.13 in a virtual environment)
- System library: `libgphoto2` (required to communicate with camera)
  - macOS: `brew install gphoto2`
  - **Windows:** Windows ships an MTP/PTP driver that typically prevents `libgphoto2` from accessing the camera. To allow gphoto2 to talk to the Sony A7 series on Windows you will usually need to replace the default driver with a libusb-compatible driver (e.g., **WinUSB** or **libusbK**).

    Quick steps (use with admin privileges):
    1. Download and run Zadig: https://zadig.akeo.ie/ (run as Administrator).
    2. From the Options menu select **"List All Devices"**.
    3. Select your camera device (may show as "MTP", "PTP", "Sony Camera", or similar).
    4. Choose **WinUSB** (or **libusbK**) as the target driver and click **Install Driver**.
    5. Reconnect the camera and verify with `gphoto2 --auto-detect` or your chosen client.

    Notes:
    - Replacing the driver may affect other applications (e.g., media import tools); to revert use Zadig to reinstall the original driver or run Windows Update.
    - On Windows, gphoto2 commonly runs via MSYS2 or WSL (with USB passthrough); consult libgphoto2 documentation for platform-specific guidance.

- Python packages (see `requirements.txt`), notably:
  - `flet` (UI)
  - `gphoto2` Python bindings (may also be provided by the OS packaging system)
  - `opencv-python` (`cv2`), `numpy`
  - Optionals: `rawpy` (better RAW thumbnail extraction), `Pillow` (EXIF rotation)

Install Python deps in your virtualenv:

```bash
python -m pip install -r requirements.txt
```

Note: If the Python `gphoto2` package is not available on PyPI for your platform, install `libgphoto2` and the Python bindings via your OS package manager.

---

## 🚀 Run the app

Activate your virtual environment and run:

```bash
python main.py
```

The app opens a Flet desktop window showing the live preview and status. The app auto-starts its camera watcher; when a camera is detected it will begin handling live preview and tether downloads.

---

## ⌨️ Keyboard Shortcuts

Common keyboard shortcuts used in the app (works with regular keys, numpad variants, and on most platforms):

- **1** / **Numpad1** / **Digit1** — Rotation **0°** (normal)
- **2** / **Numpad2** / **Digit2** — Rotation **90°**
- **3** / **Numpad3** / **Digit3** — Rotation **180°**
- **4** / **Numpad4** / **Digit4** — Rotation **270°**
- **8** / **Numpad8** / **Digit8** — Set preview duration to **3s**
- **9** / **Numpad9** / **Digit9** — Set preview duration to **10s**
- **0** / **Numpad0** / **Digit0** — Set preview duration to **Always** (infinite)
- **G** — Cycle composition guides (press **Shift+G** to cycle backwards)
- **C** — Cycle guide color (white, green, red, black)
- **Space** — Toggle preview mode / return to live view
- **Left / Right Arrow** — Navigate previous / next preview image (only in preview mode)
- **F** — Toggle full-screen mode (press again to restore previous size & position)
- **H** — Show/hide keyboard shortcuts help overlay

> Note: Some platforms may expose slightly different window attributes; the UI prefers the native window API when available and falls back to older page attributes when necessary.

## ⚙️ Configuration / Environment

- `A7CAM_LOG_LEVEL` — optional, defaults to `WARNING` (e.g. `DEBUG`)
- `A7CAM_LOG_FILE` — optional, path to write logs to
- `A7CAM_DOWNLOAD_DIR` — optional, path to override the default download directory (useful if you want captures stored elsewhere)

You can also pass `--download-dir /path/to/StudioTether` to `python main.py` (CLI option takes precedence over `A7CAM_DOWNLOAD_DIR`).

Example:

```bash
A7CAM_LOG_LEVEL=DEBUG A7CAM_LOG_FILE=./a7cam.log python main.py --download-dir /Users/you/Pictures/StudioTether
```
---

## 📁 Key files & responsibilities

- `main.py` — app entrypoint, logging setup, starts Flet UI
- `camera_handler.py` — camera connection, preview capture, tethering, and robust error handling
- `image_preview.py` — RAW thumbnail extraction, EXIF rotation, preview caching and navigation
- `gui.py` — Flet UI (controls, composition guides, preview overlays)
- `translations.py` — i18n strings used by the UI
- `requirements.txt` — Python package dependencies

Downloaded captures are stored by default under your system Pictures/Images folder in a `StudioTether` subdirectory (e.g., `~/Pictures/StudioTether`). You can override this location with the `--download-dir` CLI option or the `A7CAM_DOWNLOAD_DIR` environment variable.

---

## 🧪 Tips & Troubleshooting

- If camera isn't detected: ensure `libgphoto2` is installed and the camera is in the correct USB/tether mode.
- On macOS, run `brew install gphoto2` if `gphoto2` is missing.
- If preview frames are truncated or corrupted frequently, try increasing `_min_frame_interval` in `CameraHandler` to give the camera more time between captures.

---

## 🤝 Contributing

Contributions welcome. Please open issues or PRs with a clear description and a short test demonstrating the fix/feature.

---

## 📄 License

MIT license

