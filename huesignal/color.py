"""Pure color conversion utilities - no I/O, no side effects."""

from typing import TypedDict


class Color(TypedDict):
    r: int
    g: int
    b: int


def BLACK() -> Color:
    """Return a new black color dict. Use a factory to prevent accidental mutation."""
    return {"r": 0, "g": 0, "b": 0}


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, v))


def _srgb_gamma(linear: float) -> float:
    if linear <= 0.0031308:
        return 12.92 * linear
    return 1.055 * (linear ** (1.0 / 2.4)) - 0.055


def xy_bri_to_rgb(x: float, y: float, bri: float = 1.0) -> tuple[int, int, int]:
    """Convert Hue CIE xy + brightness to sRGB (r, g, b) in 0-255 range."""
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


def rgb_preview(colors: list[Color], limit: int = 4) -> str:
    """Return a compact human-readable string of up to *limit* colors."""
    return ", ".join(f"rgb({c['r']},{c['g']},{c['b']})" for c in colors[:limit])
