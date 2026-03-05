# HueSync for SignalRGB

A lightweight bridge that mirrors your Philips Hue lighting effects into SignalRGB in real time. Whatever color or gradient your Hue lights are showing, your SignalRGB setup will follow.

---

## How it works

HueSync connects to your Hue bridge's event stream and listens for light changes in a configured entertainment zone. When colors change, it converts them from Hue's CIE xy color space to RGB and pushes them over a local WebSocket to a SignalRGB effect (an HTML canvas file). SignalRGB renders the colors as a gradient across your devices.

---

## Requirements

- Windows 10+
- Python 3.10+
- A Philips Hue Bridge (v2) with at least one entertainment zone configured
- [SignalRGB](https://signalrgb.com/) installed and running
- [mkcert](https://github.com/FiloSottile/mkcert) — for generating a trusted local SSL cert

---

## Setup

### 1. Install dependencies

```bash
pip install flask flask-sock requests urllib3
```

### 2. Generate a local SSL certificate

SignalRGB requires HTTPS, so you'll need a locally-trusted cert:

```bash
mkcert 127.0.0.1 localhost
```

This produces `localhost+1.pem` and `localhost+1-key.pem`. Place both in the same folder as `hue_sync.py`.

### 3. Create a `config.ini`

```ini
[general]
logging = false

[hue]
bridge_ip = 192.168.x.x
application_key = your-hue-app-key
entertainment_zone_name = Your Zone Name
entertainment_id =
```

You can leave `entertainment_id` blank — it'll be resolved automatically on first run.

To get your `application_key`, follow [Philips Hue's API getting started guide](https://developers.meethue.com/develop/get-started-2/).

### 4. Run it

```bash
python hue_sync.py
```

On first run it will:
- Resolve your entertainment zone and lights
- Back up and patch SignalRGB's `cacert.pem` so it trusts your local cert (requires SignalRGB to already be running)
- Write the `HueSync.html` effect file and symlink it into SignalRGB's effects folder

### 5. Load the effect in SignalRGB

Open SignalRGB, go to **Library**, and load **Hue Sync**. Done.

---

## Notes

- The server runs at `wss://127.0.0.1:5123/ws` — everything stays local, nothing goes to the cloud.
- HueSync handles Windows sleep/wake events and reconnects automatically after resume.
- If SignalRGB isn't running when you start HueSync, the cacert patch is skipped — just restart HueSync once SignalRGB is open.
- Gradient lights (like the Hue Play gradient strip) are fully supported and display as a multi-stop gradient in SignalRGB.