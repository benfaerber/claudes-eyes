---
name: look
description: Look through the user's desk webcam (Claude's Eyes). Use when the user types "l", says "look", "take a look", "what do you see", "check the camera", asks about a physical object on their desk or in front of the camera, or wants the eyes server started, paused, or resumed.
allowed-tools: Bash(curl -s http://localhost:8990/*), Bash(uv run ${CLAUDE_PLUGIN_ROOT}/eyes_server.py:*)
---

# Claude's Eyes — look through the desk webcam

One local server owns the webcam and shares it: the user watches the live
dashboard at <http://localhost:8990/> while you fetch frames on demand. A bare
"l" from the user means "look now".

## Ensure the server is running

```sh
curl -s http://localhost:8990/status
```

If the connection is refused, start the server in the background and keep it
running for the rest of the session:

```sh
uv run ${CLAUDE_PLUGIN_ROOT}/eyes_server.py
```

It needs `uv` and `ffmpeg` installed, nothing else. On first start, tell the
user the dashboard is at <http://localhost:8990/> (it binds all interfaces, so
a phone on the same LAN works too). Only one process may own a camera — if
frames never arrive, a preview app may be holding the device.

Configuration is by environment variable, set when launching the server:

- `EYES_DEVICE` — camera node (default: the newest `/dev/video*`)
- `EYES_ROTATE` — `0`, `90`, `180`, or `270` degrees clockwise (default `0`;
  use `180` for an upside-down mount)
- `EYES_PORT` — HTTP port (default `8990`; the URLs in this skill assume it)

## Narrate everything you do — this is not optional

The dashboard is the user's only window into what you're doing with their
camera, so every use of the Eyes must be reported there, not just in chat:

- **Every action** (a frame fetch that's part of a longer task, a crop, a
  gimbal move, a pause) gets an activity line via `POST /activity` *before*
  you do it, e.g. `aiming 10° left to see the printer's side…`. Frame
  fetches announce themselves automatically; everything else is on you.
- **Every look ends with an observation** via `POST /observation`, even when
  the answer is "nothing changed" or the look failed — say what happened.
- **Every gimbal session ends with an observation** that says where you left
  the camera (recentered, or restored to the starting pose).

A look with no observation posted, or a move with no activity line, is a
bug in your behaviour: the user sees the camera do things with no
explanation.

## The look loop

1. **Fetch a frame** into your scratchpad directory (shown as `<scratchpad>`
   here — substitute your session's real path), then Read the image:

   ```sh
   curl -sf http://localhost:8990/frame.jpg -o <scratchpad>/frame.jpg
   ```

   Fetching with curl automatically shows "Claude: downloaded a frame, having
   a look…" on the dashboard — the server tells you apart from browsers by
   user agent. The `-f` matters: the server refuses to serve a frame it can't
   vouch for as current (paused, not captured yet, or **expired** — older
   than 10 s), and `-f` keeps that 503 from being written over `frame.jpg` as
   if it were an image. If curl exits non-zero, `curl -s
   http://localhost:8990/status` says why (`paused`, and `frame.expired` with
   `frame.age`); see Failure modes.

2. **Zoom when detail matters** (reading labels, measuring against known
   objects): crop the JPEG locally and Read the crop.

   ```sh
   ffmpeg -y -i <scratchpad>/frame.jpg -vf "crop=W:H:X:Y" <scratchpad>/crop.jpg
   ```

   On gimbal cameras, real optical zoom via `/ptz` (below) beats cropping.

3. **Narrate longer tasks** by posting progress lines mid-look; they replace
   the dashboard's activity line:

   ```sh
   curl -s http://localhost:8990/activity --data 'measuring the envelope against the tape roll…'
   ```

4. **Finish every look with an observation.** Answer the user in chat as
   normal, and post a short one-line version to the dashboard's speech bubble
   (this also clears the activity line). Observations expire from the bubble
   after 10 minutes, so each look needs its own — never rely on an earlier
   one still being shown:

   ```sh
   curl -s http://localhost:8990/observation --data 'hmm, I see a padded envelope, roughly A5 sized'
   ```

## Aiming the camera (gimbal cameras)

On a gimbal camera (Insta360 Link 2 and similar) you can physically aim your
own eyes. Check support and the current pose first:

```sh
curl -s http://localhost:8990/ptz
```

`supported: false` (or a 501 from POST) means a fixed camera — crop instead.
`paused: true` means the user has paused movement from the dashboard: every
aim gets a 503 "movement paused", so don't try — crop instead, and never
POST `/ptz/resume` unless the user asks. Otherwise `axes` gives each axis's
range and `commanded` the last commanded pose (`null` right after a server
start: the pose is unknown because such cameras report garbage when read, so
the server only trusts what it has itself commanded).

Aim with any subset of `pan` (degrees, positive pans the view right), `tilt`
(degrees, positive tilts up), and `zoom` (the camera's native units — see
`axes.zoom` for the range; on the Link 2, 100–400 means 1x–4x):

```sh
curl -s -X POST http://localhost:8990/ptz --data '{"pan": 15, "tilt": 5}'
curl -s -X POST http://localhost:8990/ptz --data '{"zoom": 300}'
curl -s -X POST http://localhost:8990/ptz --data '{"recenter": true}'
```

Rules of the loop:

- **Announce every move first** with `POST /activity` (what you're aiming at
  and why), and note in your final observation where the camera ended up.
- **Wait ~2 seconds after a move** before fetching a frame; the gimbal is
  physical and the next frame may still show motion blur.
- **Iterate visually**: move, fetch, look, correct. Pan and tilt are absolute,
  so small corrections are cheap.
- **When only one of pan/tilt is given, the other is re-commanded** from the
  tracked pose (0 if unknown). Right after a server start, expect the first
  aim command to also square up the axis you didn't mention.
- **Leave the camera roughly where you found it**: recenter (which also
  resets zoom) or restore the pose you started from when you finish a task
  that moved it, and say so in your observation. The user can also hit
  Recenter on the dashboard.

## Failure modes

Errors are JSON: `{"error": "..."}` with the matching HTTP status.

- `/frame.jpg` returns 503 "eyes paused" — the user paused capture from the
  dashboard. Say so and stop looking; never POST `/resume` unless the user
  asks.
- 503 "no frame captured yet" or 503 "frame expired: last frame is Ns old"
  — the camera is warming up, was disconnected, or is held by another app.
  Frames older than 10 s are never served, so you can't accidentally
  describe a stale picture. The server retries the camera every few seconds:
  wait briefly and fetch again, and if it keeps failing tell the user the
  camera looks offline.
- `POST /ptz` returns 503 "movement paused" — the user froze the gimbal from
  the dashboard. Don't retry and never POST `/ptz/resume` unless the user
  asks; crop the frame locally instead.
- Connection refused — the server isn't running; start it as above.
