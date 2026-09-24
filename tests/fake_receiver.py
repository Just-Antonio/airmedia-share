"""A stand-in AirMedia receiver for tests: the TV side of the protocol, on aiortc.

It enforces what the real AM-3200 taught us, so a regression fails here first:
  - the on-screen code must match;
  - a sender that does not present a browser userAgent gets its media ignored
    ("shown" stays False even though frames arrive), as the real TV does.
"""

import asyncio
import json

import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription

TV_INDEX = 101
NAME = "FAKE-AM3200"


class FakeReceiver:
    def __init__(self, code="1234"):
        self.code = code
        self.video_frames = 0
        self.audio_frames = 0
        self.shown = False
        self.got_bye = False
        self.offer_sdp = None
        self.user_agent = None
        self.server = None
        self.port = None
        self._pcs = []

    async def start(self):
        self.server = await websockets.serve(self._handle, "127.0.0.1", 0,
                                             subprotocols=["com.splashtop.webrtc2"])
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self):
        for pc in self._pcs:
            await pc.close()
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, ws):
        client = None

        async def forward(data):
            await ws.send(json.dumps({"id": "forward", "type": "webrtc/0.5",
                                      "index": TV_INDEX, "toIndex": client, "data": data}))

        async for raw in ws:
            msg = json.loads(raw)
            if msg["id"] == "getsession":
                client = 555
                await ws.send(json.dumps({"id": "getsession", "type": "webrtc/0.5", "ack": {
                    "hosts": [TV_INDEX], "index": client, "name": NAME,
                    "res": {"w": 1920, "h": 1080}, "security": 1,
                    "value": msg["req"]["value"]}}))
                continue
            data = msg.get("data", {})
            kind = data.get("type")
            if kind == "authenticate":
                self.user_agent = data.get("userAgent", "")
                ok = data.get("value") == self.code
                await forward({"type": "authorize", "code": 200 if ok else 401,
                               "msg": "ok" if ok else "auth fail"})
            elif kind == "offer":
                self.offer_sdp = data["sdp"]
                pc = RTCPeerConnection()
                self._pcs.append(pc)
                self.shown = self.user_agent.startswith("Mozilla/5.0")

                @pc.on("track")
                def on_track(track):
                    async def drain():
                        while True:
                            try:
                                await track.recv()
                            except Exception:
                                return
                            if track.kind == "video":
                                self.video_frames += 1
                            else:
                                self.audio_frames += 1
                    asyncio.ensure_future(drain())

                await pc.setRemoteDescription(RTCSessionDescription(data["sdp"], "offer"))
                await pc.setLocalDescription(await pc.createAnswer())
                await forward({"type": "answer", "sdp": pc.localDescription.sdp})
            elif kind == "heartbeat":
                await forward({"type": "heartbeat"})
            elif kind == "bye":
                self.got_bye = True
