"""Phone -> laptop camera streaming over WebRTC, with FFmpeg frame extraction.

Run:  python server.py        then open the printed dashboard URL on the laptop
and scan the QR code with the phone (same Wi-Fi).
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import io
import ipaddress
import json
import math
import statistics
from collections import deque
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from aiohttp import web
from aiortc import RTCPeerConnection, RTCSessionDescription

import hooks
import jev_bridge
from ffmpeg_utils import CaptureSession, find_ffmpeg

BASE = Path(__file__).parent
STATIC = BASE / "static"
CAPTURES = BASE / "captures"


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def make_cert(ip: str) -> tuple[Path, Path]:
    """Self-signed cert (browsers only allow camera access on HTTPS)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    d = BASE / "certs"
    d.mkdir(exist_ok=True)
    crt, key = d / f"cert_{ip}.pem", d / f"key_{ip}.pem"
    if crt.exists() and key.exists():
        return crt, key
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "phone-stream")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(k.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address(ip)),
                 x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(k, hashes.SHA256()))
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(k.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.TraditionalOpenSSL,
                                    serialization.NoEncryption()))
    return crt, key


class State:
    def __init__(self):
        self.pcs: set[RTCPeerConnection] = set()
        self.latest: np.ndarray | None = None
        self.latest_ts = 0.0
        self.session: CaptureSession | None = None
        self.last_result: dict | None = None
        self.recv_frames = 0
        self.fps_window: list[float] = []
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.hook_busy = False
        self.last_hook = 0.0
        self.connected = False

    @property
    def fps(self) -> float:
        now = time.time()
        self.fps_window = [t for t in self.fps_window if now - t < 2]
        return len(self.fps_window) / 2


S = State()
S.acc = deque()          # (t, |accel|) samples for the last ~2 s
S.sens = None            # last raw sensor message from the phone
S.sens_recv = 0.0


def on_sensor_msg(raw: str):
    try:
        d = json.loads(raw)
    except ValueError:
        return
    now = time.time()
    S.sens, S.sens_recv = d, now
    a = (d.get("m") or {}).get("a")
    if a and len(a) == 3 and all(isinstance(v, (int, float)) for v in a):
        S.acc.append((now, math.sqrt(a[0] ** 2 + a[1] ** 2 + a[2] ** 2)))


def sensor_snapshot() -> dict | None:
    """Fused view of the phone sensors; None if the phone isn't sending any."""
    d, now = S.sens, time.time()
    if not d or now - S.sens_recv > 3:
        return None
    o, g, m = d.get("o") or {}, d.get("g") or {}, d.get("m") or {}
    while S.acc and now - S.acc[0][0] > 2:
        S.acc.popleft()
    std = statistics.pstdev(v for _, v in S.acc) if len(S.acc) >= 10 else None
    gps_ok = g.get("lat") is not None and (g.get("acc") or 999) <= 30 and \
        (d.get("t", 0) - g.get("ts", 0)) < 10000
    speed = g.get("speed") if gps_ok else None
    if std is None and speed is None:
        state = "unknown"
    elif speed is not None and speed > 6:
        state = "in vehicle"
    elif (std or 0) > 2.5 or (speed or 0) > 2.2:
        state = "running"
    elif (std or 0) > 0.6 or (speed or 0) > 0.7:
        state = "walking"
    elif (std or 0) > 0.12:
        state = "holding (steady)"
    else:
        state = "still"
    r = m.get("r")
    turn = math.sqrt(sum(v * v for v in r)) if r and all(isinstance(v, (int, float)) for v in r) else None
    return {"heading": o.get("h"), "pitch": o.get("p"), "abs": o.get("abs"), "state": state,
            "accel_std": std, "speed_ms": speed, "turn_dps": turn,
            "lat": g.get("lat"), "lon": g.get("lon"),
            "gps_acc": g.get("acc"), "alt": g.get("alt"), "gps_heading": g.get("hdg")}


def run_hook(img: np.ndarray, ts: float, sensors: dict | None = None):
    try:
        hooks.on_frame(img, ts, sensors)
    except Exception as e:  # never let user code kill the stream
        print("[hook error]", e)
    finally:
        S.hook_busy = False


def ingest(img: np.ndarray, now: float) -> None:
    """One frame from any source (phone over WebRTC, or a camera on this computer)."""
    S.latest, S.latest_ts = img, now
    S.recv_frames += 1
    S.fps_window.append(now)
    if S.session:
        S.session.push(img)
        S.session.log_sensors(sensor_snapshot(), now)
    if not S.hook_busy and now - S.last_hook >= 1 / hooks.ANALYSIS_FPS:
        S.hook_busy, S.last_hook = True, now
        S.pool.submit(run_hook, img, now, sensor_snapshot())


def open_local_camera(index: int) -> cv2.VideoCapture | None:
    """Must run on the main thread: on macOS that's the only place the camera permission prompt can appear."""
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        print(f"[camera] cannot open camera {index} (on macOS: allow camera access for the app running this server, "
              "in System Settings → Privacy & Security → Camera)")
        return None
    print(f"[camera] using local camera {index}: {int(cap.get(3))}x{int(cap.get(4))}")
    return cap


def local_camera(cap: cv2.VideoCapture, loop: asyncio.AbstractEventLoop) -> None:
    """Read a camera attached to this computer (e.g. the MacBook camera). No network involved."""
    fails = 0
    try:
        while True:
            ok, img = cap.read()
            if not ok:
                fails += 1
                if fails > 50:
                    print("[camera] stopped delivering frames")
                    return
                time.sleep(0.05)
                continue
            fails = 0
            loop.call_soon_threadsafe(ingest, img, time.time())
    finally:
        cap.release()


async def consume(track):
    try:
        while True:
            frame = await track.recv()
            ingest(frame.to_ndarray(format="bgr24"), time.time())
    except Exception:
        pass  # track ended
    finally:
        S.latest = None


async def offer(request: web.Request):
    params = await request.json()
    pc = RTCPeerConnection()  # LAN: host candidates are enough, no STUN needed
    S.pcs.add(pc)
    cands = [l.split()[4] + ":" + l.split()[5] for l in params["sdp"].splitlines()
             if l.startswith("a=candidate")]
    print(f"[offer] from {request.remote}; phone candidates: {cands or 'NONE'}")

    @pc.on("track")
    def on_track(track):
        print("[webrtc] got track:", track.kind)
        if track.kind == "video":
            asyncio.ensure_future(consume(track))

    @pc.on("datachannel")
    def on_dc(ch):
        print("[webrtc] datachannel:", ch.label)

        @ch.on("message")
        def on_msg(msg):
            if isinstance(msg, str):
                on_sensor_msg(msg)

    @pc.on("iceconnectionstatechange")
    def on_ice():
        print("[ice]", pc.iceConnectionState)

    @pc.on("connectionstatechange")
    async def on_state():
        print("[webrtc]", pc.connectionState)
        if pc.connectionState in ("failed", "closed", "disconnected"):
            S.pcs.discard(pc)
            await pc.close()

    await pc.setRemoteDescription(RTCSessionDescription(params["sdp"], params["type"]))
    await pc.setLocalDescription(await pc.createAnswer())
    return web.json_response({"sdp": pc.localDescription.sdp, "type": pc.localDescription.type})


async def mjpeg(request: web.Request):
    resp = web.StreamResponse(headers={
        "Content-Type": "multipart/x-mixed-replace; boundary=frame", "Cache-Control": "no-cache"})
    await resp.prepare(request)
    last = 0.0
    try:
        while True:
            img = S.latest
            if img is None or S.latest_ts == last:
                await asyncio.sleep(0.03)
                continue
            last = S.latest_ts
            h, w = img.shape[:2]
            if w > 960:
                img = cv2.resize(img, (960, int(h * 960 / w)), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                await resp.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + buf.tobytes() + b"\r\n")
            await asyncio.sleep(1 / 20)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return resp


async def status(_):
    s = S.session
    return web.json_response({
        "connected": S.latest is not None and time.time() - S.latest_ts < 3, "fps": round(S.fps, 1),
        "resolution": f"{S.latest.shape[1]}x{S.latest.shape[0]}" if S.latest is not None else None,
        "capturing": s is not None,
        "capture": {"dir": str(s.dir), "frames_in": s.frames_in, "dropped": s.dropped,
                    "seconds": round(time.time() - s.started, 1)} if s else None,
        "last_result": S.last_result})


async def capture_start(request: web.Request):
    if S.session:
        return web.json_response({"error": "already capturing"}, status=409)
    if S.latest is None or time.time() - S.latest_ts > 3:
        return web.json_response({"error": "no phone connected"}, status=400)
    o = await request.json()
    h, w = S.latest.shape[:2]
    d = CAPTURES / dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        S.session = CaptureSession(d, w, h, float(o.get("fps", 1)), o.get("format", "jpg"),
                                   bool(o.get("record", True)), bool(o.get("frames", True)))
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"ok": True, "dir": str(d)})


async def capture_stop(_):
    s, S.session = S.session, None
    if not s:
        return web.json_response({"error": "not capturing"}, status=409)
    S.last_result = await asyncio.get_event_loop().run_in_executor(None, s.stop)
    return web.json_response(S.last_result)


async def qr(request: web.Request):
    import qrcode

    img = qrcode.make(request.app["phone_url"], box_size=8, border=2)
    b = io.BytesIO()
    img.save(b, format="PNG")
    return web.Response(body=b.getvalue(), content_type="image/png")


async def sensors(_):
    return web.json_response({"sensors": sensor_snapshot()})


async def info(request: web.Request):
    return web.json_response({"phone_url": request.app["phone_url"]})


async def nav(_):
    return web.json_response(jev_bridge.status())


async def nav_jpg(_):
    b = jev_bridge.get()
    if b is None or b.latest_jpeg is None:
        return web.Response(status=204)
    return web.Response(body=b.latest_jpeg, content_type="image/jpeg", headers={"Cache-Control": "no-store"})


async def on_shutdown(_):
    if S.session:
        S.session.stop()
    await asyncio.gather(*[pc.close() for pc in S.pcs])


def main():
    import sys

    sys.stdout.reconfigure(line_buffering=True)  # show log lines immediately, even when not in a terminal
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--host", default=None, help="IP to advertise (default: auto-detect LAN IP)")
    ap.add_argument("--local-port", type=int, default=8080,
                    help="plain-HTTP dashboard on 127.0.0.1 only (laptop, no certificate warning); 0 = off")
    ap.add_argument("--camera", type=int, default=None,
                    help="use a camera on this computer instead of the phone (0 = built-in camera)")
    ap.add_argument("--no-jev", action="store_true", help="stream only, don't run the Jev navigation council")
    ap.add_argument("--voice", action="store_true", help="speak Jev's alerts on the laptop (HeyGen)")
    a = ap.parse_args()

    if not a.no_jev:
        jev_bridge.start(voice=a.voice)

    find_ffmpeg()  # fail fast with a helpful message
    ip = a.host or lan_ip()
    crt, key = make_cert(ip)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)

    app = web.Application()
    app["phone_url"] = f"https://{ip}:{a.port}/phone"
    app.add_routes([
        web.get("/", lambda r: web.FileResponse(STATIC / "dashboard.html")),
        web.get("/phone", lambda r: web.FileResponse(STATIC / "phone.html")),
        web.post("/offer", offer), web.get("/preview.mjpg", mjpeg),
        web.get("/api/status", status), web.get("/api/info", info), web.get("/api/nav", nav), web.get("/nav.jpg", nav_jpg), web.get("/api/sensors", sensors), web.get("/qr.png", qr),
        web.post("/api/capture/start", capture_start), web.post("/api/capture/stop", capture_stop)])
    app.on_shutdown.append(on_shutdown)

    print(f"\n  Dashboard (laptop): https://localhost:{a.port}/")
    if a.local_port:
        print(f"                      http://localhost:{a.local_port}/   (this computer only, no certificate warning)")
    print(f"  Phone URL:          {app['phone_url']}   (accept the certificate warning)\n")
    cap = open_local_camera(a.camera) if a.camera is not None else None
    asyncio.run(serve(app, a.port, ctx, a.local_port, cap))


async def serve(app: web.Application, port: int, ctx: ssl.SSLContext, local_port: int,
                camera: cv2.VideoCapture | None = None) -> None:
    if camera is not None:
        import threading

        threading.Thread(target=local_camera, args=(camera, asyncio.get_running_loop()), daemon=True).start()
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port, ssl_context=ctx).start()  # phone: HTTPS, needed for the camera
    if local_port:
        await web.TCPSite(runner, "127.0.0.1", local_port).start()  # laptop only
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    main()
