"""Outputs of a run: decisions.jsonl, annotated frames + video, and a one-page HTML report."""

from __future__ import annotations

import html
import json
from collections import Counter
from pathlib import Path

import cv2
import numpy as np

from .schemas import Action, CouncilReport, NavDecision, Urgency

COLORS = {Urgency.INFO: (90, 200, 90), Urgency.CAUTION: (0, 190, 255), Urgency.CRITICAL: (40, 40, 230)}
BOX_COLORS = {
    "person_ahead": (255, 180, 0),
    "stairs_up": (200, 120, 255),
    "stairs_down": (60, 60, 255),
    "path_obstacle": (0, 200, 255),
    "ground_hazard": (0, 140, 255),
    "overhead_hazard": (255, 0, 200),
    "crossing_object": (255, 255, 0),
}


def open_video_writer(path: Path, fps: float, width: int, height: int) -> cv2.VideoWriter:
    """H.264 so browsers and the app's viewer can play it; MPEG-4 Part 2 ('mp4v') only as a last resort."""
    for codec in ("avc1", "mp4v"):
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*codec), fps, (width, height))
        if writer.isOpened():
            return writer
        writer.release()
    raise RuntimeError(f"No usable video codec for {path}")


def encode_frames(frames_dir: Path, out: Path, fps: float) -> int:
    """Rebuild an annotated video from a run's saved frames."""
    files = sorted(frames_dir.glob("*.jpg"))
    if not files:
        return 0
    first = cv2.imread(str(files[0]))
    writer = open_video_writer(out, fps, first.shape[1], first.shape[0])
    for f in files:
        writer.write(cv2.imread(str(f)))
    writer.release()
    return len(files)


def annotate(bgr: np.ndarray, d: NavDecision, r: CouncilReport | None, threshold: float) -> np.ndarray:
    img = bgr.copy()
    h, w = img.shape[:2]
    if r and r.perception:
        for key, color in BOX_COLORS.items():
            det = getattr(r.perception, key)
            if det.box and det.presence >= threshold:
                b = det.box
                p1, p2 = (int(b.left * w), int(b.top * h)), (int(b.right * w), int(b.bottom * h))
                cv2.rectangle(img, p1, p2, color, 2)
                cv2.putText(img, f"{key} {det.presence:.2f}", (p1[0], max(15, p1[1] - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)
    bar = max(36, h // 14)
    cv2.rectangle(img, (0, 0), (w, bar), COLORS[d.urgency], -1)
    label = f"{d.action.value}  [{d.urgency.value}]"
    if d.ttc_s is not None:
        label += f"  TTC {d.ttc_s:.1f}s"
    cv2.putText(img, label, (10, int(bar * 0.7)), cv2.FONT_HERSHEY_SIMPLEX, bar / 50, (255, 255, 255), 2, cv2.LINE_AA)
    if d.reasons:
        cv2.putText(img, d.reasons[-1][:90], (10, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
    return img


class RunRecorder:
    def __init__(self, out_dir: Path, fps: float, threshold: float, save_frames: bool = True) -> None:
        self.dir = out_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "frames").mkdir(exist_ok=True)
        self.fps, self.threshold, self.save_frames = fps, threshold, save_frames
        self._jsonl = (self.dir / "decisions.jsonl").open("w", encoding="utf-8")
        self._video: cv2.VideoWriter | None = None
        self.decisions: list[NavDecision] = []
        self.spoken: list[tuple[float, str, str]] = []
        self.tokens = 0
        self.latencies: list[float] = []

    def add(self, bgr: np.ndarray, d: NavDecision, r: CouncilReport | None) -> None:
        self.decisions.append(d)
        if r:
            self.tokens += r.input_tokens
            self.latencies.append(r.latency_s)
        rec = {"decision": d.model_dump(mode="json"), "council": r.model_dump(mode="json") if r else None}
        self._jsonl.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._jsonl.flush()
        img = annotate(bgr, d, r, self.threshold)
        if self.save_frames:
            cv2.imwrite(str(self.dir / "frames" / f"{d.frame_index:06d}.jpg"), img)
        if self._video is None:
            self._video = open_video_writer(self.dir / "annotated.mp4", self.fps, img.shape[1], img.shape[0])
        self._video.write(img)

    def note_speech(self, t: float, channel: str, text: str) -> None:
        self.spoken.append((t, channel, text))

    def close(self, cost_usd: float) -> Path:
        self._jsonl.close()
        if self._video is not None:
            self._video.release()
        return self._write_report(cost_usd)

    def _write_report(self, cost_usd: float) -> Path:
        counts = Counter(d.action for d in self.decisions)
        crit = sum(1 for d in self.decisions if d.urgency == Urgency.CRITICAL)
        lat = sorted(self.latencies)
        p50 = lat[len(lat) // 2] if lat else 0
        p95 = lat[int(len(lat) * 0.95)] if lat else 0
        rows = "".join(
            f"<tr class='{d.urgency.value}'><td>{d.frame_index}</td><td>{d.timestamp_s:.1f}s</td><td>{d.action.value}</td>"
            f"<td>{d.urgency.value}</td><td>{'' if d.ttc_s is None else f'{d.ttc_s:.1f}s'}</td>"
            f"<td>{html.escape('; '.join(d.reasons))}</td></tr>"
            for d in self.decisions
        )
        spoken = "".join(f"<li><b>{t:.1f}s</b> [{c}] {html.escape(s)}</li>" for t, c, s in self.spoken) or "<li>Nothing spoken</li>"
        tiles = "".join(f"<div class='tile'><b>{counts.get(a, 0)}</b><span>{a.value}</span></div>" for a in Action)
        page = f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>
<title>Jev Nav Run</title><style>
:root{{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#e5e5e5;--info:#2e8b57;--caution:#c77700;--critical:#c62828}}
@media (prefers-color-scheme:dark){{:root{{--bg:#141414;--fg:#eee;--mut:#999;--line:#2a2a2a}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px;max-width:1100px;margin:auto}}
.tiles{{display:flex;flex-wrap:wrap;gap:8px}}.tile{{border:1px solid var(--line);border-radius:8px;padding:8px 12px;min-width:110px}}
.tile b{{display:block;font-size:20px}}.tile span{{color:var(--mut);font-size:12px}}
table{{border-collapse:collapse;width:100%;margin-top:16px}}td,th{{border-bottom:1px solid var(--line);padding:6px;text-align:left;vertical-align:top}}
tr.critical td:nth-child(3){{color:var(--critical);font-weight:600}}tr.caution td:nth-child(3){{color:var(--caution)}}tr.info td:nth-child(3){{color:var(--info)}}
.wrap{{overflow-x:auto}}</style></head><body>
<h1>Jev navigation run</h1>
<p>{len(self.decisions)} frames · {crit} critical alerts · Jev input tokens {self.tokens:,} (≈ ${cost_usd:.4f}) · latency p50 {p50:.2f}s, p95 {p95:.2f}s</p>
<div class='tiles'>{tiles}</div><h2>Spoken</h2><ul>{spoken}</ul>
<h2>Decisions</h2><div class='wrap'><table><tr><th>#</th><th>t</th><th>action</th><th>urgency</th><th>TTC</th><th>why</th></tr>{rows}</table></div>
</body></html>"""
        path = self.dir / "report.html"
        path.write_text(page, encoding="utf-8")
        return path
