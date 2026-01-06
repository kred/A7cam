# Sony A7 III Live View Monitor 🎥

**Short description:** A desktop live-view monitor for Sony A7 III that uses libgphoto2 and Flet to show camera previews, manage tethered downloads, and preview captured images.

## Demo

![Live view demo](img/a7cam.gif)

*A short screencast showing the live preview, preview navigation and overlay guides.*

---

## ⚡ Features

- Live view preview from Sony A7 III via libgphoto2
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

## ⚙️ Configuration / Environment

- `A7CAM_LOG_LEVEL` — optional, defaults to `INFO` (e.g. `DEBUG`)
- `A7CAM_LOG_FILE` — optional, path to write logs to

Example:

```bash
A7CAM_LOG_LEVEL=DEBUG A7CAM_LOG_FILE=./a7cam.log python main.py
```

---

## 📁 Key files & responsibilities

- `main.py` — app entrypoint, logging setup, starts Flet UI
- `camera_handler.py` — camera connection, preview capture, tethering, and robust error handling
- `image_preview.py` — RAW thumbnail extraction, EXIF rotation, preview caching and navigation
- `gui.py` — Flet UI (controls, composition guides, preview overlays)
- `translations.py` — i18n strings used by the UI
- `requirements.txt` — Python package dependencies

Downloaded captures are stored under `./downloads` (configured in `CameraHandler`).

---

## ✅ Quick review findings (code review summary)

- Overall: clear module separation and defensive device handling; good use of small, focused classes.
- Safety: memoryviews from libgphoto2 are converted to `bytes` immediately — this avoids buffer reuse corruption (good!).
- Logging: base64 data URIs are scrubbed to avoid huge log output — useful for safety and readability.

Potential issues / suggestions:

- `ImagePreviewManager.cleanup_download_folder()` appears to remove files including JPEGs and does not append found JPEGs into `jpeg_files` for later loading. The implementation comment suggests JPEGs should be kept and loaded into cache, but the code currently deletes files and never builds `jpeg_files`. Recommend updating the method to only remove RAW files (and any dotfiles), and populate `jpeg_files` with discovered JPEGs to load them into cache.

- `cleanup_download_folder()` currently enumerates and calls `os.remove()` for files matching RAW _or_ JPEG. This may be surprising to users. If the intended behavior is to start clean (delete all), make the behavior clear in docs or add a CLI flag/setting to preserve existing captures.

- Consider adding small unit tests / import checks (e.g., CI job) that validate core behavior like `ImagePreviewManager._find_jpeg_pair()` and rotation functions.

- Add a CONTRIBUTING section and a short PR checklist for maintainers.

---

## 🧪 Tips & Troubleshooting

- If camera isn't detected: ensure `libgphoto2` is installed and the camera is in the correct USB/tether mode.
- On macOS, run `brew install gphoto2` if `gphoto2` is missing.
- If preview frames are truncated or corrupted frequently, try increasing `_min_frame_interval` in `CameraHandler` to give the camera more time between captures.

---

## 🤝 Contributing

Contributions welcome. Suggested small items:

- Fix `cleanup_download_folder()` logic (see review note)
- Add unit tests for `image_preview` extraction and cache behavior
- Add CI workflow for linting and basic import tests

Please open issues or PRs with a clear description and a short test demonstrating the fix/feature.

---

## 📄 License

MIT license

