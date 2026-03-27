"""SignalRGB integration - cacert patching, HTML effect writing, process management."""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

import psutil

from .config import (
    ASSETS_DIR,
    EFFECTS_DIR,
    HUESIGNAL_HTML,
    SIGNALRGB_EFFECTS_DIR,
    WSS_URL,
)

logger = logging.getLogger("huesignal")

_SIGNAL_MAIN_PROCESS = "SignalRgb.exe"
_SIGNAL_LAUNCHER = (
    Path.home() / "AppData" / "Local" / "VortxEngine" / "SignalRgbLauncher.exe"
)


# ---------------------------------------------------------------------------
# Effect HTML
# ---------------------------------------------------------------------------


def write_effect_html(wss_url: str = WSS_URL) -> None:
    """Write the SignalRGB effect HTML file with the WSS URL baked in."""
    html = f"""\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Hue Signal</title>
  <style>html,body{{margin:0;padding:0;overflow:hidden}}</style>
</head>
<body>
<canvas id="c"></canvas>
<script>
(function () {{
  const canvas = document.getElementById("c");
  const ctx    = canvas.getContext("2d");
  let colors   = [{{r:0,g:0,b:0}}];

  function resize() {{
    canvas.width  = window.innerWidth  || 320;
    canvas.height = window.innerHeight || 200;
    draw();
  }}

  function draw() {{
    const W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    if (colors.length === 1) {{
      ctx.fillStyle = `rgb(${{colors[0].r}},${{colors[0].g}},${{colors[0].b}})`;
      ctx.fillRect(0, 0, W, H);
      return;
    }}
    const grad = ctx.createLinearGradient(0, 0, W, 0);
    colors.forEach((c, i) => {{
      grad.addColorStop(i / (colors.length - 1), `rgb(${{c.r}},${{c.g}},${{c.b}})`);
    }});
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, W, H);
  }}

  function connect() {{
    const ws = new WebSocket("{wss_url}");
    ws.onmessage = (e) => {{
      try {{
        colors = JSON.parse(e.data);
        draw();
      }} catch(_) {{}}
    }};
    ws.onclose = () => setTimeout(connect, 2000);
    ws.onerror = () => ws.close();
  }}

  window.addEventListener("resize", resize);
  resize();
  connect();
}})();
</script>
</body>
</html>
"""
    EFFECTS_DIR.mkdir(parents=True, exist_ok=True)
    HUESIGNAL_HTML.write_text(html, encoding="utf-8")
    logger.info("[signalrgb] Effect HTML written -> %s", HUESIGNAL_HTML)


def ensure_effects_symlink() -> None:
    """Create a symlink in the SignalRGB effects directory pointing to HueSignal.html."""
    link = SIGNALRGB_EFFECTS_DIR / "HueSignal.html"
    link.parent.mkdir(parents=True, exist_ok=True)
    if not link.exists():
        try:
            link.symlink_to(HUESIGNAL_HTML)
            logger.info("[signalrgb] Symlink created -> %s", link)
        except OSError as exc:
            logger.warning("[signalrgb] Could not create symlink: %s", exc)


# ---------------------------------------------------------------------------
# cacert patching
# ---------------------------------------------------------------------------


def find_cacert() -> Path | None:
    """Locate SignalRGB's cacert.pem by inspecting the running process path."""
    try:
        for proc in psutil.process_iter(["name", "exe"]):
            if (proc.info["name"] or "").lower() == _SIGNAL_MAIN_PROCESS.lower():
                exe = proc.info["exe"]
                if exe:
                    return Path(exe).parent / "cacert.pem"
    except Exception as exc:
        logger.warning("[signalrgb] Could not locate SignalRGB process: %s", exc)
    return None


def patch_cacert(cacert_path: Path, ca_path: Path) -> None:
    """Append HueSignal's CA cert to SignalRGB's cacert.pem if not already present.

    If patched, restarts SignalRGB automatically and notifies the user via toast.
    """
    try:
        ca_text = ca_path.read_text(encoding="utf-8")
        existing = cacert_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("[signalrgb] Cannot read cert files: %s", exc)
        return

    if ca_text.strip() in existing:
        logger.info(
            "[signalrgb] HueSignal CA already present in cacert.pem - no changes needed."
        )
        return

    # Back up once
    bak = cacert_path.with_suffix(".pem.bak")
    if not bak.exists():
        bak.write_bytes(cacert_path.read_bytes())
        logger.info("[signalrgb] Backup created: %s", bak)

    tmp = cacert_path.with_suffix(".pem.tmp")
    try:
        tmp.write_text(existing + "\n" + ca_text, encoding="utf-8")
        tmp.replace(cacert_path)
        logger.info("[signalrgb] HueSignal CA appended to %s", cacert_path)
    except OSError as exc:
        logger.error("[signalrgb] Failed to write cacert.pem: %s", exc)
        tmp.unlink(missing_ok=True)
        return

    _send_toast(
        "HueSignal \u2014 Restarting SignalRGB",
        "HueSignal's CA was added to SignalRGB's certificate store. "
        "Restarting SignalRGB to apply the change...",
    )
    _restart_signalrgb()
    logger.info("[signalrgb] Waiting for SignalRGB to initialise ...")
    time.sleep(6)
    _send_toast(
        "HueSignal \u2014 Ready",
        "SignalRGB restarted. Effect mirroring is active.",
    )


def _is_safe_cacert_path(path: Path) -> bool:
    """Return True only if path is a plausible SignalRGB cacert.pem location.

    Guards against writing to an unexpected location if the exe path returned
    by psutil is somehow malformed or points outside the user's AppData tree.
    """
    if path.name.lower() != "cacert.pem":
        return False
    try:
        app_data = (Path.home() / "AppData").resolve()
        path.resolve().relative_to(app_data)
        return True
    except ValueError:
        return False


def setup_signalrgb(ca_path: Path) -> None:
    """Top-level entry point: write HTML, create symlink, patch cacert if needed."""
    write_effect_html()
    ensure_effects_symlink()

    cacert_path = find_cacert()
    if cacert_path and cacert_path.exists():
        if _is_safe_cacert_path(cacert_path):
            patch_cacert(cacert_path, ca_path)
        else:
            logger.warning(
                "[signalrgb] Refusing to patch cacert at unexpected path: %s",
                cacert_path,
            )
    else:
        logger.info(
            "[signalrgb] Not running or cacert.pem not found - skipping cacert patch."
        )


# ---------------------------------------------------------------------------
# Toast helper (used by SignalRGBMonitor)
# ---------------------------------------------------------------------------

_APP_ID = "HueSignal"
_ICON = str(ASSETS_DIR / "logo.png")


def _send_toast(title: str, message: str) -> None:
    """Send a Windows toast notification. Fails silently if winotify is unavailable."""
    try:
        from winotify import Notification  # type: ignore

        icon = _ICON if Path(_ICON).exists() else ""
        toast = Notification(app_id=_APP_ID, title=title, msg=message, icon=icon)
        toast.show()
    except Exception as exc:
        logger.debug("[signalrgb] Toast failed: %s", exc)


# ---------------------------------------------------------------------------
# Background monitor - detects SignalRGB updates and re-patches cacert.pem
# ---------------------------------------------------------------------------


class SignalRGBMonitor(threading.Thread):
    """Polls cacert.pem and silently re-patches it if our CA was removed.

    This covers the case where SignalRGB updates while HueSignal is already
    running, overwriting cacert.pem and dropping the HueSignal CA entry.

    On detection:
      - Re-patches cacert.pem atomically (no user prompt).
      - Auto-restarts SignalRGB *only* if the process is currently running
        (i.e. it already loaded the stale cacert).  If the process is not yet
        running we just patch and let the new instance start cleanly.
      - Sends toast notifications to keep the user informed.
    """

    _CHECK_INTERVAL = 30  # seconds between polls

    def __init__(self, ca_path: Path) -> None:
        super().__init__(name="signalrgb-monitor", daemon=True)
        self._ca_path = ca_path
        self._cacert_path: Path | None = None
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.wait(self._CHECK_INTERVAL):
            try:
                self._check()
            except Exception as exc:
                logger.warning("[signalrgb] Monitor check error: %s", exc)

    def _check(self) -> None:
        # Re-discover the process path each poll so we handle installs to new locations.
        running_path = find_cacert()
        effective_path = running_path or self._cacert_path

        if effective_path is None or not effective_path.exists():
            return

        # Keep the tracked path current.
        if running_path is not None:
            self._cacert_path = running_path

        try:
            ca_text = self._ca_path.read_text(encoding="utf-8").strip()
            existing = effective_path.read_text(encoding="utf-8")
        except OSError:
            return

        if ca_text in existing:
            return  # CA present — nothing to do.

        if not _is_safe_cacert_path(effective_path):
            logger.warning(
                "[signalrgb] Refusing to auto-patch at unexpected path: %s",
                effective_path,
            )
            return

        logger.info(
            "[signalrgb] HueSignal CA missing from cacert.pem — "
            "SignalRGB was likely updated. Re-patching automatically..."
        )
        threading.Thread(
            target=_send_toast,
            args=(
                "HueSignal \u2014 SignalRGB Update Detected",
                "Re-patching certificate store. SignalRGB will restart automatically.",
            ),
            daemon=True,
        ).start()

        # Back up the new (unpatched) cacert if no backup exists yet.
        bak = effective_path.with_suffix(".pem.bak")
        if not bak.exists():
            try:
                bak.write_bytes(effective_path.read_bytes())
                logger.info("[signalrgb] Backup created: %s", bak)
            except OSError as exc:
                logger.warning("[signalrgb] Could not create backup: %s", exc)

        # Atomic patch.
        try:
            ca_text_full = self._ca_path.read_text(encoding="utf-8")
            existing = effective_path.read_text(encoding="utf-8")
            tmp = effective_path.with_suffix(".pem.tmp")
            tmp.write_text(existing + "\n" + ca_text_full, encoding="utf-8")
            tmp.replace(effective_path)
            logger.info("[signalrgb] cacert.pem re-patched successfully.")
        except OSError as exc:
            logger.error("[signalrgb] Auto re-patch failed: %s", exc)
            threading.Thread(
                target=_send_toast,
                args=(
                    "HueSignal \u2014 Patch Failed",
                    "Could not update SignalRGB certificate store. Restart HueSignal to retry.",
                ),
                daemon=True,
            ).start()
            return

        if running_path is not None:
            # SignalRGB is running and already loaded the stale cacert — restart it.
            logger.info(
                "[signalrgb] Restarting SignalRGB to apply patched certificate store..."
            )
            _restart_signalrgb()
            threading.Thread(
                target=_send_toast,
                args=(
                    "HueSignal \u2014 Done",
                    "SignalRGB restarted. Effect mirroring is active.",
                ),
                daemon=True,
            ).start()
        else:
            # SignalRGB is not running yet (mid-install); the fresh process will
            # load the already-patched cacert — no restart needed.
            threading.Thread(
                target=_send_toast,
                args=(
                    "HueSignal \u2014 Certificate Updated",
                    "SignalRGB certificate store patched. Effect mirroring will be active when SignalRGB starts.",
                ),
                daemon=True,
            ).start()


# ---------------------------------------------------------------------------
# Process restart
# ---------------------------------------------------------------------------


def _restart_signalrgb() -> None:
    logger.info("[signalrgb] Terminating %s ...", _SIGNAL_MAIN_PROCESS)
    subprocess.call(
        ["taskkill", "/F", "/IM", _SIGNAL_MAIN_PROCESS],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {_SIGNAL_MAIN_PROCESS}", "/NH"],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if _SIGNAL_MAIN_PROCESS not in result.stdout:
            break
        time.sleep(0.5)

    if not _SIGNAL_LAUNCHER.exists():
        logger.error(
            "[signalrgb] Launcher not found at %s - cannot relaunch.", _SIGNAL_LAUNCHER
        )
        return

    logger.info("[signalrgb] Launching %s ...", _SIGNAL_LAUNCHER)
    subprocess.Popen([str(_SIGNAL_LAUNCHER)], creationflags=subprocess.CREATE_NO_WINDOW)
    logger.info("[signalrgb] Relaunch issued.")
