# Copilot instructions — a7cam (StudioTether)

Purpose: Quickly orient an AI coding agent so it can be productive making focused changes in this repository.

## Big picture (what matters)
- main entry: `main.py` — launches a Flet UI (`ft.run(main)`) and configures logging (env vars below).
- Camera layer: `camera_handler.py` — wraps libgphoto2 (`gphoto2` bindings) to capture preview frames and handle tethered downloads.
  - Key behaviors: retry/backoff for transient I/O (-110), mark device lost on USB errors (-52) or unspecified fatal errors (-1).
  - Important: convert memoryviews to `bytes` immediately to avoid buffer reuse corruption.
  - Preview frames are returned as data-URI `data:image/jpeg;base64,...` strings for the Flet Image control.
- Preview manager: `image_preview.py` — extracts embedded JPEG thumbnails from RAW (uses `rawpy` if available, otherwise manual extraction), applies EXIF rotation (Pillow optional), caches previews in `./downloads`.
- UI: `gui.py` — Flet UI components, keyboard handling (robust Shift detection), composition guides, preview overlay, and background frame fetching thread.
- Localization and labels: `translations.py` — single-file dict of locales + helper functions `t()`, `set_locale()`, `get_system_locale()`.

## How to run & debug locally
- Install runtime deps: python -m pip install -r requirements.txt (macOS: `brew install gphoto2` may be required for camera support).
- Launch app (interactive desktop): python main.py (this runs Flet app).
- Helpful env vars:
  - `A7CAM_LOG_LEVEL` (e.g., DEBUG, INFO). Default is **WARNING** to avoid debug/info noise; set to `NONE` or `OFF` to fully disable logging. — controls logging level
  - `A7CAM_LOG_FILE` — path to write a log file
- There are currently no test scripts in the repository. If needed, add lightweight diagnostic scripts (keyboard, rawpy thumbnail extraction) and/or CI checks.

## Project-specific conventions & patterns
- Defensive device handling: look for checks for strings like "-110", "-52", and "-1" in `camera_handler.py`. When adding camera error handling follow the existing approach: log, set `lost_device`, try graceful `release()`, and notify callbacks.
- Short critical sections: camera operations are protected by `camera_lock` — avoid holding the lock while doing expensive work (decode/encode, disk I/O). Follow _poll_events_unlocked/_process_pending_downloads pattern.
- JPEG normalization: prefer trimming to last EOI when dealing with corrupted/truncated frames (`_trim_to_eoi`) before decoding.
- Preview pairing: RAW + JPEG arrive as pairs; `ImagePreviewManager` tracks pending RAWs and waits briefly for JPEG pairs ([`_pending_raw`, `_pair_timeout`]). When modifying pairing logic, keep timeout behavior and safe deletes consistent.
- Logging: the app scrubs base64 data URIs from logs (`Base64ScrubFilter` + `SanitizingFormatter`), so don't rely on logs containing full frame payloads.
- Internationalization: add locale entries to `TRANSLATIONS` and ensure keys match those used in `gui.py` (e.g., `app_title`, `status_*`, `tooltip_*`). Use `set_locale()` normalization when testing.

## Tests & scripts
- There are no tests or diagnostic scripts included in the repository currently.
- If you need diagnostics, add small scripts for keyboard event debugging or raw thumbnail extraction (patterns were previously in `test_keyboard.py` and `test_rawpy.py`). For CI, consider adding a GitHub Actions workflow that runs import checks, linters, and any small scripts you add.
- If you add unit tests, prefer lightweight scripts or pytest fixtures that can simulate camera failures by mocking `CameraHandler` methods.

## Integration points / external dependencies
- Hardware: libgphoto2 (and Python bindings) — required for camera preview/tether. On macOS, `brew install gphoto2` is a common setup step.
- Optional libs: `rawpy` for RAW thumbnail extraction and `Pillow` for EXIF rotation — code gracefully falls back when missing.
- UI framework: Flet — asynchronous UI updates are scheduled via the captured asyncio loop in `LiveViewGUI`.

## Small-but-critical examples to reference
- Convert memoryview to bytes to avoid corruption (camera_handler):
  - "if isinstance(file_data, memoryview): file_data = bytes(file_data)"
- Trim incomplete JPEGs before decoding (camera_handler): `_trim_to_eoi`
- How to signal disconnect to GUI: call `set_disconnect_callback` on CameraHandler and trigger `self._disconnect_callback(False, msg)`
- Where cached previews live: `./downloads` and managed by `ImagePreviewManager` (cleanup on startup)

## Tips for making safe changes
- When changing capture or tethering flows, respect `camera_lock` and do file I/O outside the lock.
- When adding new UI controls, prefer the existing pattern: create the control in `_create_ui_elements()`, wire in `_setup_event_handlers()`, and update selection UI using helper methods like `_set_active_rotation()` or `_refresh_guide_controls_ui()`.
- Run the app with `A7CAM_LOG_LEVEL=DEBUG python main.py` to get verbose logs. Use the logging sanitizer if you need to inspect frames without leaking base64 to logs.

---
Please review and tell me if any area should be expanded (e.g., CI instructions, test coverage plan, or more code examples). I can iterate on wording or add a small section for contributor workflow or PR checklist.