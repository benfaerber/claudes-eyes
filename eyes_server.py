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
    http://localhost:8990/events        audit log: looks, moves, observations

The app itself lives in the eyes/ package (Flask, camera, logs, templates);
this file is just the entrypoint uv runs.

Run:  make run   (uv supplies Python and Flask; ffmpeg must be installed)
Stop: Ctrl+C
"""

# /// script
# requires-python = ">=3.10"
# dependencies = ["flask>=3"]
# ///

import logging
import os

from eyes import EyesState, create_app


def main() -> None:
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    port = int(os.environ.get("EYES_PORT", "8990"))
    state = EyesState()
    app = create_app(state)
    state.start_capture()
    gimbal = "gimbal PTZ available" if state.ptz.supported else "no gimbal"
    print(f"Claude's Eyes on http://localhost:{port}/ (camera {state.feed.device}, {gimbal})", flush=True)
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
