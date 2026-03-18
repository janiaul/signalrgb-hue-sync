"""Configuration loading, validation, and path constants."""

from __future__ import annotations

import configparser
import logging
import logging.handlers
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("huesignal")

BASE_DIR = Path(__file__).resolve().parent.parent
CERTS_DIR = BASE_DIR / "certs"
LOGS_DIR = BASE_DIR / "logs"
EFFECTS_DIR = BASE_DIR / "effects"
ASSETS_DIR = BASE_DIR / "assets"

HUESIGNAL_HTML = EFFECTS_DIR / "HueSignal.html"
CERT_FILE = CERTS_DIR / "huesignal.pem"
KEY_FILE = CERTS_DIR / "huesignal-key.pem"
CONFIG_FILE = BASE_DIR / "config.ini"
FLASK_PORT = 5123
WSS_URL = f"wss://127.0.0.1:{FLASK_PORT}/ws"

SIGNALRGB_EFFECTS_DIR = Path.home() / "Documents" / "WhirlwindFX" / "Effects"


class ConfigError(Exception):
    """Raised when the configuration file is missing or invalid."""


@dataclass
class AppConfig:
    bridge_ip: str
    application_key: str
    entertainment_zone_name: str
    entertainment_id: str = ""
    bridge_cert_fingerprint: str = ""
    logging_enabled: bool = False
    log_level: str = "INFO"
    tray_icon: bool = True

    # Populated after zone resolution, not from file
    resolved_light_ids: list[str] = field(default_factory=list)

    @staticmethod
    def load(path: Path = CONFIG_FILE) -> "AppConfig":
        """Load and validate config.ini; raises ConfigError on any problem."""
        if not path.exists():
            raise ConfigError(
                f"Configuration file not found: {path}\n"
                "Please create config.ini based on config.ini.example."
            )

        parser = configparser.ConfigParser()
        parser.read(path, encoding="utf-8")

        missing: list[str] = []
        for section, key in [
            ("hue", "bridge_ip"),
            ("hue", "application_key"),
            ("hue", "entertainment_zone_name"),
        ]:
            if not parser.get(section, key, fallback="").strip():
                missing.append(f"[{section}] {key}")

        if missing:
            raise ConfigError(
                "The following required config values are missing or empty:\n"
                + "\n".join(f"  • {m}" for m in missing)
            )

        return AppConfig(
            bridge_ip=parser["hue"]["bridge_ip"].strip(),
            application_key=parser["hue"]["application_key"].strip(),
            entertainment_zone_name=parser["hue"]["entertainment_zone_name"].strip(),
            entertainment_id=parser["hue"].get("entertainment_id", "").strip(),
            bridge_cert_fingerprint=parser["hue"]
            .get("bridge_cert_fingerprint", "")
            .strip(),
            logging_enabled=parser["general"].getboolean("logging", fallback=False),
            log_level=parser["general"].get("log_level", "INFO").strip().upper(),
            tray_icon=parser["general"].getboolean("tray_icon", fallback=True),
        )

    def save_entertainment_id(self, path: Path = CONFIG_FILE) -> None:
        """Persist the resolved entertainment_id back to config.ini to avoid re-resolving."""
        parser = configparser.ConfigParser()
        parser.read(path, encoding="utf-8")
        parser["hue"]["entertainment_id"] = self.entertainment_id
        write_config_atomic(parser, path)

    def save_bridge_fingerprint(self, path: Path = CONFIG_FILE) -> None:
        """Persist the trusted bridge TLS certificate fingerprint to config.ini."""
        parser = configparser.ConfigParser()
        parser.read(path, encoding="utf-8")
        parser["hue"]["bridge_cert_fingerprint"] = self.bridge_cert_fingerprint
        write_config_atomic(parser, path)


def write_config_atomic(parser: configparser.ConfigParser, path: Path) -> None:
    """Write *parser* to *path* safely via a sibling temp file, then atomically rename.

    Protects against config corruption if the process is killed mid-write.
    """
    tmp = path.parent / (path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        parser.write(fh)
    tmp.replace(path)


def setup_logging(cfg: AppConfig) -> None:
    """Configure the root huesignal logger based on AppConfig."""
    level = getattr(logging, cfg.log_level, logging.INFO)
    logger.setLevel(level)
    logger.handlers.clear()
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    stream.setLevel(level)
    logger.addHandler(stream)

    if cfg.logging_enabled:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOGS_DIR / "huesignal.log"
        fh = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=1,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        fh.setLevel(logging.DEBUG)
        logger.addHandler(fh)
        logger.info("[config] File logging enabled -> %s", log_file)
