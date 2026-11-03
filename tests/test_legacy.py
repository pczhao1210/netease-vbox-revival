import importlib.util
import io
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import spk_netd


class LegacyTests(unittest.TestCase):
    def test_netd_missing_wrong_and_correct_token(self):
        handler = spk_netd.H.__new__(spk_netd.H)
        statuses = []
        handler.send_response = statuses.append
        handler.send_header = lambda *args: None
        handler.end_headers = lambda: None
        for token, supplied, expected, status in (
            ("", "anything", False, 503),
            ("configured", "wrong", False, 403),
            ("configured", "configured", True, None),
        ):
            statuses.clear()
            with patch.object(spk_netd, "token", return_value=token):
                self.assertEqual(handler._auth("t=" + supplied), expected)
            self.assertEqual(statuses, [] if status is None else [status])

    def tts(self):
        stubs = {}
        for name in ("spk_tts_qwen", "spk_tts_qwen_stream", "spk_voice"):
            stubs[name] = types.ModuleType(name)
        stubs["spk_tts_qwen"].INSTR_DEFAULT = ""
        stubs["spk_tts_qwen_stream"].available = lambda: True
        spec = importlib.util.spec_from_file_location(
            "legacy_tts_test", Path(__file__).resolve().parents[1] / "src/spk_tts_stream.py"
        )
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, stubs):
            spec.loader.exec_module(module)
        return module

    def test_tts_response_closes_unread_connection(self):
        module = self.tts()
        handler = module.H.__new__(module.H)
        headers = {}
        handler.send_response = lambda status: None
        handler.send_header = headers.__setitem__
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        handler._json(403, {"ok": False})
        self.assertEqual(headers["Connection"], "close")
        self.assertTrue(handler.close_connection)

    def test_tts_constructor_failure_releases_slot(self):
        module = self.tts()
        module._slots = threading.BoundedSemaphore(1)
        with patch.object(module, "Stream", side_effect=ValueError("fixture")):
            with self.assertRaises(ValueError):
                module.start("test", _limit=True)
        self.assertTrue(module._slots.acquire(blocking=False))


if __name__ == "__main__":
    unittest.main()
