import asyncio
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spk_platform.assets import Assets, audio_info
from spk_platform.settings import Music


class AssetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.assets = Assets(self.root / "cache", self.root / "local")
        self.settings = Music(disk_floor_mb=64)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_uploads_and_in_flight_media_survive_cache_clearing(self):
        uploaded, cached, preparing = "a" * 64, "b" * 64, "c" * 64
        for identifier in (uploaded, cached, preparing):
            self.assets.path(identifier).write_bytes(b"x" * 1024)
        self.assets.pin(uploaded)
        async with self.assets.hold(preparing):
            self.assertEqual(self.assets.trim(self.settings, clear=True), 1024)
        self.assertEqual(
            sorted(path.stem for path in self.assets.root.glob("*.mp3")), [uploaded, preparing]
        )
        reopened = Assets(self.root / "cache", self.root / "local")
        self.assertEqual(reopened.pins, {uploaded})
        self.assertEqual(reopened.trim(self.settings, clear=True), 0)
        self.assertTrue(reopened.path(uploaded).is_file())

    async def test_http_error_and_stream_size_limit_leave_no_cache(self):
        async def handler(request):
            return httpx.Response(403, content=b"<html>" + b"x" * 70000)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                await self.assets.prepare(
                    "test", self.settings, url="https://invalid.test/song", client=client
                )
        self.assertEqual(list(self.assets.root.iterdir()), [])

        async def too_large(request):
            return httpx.Response(200, content=b"x" * (2 * 1048576))

        self.settings.one_file_mb = 1
        async with httpx.AsyncClient(transport=httpx.MockTransport(too_large)) as client:
            with self.assertRaises(ValueError):
                await self.assets.prepare(
                    "test", self.settings, url="https://invalid.test/song", client=client
                )
        self.assertEqual(list(self.assets.root.iterdir()), [])

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required"
    )
    async def test_real_decode_normalization_and_single_download(self):
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(bytes(6400))
        calls = []

        async def handler(request):
            calls.append(request.url)
            return httpx.Response(
                200, content=buffer.getvalue(), headers={"Content-Type": "audio/wav"}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first, second = await asyncio.gather(
                *[
                    self.assets.prepare(
                        "same-song", self.settings, url="https://invalid.test/song", client=client
                    )
                    for index in range(2)
                ]
            )
        self.assertEqual(first["media_id"], second["media_id"])
        self.assertEqual(len(calls), 1)
        self.assertGreater(first["duration"], 45)
        self.assertLess(first["duration"], 47)
        self.assertEqual(len(list(self.assets.root.glob("*.mp3"))), 1)
        self.assertEqual(list(self.assets.root.glob("*.part")), [])
        self.assets.protected.add(first["media_id"])
        self.assets.trim(self.settings, clear=True)
        self.assertTrue(self.assets.path(first["media_id"]).exists())

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required"
    )
    async def test_resume_excerpt_keeps_the_rest_and_the_protection_tail(self):
        source, converted = self.root / "song.wav", self.root / "song.mp3"
        with wave.open(str(source), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(bytes(16000 * 2 * 20))
        await self.assets.normalize(source, converted)
        total = (await asyncio.to_thread(audio_info, converted))["duration"]
        excerpt = self.root / "rest.mp3"
        await self.assets.excerpt(converted, 12, excerpt)
        self.assertAlmostEqual(
            (await asyncio.to_thread(audio_info, excerpt))["duration"], total - 12, delta=0.2
        )
        with self.assertRaises(ValueError):
            await self.assets.excerpt(converted, 30, excerpt)

    @unittest.skipUnless(shutil.which("ffprobe"), "ffprobe required")
    async def test_id3_garbage_and_external_playlist_are_rejected(self):
        for content in (b"ID3" + b"x" * 70000, b"ffconcat version 1.0\nfile /etc/passwd\n"):
            source = self.root / "fake.mp3"
            source.write_bytes(content)
            with self.assertRaises((ValueError, subprocess.SubprocessError)):
                await asyncio.to_thread(audio_info, source)


if __name__ == "__main__":
    unittest.main()
