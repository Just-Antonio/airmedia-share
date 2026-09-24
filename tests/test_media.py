import asyncio
import fractions
import unittest

import av
import numpy as np
from aiortc import MediaStreamTrack

from airmedia_share import media


class FixedSource(MediaStreamTrack):
    kind = "video"

    def __init__(self, w, h, pts_start=0):
        super().__init__()
        self.w, self.h, self.pts = w, h, pts_start

    async def recv(self):
        frame = av.VideoFrame(self.w, self.h, "rgb24")
        for p in frame.planes:
            p.update(bytes([255]) * (p.line_size * p.height))
        frame.pts, frame.time_base = self.pts, fractions.Fraction(1, 1000)
        self.pts += 33
        return frame


class VideoTrackTest(unittest.IsolatedAsyncioTestCase):
    async def test_every_source_comes_out_at_the_canvas_size(self):
        for w, h in ((1920, 1080), (1850, 1052), (801, 601), (400, 1200), (3840, 2160)):
            track = media.ScreenVideoTrack(FixedSource(w, h), 1920, 1080)
            frame = await track.recv()
            self.assertEqual((frame.width, frame.height), (1920, 1080), (w, h))
            self.assertEqual(frame.format.name, "yuv420p")

    async def test_letterbox_is_black_and_content_is_centered(self):
        track = media.ScreenVideoTrack(FixedSource(960, 1080), 1920, 1080)
        y = (await track.recv()).to_ndarray()[:1080]  # luma rows
        self.assertLess(y[540, 100], 30)   # left bar
        self.assertGreater(y[540, 960], 200)  # white content in the middle
        self.assertLess(y[540, 1820], 30)  # right bar

    async def test_time_keeps_moving_forward_across_switches(self):
        # aiortc's MediaPlayer restarts pts at 0 per capture; that froze the TV.
        track = media.ScreenVideoTrack(FixedSource(1920, 1080, pts_start=10_000_000), 1920, 1080)
        stamps = [(await track.recv()).pts for _ in range(3)]
        track.set_source(FixedSource(800, 600, pts_start=0))
        await asyncio.sleep(0.01)
        stamps += [(await track.recv()).pts for _ in range(3)]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(len(set(stamps)), len(stamps))


class MixTest(unittest.TestCase):
    def test_sums_scales_and_clips(self):
        a = bytearray(np.full(8, 1000, np.int16).tobytes())
        b = bytearray(np.full(8, 30000, np.int16).tobytes())
        out = np.frombuffer(media.mix_chunks([a, b], 8, 1.0), np.int16)
        self.assertTrue((out == 31000).all())
        c = bytearray(np.full(8, 30000, np.int16).tobytes())
        d = bytearray(np.full(8, 30000, np.int16).tobytes())
        out = np.frombuffer(media.mix_chunks([c, d], 8, 1.0), np.int16)
        self.assertTrue((out == 32767).all())
        e = bytearray(np.full(8, 1000, np.int16).tobytes())
        out = np.frombuffer(media.mix_chunks([e], 8, 0.5), np.int16)
        self.assertTrue((out == 500).all())

    def test_short_buffer_is_padded_and_consumed(self):
        short = bytearray(np.full(4, 7, np.int16).tobytes())
        out = np.frombuffer(media.mix_chunks([short], 8, 1.0), np.int16)
        self.assertEqual(list(out), [7, 7, 7, 7, 0, 0, 0, 0])
        self.assertEqual(len(short), 0)

    def test_nothing_playing_is_silence(self):
        self.assertEqual(media.mix_chunks([], 8, 1.0), bytes(16))


class AncestorsTest(unittest.TestCase):
    def test_includes_self_and_init_chain(self):
        import os
        chain = media._ancestors(os.getpid())
        self.assertIn(os.getpid(), chain)
        self.assertIn(os.getppid(), chain)


if __name__ == "__main__":
    unittest.main()
