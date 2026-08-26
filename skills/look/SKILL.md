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

## The look loop

1. **Fetch a frame** into your scratchpad directory (shown as `<scratchpad>`
   here — substitute your session's real path), then Read the image:

   ```sh
   curl -s http://localhost:8990/frame.jpg -o <scratchpad>/frame.jpg
   ```

   Fetching with curl automatically shows "Claude: downloaded a frame, having
   a look…" on the dashboard — the server tells you apart from browsers by
   user agent.

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
   (this also clears the activity line):

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
Otherwise `axes` gives each axis's range and `commanded` the last commanded
pose (`null` right after a server start: the pose is unknown because such
cameras report garbage when read, so the server only trusts what it has
itself commanded).

Aim with any subset of `pan` (degrees, positive pans the view right), `tilt`
(degrees, positive tilts up), and `zoom` (the camera's native units — see
`axes.zoom` for the range; on the Link 2, 100–400 means 1x–4x):

```sh
curl -s -X POST http://localhost:8990/ptz --data '{"pan": 15, "tilt": 5}'
curl -s -X POST http://localhost:8990/ptz --data '{"zoom": 300}'
curl -s -X POST http://localhost:8990/ptz --data '{"recenter": true}'
```

Rules of the loop:

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

- `/frame.jpg` returns 503 "eyes paused" — the user paused capture from the
  dashboard. Say so and stop looking; never POST `/resume` unless the user
  asks.
- 503 "no frame captured yet", or the `X-Frame-Age` response header exceeds
  ~10 seconds — the camera is warming up or was disconnected; the server
  retries the camera every few seconds, so wait briefly and fetch again.
- Connection refused — the server isn't running; start it as above.
