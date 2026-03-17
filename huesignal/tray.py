"""System tray icon - status display, color preview, and context menu.

Status states
-------------
STARTING     Grey dot  - app is initialising
CONNECTING   Amber dot - attempting to reach the bridge
CONNECTED    Green dot - SSE stream is live
RECONNECTING Red dot   - stream lost, retrying

The base icon is generated programmatically so no external .ico is required.
Drop a real tray.ico into assets/ and it will be used automatically.
"""

from __future__ import annotations

import configparser
import ctypes
import logging
import os
import threading
from enum import Enum, auto
from typing import Callable

import pystray
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .color import Color
from .config import (
    CONFIG_FILE,
    HUESIGNAL_HTML,
    LOGS_DIR,
    ASSETS_DIR,
    write_config_atomic,
)

logger = logging.getLogger("huesignal")

_ICON = ASSETS_DIR / "logo.png"
_LOGO_SIZE = 64  # logo rendered at this pixel size
_DOT_RADIUS = 16  # status indicator radius (px)
_LOGO_PAD = 4  # padding around the logo on all four sides (set to 0 to fill canvas)
_DOT_PAD = 4  # transparent gap between the status dot (incl. border) and the logo (px)
_ICON_SIZE = _LOGO_SIZE + 2 * _LOGO_PAD
_DOT_CX = (
    _ICON_SIZE - _DOT_RADIUS - 1
)  # dot anchored to bottom-right; larger radius grows left/up
_LOGO_CORNER_R = round(
    46 / 256 * _LOGO_SIZE
)  # from SVG rx="46" on 256×256 canvas; used for placeholder


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

    Call run() on the main thread - it blocks until the user clicks Exit.
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

        # Cached settings - loaded once at startup, updated after each toggle
        # to avoid a config.ini read on every submenu render.
        _init_cfg = configparser.ConfigParser()
        _init_cfg.read(CONFIG_FILE, encoding="utf-8")
        self._cached_logging: bool = _init_cfg.getboolean(
            "general", "logging", fallback=False
        )
        self._cached_tray_icon: bool = _init_cfg.getboolean(
            "general", "tray_icon", fallback=True
        )

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
        """Write the current status to the icon - always reflects latest value."""
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
        logger.info("[tray] Status -> %s", status.name)
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

    @property
    def current_status(self) -> StreamStatus:
        with self._lock:
            return self._status

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
        """Load assets/tray.ico as a _ICON_SIZE image.

        For .ico files, the correctly-sized embedded frame is pulled directly
        so no scaling occurs at all.  For other formats, LANCZOS + UnsharpMask
        is used as a fallback.
        """
        if _ICON.exists():
            try:
                img = Image.open(_ICON)
                target = (_LOGO_SIZE, _LOGO_SIZE)

                if _ICON.suffix.lower() == ".ico":
                    # Use n_frames/seek() - avoids img.ico.sizes whose API
                    # changed across Pillow versions (property vs method).
                    # Goal: exact 64x64 frame > smallest frame >= 64 > largest.
                    n_frames = getattr(img, "n_frames", 1)
                    chosen = img.copy()
                    for i in range(n_frames):
                        try:
                            img.seek(i)
                        except EOFError:
                            break
                        w, h = img.size
                        cw, _ = chosen.size
                        if (w, h) == target:
                            chosen = img.copy()
                            break
                        if w >= _LOGO_SIZE and (cw < _LOGO_SIZE or w < cw):
                            chosen = img.copy()
                        elif cw < _LOGO_SIZE and w > cw:
                            chosen = img.copy()
                    frame = chosen.convert("RGBA")
                    if frame.size != target:
                        frame = frame.resize(target, Image.LANCZOS)
                        frame = frame.filter(
                            ImageFilter.UnsharpMask(radius=1, percent=180, threshold=2)
                        )
                    return frame

                # Non-.ico fallback (e.g. PNG)
                img = img.convert("RGBA")
                if img.size != target:
                    img = img.resize(target, Image.LANCZOS)
                    img = img.filter(
                        ImageFilter.UnsharpMask(radius=1, percent=180, threshold=2)
                    )
                return img

            except Exception as exc:
                logger.warning(
                    "[tray] Could not load %s: %s - using placeholder.", _ICON, exc
                )
        else:
            logger.debug("[tray] No tray icon found - using generated placeholder.")
        return _make_placeholder()

    def _render_icon(self, status: StreamStatus) -> Image.Image:
        """Overlay a coloured status dot centred on the bottom-right corner of the HS square."""
        canvas = Image.new("RGBA", (_ICON_SIZE, _ICON_SIZE), (0, 0, 0, 0))
        canvas.paste(self._base_image, (_LOGO_PAD, _LOGO_PAD), self._base_image)
        d = ImageDraw.Draw(canvas)
        dot_color = _STATUS_COLORS[status]
        r = _DOT_RADIUS
        cx = cy = _DOT_CX  # dot centre on the 45° point of the logo's rounded corner
        # Transparent padding ring on the top-left arc only so the dot stays
        # flush with the bottom-right corner while blending with the logo.
        if _DOT_PAD > 0:
            p = r + 1 + _DOT_PAD  # 1 accounts for the border ring
            erase = Image.new("L", canvas.size, 0)
            ImageDraw.Draw(erase).ellipse([cx - p, cy - p, cx + p, cy + p], fill=255)
            # Zero out the bottom-right quadrant so no padding is applied there
            ImageDraw.Draw(erase).rectangle(
                [cx, cy, canvas.width - 1, canvas.height - 1], fill=0
            )
            cr, cg, cb, ca = canvas.split()
            new_a = Image.composite(Image.new("L", canvas.size, 0), ca, erase)
            canvas = Image.merge("RGBA", (cr, cg, cb, new_a))
            d = ImageDraw.Draw(canvas)
        # Dark border ring for legibility on any taskbar colour
        d.ellipse([cx - r - 1, cy - r - 1, cx + r + 1, cy + r + 1], fill=(0, 0, 0, 200))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(*dot_color, 255))
        return canvas

    @staticmethod
    def _make_tooltip(status: StreamStatus) -> str:
        return f"HueSignal - {_STATUS_LABELS[status]}"

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
            yield pystray.MenuItem(
                f"Logging: {'on' if self._cached_logging else 'off'}",
                self._handle_toggle_logging,
            )
            yield pystray.MenuItem(
                f"Tray icon: {'on' if self._cached_tray_icon else 'off'}  (restart required)",
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
        new_val = not current
        cfg.set("general", key, str(new_val).lower())
        write_config_atomic(cfg, CONFIG_FILE)
        if key == "logging":
            self._cached_logging = new_val
        elif key == "tray_icon":
            self._cached_tray_icon = new_val
        logger.info("[tray] Settings: %s -> %s", key, new_val)

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
        if not self._cached_logging:

            def _show() -> None:
                ctypes.windll.user32.MessageBoxW(
                    0,
                    "File logging is currently disabled.\n\nEnable it via Settings -> Logging: off.",
                    "HueSignal",
                    0x40 | 0x10000,  # MB_ICONINFORMATION | MB_SETFOREGROUND
                )

            threading.Thread(target=_show, daemon=True).start()
            return
        log_file = LOGS_DIR / "huesignal.log"
        if log_file.exists():
            try:
                os.startfile(str(log_file))
            except OSError as exc:
                logger.warning("[tray] Could not open log: %s", exc)
        else:
            logger.debug("[tray] No log file at %s", log_file)

    def _handle_exit(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        logger.info("[tray] Exit requested.")
        self.stop()
        self._on_exit()


# ------------------------------------------------------------------
# Placeholder icon generator
# ------------------------------------------------------------------


def _lerp_color(c1: tuple, c2: tuple, t: float) -> tuple:
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def _gradient_color(t: float) -> tuple:
    """Three-stop gradient: electric blue (#0047FF) -> deep indigo (#3B00CC) -> hot pink (#DC0078)."""
    if t < 0.4:
        return _lerp_color((0, 71, 255), (59, 0, 204), t / 0.4)
    else:
        return _lerp_color((59, 0, 204), (220, 0, 120), (t - 0.4) / 0.6)


def _make_placeholder() -> Image.Image:
    """Generate the HueSignal 'HS' lettermark icon - used when no tray icon is present."""
    sq = _LOGO_SIZE
    img = Image.new("RGBA", (sq, sq), (0, 0, 0, 0))

    # Diagonal two-stop gradient using horizontal scanlines
    sq_img = Image.new("RGBA", (sq, sq), (0, 0, 0, 0))
    draw_sq = ImageDraw.Draw(sq_img)
    for i in range(sq * 2):
        t = i / (sq * 2)
        r, g, b = _gradient_color(t)
        # Each diagonal band is a line from (i-sq, sq) to (sq, i-sq)
        x0 = max(0, i - sq)
        x1 = min(sq, i)
        if x0 <= x1:
            draw_sq.line([(x0, i - x0), (x1, i - x1)], fill=(r, g, b, 255))

    radius = _LOGO_CORNER_R
    mask = Image.new("L", (sq, sq), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, sq - 1, sq - 1], radius=radius, fill=255
    )
    sq_img.putalpha(mask)

    img.paste(sq_img, (0, 0), sq_img)

    d = ImageDraw.Draw(img)

    # Bold font for the lettermark
    font = None
    for name in ["segoeuib.ttf", "arialbd.ttf", "calibrib.ttf"]:
        try:
            font = ImageFont.truetype(name, int(sq * 0.56))
            break
        except (IOError, OSError):
            continue
    if font is None:
        font = ImageFont.load_default()

    text = "HS"
    bbox = d.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    # Centre within the square
    tx = (sq - tw) // 2 - bbox[0]
    ty = (sq - th) // 2 - bbox[1] - max(1, sq // 20)

    s = max(1, sq // 32)
    d.text((tx + s, ty + s * 2), text, font=font, fill=(0, 0, 60, 110))
    d.text((tx, ty), text, font=font, fill=(240, 245, 255, 255))

    return img
