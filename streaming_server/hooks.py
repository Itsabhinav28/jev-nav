"""Live frame hook. Edit `on_frame` to run OpenCV / AI models on the stream.

Called in a worker thread at most ANALYSIS_FPS times per second (frames are
skipped if your code is slower, so the stream never stalls).

  img     HxWx3 uint8 BGR numpy array (OpenCV-compatible)
  ts      unix time the frame arrived
  sensors dict or None: heading, pitch, abs, state ("still"/"walking"/...), accel_std,
          speed_ms, turn_dps, lat, lon, gps_acc, alt   (None if phone sends no sensors)
"""
import numpy as np

ANALYSIS_FPS = 10


def on_frame(img: np.ndarray, ts: float, sensors: dict | None = None) -> None:
    # Example: print a status line every ~5 s. Replace with detection etc.
    if int(ts) % 5 == 0 and int(ts * ANALYSIS_FPS) % ANALYSIS_FPS == 0:
        s = sensors or {}
        print(f"[hook] {img.shape[1]}x{img.shape[0]} brightness={img.mean():.1f} "
              f"state={s.get('state')} heading={s.get('heading')}")
