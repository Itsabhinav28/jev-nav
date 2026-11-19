"""Split an existing video into frames:  python video_to_frames.py clip.mp4 --fps 10"""
import argparse
from pathlib import Path

from ffmpeg_utils import video_to_frames

p = argparse.ArgumentParser(description="Split a video into frames with FFmpeg")
p.add_argument("video")
p.add_argument("--fps", type=float, default=None, help="frames per second to extract (default: all frames)")
p.add_argument("--format", choices=["jpg", "png"], default="jpg")
p.add_argument("--out", default=None, help="output folder (default: <video>_frames)")
p.add_argument("--width", type=int, default=None, help="optionally downscale to this width")
p.add_argument("--start", default=None, help="start time, e.g. 00:00:05")
p.add_argument("--duration", default=None, help="length to extract, e.g. 10")
a = p.parse_args()

out = a.out or str(Path(a.video).with_suffix("")) + "_frames"
n = video_to_frames(a.video, out, a.fps, a.format, a.width, a.start, a.duration)
print(f"Wrote {n} frames to {out}")
