import struct
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spk_platform.settings import Speech
from spk_platform.speech import RecognitionEpoch, ssml, tts_url
from spk_platform.voice import HEADER, MAGIC, Decoder, pcm16


class SpeechTests(unittest.TestCase):
    def test_ssml_escapes_text(self):
        text = 'hello <world> & "voice"'
        root = ET.fromstring(ssml(text, Speech()))
        self.assertEqual("".join(root.itertext()), text)

    def test_endpoint_paths(self):
        config = Speech(region="eastasia")
        self.assertEqual(
            tts_url(config), "https://eastasia.tts.speech.microsoft.com/cognitiveservices/v1"
        )
        config.tts_endpoint = (
            "https://resource.cognitiveservices.azure.com/tts/cognitiveservices/v1"
        )
        self.assertEqual(
            tts_url(config, True),
            "https://resource.cognitiveservices.azure.com/tts/cognitiveservices/voices/list",
        )

    def test_pcm_clipping_and_endianness(self):
        actual = np.frombuffer(pcm16(np.array([-2, -1, 0, 1, 2, np.nan], dtype="float32")), "<i2")
        self.assertEqual(actual.tolist(), [-32768, -32768, 0, 32767, 32767, 0])

    def test_real_mictap_packet(self):
        packet = HEADER.pack(MAGIC, 1, 1, 3, 16000, 1, 2, 0) + struct.pack("<3h", -100, 0, 100)
        output = Decoder(gain=1).decode(packet)
        self.assertEqual(output, struct.pack("<3h", -100, 0, 100))
        with self.assertRaises(ValueError):
            Decoder().decode(packet[:-1])

    def test_microphone_gain_is_applied_once(self):
        packet = HEADER.pack(MAGIC, 1, 1, 1, 16000, 1, 2, 0) + struct.pack("<h", 1000)
        self.assertEqual(struct.unpack("<h", Decoder(gain=2).decode(packet))[0], 2000)

    def test_old_and_duplicate_final_events_are_rejected(self):
        epoch = RecognitionEpoch()
        old = epoch.begin()
        self.assertTrue(epoch.accept(old, "result1"))
        self.assertFalse(epoch.accept(old, "result1"))
        current = epoch.begin()
        self.assertFalse(epoch.accept(old, "late-result"))
        self.assertTrue(epoch.accept(current, "result1"))
        epoch.cancel()
        self.assertFalse(epoch.accept(current, "late"))


if __name__ == "__main__":
    unittest.main()
