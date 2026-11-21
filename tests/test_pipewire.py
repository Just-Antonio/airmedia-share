import asyncio
import unittest

import numpy as np
from aiortc import MediaStreamTrack

from airmedia_share import media
from airmedia_share.backends import pipewire


class BgrxFrameTest(unittest.TestCase):
    def test_padded_rows_are_cropped_and_colors_kept(self):
        w, h, stride = 6, 4, 32  # 24 bytes of pixels + 8 of padding per row
        buf = np.zeros((h, stride), np.uint8)
        buf[:, :w * 4] = np.tile(np.array([10, 20, 30, 0], np.uint8), (h, w))  # B, G, R, x
        buf[:, w * 4:] = 255  # padding must not leak into the picture
        frame = pipewire.bgrx_to_frame(buf.tobytes(), w, h, stride)
        self.assertEqual((frame.width, frame.height), (w, h))
        px = frame.to_ndarray(format="rgb24")
        self.assertTrue((px == [30, 20, 10]).all())

    def test_tightly_packed_rows(self):
        w, h = 8, 2
        data = bytes([1, 2, 3, 0]) * (w * h)
        frame = pipewire.bgrx_to_frame(data, w, h, w * 4)
        self.assertEqual((frame.width, frame.height), (w, h))

    def test_fits_the_canvas_like_any_other_source(self):
        # A window-sized capture goes through ScreenVideoTrack's letterboxing unchanged.
        frame = pipewire.bgrx_to_frame(bytes(4) * (640 * 480), 640, 480, 640 * 4)

        class Once(MediaStreamTrack):
            kind = "video"

            async def recv(self):
                return frame

        out = asyncio.run(media.ScreenVideoTrack(Once(), 1920, 1080).recv())
        self.assertEqual((out.width, out.height), (1920, 1080))


if __name__ == "__main__":
    unittest.main()
