import asyncio
import io
import os
import shutil
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spk_platform.assets import audio_info
from spk_platform.auth import initialize_tokens, token_file
from spk_platform.control import create_control_app
from spk_platform.playback import Intent, Track
from spk_platform.services import Services
from spk_platform.workers import create_media_app


class Router(httpx.AsyncBaseTransport):
    def __init__(self, media):
        self.media = httpx.ASGITransport(app=media)

    async def handle_async_request(self, request):
        if request.url.host == "media":
            return await self.media.handle_async_request(request)
        return httpx.Response(503, json={"detail": "Offline fixture: service disabled"})


class EndToEndTests(unittest.TestCase):
    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required"
    )
    def test_cached_playback_without_cloud_and_stale_device_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            keys = root / "keys"
            initialize_tokens(keys)
            media = create_media_app(keys, root / "media", root / "local")
            services = Services(keys, {"media": "http://media", "mcp": "http://mcp"}, Router(media))
            control = create_control_app(root / "state", keys, "https://testserver", services)
            with TestClient(
                control, base_url="https://testserver", client=("192.168.1.20", 42)
            ) as client:
                session = client.get("/api/session").json()
                headers = {"Origin": "https://testserver", "X-CSRF-Token": session["csrf"]}
                setup = client.post(
                    "/api/setup",
                    headers=headers,
                    json={
                        "password": "offline-fixture-password",
                        "bootstrap": token_file(keys, "bootstrap"),
                    },
                )
                self.assertEqual(setup.status_code, 200)
                headers["X-CSRF-Token"] = setup.json()["csrf"]
                configuration = client.get("/api/config").json()
                config = configuration["config"]
                config["device"].update(
                    {
                        "ip": "192.168.1.20",
                        "allowed_cidrs": ["192.168.1.20/32"],
                        "advertised_media_url": "http://192.168.1.5:8899",
                    }
                )
                config["music"]["night_enabled"] = False
                saved = client.post(
                    "/api/config",
                    headers=headers,
                    json={"expected_revision": configuration["saved_revision"], "config": config},
                ).json()
                client.post(
                    "/api/config/apply", headers=headers, json={"revision": saved["revision"]}
                )
                buffer = io.BytesIO()
                with wave.open(buffer, "wb") as sample:
                    sample.setnchannels(1)
                    sample.setsampwidth(2)
                    sample.setframerate(16000)
                    sample.writeframes(bytes(6400))
                uploaded = client.post(
                    "/api/music/upload",
                    content=buffer.getvalue(),
                    headers={**headers, "X-Filename": "Offline.wav"},
                )
                self.assertEqual(uploaded.status_code, 200)
                track = uploaded.json()
                library = client.get("/api/music/library").json()
                self.assertEqual(library["cache"][0]["title"], "Offline")
                submitted = client.post(
                    "/api/playback",
                    headers=headers,
                    json={"request_id": "cached-play", "intent": {"track": track}},
                )
                self.assertEqual(submitted.status_code, 200)

                async def ready():
                    await control.state.player.task

                client.portal.call(ready)
                self.assertEqual(
                    client.get("/api/playback").json()["current"]["status"], "buffering"
                )
                device = {"Authorization": "Bearer " + token_file(keys, "device")}
                play = None
                for _command in range(3):
                    response = client.get("/device/cmd", headers=device)
                    self.assertEqual(response.status_code, 200)
                    version, identifier, generation, ttl, action, argument = (
                        response.text.strip().split("\t")
                    )
                    self.assertEqual(version, "v2")
                    if action == "play":
                        play = (identifier, generation, argument)
                    returned = client.post(
                        "/device/result",
                        headers=device,
                        content=urlencode(
                            {"id": identifier, "generation": generation, "result": "accepted"}
                        ),
                    )
                    self.assertTrue(returned.json()["accepted"])
                self.assertIsNotNone(play)
                path = urlsplit(play[2])
                with TestClient(media) as reader:
                    received = reader.get(
                        path.path + "?" + path.query, headers={"Range": "bytes=0-99"}
                    )
                    self.assertEqual((received.status_code, len(received.content)), (206, 100))
                self.assertEqual(client.get("/api/playback").json()["audible"], "unknown")
                client.post("/api/playback/stop", headers=headers, json={})
                late = client.post(
                    "/device/result",
                    headers=device,
                    content=urlencode({"id": play[0], "generation": play[1], "result": "playing"}),
                )
                self.assertFalse(late.json()["accepted"])
                next_command = client.get("/device/cmd", headers=device).text
                self.assertIn("\tstop\t", next_command)

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required"
    )
    def test_wake_resume_serves_the_rest_of_the_song(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"SPK_DEVICE_HOLD": "0"}),
        ):
            root = Path(directory)
            keys = root / "keys"
            initialize_tokens(keys)
            media = create_media_app(keys, root / "media", root / "local")
            services = Services(keys, {"media": "http://media", "mcp": "http://mcp"}, Router(media))
            control = create_control_app(root / "state", keys, "https://testserver", services)
            store = control.state.store
            revision, config = store.config()
            config.device.ip = "192.168.1.20"
            config.device.allowed_cidrs = ["192.168.1.20/32"]
            config.device.advertised_media_url = "http://192.168.1.5:8899"
            config.music.night_enabled = False
            store.apply(store.save(config, revision))
            device = {"Authorization": "Bearer " + token_file(keys, "device")}
            voice = {"Authorization": "Bearer " + token_file(keys, "voice")}
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as sample:
                sample.setnchannels(1)
                sample.setsampwidth(2)
                sample.setframerate(16000)
                sample.writeframes(bytes(16000 * 2 * 30))
            with TestClient(control, client=("192.168.1.20", 42)) as client:
                player = control.state.player

                async def upload():
                    response = await services.http.post(
                        "http://media/internal/upload",
                        content=buffer.getvalue(),
                        headers={"Authorization": "Bearer " + token_file(keys, "media")},
                    )
                    return response.json()

                async def submit(track):
                    player.submit(Intent(track=Track.model_validate(track)), "song")
                    while player.task and not player.task.done():
                        await asyncio.gather(player.task, return_exceptions=True)

                async def settle():
                    while player.task and not player.task.done():
                        await asyncio.gather(player.task, return_exceptions=True)

                async def duration(url):
                    parts = urlsplit(url)
                    response = await services.http.get(
                        "http://media" + parts.path + "?" + parts.query
                    )
                    response.raise_for_status()
                    path = root / (parts.path.rsplit("/", 1)[-1])
                    path.write_bytes(response.content)
                    return audio_info(path)["duration"]

                def play_command():
                    play = None
                    while (
                        response := client.get("/device/cmd", headers=device)
                    ).status_code == 200:
                        fields = response.text.strip().split("\t")
                        play = fields if fields[4] == "play" else play
                        result = {"id": fields[1], "generation": fields[2], "result": "accepted"}
                        client.post("/device/result", headers=device, content=urlencode(result))
                    return play

                client.portal.call(submit, client.portal.call(upload))
                play = play_command()
                result = {"id": play[1], "generation": play[2], "result": "playing"}
                client.post("/device/result", headers=device, content=urlencode(result))
                store.db.execute("UPDATE jobs SET updated=updated-15 WHERE status='playing'")
                interrupted = client.post("/internal/voice/interrupt", headers=voice, json={})
                self.assertTrue(interrupted.json()["interrupted_owned_playback"])
                resumed = client.post("/internal/voice/done", headers=voice, json={})
                self.assertTrue(resumed.json()["resumed"])
                client.portal.call(settle)
                rest = play_command()
                self.assertNotEqual(urlsplit(rest[5]).path, urlsplit(play[5]).path)
                self.assertAlmostEqual(
                    client.portal.call(duration, rest[5]),
                    client.portal.call(duration, play[5]) - 12,
                    delta=1.2,
                )


if __name__ == "__main__":
    unittest.main()
