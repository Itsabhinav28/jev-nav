# Phone → Laptop camera streaming (WebRTC + FFmpeg)

```
Phone browser (getUserMedia, H.264/VP8, up to 4K/60)
   │  WebRTC: UDP, DTLS-SRTP encrypted, adaptive bitrate, signalling = one HTTPS POST /offer
   ▼
Laptop: server.py (aiohttp + aiortc)
   ├─ /            dashboard: live preview, FPS picker, start/stop capture, QR code
   ├─ /phone       the page you open on the phone
   ├─ hooks.py     on_frame(img, ts): live numpy/OpenCV frames for your own AI code
   └─ FFmpeg       one process: frames @ chosen FPS (jpg/png) + full MP4 recording
```

## Run
```
pip install -r requirements.txt
winget install Gyan.FFmpeg          # then reopen the terminal
python server.py
```
1. Open `https://localhost:8443/` on the laptop (accept the certificate warning).
2. Scan the QR code with the phone (same Wi-Fi), accept the warning, tap **Start streaming**.
3. On the dashboard pick FPS (1, 10, 30, or custom), format, then **Start capture**.
   Output goes to `captures/<timestamp>/frames/frame_000001.jpg` and `recording.mp4`.

If the phone can't connect, allow Python through Windows Firewall (Private networks) —
the page is TCP 8443 and the video flows over UDP.

## Split an existing video
```
python video_to_frames.py clip.mp4 --fps 10 --format png --width 1280
```

## Quality notes
- Phone prefers hardware H.264, bitrate selectable (up to 15 Mbps on LAN), `maintain-resolution`,
  `contentHint=motion`; the status line shows real fps/kbps and whether the encoder is being limited.
- Laptop → FFmpeg uses a bounded queue (drops frames instead of lagging) and wall-clock timestamps.
- Raw frames are piped as BGR, so frame saving costs CPU; 4K@30 PNG will be heavy — prefer JPG.
- Extend `hooks.py` for detection/OCR/etc. It runs in a worker thread and skips frames if slow.
