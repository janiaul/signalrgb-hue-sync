"""Windows sleep/wake monitoring via a hidden Win32 message-loop window."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import threading
import time
from typing import Callable

from .color import Color
from .config import AppConfig

logger = logging.getLogger("huesignal")

WM_POWERBROADCAST = 0x0218
PBT_APMRESUMEAUTOMATIC = 0x0012

# How long to wait for the network after wake before giving up (seconds)
_WAKE_NETWORK_TIMEOUT = 60
_WAKE_RETRY_INTERVAL = 2


class PowerMonitor(threading.Thread):
    """Background thread that registers a hidden Win32 window to receive
    WM_POWERBROADCAST messages and calls *on_wake* when the system resumes."""

    def __init__(self, on_wake: Callable[[], None]) -> None:
        super().__init__(name="power-monitor", daemon=True)
        self._on_wake = on_wake

    def run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,
            ctypes.wintypes.HWND,
            ctypes.c_uint,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        )

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", ctypes.c_uint),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", ctypes.wintypes.HINSTANCE),
                ("hIcon", ctypes.wintypes.HICON),
                ("hCursor", ctypes.wintypes.HANDLE),
                ("hbrBackground", ctypes.wintypes.HBRUSH),
                ("lpszMenuName", ctypes.wintypes.LPCWSTR),
                ("lpszClassName", ctypes.wintypes.LPCWSTR),
            ]

        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.c_uint,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]

        def wnd_proc(hwnd, msg, wparam, lparam):
            if msg == WM_POWERBROADCAST and wparam == PBT_APMRESUMEAUTOMATIC:
                logger.info("[power] Wake event detected — triggering reconnect ...")
                threading.Thread(target=self._on_wake, daemon=True).start()
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        hinstance = kernel32.GetModuleHandleW(None)
        class_name = "HueSignalPowerMonitor"

        wc = WNDCLASSW()
        wc.lpfnWndProc = WNDPROC(wnd_proc)
        wc.hInstance = hinstance
        wc.lpszClassName = class_name
        user32.RegisterClassW(ctypes.byref(wc))

        hwnd = user32.CreateWindowExW(
            0,
            class_name,
            class_name,
            0,
            0,
            0,
            0,
            0,
            None,
            None,
            hinstance,
            None,
        )

        logger.info("[power] Sleep/wake monitor active.")
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), hwnd, 0, 0) != 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))


def make_wake_handler(
    cfg: AppConfig,
    stream_interrupt: threading.Event,
    on_colors: Callable[[list[Color]], None],
    fetch_colors: Callable[[AppConfig], list[Color]],
) -> Callable[[], None]:
    """Return a wake callback that interrupts the SSE stream, waits for the
    network, re-fetches light state, and re-seeds WebSocket clients."""

    def _handler() -> None:
        stream_interrupt.set()
        logger.info("[power] Waiting for network after wake ...")
        deadline = time.monotonic() + _WAKE_NETWORK_TIMEOUT
        attempt = 0

        try:
            while time.monotonic() < deadline:
                time.sleep(_WAKE_RETRY_INTERVAL)
                attempt += 1
                try:
                    logger.info(
                        "[power] Re-fetching light state (attempt %d) ...", attempt
                    )
                    colors = fetch_colors(cfg)
                    on_colors(colors)
                    logger.info("[power] Colors re-seeded.")
                    time.sleep(
                        2
                    )  # let stream thread reconnect before clearing interrupt
                    return
                except Exception as exc:
                    logger.warning("[power] Network not ready yet: %s", exc)

            logger.warning(
                "[power] Gave up re-seeding after wake — stream reconnect will recover."
            )
        finally:
            # Always unblock the stream thread, even if re-seeding failed
            stream_interrupt.clear()

    return _handler
