"""Seed the demo lot so the dashboard has something to show.

The API reads cameras and slots from the database, not from the config files,
so a fresh deploy shows all zeros until rows exist. This registers `lot_cam1`
and the five bays from its calibration file (configs/slots/lot_a_cam1.json)
through the same admin endpoints the browser uses. The running pipeline already
watches lot_cam1, so once these rows exist its FSM commits live FREE/OCCUPIED
statuses onto them and the layout starts moving on its own.

Idempotent: a camera or slot that already exists is skipped, so this is safe to
re-run after a reboot.

    python scripts/seed_demo.py --email you@example.com --password 'Admin@123456'
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

BASE = "http://localhost:8000/api/v1"

CAMERA = {
    "camera_id": "lot_cam1",
    "name": "Lot camera 1",
    "role": "LOT",
    "source": "data/samples/lot_shot5.mp4",
    "slots_file": "configs/slots/lot_a_cam1.json",
}

FRAME_SIZE = {"width": 478, "height": 850}

# Mirrors configs/slots/lot_a_cam1.json (normalised_0_1). Stored verbatim, the
# same way SlotRepository.sync keeps the calibration geometry.
SLOTS = [
    {"code": "A1", "type": "REGULAR",
     "polygon": [[0.293, 0.445], [0.722, 0.447], [0.900, 0.824], [0.126, 0.824]]},
    {"code": "A2", "type": "REGULAR",
     "polygon": [[0.722, 0.447], [1.000, 0.461], [1.000, 0.824], [0.900, 0.824]]},
    {"code": "B1", "type": "DISABLED",
     "polygon": [[0.000, 0.231], [0.163, 0.231], [0.184, 0.320], [0.000, 0.320]]},
    {"code": "B2", "type": "REGULAR",
     "polygon": [[0.324, 0.231], [0.582, 0.231], [0.603, 0.336], [0.310, 0.336]]},
    {"code": "B3", "type": "RESERVED",
     "polygon": [[0.732, 0.235], [0.979, 0.235], [0.987, 0.336], [0.724, 0.336]]},
]


def call(method: str, path: str, token: str | None = None, body: dict | None = None):
    """One JSON request. Returns (status, parsed_or_text)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        status = exc.code
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", required=True)
    args = ap.parse_args()

    status, resp = call(
        "POST", "/auth/login",
        body={"email": args.email, "password": args.password},
    )
    if status != 200 or not isinstance(resp, dict) or "access_token" not in resp:
        print(f"login failed ({status}): {resp}", file=sys.stderr)
        return 1
    token = resp["access_token"]
    print("logged in")

    status, resp = call("POST", "/cameras", token=token, body=CAMERA)
    if status == 201:
        print(f"registered camera {CAMERA['camera_id']}")
    elif status == 409:
        print(f"camera {CAMERA['camera_id']} already registered - skipping")
    else:
        print(f"camera register failed ({status}): {resp}", file=sys.stderr)
        return 1

    for slot in SLOTS:
        status, resp = call("POST", "/slots", token=token, body={
            "camera_id": CAMERA["camera_id"],
            "slot_code": slot["code"],
            "slot_type": slot["type"],
            "polygon": slot["polygon"],
            "frame_size": FRAME_SIZE,
        })
        if status == 201:
            print(f"  + {slot['code']} ({slot['type']})")
        elif status == 409:
            print(f"  = {slot['code']} already exists - skipping")
        else:
            print(f"  ! {slot['code']} failed ({status}): {resp}", file=sys.stderr)

    status, resp = call("GET", f"/slots?camera_id={CAMERA['camera_id']}", token=token)
    count = len(resp) if isinstance(resp, list) else "?"
    print(f"done - {count} slot rows now in the database for {CAMERA['camera_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
