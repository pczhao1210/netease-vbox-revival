import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spk_platform.auth import initialize_tokens, token_file
from spk_platform.services import Services, media_ticket
from spk_platform.settings import Settings
from spk_platform.workers import create_media_app


class MediaWorkerTests(unittest.TestCase):
    def test_tickets_range_and_separate_internal_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            keys = root / "keys"
            initialize_tokens(keys)
            media = root / "media"
            media.mkdir()
            identifier = "a" * 64
            (media / (identifier + ".mp3")).write_bytes(b"0123456789")
            app = create_media_app(keys, media, root / "local")
            with TestClient(app) as client:
                self.assertEqual(client.get("/health").status_code, 200)
                self.assertEqual(client.get("/internal/library").status_code, 403)
                self.assertEqual(
                    client.get(
                        "/internal/library",
                        headers={"Authorization": "Bearer " + token_file(keys, "device")},
                    ).status_code,
                    403,
                )
                self.assertEqual(client.get("/media/" + identifier + ".mp3").status_code, 403)
                expires = int(time.time()) + 60
                params = {
                    "playback_id": "job1",
                    "expires": expires,
                    "ticket": media_ticket(
                        token_file(keys, "media-ticket"), identifier, "job1", expires
                    ),
                }
                result = client.get(
                    "/media/" + identifier + ".mp3", params=params, headers={"Range": "bytes=2-5"}
                )
                self.assertEqual((result.status_code, result.content), (206, b"2345"))
                result = client.head("/media/" + identifier + ".mp3", params=params)
                self.assertEqual(result.headers["Content-Length"], "10")


class ServicesTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_online_track_skips_provider_resolution(self):
        calls, cached = [], set()

        def handler(request):
            body = json.loads(request.content)
            calls.append(request.url.host + ("+url" if body.get("url") else ""))
            if request.url.host == "resolver":
                return httpx.Response(200, json={"url": "https://music.example/a.mp3"})
            if not body.get("url") and body["cache_key"] not in cached:
                return httpx.Response(400, json={"detail": "No media source"})
            cached.add(body["cache_key"])
            return httpx.Response(200, json={"media_id": "a" * 64, "duration": 100})

        with tempfile.TemporaryDirectory() as directory:
            keys = Path(directory) / "keys"
            initialize_tokens(keys)
            services = Services(
                keys,
                {"media": "http://media", "resolver": "http://resolver"},
                httpx.MockTransport(handler),
            )
            config = Settings()
            config.device.advertised_media_url = "http://192.168.1.5:8899"
            payload = {
                "kind": "music",
                "track": {"provider": "ncm", "id": "ENC", "original_id": "1"},
            }
            try:
                await services.prepare("a" * 32, payload, config, None)
                self.assertEqual(calls, ["media", "resolver", "media+url"])
                calls.clear()
                media = await services.prepare("b" * 32, payload, config, None)
                self.assertEqual(calls, ["media"])
                self.assertIn("playback_id=" + "b" * 32, media["url"])
                calls.clear()
                config.music.cache_enabled = False
                await services.prepare("c" * 32, payload, config, None)
                self.assertEqual(calls, ["resolver", "media+url"])
            finally:
                await services.close()


if __name__ == "__main__":
    unittest.main()
