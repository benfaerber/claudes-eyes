"""Claude's Eyes — shared webcam dashboard.

One process owns the desk webcam and serves the live view to everyone at
once:

    http://localhost:8990/              the dashboard Ben watches
    http://localhost:8990/frame.jpg     the latest frame Claude curls
    POST /observation  (plain text)     Claude narrates what it just saw
    http://localhost:8990/observations  the narration log as JSON
    POST /activity     (plain text)     Claude reports what it's doing mid-look
    http://localhost:8990/status        paused flag + Claude's current activity

Run:  make run   (uv supplies Python; ffmpeg must be installed)
Stop: Ctrl+C
"""

# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class CameraFeed:
    """Owns the camera via a self-restarting ffmpeg that continuously
    overwrites the latest-frame JPEG.

    Configure via environment:
        EYES_DEVICE  camera node (default: newest /dev/video*)
        EYES_ROTATE  0, 90, 180 or 270 degrees clockwise (default 0)
    """

    FPS = 4
    RESTART_DELAY_SECONDS = 3
    ROTATION_FILTERS = {"0": None, "90": "transpose=1", "180": "transpose=1,transpose=1", "270": "transpose=2"}

    def __init__(self, frame_path: Path):
        self.frame_path = frame_path
        self.device = os.environ.get("EYES_DEVICE") or self.newest_video_device()
        self.rotation_filter = self.ROTATION_FILTERS.get(os.environ.get("EYES_ROTATE", "0"))
        self.process: subprocess.Popen | None = None
        self.unpaused = threading.Event()
        self.unpaused.set()

    @staticmethod
    def newest_video_device() -> str:
        devices = sorted(Path("/dev").glob("video*"), key=lambda d: d.stat().st_mtime)
        if not devices:
            raise SystemExit("No /dev/video* device found — plug in a webcam or set EYES_DEVICE.")
        return str(devices[-1])

    def ffmpeg_command(self) -> list[str]:
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "v4l2", "-video_size", "1920x1080",
            "-i", self.device,
        ]
        if self.rotation_filter:
            command += ["-vf", self.rotation_filter]
        command += [
            "-r", str(self.FPS), "-q:v", "5",
            "-update", "1", "-y", str(self.frame_path),
        ]
        return command

    def run_forever(self) -> None:
        while True:
            self.unpaused.wait()
            self.process = subprocess.Popen(self.ffmpeg_command())
            self.process.wait()
            time.sleep(self.RESTART_DELAY_SECONDS)

    def frame_age_seconds(self) -> float | None:
        if not self.frame_path.exists():
            return None
        return time.time() - self.frame_path.stat().st_mtime

    def pause(self) -> None:
        self.unpaused.clear()
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def resume(self) -> None:
        self.unpaused.set()

    def is_paused(self) -> bool:
        return not self.unpaused.is_set()


class ObservationLog:
    """What Claude said it saw, newest first, capped."""

    MAX_ENTRIES = 200
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


class ClaudeActivity:
    """What Claude is doing with the Eyes right now, shown live on the dash.
    Set automatically when a non-browser client downloads a frame, refined by
    POST /activity, cleared when the observation lands."""

    def __init__(self) -> None:
        self.text: str | None = None
        self.at: float = 0.0
        self.lock = threading.Lock()

    def set(self, text: str) -> None:
        with self.lock:
            self.text = text
            self.at = time.time()

    def clear(self) -> None:
        with self.lock:
            self.text = None

    def snapshot(self) -> dict | None:
        with self.lock:
            if self.text is None:
                return None
            return {"text": self.text, "at": self.at}


class EyesRequestHandler(BaseHTTPRequestHandler):
    feed: CameraFeed
    observations: ObservationLog
    activity: ClaudeActivity

    PAGE = """<!doctype html>
<html><head><title>Claude's Eyes</title>
<style>
  body { margin: 0; background: #14181d; color: #d8dee6; font-family: system-ui, sans-serif;
         display: flex; flex-direction: column; align-items: center; gap: 12px; padding: 16px; }
  h1 { font-size: 1.1em; font-weight: 600; margin: 0; }
  img { max-width: 96vw; max-height: 70vh; border: 1px solid #2c343d; border-radius: 6px; }
  img[hidden] { display: none; }
  #loader { width: min(96vw, 640px); height: 300px; border: 1px solid #2c343d; border-radius: 6px;
            display: flex; align-items: center; justify-content: center; color: #8b98a5;
            font-size: 0.9em; background: linear-gradient(100deg, #1a2027 40%, #232b34 50%, #1a2027 60%);
            background-size: 300% 100%; animation: shimmer 1.6s infinite; }
  #loader[hidden] { display: none; }
  @keyframes shimmer { from { background-position: 100% 0; } to { background-position: -100% 0; } }
  @media (prefers-reduced-motion: reduce) { #loader { animation: none; } }
  #status { font-size: 0.85em; color: #8b98a5; }
  .stale { color: #e0a458; }
  #pause { background: #1e2833; color: #d8dee6; border: 1px solid #31404f; border-radius: 8px;
           padding: 6px 18px; font-size: 0.9em; cursor: pointer; }
  #pause.paused { background: #4a2b2b; border-color: #7a4040; }
  #pause:disabled { opacity: 0.6; cursor: wait; }
  #bubble { max-width: 60ch; background: #1e2833; border: 1px solid #31404f; border-radius: 12px;
            padding: 10px 16px; font-size: 0.95em; line-height: 1.45; }
  #bubble .who { color: #6cb2ff; font-weight: 600; margin-right: 6px; }
  #bubble .when { color: #8b98a5; font-size: 0.8em; margin-left: 8px; }
  #log { max-width: 60ch; width: 100%; font-size: 0.8em; color: #8b98a5; list-style: none;
         padding: 0; margin: 0; }
  #log li { padding: 2px 0; border-top: 1px solid #1e242b; }
  #grow { background: none; color: #6cb2ff; border: none; font-size: 0.8em; cursor: pointer;
          padding: 4px 0; }
  #grow[hidden] { display: none; }
  #activity { font-size: 0.9em; color: #6cb2ff; display: flex; align-items: center; gap: 8px; }
  #activity[hidden] { display: none; }
  #activity .dot { width: 8px; height: 8px; border-radius: 50%; background: #6cb2ff;
                   animation: blink 1.2s infinite; }
  @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.2; } }
  @media (prefers-reduced-motion: reduce) { #activity .dot { animation: none; } }
</style></head>
<body>
<h1>&#128065; Claude's Eyes</h1>
<div id="loader">warming up the eyes&hellip;</div>
<img id="view" alt="camera frame" hidden>
<div id="status">connecting&hellip;</div>
<div id="activity" hidden><span class="dot"></span><span id="activityText"></span></div>
<button id="pause">Pause Eyes</button>
<div id="bubble" hidden><span class="who">Claude</span><span id="latest"></span><span class="when" id="latestWhen"></span></div>
<ul id="log"></ul>
<button id="grow" hidden>show older</button>
<script>
  const view = document.getElementById('view');
  const status = document.getElementById('status');
  const bubble = document.getElementById('bubble');
  const latest = document.getElementById('latest');
  const latestWhen = document.getElementById('latestWhen');
  const log = document.getElementById('log');
  const pause = document.getElementById('pause');
  const loader = document.getElementById('loader');

  let paused = false;
  pause.addEventListener('click', async () => {
    pause.disabled = true;
    pause.textContent = paused ? 'Resuming\\u2026' : 'Pausing\\u2026';
    try {
      const data = await (await fetch(paused ? '/resume' : '/pause', {method: 'POST'})).json();
      paused = data.paused;
      if (!paused) {
        view.hidden = true;
        loader.hidden = false;
        loader.textContent = 'reopening the eyes\\u2026';
      }
    } finally {
      pause.disabled = false;
      pause.textContent = paused ? 'Resume Eyes' : 'Pause Eyes';
      pause.className = paused ? 'paused' : '';
    }
  });

  const agoText = seconds => seconds < 90 ? Math.round(seconds) + 's ago'
      : seconds < 5400 ? Math.round(seconds / 60) + 'm ago'
      : Math.round(seconds / 3600) + 'h ago';

  setInterval(async () => {
    if (paused) {
      status.textContent = 'eyes paused \\u2014 nothing is being captured';
      status.className = 'stale';
      return;
    }
    try {
      const response = await fetch('/frame.jpg?t=' + Date.now(), {cache: 'no-store'});
      if (!response.ok) throw new Error(response.status);
      const age = parseFloat(response.headers.get('X-Frame-Age') || '0');
      view.src = URL.createObjectURL(await response.blob());
      view.hidden = false;
      loader.hidden = true;
      status.textContent = age > 10
        ? 'camera offline? last frame ' + Math.round(age) + 's old'
        : 'live \\u00b7 frame ' + age.toFixed(1) + 's old';
      status.className = age > 10 ? 'stale' : '';
    } catch (e) {
      status.textContent = 'no frames yet (' + e.message + ')';
      status.className = 'stale';
    }
  }, 500);

  const grow = document.getElementById('grow');
  let logLimit = 8;
  grow.addEventListener('click', () => { logLimit += 20; });

  const activity = document.getElementById('activity');
  const activityText = document.getElementById('activityText');
  setInterval(async () => {
    try {
      const data = await (await fetch('/status', {cache: 'no-store'})).json();
      const seconds = data.activity ? Date.now() / 1000 - data.activity.at : 999;
      if (data.activity && seconds < 120) {
        activity.hidden = false;
        activityText.textContent = 'Claude: ' + data.activity.text + ' (' + agoText(seconds) + ')';
      } else {
        activity.hidden = true;
      }
    } catch (e) {}
  }, 500);

  setInterval(async () => {
    try {
      const data = await (await fetch('/observations', {cache: 'no-store'})).json();
      const entries = data.observations;
      if (!entries.length) return;
      bubble.hidden = false;
      latest.textContent = entries[0].text;
      latestWhen.textContent = agoText(Date.now() / 1000 - entries[0].at);
      log.replaceChildren(...entries.slice(1, logLimit).map(entry => {
        const item = document.createElement('li');
        item.textContent = agoText(Date.now() / 1000 - entry.at) + ' \\u2014 ' + entry.text;
        return item;
      }));
      const hiddenCount = Math.max(0, entries.length - logLimit);
      grow.hidden = hiddenCount === 0;
      grow.textContent = 'show older (' + hiddenCount + ' more)';
    } catch (e) {}
  }, 1000);
</script>
</body></html>"""

    def do_GET(self) -> None:
        if self.path.startswith("/frame.jpg"):
            self.send_frame()
        elif self.path.startswith("/observations"):
            self.send_json(self.observations.as_json())
        elif self.path.startswith("/status"):
            self.send_json(json.dumps({
                "paused": self.feed.is_paused(),
                "activity": self.activity.snapshot(),
            }).encode())
        else:
            self.send_page()

    def is_claude(self) -> bool:
        return not self.headers.get("User-Agent", "").startswith("Mozilla")

    def do_POST(self) -> None:
        if self.path.startswith("/pause"):
            self.feed.pause()
            self.send_json(b'{"ok": true, "paused": true}')
        elif self.path.startswith("/resume"):
            self.feed.resume()
            self.send_json(b'{"ok": true, "paused": false}')
        elif self.path.startswith("/observation"):
            length = int(self.headers.get("Content-Length", 0))
            text = self.rfile.read(length).decode("utf-8", errors="replace").strip()
            if not text:
                self.send_error(400, "empty observation")
                return
            self.observations.add(text)
            self.activity.clear()
            self.send_json(b'{"ok": true}')
        elif self.path.startswith("/activity"):
            length = int(self.headers.get("Content-Length", 0))
            text = self.rfile.read(length).decode("utf-8", errors="replace").strip()
            if not text:
                self.activity.clear()
            else:
                self.activity.set(text)
            self.send_json(b'{"ok": true}')
        else:
            self.send_error(404)

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
        if self.feed.is_paused():
            self.send_error(503, "eyes paused")
            return
        age = self.feed.frame_age_seconds()
        if age is None:
            self.send_error(503, "no frame captured yet")
            return
        if self.is_claude():
            self.activity.set("downloaded a frame, having a look…")
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
    def __init__(self) -> None:
        self.port = int(os.environ.get("EYES_PORT", "8990"))
        frame_path = Path.home() / ".cache" / "claudes-eyes" / "latest.jpg"
        frame_path.parent.mkdir(parents=True, exist_ok=True)
        self.feed = CameraFeed(frame_path)
        self.observations = ObservationLog()
        self.activity = ClaudeActivity()

    def run(self) -> None:
        threading.Thread(target=self.feed.run_forever, daemon=True).start()
        EyesRequestHandler.feed = self.feed
        EyesRequestHandler.observations = self.observations
        EyesRequestHandler.activity = self.activity
        server = ThreadingHTTPServer(("0.0.0.0", self.port), EyesRequestHandler)
        print(f"Claude's Eyes on http://localhost:{self.port}/ (camera {self.feed.device})")
        server.serve_forever()


if __name__ == "__main__":
    EyesServer().run()
