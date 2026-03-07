import json
import logging
import logging.handlers
import threading
import time
import subprocess
import ssl
import configparser
import ctypes
import ctypes.wintypes
import functools
from typing import Callable

import urllib3
import requests
from flask import Flask
from flask_sock import Sock
from pathlib import Path

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# TYPES
# ==========================================

Color = dict[str, int]  # {"r": int, "g": int, "b": int}

# ==========================================
# CONFIG
# ==========================================

BASE_DIR = Path(__file__).resolve().parent
HUESYNC_HTML = BASE_DIR / "HueSync.html"

FLASK_PORT = 5123

_config = configparser.ConfigParser()
_config.read(BASE_DIR / "config.ini")

BRIDGE_IP = _config["hue"]["bridge_ip"]
APPLICATION_KEY = _config["hue"]["application_key"]
ENTERTAINMENT_ZONE_NAME = _config["hue"]["entertainment_zone_name"]
ENTERTAINMENT_ID = _config["hue"].get("entertainment_id", "")

logger = logging.getLogger("huesync")
logger.setLevel(logging.INFO)
_formatter = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(_formatter)
logger.addHandler(_stream_handler)

if _config["general"].getboolean("logging", fallback=False):
    _file_handler = logging.handlers.RotatingFileHandler(
        BASE_DIR / "huesync.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=0,
        encoding="utf-8",
    )
    _file_handler.setFormatter(_formatter)
    logger.addHandler(_file_handler)

CERT_FILE = BASE_DIR / "localhost+1.pem"
KEY_FILE = BASE_DIR / "localhost+1-key.pem"

# ==========================================
# FLASK / WEBSOCKET
# ==========================================

app = Flask(__name__)
sock = Sock(app)

_connected_clients: set = set()
_clients_lock = threading.Lock()
_latest_colors: list[Color] = [{"r": 0, "g": 0, "b": 0}]
_colors_lock = threading.Lock()
_stream_interrupt = threading.Event()


@sock.route("/ws")
def ws_handler(ws):
    """Accept a new WebSocket client, send the current color immediately, then keep alive."""
    with _clients_lock:
        _connected_clients.add(ws)
    logger.info("[ws] Client connected (%d total)", len(_connected_clients))
    try:
        with _colors_lock:
            ws.send(json.dumps(_latest_colors, separators=(",", ":")))
        while True:
            ws.receive(timeout=None)
    except Exception:
        pass
    finally:
        with _clients_lock:
            _connected_clients.discard(ws)
        logger.info("[ws] Client disconnected (%d total)", len(_connected_clients))


def broadcast(msg: str) -> None:
    """Send a message to all connected WebSocket clients, dropping any dead connections."""
    with _clients_lock:
        dead = set()
        for ws in _connected_clients:
            try:
                ws.send(msg)
            except Exception:
                dead.add(ws)
        _connected_clients.difference_update(dead)


# ==========================================
# ZONE / LIGHT RESOLUTION
# ==========================================


def resolve_zone_id(bridge_ip: str, api_key: str, zone_name: str) -> str:
    """Find the entertainment zone ID matching the configured zone name."""
    url = f"https://{bridge_ip}/clip/v2/resource/entertainment_configuration"
    resp = requests.get(
        url, headers={"hue-application-key": api_key}, verify=False, timeout=5
    )
    resp.raise_for_status()
    zones = resp.json().get("data", [])
    for zone in zones:
        if zone.get("name", "").lower() == zone_name.lower():
            return zone["id"]
    available = [z.get("name") for z in zones]
    raise ValueError(f"Zone '{zone_name}' not found. Available: {available}")


def resolve_light_ids_in_zone(bridge_ip: str, api_key: str, zone_id: str) -> list[str]:
    """Walk the zone's channel/entertainment/device graph to collect all light resource IDs."""
    headers = {"hue-application-key": api_key}
    url = f"https://{bridge_ip}/clip/v2/resource/entertainment_configuration/{zone_id}"
    resp = requests.get(url, headers=headers, verify=False, timeout=5)
    resp.raise_for_status()
    config = resp.json().get("data", [{}])[0]

    entertainment_rids = set()
    for channel in config.get("channels", []):
        for member in channel.get("members", []):
            svc = member.get("service", {})
            if svc.get("rtype") == "entertainment":
                entertainment_rids.add(svc["rid"])

    device_rids = set()
    for ent_rid in entertainment_rids:
        url = f"https://{bridge_ip}/clip/v2/resource/entertainment/{ent_rid}"
        resp = requests.get(url, headers=headers, verify=False, timeout=5)
        resp.raise_for_status()
        owner = resp.json().get("data", [{}])[0].get("owner", {})
        if owner.get("rtype") == "device":
            device_rids.add(owner["rid"])

    light_rids = []
    for device_rid in device_rids:
        url = f"https://{bridge_ip}/clip/v2/resource/device/{device_rid}"
        resp = requests.get(url, headers=headers, verify=False, timeout=5)
        resp.raise_for_status()
        for svc in resp.json().get("data", [{}])[0].get("services", []):
            if svc.get("rtype") == "light":
                light_rids.append(svc["rid"])
    return light_rids


def fetch_initial_colors(
    bridge_ip: str, api_key: str, light_ids: list[str]
) -> list[Color]:
    """Fetch current color state of each light at startup; returns black if all lights are off."""
    headers = {"hue-application-key": api_key}
    colors = []

    for light_id in light_ids:
        url = f"https://{bridge_ip}/clip/v2/resource/light/{light_id}"
        resp = requests.get(url, headers=headers, verify=False, timeout=5)
        resp.raise_for_status()
        data = resp.json().get("data", [{}])[0]

        if not data.get("on", {}).get("on", False):
            colors.append({"r": 0, "g": 0, "b": 0})
            continue

        bri = data.get("dimming", {}).get("brightness", 100.0) / 100.0

        if "gradient" in data and data["gradient"].get("points"):
            for point in data["gradient"]["points"]:
                xy = point["color"]["xy"]
                r, g, b = xy_bri_to_rgb(xy["x"], xy["y"], bri)
                colors.append({"r": r, "g": g, "b": b})
        elif "color" in data and "xy" in data["color"]:
            xy = data["color"]["xy"]
            r, g, b = xy_bri_to_rgb(xy["x"], xy["y"], bri)
            colors.append({"r": r, "g": g, "b": b})
        else:
            colors.append({"r": 0, "g": 0, "b": 0})

    return colors if colors else [{"r": 0, "g": 0, "b": 0}]


def fetch_current_colors(bridge_ip: str, api_key: str, light_id: str) -> list[Color]:
    """Fetch the current color of a single light; used when a toggle-on event carries no color."""
    headers = {"hue-application-key": api_key}
    url = f"https://{bridge_ip}/clip/v2/resource/light/{light_id}"
    resp = requests.get(url, headers=headers, verify=False, timeout=5)
    resp.raise_for_status()
    data = resp.json().get("data", [{}])[0]
    bri = data.get("dimming", {}).get("brightness", 100.0) / 100.0
    if "gradient" in data and data["gradient"].get("points"):
        colors = []
        for point in data["gradient"]["points"]:
            xy = point["color"]["xy"]
            r, g, b = xy_bri_to_rgb(xy["x"], xy["y"], bri)
            colors.append({"r": r, "g": g, "b": b})
        return colors
    elif "color" in data and "xy" in data["color"]:
        xy = data["color"]["xy"]
        r, g, b = xy_bri_to_rgb(xy["x"], xy["y"], bri)
        return [{"r": r, "g": g, "b": b}]
    return [{"r": 0, "g": 0, "b": 0}]


# ==========================================
# COLOR CONVERSION
# ==========================================


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp a value between lo and hi."""
    return max(lo, min(hi, v))


def _srgb_gamma(linear: float) -> float:
    """Apply sRGB gamma correction to a linear light value."""
    if linear <= 0.0031308:
        return 12.92 * linear
    return 1.055 * (linear ** (1.0 / 2.4)) - 0.055


def xy_bri_to_rgb(x: float, y: float, bri: float = 1.0) -> tuple[int, int, int]:
    """Convert Hue CIE xy + brightness to an sRGB (r, g, b) tuple in 0-255 range."""
    if y == 0:
        return 0, 0, 0
    Y = bri
    X = (Y / y) * x
    Z = (Y / y) * (1.0 - x - y)
    r_lin = X * 1.656492 - Y * 0.354851 - Z * 0.255038
    g_lin = -X * 0.707196 + Y * 1.655397 + Z * 0.036152
    b_lin = X * 0.051713 - Y * 0.121364 + Z * 1.011530
    min_lin = min(r_lin, g_lin, b_lin)
    if min_lin < 0:
        r_lin -= min_lin
        g_lin -= min_lin
        b_lin -= min_lin
    max_lin = max(r_lin, g_lin, b_lin)
    if max_lin > 1.0:
        r_lin /= max_lin
        g_lin /= max_lin
        b_lin /= max_lin
    r_lin *= bri
    g_lin *= bri
    b_lin *= bri
    return (
        int(_clamp(_srgb_gamma(_clamp(r_lin))) * 255 + 0.5),
        int(_clamp(_srgb_gamma(_clamp(g_lin))) * 255 + 0.5),
        int(_clamp(_srgb_gamma(_clamp(b_lin))) * 255 + 0.5),
    )


# ==========================================
# SIGNALRGB
# ==========================================

_SIGNAL_MAIN_PROCESS = "SignalRgb.exe"
_SIGNAL_LAUNCHER = (
    Path.home() / "AppData" / "Local" / "VortxEngine" / "SignalRgbLauncher.exe"
)

# Windows MessageBox constants
_MB_YESNO = 0x00000004
_MB_ICONQUESTION = 0x00000020
_MB_SETFOREGROUND = 0x00010000
_IDYES = 6


def find_signalrgb_cacert() -> Path | None:
    """Find cacert.pem by locating the running SignalRgb.exe process."""
    result = subprocess.run(
        [
            "wmic",
            "process",
            "where",
            f"name='{_SIGNAL_MAIN_PROCESS}'",
            "get",
            "ExecutablePath",
        ],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.lower().endswith("signalrgb.exe"):
            return Path(line).parent / "cacert.pem"
    return None


def ensure_ca_in_cacert(cacert_path: Path, ca_cert_path: Path) -> bool:
    """Append mkcert's CA to SignalRGB's cacert.pem if not already present; backs up first.

    Returns:
        True  - cert was appended (SignalRGB needs a restart to pick it up).
        False - cert was already present, no changes made.
    """
    ca_cert_text = ca_cert_path.read_text(encoding="utf-8")
    cacert_text = cacert_path.read_text(encoding="utf-8")

    if ca_cert_text.strip() in cacert_text:
        logger.info("[signalrgb] mkcert CA already in cacert.pem, skipping.")
        return False

    bak_path = cacert_path.with_suffix(".pem.bak")
    if not bak_path.exists():
        bak_path.write_bytes(cacert_path.read_bytes())
        logger.info("[signalrgb] Backup created: %s", bak_path)

    cacert_path.write_text(cacert_text + "\n" + ca_cert_text, encoding="utf-8")
    logger.info("[signalrgb] mkcert CA appended to %s", cacert_path)
    return True


def _prompt_signalrgb_restart() -> bool:
    """Terminate SignalRgb.exe and relaunch it via the launcher."""
    result = ctypes.windll.user32.MessageBoxW(
        0,
        "The certificate store was updated and SignalRGB needs to restart.\n\nRestart now?",
        "HueSync - Restart SignalRGB",
        _MB_YESNO | _MB_ICONQUESTION | _MB_SETFOREGROUND,
    )
    return result == _IDYES


def _restart_signalrgb() -> None:
    """Terminate all SignalRGB processes and relaunch the launcher.

    Waits up to 10 s for all processes to exit before relaunching so that the
    launcher does not immediately collide with a lingering service process.
    """
    logger.info("[signalrgb] Stopping %s ...", _SIGNAL_MAIN_PROCESS)
    subprocess.call(
        ["taskkill", "/F", "/IM", _SIGNAL_MAIN_PROCESS],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    # Poll until the process is gone or we hit the deadline
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
            "[signalrgb] Launcher not found at %s — cannot relaunch.", _SIGNAL_LAUNCHER
        )
        return

    logger.info("[signalrgb] Launching %s ...", _SIGNAL_LAUNCHER)
    subprocess.Popen(
        [str(_SIGNAL_LAUNCHER)],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    logger.info("[signalrgb] Relaunch issued.")


def patch_signalrgb_cacert(cacert_path: Path, ca_cert_path: Path) -> None:
    """Patch SignalRGB's cacert.pem and, if patched, prompt the user to restart."""
    patched = ensure_ca_in_cacert(cacert_path, ca_cert_path)
    if not patched:
        return

    if _prompt_signalrgb_restart():
        _restart_signalrgb()
        # Give SignalRGB time to initialise its HTTP/WebSocket stack before
        # the rest of main() continues (e.g. writing the effect HTML).
        logger.info("[signalrgb] Waiting for SignalRGB to initialise ...")
        time.sleep(6)
    else:
        logger.info(
            "[signalrgb] User chose not to restart — "
            "Hue Sync effect may not work until SignalRGB is restarted manually."
        )


# ==========================================
# HTML
# ==========================================


def write_html(file_path: Path, wss_url: str) -> None:
    """Write the SignalRGB effect HTML file with the current tunnel URL baked in."""
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Hue Sync</title>
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
    file_path.write_text(html, encoding="utf-8")


# ==========================================
# HUE EVENT PARSER
# ==========================================


def extract_colors_from_event(data: list, watched_ids: set) -> list[Color]:
    """
    Parse SSE event data into a list of RGB dicts for watched lights.
    Returns black on light-off; fetches current state from bridge on toggle-on with no color data.
    """
    colors = []
    for event in data:
        if event.get("type") != "update":
            continue
        for item in event.get("data", []):
            if item.get("type") != "light":
                continue
            light_id = item.get("id")
            if light_id not in watched_ids:
                continue

            on_state = item.get("on", {})

            # Light turned off — push black
            if "on" in on_state and not on_state["on"]:
                colors.append({"r": 0, "g": 0, "b": 0})
                continue

            bri = item.get("dimming", {}).get("brightness", 100.0) / 100.0

            has_color = False
            if "gradient" in item and item["gradient"].get("points"):
                has_color = True
                for point in item["gradient"]["points"]:
                    xy = point["color"]["xy"]
                    r, g, b = xy_bri_to_rgb(xy["x"], xy["y"], bri)
                    colors.append({"r": r, "g": g, "b": b})
            elif "color" in item and "xy" in item["color"]:
                has_color = True
                xy = item["color"]["xy"]
                r, g, b = xy_bri_to_rgb(xy["x"], xy["y"], bri)
                colors.append({"r": r, "g": g, "b": b})

            if not has_color:
                if "on" in on_state and on_state["on"]:
                    # Toggle-on with no color — fetch from bridge
                    logger.info(
                        "[hue] Toggle-on with no color data, fetching state for %s",
                        light_id,
                    )
                    colors.extend(
                        fetch_current_colors(BRIDGE_IP, APPLICATION_KEY, light_id)
                    )
                elif "dimming" in item:
                    # Brightness-only event — fetch current colors from bridge at new brightness
                    logger.info(
                        "[hue] Brightness change, fetching state for %s", light_id
                    )
                    colors.extend(
                        fetch_current_colors(BRIDGE_IP, APPLICATION_KEY, light_id)
                    )

    return colors


# ==========================================
# HUE SSE STREAM
# ==========================================


def hue_stream_thread(bridge_ip: str, api_key: str, watched_ids: set) -> None:
    """Listen to the Hue bridge SSE event stream and broadcast color updates to WebSocket clients."""
    global _latest_colors
    url = f"https://{bridge_ip}/eventstream/clip/v2"
    headers = {"hue-application-key": api_key, "Accept": "text/event-stream"}
    backoff = 3

    while True:
        _stream_interrupt.clear()
        try:
            logger.info("Connecting to Hue bridge at %s", bridge_ip)
            with requests.get(
                url, headers=headers, stream=True, verify=False, timeout=None
            ) as resp:
                resp.raise_for_status()
                backoff = 3
                logger.info("Connected. Listening for Hue events ...")
                buffer = []
                for raw_line in resp.iter_lines(decode_unicode=True):
                    # Check for wake-triggered interrupt
                    if _stream_interrupt.is_set():
                        logger.info("[hue] Stream interrupted by wake event.")
                        resp.close()
                        break
                    if raw_line.startswith("data:"):
                        buffer.append(raw_line[5:].strip())
                    elif raw_line == "" and buffer:
                        payload = " ".join(buffer)
                        buffer = []
                        try:
                            events = json.loads(payload)
                            colors = extract_colors_from_event(events, watched_ids)
                            if colors:
                                with _colors_lock:
                                    _latest_colors = colors
                                msg = json.dumps(colors, separators=(",", ":"))
                                rgb_preview = ", ".join(
                                    f"rgb({c['r']},{c['g']},{c['b']})"
                                    for c in colors[:4]
                                )
                                logger.info("Push -> %s", rgb_preview)
                                broadcast(msg)
                        except json.JSONDecodeError as exc:
                            logger.warning(
                                "[hue] Malformed SSE payload, skipping: %s", exc
                            )
        except requests.RequestException as exc:
            logger.error("Stream error: %s", exc)
        except Exception:
            logger.exception("Unhandled exception in hue stream thread")

        if _stream_interrupt.is_set():
            backoff = 3
            continue

        logger.info("Reconnecting in %ds ...", backoff)
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


# ==========================================
# SLEEP / WAKE HANDLING
# ==========================================


def sleep_wake_monitor(on_wake_callback: Callable[[], None]) -> None:
    """Listens for Windows sleep/wake events and calls on_wake_callback on resume."""
    WM_POWERBROADCAST = 0x0218
    PBT_APMRESUMEAUTOMATIC = 0x0012

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # Use pointer-sized types so 64-bit WPARAM/LPARAM don't overflow
    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,  # return type (LRESULT)
        ctypes.wintypes.HWND,
        ctypes.c_uint,
        ctypes.c_size_t,  # WPARAM  (unsigned pointer-sized)
        ctypes.c_ssize_t,  # LPARAM  (signed pointer-sized)
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

    # Tell ctypes the correct signature for DefWindowProcW
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
            on_wake_callback()
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    hinstance = kernel32.GetModuleHandleW(None)
    class_name = "HueSyncPowerMonitor"

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

    msg = ctypes.wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), hwnd, 0, 0) != 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def on_wake(bridge_ip: str, api_key: str, light_ids: list[str]) -> None:
    """Called on Windows wake: force SSE reconnect, then re-seed WS clients."""
    global _latest_colors

    _stream_interrupt.set()

    logger.info("[power] Waiting for network after wake ...")
    deadline = time.monotonic() + 60
    attempt = 0
    while time.monotonic() < deadline:
        time.sleep(2)
        attempt += 1
        try:
            logger.info("[power] Re-fetching light state (attempt %d) ...", attempt)
            fresh_colors = fetch_initial_colors(bridge_ip, api_key, light_ids)
            with _colors_lock:
                _latest_colors = fresh_colors
            broadcast(json.dumps(fresh_colors, separators=(",", ":")))
            logger.info("[power] Colors re-seeded to WS clients.")
            return
        except Exception as exc:
            logger.warning("[power] Not ready yet: %s", exc)

    logger.info(
        "[power] Giving up re-seeding after wake — stream reconnect will recover."
    )


# ==========================================
# MAIN
# ==========================================


def main():
    """Resolve zone/lights, patch SignalRGB cacert, seed initial color state, and start Flask over SSL."""
    global ENTERTAINMENT_ID, _latest_colors

    link = Path.home() / "Documents" / "WhirlwindFX" / "Effects" / "HueSync.html"
    link.parent.mkdir(parents=True, exist_ok=True)
    if not link.exists():
        link.symlink_to(HUESYNC_HTML)

    mkcert_caroot = Path(
        subprocess.check_output(
            ["mkcert", "-CAROOT"], text=True, creationflags=subprocess.CREATE_NO_WINDOW
        ).strip()
    )
    mkcert_ca_cert = mkcert_caroot / "rootCA.pem"

    if not ENTERTAINMENT_ID:
        logger.info("Resolving zone ID for '%s' ...", ENTERTAINMENT_ZONE_NAME)
        ENTERTAINMENT_ID = resolve_zone_id(
            BRIDGE_IP, APPLICATION_KEY, ENTERTAINMENT_ZONE_NAME
        )
        logger.info("Zone ID: %s", ENTERTAINMENT_ID)

    logger.info("Fetching light IDs in zone ...")
    light_ids = resolve_light_ids_in_zone(BRIDGE_IP, APPLICATION_KEY, ENTERTAINMENT_ID)
    logger.info("Watching %d light(s): %s", len(light_ids), light_ids)
    watched_ids = set(light_ids)

    logger.info("Fetching initial light state ...")
    with _colors_lock:
        _latest_colors = fetch_initial_colors(BRIDGE_IP, APPLICATION_KEY, light_ids)
    rgb_preview = ", ".join(
        f"rgb({c['r']},{c['g']},{c['b']})" for c in _latest_colors[:4]
    )
    logger.info("Initial colors: %s", rgb_preview)

    logger.info("[signalrgb] Checking cacert.pem ...")
    cacert_path = find_signalrgb_cacert()
    if cacert_path and cacert_path.exists():
        patch_signalrgb_cacert(cacert_path, mkcert_ca_cert)
    else:
        logger.info(
            "[signalrgb] Not running or cacert.pem not found, skipping cacert patch."
        )

    wss_url = "wss://127.0.0.1:5123/ws"
    write_html(HUESYNC_HTML, wss_url)
    logger.info("Effect file written: %s", HUESYNC_HTML)

    hue_thread = threading.Thread(
        target=hue_stream_thread,
        args=(BRIDGE_IP, APPLICATION_KEY, watched_ids),
        daemon=True,
    )
    hue_thread.start()

    wake_cb = functools.partial(on_wake, BRIDGE_IP, APPLICATION_KEY, light_ids)

    power_thread = threading.Thread(
        target=sleep_wake_monitor,
        args=(lambda: threading.Thread(target=wake_cb, daemon=True).start(),),
        daemon=True,
    )
    power_thread.start()

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
    app.run(host="127.0.0.1", port=FLASK_PORT, ssl_context=ssl_ctx)


if __name__ == "__main__":
    main()
