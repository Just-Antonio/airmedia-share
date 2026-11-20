"""Wayland capture, second half: read the PipeWire node the portal granted (portal.py).

GStreamer's pipewiresrc does the reading, through PyGObject. Nothing here touches GTK,
and `gi` is imported lazily, so the pure parts can be tested without a display.
"""

import asyncio
import logging
import time

import av
import numpy as np
from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError

log = logging.getLogger(__name__)


def bgrx_to_frame(data, width, height, stride):
    """A BGRx buffer as an av.VideoFrame. GStreamer may pad each row out to `stride` bytes."""
    rows = np.frombuffer(data, np.uint8)[:height * stride].reshape(height, stride)
    return av.VideoFrame.from_ndarray(
        np.ascontiguousarray(rows[:, :width * 4].reshape(height, width, 4)), format="bgra")


def _gst():
    import gi
    gi.require_version("Gst", "1.0")
    gi.require_version("GstVideo", "1.0")
    from gi.repository import Gst, GstVideo
    Gst.init(None)
    return Gst, GstVideo


def capture_problem():
    """Why PipeWire capture cannot work here (and the fix), or None if it can."""
    try:
        Gst, _ = _gst()
    except (ImportError, ValueError) as e:
        return (f"The GStreamer Python bindings are missing ({e}). "
                "On Ubuntu: sudo apt install gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0")
    missing = [name for name in ("pipewiresrc", "videoconvert", "appsink")
               if Gst.ElementFactory.find(name) is None]
    if missing:
        return (f"GStreamer is missing {', '.join(missing)}. "
                "On Ubuntu: sudo apt install gstreamer1.0-pipewire gstreamer1.0-plugins-base")
    return None


class PipeWireVideoSource(MediaStreamTrack):
    """Frames of a screen or window granted by the desktop portal (see portal.PortalStream).

    GStreamer's pipewiresrc reads the node. A PipeWire screen capture only sends a frame
    when something on screen changed, while the encoder wants a steady feed (x11grab
    gives one), so recv() ticks at `fps` and repeats the newest frame when nothing new
    arrived. Sizes may change at any time (a resized window); media.ScreenVideoTrack
    fits whatever comes out into its fixed canvas.
    """

    kind = "video"
    FIRST_FRAME_TIMEOUT = 15

    def __init__(self, stream, fps=30):
        super().__init__()
        Gst, GstVideo = _gst()
        self._Gst, self._GstVideo = Gst, GstVideo
        self._stream = stream
        self._fps = fps
        self._frame = None
        self._next = None
        self._opened = time.monotonic()
        self._pipeline = Gst.parse_launch(
            f"pipewiresrc fd={stream.fd} path={stream.node_id} do-timestamp=true "
            "! videoconvert ! video/x-raw,format=BGRx "
            "! appsink name=sink max-buffers=1 drop=true sync=false")
        self._sink = self._pipeline.get_by_name("sink")
        self._bus = self._pipeline.get_bus()
        self._pipeline.set_state(Gst.State.PLAYING)
        # If the user ends the share from the desktop's own indicator, so do we.
        stream.on_closed = self.stop

    def _check_bus(self):
        Gst = self._Gst
        msg = self._bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
        if msg is None:
            return
        if msg.type == Gst.MessageType.ERROR:
            log.error("pipewire capture failed: %s", msg.parse_error()[0].message)
        else:
            log.info("pipewire capture ended")
        self.stop()

    def _convert(self, sample):
        Gst, GstVideo = self._Gst, self._GstVideo
        info = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
        buf = sample.get_buffer()
        meta = GstVideo.buffer_get_video_meta(buf)  # the real row stride, if it differs
        stride = meta.stride[0] if meta is not None else info.stride[0]
        ok, mapped = buf.map(Gst.MapFlags.READ)
        if not ok:
            return None
        try:
            return bgrx_to_frame(mapped.data, info.width, info.height, stride)
        finally:
            buf.unmap(mapped)

    async def recv(self):
        if self._next is None:
            self._next = time.monotonic()
        self._next += 1 / self._fps
        delay = self._next - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        elif delay < -0.2:
            self._next = time.monotonic()  # fell far behind (e.g. suspend)
        while True:
            if self.readyState != "live":
                raise MediaStreamError
            self._check_bus()
            if self.readyState != "live":
                raise MediaStreamError
            # The signal, not AppSink.try_pull_sample: that method only exists once the
            # separate GstApp typelib is imported.
            sample = self._sink.emit("try-pull-sample", 0)
            if sample is not None:
                frame = self._convert(sample)
                if frame is not None:
                    self._frame = frame
            if self._frame is not None:
                return self._frame
            if time.monotonic() - self._opened > self.FIRST_FRAME_TIMEOUT:
                log.error("no frames from the portal's PipeWire stream")
                self.stop()
                raise MediaStreamError
            await asyncio.sleep(0.02)

    def stop(self):
        was_live = self.readyState == "live"
        super().stop()
        if was_live:
            self._pipeline.set_state(self._Gst.State.NULL)
            self._stream.close()
