import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import httpx

from spk_platform.auth import token_file
from spk_platform.control import create_control_app
from spk_platform.playback import Intent, Track


class FakeServices:
    async def prepare(self, identifier, payload, config, state):
        return {
            "media_id": "a" * 64,
            "duration": 60,
            "url": "http://192.168.1.5:8899/media/test.mp3",
        }

    async def request(self, *args, **kwargs):
        return []

    async def cancel(self, identifier):
        pass

    async def close(self):
        pass


class ControlTests(unittest.TestCase):
    def administrator(self, client):
        session = client.get("/api/session").json()
        headers = {"Origin": "http://testserver", "X-CSRF-Token": session["csrf"]}
        setup = client.post(
            "/api/setup", headers=headers, json={"password": "long-test-password"}
        ).json()
        headers["X-CSRF-Token"] = setup["csrf"]
        return headers

    def configure(self, store, **device):
        revision, config = store.config()
        config.device.advertised_media_url = "http://192.168.1.5:8899"
        for key, value in device.items():
            setattr(config.device, key, value)
        config.music.night_enabled = False
        store.apply(store.save(config, revision))

    def test_apply_rejects_revision_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_control_app(
                root / "state", root / "keys", "http://testserver", FakeServices()
            )
            with TestClient(app) as client:
                headers = self.administrator(client)
                result = client.post("/api/config/apply", headers=headers, json={"revision": 0})
                self.assertEqual(result.status_code, 422)
                self.assertEqual(client.get("/api/config").status_code, 200)

    def test_empty_device_poll_is_held_and_commands_are_immediate(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"SPK_DEVICE_HOLD": "0.4"}),
        ):
            root = Path(directory)
            app = create_control_app(
                root / "state", root / "keys", "http://testserver", FakeServices()
            )
            self.configure(app.state.store, ip="192.168.1.20", allowed_cidrs=["192.168.1.20/32"])
            device = {"Authorization": "Bearer " + token_file(root / "keys", "device")}
            with TestClient(app, client=("192.168.1.20", 42)) as client:
                started = time.monotonic()
                self.assertEqual(client.get("/device/cmd", headers=device).status_code, 204)
                self.assertGreaterEqual(time.monotonic() - started, 0.35)

                async def stop():
                    app.state.player.stop()

                client.portal.call(stop)
                started = time.monotonic()
                self.assertIn("\tstop\t", client.get("/device/cmd", headers=device).text)
                self.assertLess(time.monotonic() - started, 0.3)

    def test_collection_play_survives_track_changes_but_not_stop(self):
        class Collections(FakeServices):
            action = None

            async def request(self, service, path, body=None, **kwargs):
                if path != "/collections":
                    return []
                self.action()
                return {"tracks": [{"provider": "ncm", "id": "ENC", "original_id": "1"}]}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            services = Collections()
            app = create_control_app(root / "state", root / "keys", "http://testserver", services)
            with TestClient(app) as client:
                headers = self.administrator(client)
                store = app.state.store
                self.configure(store)
                services.action = lambda: store.set_meta(
                    "generation", int(store.meta("generation")) + 5
                )
                body = {"kind": "daily", "request_id": "daily-one"}
                result = client.post("/api/music/collections/play", headers=headers, json=body)
                self.assertEqual(result.status_code, 200)
                services.action = app.state.player.stop
                body["request_id"] = "daily-two"
                result = client.post("/api/music/collections/play", headers=headers, json=body)
                self.assertEqual(result.status_code, 409)

    def test_voice_session_end_resumes_interrupted_music(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_control_app(
                root / "state", root / "keys", "http://testserver", FakeServices()
            )
            voice = {"Authorization": "Bearer " + token_file(root / "keys", "voice")}
            with TestClient(app) as client:
                self.configure(app.state.store)
                player = app.state.player

                async def play(name):
                    player.submit(Intent(track=Track(provider="local", id=name)), name)
                    await player.task

                client.portal.call(play, "first")
                self.assertTrue(
                    client.post("/internal/voice/interrupt", headers=voice, json={}).json()[
                        "interrupted_owned_playback"
                    ]
                )
                self.assertGreater(player.listening_until, 0)
                revision = app.state.store.config()[0]
                stopped = client.post(
                    "/internal/voice/text",
                    headers=voice,
                    json={"text": "停止", "request_id": "r", "revision": revision},
                ).json()
                self.assertFalse(stopped["listen"])
                self.assertFalse(
                    client.post("/internal/voice/done", headers=voice, json={}).json()["resumed"]
                )
                client.portal.call(play, "second")
                client.post("/internal/voice/interrupt", headers=voice, json={})
                result = client.post("/internal/voice/done", headers=voice, json={}).json()
                self.assertTrue(result["resumed"])
                self.assertEqual(player.listening_until, 0)
                self.assertEqual(player.snapshot()["current"]["source"], "resume")

    def test_list_submission_rejects_a_generation_cancelled_by_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_control_app(
                root / "state", root / "keys", "http://testserver", FakeServices()
            )
            with TestClient(app) as client:
                session = client.get("/api/session").json()
                headers = {"Origin": "http://testserver", "X-CSRF-Token": session["csrf"]}
                setup = client.post(
                    "/api/setup", headers=headers, json={"password": "long-test-password"}
                ).json()
                headers["X-CSRF-Token"] = setup["csrf"]
                generation = app.state.player.snapshot()["generation"]
                client.post("/api/playback/stop", headers=headers, json={})
                result = client.post(
                    "/api/playback/list",
                    headers=headers,
                    json={
                        "request_id": "stale-list",
                        "expected_generation": generation,
                        "tracks": [
                            {"provider": "local", "id": "one"},
                            {"provider": "local", "id": "two"},
                        ],
                    },
                )
                self.assertEqual(result.status_code, 409)
                self.assertEqual(app.state.player.snapshot()["queue"], [])

    def test_music_auth_and_capability_errors_reach_ui_without_raw_cli_output(self):
        class MissingCapability(FakeServices):
            async def request(self, *args, **kwargs):
                request = httpx.Request("POST", "http://resolver/collections")
                response = httpx.Response(
                    424, request=request, json={"detail": "当前账号未提供每日推荐"}
                )
                raise httpx.HTTPStatusError("provider", request=request, response=response)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_control_app(
                root / "state", root / "keys", "http://testserver", MissingCapability()
            )
            with TestClient(app) as client:
                session = client.get("/api/session").json()
                headers = {"Origin": "http://testserver", "X-CSRF-Token": session["csrf"]}
                setup = client.post(
                    "/api/setup", headers=headers, json={"password": "long-test-password"}
                ).json()
                headers["X-CSRF-Token"] = setup["csrf"]
                result = client.post(
                    "/api/music/collections", headers=headers, json={"kind": "daily"}
                )
                self.assertEqual(result.status_code, 424)
                self.assertEqual(result.json()["detail"], "当前账号未提供每日推荐")

    def test_wake_does_not_stop_unowned_phone_playback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_control_app(
                root / "state", root / "keys", "http://testserver", FakeServices()
            )
            with TestClient(app) as client:
                voice = {"Authorization": "Bearer " + token_file(root / "keys", "voice")}
                response = client.post("/internal/voice/interrupt", headers=voice, json={})
                self.assertFalse(response.json()["interrupted_owned_playback"])
                self.assertIsNone(app.state.player.poll())
                revision, config = app.state.store.config()
                config.device.advertised_media_url = "http://192.168.1.5:8899"
                config.music.night_enabled = False
                app.state.store.apply(app.state.store.save(config, revision))

                async def submit():
                    app.state.player.submit(
                        Intent(track=Track(provider="local", id="fixture")), "owned"
                    )
                    await app.state.player.task

                client.portal.call(submit)
                response = client.post("/internal/voice/interrupt", headers=voice, json={})
                self.assertTrue(response.json()["interrupted_owned_playback"])
                self.assertEqual(app.state.player.poll()["action"], "stop")

    def test_setup_config_and_idempotent_playback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_control_app(
                root / "state", root / "keys", "http://testserver", FakeServices()
            )
            with TestClient(app) as client:
                self.assertEqual(client.get("/api/config").status_code, 401)
                session = client.get("/api/session").json()
                headers = {"Origin": "http://testserver", "X-CSRF-Token": session["csrf"]}
                result = client.post(
                    "/api/setup", headers=headers, json={"password": "long-test-password"}
                )
                self.assertEqual(result.status_code, 200)
                headers["X-CSRF-Token"] = result.json()["csrf"]
                current = client.get("/api/config").json()
                config = current["config"]
                config["device"]["advertised_media_url"] = "http://192.168.1.5:8899"
                config["music"]["night_enabled"] = False
                saved = client.post(
                    "/api/config",
                    headers=headers,
                    json={"expected_revision": current["saved_revision"], "config": config},
                )
                self.assertEqual(saved.status_code, 200)
                self.assertNotEqual(
                    saved.json()["revision"], client.get("/api/config").json()["applied_revision"]
                )
                client.post(
                    "/api/config/apply",
                    headers=headers,
                    json={"revision": saved.json()["revision"]},
                )
                intent = {
                    "request_id": "same-id",
                    "intent": {"track": {"provider": "local", "id": "song"}},
                }
                first = client.post("/api/playback", headers=headers, json=intent)
                second = client.post("/api/playback", headers=headers, json=intent)
                self.assertEqual(first.status_code, 200)
                self.assertEqual(first.json()["playback_id"], second.json()["playback_id"])
                self.assertEqual(client.post("/api/playback/stop", json={}).status_code, 403)
                self.assertEqual(client.get("/device/cmd").status_code, 403)
                self.assertNotIn("password", str(client.get("/api/backup").json()))
                client.post("/api/playback/stop", headers=headers, json={})


if __name__ == "__main__":
    unittest.main()
