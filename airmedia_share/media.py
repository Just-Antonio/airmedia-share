"""What gets sent to the TV: the screen or one window, and the computer's sound.

Video comes from X11 via PyAV's x11grab (so this needs an X11 session, not Wayland);
audio comes from PulseAudio via `parec`. Both are wrapped so that the WebRTC session
keeps one track of each for its whole life and only what feeds them changes.
"""

import asyncio
import fractions
import json
import logging
import re
import time

import av
import numpy as np
from aiortc import MediaStreamTrack
from aiortc.contrib.media import MediaPlayer
from aiortc.mediastreams import MediaStreamError

log = logging.getLogger(__name__)

# Every source is fitted into this canvas; the AM-3200 asks for 1920x1080.
CANVAS = (1920, 1080)


# One clock for every video frame in this process. aiortc's MediaPlayer restarts pts
# at 0 for each new capture, so after a source switch the TV saw frames stamped far
# in its past and froze until the new timeline caught up (about as long as the old
# source had been shared). Stamping from a shared monotonic clock keeps time moving
# forward across switches.
_VIDEO_T0 = time.monotonic()
_VIDEO_TIME_BASE = fractions.Fraction(1, 90000)
# yuv420p black: Y=16, U=V=128; chroma planes are half size in both directions.
_PLANES = ((16, 1), (128, 2), (128, 2))


def _plane_array(plane):
    return np.frombuffer(plane, np.uint8).reshape(plane.height, plane.line_size)[:, :plane.width]


class ScreenVideoTrack(MediaStreamTrack):
    """The one video track a session sends; what feeds it can be swapped live.

    Frames are fitted into a fixed width x height canvas (letterboxed, yuv420p), so the
    TV's decoder never reinitializes when the source changes size. Swapping the feed
    instead of calling RTCRtpSender.replaceTrack matters: aiortc's send loop ends for
    good if the track it is awaiting raises MediaStreamError, which is exactly what a
    stopped capture does.
    """

    kind = "video"

    def __init__(self, source, width, height):
        super().__init__()
        self._source = source
        self._w = width
        self._h = height

    def set_source(self, source):
        old, self._source = self._source, source
        old.stop()

    async def recv(self):
        while True:
            source = self._source
            try:
                frame = await source.recv()
            except MediaStreamError:
                if source is not self._source:
                    continue  # it was swapped out while we waited on it
                self.stop()
                raise
            if source is self._source:
                break
        scale = min(self._w / frame.width, self._h / frame.height)
        w = min(self._w, round(frame.width * scale)) & ~1
        h = min(self._h, round(frame.height * scale)) & ~1
        img = frame.reformat(width=w, height=h, format="yuv420p")
        if (w, h) == (self._w, self._h):
            out = img
        else:
            out = av.VideoFrame(self._w, self._h, "yuv420p")
            x, y = ((self._w - w) // 2) & ~1, ((self._h - h) // 2) & ~1
            for i, (black, div) in enumerate(_PLANES):
                dst = out.planes[i]
                canvas = np.full((dst.height, dst.line_size), black, np.uint8)
                src = _plane_array(img.planes[i])
                canvas[y // div:y // div + src.shape[0], x // div:x // div + src.shape[1]] = src
                dst.update(canvas)
        out.pts = int((time.monotonic() - _VIDEO_T0) * 90000)
        out.time_base = _VIDEO_TIME_BASE
        return out

    def stop(self):
        super().stop()
        self._source.stop()


def _ancestors(pid):
    """pid and its parents, from /proc (for apps whose audio is a child process)."""
    chain = set()
    while pid > 1 and pid not in chain:
        chain.add(pid)
        try:
            with open(f"/proc/{pid}/stat") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return chain


def mix_chunks(buffers, samples, gain):
    """Take up to one chunk of s16le from each bytearray (consuming it), sum, scale.

    A buffer that is short (its stream hiccupped) contributes what it has; the rest of
    the chunk is silence. Returns exactly samples * 2 bytes.
    """
    mix = np.zeros(samples, np.int32)
    for buf in buffers:
        take = min(len(buf), samples * 2) & ~3
        if take:
            mix[:take // 2] += np.frombuffer(bytes(buf[:take]), np.int16)
            del buf[:take]
    if gain != 1.0:
        mix = (mix * gain).astype(np.int32)
    return np.clip(mix, -32768, 32767).astype(np.int16).tobytes()


class ComputerAudioTrack(MediaStreamTrack):
    """This computer's sound: everything it plays, or only the streams of chosen apps.

    Each source is a `parec` (PyAV's bundled ffmpeg has no pulse input): the default
    sink's monitor for everything, or `--monitor-stream=<sink input>` per stream of the
    shared app, matched by process id or any ancestor of it (Chrome plays audio from a
    child process). The streams are mixed on a 20 ms clock, one Opus frame each.

    The capture is taken before the computer's own output volume, so the TV's level is
    independent of the computer speakers; `volume` (0..1) trims it from here, and
    `muted` sends silence without renegotiating. With `follow_system_volume` the
    computer's volume and mute (keyboard keys, top-bar slider) also scale it.
    """

    kind = "audio"
    RATE = 48000
    CHANNELS = 2
    SAMPLES = 960  # 20 ms
    CHUNK = SAMPLES * CHANNELS * 2
    # Keep at most this much queued per stream; beyond it, drop the oldest audio so
    # clock drift between parec and our 20 ms tick never builds up latency.
    MAX_QUEUED = CHUNK * 4

    def __init__(self):
        super().__init__()
        self.muted = False
        self.volume = 1.0
        self.follow_system_volume = False
        self._system_gain = 1.0
        self._pids = None  # None: everything
        self._streams = {}  # key -> (process, bytearray, reader task)
        self._watch = None
        self._subscribe = None
        self._next_tick = None
        self._pts = 0

    def follow_pids(self, pids):
        """Only these apps' sound (None or empty: all of it). Safe to call any time."""
        self._pids = set(pids) if pids else None
        if self._watch is not None:
            asyncio.ensure_future(self._rescan())

    async def _pactl_json(self, *args):
        proc = await asyncio.create_subprocess_exec(
            "pactl", "-f", "json", *args, stdout=asyncio.subprocess.PIPE)
        out = (await proc.communicate())[0]
        return json.loads(out or b"[]")

    async def _rescan(self):
        wanted = {}
        if self._pids is None:
            wanted["all"] = ["-d", "@DEFAULT_MONITOR@"]
        else:
            for si in await self._pactl_json("list", "sink-inputs"):
                try:
                    pid = int(si["properties"]["application.process.id"])
                except (KeyError, ValueError):
                    continue
                if _ancestors(pid) & self._pids:
                    wanted[f"stream{si['index']}"] = [f"--monitor-stream={si['index']}"]
        for key in list(self._streams):
            if key not in wanted:
                self._close_stream(key)
        for key, args in wanted.items():
            if key not in self._streams:
                await self._open_stream(key, args)
        log.info("audio sources: %s", sorted(self._streams) or "none (silence)")

    async def _open_stream(self, key, args):
        proc = await asyncio.create_subprocess_exec(
            "parec", "--format=s16le", f"--rate={self.RATE}",
            f"--channels={self.CHANNELS}", "--latency-msec=20",
            "--client-name=Share to Office TV", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        buf = bytearray()

        async def reader():
            while True:
                data = await proc.stdout.read(self.CHUNK)
                if not data:
                    return
                buf.extend(data)
                if len(buf) > self.MAX_QUEUED:
                    del buf[:len(buf) - self.CHUNK * 2]

        self._streams[key] = (proc, buf, asyncio.ensure_future(reader()))

    def _close_stream(self, key):
        proc, _buf, task = self._streams.pop(key)
        task.cancel()
        if proc.returncode is None:
            proc.kill()

    async def _read_system_volume(self):
        sinks = {s["name"]: s for s in await self._pactl_json("list", "sinks")}
        info = await self._pactl_json("info")
        sink = sinks.get(info.get("default_sink_name") if isinstance(info, dict) else None)
        if not sink:
            self._system_gain = 1.0
            return
        levels = [int(str(ch["value"])) for ch in sink.get("volume", {}).values()]
        level = sum(levels) / len(levels) / 65536 if levels else 1.0
        # PulseAudio maps its volume scale to amplitude with a cubic curve.
        self._system_gain = 0.0 if sink.get("mute") else level ** 3

    async def _watch_streams(self):
        await self._rescan()
        await self._read_system_volume()
        self._subscribe = await asyncio.create_subprocess_exec(
            "pactl", "subscribe", stdout=asyncio.subprocess.PIPE)
        async for line in self._subscribe.stdout:
            # Apps open a new stream per video/tab; the default sink can change too.
            if b"on sink-input" in line or b"on server" in line:
                await self._rescan()
            if b"on sink #" in line or b"on server" in line:
                await self._read_system_volume()

    async def recv(self):
        if self._watch is None:
            self._watch = asyncio.ensure_future(self._watch_streams())
            self._next_tick = time.monotonic()
        self._next_tick += self.SAMPLES / self.RATE
        delay = self._next_tick - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        elif delay < -0.2:
            self._next_tick = time.monotonic()  # fell far behind (e.g. suspend)

        gain = 0.0 if self.muted else self.volume
        if self.follow_system_volume:
            gain *= self._system_gain
        buffers = [buf for _proc, buf, _task in list(self._streams.values())]
        data = mix_chunks(buffers, self.SAMPLES * self.CHANNELS, gain)

        frame = av.AudioFrame(format="s16", layout="stereo", samples=self.SAMPLES)
        frame.planes[0].update(data)
        frame.sample_rate = self.RATE
        frame.time_base = fractions.Fraction(1, self.RATE)
        frame.pts = self._pts
        self._pts += self.SAMPLES
        return frame

    def stop(self):
        super().stop()
        for task in (self._watch,):
            if task is not None:
                task.cancel()
        for key in list(self._streams):
            self._close_stream(key)
        if self._subscribe is not None and self._subscribe.returncode is None:
            self._subscribe.kill()


def open_capture(display, *, window_id=None, screen_size=None, fps=30):
    """x11grab of the whole screen, or of one window by X id (follows it when moved)."""
    options = {"framerate": str(fps), "draw_mouse": "1"}
    if window_id is not None:
        options["window_id"] = str(window_id)
    else:
        options["video_size"] = "%dx%d" % screen_size
    return MediaPlayer(display, format="x11grab", options=options)
