#!/usr/bin/env python3
"""Download pinned public Leaflet assets once for offline browser tests.

Run explicitly: python tools/browser_assets.py
No route data is read or sent. The test runner itself never downloads assets.
"""

import base64
import hashlib
from pathlib import Path
from urllib.request import urlopen

BASE_URL = "https://unpkg.com/leaflet@1.9.4/dist/"
CACHE = Path(__file__).resolve().parents[1] / "cache" / "browser"
ASSETS = {
    "leaflet.js": "20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=",
    "leaflet.css": "p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=",
}


def verify(name, body):
    if hashlib.sha256(body).digest() != base64.b64decode(ASSETS[name]):
        raise ValueError(f"checksum mismatch for {name}")
    return body


def read_assets():
    try:
        return {BASE_URL + name: verify(name, (CACHE / name).read_bytes())
                for name in ASSETS}
    except (OSError, ValueError) as exc:
        raise SystemExit(f"{exc}\nRun python tools/browser_assets.py once first.") from exc


def main():
    CACHE.mkdir(parents=True, exist_ok=True)
    for name in ASSETS:
        with urlopen(BASE_URL + name, timeout=30) as response:
            body = verify(name, response.read())
        target = CACHE / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(body)
        temporary.replace(target)
        print(f"Verified Leaflet 1.9.4: {target}")


if __name__ == "__main__":
    main()
