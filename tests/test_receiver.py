import asyncio
import unittest

from aiortc import AudioStreamTrack, VideoStreamTrack

from airmedia_share import media, receiver
from tests.fake_receiver import NAME, FakeReceiver


class LoopbackTest(unittest.IsolatedAsyncioTestCase):
    """A full session against the fake receiver: handshake, code, WebRTC, media, bye."""

    async def asyncSetUp(self):
        self.tv = await FakeReceiver(code="1234").start()
        self.patch_port = receiver.PORT
        receiver.PORT = self.tv.port

    async def asyncTearDown(self):
        receiver.PORT = self.patch_port
        await self.tv.close()

    def session(self, codes):
        codes = list(codes)
        asked = []

        async def get_code(retry):
            asked.append(retry)
            return codes.pop(0) if codes else None

        video = media.ScreenVideoTrack(VideoStreamTrack(), *media.CANVAS)
        states = []
        s = receiver.Session("127.0.0.1", get_code, video, AudioStreamTrack(),
                             on_state=lambda st, d="": states.append(st))
        return s, asked, states

    async def run_until(self, s, predicate, timeout=20):
        task = asyncio.ensure_future(s.run())
        try:
            for _ in range(int(timeout * 10)):
                if predicate() or task.done():
                    break
                await asyncio.sleep(0.1)
        finally:
            await s.stop()
            try:
                await asyncio.wait_for(task, 10)
            except receiver.Cancelled:
                pass  # stopped before sharing had started

    async def test_share_reaches_the_tv_and_is_shown(self):
        s, asked, states = self.session(["1234"])
        await self.run_until(s, lambda: self.tv.video_frames >= 10 and self.tv.audio_frames >= 10)
        self.assertGreaterEqual(self.tv.video_frames, 10)
        self.assertGreaterEqual(self.tv.audio_frames, 10)
        # The real TV only displays browser-like senders.
        self.assertTrue(self.tv.shown, f"userAgent was {self.tv.user_agent!r}")
        self.assertEqual(s.receiver_name, NAME)
        self.assertEqual(asked, [False])
        self.assertIn("sharing", states)
        self.assertEqual(states[-1], "stopped")
        self.assertTrue(self.tv.got_bye)

    async def test_offer_has_the_shape_the_tv_was_built_for(self):
        s, _, _ = self.session(["1234"])
        await self.run_until(s, lambda: self.tv.offer_sdp is not None)
        sdp = self.tv.offer_sdp
        self.assertNotIn("UDP/TLS/RTP/SAVPF", sdp)
        self.assertIn("RTP/SAVPF", sdp)
        self.assertNotIn(" rtx/", sdp)
        self.assertRegex(sdp, r"a=ssrc:\d+ msid:\S+ \S+")
        self.assertLess(sdp.index("m=audio"), sdp.index("m=video"))

    async def test_wrong_code_is_asked_again(self):
        s, asked, _ = self.session(["0000", "1234"])
        await self.run_until(s, lambda: self.tv.video_frames >= 3)
        self.assertEqual(asked, [False, True])
        self.assertGreaterEqual(self.tv.video_frames, 3)

    async def test_three_wrong_codes_give_up(self):
        s, asked, _ = self.session(["0000", "1111", "2222"])
        with self.assertRaises(receiver.WrongCode):
            await asyncio.wait_for(s.run(), 20)
        self.assertEqual(len(asked), 3)

    async def test_cancel_while_waiting_for_the_code(self):
        waiting = asyncio.Event()

        async def get_code(retry):
            waiting.set()
            await asyncio.sleep(3600)

        video = media.ScreenVideoTrack(VideoStreamTrack(), *media.CANVAS)
        s = receiver.Session("127.0.0.1", get_code, video, AudioStreamTrack())
        task = asyncio.ensure_future(s.run())
        await asyncio.wait_for(waiting.wait(), 10)
        await s.stop()
        with self.assertRaises(receiver.Cancelled):
            await asyncio.wait_for(task, 10)

    async def test_switching_source_keeps_frames_flowing(self):
        s, _, _ = self.session(["1234"])
        task = asyncio.ensure_future(s.run())
        try:
            for _ in range(200):
                if self.tv.video_frames >= 5:
                    break
                await asyncio.sleep(0.1)
            s.video.set_source(VideoStreamTrack())
            before = self.tv.video_frames
            for _ in range(100):
                if self.tv.video_frames >= before + 10:
                    break
                await asyncio.sleep(0.1)
            self.assertGreaterEqual(self.tv.video_frames, before + 10)
        finally:
            await s.stop()
            await asyncio.wait_for(task, 10)


if __name__ == "__main__":
    unittest.main()
