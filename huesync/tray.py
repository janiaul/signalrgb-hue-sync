"""System tray icon — status display, color preview, and context menu.

Status states
-------------
STARTING     Grey dot  — app is initialising
CONNECTING   Amber dot — attempting to reach the bridge
CONNECTED    Green dot — SSE stream is live
RECONNECTING Red dot   — stream lost, retrying

The base icon is generated programmatically so no external .ico is required.
Drop a real tray.ico into assets/ and it will be used automatically.
"""

from __future__ import annotations

import logging
import os
import threading
from enum import Enum, auto
from typing import Callable

import pystray
from PIL import Image, ImageDraw, ImageFont

from .color import Color, rgb_preview
from .config import ASSETS_DIR, LOGS_DIR, TRAY_ICON

logger = logging.getLogger("huesync")

_ICON_SIZE = 64
_DOT_RADIUS = 10
_DOT_MARGIN = 4


class StreamStatus(Enum):
    STARTING = auto()
    CONNECTING = auto()
    CONNECTED = auto()
    RECONNECTING = auto()


# Map the string tokens emitted by HueStreamThread to enum values
STATUS_MAP: dict[str, StreamStatus] = {
    "starting": StreamStatus.STARTING,
    "connecting": StreamStatus.CONNECTING,
    "connected": StreamStatus.CONNECTED,
    "reconnecting": StreamStatus.RECONNECTING,
}

_STATUS_COLORS: dict[StreamStatus, tuple[int, int, int]] = {
    StreamStatus.STARTING: (160, 160, 160),  # grey
    StreamStatus.CONNECTING: (230, 160, 0),  # amber
    StreamStatus.CONNECTED: (60, 200, 80),  # green
    StreamStatus.RECONNECTING: (220, 50, 50),  # red
}

_STATUS_LABELS: dict[StreamStatus, str] = {
    StreamStatus.STARTING: "Starting…",
    StreamStatus.CONNECTING: "Connecting…",
    StreamStatus.CONNECTED: "Connected",
    StreamStatus.RECONNECTING: "Reconnecting…",
}


class TrayIcon:
    """Manages the system tray icon lifecycle.

    Call run() on the main thread — it blocks until the user clicks Exit.
    All other methods are safe to call from any thread.
    """

    def __init__(
        self,
        on_restart_stream: Callable[[], None],
        on_exit: Callable[[], None],
        get_latest_colors: Callable[[], list[Color]],
    ) -> None:
        self._on_restart_stream = on_restart_stream
        self._on_exit = on_exit
        self._get_latest_colors = get_latest_colors

        self._status = StreamStatus.STARTING
        self._lock = threading.Lock()
        self._base_image = self._load_base_image()

        self._icon = pystray.Icon(
            name="HueSync",
            icon=self._render_icon(StreamStatus.STARTING),
            title=self._make_tooltip(StreamStatus.STARTING),
            menu=self._build_menu(),
        )

    # ------------------------------------------------------------------
    # Public API (thread-safe)
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the tray icon. Blocks the calling (main) thread."""
        logger.info("[tray] System tray icon started.")
        self._icon.run(setup=self._on_ready)

    def _on_ready(self, icon: pystray.Icon) -> None:
        """Called by pystray once the icon is fully initialised.
        Must set icon.visible = True or the icon will never appear.
        Re-applies the current status in case set_status() was called before
        the Win32 message loop was ready to receive updates."""
        with self._lock:
            status = self._status
        icon.icon = self._render_icon(status)
        icon.title = self._make_tooltip(status)
        icon.visible = True

    def set_status(self, status: StreamStatus) -> None:
        """Update the status dot and tooltip. Safe to call from any thread."""
        with self._lock:
            if self._status == status:
                return
            self._status = status
        self._icon.icon = self._render_icon(status)
        self._icon.title = self._make_tooltip(status)
        logger.info("[tray] Status → %s", status.name)

    def stop(self) -> None:
        """Remove the tray icon and unblock run()."""
        self._icon.stop()

    # ------------------------------------------------------------------
    # Icon rendering
    # ------------------------------------------------------------------

    def _load_base_image(self) -> Image.Image:
        """Load assets/tray.ico if present, otherwise generate a placeholder."""
        if TRAY_ICON.exists():
            try:
                img = Image.open(TRAY_ICON).convert("RGBA")
                return img.resize((_ICON_SIZE, _ICON_SIZE), Image.LANCZOS)
            except Exception as exc:
                logger.warning(
                    "[tray] Could not load %s: %s — using placeholder.", TRAY_ICON, exc
                )
        return _make_placeholder()

    def _render_icon(self, status: StreamStatus) -> Image.Image:
        """Overlay a coloured status dot on the base icon."""
        img = self._base_image.copy()
        d = ImageDraw.Draw(img)
        dot_color = _STATUS_COLORS[status]
        r = _DOT_RADIUS
        cx = _ICON_SIZE - r - _DOT_MARGIN
        cy = _ICON_SIZE - r - _DOT_MARGIN
        # Dark border ring for legibility on any taskbar colour
        d.ellipse([cx - r - 1, cy - r - 1, cx + r + 1, cy + r + 1], fill=(0, 0, 0, 180))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*dot_color, 255))
        return img

    @staticmethod
    def _make_tooltip(status: StreamStatus) -> str:
        return f"HueSync — {_STATUS_LABELS[status]}"

    # ------------------------------------------------------------------
    # Menu  (dynamic items avoid full rebuild on every color update)
    # ------------------------------------------------------------------

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            # Dynamic color preview — re-evaluated each time menu opens
            pystray.MenuItem(
                lambda _: "Colors: " + rgb_preview(self._get_latest_colors()),
                action=None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Restart stream", self._handle_restart),
            pystray.MenuItem("Open log", self._handle_open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._handle_exit),
        )

    # ------------------------------------------------------------------
    # Menu handlers
    # ------------------------------------------------------------------

    def _handle_restart(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        logger.info("[tray] Restart stream requested.")
        self.set_status(StreamStatus.CONNECTING)
        threading.Thread(target=self._on_restart_stream, daemon=True).start()

    def _handle_open_log(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        log_file = LOGS_DIR / "huesync.log"
        if log_file.exists():
            try:
                os.startfile(str(log_file))
            except OSError as exc:
                logger.warning("[tray] Could not open log: %s", exc)
        else:
            logger.info("[tray] No log file at %s", log_file)

    def _handle_exit(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        logger.info("[tray] Exit requested.")
        self.stop()
        self._on_exit()


# ------------------------------------------------------------------
# Placeholder icon generator
# ------------------------------------------------------------------


def _make_placeholder() -> Image.Image:
    """Generate a clean 'H' glyph icon — used when no tray.ico is present."""
    size = _ICON_SIZE
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Background circle
    pad = 2
    d.ellipse([pad, pad, size - pad, size - pad], fill=(45, 55, 72, 255))

    # "H" glyph — try Segoe UI first, fall back to built-in default
    try:
        font = ImageFont.truetype("segoeui.ttf", size=36)
    except (IOError, OSError):
        font = ImageFont.load_default()

    text = "H"
    bbox = d.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (size - tw) / 2 - bbox[0]
    ty = (size - th) / 2 - bbox[1] - 2
    d.text((tx, ty), text, font=font, fill=(255, 255, 255, 255))

    return img
