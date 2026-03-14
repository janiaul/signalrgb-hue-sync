"""Flask/WebSocket server - completely decoupled from Hue logic.

The server owns no Hue state. It receives color updates via push_colors()
and broadcasts them to all connected WebSocket clients.
"""

from __future__ import annotations

import json
import logging
import ssl
import threading

from flask import Flask
from flask_sock import Sock

from .color import Color, BLACK
from .config import AppConfig, CERT_FILE, KEY_FILE, FLASK_PORT

logger = logging.getLogger("huesignal")


class ColorServer:
    """Manages the Flask/WSS server and connected WebSocket clients."""

    def __init__(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._app = Flask(__name__)
        self._sock = Sock(self._app)
        self._clients: set = set()
        self._clients_lock = threading.Lock()
        self._latest_colors: list[Color] = [BLACK]
        self._colors_lock = threading.RLock()

        self._sock.route("/ws")(self._ws_handler)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def push_colors(self, colors: list[Color]) -> None:
        """Update the latest colors and broadcast to all connected clients."""
        with self._colors_lock:
            self._latest_colors = colors
        self._broadcast(json.dumps(colors, separators=(",", ":")))

    @property
    def latest_colors(self) -> list[Color]:
        with self._colors_lock:
            return list(self._latest_colors)

    @property
    def client_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)

    def run(self) -> None:
        """Start the Flask server (blocking). Call from the main thread or a dedicated thread."""
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            ssl_ctx.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
        except (FileNotFoundError, ssl.SSLError) as exc:
            raise RuntimeError(
                f"Failed to load SSL certificate: {exc}\n"
                f"Expected files:\n  {CERT_FILE}\n  {KEY_FILE}\n"
                "Run: mkcert 127.0.0.1 localhost"
            ) from exc

        logger.info("[server] Listening on wss://127.0.0.1:%d/ws", FLASK_PORT)
        self._app.run(host="127.0.0.1", port=FLASK_PORT, ssl_context=ssl_ctx)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ws_handler(self, ws) -> None:
        with self._clients_lock:
            self._clients.add(ws)
        logger.info("[ws] Client connected (%d total)", self.client_count)
        try:
            # Immediately seed the new client with the current color
            with self._colors_lock:
                ws.send(json.dumps(self._latest_colors, separators=(",", ":")))
            while True:
                ws.receive(timeout=None)
        except Exception:
            pass
        finally:
            with self._clients_lock:
                self._clients.discard(ws)
            logger.info("[ws] Client disconnected (%d total)", self.client_count)

    def _broadcast(self, msg: str) -> None:
        with self._clients_lock:
            snapshot = set(self._clients)
        dead: set = set()
        for ws in snapshot:
            try:
                ws.send(msg)
            except Exception:
                dead.add(ws)
        if dead:
            with self._clients_lock:
                self._clients.difference_update(dead)
