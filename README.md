# Claude's Eyes

A shared desk webcam for pair-working with Claude Code. One small server owns
the camera and serves frames over HTTP, so Ben's browser and Claude can both
look at the same time — no fighting over the video device.

```
Ben's browser ──▶ http://localhost:8990/           live dashboard
Claude        ──▶ http://localhost:8990/frame.jpg  grabs a frame per "look"
Claude        ──▶ POST /observation                "hmm, I see …" → speech bubble
```

![The dashboard: live view with Claude's narration](images/claudes-eyes.png)

Real sessions it has carried: Claude sizing an envelope against known desk
objects, and closing a full design → print → verify loop on a label printer,
narrating each step.

| ![Measuring an envelope](images/envelope-measuring.png) | ![Design, print, verify](images/design-print-verify.png) |
| --- | --- |

## Setup

1. **Plug in a webcam.** Find its device node: it's the newest `/dev/videoN`
   (check `ls -la /dev/video*` timestamps). If it isn't `/dev/video2`, change
   `CameraFeed.DEVICE` in `eyes_server.py`.
2. **Install [uv](https://docs.astral.sh/uv/) and ffmpeg** — the only two
   system dependencies. The script itself is stdlib-only Python; uv supplies
   the interpreter.
3. **Run it:**

   ```sh
   make run
   ```

4. Open <http://localhost:8990/> — you should see the live view within a few
   seconds. It binds all interfaces, so a phone on the same LAN works too.

Mounted the camera upside-down? The server rotates frames 180° in ffmpeg
(`transpose=1,transpose=1` in `CameraFeed`) — remove that filter for a
right-side-up mount.

Only one process may own a camera: close any preview app before starting the
server, and never open `/dev/videoN` directly while it runs. If the camera is
unplugged or stolen by another app, the server reopens it automatically every
few seconds.

## The dashboard

- **Live view** at ~4 fps with a staleness indicator (turns amber if frames
  stop) and shimmer placeholders while the camera warms up or reopens.
- **Claude activity** — a pulsing blue line ("Claude: downloaded a frame,
  having a look…") appears the moment Claude fetches a frame, updates as
  Claude posts progress, and clears when the observation lands. Claude is
  told apart from browsers by user-agent, so the dashboard's own polling
  never triggers it.
- **Speech bubble + history** — Claude's latest "hmm, I see …" with older
  remarks below; "show older" grows the list (server keeps the last 200,
  in memory only — a restart clears the log).
- **Pause Eyes** — kills the capture process outright (a real privacy pause:
  nothing is recorded, and even Claude's `/frame.jpg` gets a 503 until you
  resume).

## API

| Route                | Method | What                                            |
| -------------------- | ------ | ----------------------------------------------- |
| `/`                  | GET    | the dashboard                                   |
| `/frame.jpg`         | GET    | latest frame; `X-Frame-Age` header in seconds   |
| `/observations`      | GET    | narration log as JSON, newest first             |
| `/observation`       | POST   | plain-text note → speech bubble, clears activity|
| `/activity`          | POST   | plain-text progress line (empty body clears)    |
| `/status`            | GET    | `{paused, activity}`                            |
| `/pause`, `/resume`  | POST   | stop/restart capture                            |

## Working with Claude

Tell Claude the server is running (or let it start the server itself) and
that `l` means "look now". On each look Claude fetches a frame, zooms by
cropping the JPEG locally when detail matters, replies in chat, and posts the
short version to the dashboard. This protocol lives in Claude's CLAUDE.md /
the repo's AGENTS.md so new sessions pick it up automatically.
