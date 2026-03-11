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

import configparser
import logging
import os
import threading
from enum import Enum, auto
from typing import Callable

import pystray
from PIL import Image, ImageDraw, ImageFont

from .color import Color
from .config import ASSETS_DIR, CONFIG_FILE, HUESIGNAL_HTML, LOGS_DIR, TRAY_ICON

logger = logging.getLogger("huesignal")

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
        on_resume: Callable[[], None] | None = None,
    ) -> None:
        self._on_restart_stream = on_restart_stream
        self._on_exit = on_exit
        self._get_latest_colors = get_latest_colors
        self._on_resume = on_resume or (lambda: None)

        self._status = StreamStatus.STARTING
        self._lock = threading.Lock()
        self._ready = False
        self._paused = False
        self._base_image = self._load_base_image()

        self._icon = pystray.Icon(
            name="HueSignal",
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
        Makes the icon visible then re-applies current status after a short
        delay to ensure NIM_ADD has been fully processed by the shell before
        any NIM_MODIFY updates are sent."""
        icon.visible = True
        with self._lock:
            self._ready = True
            status = self._status
        # Brief pause lets the shell settle after NIM_ADD before we update
        threading.Timer(0.5, self._apply_status, args=(status,)).start()

    def _apply_status(self, status: StreamStatus) -> None:
        """Write the current status to the icon — always reflects latest value."""
        with self._lock:
            status = self._status  # re-read in case it changed during the delay
        self._icon.icon = self._render_icon(status)
        self._icon.title = self._make_tooltip(status)

    def set_status(self, status: StreamStatus) -> None:
        """Update the status dot and tooltip. Safe to call from any thread."""
        with self._lock:
            if self._status == status:
                return
            self._status = status
            ready = self._ready
        logger.info("[tray] Status → %s", status.name)
        if ready:
            self._icon.icon = self._render_icon(status)
            self._icon.title = self._make_tooltip(status)

    def stop(self) -> None:
        """Remove the tray icon and unblock run()."""
        self._icon.stop()

    @property
    def is_paused(self) -> bool:
        with self._lock:
            return self._paused

    def toggle_pause(self) -> None:
        """Toggle the paused state. Thread-safe."""
        with self._lock:
            self._paused = not self._paused
            paused = self._paused
        logger.info("[tray] Sync %s.", "paused" if paused else "resumed")
        if not paused:
            threading.Thread(target=self._on_resume, daemon=True).start()

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
        return f"HueSignal — {_STATUS_LABELS[status]}"

    # ------------------------------------------------------------------
    # Menu  (dynamic items avoid full rebuild on every color update)
    # ------------------------------------------------------------------

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem("Color preview", self._submenu_preview),
            pystray.MenuItem("Settings", self._submenu_settings),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda _: "Resume sync" if self._paused else "Pause sync",
                self._handle_toggle_pause,
            ),
            pystray.MenuItem("Restart stream", self._handle_restart),
            pystray.MenuItem("Open log", self._handle_open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._handle_exit),
        )

    @property
    def _submenu_preview(self) -> pystray.Menu:
        """Dynamic submenu showing the current color gradient as rgb() values."""

        def _items():
            yield pystray.MenuItem("Open in browser", self._handle_open_browser)
            yield pystray.Menu.SEPARATOR
            colors = self._get_latest_colors()
            if not colors:
                yield pystray.MenuItem("No color data", None, enabled=False)
                return
            for i, c in enumerate(colors, 1):
                label = f"Light {i}:  rgb({c['r']}, {c['g']}, {c['b']})"
                yield pystray.MenuItem(label, None, enabled=False)

        return pystray.Menu(_items)

    @property
    def _submenu_settings(self) -> pystray.Menu:
        """Submenu with toggle actions for logging and tray icon."""

        def _items():
            cfg = configparser.ConfigParser()
            cfg.read(CONFIG_FILE, encoding="utf-8")
            logging_on = cfg.getboolean("general", "logging", fallback=False)
            tray_icon_on = cfg.getboolean("general", "tray_icon", fallback=True)

            yield pystray.MenuItem(
                f"Logging: {'on' if logging_on else 'off'}",
                self._handle_toggle_logging,
            )
            yield pystray.MenuItem(
                f"Tray icon: {'on' if tray_icon_on else 'off'}  (restart required)",
                self._handle_toggle_tray,
            )

        return pystray.Menu(_items)

    # ------------------------------------------------------------------
    # Menu handlers
    # ------------------------------------------------------------------

    def _handle_toggle_logging(
        self, icon: pystray.Icon, item: pystray.MenuItem
    ) -> None:
        self._toggle_config_bool("logging")

    def _handle_toggle_tray(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self._toggle_config_bool("tray_icon")

    def _toggle_config_bool(self, key: str) -> None:
        cfg = configparser.ConfigParser()
        cfg.read(CONFIG_FILE, encoding="utf-8")
        if not cfg.has_section("general"):
            cfg.add_section("general")
        current = cfg.getboolean("general", key, fallback=False)
        cfg.set("general", key, str(not current).lower())
        with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
            cfg.write(fh)
        logger.info("[tray] Settings: %s → %s", key, not current)

    def _handle_open_browser(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        try:
            os.startfile(str(HUESIGNAL_HTML))
        except OSError as exc:
            logger.warning("[tray] Could not open browser: %s", exc)

    def _handle_toggle_pause(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        self.toggle_pause()

    def _handle_restart(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        logger.info("[tray] Restart stream requested.")
        threading.Thread(target=self._on_restart_stream, daemon=True).start()

    def _handle_open_log(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        log_file = LOGS_DIR / "huesignal.log"
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
