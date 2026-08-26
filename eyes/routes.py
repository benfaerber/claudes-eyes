"""The HTTP layer: same API the stdlib server spoke, now as a blueprint.

Claude is told apart from browsers by user agent, so the dashboard's own
polling never shows up as Claude activity or audit-log looks.
"""

from flask import Blueprint, Response, abort, current_app, jsonify, render_template, request

bp = Blueprint("eyes", __name__)


def state():
    return current_app.config["EYES"]


def is_claude() -> bool:
    return not request.headers.get("User-Agent", "").startswith("Mozilla")


def actor() -> str:
    return "Claude" if is_claude() else "the dashboard"


@bp.get("/")
def dashboard():
    return render_template("dashboard.html")


@bp.get("/frame.jpg")
def frame():
    eyes = state()
    if eyes.feed.is_paused():
        abort(503, description="eyes paused")
    age = eyes.feed.frame_age_seconds()
    if age is None:
        abort(503, description="no frame captured yet")
    if is_claude():
        eyes.activity.set("downloaded a frame, having a look…")
        eyes.events.add("look", "Claude fetched a frame", coalesce=True)
    return Response(
        eyes.feed.frame_path.read_bytes(),
        mimetype="image/jpeg",
        headers={"Cache-Control": "no-store", "X-Frame-Age": f"{age:.1f}"},
    )


@bp.get("/observations")
def observations():
    return jsonify(state().observations.as_dict())


@bp.get("/status")
def status():
    eyes = state()
    return jsonify({"paused": eyes.feed.is_paused(), "activity": eyes.activity.snapshot()})


@bp.get("/ptz")
def ptz_status():
    return jsonify(state().ptz.status())


@bp.get("/events")
def events():
    return jsonify(state().events.as_dict())


@bp.post("/observation")
def observation():
    eyes = state()
    text = request.get_data(as_text=True).strip()
    if not text:
        abort(400, description="empty observation")
    eyes.observations.add(text)
    eyes.activity.clear()
    eyes.events.add("observation", text)
    return jsonify(ok=True)


@bp.post("/activity")
def activity():
    eyes = state()
    text = request.get_data(as_text=True).strip()
    if not text:
        eyes.activity.clear()
    else:
        eyes.activity.set(text)
        eyes.events.add("activity", text)
    return jsonify(ok=True)


@bp.post("/ptz")
def ptz_move():
    eyes = state()
    if not eyes.ptz.supported:
        abort(501, description="this camera has no pan/tilt/zoom controls")
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        abort(400, description="body must be a JSON object")
    try:
        if is_claude():
            eyes.activity.set("aiming the camera…")
        if body.get("recenter"):
            result = eyes.ptz.recenter()
            eyes.events.add("move", f"{actor()} recentered the camera")
        else:
            result = eyes.ptz.move(pan=body.get("pan"), tilt=body.get("tilt"), zoom=body.get("zoom"))
            asked = ", ".join(
                f"{axis} {body[axis]}" for axis in ("pan", "tilt", "zoom") if body.get(axis) is not None
            )
            eyes.events.add("move", f"{actor()} aimed: {asked}")
    except (ValueError, TypeError) as error:
        abort(400, description=str(error))
    except OSError as error:
        abort(503, description=f"camera rejected the move: {error}")
    return jsonify(result)


@bp.post("/pause")
def pause():
    eyes = state()
    eyes.feed.pause()
    eyes.events.add("pause", f"eyes paused by {actor()}")
    return jsonify(ok=True, paused=True)


@bp.post("/resume")
def resume():
    eyes = state()
    eyes.feed.resume()
    eyes.events.add("resume", f"eyes resumed by {actor()}")
    return jsonify(ok=True, paused=False)
