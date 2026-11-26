"""Run Sample.mp4 through ORB-SLAM3 (slam_server on 127.0.0.1:5555) once and save the map as demo/slam.json.
Usage: python demo/record_slam.py <video> [out.json]"""
import json, struct, sys
from pathlib import Path
import cv2
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "streaming_server"))
import slam_bridge as sb

video = sys.argv[1]; out = sys.argv[2] if len(sys.argv) > 2 else str(Path(__file__).with_name("slam.json"))
cap = cv2.VideoCapture(video); fps = cap.get(cv2.CAP_PROP_FPS) or 30
step = max(1, round(fps / sb.SLAM_FPS))
b = sb.SlamBridge()
epoch, seen, pts, path, states, lost_since = 0, {}, [], [], [], None
i = 0
while True:
    ok, img = cap.read()
    if not ok: break
    t = i / fps; i += 1
    if (i - 1) % step: continue
    h, w = img.shape[:2]
    size = (sb.PROC_WIDTH, max(2, int(round(sb.PROC_WIDTH * h / w / 2)) * 2))
    frame = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
    if b.sock is None: b._connect(size)
    jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])[1]
    b.sock.sendall(struct.pack("<Id", len(jpg), t) + jpg.tobytes())
    r = json.loads(b.rfile.readline())
    st = sb.STATES.get(r["state"], "?"); states.append([round(t, 2), st, epoch])
    if r["pose"]: path.append([round(c, 3) for c in r["pose"][:3]] + [round(t, 2), epoch])
    for p in r["pts"]:
        k = (epoch, int(p[0]))
        if k not in seen:
            seen[k] = 1; pts.append([round(p[1], 3), round(p[2], 3), round(p[3], 3), round(t, 2), epoch])
    if r["state"] == 3:
        lost_since = lost_since if lost_since is not None else t
        if t - lost_since >= sb.LOST_RESET_S:
            b.sock.sendall(struct.pack("<I", 0xFFFFFFFF)); json.loads(b.rfile.readline())
            epoch += 1; lost_since = None
    else: lost_since = None
    if (i // step) % 50 == 0: print(f"t={t:5.1f}s state={st} pts={len(pts)} path={len(path)} epoch={epoch}", flush=True)
b._close()
json.dump({"duration": i / fps, "points": pts, "path": path, "states": states}, open(out, "w"))
print("saved", out, len(pts), "points", len(path), "poses", "epochs", epoch + 1)
