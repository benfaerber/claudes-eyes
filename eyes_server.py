"""Claude's Eyes — shared webcam dashboard.

One process owns /dev/video2 (the desk webcam, mounted upside-down on the
monitor) and serves the live view to everyone at once:

    http://localhost:8990/              the dashboard Ben watches
    http://localhost:8990/frame.jpg     the latest frame Claude curls
    POST /observation  (plain text)     Claude narrates what it just saw
    http://localhost:8990/observations  the narration log as JSON

Run:  make run   (uv supplies Python; ffmpeg must be installed)
Stop: Ctrl+C
"""

# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///

import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class CameraFeed:
    """Owns the camera via a self-restarting ffmpeg that continuously
    overwrites frames/latest.jpg (rotated 180 for the upside-down mount)."""

    DEVICE = "/dev/video2"
    FPS = 4
    RESTART_DELAY_SECONDS = 3

    def __init__(self, frame_path: Path):
        self.frame_path = frame_path
        self.process: subprocess.Popen | None = None

    def run_forever(self) -> None:
        while True:
            self.process = subprocess.Popen(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "v4l2", "-video_size", "1920x1080",
                    "-i", self.DEVICE,
                    "-vf", "transpose=1,transpose=1",
                    "-r", str(self.FPS), "-q:v", "5",
                    "-update", "1", "-y", str(self.frame_path),
                ],
            )
            self.process.wait()
            time.sleep(self.RESTART_DELAY_SECONDS)

    def frame_age_seconds(self) -> float | None:
        if not self.frame_path.exists():
            return None
        return time.time() - self.frame_path.stat().st_mtime


class ObservationLog:
    """What Claude said it saw, newest first, capped."""

    MAX_ENTRIES = 50
    MAX_TEXT_BYTES = 2000

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self.lock = threading.Lock()

    def add(self, text: str) -> None:
        entry = {"text": text[: self.MAX_TEXT_BYTES], "at": time.time()}
        with self.lock:
            self.entries.insert(0, entry)
            del self.entries[self.MAX_ENTRIES:]

    def as_json(self) -> bytes:
        with self.lock:
            return json.dumps({"observations": self.entries}).encode()


class EyesRequestHandler(BaseHTTPRequestHandler):
    feed: CameraFeed
    observations: ObservationLog

    PAGE = """<!doctype html>
<html><head><title>Claude's Eyes</title>
<style>
  body { margin: 0; background: #14181d; color: #d8dee6; font-family: system-ui, sans-serif;
         display: flex; flex-direction: column; align-items: center; gap: 12px; padding: 16px; }
  h1 { font-size: 1.1em; font-weight: 600; margin: 0; }
  img { max-width: 96vw; max-height: 70vh; border: 1px solid #2c343d; border-radius: 6px; }
  #status { font-size: 0.85em; color: #8b98a5; }
  .stale { color: #e0a458; }
  #bubble { max-width: 60ch; background: #1e2833; border: 1px solid #31404f; border-radius: 12px;
            padding: 10px 16px; font-size: 0.95em; line-height: 1.45; }
  #bubble .who { color: #6cb2ff; font-weight: 600; margin-right: 6px; }
  #bubble .when { color: #8b98a5; font-size: 0.8em; margin-left: 8px; }
  #log { max-width: 60ch; width: 100%; font-size: 0.8em; color: #8b98a5; list-style: none;
         padding: 0; margin: 0; }
  #log li { padding: 2px 0; border-top: 1px solid #1e242b; }
</style></head>
<body>
<h1>&#128065; Claude's Eyes</h1>
<img id="view" src="/frame.jpg" alt="camera frame">
<div id="status">connecting&hellip;</div>
<div id="bubble" hidden><span class="who">Claude</span><span id="latest"></span><span class="when" id="latestWhen"></span></div>
<ul id="log"></ul>
<script>
  const view = document.getElementById('view');
  const status = document.getElementById('status');
  const bubble = document.getElementById('bubble');
  const latest = document.getElementById('latest');
  const latestWhen = document.getElementById('latestWhen');
  const log = document.getElementById('log');

  const agoText = seconds => seconds < 90 ? Math.round(seconds) + 's ago'
      : seconds < 5400 ? Math.round(seconds / 60) + 'm ago'
      : Math.round(seconds / 3600) + 'h ago';

  setInterval(async () => {
    try {
      const response = await fetch('/frame.jpg?t=' + Date.now(), {cache: 'no-store'});
      if (!response.ok) throw new Error(response.status);
      const age = parseFloat(response.headers.get('X-Frame-Age') || '0');
      view.src = URL.createObjectURL(await response.blob());
      status.textContent = age > 10
        ? 'camera offline? last frame ' + Math.round(age) + 's old'
        : 'live \\u00b7 frame ' + age.toFixed(1) + 's old';
      status.className = age > 10 ? 'stale' : '';
    } catch (e) {
      status.textContent = 'no frames yet (' + e.message + ')';
      status.className = 'stale';
    }
  }, 500);

  setInterval(async () => {
    try {
      const data = await (await fetch('/observations', {cache: 'no-store'})).json();
      const entries = data.observations;
      if (!entries.length) return;
      bubble.hidden = false;
      latest.textContent = entries[0].text;
      latestWhen.textContent = agoText(Date.now() / 1000 - entries[0].at);
      log.replaceChildren(...entries.slice(1, 8).map(entry => {
        const item = document.createElement('li');
        item.textContent = agoText(Date.now() / 1000 - entry.at) + ' \\u2014 ' + entry.text;
        return item;
      }));
    } catch (e) {}
  }, 1000);
</script>
</body></html>"""

    def do_GET(self) -> None:
        if self.path.startswith("/frame.jpg"):
            self.send_frame()
        elif self.path.startswith("/observations"):
            self.send_json(self.observations.as_json())
        else:
            self.send_page()

    def do_POST(self) -> None:
        if not self.path.startswith("/observation"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        text = self.rfile.read(length).decode("utf-8", errors="replace").strip()
        if not text:
            self.send_error(400, "empty observation")
            return
        self.observations.add(text)
        self.send_json(b'{"ok": true}')

    def send_page(self) -> None:
        body = self.PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_frame(self) -> None:
        age = self.feed.frame_age_seconds()
        if age is None:
            self.send_error(503, "no frame captured yet")
            return
        body = self.feed.frame_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Age", f"{age:.1f}")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass


class EyesServer:
    PORT = 8990

    def __init__(self) -> None:
        frame_path = Path(__file__).parent / "frames" / "latest.jpg"
        frame_path.parent.mkdir(exist_ok=True)
        self.feed = CameraFeed(frame_path)
        self.observations = ObservationLog()

    def run(self) -> None:
        threading.Thread(target=self.feed.run_forever, daemon=True).start()
        EyesRequestHandler.feed = self.feed
        EyesRequestHandler.observations = self.observations
        server = ThreadingHTTPServer(("0.0.0.0", self.PORT), EyesRequestHandler)
        print(f"Claude's Eyes on http://localhost:{self.PORT}/ (camera {CameraFeed.DEVICE})")
        server.serve_forever()


if __name__ == "__main__":
    EyesServer().run()
