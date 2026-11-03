import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spk_platform.assets import audio_info
from spk_platform.settings import Speech
from spk_platform.streaming import TTSStream


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required"
    )
    async def test_stream_is_decodable_and_has_protection_tail(self):
        original = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=48000:cl=mono",
                "-t",
                "0.2",
                "-f",
                "mp3",
                "pipe:1",
            ],
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout

        async def source():
            for offset in range(0, len(original), 1024):
                yield original[offset : offset + 1024]

        stream = TTSStream("fixture", Speech(), "not-real", source=source)
        await stream.start()
        output = bytearray()
        async for chunk in stream.content():
            output.extend(chunk)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "stream.mp3")
            target.write_bytes(output)
            self.assertGreater(audio_info(target)["duration"], 45)
        self.assertFalse(stream.error)
        self.assertTrue(stream.done)

    async def test_cancel_before_first_chunk_cannot_resume(self):
        stream = TTSStream("fixture", Speech(), "not-real")
        await stream.cancel()
        with self.assertRaises(ValueError):
            await stream.start()
        self.assertEqual([chunk async for chunk in stream.content()], [])


if __name__ == "__main__":
    unittest.main()
