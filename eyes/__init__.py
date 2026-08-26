"""Claude's Eyes as a Flask application.

EyesState owns the hardware and the logs; create_app wraps it in routes.
The entrypoint (eyes_server.py at the repo root) builds one of each.
"""

import threading
from pathlib import Path

from flask import Flask

from .camera import CameraFeed, GimbalPTZ, V4L2Device
from .logs import ClaudeActivity, EventLog, ObservationLog


class EyesState:
    """Everything the server knows: the camera, the gimbal, and the logs."""

    def __init__(self) -> None:
        frame_path = Path.home() / ".cache" / "claudes-eyes" / "latest.jpg"
        frame_path.parent.mkdir(parents=True, exist_ok=True)
        self.feed = CameraFeed(frame_path)
        self.ptz = GimbalPTZ(V4L2Device(self.feed.device))
        self.observations = ObservationLog()
        self.activity = ClaudeActivity()
        self.events = EventLog()

    def start_capture(self) -> None:
        threading.Thread(target=self.feed.run_forever, daemon=True).start()
        self.events.add("start", f"server started on camera {self.feed.device}")


def create_app(state: EyesState) -> Flask:
    app = Flask(__name__)
    app.config["EYES"] = state

    from .routes import bp

    app.register_blueprint(bp)
    return app
