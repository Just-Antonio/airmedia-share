# The AirMedia sender protocol, as this app speaks it

Crestron's "AirMedia Sender" Chrome extension (ID `ljophmlbljnjodcbogmdogcpclifenpk`,
v1.0.2.7) is built on Splashtop's Mirroring360. Its signaling code
(`singalchannel.js`, `peerconnection.js`) is readable JavaScript. Everything below was
derived from that code and then checked against an AM-3200 in September 2026.

## Transport

- WebSocket, **plain `ws://<receiver>:7300`**, subprotocol `com.splashtop.webrtc2`.
- Messages are JSON with `"type": "webrtc/0.5"`.
- Messages between the two peers are wrapped as
  `{"id": "forward", "index": <from>, "toIndex": <to>, "data": {...}}`.

## Sequence

```
sender                                              receiver
  getsession {req: {product: "smx", version: "1.0.2.7", platform: "linux",
              uuid, useragent: "smx/1.0.2.7 splashtop2 chrome linux",
              value: "bc75c746-6e64-3a54-a5ba-1b0983df9125"}}
                                      <- getsession ack {index, hosts[], name,
                                                          res {w,h}, security}
                                         (the TV now puts a 4-digit code on screen)
  forward authenticate {value: <code>, userAgent: <browser UA>}
                                      <- forward authorize {code: 200 | 401 | 403 | 404 | 408}
  forward offer {sdp, res}            ->
                                      <- forward answer {sdp}
                                      <- forward candidate {candidate, id, label}  (trickle)
  forward heartbeat  (every 5 s)      <-> forward heartbeat
  forward bye                         ->
```

`value` in `getsession` is the extension's hard-coded `config_uuid`; the receiver echoes
it back. `name` is the receiver's own name (e.g. `ROOM-101-AM3200`). `res` is the
resolution it wants.

## Things that cost hours

1. **The code appears only after `getsession`, and it is per connection.** Asking for
   the code before connecting, or reusing one, fails. The sender must connect, then ask
   the person, while keeping the WebSocket alive with heartbeats.

2. **`authenticate.userAgent` must look like a browser.** With
   `smx/… splashtop2 chrome linux` the TV authorized the sender and accepted the media:
   ICE and DTLS came up, and it even sent REMB bandwidth estimates. But it never put the
   picture on screen. A Chrome `navigator.userAgent`
   (`Mozilla/5.0 (X11; Linux x86_64) … Chrome/… Safari/537.36`) makes it show.

3. **Other senders' `bye` messages are broadcast.** A `bye` whose `index` isn't the
   receiver's belongs to someone else (e.g. a lingering browser extension session). It
   must not end our session.

4. **Old WebRTC on the receiver.** The answer uses `a=msid-semantic: WMS` and
   `RTP/SAVPF`, i.e. a Plan-B-era libwebrtc. The offer is rewritten the way the
   extension rewrites Chrome's:
   - `UDP/TLS/RTP/SAVPF` → `RTP/SAVPF`
   - RTX removed
   - per-SSRC `msid`/`mslabel`/`label` lines added, as Chrome of that era sent them
   - audio m-line before video (Chrome's `addStream` order)

   The receiver answered with VP8 as its first choice and also accepts H264 and Opus.

5. **Timestamps must never go backwards.** aiortc's `MediaPlayer` restarts `pts` at 0
   for each new capture. After switching the shared source, the TV held the picture
   until the new timeline "caught up", about as long as the previous source had been
   shared (30–45 s). All frames are now stamped from one monotonic 90 kHz clock.
   Swapping the capture behind a single persistent track also avoids aiortc ending its
   send loop when a replaced track stops.

6. **Constant frame size.** Letterboxing every source into 1920×1080 keeps the
   receiver's decoder from reinitializing on every switch or window resize.

## Audio

- Recorded with `parec`, because PyAV's bundled ffmpeg has no PulseAudio input.
- The whole screen records the default sink's monitor; one window records
  `--monitor-stream=<sink input>` for each stream whose `application.process.id` is the
  window's `_NET_WM_PID` or a child of it.
- Streams are mixed on a 20 ms clock into 48 kHz stereo Opus frames.
- The monitor is taken before the sink's hardware volume, so the level sent to the TV
  is independent of the computer's speakers. The app applies its own slider, plus the
  system volume if asked, with PulseAudio's cubic curve.

## Bandwidth

The AM-3200's REMB estimate reached about 24 Mbps on a wired campus LAN. aiortc's VP8
defaults (0.5–1.5 Mbps) are made for webcams. This app uses 2–20 Mbps, adjustable with
`max_mbps` in settings.
