"""Philips Hue bridge I/O: zone resolution, light queries, and SSE streaming."""

from __future__ import annotations

import json
import logging
import time
import threading
from typing import Callable, Optional

import requests
import urllib3

from .color import Color, BLACK, xy_bri_to_rgb, rgb_preview
from .config import AppConfig

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger("huesync")

# How long to wait between SSE reconnect attempts (doubles each failure, caps at 60s)
_BACKOFF_INITIAL = 3
_BACKOFF_MAX = 60


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

    # Zone → entertainment service IDs
    url = f"{base}/clip/v2/resource/entertainment_configuration/{cfg.entertainment_id}"
    config = _get(cfg, url).json().get("data", [{}])[0]

    ent_rids: set[str] = set()
    for channel in config.get("channels", []):
        for member in channel.get("members", []):
            svc = member.get("service", {})
            if svc.get("rtype") == "entertainment":
                ent_rids.add(svc["rid"])

    # Entertainment → device IDs
    device_rids: set[str] = set()
    for ent_rid in ent_rids:
        owner = (
            requests.get(
                f"{base}/clip/v2/resource/entertainment/{ent_rid}",
                headers=headers,
                verify=False,
                timeout=5,
            )
            .json()
            .get("data", [{}])[0]
            .get("owner", {})
        )
        if owner.get("rtype") == "device":
            device_rids.add(owner["rid"])

    # Device → light IDs
    light_rids: list[str] = []
    for device_rid in device_rids:
        services = (
            requests.get(
                f"{base}/clip/v2/resource/device/{device_rid}",
                headers=headers,
                verify=False,
                timeout=5,
            )
            .json()
            .get("data", [{}])[0]
            .get("services", [])
        )
        for svc in services:
            if svc.get("rtype") == "light":
                light_rids.append(svc["rid"])

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
    colors: list[Color] = []
    for light_id in cfg.resolved_light_ids:
        data = (
            _get(cfg, f"https://{cfg.bridge_ip}/clip/v2/resource/light/{light_id}")
            .json()
            .get("data", [{}])[0]
        )
        if not data.get("on", {}).get("on", False):
            colors.append(BLACK)
            continue
        bri = data.get("dimming", {}).get("brightness", 100.0) / 100.0
        colors.extend(_colors_from_light_data(data, bri))

    return colors or [BLACK]


# ---------------------------------------------------------------------------
# SSE event parsing
# ---------------------------------------------------------------------------


def extract_colors_from_event(
    data: list,
    watched_ids: set[str],
    cfg: AppConfig,
) -> list[Color]:
    """Parse SSE event payload into a list of Colors for watched lights."""
    colors: list[Color] = []

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
                # Toggle-on or brightness-only event with no color data — fetch from bridge
                reason = (
                    "toggle-on"
                    if ("on" in on_state and on_state["on"])
                    else "brightness change"
                )
                logger.info(
                    "[hue] %s with no color data, fetching state for %s",
                    reason,
                    light_id,
                )
                try:
                    colors.extend(fetch_light_colors(cfg, light_id))
                except Exception as exc:
                    logger.warning(
                        "[hue] Could not fetch light state for %s: %s", light_id, exc
                    )

    return colors


# ---------------------------------------------------------------------------
# SSE stream (runs in its own thread)
# ---------------------------------------------------------------------------


class HueStreamThread(threading.Thread):
    """Background thread that subscribes to the Hue SSE stream and calls
    *on_colors* whenever new color data arrives.

    Uses requests for SSE streaming with an infinite read timeout. The Hue
    bridge sends no keepalives — it only writes on light state changes — so
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

    def interrupt(self) -> None:
        """Signal the stream to reconnect at the next bridge event."""
        self._interrupt.set()

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
                with requests.get(
                    url,
                    headers=headers,
                    stream=True,
                    verify=False,
                    timeout=(10, None),
                ) as resp:
                    resp.raise_for_status()
                    backoff = _BACKOFF_INITIAL
                    self._on_status("connected")
                    logger.info("[hue] Connected. Listening for events ...")
                    self._on_reseed()

                    buffer: list[str] = []
                    for raw_line in resp.iter_lines(decode_unicode=True):
                        if self._interrupt.is_set():
                            logger.info("[hue] Stream interrupted — reconnecting.")
                            break
                        if raw_line.startswith("data:"):
                            buffer.append(raw_line[5:].strip())
                        elif raw_line == "" and buffer:
                            self._dispatch(" ".join(buffer), cfg)
                            buffer.clear()

            except requests.RequestException as exc:
                logger.error("[hue] Stream error: %s", exc)
            except Exception:
                logger.exception("[hue] Unhandled exception in stream thread")

            if self._interrupt.is_set():
                backoff = _BACKOFF_INITIAL
                continue

            self._on_status("reconnecting")
            logger.info("[hue] Reconnecting in %ds ...", backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX)

    def _dispatch(self, payload: str, cfg: AppConfig) -> None:
        if self._interrupt.is_set():
            return  # restart in progress — discard stale events
        watched = set(cfg.resolved_light_ids)
        try:
            events = json.loads(payload)
            colors = extract_colors_from_event(events, watched, cfg)
            if colors:
                logger.info("[hue] Push → %s", rgb_preview(colors))
                self._on_colors(colors)
        except json.JSONDecodeError as exc:
            logger.warning("[hue] Malformed SSE payload, skipping: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _headers(cfg: AppConfig) -> dict[str, str]:
    return {"hue-application-key": cfg.application_key}


def _get(cfg: AppConfig, url: str) -> requests.Response:
    resp = requests.get(url, headers=_headers(cfg), verify=False, timeout=5)
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
