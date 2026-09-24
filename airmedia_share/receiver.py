"""Talk to a Crestron AirMedia receiver the way Crestron's own browser sender does.

The receiver speaks Splashtop's Mirroring360 protocol, which Crestron's "AirMedia Sender"
Chrome extension (v1.0.2.7) implements in JavaScript: JSON messages over a WebSocket on
port 7300 (subprotocol com.splashtop.webrtc2), then an ordinary WebRTC session in which
the sender makes the offer. This module is the sender half of that exchange, on aiortc:

    getsession  -> ack {index, hosts, name, res, security}   (the TV now shows a code)
    forward authenticate {value: <code>, userAgent}  -> forward authorize {code: 200|401|...}
    forward offer {sdp, res}  -> forward answer {sdp};  candidates both ways
    forward heartbeat every few seconds; forward bye to stop

Message shapes follow the extension's singalchannel.js / peerconnection.js. See
docs/protocol.md for what was learned the hard way.
"""

import asyncio
import json
import logging
import secrets
import time

import aiortc.codecs.h264 as h264
import aiortc.codecs.vpx as vpx
import websockets
from aiortc import RTCConfiguration, RTCPeerConnection, RTCRtpSender, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp

log = logging.getLogger(__name__)

PORT = 7300
API = "webrtc/0.5"
# config_uuid in the extension. The receiver echoes it back as the session "value".
SESSION_ID = "bc75c746-6e64-3a54-a5ba-1b0983df9125"
CLIENT_VERSION = "1.0.2.7"
USER_AGENT = f"smx/{CLIENT_VERSION} splashtop2 chrome linux"
# What the extension puts in "authenticate": the browser's navigator.userAgent.
BROWSER_USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")
HEARTBEAT_S = 5
# The extension gives up after 20 s without hearing from the receiver.
HEARTBEAT_TIMEOUT_S = 20



def set_bitrate_range(min_bps=2_000_000, start_bps=10_000_000, max_bps=20_000_000):
    """aiortc's caps suit a webcam call; a desktop full of small text needs far more.

    An AM-3200's bandwidth estimate (REMB) reached ~24 Mbps on a campus LAN. These are
    module constants in aiortc, read each time an encoder is (re)configured.
    """
    for codec in (vpx, h264):
        codec.MIN_BITRATE, codec.DEFAULT_BITRATE, codec.MAX_BITRATE = min_bps, start_bps, max_bps


set_bitrate_range()


class AirMediaError(Exception):
    pass


class WrongCode(AirMediaError):
    pass


class ReceiverBusy(AirMediaError):
    pass


class Cancelled(AirMediaError):
    pass


def _chrome_style_sdp(sdp):
    """Apply the same two rewrites the extension applies to Chrome's offer.

    peerconnection.js strips "UDP/TLS/" from the m-line profile and drops RTX; the
    receiver was built against that shape, so match it rather than find out otherwise.
    """
    lines = []
    rtx = set()
    for line in sdp.split("\r\n"):
        if line.startswith("a=rtpmap:") and " rtx/" in line:
            rtx.add(line[len("a=rtpmap:"):].split()[0])
            continue
        if line.startswith("a=fmtp:") and "apt=" in line:
            continue
        lines.append(line.replace("UDP/TLS/RTP/SAVPF", "RTP/SAVPF"))
    out = []
    msid = None
    for line in lines:
        if line.startswith("m="):
            msid = None
        if line.startswith("a=msid:"):
            msid = line[len("a=msid:"):].split()
        if line.startswith("a=ssrc:") and " cname:" in line and msid:
            # The receiver's answer ("a=msid-semantic: WMS", RTP/SAVPF) is an old
            # Plan B libwebrtc, which names incoming streams from these per-SSRC
            # lines. aiortc omits them; Chrome's offers of that era carried them.
            ssrc = line[len("a=ssrc:"):].split()[0]
            out.append(line)
            out.append(f"a=ssrc:{ssrc} msid:{msid[0]} {msid[1]}")
            out.append(f"a=ssrc:{ssrc} mslabel:{msid[0]}")
            out.append(f"a=ssrc:{ssrc} label:{msid[1]}")
            continue
        if line.startswith("m=") and rtx:
            head, fmts = line.split(" ", 3)[:3], line.split(" ", 3)[3].split()
            line = " ".join(head + [f for f in fmts if f not in rtx])
        elif any(line.startswith(p) and line[len(p):].split()[0] in rtx
                 for p in ("a=rtcp-fb:",)):
            continue
        out.append(line)
    return "\r\n".join(out)


class Session:
    """One presentation to one receiver. run() returns when the session ends."""

    def __init__(self, host, get_code, video_track, audio_track, on_state=None,
                 video_codec=None):
        """get_code(retry: bool) is awaited for the on-screen code; None cancels.

        The TV only puts a code on screen once a sender has asked for a session, and
        each connection gets a fresh one, so it cannot be asked for up front.
        """
        self.host = host
        self.get_code = get_code
        self.video_codec = video_codec
        self._hb = None
        self._pumping = False
        self.video = video_track
        self.audio = audio_track
        self.on_state = on_state or (lambda state, detail="": None)
        self.receiver_name = None
        self._ws = None
        self._pc = None
        self._index = None
        self._remote = None
        self._last_recv = time.monotonic()
        self._task = None
        self._ended = asyncio.Event()

    async def _send(self, data, to=True):
        msg = {"id": "forward", "type": API, "index": self._index, "data": data}
        if to and self._remote is not None:
            msg["toIndex"] = self._remote
        await self._ws.send(json.dumps(msg))

    async def _recv_until(self, pred, timeout=15):
        async def loop():
            while True:
                msg = json.loads(await self._ws.recv())
                self._last_recv = time.monotonic()
                if pred(msg):
                    return msg
                log.debug("ignored while waiting: %s", msg)
        return await asyncio.wait_for(loop(), timeout)

    async def run(self):
        self._task = asyncio.current_task()
        try:
            await self._handshake()
            await self._negotiate()
            self.on_state("sharing", self.receiver_name or self.host)
            await self._pump()
        except asyncio.CancelledError:
            raise Cancelled("Cancelled.") from None
        finally:
            await self._teardown()

    async def _handshake(self):
        self.on_state("connecting", self.host)
        self._ws = await websockets.connect(
            f"ws://{self.host}:{PORT}", subprotocols=["com.splashtop.webrtc2"],
            ping_interval=None, open_timeout=8)
        await self._ws.send(json.dumps({"id": "getsession", "type": API, "req": {
            "product": "smx", "version": CLIENT_VERSION, "platform": "linux",
            "oem": None, "uuid": secrets.token_hex(16), "useragent": USER_AGENT,
            "value": SESSION_ID}}))
        ack = (await self._recv_until(lambda m: m.get("id") == "getsession"))["ack"]
        log.info("getsession ack: %s", ack)
        self._index = ack["index"]
        self.receiver_name = ack.get("name")
        self._res = ack.get("res") or {"w": 1920, "h": 1080}
        if not ack.get("hosts"):
            raise AirMediaError("The TV did not offer a presentation slot.")
        # Keep the connection alive while a person reads the code off the TV.
        self._hb = asyncio.ensure_future(self._heartbeat())

        for attempt in range(3):
            # The TV accepts any userAgent here but only puts the stream on screen
            # when it looks like a browser's (verified on the AM-3200, 2026-09-24:
            # with "smx/... splashtop2 chrome linux" media flowed and nothing showed).
            auth = {"type": "authenticate", "userAgent": BROWSER_USER_AGENT}
            if str(ack.get("security")) == "1":
                self.on_state("code", self.receiver_name or self.host)
                code = await self.get_code(attempt > 0)
                if not code:
                    raise Cancelled("Cancelled.")
                auth["value"] = code.strip()
            await self._send(auth, to=False)
            reply = await self._recv_until(
                lambda m: m.get("id") == "forward" and m["data"].get("type") == "authorize")
            code = reply["data"].get("code")
            log.info("authorize: %s", reply["data"])
            if code != 401:
                break
        if code == 401:
            raise WrongCode("The TV rejected that code.")
        if code in (403, 408):
            raise AirMediaError("The TV declined the request.")
        if code == 404:
            raise ReceiverBusy("The TV is busy with another presenter.")
        if code != 200:
            raise AirMediaError(f"Unexpected answer from the TV: {reply['data']}")
        self._remote = reply["index"]
        self._res = reply["data"].get("res") if isinstance(reply["data"].get("res"), dict) \
            else self._res

    async def _negotiate(self):
        self._pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))

        @self._pc.on("connectionstatechange")
        async def _():
            state = self._pc.connectionState
            log.info("peer connection: %s", state)
            if state in ("failed", "closed"):
                self._ended.set()

        # Audio first: Chrome's addStream orders audio before video.
        self._pc.addTrack(self.audio)
        self._pc.addTrack(self.video)
        if self.video_codec:
            want = [c for c in RTCRtpSender.getCapabilities("video").codecs
                    if c.mimeType.lower() == f"video/{self.video_codec.lower()}"]
            for t in self._pc.getTransceivers():
                if t.kind == "video":
                    t.setCodecPreferences(want)

        self.video.on("ended", self._video_ended)

        await self._pc.setLocalDescription(await self._pc.createOffer())
        log.debug("offer sdp:\n%s", self._pc.localDescription.sdp)
        sdp = _chrome_style_sdp(self._pc.localDescription.sdp)
        await self._send({"type": "offer", "sdp": sdp, "res": self._res})

        pending = []
        while True:
            msg = json.loads(await asyncio.wait_for(self._ws.recv(), 20))
            if msg.get("id") != "forward":
                continue
            data = msg["data"]
            log.debug("negotiate recv: %s", json.dumps(data)[:3000])
            if data.get("type") == "answer":
                await self._pc.setRemoteDescription(
                    RTCSessionDescription(sdp=data["sdp"], type="answer"))
                for c in pending:
                    await self._add_candidate(c)
                return
            if data.get("type") == "candidate":
                pending.append(data)
            elif data.get("type") == "bye" and msg.get("index") == self._remote:
                raise AirMediaError("The TV ended the session before it started.")

    def _video_ended(self):
        log.info("video source ended")
        self._ended.set()

    def switch(self, video_source, audio_pids):
        """Share something else without a new code. Call on the session's loop."""
        self.video.set_source(video_source)
        self.audio.follow_pids(audio_pids)

    async def _add_candidate(self, data):
        text = data["candidate"]
        cand = candidate_from_sdp(text.split(":", 1)[1] if text.startswith("candidate:") else text)
        cand.sdpMid = data.get("id")
        cand.sdpMLineIndex = data.get("label")
        await self._pc.addIceCandidate(cand)

    async def _pump(self):
        async def reader():
            async for raw in self._ws:
                log.debug("recv: %s", raw[:600])
                msg = json.loads(raw)
                self._last_recv = time.monotonic()
                if msg.get("id") != "forward":
                    continue
                kind = msg["data"].get("type")
                if kind == "candidate":
                    await self._add_candidate(msg["data"])
                elif kind == "bye" and msg.get("index") == self._remote:
                    # Other presenters' byes are broadcast too; only the TV's ends us.
                    log.info("receiver said bye")
                    return

        self._last_recv = time.monotonic()
        self._pumping = True
        tasks = [asyncio.ensure_future(reader()), self._hb,
                 asyncio.ensure_future(self._ended.wait())]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            # Only judge silence once the reader is running; before that, nobody is
            # draining the socket (e.g. while the code box is open).
            if self._pumping and time.monotonic() - self._last_recv > HEARTBEAT_TIMEOUT_S:
                log.error("receiver went quiet for %d s", HEARTBEAT_TIMEOUT_S)
                return
            await self._send({"type": "heartbeat"}, to=False)

    async def stop(self):
        self._ended.set()
        # Before sharing starts (connecting, waiting for a code, negotiating) nothing
        # watches _ended, so cancel the attempt outright.
        if not self._pumping and self._task is not None:
            self._task.cancel()

    async def _teardown(self):
        if self._hb is not None:
            self._hb.cancel()
        if self._ws is not None and self._index is not None:
            try:
                await self._send({"type": "bye"})
            except Exception:
                pass
        try:
            if self._pc is not None:
                await self._pc.close()
        finally:
            for track in (self.video, self.audio):
                track.stop()
            if self._ws is not None:
                await self._ws.close()
            self.on_state("stopped", "")
