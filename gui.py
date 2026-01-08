"""
GUI Module
Flet-based user interface for camera live view application.
"""
import flet as ft
import flet.canvas as cv
import cv2
import numpy as np
import base64
import os
import threading
import time
import asyncio
import logging
from translations import t, set_locale, get_system_locale, is_supported
from image_preview import ImagePreviewManager, CachedImage

logger = logging.getLogger(__name__)


class LiveViewGUI:
    """Main GUI controller for the live view application."""
    
    def __init__(self, camera_handler, download_dir: str = None):
        """
        Initialize GUI with camera handler.
        
        Args:
            camera_handler: Instance of CameraHandler
            download_dir: Optional override for the preview download directory. If None,
                the ImagePreviewManager will pick a sensible platform-specific default.
        """
        self.camera = camera_handler
        # Preview manager lets the GUI show and navigate captured images
        try:
            self._preview_manager = ImagePreviewManager(download_dir=download_dir)
        except Exception:
            # Fallback to default constructor if something goes wrong
            self._preview_manager = ImagePreviewManager()

        self.streaming_event = threading.Event()
        self.frame_thread = None
        
        # Execution loop (captured in build) for scheduling UI updates from background threads
        self._loop = None
        # Keep frame lock in case of future buffering needs
        self._frame_lock = threading.Lock()
        # Display throttling to avoid flicker and overload (seconds)
        self._display_min_interval = 1.0 / 15.0  # 15 FPS
        self._last_display_ts = 0.0

        # Preview mode state
        self._preview_mode = False  # True when showing captured image preview
        self._preview_timer = None  # Timer for auto-return to live view
        self._preview_timeout = 3.0  # Seconds before returning to live view
        self._preview_duration = 3.0  # Current duration setting: 3.0, 10.0, or float('inf')
        self._last_user_interaction = 0.0  # Timestamp of last keyboard navigation
        
        # Composition guide state
        self._guide_type = "none"  # none, thirds, golden, grid, diagonal, center, fibonacci, fibonacci_vflip, fibonacci_hflip, fibonacci_both, triangles, triangles_flip
        self._guide_color = "white"  # white, green, red, yellow, blue, black
        self._guide_types = ["none", "thirds", "golden", "grid", "diagonal", "center", "fibonacci", "fibonacci_vflip", "fibonacci_hflip", "fibonacci_both", "triangles", "triangles_flip"]
        # Order chosen to display in two rows: top (white, green, red), bottom (yellow, blue, black)
        self._guide_colors = ["white", "green", "red", "yellow", "blue", "black"]
        
        # Image preview manager (callback set in _start_stream after method is available)
        self._preview_manager = ImagePreviewManager()

        # UI elements (initialized in build())
        self.page = None
        self.img_control = None
        self.status_text = None
        
        # Preview overlay elements (initialized in _create_ui_elements)
        self._preview_overlay = None
        self._preview_image = None
        self._preview_filename_text = None
        self._preview_counter_text = None

        # Saved window bounds for toggling full-screen mode
        self._saved_window_bounds = None

        # Help overlay (toggle with 'H')
        self._help_overlay = None
        self._help_text = None


    def build(self, page: ft.Page):
        """
        Build and configure the GUI.
        
        Args:
            page: Flet page object
        """
        self.page = page
        # Prefer system locale (OS-level) when available, fall back to page locale
        try:
            sys_loc = get_system_locale()
            if is_supported(sys_loc):
                set_locale(sys_loc)
            else:
                lc = getattr(page, 'locale_configuration', None)
                if lc and getattr(lc, 'language_code', None):
                    lang = lc.language_code.split('-')[0].lower()
                    if is_supported(lang):
                        set_locale(lang)
        except Exception:
            pass
        
        # Clean up download folder on startup
        try:
            self._preview_manager.cleanup_download_folder()
        except Exception as e:
            logger.warning(f"Failed to cleanup download folder: {e}")
        
        # Capture the running asyncio loop for scheduling UI updates from threads
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

        # Register disconnect callback so GUI can react immediately
        try:
            self.camera.set_disconnect_callback(self._on_camera_lost)
        except Exception:
            pass

        self._configure_page()
        self._create_ui_elements()
        self._setup_layout()
        self._setup_event_handlers()
        
        page.update()

        # Start camera watcher (auto-start enabled by design)
        try:
            self.camera.start_watch(callback=self._on_camera_detected)
        except Exception:
                logger.exception("Failed to start camera watch")
        self.page.title = t('app_title')
        self.page.theme_mode = ft.ThemeMode.DARK
        self.page.padding = 0
        
        # Maximize window on launch
        try:
            self.page.window_maximized = True
        except Exception:
            pass

    def _configure_page(self):
        """Configure `page` properties (title, theme, padding)."""
        try:
            if not self.page:
                return
            self.page.title = t('app_title')
            self.page.theme_mode = ft.ThemeMode.DARK
            self.page.padding = 0
            try:
                self.page.window_maximized = True
            except Exception:
                pass
        except Exception:
            logger.exception("_configure_page error")

    def _create_ui_elements(self):
        """Create all UI controls."""
        # Video preview with placeholder
        placeholder_b64 = self._create_placeholder_image()
        self.img_control = ft.Image(
            src=placeholder_b64,
            fit=ft.BoxFit.CONTAIN,
            gapless_playback=True,
            width=float("inf"),
            expand=True,
        )
        
        # Video container holds the image control so it scales with the layout
        self.video_container = ft.Container(
            content=self.img_control,
            alignment=ft.Alignment.CENTER,
            bgcolor=ft.Colors.BLACK,
            expand=True,
            border_radius=8,
            border=ft.border.all(1, ft.Colors.GREY_800)
        )
        
        # Status indicator
        self.status_icon = ft.Icon(ft.Icons.VIDEOCAM_OFF, color="red", size=24)
        self.status_text = ft.Text(
            value=t('status_not_connected'),
            size=16,
            weight=ft.FontWeight.W_500,
            color="red"
        )

        # Rotation control: custom buttons similar to guide buttons
        self._rotation_buttons = {}
        ROT_BTN_WIDTH = 56
        ROT_BTN_HEIGHT = 32

        def _make_rotation_btn(val, label, tooltip):
            def _on_click(e):
                self._handle_rotation_click(val)
            btn = ft.Container(
                content=ft.Text(label, size=14, color=ft.Colors.WHITE, weight=ft.FontWeight.W_500),
                width=ROT_BTN_WIDTH,
                height=ROT_BTN_HEIGHT,
                alignment=ft.Alignment.CENTER,
                tooltip=tooltip,
                border=ft.border.all(1, ft.Colors.GREY_700),
                border_radius=6,
                on_click=_on_click,
            )
            self._rotation_buttons[val] = btn
            return btn

        # Autorotate toggle button
        def _make_autorotate_btn():
            def _on_click(e):
                self._toggle_autorotate()
            icon = ft.Icon(ft.Icons.SCREEN_ROTATION, color=ft.Colors.WHITE)
            btn = ft.Container(
                content=icon,
                width=ROT_BTN_HEIGHT,
                height=ROT_BTN_HEIGHT,
                alignment=ft.Alignment.CENTER,
                tooltip=t('tooltip_autorotate'),
                border=ft.border.all(1, ft.Colors.GREY_700),
                border_radius=6,
                on_click=_on_click,
            )
            # Store reference for runtime updates
            self._autorotate_btn = btn
            return btn

        rotation_btns = [
            _make_autorotate_btn(),
            _make_rotation_btn("0", t('rotate_0_label'), t('tooltip_rotation_0')),
            _make_rotation_btn("90", t('rotate_90_label'), t('tooltip_rotation_90')),
            _make_rotation_btn("180", t('rotate_180_label'), t('tooltip_rotation_180')),
            _make_rotation_btn("270", t('rotate_270_label'), t('tooltip_rotation_270')),
        ]
        self.rotate_control = ft.Row(rotation_btns, spacing=4)

        # Preview duration control: custom buttons similar to guide buttons
        self._duration_buttons = {}
        DUR_BTN_WIDTH = 60
        DUR_BTN_HEIGHT = 32

        def _make_duration_btn(val, label, tooltip):
            def _on_click(e):
                self._handle_duration_click(val)
            btn = ft.Container(
                content=ft.Text(label, size=14, color=ft.Colors.WHITE, weight=ft.FontWeight.W_500),
                width=DUR_BTN_WIDTH,
                height=DUR_BTN_HEIGHT,
                alignment=ft.Alignment.CENTER,
                tooltip=tooltip,
                border=ft.border.all(1, ft.Colors.GREY_700),
                border_radius=6,
                on_click=_on_click,
            )
            self._duration_buttons[val] = btn
            return btn

        duration_btns = [
            _make_duration_btn("3", t('duration_3s_label'), t('tooltip_duration_3s')),
            _make_duration_btn("10", t('duration_10s_label'), t('tooltip_duration_10s')),
            _make_duration_btn("inf", t('duration_inf_label'), t('tooltip_duration_inf')),
        ]
        self.duration_control = ft.Row(duration_btns, spacing=4)

        # Composition guide controls using SegmentedButton (horizontal but stacked vertically)
        # Horizontal SegmentedButtons render reliably on desktop builds. We stack them in a Column
        # so they appear as vertical groups visually.
        # Compact square icon-like buttons for guide types and colors
        self._guide_type_buttons = {}
        self._guide_color_buttons = {}
        BTN_SIZE = 36

        def _make_type_btn(val, label, tooltip):
            def _on_click(e):
                self._set_active_guide_type(val)
            btn = ft.Container(
                content=ft.Text(label, size=14, color=ft.Colors.WHITE),
                width=BTN_SIZE,
                height=BTN_SIZE,
                alignment=ft.Alignment.CENTER,
                tooltip=tooltip,
                border=ft.border.all(1, ft.Colors.GREY_700),
                border_radius=6,
                on_click=_on_click,
            )
            self._guide_type_buttons[val] = btn
            return btn

        # Fibonacci popup menu button with 4 variants
        def _make_fibonacci_popup():
            def _on_fib_select(e):
                self._set_active_guide_type(e.control.data)
            
            popup_btn = ft.PopupMenuButton(
                content=ft.Container(
                    content=ft.Text("φ", size=16, color=ft.Colors.WHITE, weight=ft.FontWeight.BOLD),
                    width=BTN_SIZE,
                    height=BTN_SIZE,
                    alignment=ft.Alignment.CENTER,
                    border=ft.border.all(1, ft.Colors.GREY_700),
                    border_radius=6,
                ),
                tooltip=t('tooltip_guide_fibonacci'),
                items=[
                    ft.PopupMenuItem(content=ft.Text(t('guide_fibonacci_variant_normal')), data="fibonacci", on_click=_on_fib_select),
                    ft.PopupMenuItem(content=ft.Text(t('guide_fibonacci_variant_vflip')), data="fibonacci_vflip", on_click=_on_fib_select),
                    ft.PopupMenuItem(content=ft.Text(t('guide_fibonacci_variant_hflip')), data="fibonacci_hflip", on_click=_on_fib_select),
                    ft.PopupMenuItem(content=ft.Text(t('guide_fibonacci_variant_both')), data="fibonacci_both", on_click=_on_fib_select),
                ],
                menu_padding=ft.padding.all(4),
            )
            # Store reference for highlighting
            self._guide_type_buttons["fibonacci"] = popup_btn.content
            self._guide_type_buttons["fibonacci_vflip"] = popup_btn.content
            self._guide_type_buttons["fibonacci_hflip"] = popup_btn.content
            self._guide_type_buttons["fibonacci_both"] = popup_btn.content
            return popup_btn

        # Arrange guide types in two rows for compactness
        top_row = [
            _make_type_btn("none", t('guide_none_label'), t('tooltip_guide_none')),
            _make_type_btn("thirds", t('guide_thirds_label'), t('tooltip_guide_thirds')),
            _make_type_btn("golden", t('guide_golden_label'), t('tooltip_guide_golden')),
            _make_type_btn("grid", t('guide_grid_label'), t('tooltip_guide_grid')),
            _make_type_btn("diagonal", t('guide_diagonal_label'), t('tooltip_guide_diagonal')),
        ]
        bottom_row = [
            _make_type_btn("center", t('guide_center_label'), t('tooltip_guide_center')),
            _make_fibonacci_popup(),
            _make_type_btn("triangles", t('guide_triangles_label'), t('tooltip_guide_triangles')),
            _make_type_btn("triangles_flip", t('guide_triangles_flip_label'), t('tooltip_guide_triangles_flip')),
        ]
        self.guide_type_control = ft.Column([
            ft.Row(top_row, spacing=6),
            ft.Row(bottom_row, spacing=6),
        ], spacing=6)

        def _make_color_btn(val, label, tooltip, color):
            def _on_click(e):
                self._set_active_guide_color(val)
            inner = ft.Container(width=16, height=16, bgcolor=color, border_radius=8, border=ft.border.all(1, ft.Colors.GREY_900))
            btn = ft.Container(
                content=inner,
                width=BTN_SIZE,
                height=BTN_SIZE,
                alignment=ft.Alignment.CENTER,
                tooltip=tooltip,
                border=ft.border.all(1, ft.Colors.GREY_700),
                border_radius=6,
                on_click=_on_click,
            )
            self._guide_color_buttons[val] = btn
            return btn

        # Arrange guide colors in two rows (like guide types)
        top_row_colors = [
            _make_color_btn("white", t('color_white_label'), t('tooltip_color_white'), ft.Colors.WHITE),
            _make_color_btn("green", t('color_green_label'), t('tooltip_color_green'), ft.Colors.GREEN),
            _make_color_btn("red", t('color_red_label'), t('tooltip_color_red'), ft.Colors.RED),
        ]
        bottom_row_colors = [
            _make_color_btn("yellow", t('color_yellow_label'), t('tooltip_color_yellow'), ft.Colors.YELLOW),
            _make_color_btn("blue", t('color_blue_label'), t('tooltip_color_blue'), ft.Colors.BLUE),
            _make_color_btn("black", t('color_black_label'), t('tooltip_color_black'), ft.Colors.BLACK),
        ]
        self.guide_color_control = ft.Column([
            ft.Row(top_row_colors, spacing=6),
            ft.Row(bottom_row_colors, spacing=6),
        ], spacing=6)

        # Composition guide canvas overlay (transparent, covers the image area)
        self._guide_canvas = cv.Canvas(
            shapes=[],
            expand=True,
            on_resize=self._on_guide_canvas_resize,
        )
        self._guide_canvas_width = 0
        self._guide_canvas_height = 0
        
        # Second guide canvas for preview overlay
        self._preview_guide_canvas = cv.Canvas(
            shapes=[],
            expand=True,
            on_resize=self._on_preview_guide_canvas_resize,
        )
        self._preview_guide_canvas_width = 0
        self._preview_guide_canvas_height = 0

        # Keyboard listener: capture key presses (1-4) to change rotation
        self.keyboard_listener = ft.KeyboardListener(
            content=self.video_container,
            autofocus=True,
            on_key_down=None,  # assigned in _setup_event_handlers
        )
        
        # Preview overlay elements
        self._preview_image = ft.Image(
            src=placeholder_b64,
            fit=ft.BoxFit.CONTAIN,
            gapless_playback=True,
            width=float("inf"),
            expand=True,
        )
        
        self._preview_filename_text = ft.Text(
            value="",
            size=14,
            weight=ft.FontWeight.W_500,
            color=ft.Colors.WHITE,
        )
        
        self._preview_counter_text = ft.Text(
            value="",
            size=12,
            color=ft.Colors.WHITE70,
        )
        
        # Preview overlay container (hidden by default)
        self._preview_overlay = ft.Container(
            content=ft.Stack([
                # Preview image fills the container
                ft.Container(
                    content=self._preview_image,
                    alignment=ft.Alignment.CENTER,
                    bgcolor=ft.Colors.BLACK,
                    expand=True,
                ),
                # Guide canvas overlay for preview
                ft.Container(
                    content=self._preview_guide_canvas,
                    expand=True,
                ),

            ], expand=True),
            expand=True,
            visible=False,  # Hidden by default
        )

        # Help overlay (toggle with 'H') - compact centered box using translations
        help_text = t('help_text')
        help_lines = []
        try:
            for line in str(help_text).split('\n'):
                help_lines.append(ft.Text(line, size=13, color=ft.Colors.WHITE70))
        except Exception:
            help_lines = [ft.Text('Keyboard Shortcuts', size=14, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE70)]

        help_inner = ft.Container(
            content=ft.Column(help_lines, spacing=6, horizontal_alignment=ft.CrossAxisAlignment.BASELINE),
            width=560,
            height=370,
            offset=ft.Offset(0, -0.15),
            padding=ft.padding.all(12),
            bgcolor="rgba(0,0,0,0.86)",
            border_radius=8,
            border=ft.border.all(1, ft.Colors.GREY_700),
        )
        self._help_text = help_inner
        self._help_overlay = ft.Container(
            content=ft.Column([help_inner], alignment=ft.MainAxisAlignment.CENTER, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
            expand=True,
            visible=False,
            bgcolor="rgba(0,0,0,0.34)",
            alignment=ft.Alignment.CENTER,
        )

        # Track currently active rotation as integer degrees
        self._active_rotation = 0
        # Auto-rotate setting (enabled by default)
        self._autorotate_enabled = True
        self._autorotate_btn = None

        # HUD elements for preview (outside preview overlay so they can appear above controls)
        self._preview_filename_container = ft.Container(
            content=ft.Row([
                ft.Icon(ft.Icons.IMAGE, color=ft.Colors.WHITE70, size=16),
                self._preview_filename_text,
            ], spacing=6),
            left=12,
            top=56,  # place below the status label
            padding=ft.padding.symmetric(horizontal=12, vertical=8),
            bgcolor="rgba(0,0,0,0.70)",
            border_radius=8,
            visible=False,
            expand=False,
        )

        self._preview_counter_container = ft.Container(
            content=ft.Column([
                self._preview_counter_text,
            ], spacing=2, horizontal_alignment=ft.CrossAxisAlignment.END),
            right=12,
            bottom=40,  # move slightly up for better spacing
            padding=ft.padding.symmetric(horizontal=12, vertical=8),
            bgcolor="rgba(0,0,0,0.70)",
            border_radius=8,
            visible=False,
            expand=False,
        )


        # Ensure UI has correct selection
        try:
            self._set_active_rotation(self._active_rotation)
        except Exception:
            pass

        # Initialize autorotate button reference and UI
        try:
            # Ensure autorotate button reference exists and update its UI
            if not hasattr(self, '_autorotate_btn') or self._autorotate_btn is None:
                try:
                    if hasattr(self, 'rotate_control') and self.rotate_control is not None and len(self.rotate_control.controls) > 0:
                        self._autorotate_btn = self.rotate_control.controls[0]
                except Exception:
                    pass
            self._update_autorotate_ui()
        except Exception:
            pass

        # Ensure preview duration button reflects current setting
        try:
            if self._preview_duration == float('inf'):
                self._set_active_duration('inf')
            else:
                # convert float seconds to string key ('3' or '10')
                dkey = str(int(self._preview_duration))
                self._set_active_duration(dkey)
        except Exception:
            pass

        # Ensure guide UI reflects current defaults
        try:
            self._refresh_guide_controls_ui()
        except Exception:
            pass
        

    def _setup_layout(self):
        """Setup page layout."""
        # Minimal full-window layout using a Stack: live preview fills the window
        # and connection status + rotation controls are overlaid at the top.
        # Preview overlay sits on top of everything when visible.
        self._main_stack = ft.Stack(
            [
                # Base: the live preview container expands to fill the window
                # Wrap with a KeyboardListener so the content can receive key events
                ft.Container(content=self.keyboard_listener, expand=True),

                # Composition guide canvas overlay (non-interactive, below controls)
                ft.Container(
                    content=self._guide_canvas,
                    expand=True,
                ),

                # Preview overlay (on top of everything when visible)
                self._preview_overlay,

                # Overlay: connection status at the top-left (always on top)
                ft.Container(
                    content=ft.Row([self.status_icon, self.status_text], spacing=8),
                    left=0,
                    top=0,
                    padding=ft.padding.symmetric(horizontal=12, vertical=8),
                    bgcolor="rgba(0,0,0,0.70)",
                    border_radius=8,
                    expand=False,
                ),

                # Overlay: horizontal rotation control at the top-right (direct child so receives clicks)
                ft.Container(
                    content=self.rotate_control,
                    right=0,
                    top=0,
                    padding=ft.padding.symmetric(horizontal=12, vertical=8),
                    bgcolor="rgba(0,0,0,0.70)",
                    border_radius=8,
                    expand=False,
                ),

                # Overlay: preview duration control at the bottom-right (direct child so receives clicks)
                ft.Container(
                    content=self.duration_control,
                    right=0,
                    bottom=0,
                    padding=ft.padding.symmetric(horizontal=12, vertical=8),
                    bgcolor="rgba(0,0,0,0.70)",
                    border_radius=8,
                    expand=False,
                ),

                # Overlay: guide controls pinned to the bottom-left (on top of canvas)
                ft.Container(
                    content=ft.Row(
                        [
                            ft.Column(
                                [
                                    self.guide_type_control,
                                ],
                                spacing=6,
                                tight=True,
                            ),
                            ft.Column(
                                [
                                    self.guide_color_control,
                                ],
                                spacing=6,
                                tight=True,
                            ),
                        ],
                        spacing=12,
                        tight=True,
                    ),
                    padding=ft.padding.symmetric(horizontal=10, vertical=10),
                    bgcolor="rgba(0,0,0,0.70)",
                    border_radius=8,
                    border=ft.border.all(1, ft.Colors.GREY_800),
                    left=12,
                    bottom=12,
                    expand=False,
                ),

                # Preview HUD elements (appear on top of controls when previewing)
                self._preview_filename_container,
                self._preview_counter_container,
                # Help overlay sits on top of everything when visible
                self._help_overlay,
            ],
            expand=True,
        )

        # Add the stack as the primary page content so the preview fills the window
        self.page.add(self._main_stack)


    def _handle_rotation_click(self, val):
        """Handle rotation button click (0, 90, 180, 270)."""
        orientation_map = {
            "0": self.camera.ORIENTATION_NORMAL,
            "90": self.camera.ORIENTATION_90,
            "180": self.camera.ORIENTATION_180,
            "270": self.camera.ORIENTATION_270,
        }
        code = orientation_map.get(val, self.camera.ORIENTATION_NORMAL)
        self.camera.set_orientation(code)
        self._set_active_rotation(int(val))

    def _handle_duration_click(self, val):
        """Handle preview duration button click (3, 10, inf)."""
        duration_map = {'3': 3.0, '10': 10.0, 'inf': float('inf')}
        self._preview_duration = duration_map.get(val, 3.0)
        self._set_active_duration(val)
        if self._preview_mode:
            self._start_preview_timer()

    def _setup_event_handlers(self):
        """Wire up all event handlers."""
        # Rotation and duration button handlers are now attached directly in _create_ui_elements
        
        # Guide controls are click-driven; selection is reflected by _refresh_guide_controls_ui()

        # Keyboard shortcut handler for rotation (keys 1-4) and preview navigation
        orientation_map = {
            0: (self.camera.ORIENTATION_NORMAL, None),
            90: (self.camera.ORIENTATION_90, None),
            180: (self.camera.ORIENTATION_180, None),
            270: (self.camera.ORIENTATION_270, None),
        }
        
        # Track last key event to prevent duplicate handling
        import time
        last_key_event = {'time': 0, 'key': None, 'shift': None}
        
        # Track Shift key state independently (workaround for macOS/Flet uppercase key issue)
        shift_key_pressed = {'left': False, 'right': False}
        
        def _on_key_down(e):
            try:
                raw_key = str(getattr(e, 'key', ''))

                # Helper: coerce various truthy values (bool or string) into boolean True/False
                def _is_true(val):
                    try:
                        if isinstance(val, bool):
                            return val
                        s = str(val).strip().lower()
                        return s in ('true', '1', 'yes', 'on')
                    except Exception:
                        return False

                # Detect Shift via attributes, modifiers, or uppercase letter (robust to string-typed flags)
                shift_pressed = False
                try:
                    if _is_true(getattr(e, 'shift', False)) or _is_true(getattr(e, 'shiftKey', False)):
                        shift_pressed = True
                except Exception:
                    pass
                try:
                    mods = getattr(e, 'modifiers', None)
                    if isinstance(mods, (list, tuple)):
                        if any(str(m).lower() == 'shift' for m in mods):
                            shift_pressed = True
                    elif isinstance(mods, str):
                        if 'shift' in mods.lower():
                            shift_pressed = True
                except Exception:
                    pass

                # Normalize key name by stripping surrounding quotes and whitespace
                try:
                    key_name = raw_key.strip().strip("'\"")
                except Exception:
                    key_name = raw_key

                # Use normalized lower-case key for matching
                key = key_name.lower() if key_name else ''
                # Normalize key by removing spaces and hyphens (e.g., 'Arrow Left' -> 'arrowleft')
                nkey = key.replace(' ', '').replace('-', '')

                # Track Shift key state (workaround for macOS/Flet where all letters arrive uppercase)
                if nkey in ('shift', 'shiftleft', 'shiftright'):
                    if 'left' in nkey:
                        shift_key_pressed['left'] = True
                    elif 'right' in nkey:
                        shift_key_pressed['right'] = True
                    else:
                        shift_key_pressed['left'] = True  # Generic shift, assume left
                    logger.debug(f"Shift key pressed: {nkey}")
                    return  # Don't process shift key itself
                
                # Check if Shift is currently held down (tracked state)
                if not shift_pressed and (shift_key_pressed['left'] or shift_key_pressed['right']):
                    shift_pressed = True
                    logger.debug(f"Shift detected from tracked key state")

                # Prevent duplicate event handling (both keyboard_listener and page.on_key_down fire)
                # Check if this is the same key event within 50ms
                current_time = time.time()
                if (current_time - last_key_event['time'] < 0.05 and 
                    last_key_event['key'] == key and 
                    last_key_event['shift'] == shift_pressed):
                    logger.debug(f"Ignoring duplicate key event: {key}, shift={shift_pressed}")
                    return
                last_key_event['time'] = current_time
                last_key_event['key'] = key
                last_key_event['shift'] = shift_pressed

                # Focused debug for 'G' key events: capture only a small set of attributes and truncate long strings
                try:
                    if key == 'g':
                        def _short(x):
                            try:
                                s = repr(x)
                                return s if len(s) <= 200 else s[:200] + '...'
                            except Exception:
                                return str(type(x))
                        attrs = {
                            'raw_key': _short(raw_key),
                            'key': _short(key),
                            'character': _short(getattr(e, 'character', None)),
                            'shift': _short(getattr(e, 'shift', False)),
                            'shiftKey': _short(getattr(e, 'shiftKey', False)),
                            'modifiers': _short(getattr(e, 'modifiers', None)),
                            'shift_pressed': shift_pressed,  # Add computed value
                        }
                        logger.debug('Key G event attributes: %s', attrs)
                except Exception:
                    logger.exception('Failed to log key event attributes')
                
                # Rotation shortcuts (1-4)
                key_map = {
                    '1': 0, 'digit1': 0, 'numpad1': 0,
                    '2': 90, 'digit2': 90, 'numpad2': 90,
                    '3': 180, 'digit3': 180, 'numpad3': 180,
                    '4': 270, 'digit4': 270, 'numpad4': 270,
                }
                if key in key_map:
                    deg = key_map[key]
                    code = orientation_map.get(deg, (self.camera.ORIENTATION_NORMAL, None))[0]
                    self.camera.set_orientation(code)
                    self._set_active_rotation(deg)
                    return                
                # Duration keys: 8, 9, 0
                duration_map = {
                    '8': ('3', 3.0), 'digit8': ('3', 3.0), 'numpad8': ('3', 3.0),
                    '9': ('10', 10.0), 'digit9': ('10', 10.0), 'numpad9': ('10', 10.0),
                    '0': ('inf', float('inf')), 'digit0': ('inf', float('inf')), 'numpad0': ('inf', float('inf')),
                }
                if key in duration_map:
                    val, duration = duration_map[key]
                    self._preview_duration = duration
                    self._set_active_duration(val)
                    # Restart timer if in preview mode
                    if self._preview_mode:
                        self._start_preview_timer()
                    return
                
                # Guide type key: G cycles through guide types (Shift+G -> backward)
                if key == 'g':
                    try:
                        logger.debug(f"GUI: cycling guides - shift_pressed={shift_pressed}, reverse={shift_pressed}")
                        if shift_pressed:
                            self._cycle_guide_type(reverse=True)
                        else:
                            self._cycle_guide_type()
                    except Exception:
                        logger.exception("Failed to cycle guide type")
                    return
                
                # Guide color key: C cycles through guide colors (Shift+C -> reverse)
                if key == 'c':
                    try:
                        logger.debug(f"GUI: cycling guide colors - shift_pressed={shift_pressed}")
                        if shift_pressed:
                            self._cycle_guide_color(reverse=True)
                        else:
                            self._cycle_guide_color()
                    except Exception:
                        logger.exception("Failed to cycle guide color")
                    return

                # Help overlay key: H toggles help in center of screen
                if key == 'h':
                    try:
                        self._toggle_help_overlay()
                    except Exception:
                        logger.exception("Failed toggling help overlay")
                    return

                # Autorotate toggle: 'A' toggles automatic rotation
                if key == 'a':
                    try:
                        self._toggle_autorotate()
                    except Exception:
                        logger.exception("Failed toggling autorotate")
                    return

                # Full-screen toggle: 'f' toggles full-screen and restores previous bounds
                if key == 'f':
                    try:
                        # If a Window object exists on the page, prefer toggling it directly
                        win = getattr(self.page, 'window', None)
                        if win is not None:
                            # Log current window state
                            try:
                                attrs = {
                                    'full_screen': getattr(win, 'full_screen', None),
                                    'width': getattr(win, 'width', None),
                                    'height': getattr(win, 'height', None),
                                    'left': getattr(win, 'left', None),
                                    'top': getattr(win, 'top', None),
                                    'maximized': getattr(win, 'maximized', None),
                                }
                            except Exception:
                                attrs = {'full_screen': None}
                            logger.info("F key pressed; window attrs: %s", attrs)

                            current_fs = getattr(win, 'full_screen', False)
                            if not current_fs:
                                # Save current bounds (may be None on some platforms)
                                self._saved_window_bounds = {
                                    'width': getattr(win, 'width', None),
                                    'height': getattr(win, 'height', None),
                                    'left': getattr(win, 'left', None),
                                    'top': getattr(win, 'top', None),
                                    'maximized': getattr(win, 'maximized', None),
                                }
                                try:
                                    win.full_screen = True
                                    logger.debug("Set window.full_screen = True")
                                    win.update()
                                except Exception:
                                    logger.exception("Failed setting window.full_screen")
                                logger.debug("Entering full-screen (saved bounds: %s)", self._saved_window_bounds)
                            else:
                                try:
                                    win.full_screen = False
                                    logger.debug("Set window.full_screen = False")
                                    win.update()
                                except Exception:
                                    logger.exception("Failed unsetting window.full_screen")
                                b = getattr(self, '_saved_window_bounds', None)
                                if b:
                                    try:
                                        if b.get('width') is not None:
                                            win.width = b['width']
                                        if b.get('height') is not None:
                                            win.height = b['height']
                                        if b.get('left') is not None:
                                            win.left = b['left']
                                        if b.get('top') is not None:
                                            win.top = b['top']
                                        if b.get('maximized') is not None:
                                            win.maximized = b['maximized']
                                        win.update()
                                    except Exception:
                                        logger.exception("Failed restoring window bounds")
                                    finally:
                                        self._saved_window_bounds = None
                                logger.debug("Exiting full-screen, restored bounds")
                            return

                        # Fallback: older attribute names on page (some runtimes expose them)

                        attrs = {
                            'window_full_screen': getattr(self.page, 'window_full_screen', None),
                            'window_width': getattr(self.page, 'window_width', None),
                            'window_height': getattr(self.page, 'window_height', None),
                            'window_x': getattr(self.page, 'window_x', None),
                            'window_y': getattr(self.page, 'window_y', None),
                        }
                        logger.debug("F key pressed; page attrs: %s", attrs)
                        current_fs = getattr(self.page, 'window_full_screen', False)
                        if not current_fs:
                            # Save current bounds (may be None on some platforms)
                            self._saved_window_bounds = attrs
                            try:
                                self.page.window_full_screen = True
                                logger.debug("Set page.window_full_screen = True")
                            except Exception:
                                logger.exception("Failed setting page.window_full_screen")
                            logger.debug("Entering full-screen (saved bounds: %s)", self._saved_window_bounds)
                        else:
                            try:
                                self.page.window_full_screen = False
                                logger.debug("Set page.window_full_screen = False")
                            except Exception:
                                logger.exception("Failed unsetting page.window_full_screen")
                            b = getattr(self, '_saved_window_bounds', None)
                            if b:
                                try:
                                    if b.get('width') is not None:
                                        self.page.window_width = b['width']
                                    if b.get('height') is not None:
                                        self.page.window_height = b['height']
                                    if b.get('x') is not None:
                                        self.page.window_x = b['x']
                                    if b.get('y') is not None:
                                        self.page.window_y = b['y']
                                except Exception:
                                    logger.exception("Failed restoring window bounds")
                                finally:
                                    self._saved_window_bounds = None
                            logger.debug("Exiting full-screen, restored bounds")
                        try:
                            self.page.update()
                        except Exception:
                            pass


                    except Exception:
                        logger.exception("Failed toggling full-screen")
                    return

                # Space key: toggle preview mode / return to live view
                if nkey in ('', 'space') or key in (' ', 'space'):
                    logger.debug("GUI: key press SPACE (preview_mode=%s)", self._preview_mode)
                    if self._preview_mode:
                        # Return to live view
                        self._hide_preview()
                    else:
                        # Show latest preview if available
                        if self._preview_manager.has_cached_images():
                            preview = self._preview_manager.get_latest_preview()
                            if preview:
                                self._show_preview(preview)
                    return
                
                # Arrow keys: navigate cached images (only when preview mode is active)
                if nkey in ('arrowleft', 'left'):
                    logger.debug("GUI: key press LEFT (preview_mode=%s)", self._preview_mode)
                    # Ignore arrow navigation when in live view
                    if not self._preview_mode:
                        logger.debug("GUI: LEFT ignored (not in preview mode)")
                        return
                    if self._preview_manager.has_cached_images():
                        preview = self._preview_manager.navigate_previous()
                        logger.debug("GUI: navigate_previous returned: %s", getattr(preview, 'filename', None))
                        if preview:
                            self._show_preview(preview, reset_timer=True)
                    return
                
                if nkey in ('arrowright', 'right'):
                    logger.debug("GUI: key press RIGHT (preview_mode=%s)", self._preview_mode)
                    # Ignore arrow navigation when in live view
                    if not self._preview_mode:
                        logger.debug("GUI: RIGHT ignored (not in preview mode)")
                        return
                    if self._preview_manager.has_cached_images():
                        preview = self._preview_manager.navigate_next()
                        logger.debug("GUI: navigate_next returned: %s", getattr(preview, 'filename', None))
                        if preview:
                            self._show_preview(preview, reset_timer=True)
                    return
                    
            except Exception as ex:
                logger.exception("Keyboard handler error")

        def _on_key_up(e):
            """Handle key release to track Shift key state"""
            try:
                raw_key = str(getattr(e, 'key', ''))
                try:
                    key_name = raw_key.strip().strip("'\"")
                except Exception:
                    key_name = raw_key
                
                key = key_name.lower() if key_name else ''
                nkey = key.replace(' ', '').replace('-', '')
                
                # Reset Shift key state when released
                if nkey in ('shift', 'shiftleft', 'shiftright'):
                    if 'left' in nkey:
                        shift_key_pressed['left'] = False
                    elif 'right' in nkey:
                        shift_key_pressed['right'] = False
                    else:
                        # Generic shift release - clear both
                        shift_key_pressed['left'] = False
                        shift_key_pressed['right'] = False
                    logger.debug(f"Shift key released: {nkey}")
            except Exception as ex:
                logger.exception("Key up handler error")

        # attach keyboard handler to the listener (if present)
        try:
            if hasattr(self, 'keyboard_listener') and self.keyboard_listener is not None:
                self.keyboard_listener.on_key_down = _on_key_down
                self.keyboard_listener.on_key_up = _on_key_up
            # Also assign to page to ensure key events are received even when focus differs
            try:
                self.page.on_key_down = _on_key_down
                self.page.on_key_up = _on_key_up
            except Exception:
                pass
        except Exception:
            pass
        
        # Window close handler
        self.page.on_window_event = self._on_window_event


    def _deg_from_orientation(self, orientation_code):
        """Map camera orientation code to degrees used by the rotation buttons."""
        mapping = {
            self.camera.ORIENTATION_NORMAL: 0,
            self.camera.ORIENTATION_90: 90,
            self.camera.ORIENTATION_180: 180,
            self.camera.ORIENTATION_270: 270,
        }
        return mapping.get(orientation_code, None)

    def _sync_rotation_from_camera(self):
        """Set the active rotation button to match the camera's current orientation."""
        try:
            if not hasattr(self.camera, 'orientation'):
                return
            deg = self._deg_from_orientation(self.camera.orientation)
            if deg is not None:
                self._set_active_rotation(deg)
        except Exception as e:
                logger.exception("_sync_rotation_from_camera error")

    def _start_stream(self):
        """Start camera streaming (auto-start path)."""
        success, msg = self.camera.connect()
        if success:
            self.status_text.value = t('status_streaming')
            self.status_text.color = ft.Colors.GREEN_400
            self.status_text.opacity = 0.7
            # Transmission: yellow text and icon
            self.status_icon.name = ft.Icons.VIDEOCAM
            self.status_icon.color = ft.Colors.AMBER_400
            self.status_text.color = ft.Colors.AMBER_400
            self.streaming_event.set()
            self.frame_thread = threading.Thread(target=self._frame_update_loop, daemon=True)
            self.frame_thread.start()
            # Enable tethering with preview callback
            self.camera.start_tether(callback=self._preview_manager.process_downloaded_file)
            self._preview_manager.set_preview_callback(self._on_new_preview)
            self._preview_manager.set_orientation_callback(self._on_orientation_detected)
            logger.info("GUI: Preview and orientation callbacks registered, tether started")
            # Sync active rotation button with camera state
            try:
                self._sync_rotation_from_camera()
            except Exception:
                pass
        else:
            self.status_text.value = t('status_error', msg=msg)
            # Error: crossed red video camera
            self.status_icon.name = ft.Icons.VIDEOCAM_OFF
            self.status_icon.color = "red"
            self.page.snack_bar = ft.SnackBar(ft.Text(t('snackbar_failed_connect', msg=msg)))
            self.page.snack_bar.open = True

    def _stop_stream(self):
        """Stop camera streaming (manual stop)."""
        self.streaming_event.clear()
        # Stop tethering
        self.camera.stop_tether()
        # Wait for background thread to exit
        time.sleep(0.2)
        if self.frame_thread is not None:
            try:
                self.frame_thread.join(timeout=1)
            except Exception:
                pass
            self.frame_thread = None
        # Release camera
        self.camera.release()
        # Update UI
        self.status_text.value = t('status_not_connected')
        self.status_text.color = "red"
        self.status_icon.name = ft.Icons.VIDEOCAM_OFF
        self.status_icon.color = "red"

    def _frame_update_loop(self):
        """Background loop to fetch frames and schedule UI updates."""
        while self.streaming_event.is_set():
            try:
                frame_b64 = self.camera.get_frame_base64()
                
                if frame_b64:
                    # Throttle display updates to avoid flicker
                    now = time.time()
                    if now - self._last_display_ts < self._display_min_interval:
                        continue
                    self._last_display_ts = now

                    # Schedule coroutine to update UI on Flet's asyncio loop
                    if self._loop:
                        try:
                            asyncio.run_coroutine_threadsafe(self._update_image(frame_b64), self._loop)
                        except Exception as e:
                            logger.exception("Failed to schedule UI update")
                    else:
                        # As a fallback, set image control src directly
                        try:
                            self.img_control.src = frame_b64
                            self.page.update()
                        except Exception:
                            pass
                else:
                    # Handle errors
                    if self._handle_frame_error():
                        break
                    
            except Exception as e:
                logger.exception("Exception in frame update loop")
                if self._handle_frame_error():
                    break
                time.sleep(0.5)

    async def _update_image(self, frame_b64):
        """Run on main event loop: apply new image and update page."""
        try:
            # Fast, gapless update of the Image control to avoid flicker
            if self.img_control is not None:
                self.img_control.src = frame_b64
            else:
                # Fallback - set container decoration
                self.video_container.image = ft.DecorationImage(src=frame_b64, fit=ft.BoxFit.CONTAIN)
            # Remember current frame so we can reapply on resize
            self._current_frame = frame_b64

            # Capture source image dimensions (only once to avoid overhead)
            try:
                if not hasattr(self, '_current_frame_size') or self._current_frame_size is None:
                    # frame_b64 is typically a data URI (live frame)
                    if isinstance(frame_b64, str) and frame_b64.startswith('data:'):
                        b64 = frame_b64.split(',', 1)[1]
                        data = base64.b64decode(b64)
                        arr = np.frombuffer(data, dtype=np.uint8)
                        img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
                        if img is not None:
                            h, w = img.shape[:2]
                            self._current_frame_size = (w, h)
                    else:
                        # Could be a filepath for preview; try reading metadata
                        try:
                            img = cv2.imread(frame_b64)
                            if img is not None:
                                h, w = img.shape[:2]
                                self._current_frame_size = (w, h)
                        except Exception:
                            pass
            except Exception:
                pass

            # Page update must be invoked on main loop
            self.page.update()
        except Exception as e:
            logger.exception("Exception in _update_image")

    async def _reapply_current_frame(self):
        """Reapply the current frame to force a re-render after resize."""
        try:
            if hasattr(self, "_current_frame") and self._current_frame:
                if self.img_control is not None:
                    self.img_control.src = self._current_frame
                else:
                    self.video_container.image = ft.DecorationImage(src=self._current_frame, fit=ft.BoxFit.CONTAIN)
                self.page.update()
        except Exception as e:
            logger.exception("Exception in _reapply_current_frame")
    def _start_update_timer(self):
        """Start timer to poll for new frames and update UI from main thread."""
        def update_frame():
            """Timer callback to update UI with latest frame."""
            if not self.streaming_event.is_set():
                return
            
            with self._frame_lock:
                if self._latest_frame:
                        # Set container decoration image so it scales to container bounds
                        self.video_container.image = ft.DecorationImage(
                            src=self._latest_frame, fit=ft.BoxFit.CONTAIN
                        )
                        self._latest_frame = None  # Clear after consuming
                        self.page.update()
            # Schedule next update (60ms = ~16fps)
            if self.streaming_event.is_set():
                import threading
                self._update_timer = threading.Timer(0.06, update_frame)
                self._update_timer.daemon = True
                self._update_timer.start()
        
        # Start the update cycle
        update_frame()
    
    def _stop_update_timer(self):
        """Stop the UI update timer."""
        if self._update_timer:
            self._update_timer.cancel()
            self._update_timer = None

    def _handle_frame_error(self):
        """
        Handle frame capture errors and update UI accordingly.
        
        Returns:
            bool: True if streaming should stop, False otherwise
        """
        # Device disconnected
        if self.camera.lost_device:
            self.streaming_event.clear()
            self.camera.release()
            self.status_text.value = t('status_disconnected')
            self.status_text.color = "red"
            self.status_icon.name = ft.Icons.VIDEOCAM_OFF
            self.status_icon.color = "red"
            self.page.update()
            return True
        
        # I/O busy errors
        if self.camera._io_error_counter > 0:
            self.status_text.value = t('status_io_busy')
            self.status_text.color = ft.Colors.ORANGE_400
            self.status_icon.name = ft.Icons.WARNING_AMBER_ROUNDED
            self.status_icon.color = ft.Colors.ORANGE_400
            self.page.update()
            
            # Too many errors - stop streaming
            if self.camera._io_error_counter >= self.camera._io_error_threshold:
                self.streaming_event.clear()
                self.camera.lost_device = True
                self.camera.release()
                self.status_text.value = t('status_disconnected_io')
                self.status_text.color = "red"
                # Error: crossed red video camera
                self.status_icon.name = ft.Icons.VIDEOCAM_OFF
                self.status_icon.color = "red"
                # UI button removed; showing disconnected status instead
                self.page.update()
                return True
            
            time.sleep(0.2)
            return False
        
        # Other transient errors
        time.sleep(0.1)
        return False

    def _on_window_event(self, e):
        """Handle window events (e.g., close, resize)."""
        # Close event
        if e.data == "close":
            self.streaming_event.clear()
            try:
                self.camera.stop_watch()
            except Exception:
                pass
            self.camera.release()
            self.page.window_destroy()
            return

        # On resize or other window layout events, reapply the current frame to
        # force the Image control to re-render at the new size. This avoids the
        # case where the image retains its old intrinsic size until the next
        # incoming frame arrives.
        try:
            data_str = str(e.data)
            if "resize" in data_str.lower() or "size" in data_str.lower():
                if hasattr(self, "_current_frame") and self._current_frame:
                    if self._loop:
                        asyncio.run_coroutine_threadsafe(self._reapply_current_frame(), self._loop)
                    else:
                        try:
                            self.img_control.src = self._current_frame
                            self.page.update()
                        except Exception:
                            pass
        except Exception:
            pass

    def _toggle_help_overlay(self):
        """Toggle visibility of the centered help overlay."""
        try:
            if not hasattr(self, '_help_overlay') or self._help_overlay is None:
                return
            vis = not getattr(self._help_overlay, 'visible', False)
            self._help_overlay.visible = vis
            if vis:
                # When showing help, hide preview overlay and HUD so help is clear
                try:
                    if hasattr(self, '_preview_overlay') and self._preview_overlay is not None:
                        self._preview_overlay.visible = False
                    if hasattr(self, '_preview_filename_container') and self._preview_filename_container is not None:
                        self._preview_filename_container.visible = False
                    if hasattr(self, '_preview_counter_container') and self._preview_counter_container is not None:
                        self._preview_counter_container.visible = False

                except Exception:
                    pass

            # Trigger UI refresh
            try:
                self.page.update()
            except Exception:
                pass
        except Exception:
            logger.exception("Failed toggling help overlay")


    def _on_camera_detected(self, success, msg):
        """Callback invoked by camera watcher when camera connects."""
        try:
            # Auto-start when camera is detected
            if success and not self.streaming_event.is_set():
                logger.info("Auto-starting live view: %s", msg)
                # Start streaming on the main loop
                if self._loop:
                    try:
                        asyncio.run_coroutine_threadsafe(self._start_stream_async(), self._loop)
                    except Exception:
                        # Fallback to direct call
                        self._start_stream()
                else:
                    self._start_stream()
        except Exception as e:
                logger.exception("Exception in camera detected callback")
    async def _start_stream_async(self):
        """Async wrapper to call _start_stream on the main loop."""
        self._start_stream()

    def _on_camera_lost(self, success, msg):
        """Called by CameraHandler when the device is lost."""
        try:
            # Schedule the main-thread UI update
            if self._loop:
                asyncio.run_coroutine_threadsafe(self._handle_camera_lost(msg), self._loop)
            else:
                self._handle_camera_lost_sync(msg)
        except Exception as e:
            logger.exception("Exception in _on_camera_lost")
    def _format_disconnect_message(self, msg: str) -> str:
        """Return a user-friendly disconnect message for known errors."""
        if not msg:
            return t('status_disconnected')
        s = str(msg)
        # Known hardware disconnect indicators
        disconnect_tokens = ["-52", "Could not find the requested device", "-1", "Unspecified error", "-105", "Unknown model"]
        for token in disconnect_tokens:
            if token in s:
                return t('status_disconnected_device')
        # Otherwise show a short, single-line message
        short = s.splitlines()[0]
        if len(short) > 80:
            short = short[:77] + "..."
        return t('status_disconnected_short', short=short)

    async def _handle_camera_lost(self, msg):
        """Main-thread handler to stop streaming and update UI on disconnect."""
        try:
            self.streaming_event.clear()
            try:
                self.camera.release()
            except Exception:
                pass
            self.status_text.value = self._format_disconnect_message(msg)
            self.status_text.color = "red"
            self.status_icon.name = ft.Icons.VIDEOCAM_OFF
            self.status_icon.color = "red"
            self.page.update()
        except Exception as e:
            logger.exception("Exception in _handle_camera_lost")
    def _handle_camera_lost_sync(self, msg):
        try:
            self.streaming_event.clear()
            try:
                self.camera.release()
            except Exception:
                pass
            self.status_text.value = self._format_disconnect_message(msg)
            self.status_text.color = "red"
            self.status_icon.name = ft.Icons.VIDEOCAM_OFF
            self.status_icon.color = "red"
            self.page.update()
        except Exception as e:
            logger.exception("Exception in _handle_camera_lost_sync")
    @staticmethod
    def _create_placeholder_image():
        """Create a 1x1 black placeholder image as base64 with data URI."""
        placeholder_img = np.zeros((1, 1, 3), dtype=np.uint8)
        _, placeholder_buffer = cv2.imencode('.jpg', placeholder_img)
        b64_str = base64.b64encode(placeholder_buffer).decode('utf-8')
        return f"data:image/jpeg;base64,{b64_str}"

    def _set_active_rotation(self, degrees: int):
        """Update rotation selection highlighting.

        degrees should be one of the values: 0, 90, 180, 270.
        """
        try:
            self._active_rotation = degrees
            val_str = str(degrees)
            for val, btn in self._rotation_buttons.items():
                if val == val_str:
                    btn.bgcolor = "rgba(100,100,255,0.4)"
                    btn.border = ft.border.all(2, ft.Colors.BLUE_400)
                else:
                    btn.bgcolor = None
                    btn.border = ft.border.all(1, ft.Colors.GREY_700)
            
            # Update autorotate UI in case manual rotation implies user preference
            try:
                # If user manually rotates, disable autorotate so it doesn't override
                if getattr(self, '_autorotate_enabled', True):
                    self._autorotate_enabled = False
                    self._update_autorotate_ui()
            except Exception:
                pass

            # Update guide overlays when rotation changes
            self._update_guide_canvas()
            self._update_preview_guide_canvas()
            
            self.page.update()
        except Exception as e:
            logger.exception("_set_active_rotation error")

    def _set_active_duration(self, value: str):
        """Update duration selection highlighting.

        value should be one of: "3", "10", "inf"
        """
        try:
            for val, btn in self._duration_buttons.items():
                if val == value:
                    btn.bgcolor = "rgba(100,100,255,0.4)"
                    btn.border = ft.border.all(2, ft.Colors.BLUE_400)
                else:
                    btn.bgcolor = None
                    btn.border = ft.border.all(1, ft.Colors.GREY_700)
            self.page.update()
        except Exception as e:
            logger.exception("_set_active_duration error")

    def _set_active_guide_type(self, value: str):
        """Update guide type selection."""
        try:
            self._guide_type = value
            self._refresh_guide_controls_ui()
            self._update_guide_canvas()
            self._update_preview_guide_canvas()
        except Exception as e:
            logger.exception("_set_active_guide_type error")
    def _set_active_guide_color(self, value: str):
        """Update guide color selection."""
        try:
            self._guide_color = value
            self._refresh_guide_controls_ui()
            self._update_guide_canvas()
            self._update_preview_guide_canvas()
        except Exception as e:
            logger.exception("_set_active_guide_color error")
    def _refresh_guide_controls_ui(self):
        """Refresh visual state of the guide buttons (selection sync)."""
        try:
            # guide type buttons
            if hasattr(self, '_guide_type_buttons') and self._guide_type_buttons:
                for val, btn in self._guide_type_buttons.items():
                    try:
                        # Treat all fibonacci variants as a single group for highlighting
                        if val.startswith("fibonacci"):
                            active = str(self._guide_type).startswith("fibonacci")
                        else:
                            active = (val == self._guide_type)

                        if active:
                            btn.bgcolor = ft.Colors.AMBER_400
                            btn.border = ft.border.all(1, ft.Colors.AMBER_400)
                        else:
                            btn.bgcolor = None
                            btn.border = ft.border.all(1, ft.Colors.GREY_700)

                        btn.update()
                    except Exception:
                        pass

            # guide color buttons
            if hasattr(self, '_guide_color_buttons') and self._guide_color_buttons:
                for val, btn in self._guide_color_buttons.items():
                    if val == self._guide_color:
                        btn.border = ft.border.all(2, ft.Colors.WHITE)
                    else:
                        btn.border = ft.border.all(1, ft.Colors.GREY_700)
                    try:
                        btn.update()
                    except Exception:
                        pass
        except Exception as e:
            logger.exception("_refresh_guide_controls_ui error")
    def _cycle_guide_type(self, reverse: bool = False):
        """Cycle through guide types.

        Args:
            reverse: If True, cycle backward instead of forward.
        """
        try:
            idx = self._guide_types.index(self._guide_type)
            if reverse:
                next_idx = (idx - 1) % len(self._guide_types)
            else:
                next_idx = (idx + 1) % len(self._guide_types)
            self._set_active_guide_type(self._guide_types[next_idx])
        except Exception as e:
            logger.exception("_cycle_guide_type error")
    def _cycle_guide_color(self, reverse: bool = False):
        """Cycle through guide colors.

        Args:
            reverse: If True, cycle backward instead of forward.
        """
        try:
            idx = self._guide_colors.index(self._guide_color)
            if reverse:
                next_idx = (idx - 1) % len(self._guide_colors)
            else:
                next_idx = (idx + 1) % len(self._guide_colors)
            self._set_active_guide_color(self._guide_colors[next_idx])
        except Exception as e:
            logger.exception("_cycle_guide_color error")
    def _get_guide_paint(self):
        """Get paint object for current guide color."""
        color_map = {
            "white": ft.Colors.WHITE,
            "green": ft.Colors.GREEN,
            "red": ft.Colors.RED,
            "yellow": getattr(ft.Colors, 'YELLOW', ft.Colors.YELLOW if hasattr(ft.Colors, 'YELLOW') else ft.Colors.ORANGE),
            "blue": getattr(ft.Colors, 'BLUE', ft.Colors.BLUE if hasattr(ft.Colors, 'BLUE') else ft.Colors.INDIGO),
            "black": ft.Colors.BLACK,
        }
        color = color_map.get(self._guide_color, ft.Colors.WHITE)
        return ft.Paint(
            stroke_width=1.5,
            style=ft.PaintingStyle.STROKE,
            color=color,
        )

    def _on_guide_canvas_resize(self, e):
        """Handle canvas resize to update guide shapes with new dimensions."""
        try:
            self._guide_canvas_width = e.width
            self._guide_canvas_height = e.height
            self._update_guide_canvas()
        except Exception as ex:
            logger.exception("_on_guide_canvas_resize error")
    def _on_preview_guide_canvas_resize(self, e):
        """Handle preview canvas resize to update guide shapes with new dimensions."""
        try:
            self._preview_guide_canvas_width = e.width
            self._preview_guide_canvas_height = e.height
            self._update_preview_guide_canvas()
        except Exception as ex:
            logger.exception("_on_preview_guide_canvas_resize error")
    def _update_guide_canvas(self):
        """Update the guide canvas with current guide type and color.

        Guides should align with the displayed image area (letterboxed within the video container).
        Compute the displayed image rectangle and draw guide shapes only within that area.
        """
        try:
            if not hasattr(self, '_guide_canvas') or self._guide_canvas is None:
                return

            cw = getattr(self, '_guide_canvas_width', 0)
            ch = getattr(self, '_guide_canvas_height', 0)
            if cw <= 0 or ch <= 0:
                return

            # If we don't know source image size, fall back to full canvas
            src_w, src_h = getattr(self, '_current_frame_size', (None, None))
            if not src_w or not src_h:
                # fallback: draw over full canvas
                shapes = self._create_guide_shapes(cw, ch)
                self._guide_canvas.shapes = shapes
            else:
                # Account for image rotation: swap dimensions if 90° or 270°
                rotation = getattr(self, '_active_rotation', 0)
                if rotation in (90, 270):
                    display_src_w, display_src_h = src_h, src_w
                else:
                    display_src_w, display_src_h = src_w, src_h
                
                # Compute displayed image size when using BoxFit.CONTAIN (preserves aspect)
                scale = min(cw / display_src_w, ch / display_src_h)
                disp_w = display_src_w * scale
                disp_h = display_src_h * scale
                ox = (cw - disp_w) / 2.0
                oy = (ch - disp_h) / 2.0

                # Create shapes for the image area and then offset them into canvas space
                shapes = self._create_guide_shapes(disp_w, disp_h)
                offset_shapes = []
                for s in shapes:
                    try:
                        if isinstance(s, cv.Line):
                            offset_shapes.append(cv.Line(s.x1 + ox, s.y1 + oy, s.x2 + ox, s.y2 + oy, paint=s.paint))
                        elif isinstance(s, cv.Circle):
                            offset_shapes.append(cv.Circle(s.x + ox, s.y + oy, s.radius, paint=s.paint))
                        else:
                            # Unknown shape - add as-is
                            offset_shapes.append(s)
                    except Exception:
                        # If shape lacks expected attributes, skip it
                        continue
                self._guide_canvas.shapes = offset_shapes

            try:
                self._guide_canvas.update()
            except Exception:
                pass

        except Exception as e:
            logger.exception("_update_guide_canvas error")
    def _update_preview_guide_canvas(self):
        """Update the preview guide canvas with current guide type and color.

        Align shapes to the preview image area inside the preview overlay (which may be letterboxed).
        """
        try:
            if not hasattr(self, '_preview_guide_canvas') or self._preview_guide_canvas is None:
                return

            cw = getattr(self, '_preview_guide_canvas_width', 0)
            ch = getattr(self, '_preview_guide_canvas_height', 0)
            if cw <= 0 or ch <= 0:
                return

            # Try to get source preview image size if available
            src_w, src_h = getattr(self, '_current_preview_size', (None, None))
            if not src_w or not src_h:
                shapes = self._create_guide_shapes(cw, ch)
                self._preview_guide_canvas.shapes = shapes
            else:
                # Account for image rotation: swap dimensions if 90° or 270°
                rotation = getattr(self, '_active_rotation', 0)
                if rotation in (90, 270):
                    display_src_w, display_src_h = src_h, src_w
                else:
                    display_src_w, display_src_h = src_w, src_h
                
                scale = min(cw / display_src_w, ch / display_src_h)
                disp_w = display_src_w * scale
                disp_h = display_src_h * scale
                ox = (cw - disp_w) / 2.0
                oy = (ch - disp_h) / 2.0
                shapes = self._create_guide_shapes(disp_w, disp_h)
                offset_shapes = []
                for s in shapes:
                    try:
                        if isinstance(s, cv.Line):
                            offset_shapes.append(cv.Line(s.x1 + ox, s.y1 + oy, s.x2 + ox, s.y2 + oy, paint=s.paint))
                        elif isinstance(s, cv.Circle):
                            offset_shapes.append(cv.Circle(s.x + ox, s.y + oy, s.radius, paint=s.paint))
                        else:
                            offset_shapes.append(s)
                    except Exception:
                        continue
                self._preview_guide_canvas.shapes = offset_shapes

            try:
                self._preview_guide_canvas.update()
            except Exception:
                pass
        except Exception as e:
            logger.exception("_update_preview_guide_canvas error")
    def _create_guide_shapes(self, w, h):
        """Create guide shapes for the given dimensions.
        
        Note: The camera handler rotates the actual image data, so w and h
        are already in the correct orientation. No additional transformation needed.
        """
        if self._guide_type == "none":
            return []
        elif self._guide_type == "thirds":
            return self._create_thirds_shapes(w, h)
        elif self._guide_type == "golden":
            return self._create_golden_shapes(w, h)
        elif self._guide_type == "grid":
            return self._create_grid_shapes(w, h)
        elif self._guide_type == "diagonal":
            return self._create_diagonal_shapes(w, h)
        elif self._guide_type == "center":
            return self._create_center_shapes(w, h)
        elif self._guide_type == "fibonacci":
            return self._create_fibonacci_shapes(w, h, flip_h=False, flip_v=False)
        elif self._guide_type == "fibonacci_vflip":
            return self._create_fibonacci_shapes(w, h, flip_h=False, flip_v=True)
        elif self._guide_type == "fibonacci_hflip":
            return self._create_fibonacci_shapes(w, h, flip_h=True, flip_v=False)
        elif self._guide_type == "fibonacci_both":
            return self._create_fibonacci_shapes(w, h, flip_h=True, flip_v=True)
        elif self._guide_type == "triangles":
            return self._create_triangles_shapes(w, h, flipped=False)
        elif self._guide_type == "triangles_flip":
            return self._create_triangles_shapes(w, h, flipped=True)
        return []

    def _create_thirds_shapes(self, w, h):
        """Create rule of thirds guide shapes."""
        paint = self._get_guide_paint()
        # Vertical lines at 1/3 and 2/3
        x1 = w / 3
        x2 = w * 2 / 3
        # Horizontal lines at 1/3 and 2/3
        y1 = h / 3
        y2 = h * 2 / 3
        return [
            cv.Line(x1, 0, x1, h, paint=paint),
            cv.Line(x2, 0, x2, h, paint=paint),
            cv.Line(0, y1, w, y1, paint=paint),
            cv.Line(0, y2, w, y2, paint=paint),
        ]

    def _create_golden_shapes(self, w, h):
        """Create golden ratio guide shapes."""
        paint = self._get_guide_paint()
        # Golden ratio: φ ≈ 1.618, positions at ~0.382 and ~0.618
        phi = 0.381966  # 1 - 1/φ
        x1 = w * phi
        x2 = w * (1 - phi)
        y1 = h * phi
        y2 = h * (1 - phi)
        return [
            cv.Line(x1, 0, x1, h, paint=paint),
            cv.Line(x2, 0, x2, h, paint=paint),
            cv.Line(0, y1, w, y1, paint=paint),
            cv.Line(0, y2, w, y2, paint=paint),
        ]

    def _create_grid_shapes(self, w, h):
        """Create 3x3 grid guide shapes (same as thirds but more visible)."""
        paint = self._get_guide_paint()
        shapes = []
        # 3x3 grid = 4 lines each direction
        for i in range(1, 4):
            x = w * i / 4
            y = h * i / 4
            shapes.append(cv.Line(x, 0, x, h, paint=paint))
            shapes.append(cv.Line(0, y, w, y, paint=paint))
        return shapes

    def _create_diagonal_shapes(self, w, h):
        """Create diagonal guide shapes from corners."""
        paint = self._get_guide_paint()
        return [
            # Main diagonals
            cv.Line(0, 0, w, h, paint=paint),
            cv.Line(w, 0, 0, h, paint=paint),
        ]

    def _create_center_shapes(self, w, h):
        """Create center crosshair shapes."""
        paint = self._get_guide_paint()
        cx = w / 2
        cy = h / 2
        # Small crosshair at center
        size = min(w, h) * 0.05  # 5% of smaller dimension
        return [
            cv.Line(cx - size, cy, cx + size, cy, paint=paint),
            cv.Line(cx, cy - size, cx, cy + size, paint=paint),
            # Optional: add a small circle
            cv.Circle(cx, cy, size * 0.5, paint=paint),
        ]

    def _create_fibonacci_shapes(self, w, h, flip_h=False, flip_v=False):
        """
        Create a Golden Spiral using the "Virtual Canvas" method.
        
        We calculate the spiral on a perfect normalized Golden Rectangle (PHI x 1.0)
        to ensure the geometry never breaks, and then stretch/map the points 
        to the actual screen dimensions. This guarantees a complete spiral 
        that fills the frame regardless of the camera's aspect ratio (16:9, 3:2, etc).
        
        Args:
            w: Width of the display area
            h: Height of the display area
            flip_h: Flip horizontally (mirror vertically around horizontal axis)
            flip_v: Flip vertically (mirror horizontally around vertical axis)
        """
        import math
        paint = self._get_guide_paint()
        shapes = []
        points = []

        # 1. Konfiguracja Wirtualnego Płótna (Idealny Złoty Prostokąt)
        # Pracujemy na liczbach zmiennoprzecinkowych 0.0 -> PHI
        PHI = 1.61803398875
        
        # Ustalamy wirtualne granice.
        # Zakładamy orientację Landscape (poziomą), bo to standard w aparatach.
        v_left, v_top = 0.0, 0.0
        v_right, v_bottom = PHI, 1.0
        
        # Kierunek: 0=Lewo, 1=Góra, 2=Prawo, 3=Dół
        direction = 0 
        iterations = 16 # Wystarczająco, by zejść do niewidocznych detali

        for i in range(iterations):
            # Obliczamy wymiary wirtualnego pudełka
            v_width = v_right - v_left
            v_height = v_bottom - v_top
            
            # W idealnym Złotym Prostokącie ten algorytm NIGDY się nie zatnie.
            side = min(v_width, v_height)
            
            cx, cy = 0.0, 0.0
            angle_start, angle_end = 0, 0

            step = direction % 4
            
            if step == 0:  # Lewy kwadrat (łuk w górę)
                cx = v_left + side
                cy = v_top + side
                angle_start, angle_end = 180, 270
                v_left += side

            elif step == 1: # Górny kwadrat (łuk w prawo)
                cx = v_left
                cy = v_top + side
                angle_start, angle_end = 270, 360
                v_top += side

            elif step == 2: # Prawy kwadrat (łuk w dół)
                cx = v_right - side
                cy = v_top
                angle_start, angle_end = 0, 90
                v_right -= side

            elif step == 3: # Dolny kwadrat (łuk w lewo)
                cx = v_right
                cy = v_bottom - side
                angle_start, angle_end = 90, 180
                v_bottom -= side

            direction += 1

            # Generowanie punktów w przestrzeni wirtualnej
            segments = 10
            a_start_rad = math.radians(angle_start)
            a_end_rad = math.radians(angle_end)
            
            for j in range(segments + 1):
                t = j / segments
                ang = a_start_rad + (a_end_rad - a_start_rad) * t
                
                # Punkt na wirtualnym płótnie
                vx = cx + side * math.cos(ang)
                vy = cy + side * math.sin(ang)
                
                # 2. MAPOWANIE (Skalowanie) do rzeczywistego ekranu
                # Przeliczamy współrzędną wirtualną (0..PHI) na ekranową (0..w)
                # To sprawia, że spirala idealnie wypełnia kadr 16:9
                
                screen_x = (vx / PHI) * w
                screen_y = (vy / 1.0) * h
                
                points.append((screen_x, screen_y))

        # 3. Rysowanie
        prev_pt = None
        for pt in points:
            px, py = pt
            
            # Obsługa lustrzanego odbicia
            if flip_v:  # Vertical flip (mirror around vertical axis)
                px = w - px
            if flip_h:  # Horizontal flip (mirror around horizontal axis)
                py = h - py
            
            if prev_pt is not None:
                # Rzutowanie na int jest bezpieczne dla cv.Line, ale zachowujemy float w obliczeniach
                shapes.append(cv.Line(prev_pt[0], prev_pt[1], px, py, paint=paint))
            prev_pt = (px, py)

        return shapes

    def _create_triangles_shapes(self, w, h, flipped=False):
        """Create golden/harmonious triangle guide shapes.
        
        Harmonious triangles: A main diagonal line from corner to corner,
        with two additional lines from the other corners that meet the
        main diagonal at right angles (perpendicular).
        
        The perpendicular intersection point divides the diagonal at a
        ratio based on the frame's aspect ratio.
        """
        import math
        paint = self._get_guide_paint()
        
        # Calculate the perpendicular distance from corner to main diagonal
        # For a rectangle with width w and height h, the main diagonal goes
        # from (0,0) to (w,h). The perpendicular from (w,0) hits the diagonal
        # at a specific point.
        
        # The distance along the diagonal where the perpendicular from (w,0) meets it:
        # Using projection formula: the foot of perpendicular from point P to line AB
        # For diagonal from A(0,0) to B(w,h), perpendicular from P(w,0):
        # dst = (w*w) / sqrt(w*w + h*h) ... but we need the actual intersection point
        
        # The perpendicular from (w,0) to line y = (h/w)*x meets at:
        # x = w*w / (w + h*h/w) = w^2 / (w + h^2/w) = w^3 / (w^2 + h^2)
        # y = h * x / w
        
        # Actually, for harmonious triangles, we compute dst as:
        # dst = height * cos(atan(width/height)) / cos(atan(height/width))
        # This gives the x-coordinate where the perpendicular from top-right corner
        # meets the left edge (extended if needed)
        
        # Simpler: compute intersection of perpendicular with diagonal
        diag_len_sq = w * w + h * h
        
        # Perpendicular from (w, 0) to diagonal y = (h/w)*x
        # Foot of perpendicular: t = (w*w + 0*h) / (w*w + h*h) = w*w / diag_len_sq
        # Point on diagonal: (t*w, t*h) = (w^3/diag_len_sq, w^2*h/diag_len_sq)
        px1 = (w * w * w) / diag_len_sq
        py1 = (w * w * h) / diag_len_sq
        
        # Perpendicular from (0, h) to diagonal
        # Foot of perpendicular: t = (0*w + h*h) / diag_len_sq = h*h / diag_len_sq  
        # Point on diagonal: (t*w, t*h) = (w*h^2/diag_len_sq, h^3/diag_len_sq)
        px2 = (w * h * h) / diag_len_sq
        py2 = (h * h * h) / diag_len_sq
        
        if not flipped:
            # Standard orientation: main diagonal from top-left to bottom-right
            return [
                # Main diagonal from top-left (0,0) to bottom-right (w,h)
                cv.Line(0, 0, w, h, paint=paint),
                # Perpendicular from top-right (w,0) to the diagonal
                cv.Line(w, 0, px1, py1, paint=paint),
                # Perpendicular from bottom-left (0,h) to the diagonal
                cv.Line(0, h, px2, py2, paint=paint),
            ]
        else:
            # Flipped orientation: main diagonal from top-right to bottom-left
            # Mirror the x-coordinates
            return [
                # Main diagonal from top-right (w,0) to bottom-left (0,h)
                cv.Line(w, 0, 0, h, paint=paint),
                # Perpendicular from top-left (0,0) to the diagonal
                cv.Line(0, 0, w - px1, py1, paint=paint),
                # Perpendicular from bottom-right (w,h) to the diagonal
                cv.Line(w, h, w - px2, py2, paint=paint),
            ]

    # --- Preview Mode Methods ---
    
    def _on_new_preview(self, cached_image: CachedImage):
        """Callback invoked when a new preview image is ready."""
        # Preview ready for display
        try:
            if self._loop:
                asyncio.run_coroutine_threadsafe(self._show_preview_async(cached_image), self._loop)
            else:
                self._show_preview(cached_image)
        except Exception as e:
            logger.exception("Exception in _on_new_preview")
    
    async def _show_preview_async(self, cached_image: CachedImage):
        """Async wrapper to show preview on main loop."""
        self._show_preview(cached_image)
    
    def _show_preview(self, cached_image: CachedImage, reset_timer: bool = True):
        """Show the preview overlay with the given image."""
        try:
            # Update interaction timestamp first
            self._last_user_interaction = time.time()
            
            # If already showing this exact image, just reset timer
            if (self._preview_mode and 
                hasattr(self._preview_image, 'src') and 
                self._preview_image.src == cached_image.filepath and
                not reset_timer):
                return
            
            was_preview = getattr(self, '_preview_mode', False)
            self._preview_mode = True
            
            # Update preview image using file path (much faster than base64!)
            self._preview_image.src = cached_image.filepath
            # Record preview image original size for proper guide alignment
            try:
                img = cv2.imread(cached_image.filepath)
                if img is not None:
                    h, w = img.shape[:2]
                    self._current_preview_size = (w, h)
            except Exception:
                self._current_preview_size = None
            
            # Update filename
            self._preview_filename_text.value = cached_image.filename
            
            # Update counter and show overlay
            current, total = self._preview_manager.get_cache_info()
            self._preview_counter_text.value = t('preview_counter', current=current, total=total)
            self._preview_overlay.visible = True
            # show HUD elements on top of controls; update main status to preview
            try:
                # Only save previous status when entering preview from live view
                if not was_preview:
                    self._prev_status = {
                        'value': self.status_text.value,
                        'color': self.status_text.color,
                        'opacity': getattr(self.status_text, 'opacity', None),
                        'icon_name': getattr(self.status_icon, 'name', None),
                        'icon_color': getattr(self.status_icon, 'color', None),
                    }
                    try:
                        logger.debug("saved prev_status: %s", self._prev_status)
                    except Exception:
                        pass
                    # set shared status to Preview mode (green still camera)
                    self.status_text.value = t('preview_mode')
                    self.status_text.color = ft.Colors.GREEN_400
                    try:
                        self.status_text.opacity = 0.95
                    except Exception:
                        pass
                    self.status_icon.name = ft.Icons.PHOTO_CAMERA
                    self.status_icon.color = ft.Colors.GREEN_400

                # Always ensure HUD elements are visible when previewing
                self._preview_filename_container.visible = True
                self._preview_counter_container.visible = True
            except Exception:
                pass
            self.page.update()
            # Log concise preview info
            logger.info(f"GUI: showing preview {cached_image.filename} ({current}/{total})")
            # Start/reset auto-hide timer
            if reset_timer:
                self._start_preview_timer()
                
        except Exception as e:
            logger.exception("Exception in _show_preview")
    
    def _hide_preview(self):
        """Hide the preview overlay and return to live view."""
        try:
            self._preview_mode = False
            self._preview_overlay.visible = False
            # hide HUD elements and restore status
            try:
                self._preview_filename_container.visible = False
                self._preview_counter_container.visible = False
                # restore previous status
                prev = getattr(self, '_prev_status', None)
                if prev:
                    try:
                        logger.debug("restoring status from preview: %s", prev)
                        self.status_text.value = prev.get('value', self.status_text.value)
                        self.status_text.color = prev.get('color', self.status_text.color)
                        if prev.get('opacity') is not None:
                            try:
                                self.status_text.opacity = prev.get('opacity')
                            except Exception:
                                pass
                        self.status_icon.name = prev.get('icon_name', self.status_icon.name)
                        self.status_icon.color = prev.get('icon_color', self.status_icon.color)
                    except Exception:
                        pass
                    finally:
                        # clear saved state
                        try:
                            self._prev_status = None
                        except Exception:
                            pass
                else:
                    # No saved state - fall back to streaming if stream appears active
                    try:
                        if getattr(self, 'streaming_event', None) and self.streaming_event.is_set():
                            self.status_text.value = t('status_streaming')
                            self.status_text.color = ft.Colors.AMBER_400
                            self.status_text.opacity = 0.7
                            # Transmission: yellow video camera icon
                            self.status_icon.name = ft.Icons.VIDEOCAM
                            self.status_icon.color = ft.Colors.AMBER_400
                    except Exception:
                        pass
            except Exception:
                pass
            self._preview_manager.reset_to_live_view()
            
            # Cancel any pending timer
            if self._preview_timer:
                self._preview_timer.cancel()
                self._preview_timer = None
            
            # Ensure autorotate HUD is not left in a strange visible state
            try:
                if hasattr(self, '_autorotate_btn') and self._autorotate_btn is not None:
                    try:
                        self._autorotate_btn.visible = True
                    except Exception:
                        pass
            except Exception:
                pass
            
            self.page.update()
            
        except Exception as e:
            logger.exception("Exception in _hide_preview")
    def _start_preview_timer(self):
        """Start timer to auto-hide preview after timeout."""
        # Cancel existing timer
        if self._preview_timer:
            self._preview_timer.cancel()
            self._preview_timer = None
        
        # Skip timer if duration is infinity
        if self._preview_duration == float('inf'):
            return
        
        def _timer_callback():
            # Check if user has interacted recently
            elapsed = time.time() - self._last_user_interaction
            if elapsed >= self._preview_duration and self._preview_mode:
                # Schedule hide on main loop
                if self._loop:
                    asyncio.run_coroutine_threadsafe(
                        self._hide_preview_async(), self._loop
                    )
                else:
                    self._hide_preview()
        
        self._preview_timer = threading.Timer(self._preview_duration, _timer_callback)
        self._preview_timer.daemon = True
        self._preview_timer.start()
    
    async def _hide_preview_async(self):
        """Async wrapper to hide preview on main loop."""
        self._hide_preview()

    def _toggle_autorotate(self):
        """Toggle the autorotate setting and update UI."""
        try:
            self._autorotate_enabled = not getattr(self, '_autorotate_enabled', True)
            logger.info("Autorotate %s", 'enabled' if self._autorotate_enabled else 'disabled')
            self._update_autorotate_ui()
        except Exception as e:
            logger.exception("Failed to toggle autorotate")

    def _update_autorotate_ui(self):
        """Update the autorotate button appearance to reflect current state."""
        try:
            btn = getattr(self, '_autorotate_btn', None)
            if not btn:
                return
            # The button content is an Icon; change color to indicate enabled/disabled
            try:
                icon = getattr(btn, 'content', None)
                if icon and hasattr(icon, 'color'):
                    icon.color = ft.Colors.AMBER_400 if self._autorotate_enabled else ft.Colors.WHITE54
            except Exception:
                pass

            # Background highlight when enabled
            try:
                if self._autorotate_enabled:
                    btn.bgcolor = "rgba(100,100,255,0.18)"
                    btn.border = ft.border.all(2, ft.Colors.BLUE_400)
                else:
                    btn.bgcolor = None
                    btn.border = ft.border.all(1, ft.Colors.GREY_700)
            except Exception:
                pass

            try:
                btn.update()
            except Exception:
                pass
        except Exception as e:
            logger.exception("_update_autorotate_ui error")
    
    def _on_orientation_detected(self, orientation_code: int):
        """Callback invoked when EXIF orientation is detected from a downloaded image.
        
        Automatically rotates the live view to match the camera orientation (if enabled).
        
        Args:
            orientation_code: EXIF orientation value (1, 3, 6, or 8)
        """
        try:
            # Respect user preference: only auto-rotate when enabled
            if not getattr(self, '_autorotate_enabled', True):
                logger.debug("Autorotate disabled — ignoring detected orientation %s", orientation_code)
                return

            # Map EXIF orientation to camera handler orientation
            # EXIF 1 = Normal (0°) -> ORIENTATION_NORMAL
            # EXIF 3 = 180° -> ORIENTATION_180
            # EXIF 6 = 270° -> ORIENTATION_270
            # EXIF 8 = 90° -> ORIENTATION_90
            if orientation_code == 1:
                deg = 0
            elif orientation_code == 3:
                deg = 180
            elif orientation_code == 6:
                deg = 270
            elif orientation_code == 8:
                deg = 90
            else:
                logger.warning("Unknown orientation code: %s", orientation_code)
                return
            
            logger.info("Auto-rotating live view to %d° based on captured image EXIF", deg)
            
            # Update camera orientation
            orientation_map = {
                0: self.camera.ORIENTATION_NORMAL,
                90: self.camera.ORIENTATION_90,
                180: self.camera.ORIENTATION_180,
                270: self.camera.ORIENTATION_270,
            }
            code = orientation_map.get(deg, self.camera.ORIENTATION_NORMAL)
            self.camera.set_orientation(code)
            
            # Update UI to reflect new rotation
            self._set_active_rotation(deg)
            
        except Exception as e:
            logger.exception("Failed to auto-rotate from EXIF orientation")



