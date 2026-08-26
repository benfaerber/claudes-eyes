"""Claude's Eyes — shared webcam dashboard.

One process owns the desk webcam and serves the live view to everyone at
once:

    http://localhost:8990/              the dashboard Ben watches
    http://localhost:8990/frame.jpg     the latest frame Claude curls
    POST /observation  (plain text)     Claude narrates what it just saw
    http://localhost:8990/observations  the narration log as JSON
    POST /activity     (plain text)     Claude reports what it's doing mid-look
    http://localhost:8990/status        paused flag + Claude's current activity
    http://localhost:8990/ptz           gimbal support, axis ranges, last pose
    POST /ptz          (JSON)           aim the camera (gimbal cameras only)

Run:  make run   (uv supplies Python; ffmpeg must be installed)
Stop: Ctrl+C
"""

# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///

import ctypes
import fcntl
import json
import os
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class V4L2Device:
    """Minimal stdlib ioctl access to a /dev/video* node. V4L2 allows a
    second fd for capability queries and controls while ffmpeg owns the
    stream, so this opens per call and never touches the video data."""

    QUERYCAP = 0x80685600
    QUERYCTRL = 0xC0445624
    S_CTRL = 0xC008561C
    S_EXT_CTRLS = 0xC0205648
    EXT_CONTROL_BYTES = 20
    CAP_VIDEO_CAPTURE = 0x00000001
    CAP_DEVICE_CAPS = 0x80000000
    CTRL_FLAG_DISABLED = 0x00000001

    def __init__(self, path: str):
        self.path = path

    def _ioctl(self, request: int, buffer: bytearray) -> bytearray:
        fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        try:
            fcntl.ioctl(fd, request, buffer)
        finally:
            os.close(fd)
        return buffer

    def is_capture_device(self) -> bool:
        """False for the metadata sibling nodes modern UVC cameras expose
        (the Insta360 Link 2's /dev/video1 has no video formats at all)."""
        try:
            buffer = self._ioctl(self.QUERYCAP, bytearray(104))
        except OSError:
            return False
        capabilities, device_caps = struct.unpack_from("II", buffer, 84)
        effective = device_caps if capabilities & self.CAP_DEVICE_CAPS else capabilities
        return bool(effective & self.CAP_VIDEO_CAPTURE)

    def query_control(self, control_id: int) -> dict | None:
        buffer = bytearray(68)
        struct.pack_into("I", buffer, 0, control_id)
        try:
            self._ioctl(self.QUERYCTRL, buffer)
        except OSError:
            return None
        _, _, _, minimum, maximum, step, default, flags = struct.unpack_from("II32siiiiI", buffer)
        if flags & self.CTRL_FLAG_DISABLED:
            return None
        return {"min": minimum, "max": maximum, "step": step, "default": default}

    def set_control(self, control_id: int, value: int) -> None:
        self._ioctl(self.S_CTRL, bytearray(struct.pack("Ii", control_id, value)))

    def set_controls(self, values: dict[int, int]) -> None:
        """Sets several controls in one VIDIOC_S_EXT_CTRLS transaction.
        Needed for pan+tilt: they share one UVC control, the driver
        read-modify-writes single-axis changes, and cameras with broken
        readback (Insta360 Link 2) then fail validation with ERANGE."""
        array = ctypes.create_string_buffer(self.EXT_CONTROL_BYTES * len(values))
        for index, (control_id, value) in enumerate(values.items()):
            struct.pack_into("IIIi", array, self.EXT_CONTROL_BYTES * index, control_id, 0, 0, value)
        header = struct.pack("IIIiI4xQ", 0, len(values), 0, 0, 0, ctypes.addressof(array))
        self._ioctl(self.S_EXT_CTRLS, bytearray(header))


class GimbalPTZ:
    """Aims gimbal cameras (e.g. the Insta360 Link 2) through the standard
    UVC pan/tilt/zoom controls. Pan and tilt are absolute in arc-seconds;
    the Link 2 returns garbage when those controls are read back, so the
    last commanded pose is tracked here and the hardware is never asked."""

    PAN = 0x009A0908
    TILT = 0x009A0909
    ZOOM = 0x009A090D
    ARCSECONDS_PER_DEGREE = 3600

    def __init__(self, device: V4L2Device):
        self.device = device
        self.ranges = {
            "pan": device.query_control(self.PAN),
            "tilt": device.query_control(self.TILT),
            "zoom": device.query_control(self.ZOOM),
        }
        self.commanded: dict[str, float | int] = {}
        self.lock = threading.Lock()

    @property
    def supported(self) -> bool:
        return any(self.ranges.values())

    def status(self) -> dict:
        axes = {}
        for name, control_range in self.ranges.items():
            if not control_range:
                continue
            if name in ("pan", "tilt"):
                axes[name] = {
                    "unit": "degrees",
                    "min": control_range["min"] / self.ARCSECONDS_PER_DEGREE,
                    "max": control_range["max"] / self.ARCSECONDS_PER_DEGREE,
                }
            else:
                axes[name] = {"unit": "native", **control_range}
        with self.lock:
            return {"supported": self.supported, "axes": axes, "commanded": dict(self.commanded) or None}

    def move(self, pan: float | None = None, tilt: float | None = None, zoom: int | None = None) -> dict:
        with self.lock:
            if pan is not None or tilt is not None:
                self._aim(pan, tilt)
            if zoom is not None:
                if not self.ranges["zoom"]:
                    raise ValueError("this camera has no zoom control")
                snapped = self._snap("zoom", zoom)
                self.device.set_control(self.ZOOM, snapped)
                self.commanded["zoom"] = snapped
        return self.status()

    def _aim(self, pan: float | None, tilt: float | None) -> None:
        """Pan and tilt are one shared UVC control whose readback the
        Link 2 corrupts, so both axes are always written together, the
        unrequested one filled from the last commanded pose (0° when the
        pose was never commanded and is therefore unknowable)."""
        if not (self.ranges["pan"] and self.ranges["tilt"]):
            self._aim_single_axis(pan, tilt)
            return
        pan = self.commanded.get("pan", 0.0) if pan is None else pan
        tilt = self.commanded.get("tilt", 0.0) if tilt is None else tilt
        pan_units = self._snap("pan", pan * self.ARCSECONDS_PER_DEGREE)
        tilt_units = self._snap("tilt", tilt * self.ARCSECONDS_PER_DEGREE)
        self.device.set_controls({self.PAN: pan_units, self.TILT: tilt_units})
        self.commanded["pan"] = pan_units / self.ARCSECONDS_PER_DEGREE
        self.commanded["tilt"] = tilt_units / self.ARCSECONDS_PER_DEGREE

    def _aim_single_axis(self, pan: float | None, tilt: float | None) -> None:
        for name, control_id, degrees in (("pan", self.PAN, pan), ("tilt", self.TILT, tilt)):
            if degrees is None:
                continue
            if not self.ranges[name]:
                raise ValueError(f"this camera has no {name} control")
            units = self._snap(name, degrees * self.ARCSECONDS_PER_DEGREE)
            self.device.set_control(control_id, units)
            self.commanded[name] = units / self.ARCSECONDS_PER_DEGREE

    def recenter(self) -> dict:
        zoom_default = self.ranges["zoom"]["default"] if self.ranges["zoom"] else None
        return self.move(
            pan=0 if self.ranges["pan"] else None,
            tilt=0 if self.ranges["tilt"] else None,
            zoom=zoom_default,
        )

    def _snap(self, name: str, value: float) -> int:
        control_range = self.ranges[name]
        step = control_range["step"] or 1
        clamped = max(control_range["min"], min(control_range["max"], value))
        return int(control_range["min"] + round((clamped - control_range["min"]) / step) * step)


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
        capture_devices = [d for d in devices if V4L2Device(str(d)).is_capture_device()]
        if not capture_devices:
            raise SystemExit(
                "No capture-capable /dev/video* device found — plug in a webcam or set EYES_DEVICE."
            )
        return str(capture_devices[-1])

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
    ptz: GimbalPTZ

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
  #ptz { font-size: 0.85em; color: #8b98a5; display: flex; align-items: center; gap: 10px; }
  #ptz[hidden] { display: none; }
  #recenter { background: #1e2833; color: #d8dee6; border: 1px solid #31404f; border-radius: 8px;
              padding: 3px 12px; font-size: 0.9em; cursor: pointer; }
  #recenter:disabled { opacity: 0.6; cursor: wait; }
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
<div id="ptz" hidden><span id="ptzText"></span><button id="recenter">Recenter</button></div>
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

  const ptz = document.getElementById('ptz');
  const ptzText = document.getElementById('ptzText');
  const recenter = document.getElementById('recenter');
  let ptzSupported = null;
  const showPtz = data => {
    ptzSupported = data.supported;
    if (!data.supported) return;
    ptz.hidden = false;
    const pose = data.commanded || {};
    const axis = (name, unit) => name in pose ? pose[name] + unit : '?';
    ptzText.textContent = 'gimbal: pan ' + axis('pan', '\\u00b0') + ' \\u00b7 tilt '
        + axis('tilt', '\\u00b0') + ' \\u00b7 zoom ' + axis('zoom', '');
  };
  setInterval(async () => {
    if (ptzSupported === false) return;
    try { showPtz(await (await fetch('/ptz', {cache: 'no-store'})).json()); } catch (e) {}
  }, 2000);
  recenter.addEventListener('click', async () => {
    recenter.disabled = true;
    try {
      const response = await fetch('/ptz', {method: 'POST', body: JSON.stringify({recenter: true})});
      showPtz(await response.json());
    } finally { recenter.disabled = false; }
  });

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
        elif self.path.startswith("/ptz"):
            self.send_json(json.dumps(self.ptz.status()).encode())
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
        elif self.path.startswith("/ptz"):
            self.handle_ptz()
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

    def handle_ptz(self) -> None:
        if not self.ptz.supported:
            self.send_error(501, "this camera has no pan/tilt/zoom controls")
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(request, dict):
                raise ValueError("body must be a JSON object")
            if self.is_claude():
                self.activity.set("aiming the camera…")
            if request.get("recenter"):
                result = self.ptz.recenter()
            else:
                result = self.ptz.move(
                    pan=request.get("pan"),
                    tilt=request.get("tilt"),
                    zoom=request.get("zoom"),
                )
        except (ValueError, TypeError) as error:
            self.send_error(400, str(error))
            return
        except OSError as error:
            self.send_error(503, f"camera rejected the move: {error}")
            return
        self.send_json(json.dumps(result).encode())

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
        self.ptz = GimbalPTZ(V4L2Device(self.feed.device))

    def run(self) -> None:
        threading.Thread(target=self.feed.run_forever, daemon=True).start()
        EyesRequestHandler.feed = self.feed
        EyesRequestHandler.observations = self.observations
        EyesRequestHandler.activity = self.activity
        EyesRequestHandler.ptz = self.ptz
        server = ThreadingHTTPServer(("0.0.0.0", self.port), EyesRequestHandler)
        gimbal = "gimbal PTZ available" if self.ptz.supported else "no gimbal"
        print(f"Claude's Eyes on http://localhost:{self.port}/ (camera {self.feed.device}, {gimbal})", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    EyesServer().run()
