"""Offline demo server with HTTP Range support (video seeking). Usage: python demo/serve.py [port]"""
import os, re, sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))

class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=ROOT, **k)
    def send_head(self):
        rng = self.headers.get("Range"); path = self.translate_path(self.path)
        if not rng or not os.path.isfile(path): return super().send_head()
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        size = os.path.getsize(path)
        a = int(m.group(1)) if m.group(1) else 0
        b = int(m.group(2)) if m.group(2) else size - 1
        b = min(b, size - 1)
        f = open(path, "rb"); f.seek(a)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
        self.send_header("Content-Length", str(b - a + 1))
        self.end_headers()
        self._left = b - a + 1
        return f
    def copyfile(self, src, dst):
        left = getattr(self, "_left", None)
        try:
            while True:
                n = 65536 if left is None else min(65536, left)
                if n <= 0: break
                buf = src.read(n)
                if not buf: break
                dst.write(buf)
                if left is not None: left -= len(buf)
        except (ConnectionResetError, BrokenPipeError): pass
    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes"); super().end_headers()
    def log_message(self, *a): pass

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5173
    print(f"Demo on http://localhost:{port}/")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
