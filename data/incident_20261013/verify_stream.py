"""Verify the recovered database serves the whole surface, and probe /stream.mjpg.

Run against the scratch copy only. The point is to answer two things before any
frontend code is written: does the recovered file work end to end, and what exactly
does the annotated stream send to a browser `<img>` tag.
"""

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8010"
API = f"{BASE}/api/v1"
PW = Path(__file__).parent.joinpath("work/.verify_pw").read_text(encoding="ascii").strip()

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, f"{label}{'  |  ' + detail if detail else ''}"))
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"\n        {detail}" if detail else ""))


def post(path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        API + path,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", **({"authorization": f"Bearer {token}"} if token else {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"raw": raw[:200].decode(errors="replace")}


def get(path: str, token: str | None = None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(API + path, headers={"authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
    try:
        return resp.status, json.loads(raw), raw
    except Exception:
        return resp.status, {}, raw


status, body = post("/auth/login", {"email": "verify@scratch.local", "password": PW})
if status != 200:
    check(False, "login", str(body))
    sys.exit(1)
token = body["access_token"]
check(True, "login returns a token pair", f"keys={sorted(body)}")

def items_of(payload) -> list:
    """Both shapes the API uses: a bare list, or a page with `items`."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("items", [])
    return []


status, cameras, _ = get("/cameras", token)
found = items_of(cameras)
check(status == 200 and len(found) >= 4, "GET /cameras",
      f"{status} {[c.get('camera_id') for c in found]}")

status, slots, _ = get("/slots?camera_id=lot_cam1", token)
bays = items_of(slots)
statuses = sorted({s.get("current_status") for s in bays})
check(status == 200 and len(bays) == 5, "GET /slots?camera_id=lot_cam1",
      f"{status} {len(bays)} bays, statuses present={statuses}")

status, sessions, _ = get("/sessions?limit=5", token)
sitems = items_of(sessions)
check(status == 200 and len(sitems) >= 1, "GET /sessions",
      f"{status} first row keys={sorted(sitems[0]) if sitems else 'none'}")

# --- the stream: what a browser <img> would actually receive ---
try:
    req = urllib.request.Request(f"{API}/cameras/lot_cam1/stream.mjpg")
    with urllib.request.urlopen(req, timeout=10) as resp:
        check(False, "stream.mjpg without a credential should be refused", f"got {resp.status}")
except urllib.error.HTTPError as exc:
    check(exc.code in (401, 403), "stream.mjpg refuses an anonymous viewer", f"HTTP {exc.code}")

try:
    url = f"{API}/cameras/lot_cam1/stream.mjpg?token={token}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=25) as resp:
        ctype = resp.headers.get("content-type", "")
        check("multipart/x-mixed-replace" in ctype, "stream.mjpg content type", ctype)
        buf = resp.read(200_000)
except Exception as exc:
    buf = b""
    check(False, "stream.mjpg opened", f"{type(exc).__name__}: {exc}")

if buf:
    parts = buf.split(b"--smartparkinglot_cam1")
    images = [p for p in parts if b"\xff\xd8\xff" in p]
    check(len(images) >= 1, "stream carries real JPEG frames",
          f"{len(buf)} bytes read, {len(parts)} multipart sections, {len(images)} with a JPEG header")
    check(b"Content-Type: image/jpeg" in buf, "each part declares image/jpeg")
    check(b"\xff\xd9" in buf, "frames are complete JPEGs (EOI marker present)")
    sizes = sorted({len(p) for p in images})
    check(all(s > 8000 for s in sizes), "frames are large enough to be annotated video",
          f"distinct part sizes={sizes[:6]}")

# --- the JSON half, which is the alternative to burning boxes into the picture ---
status, overlay, _ = get("/cameras/lot_cam1/overlay", token)
check(status == 200, "GET /cameras/lot_cam1/overlay", f"{status} keys={sorted(overlay)}")
if status == 200:
    check("tracks" in overlay, "overlay carries per-frame tracks",
          f"tracks={len(overlay.get('tracks', []))} slots={len(overlay.get('slots', []))}")

# --- the WebSocket the dashboard already uses, plus a ticket ---
tstatus, tbody = post("/auth/ws-ticket", {}, token)
check(tstatus == 200 and "ticket" in tbody, "POST /auth/ws-ticket", f"{tstatus} keys={sorted(tbody)}")

if "ticket" in tbody:
    import asyncio

    import websockets

    async def grab() -> tuple[int, list]:
        got: list = []
        async with websockets.connect(f"ws://127.0.0.1:8010/ws/live?ticket={tbody['ticket']}") as ws:
            for _ in range(4):
                try:
                    got.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=6)))
                except (asyncio.TimeoutError, TimeoutError):
                    break
        return len(got), got

    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    n, msgs = asyncio.run(grab())
    check(n >= 1, "WS /ws/live accepts the one-time ticket", f"{n} message(s), kinds={sorted({m.get('kind', m.get('type')) for m in msgs})}")

print(f"\n{sum(1 for r in results if r[0])}/{len(results)} checks passed")
sys.exit(0 if all(r[0] for r in results) else 1)
