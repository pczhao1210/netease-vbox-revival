import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spk_platform.settings import Settings
from spk_platform.voice import VoiceReceiver


class VoiceSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_final_utterances_share_one_wake_session(self):
        submitted = []

        async def api(method, path, body=None):
            if path == "/internal/voice/text":
                submitted.append(body)
            return {}

        receiver = VoiceReceiver(api)
        receiver.session = {"revision": 1, "config": Settings().model_dump(), "asr_key": "fixture"}
        instances = []

        class Recognizer:
            def __init__(self, config, key, emit):
                self.emit = emit
                self.number = len(instances)
                self.closed = False
                instances.append(self)

            async def start(self):
                if self.number < 2:
                    self.emit("final", "utterance-%s" % self.number, "result-%s" % self.number)
                    self.emit("final", "duplicate", "result-%s" % self.number)
                else:
                    self.emit("no_match", "", "done")

            def write(self, pcm):
                pass

            async def close(self):
                self.closed = True

        with patch("spk_platform.voice.AzureASR", Recognizer):
            await receiver.listen()
        self.assertEqual([body["text"] for body in submitted], ["utterance-0", "utterance-1"])
        self.assertTrue(all(instance.closed for instance in instances))
        self.assertIsNone(receiver.recognizer)
        self.assertEqual(receiver.errors, 0)

    async def test_silence_ends_the_session_and_asks_to_resume(self):
        calls = []

        async def api(method, path, body=None):
            calls.append(path)
            return {"listen": False}

        config = Settings()
        config.speech.resume_seconds = 0.2
        receiver = VoiceReceiver(api)
        receiver.session = {"revision": 1, "config": config.model_dump(), "asr_key": "fixture"}
        script = [[], [("partial", "放", "p"), ("final", "放首歌", "f")]]

        class Recognizer:
            def __init__(self, config, key, emit):
                self.emit = emit
                self.events = script.pop(0) if script else []

            async def start(self):
                for event in self.events:
                    self.emit(*event)

            def write(self, pcm):
                pass

            async def close(self):
                pass

        with patch("spk_platform.voice.AzureASR", Recognizer):
            started = asyncio.get_running_loop().time()
            await receiver.listen()
            self.assertLess(asyncio.get_running_loop().time() - started, 2)
            self.assertEqual(calls, ["/internal/voice/done"])
            await receiver.listen()
        self.assertEqual(calls[1:], ["/internal/voice/text", "/internal/voice/done"])
        self.assertEqual(receiver.errors, 0)


if __name__ == "__main__":
    unittest.main()
