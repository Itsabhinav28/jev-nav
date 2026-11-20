"""Connects the live stream to ORB-SLAM3 (monocular), which runs as `slam_server` inside WSL.

Frames are sent over TCP (localhost:5555, WSL2 forwards it) as JPEGs; the reply per frame is the camera pose and the
map points tracked in that frame. The bridge accumulates those into a point cloud and a camera path that the dashboard's
Map panel draws (GET /api/slam). If a frame arrives while the previous one is still being tracked it is dropped, so the
stream never lags. Scale is arbitrary (monocular) and the intrinsics are guessed, so treat the map as a sketch.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import threading
import time
from collections import deque

import cv2
import numpy as np

HOST = os.environ.get("SLAM_HOST", "127.0.0.1")
PORT = int(os.environ.get("SLAM_PORT", "5555"))
AUTOSTART = os.environ.get("SLAM_AUTOSTART", "1") != "0"   # "0" (Docker): never spawn WSL, just connect
PROC_WIDTH = 640          # frames are resized to this width (height keeps the aspect ratio, rounded to even)
SLAM_FPS = 15             # frames per second sent to SLAM
MAX_POINTS = 12000        # cap on the accumulated cloud sent to the dashboard
PATH_LEN = 600
LOST_RESET_S = 2.0        # tracking lost this long -> reset the map
STATES = {-1: "waiting", 0: "no images", 1: "initialising", 2: "tracking", 3: "lost"}

WSL_DISTRO = os.environ.get("SLAM_WSL_DISTRO", "Ubuntu-22.04")
SLAM_BIN = os.environ.get("SLAM_BIN", "/mnt/d/ORB_SLAM3/Examples/Monocular/slam_server")
VOCAB = os.environ.get("SLAM_VOCAB", "/mnt/d/ORB_SLAM3/Vocabulary/ORBvoc.txt")


def start_wsl_server() -> subprocess.Popen | None:
    """Launch slam_server in WSL (no-op if something already listens on HOST:PORT).

    With SLAM_AUTOSTART=0 nothing is spawned: the service is started externally (Docker entrypoint) and the bridge
    just connects to SLAM_HOST:SLAM_PORT, per frame, so a late-starting service is fine.
    """
    try:
        socket.create_connection((HOST, PORT), timeout=0.5).close()
        print(f"[slam] slam_server already listening on {HOST}:{PORT}")
        return None
    except OSError:
        pass
    if not AUTOSTART:
        print(f"[slam] SLAM_AUTOSTART=0: not spawning; expecting slam_server at {HOST}:{PORT} (will connect when it is up)")
        return None
    cmd = ["wsl", "-d", WSL_DISTRO, "-u", "root", "--", SLAM_BIN, VOCAB, str(PORT)]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        try:
            socket.create_connection((HOST, PORT), timeout=0.5).close()
            print(f"[slam] slam_server started in WSL on port {PORT}")
            return proc
        except OSError:
            time.sleep(0.5)
    print("[slam] slam_server did not come up; SLAM disabled")
    return proc


class SlamBridge:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.busy = False
        self.last_sent = 0.0
        self.sock: socket.socket | None = None
        self.rfile = None
        self.size: tuple[int, int] | None = None
        self.points: dict[int, tuple[float, float, float]] = {}
        self.path: deque[tuple[float, float, float]] = deque(maxlen=PATH_LEN)
        self.pose: list[float] | None = None
        self.state = "waiting"
        self.error: str | None = None
        self.frames = 0
        self.latency = 0.0
        self.version = 0
        self.lost_since: float | None = None
        self.resets = 0
        self.latest_jpeg: bytes | None = None

    def _connect(self, size: tuple[int, int]) -> None:
        self._close()
        s = socket.create_connection((HOST, PORT), timeout=5)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.sendall(struct.pack("<II", *size))
        s.settimeout(120)  # loading the vocabulary takes a while
        rf = s.makefile("rb")
        if rf.readline().strip() != b"READY":
            raise ConnectionError("slam_server did not say READY")
        s.settimeout(10)
        self.sock, self.rfile, self.size = s, rf, size
        with self.lock:  # a new session starts a new map
            self.points.clear(), self.path.clear()
            self.pose, self.version = None, self.version + 1

    def _close(self) -> None:
        for c in (self.rfile, self.sock):
            try:
                c and c.close()
            except OSError:
                pass
        self.sock = self.rfile = self.size = None

    def submit(self, img: np.ndarray, now: float) -> None:
        """Called from the frame loop; cheap when SLAM is busy or the rate limit isn't up."""
        if self.busy or now - self.last_sent < 1 / SLAM_FPS:
            return
        self.busy, self.last_sent = True, now
        threading.Thread(target=self._process, args=(img, now), daemon=True, name="slam").start()

    def _process(self, img: np.ndarray, now: float) -> None:
        try:
            h, w = img.shape[:2]
            size = (PROC_WIDTH, max(2, int(round(PROC_WIDTH * h / w / 2)) * 2))
            frame = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
            if self.sock is None or self.size != size:
                self.state = "starting SLAM"
                self._connect(size)
            ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            t0 = time.time()
            self.sock.sendall(struct.pack("<Id", len(jpg), now) + jpg.tobytes())
            reply = json.loads(self.rfile.readline())
            self.latency = time.time() - t0
            self._apply(reply)
            self._annotate(frame, reply)
            self._auto_reset(reply["state"])
            self.error = None
        except Exception as e:  # server missing, reset, bad reply: reconnect on the next frame
            self.error = f"{type(e).__name__}: {e}"
            self.state = "disconnected"
            self._close()
        finally:
            self.busy = False

    def _annotate(self, frame: np.ndarray, r: dict) -> None:
        """The frame SLAM just saw, with a green square on every tracked feature and a status bar (like ORB-SLAM3's viewer)."""
        vis = frame.copy()
        matches = 0
        for p in r["pts"]:
            if len(p) >= 6:
                u, v = int(p[4]), int(p[5])
                cv2.rectangle(vis, (u - 4, v - 4), (u + 4, v + 4), (0, 220, 0), 1)
                matches += 1
        bar = f"{STATES.get(r['state'], '?').upper()} | Map points: {len(self.points)} | Matches: {matches}"
        cv2.rectangle(vis, (0, vis.shape[0] - 22), (vis.shape[1], vis.shape[0]), (255, 255, 255), -1)
        cv2.putText(vis, bar, (6, vis.shape[0] - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)
        self.latest_jpeg = cv2.imencode(".jpg", vis, [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()

    def _auto_reset(self, state: int) -> None:
        """Tracking lost for LOST_RESET_S in a row: reset SLAM and clear the map so the next walk starts clean."""
        if state != 3:
            self.lost_since = None
            return
        self.lost_since = self.lost_since or time.time()
        if time.time() - self.lost_since < LOST_RESET_S:
            return
        self.sock.sendall(struct.pack("<I", 0xFFFFFFFF))
        json.loads(self.rfile.readline())
        self.lost_since = None
        with self.lock:
            self.points.clear(), self.path.clear()
            self.pose, self.state = None, "reset, re-initialising"
            self.resets += 1
            self.version += 1
        print("[slam] tracking lost; map reset")

    def _apply(self, r: dict) -> None:
        with self.lock:
            self.state = STATES.get(r["state"], str(r["state"]))
            self.frames += 1
            if r["pose"]:
                self.pose = r["pose"]
                self.path.append(tuple(r["pose"][:3]))
            for p in r["pts"]:
                self.points[int(p[0])] = (p[1], p[2], p[3])
            if len(self.points) > MAX_POINTS:  # drop the oldest ids first
                for k in sorted(self.points)[: len(self.points) - MAX_POINTS]:
                    del self.points[k]
            self.version += 1

    def snapshot(self) -> dict:
        with self.lock:
            pts = np.array(list(self.points.values()), dtype=np.float32).reshape(-1, 3)
            return {
                "state": self.state, "error": self.error, "frames": self.frames, "resets": self.resets, "latency": round(self.latency, 3),
                "pose": self.pose, "n_points": len(pts), "version": self.version,
                "points": np.round(pts, 3).tolist(), "path": [[round(c, 3) for c in p] for p in self.path],
            }


_bridge: SlamBridge | None = None


def get() -> SlamBridge | None:
    return _bridge


def enable() -> SlamBridge:
    global _bridge
    _bridge = SlamBridge()
    return _bridge
