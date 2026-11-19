"""Stand-in for the phone: streams a video file to server.py over WebRTC, with phone-style sensor messages.

    python fake_phone.py ../videos/fpv_01.mp4                 # https://localhost:8443
    python fake_phone.py clip.mp4 --server https://192.168.1.5:8443 --still-every 12

Sensors mimic phone.html's data channel: {"t", "o": {h, p, abs}, "m": {a: [x,y,z], r: [a,b,g]}}.
Acceleration shake is walking-level, with optional short "standing still" pauses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import ssl
import time

import aiohttp
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer


async def main(video: str, server: str, seconds: float | None, still_every: float) -> None:
    pc = RTCPeerConnection()
    player = MediaPlayer(video)
    pc.addTrack(player.video)
    dc = pc.createDataChannel("sensors", ordered=False, maxRetransmits=0)
    done = asyncio.Event()
    player.video.on("ended", done.set)

    @pc.on("connectionstatechange")
    async def on_state() -> None:
        print("[fake phone]", pc.connectionState)
        if pc.connectionState in ("failed", "closed"):
            done.set()

    await pc.setLocalDescription(await pc.createOffer())
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # server.py uses a self-signed certificate
    async with aiohttp.ClientSession() as http:
        async with http.post(f"{server}/offer", json={"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}, ssl=ctx) as r:
            ans = await r.json()
    await pc.setRemoteDescription(RTCSessionDescription(ans["sdp"], ans["type"]))

    async def sensors() -> None:
        t0 = time.time()
        while not done.is_set():
            t = time.time() - t0
            still = still_every > 0 and (t % still_every) > still_every - 3  # 3 s pause each cycle
            shake = 0.05 if still else 1.3
            a = [random.gauss(0, shake), 9.81 + random.gauss(0, shake), random.gauss(0, shake)]
            r = [random.gauss(0, 3), random.gauss(0, 3), random.gauss(0, 3)]
            msg = {"t": int(time.time() * 1000), "o": {"h": (90 + 20 * math.sin(t / 5)) % 360, "p": -10.0, "abs": True},
                   "m": {"a": a, "r": r}}
            if dc.readyState == "open":
                dc.send(json.dumps(msg))
            await asyncio.sleep(0.1)

    task = asyncio.create_task(sensors())
    try:
        await asyncio.wait_for(done.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
    task.cancel()
    await pc.close()
    print("[fake phone] finished")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--server", default="https://localhost:8443")
    ap.add_argument("--seconds", type=float, default=None, help="stop after this long (default: end of video)")
    ap.add_argument("--still-every", type=float, default=0, help="pause (stand still) for 3 s every N seconds")
    a = ap.parse_args()
    asyncio.run(main(a.video, a.server, a.seconds, a.still_every))
