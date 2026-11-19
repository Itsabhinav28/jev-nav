"""Frame sources and local (non-AI) checks: Jev image prep, blur/light gate, ego-motion."""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .schemas import EgoMotion, FrameQuality, UserActivity

# Jev image limits (docs.typesafe.ai/images): PNG/JPEG, each side 280..1568 px, never resized server-side.
JEV_MAX_SIDE = 1568
JEV_MIN_SIDE = 280
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


@dataclass
class Frame:
    index: int
    timestamp_s: float
    bgr: np.ndarray


def iter_frames(source: str | Path | int, sample_fps: float, realtime: bool = False) -> Iterator[Frame]:
    """Yield frames from a video file, a folder of images, or a camera index, sampled at `sample_fps`."""
    if isinstance(source, int) or (isinstance(source, str) and source.isdigit()):
        yield from _iter_camera(int(source), sample_fps)
        return
    path = Path(source)
    if path.is_dir():
        files = sorted(p for p in path.iterdir() if p.suffix.lower() in IMAGE_EXTS)
        for i, f in enumerate(files):
            img = cv2.imread(str(f))
            if img is None:
                continue
            yield Frame(i, i / sample_fps, img)
            if realtime:
                time.sleep(1 / sample_fps)
        return
    yield from _iter_video(path, sample_fps, realtime)


def _iter_video(path: Path, sample_fps: float, realtime: bool) -> Iterator[Frame]:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open video: {path}")
    native = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, round(native / sample_fps))
    raw_i = out_i = 0
    try:
        while True:
            ok, img = cap.read()
            if not ok:
                break
            if raw_i % step == 0:
                yield Frame(out_i, raw_i / native, img)
                out_i += 1
                if realtime:
                    time.sleep(1 / sample_fps)
            raw_i += 1
    finally:
        cap.release()


def _iter_camera(index: int, sample_fps: float) -> Iterator[Frame]:
    cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open camera {index}")
    start = time.monotonic()
    next_t = 0.0
    i = 0
    try:
        while True:
            ok, img = cap.read()
            if not ok:
                break
            t = time.monotonic() - start
            if t >= next_t:
                yield Frame(i, t, img)
                i += 1
                next_t = t + 1 / sample_fps
    finally:
        cap.release()


def prepare_for_jev(bgr: np.ndarray) -> bytes:
    """Resize into Jev's pixel limits and encode as JPEG. Pixels from OpenCV are already upright."""
    h, w = bgr.shape[:2]
    scale = min(1.0, JEV_MAX_SIDE / max(h, w))
    if min(h, w) * scale < JEV_MIN_SIDE:
        scale = JEV_MIN_SIDE / min(h, w)
    if scale != 1.0:
        bgr = cv2.resize(bgr, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)
    h, w = bgr.shape[:2]
    if max(h, w) > JEV_MAX_SIDE:  # extreme aspect ratio: crop the long side to fit both limits
        if w > h:
            x0 = (w - JEV_MAX_SIDE) // 2
            bgr = bgr[:, x0 : x0 + JEV_MAX_SIDE]
        else:
            y0 = (h - JEV_MAX_SIDE) // 2
            bgr = bgr[y0 : y0 + JEV_MAX_SIDE]
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise ValueError("JPEG encode failed")
    return buf.tobytes()


def assess_quality(bgr: np.ndarray, min_sharpness: float = 40.0) -> FrameQuality:
    """Blur (variance of Laplacian) and exposure gate, run before spending Jev calls."""
    gray = cv2.cvtColor(_small(bgr), cv2.COLOR_BGR2GRAY)
    sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    bright = float(gray.mean())
    if bright < 25:
        return FrameQuality(sharpness=sharp, brightness=bright, ok=False, reason="too_dark")
    if bright > 235:
        return FrameQuality(sharpness=sharp, brightness=bright, ok=False, reason="glare")
    if sharp < min_sharpness:
        return FrameQuality(sharpness=sharp, brightness=bright, ok=False, reason="blurry")
    return FrameQuality(sharpness=sharp, brightness=bright, ok=True)


class EgoMotionEstimator:
    """Dense optical flow between consecutive frames → what the user's body is doing."""

    def __init__(self, still_px: float = 0.6, turn_ratio: float = 2.0) -> None:
        self._prev: np.ndarray | None = None
        self.still_px = still_px
        self.turn_ratio = turn_ratio

    def update(self, bgr: np.ndarray) -> EgoMotion:
        gray = cv2.cvtColor(_small(bgr), cv2.COLOR_BGR2GRAY)
        prev, self._prev = self._prev, gray
        if prev is None or prev.shape != gray.shape:
            return EgoMotion(magnitude=0, horizontal=0, vertical=0, activity=UserActivity.UNKNOWN)
        flow = cv2.calcOpticalFlowFarneback(prev, gray, None, 0.5, 3, 15, 3, 5, 1.2, 0)
        fx, fy = flow[..., 0], flow[..., 1]
        mag = float(np.sqrt(fx**2 + fy**2).mean())
        hx, vy = float(fx.mean()), float(fy.mean())
        if mag < self.still_px:
            act = UserActivity.STANDING
        elif abs(hx) > self.turn_ratio * max(abs(vy), 0.1) and abs(hx) > 0.5 * mag:
            act = UserActivity.TURNING
        elif abs(vy) > 0.6 * mag and abs(vy) > 1.5:
            act = UserActivity.CLIMBING  # strong, consistent vertical scene motion
        else:
            act = UserActivity.WALKING
        return EgoMotion(magnitude=mag, horizontal=hx, vertical=vy, activity=act)


def _small(bgr: np.ndarray, width: int = 320) -> np.ndarray:
    h, w = bgr.shape[:2]
    if w <= width:
        return bgr
    return cv2.resize(bgr, (width, round(h * width / w)), interpolation=cv2.INTER_AREA)
