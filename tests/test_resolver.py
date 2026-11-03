import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spk_platform.auth import initialize_tokens, token_file
from spk_platform.resolver import collection_command, create_resolver_app, login_status, login_url


class ResolverTests(unittest.TestCase):
    def test_collection_uses_catalog_and_keeps_unplayable_tracks_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_tokens(root / "keys")
            cli = root / "fixture.js"
            cli.write_bytes(b"fixture")
            calls = []

            async def runner(arguments, timeout):
                calls.append(arguments)
                if arguments == ["login", "--check"]:
                    return 0, '{"data":{"loggedIn":true}}'
                if arguments == ["commands"]:
                    return (
                        0,
                        "recommend songs        每日推荐\nplaylist liked         红心歌曲\nplaylist collected     收藏歌单\n",
                    )
                if arguments[:2] == ["playlist", "collected"]:
                    return 0, '{"data":{"records":[{"originalId":"123","name":"收藏"}]}}'
                return (
                    0,
                    '{"data":{"records":[{"id":"encrypted","originalId":123,"name":"歌","visible":false}]}}',
                )

            app = create_resolver_app(root / "keys", root / "runtime", cli, runner=runner)
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer " + token_file(root / "keys", "resolver")}
                result = client.post("/collections", headers=headers, json={"kind": "daily"})
                self.assertEqual(result.status_code, 200)
                self.assertFalse(result.json()["tracks"][0]["playable"])
                self.assertIn(["recommend", "songs", "--userInput", "查看每日推荐歌曲"], calls)
                result = client.post(
                    "/collections", headers=headers, json={"kind": "favorites"}
                ).json()
                self.assertEqual(result["playlists"][0]["id"], "123")
                self.assertEqual(
                    client.post("/resolve", headers=headers, json={"id": "encrypted"}).status_code,
                    403,
                )

    def test_collection_command_requires_exact_whitelisted_catalog_entry(self):
        self.assertIsNone(collection_command("daily", "play   播放\n"))
        self.assertIsNone(collection_command("liked", "playlist liked_shell   执行命令\n"))
        self.assertEqual(
            collection_command("liked", "playlist liked       红心\n"), ["playlist", "liked"]
        )
        self.assertEqual(
            collection_command(
                "daily", "recommend     推荐\n  songs    每日歌曲\nuser      用户\n"
            ),
            ["recommend", "songs"],
        )
        self.assertIsNone(collection_command("liked", "user       用户\n  liked    红心\n"))

    def test_pem_private_key_is_a_value_not_a_cli_option(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_tokens(root / "keys")
            cli = root / "fixture-cli.js"
            cli.write_bytes(b"fixture")
            app = create_resolver_app(root / "keys", root / "runtime", cli)
            process = AsyncMock()
            process.returncode = 0
            process.communicate.return_value = (b"configured", b"")
            private = "-----BEGIN PRIVATE KEY-----\n" + "fixture" * 20
            with patch(
                "spk_platform.resolver.asyncio.create_subprocess_exec", return_value=process
            ) as spawn:
                with TestClient(app) as client:
                    headers = {"Authorization": "Bearer " + token_file(root / "keys", "resolver")}
                    result = client.post(
                        "/account/credentials",
                        headers=headers,
                        json={"app_id": "fixture", "private_key": private},
                    )
                    self.assertEqual(result.status_code, 200)
                    calls = [call.args for call in spawn.call_args_list]
                    self.assertIn(("privateKey", "--", private), [args[-3:] for args in calls])
                    self.assertNotIn(private, result.text)

    def test_background_exit_does_not_mean_authorized_and_qr_is_real(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_tokens(root / "keys")
            cli = root / "fixture.js"
            cli.write_bytes(b"fixture")
            checks = iter(["未登录", "已登录，登录状态有效"])

            async def runner(arguments, timeout):
                if arguments == ["login", "--background"]:
                    return 0, "登录链接: https://music.163.com/st/authorize?code=fixture"
                if arguments == ["login", "--check"]:
                    return 0, next(checks)
                return 0, "ok"

            app = create_resolver_app(root / "keys", root / "runtime", cli, runner=runner)
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer " + token_file(root / "keys", "resolver")}
                login = client.post("/account/login", headers=headers).json()
                self.assertEqual(login["state"], "pending")
                self.assertTrue(login["qr_image"].startswith("data:image/png;base64,"))
                self.assertEqual(
                    client.post("/account/poll", headers=headers).json()["state"], "pending"
                )
                bound = client.post("/account/poll", headers=headers).json()
                self.assertEqual(bound["state"], "authorized")
                self.assertNotIn("qr_url", bound)
                self.assertNotIn("output", bound)

    def test_login_parser_rejects_unknown_success_and_unsafe_urls(self):
        self.assertEqual(login_status(0, "后台进程已启动"), "unknown")
        self.assertEqual(login_status(0, "未登录，不是已登录"), "unauthorized")
        self.assertEqual(login_url("https://evil.invalid/?token=fixture"), "")
        self.assertEqual(login_url("https://user:secret@music.163.com/auth"), "")

    def test_unconfigured_and_unknown_track_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_tokens(root / "keys")
            app = create_resolver_app(root / "keys", root / "runtime", root / "missing-cli")
            with TestClient(app) as client:
                headers = {"Authorization": "Bearer " + token_file(root / "keys", "resolver")}
                self.assertEqual(client.get("/account").status_code, 403)
                account = client.get("/account", headers=headers).json()
                self.assertFalse(account["available"])
                self.assertEqual(account["state"], "unknown")
                self.assertEqual(
                    client.post("/search", headers=headers, json={"query": "test"}).status_code, 503
                )
                self.assertEqual(
                    client.post("/resolve", headers=headers, json={"id": "unknown"}).status_code,
                    400,
                )

    def test_saved_track_resolves_after_catalog_eviction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_tokens(root / "keys")
            cli = root / "node_modules/@music163/ncm-cli/dist/index.js"
            app = create_resolver_app(root / "keys", root / "runtime", cli)
            resolved = []

            def fetch(song, timeout, wait):
                resolved.append(song)
                return "http://music.example/a.mp3", "", False

            with patch("spk_alarm.fetch_url", fetch), TestClient(app) as client:
                headers = {"Authorization": "Bearer " + token_file(root / "keys", "resolver")}
                result = client.post(
                    "/resolve", headers=headers, json={"id": "ENC123", "original_id": "42"}
                )
                self.assertEqual(result.status_code, 200)
                self.assertEqual(resolved, [{"id": "ENC123", "orig": "42", "play": True}])
                rejected = client.post(
                    "/resolve", headers=headers, json={"id": "ENC123", "original_id": "4 2"}
                )
                self.assertEqual(rejected.status_code, 400)


if __name__ == "__main__":
    unittest.main()
