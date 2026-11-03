import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spk_platform.auth import AdminAuth, initialize_tokens, set_admin_password, token_file
from spk_platform.services import check_ticket, media_ticket
from spk_platform.state import State


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state = State(root / "data", root / "keys" / "master.key")
        self.auth = AdminAuth(self.state, "http://localhost:8080")

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def request(self, cookie="", csrf="", origin="http://localhost:8080"):
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/",
                "client": ("127.0.0.1", 42),
                "headers": [
                    (b"cookie", cookie.encode()),
                    (b"x-csrf-token", csrf.encode()),
                    (b"origin", origin.encode()),
                ],
            }
        )

    def test_password_and_csrf(self):
        self.auth.setup(self.request(), "long-test-password")
        self.auth.login("long-test-password")
        with self.assertRaises(HTTPException):
            self.auth.login("wrong-test-password")
        response = Response()
        record = self.auth.issue(response, authenticated=True)
        cookie = response.headers["set-cookie"].split(";")[0]
        self.auth.write(self.request(cookie, record["csrf"]))
        with self.assertRaises(HTTPException):
            self.auth.write(self.request(cookie, record["csrf"], "https://evil.invalid"))
        with self.assertRaises(HTTPException):
            self.auth.write(self.request(cookie, "wrong"))

    def test_password_reset_revokes_sessions_and_preserves_other_data(self):
        self.auth.setup(self.request(), "long-test-password")
        response = Response()
        self.auth.issue(response, authenticated=True)
        cookie = response.headers["set-cookie"].split(";")[0]
        self.auth.session(self.request(cookie))
        revision, config = self.state.config()
        self.state.put_secret("llm", "fixture-cloud-key")
        set_admin_password(self.state, "replacement-password")
        with self.assertRaises(HTTPException):
            self.auth.session(self.request(cookie))
        with self.assertRaises(HTTPException):
            self.auth.login("long-test-password")
        self.auth.login("replacement-password")
        self.assertEqual(self.state.config()[0], revision)
        self.assertEqual(self.state.config()[1], config)
        self.assertEqual(self.state.secret("llm"), "fixture-cloud-key")

    def test_open_status_stream_stops_after_password_reset(self):
        from test_control import FakeServices

        from spk_platform.control import create_control_app

        root = Path(self.temp.name)
        app = create_control_app(
            root / "stream-data", root / "stream-keys", "http://localhost:8080", FakeServices()
        )
        app.state.auth.setup(self.request(), "original-stream-password")
        response = Response()
        app.state.auth.issue(response, authenticated=True)
        request = self.request(response.headers["set-cookie"].split(";")[0])
        request.is_disconnected = AsyncMock(return_value=False)
        endpoint = next(
            route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/events"
        )

        async def verify():
            response = await endpoint(request)
            stream = response.body_iterator
            while not (await anext(stream)).startswith("event: playback"):
                pass
            set_admin_password(app.state.store, "replacement-stream-password")
            app.state.player.changed.set()
            self.assertIn("session-expired", await anext(stream))
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)

        try:
            asyncio.run(verify())
        finally:
            app.state.store.close()

    def test_https_alone_does_not_authorize_remote_setup(self):
        auth = AdminAuth(self.state, "https://speaker.example")
        request = self.request()
        request.scope["client"] = ("172.20.0.1", 42)
        with self.assertRaises(HTTPException):
            auth.setup(request, "long-test-password")
        with self.assertRaises(HTTPException):
            auth.setup(request, "long-test-password", "wrong", "expected-token")
        auth.setup(request, "long-test-password", "expected-token", "expected-token")

    def test_ticket_and_separate_tokens(self):
        initialize_tokens(Path(self.temp.name) / "keys")
        device = token_file(Path(self.temp.name) / "keys", "device")
        media = token_file(Path(self.temp.name) / "keys", "media")
        self.assertNotEqual(device, media)
        expires = int(time.time()) + 60
        signature = media_ticket(media, "id", "playback1", expires)
        self.assertTrue(check_ticket(media, "id", "playback1", expires, signature))
        self.assertFalse(check_ticket(media, "id", "playback2", expires, signature))
        self.assertFalse(check_ticket(media, "id", "playback1", 0, signature))
        self.assertFalse(check_ticket(media, "id", "playback1", expires, "é" * 64))

    def test_anonymous_sessions_do_not_evict_administrators(self):
        record = self.auth.issue(Response(), authenticated=True)
        for _ in range(300):
            self.auth.issue(Response())
        self.assertIn(record, self.auth.sessions.values())
        self.assertLessEqual(len(self.auth.sessions), 256)


if __name__ == "__main__":
    unittest.main()
