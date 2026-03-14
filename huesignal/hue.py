"""Philips Hue bridge I/O: zone resolution, light queries, and SSE streaming."""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import ssl
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import requests
import urllib3
from requests.adapters import HTTPAdapter

from .color import Color, BLACK, xy_bri_to_rgb, rgb_preview
from .config import AppConfig

# verify=False is used alongside fingerprint pinning - CA chain check is intentionally
# skipped because the bridge uses a Signify-internal CA; fingerprint provides security.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger("huesignal")

# How long to wait between SSE reconnect attempts (doubles each failure, caps at 60s)
_BACKOFF_INITIAL = 3
_BACKOFF_MAX = 60


# ---------------------------------------------------------------------------
# Fingerprint-pinned session
# ---------------------------------------------------------------------------


class _FingerprintAdapter(HTTPAdapter):
    """Requests transport adapter that pins every connection to a known cert fingerprint.

    urllib3's assert_fingerprint verifies the SHA-256 digest of the server's
    DER-encoded certificate after every TLS handshake, independently of CA
    chain and hostname checks.
    """

    def __init__(self, fingerprint: str, **kwargs) -> None:
        self._fingerprint = fingerprint
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs) -> None:
        kwargs["assert_fingerprint"] = self._fingerprint
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs) -> requests.adapters.HTTPAdapter:
        proxy_kwargs["assert_fingerprint"] = self._fingerprint
        return super().proxy_manager_for(proxy, **proxy_kwargs)


_hue_session: Optional[requests.Session] = None


def init_hue_session(fingerprint: str) -> None:
    """Configure the module-level session with SHA-256 certificate fingerprint pinning."""
    global _hue_session
    session = requests.Session()
    session.mount("https://", _FingerprintAdapter(fingerprint))
    _hue_session = session


def get_hue_session() -> requests.Session:
    """Return the pinned session. Falls back to a plain session with a warning if not initialised."""
    if _hue_session is None:
        logger.warning("[hue] Session not initialised - using unverified fallback.")
        return requests.Session()
    return _hue_session


def fetch_bridge_fingerprint(host: str, port: int = 443, timeout: int = 5) -> str:
    """Open a raw TLS socket and return the SHA-256 fingerprint of the bridge certificate."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock) as ssock:
            cert_der = ssock.getpeercert(binary_form=True)
    if not cert_der:
        raise ValueError("Bridge returned no certificate during TLS handshake.")
    return hashlib.sha256(cert_der).hexdigest()


# ---------------------------------------------------------------------------
# Zone / light resolution
# ---------------------------------------------------------------------------


def resolve_zone_id(cfg: AppConfig) -> str:
    """Return the entertainment zone ID matching cfg.entertainment_zone_name."""
    url = f"https://{cfg.bridge_ip}/clip/v2/resource/entertainment_configuration"
    resp = _get(cfg, url)
    zones = resp.json().get("data", [])
    for zone in zones:
        if zone.get("name", "").lower() == cfg.entertainment_zone_name.lower():
            return zone["id"]
    available = [z.get("name") for z in zones]
    raise ValueError(
        f"Zone '{cfg.entertainment_zone_name}' not found on bridge. "
        f"Available zones: {available}"
    )


def resolve_light_ids(cfg: AppConfig) -> list[str]:
    """Walk the zone's channel/entertainment/device graph; return all light resource IDs."""
    headers = _headers(cfg)
    base = f"https://{cfg.bridge_ip}"

    # Zone -> entertainment service IDs
    url = f"{base}/clip/v2/resource/entertainment_configuration/{cfg.entertainment_id}"
    config = _get(cfg, url).json().get("data", [{}])[0]

    ent_rids: set[str] = set()
    for channel in config.get("channels", []):
        for member in channel.get("members", []):
            svc = member.get("service", {})
            if svc.get("rtype") == "entertainment":
                ent_rids.add(svc["rid"])

    # Entertainment -> device IDs (parallel)
    def _get_device_rid(ent_rid: str) -> str | None:
        owner = (
            get_hue_session()
            .get(
                f"{base}/clip/v2/resource/entertainment/{ent_rid}",
                headers=headers,
                verify=False,
                timeout=5,
            )
            .json()
            .get("data", [{}])[0]
            .get("owner", {})
        )
        return owner["rid"] if owner.get("rtype") == "device" else None

    device_rids: set[str] = set()
    if ent_rids:
        with ThreadPoolExecutor(max_workers=min(len(ent_rids), 8)) as pool:
            for rid in pool.map(_get_device_rid, ent_rids):
                if rid is not None:
                    device_rids.add(rid)

    # Device -> light IDs (parallel)
    def _get_light_rids(device_rid: str) -> list[str]:
        services = (
            get_hue_session()
            .get(
                f"{base}/clip/v2/resource/device/{device_rid}",
                headers=headers,
                verify=False,
                timeout=5,
            )
            .json()
            .get("data", [{}])[0]
            .get("services", [])
        )
        return [svc["rid"] for svc in services if svc.get("rtype") == "light"]

    light_rids: list[str] = []
    if device_rids:
        with ThreadPoolExecutor(max_workers=min(len(device_rids), 8)) as pool:
            for rids in pool.map(_get_light_rids, device_rids):
                light_rids.extend(rids)

    return light_rids


# ---------------------------------------------------------------------------
# Color fetching
# ---------------------------------------------------------------------------


def fetch_light_colors(cfg: AppConfig, light_id: str) -> list[Color]:
    """Return the current color(s) of a single light resource."""
    data = (
        _get(cfg, f"https://{cfg.bridge_ip}/clip/v2/resource/light/{light_id}")
        .json()
        .get("data", [{}])[0]
    )
    bri = data.get("dimming", {}).get("brightness", 100.0) / 100.0
    return _colors_from_light_data(data, bri)


def fetch_initial_colors(cfg: AppConfig) -> list[Color]:
    """Fetch current colors for all lights in the zone at startup."""
    if not cfg.resolved_light_ids:
        return [BLACK]

    def _fetch_one(light_id: str) -> list[Color]:
        data = (
            _get(cfg, f"https://{cfg.bridge_ip}/clip/v2/resource/light/{light_id}")
            .json()
            .get("data", [{}])[0]
        )
        if not data.get("on", {}).get("on", False):
            return [BLACK]
        bri = data.get("dimming", {}).get("brightness", 100.0) / 100.0
        return _colors_from_light_data(data, bri)

    colors: list[Color] = []
    with ThreadPoolExecutor(max_workers=min(len(cfg.resolved_light_ids), 8)) as pool:
        for result in pool.map(_fetch_one, cfg.resolved_light_ids):
            colors.extend(result)

    return colors or [BLACK]


# ---------------------------------------------------------------------------
# SSE event parsing
# ---------------------------------------------------------------------------


def extract_colors_from_event(
    data: list,
    watched_ids: set[str],
    cfg: AppConfig,
) -> tuple[list[Color], list[str]]:
    """Parse SSE event payload into colors and a list of light IDs needing a fetch.

    Returns (colors, needs_fetch) where:
    - colors: inline color data extracted directly from the event
    - needs_fetch: light IDs that were toggled on with no color data yet
    """
    colors: list[Color] = []
    needs_fetch: list[str] = []

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

            # Light turned off
            if "on" in on_state and not on_state["on"]:
                colors.append(BLACK)
                continue

            bri = item.get("dimming", {}).get("brightness", 100.0) / 100.0
            event_colors = _colors_from_light_data(item, bri)

            if event_colors:
                colors.extend(event_colors)
            else:
                # No inline color data - brightness change or toggle-on.
                # Caller decides whether to fetch based on context.
                reason = (
                    "toggle-on"
                    if ("on" in on_state and on_state["on"])
                    else "brightness change"
                )
                logger.debug("[hue] %s with no color data for %s", reason, light_id)
                needs_fetch.append(light_id)

    return colors, needs_fetch


# ---------------------------------------------------------------------------
# SSE stream (runs in its own thread)
# ---------------------------------------------------------------------------


class HueStreamThread(threading.Thread):
    """Background thread that subscribes to the Hue SSE stream and calls
    *on_colors* whenever new color data arrives.

    Uses requests for SSE streaming with an infinite read timeout. The Hue
    bridge sends no keepalives - it only writes on light state changes - so
    the stream may be idle indefinitely. interrupt() sets a flag that is
    checked each time iter_lines() yields; restart latency is bounded by
    how long until the next bridge event.

    Optionally calls *on_status* with a string token so the tray icon can
    reflect the current connection state.
    """

    def __init__(
        self,
        cfg: AppConfig,
        on_colors: Callable[[list[Color]], None],
        interrupt: threading.Event,
        on_status: Optional[Callable[[str], None]] = None,
        on_reseed: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__(name="hue-stream", daemon=True)
        self._cfg = cfg
        self._on_colors = on_colors
        self._interrupt = interrupt
        self._on_status = on_status or (lambda _: None)
        self._on_reseed = on_reseed or (lambda: None)
        self._last_pushed: list[Color] = []
        self._last_color_event: list[Color] = []
        self._resp: Optional[requests.Response] = None
        self._resp_lock = threading.Lock()
        self._watched_ids: frozenset[str] = frozenset(cfg.resolved_light_ids)

    def interrupt(self) -> None:
        """Signal the stream to reconnect and unblock iter_lines() immediately."""
        self._interrupt.set()
        with self._resp_lock:
            if self._resp is not None:
                self._resp.close()

    def run(self) -> None:
        cfg = self._cfg
        url = f"https://{cfg.bridge_ip}/eventstream/clip/v2"
        headers = {**_headers(cfg), "Accept": "text/event-stream"}
        backoff = _BACKOFF_INITIAL

        while True:
            self._interrupt.clear()
            try:
                logger.info("[hue] Connecting to bridge at %s ...", cfg.bridge_ip)
                self._on_status("connecting")
                with get_hue_session().get(
                    url,
                    headers=headers,
                    stream=True,
                    verify=False,
                    timeout=(10, None),
                ) as resp:
                    with self._resp_lock:
                        self._resp = resp
                    try:
                        resp.raise_for_status()
                        backoff = _BACKOFF_INITIAL
                        self._last_pushed = []
                        self._last_color_event = []
                        self._on_status("connected")
                        logger.info("[hue] Connected. Listening for events ...")
                        self._on_reseed()

                        buffer: list[str] = []
                        for raw_line in resp.iter_lines(decode_unicode=True):
                            if self._interrupt.is_set():
                                logger.warning(
                                    "[hue] Stream interrupted - reconnecting."
                                )
                                break
                            if raw_line.startswith("data:"):
                                buffer.append(raw_line[5:].strip())
                            elif raw_line == "" and buffer:
                                self._dispatch(" ".join(buffer), cfg)
                                buffer.clear()
                    finally:
                        with self._resp_lock:
                            self._resp = None

            except requests.RequestException as exc:
                cause = exc
                while cause is not None:
                    if isinstance(cause, ConnectionAbortedError):
                        logger.warning(
                            "[hue] Connection closed by bridge - reconnecting."
                        )
                        break
                    cause = getattr(cause, "__cause__", None) or getattr(
                        cause, "__context__", None
                    )
                else:
                    logger.error("[hue] Stream error: %s", exc)
            except AttributeError:
                # resp.close() from interrupt() can null the internal file pointer
                # while iter_lines() is still reading - treat it as a clean interrupt.
                logger.debug("[hue] Stream closed during read.")
            except Exception:
                logger.exception("[hue] Unhandled exception in stream thread")

            if self._interrupt.is_set():
                backoff = _BACKOFF_INITIAL
                continue

            self._on_status("reconnecting")
            logger.info("[hue] Reconnecting in %ds ...", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    @staticmethod
    def _colors_match(a: list[Color], b: list[Color], tol: int = 2) -> bool:
        """Return True if two color lists are identical within per-channel tolerance."""
        return len(a) == len(b) and all(
            abs(x["r"] - y["r"]) <= tol
            and abs(x["g"] - y["g"]) <= tol
            and abs(x["b"] - y["b"]) <= tol
            for x, y in zip(a, b)
        )

    def _push(self, colors: list[Color], label: str = "") -> None:
        """Push colors to the callback, skipping if nearly identical to last push."""
        if self._colors_match(colors, self._last_pushed):
            logger.debug("[hue] Push skipped - colors unchanged.")
            return
        self._last_pushed = colors
        suffix = f" ({label})" if label else ""
        logger.info("[hue] Push -> %s%s", rgb_preview(colors), suffix)
        self._on_colors(colors)

    def _dispatch(self, payload: str, cfg: AppConfig) -> None:
        if self._interrupt.is_set():
            return  # restart in progress - discard stale events
        watched = self._watched_ids
        try:
            events = json.loads(payload)
            colors, needs_fetch = extract_colors_from_event(events, watched, cfg)
            if colors:
                if self._colors_match(colors, self._last_color_event, tol=1):
                    logger.debug("[hue] Color event skipped - duplicate scene recall.")
                else:
                    self._last_color_event = colors
                    self._push(colors)
            if needs_fetch and not colors:
                self._fetch_light_state(needs_fetch, cfg)
        except json.JSONDecodeError as exc:
            logger.warning("[hue] Malformed SSE payload, skipping: %s", exc)

    def _fetch_light_state(self, light_ids: list[str], cfg: AppConfig) -> None:
        """Fetch current color for lights with no inline color data in the event."""
        if self._interrupt.is_set():
            return
        colors: list[Color] = []
        for light_id in light_ids:
            try:
                colors.extend(fetch_light_colors(cfg, light_id))
            except Exception as exc:
                logger.warning(
                    "[hue] Could not fetch light state for %s: %s", light_id, exc
                )
        if colors:
            self._push(colors, "brightness fetch")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _headers(cfg: AppConfig) -> dict[str, str]:
    return {"hue-application-key": cfg.application_key}


def _get(cfg: AppConfig, url: str) -> requests.Response:
    resp = get_hue_session().get(
        url,
        headers={**_headers(cfg), "Connection": "close"},
        verify=False,
        timeout=5,
    )
    resp.raise_for_status()
    return resp


def _colors_from_light_data(data: dict, bri: float) -> list[Color]:
    """Extract color(s) from a light resource dict. Returns [] if no color data present."""
    if "gradient" in data and data["gradient"].get("points"):
        colors = []
        for point in data["gradient"]["points"]:
            xy = point["color"]["xy"]
            r, g, b = xy_bri_to_rgb(xy["x"], xy["y"], bri)
            colors.append({"r": r, "g": g, "b": b})
        return colors
    if "color" in data and "xy" in data["color"]:
        xy = data["color"]["xy"]
        r, g, b = xy_bri_to_rgb(xy["x"], xy["y"], bri)
        return [{"r": r, "g": g, "b": b}]
    return []
