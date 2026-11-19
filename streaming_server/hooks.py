"""Live frame hook: every phone frame that passes the rate limit goes to the Jev navigation council.

Called in a worker thread at most ANALYSIS_FPS times per second (frames are
skipped if the analysis is slower, so the stream never stalls).

  img     HxWx3 uint8 BGR numpy array (OpenCV-compatible)
  ts      unix time the frame arrived
  sensors dict or None: heading, pitch, abs, state ("still"/"walking"/...), accel_std,
          speed_ms, turn_dps, lat, lon, gps_acc, alt   (None if phone sends no sensors)
"""
import numpy as np

import jev_bridge

ANALYSIS_FPS = 2  # matches SAMPLE_FPS in .env; Jev takes ~1 s per frame, extra frames would just be dropped


def on_frame(img: np.ndarray, ts: float, sensors: dict | None = None) -> None:
    bridge = jev_bridge.get()
    if bridge is not None:
        bridge.submit(img, ts, sensors)
