"""SignalRGB integration - cacert patching, HTML effect writing, process management."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import subprocess
import time
from pathlib import Path

import psutil

from .config import (
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

# Windows MessageBox constants
_MB_YESNO = 0x00000004
_MB_ICONQUESTION = 0x00000020
_MB_SETFOREGROUND = 0x00010000
_IDYES = 6


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

    If patched, prompts the user to restart SignalRGB.
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

    if _prompt_restart():
        _restart_signalrgb()
        logger.info("[signalrgb] Waiting for SignalRGB to initialise ...")
        time.sleep(6)
    else:
        logger.warning(
            "[signalrgb] Skipped restart - Hue Sync effect may not work until "
            "SignalRGB is restarted manually."
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
# Process restart
# ---------------------------------------------------------------------------


def _prompt_restart() -> bool:
    result = ctypes.windll.user32.MessageBoxW(
        0,
        "HueSignal's CA certificate was added to SignalRGB's certificate store "
        "so it can trust HueSignal's secure local connection.\n\n"
        "SignalRGB needs to restart to apply this change.\n\n"
        "Restart now?",
        "HueSignal - Restart SignalRGB",
        _MB_YESNO | _MB_ICONQUESTION | _MB_SETFOREGROUND,
    )
    return result == _IDYES


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
