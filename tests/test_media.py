import http.client
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import mp3srv


class MediaHTTPTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "empty.mp3").write_bytes(b"")
        (self.root / "song.mp3").write_bytes(b"0123456789")
        self.root_patch = patch.object(mp3srv, "ROOT", self.directory.name)
        self.root_patch.start()
        self.server = mp3srv.ThreadingHTTPServer(("127.0.0.1", 0), mp3srv.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.root_patch.stop()
        self.directory.cleanup()

    def request(self, path, method="GET", headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_empty_get_and_head(self):
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                status, headers, body = self.request("/empty.mp3", method)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Length"], "0")
                self.assertEqual(body, b"")

    def test_ranges(self):
        for value, expected in (("bytes=2-4", b"234"), ("bytes=-3", b"789"), ("bytes=8-", b"89")):
            with self.subTest(value=value):
                status, headers, body = self.request("/song.mp3", headers={"Range": value})
                self.assertEqual(status, 206)
                self.assertEqual(body, expected)
                self.assertEqual(int(headers["Content-Length"]), len(expected))
        status, headers, body = self.request("/song.mp3", headers={"Range": "bytes=10-"})
        self.assertEqual((status, body), (416, b""))
        self.assertEqual(headers["Content-Range"], "bytes */10")

    def test_empty_range_is_unsatisfiable(self):
        status, headers, body = self.request("/empty.mp3", headers={"Range": "bytes=0-"})
        self.assertEqual((status, body), (416, b""))
        self.assertEqual(headers["Content-Range"], "bytes */0")

    def test_symlinks_are_not_served(self):
        with tempfile.TemporaryDirectory() as external:
            Path(external, "private.mp3").write_bytes(b"private")
            os.symlink(Path(external, "private.mp3"), self.root / "link.mp3")
            os.symlink(external, self.root / "linked-directory")
            for path in ("/link.mp3", "/linked-directory/private.mp3"):
                with self.subTest(path=path):
                    status, headers, body = self.request(path)
                    self.assertEqual(status, 404)
                    self.assertNotIn(b"private", body)

    def test_swap_to_symlink_between_check_and_open(self):
        with tempfile.TemporaryDirectory() as external:
            private = Path(external, "private.mp3")
            private.write_bytes(b"private")
            original = mp3srv.Handler._full_path

            def replace_path(handler):
                full = original(handler)
                (self.root / "song.mp3").unlink()
                os.symlink(private, self.root / "song.mp3")
                return full

            with patch.object(mp3srv.Handler, "_full_path", replace_path):
                status, headers, body = self.request("/song.mp3")
            self.assertEqual(status, 404)
            self.assertNotIn(b"private", body)


if __name__ == "__main__":
    unittest.main()
