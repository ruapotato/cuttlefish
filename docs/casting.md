# Casting / Multi-device session control

> **Status: planned, not implemented.** This document is design notes, not
> a built feature. Implementing it cleanly requires a real architecture
> conversation about UX edge cases that haven't happened yet.

## Goal

> "Duplicate the casting feature where you can log into the same account on
> multiple devices and control the other sessions. For example, you can on
> the TV computer play a show and then on your cell phone control that show."

A user logged into cuttlefish on multiple devices simultaneously can
designate any of them as a *playback target* and any as a *controller*. The
controller's actions (play, pause, seek, change track) execute on the
target.

## Why this is harder than it looks

Three things have to be true simultaneously:

1. **Targets must announce themselves** as available, and continue to
   confirm liveness.
2. **Controllers must discover** active targets in real time, and notice
   when they go away.
3. **Commands must round-trip with low latency** — sub-200ms for a pause
   button to feel responsive.

This is a websocket pub/sub problem, not an HTTP API problem.

## Architecture sketch

### Session model

Augment `sessions` with a `device_label` (e.g. "Living Room TV", "iPhone")
and a `last_seen_at` timestamp. When a device opens cuttlefish, it
heartbeats every ~30s.

```sql
ALTER TABLE sessions ADD COLUMN device_label TEXT;
ALTER TABLE sessions ADD COLUMN last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE sessions ADD COLUMN can_be_target INTEGER NOT NULL DEFAULT 1;
```

### WebSocket channel

A new endpoint `WS /api/cast/channel` does two things:

- **Identify**: the device sends `{type: "identify", session_token, role: "target" | "controller"}` on connect.
- **Subscribe**: the device receives a stream of events scoped to its user_id.

Events on the bus:

```json
{ "type": "target_available", "session_token": "...", "device_label": "Living Room TV" }
{ "type": "target_gone",      "session_token": "..." }
{ "type": "command",          "to": "<session_token>", "action": "play|pause|seek|load",
  "payload": { ... } }
{ "type": "state_update",     "from": "<session_token>",
  "media_id": 17, "position_seconds": 412.3, "playing": true }
```

The server keeps an in-memory map `{user_id: {session_token: websocket}}` and
fans events out to the matching connections. No persistence — if cuttlefish
restarts, devices reconnect and re-announce.

### Target endpoint behavior

The `/watch/{media_id}` page detects whether it's been opened in
"target mode" (e.g. via `?as_target=1` query param when launched from a
controller) and:

1. Connects to `/api/cast/channel` as `role=target`.
2. Listens for `command` events; applies them to the local `<video>`
   element (`video.play()`, `video.currentTime = ...`, etc.).
3. Periodically emits `state_update` events with the current playback
   position so controllers can show a sync'd progress bar.

### Controller UI

A small "cast" button on the watch page lists the user's other live
sessions (from the `target_available` events). Tapping one switches the
phone's UI from "watching" to "remote controlling": the video element is
hidden, replaced with a transport-controls UI that emits `command` events.

## Open design questions

These need user input before code:

- **Target consent.** Should a freshly-opened TV silently become a target,
  or should it require an explicit "this device can be controlled by other
  sessions" confirmation? (Probably explicit, with persistence in the DB.)
- **Hand-off vs. mirror.** When a controller starts playback, does the
  controller stop being a player too? Or does it keep a synced view?
- **Picking the target on launch.** If the user is on their phone and
  taps "play" on a movie, do we ask them where to play it, or default to
  whatever was the last target?
- **Audiobook chapter selection** is its own UI surface; treat as
  out-of-scope for the initial casting MVP.
- **Roku / smart TV integration.** Roku has a separate ECP / SDK. Doing
  "cast to Roku" properly is a meaningful chunk of work — the MVP can
  scope to "browser tab on the TV is the target." Native Roku support is
  a separate follow-on.

## Why this is deferred

- Requires non-trivial server architecture (websocket bus + in-memory
  state + per-user fan-out) that we don't need for any other feature.
- The user-facing UX is genuinely subtle (see open questions above) and
  benefits from being designed against a working single-device app first.
- It's a "nice-to-have" relative to the core "scan / encode / stream /
  watch" loop. Build that first, then revisit.
