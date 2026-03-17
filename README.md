<div align="center">
  <img src="assets/logo.png" width="80" alt="HueSignal">

  # HueSignal

  Mirror your Philips Hue lighting into SignalRGB in real time.
</div>

---

HueSignal listens to your Hue bridge's event stream, converts light colors from CIE xy to RGB, and pushes them over a local WebSocket to a SignalRGB HTML effect — rendering your lights as a live gradient across your devices.

## Contents

- [Requirements](#requirements)
- [Setup](#setup)
- [System tray](#system-tray)
- [Notes](#notes)

## Requirements

- Windows 10+
- Python 3.10+
- Philips Hue Bridge v2 with an entertainment zone configured
- [SignalRGB](https://signalrgb.com/) installed and running
- [mkcert](https://github.com/FiloSottile/mkcert) for generating a trusted local SSL certificate

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Generate a local SSL certificate

SignalRGB requires HTTPS:

```bash
mkcert 127.0.0.1 localhost
```

Place the generated `localhost+1.pem` and `localhost+1-key.pem` in the `certs/` folder.

### 3. Configure

Copy [`config.example.ini`](config.example.ini) to `config.ini` — every option is documented inline. The minimum required fields are:

| Field | Description |
|---|---|
| `bridge_ip` | Local IP of your Hue bridge |
| `application_key` | From the [Hue API getting started guide](https://developers.meethue.com/develop/get-started-2/) |
| `entertainment_zone_name` | Exact name of the zone to mirror (case-insensitive) |

`entertainment_id` and `bridge_cert_fingerprint` are resolved and cached automatically on first run.

### 4. Run

```bash
pythonw -m huesignal   # no console window (normal use)
python  -m huesignal   # with console (troubleshooting)
```

On first run HueSignal will resolve zone and light IDs, patch SignalRGB's certificate store, and write the `HueSignal.html` effect file into SignalRGB's effects folder.

### 5. Load the effect in SignalRGB

Open SignalRGB → **Library** → select **Hue Signal**.

## System tray

The tray icon shows connection status via a colored dot:

| Dot | Status |
|---|---|
| Grey | Starting |
| Amber | Connecting to bridge |
| Green | Connected — stream live |
| Red | Reconnecting |

Right-click for: color preview (live RGB per light), pause/resume sync, settings (logging and tray icon toggles), restart stream, open log, and exit.

To run headless without a tray icon, set `tray_icon = false` in `config.ini` and use Ctrl+C in the console to stop.

## Notes

- Everything is local — the WebSocket runs at `wss://127.0.0.1:5123/ws`, nothing reaches the cloud.
- Sleep/wake is handled automatically; the stream reconnects after resume.
- If SignalRGB isn't running on first launch, the cert patch is skipped — restart HueSignal once SignalRGB is open.
- Gradient lights (e.g. Hue Play gradient lightstrip) are fully supported and render as a multi-stop gradient.
- Only one entertainment zone is supported by design; mixing zones produces unpredictable colors.
