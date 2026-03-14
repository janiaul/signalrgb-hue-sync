"""BridgeMonitor - periodic bridge reachability check.

Pings the Hue bridge every PING_INTERVAL seconds. On state transitions:
  reachable -> unreachable : fires on_lost callback  (toast + tray update)
  unreachable -> reachable : fires on_restored callback (toast + tray update)

Owns all toast decisions. The stream thread drives CONNECTING/CONNECTED
status during reconnect cycles; BridgeMonitor forces RECONNECTING on loss.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable, Optional

import urllib3

from .config import AppConfig, ASSETS_DIR
from .hue import get_hue_session

# verify=False used alongside fingerprint pinning - see hue.py for rationale.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger("huesignal")

PING_INTERVAL = 10  # seconds between reachability checks
PING_TIMEOUT = 5  # seconds before a ping attempt is considered failed

_APP_ID = "HueSignal"
_ICON = str(ASSETS_DIR / "toast.png")


def _send_toast(title: str, message: str) -> None:
    """Send a Windows toast notification. Fails silently if winotify is unavailable."""
    try:
        from winotify import Notification  # type: ignore

        icon = _ICON if Path(_ICON).exists() else ""
        toast = Notification(app_id=_APP_ID, title=title, msg=message, icon=icon)
        toast.show()
    except Exception as exc:
        logger.debug("[monitor] Toast failed: %s", exc)


class BridgeMonitor(threading.Thread):
    """Background thread that pings the bridge and drives toast + tray state."""

    def __init__(
        self,
        cfg: AppConfig,
        on_lost: Callable[[], None],
        on_restored: Callable[[], None],
    ) -> None:
        super().__init__(name="bridge-monitor", daemon=True)
        self._cfg = cfg
        self._on_lost = on_lost
        self._on_restored = on_restored
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()  # set by stop() to unblock the run() wait

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()

    def run(self) -> None:
        reachable: Optional[bool] = None  # None = unknown (startup)

        while not self._stop_event.is_set():
            self._wake_event.clear()
            now_reachable = self._ping()

            if reachable is None:
                # First check - establish baseline silently
                reachable = now_reachable
            elif now_reachable and not reachable:
                reachable = True
                logger.info("[monitor] Bridge reachable - connection restored.")
                threading.Thread(
                    target=_send_toast,
                    args=("HueSignal - Connected", "Hue bridge connection restored."),
                    daemon=True,
                ).start()
                threading.Thread(target=self._on_restored, daemon=True).start()
            elif not now_reachable and reachable:
                reachable = False
                logger.warning("[monitor] Bridge unreachable - connection lost.")
                threading.Thread(
                    target=_send_toast,
                    args=("HueSignal - Disconnected", "Lost connection to Hue bridge."),
                    daemon=True,
                ).start()
                threading.Thread(target=self._on_lost, daemon=True).start()

            # Sleep for PING_INTERVAL, but wake early if signalled
            self._wake_event.wait(timeout=PING_INTERVAL)

    def _ping(self) -> bool:
        """Return True if the bridge responds to a lightweight request."""
        cfg = self._cfg
        url = f"https://{cfg.bridge_ip}/clip/v2/resource/device"
        headers = {
            "hue-application-key": cfg.application_key,
            "Accept": "application/json",
        }
        try:
            resp = get_hue_session().get(
                url,
                headers=headers,
                verify=False,
                timeout=PING_TIMEOUT,
            )
            return resp.status_code < 500
        except Exception:
            return False
