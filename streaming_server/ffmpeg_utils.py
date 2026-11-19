"""FFmpeg helpers: locate the binary, live frame/record sink, and video->frames."""
from __future__ import annotations

import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

import numpy as np


def find_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path:
        return path
    # winget installs don't reach already-open terminals' PATH; look there directly
    import glob
    import os

    for pat in (os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffmpeg.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
                r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"):
        hits = glob.glob(pat)
        if hits:
            return hits[0]
    try:  # optional fallback: pip install imageio-ffmpeg
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise RuntimeError(
            "FFmpeg not found. Install it (winget install Gyan.FFmpeg) and reopen the terminal, "
            "or `pip install imageio-ffmpeg`."
        )


class CaptureSession:
    """Pipes raw BGR frames into one FFmpeg process that can simultaneously
    (a) write frames at `fps` as jpg/png and (b) record a full-rate MP4."""

    def __init__(self, out_dir: Path, width: int, height: int, fps: float,
                 fmt: str = "jpg", record: bool = True, save_frames: bool = True,
                 record_fps: int = 30):
        self.width, self.height = width - width % 2, height - height % 2
        self.dir = Path(out_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.fps, self.fmt = fps, fmt
        self.frames_dir = self.dir / "frames"
        self.video_path = self.dir / "recording.mp4"
        self.started = time.time()
        self.frames_in = 0
        self.dropped = 0

        cmd = [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
               "-f", "rawvideo", "-pix_fmt", "bgr24",
               "-s", f"{self.width}x{self.height}",
               "-use_wallclock_as_timestamps", "1", "-i", "pipe:0"]
        if record:
            cmd += ["-map", "0:v", "-vf", f"fps={record_fps}", "-c:v", "libx264",
                    "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                    "-movflags", "+faststart", str(self.video_path)]
        if save_frames:
            self.frames_dir.mkdir(exist_ok=True)
            cmd += ["-map", "0:v", "-vf", f"fps={fps}"]
            cmd += ["-q:v", "2"] if fmt == "jpg" else []
            cmd += [str(self.frames_dir / f"frame_%06d.{fmt}")]
        if not record and not save_frames:
            raise ValueError("Nothing to do: enable recording and/or frame saving")

        # sensors.csv: one row per extracted frame interval (row N ~ frame_00000N)
        self._csv = self._csv_w = None
        self._next_row = time.time()
        self.rows = 0
        if save_frames:
            import csv

            self._csv = open(self.dir / "sensors.csv", "w", newline="", encoding="utf-8")
            self._csv_w = csv.writer(self._csv)
            self._csv_w.writerow(["frame_approx", "unix_time", "heading_deg", "pitch_deg", "compass_absolute",
                                  "state", "accel_std", "speed_ms", "turn_dps", "lat", "lon", "gps_acc_m",
                                  "alt_m"])

        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        self._q: queue.Queue = queue.Queue(maxsize=90)  # ~3 s of backlog, then drop
        self._t = threading.Thread(target=self._writer, daemon=True)
        self._t.start()

    def _writer(self):
        while True:
            buf = self._q.get()
            if buf is None:
                break
            try:
                self.proc.stdin.write(buf)
            except (BrokenPipeError, OSError):
                break
        try:
            self.proc.stdin.close()
        except Exception:
            pass

    def push(self, bgr: np.ndarray):
        if bgr.shape[1] != self.width or bgr.shape[0] != self.height:
            import cv2

            bgr = cv2.resize(bgr, (self.width, self.height), interpolation=cv2.INTER_AREA)
        try:
            self._q.put_nowait(bgr.tobytes())
            self.frames_in += 1
        except queue.Full:
            self.dropped += 1

    def log_sensors(self, snap: dict | None, now: float):
        if not self._csv_w or now < self._next_row:
            return
        self._next_row = max(self._next_row + 1 / self.fps, now - 1 / self.fps)
        self.rows += 1
        s = snap or {}
        r = lambda k, n=2: None if s.get(k) is None else round(s[k], n)
        self._csv_w.writerow([self.rows, round(now, 3), r("heading", 1), r("pitch", 1), s.get("abs"),
                              s.get("state"), r("accel_std", 3), r("speed_ms"), r("turn_dps", 1),
                              r("lat", 7), r("lon", 7), r("gps_acc", 1), r("alt", 1)])

    def stop(self) -> dict:
        if self._csv:
            self._csv.close()
            self._csv_w = None
        self._q.put(None)
        self._t.join(timeout=10)
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        err = self.proc.stderr.read().decode(errors="replace").strip() if self.proc.stderr else ""
        n = len(list(self.frames_dir.glob(f"*.{self.fmt}"))) if self.frames_dir.exists() else 0
        return {"dir": str(self.dir), "frames_saved": n, "frames_received": self.frames_in,
                "dropped": self.dropped, "seconds": round(time.time() - self.started, 1),
                "ffmpeg_error": err or None}


def video_to_frames(video: str, out_dir: str, fps: float | None = None, fmt: str = "jpg",
                    scale_width: int | None = None, start: str | None = None,
                    duration: str | None = None) -> int:
    """Split a video file into images. fps=None keeps every frame (native rate)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    filters = []
    if fps:
        filters.append(f"fps={fps}")
    if scale_width:
        filters.append(f"scale={scale_width}:-2")
    cmd = [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y"]
    if start:
        cmd += ["-ss", start]
    cmd += ["-i", video]
    if duration:
        cmd += ["-t", duration]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    if fmt == "jpg":
        cmd += ["-q:v", "2"]
    cmd += ["-fps_mode", "vfr", str(out / f"frame_%06d.{fmt}")]
    subprocess.run(cmd, check=True)
    return len(list(out.glob(f"*.{fmt}")))
