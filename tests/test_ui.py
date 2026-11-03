import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import re

from test_control import FakeServices

from spk_platform.auth import password_hash
from spk_platform.control import create_control_app
from spk_platform.display import TEMPLATES


class UITests(unittest.TestCase):
    def test_all_html_templates_parse_without_using_cached_compilation(self):
        for name in TEMPLATES.list_templates(filter_func=lambda name: name.endswith(".html")):
            with self.subTest(template=name):
                source, filename, fresh = TEMPLATES.loader.get_source(TEMPLATES, name)
                TEMPLATES.parse(source, name=name, filename=filename)

    def test_music_entries_follow_authorization_and_capabilities(self):
        helper = Path(__file__).resolve().parents[1] / "src/spk_platform/static/music-access.js"
        script = """
const {collectionAccess} = require(process.argv[1]);
const permissions = {daily:{available:true},liked:{available:true},favorites:{available:false}};
console.log(JSON.stringify([
  collectionAccess({state:'unauthorized'}, permissions),
  collectionAccess({state:'pending'}, permissions),
  collectionAccess({state:'authorized'}, null),
  collectionAccess({state:'authorized'}, permissions),
  collectionAccess({state:'expired'}, permissions)
]));
"""
        result = subprocess.run(
            ["node", "-e", script, str(helper)],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        states = json.loads(result.stdout)
        for index in (0, 1, 2, 4):
            self.assertFalse(any(item["enabled"] for item in states[index].values()))
        self.assertTrue(states[3]["daily"]["enabled"])
        self.assertTrue(states[3]["liked"]["enabled"])
        self.assertFalse(states[3]["favorites"]["enabled"])
        self.assertEqual(states[4]["liked"]["reason"], "未绑定网易云账号")

    def test_eink_display_works_without_javascript_and_requires_login(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_control_app(
                root / "state", root / "keys", "http://testserver", FakeServices()
            )
            with TestClient(app) as client:
                app.state.store.set_meta("admin_password", password_hash("long-test-password"))
                login = client.get("/display")
                self.assertIn("显示屏登录", login.text)
                self.assertNotIn("<script", login.text)
                csrf = re.search('name="csrf" value="([^"]+)"', login.text).group(1)
                rejected = client.post(
                    "/display/login",
                    data={"csrf": csrf, "password": "long-test-password"},
                    headers={"Origin": "https://evil.invalid"},
                )
                self.assertEqual(rejected.status_code, 403)
                response = client.post(
                    "/display/login",
                    data={"csrf": csrf, "password": "long-test-password"},
                    headers={"Origin": "http://testserver"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn('http-equiv="refresh" content="60"', response.text)
                self.assertNotIn("<script", response.text)
                self.assertIn("等待播放", response.text)
                revision, config = app.state.store.config()
                config.appearance.theme = "dark"
                app.state.store.apply(app.state.store.save(config, revision))
                self.assertIn('data-theme="dark"', client.get("/").text)

                snapshot = {
                    "current": {
                        "intent": {"title": "正在播放的歌 <script>", "artist": "歌手"},
                        "status": "playing",
                        "source": "ui",
                        "kind": "music",
                    },
                    "queue": [{"intent": {"title": "下一首", "artist": "第二位歌手"}}],
                    "device_state": "playing",
                }
                with patch.object(app.state.player, "snapshot", return_value=snapshot):
                    page = client.get("/display?layout=landscape")
                    self.assertIn("layout-landscape", page.text)
                    self.assertIn("正在播放的歌 &lt;script&gt;", page.text)
                    self.assertIn("歌手", page.text)
                    self.assertIn("网页点歌", page.text)
                    self.assertIn("下一首", page.text)
                    self.assertNotIn("<script>", page.text)
                    self.assertNotIn("<script src", page.text)
                self.assertEqual(client.get("/display?layout=bad").status_code, 422)

    def test_ui_and_assets_load_without_cloud_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_control_app(
                root / "state", root / "keys", "http://testserver", FakeServices()
            )
            with TestClient(app) as client:
                result = client.get("/")
                self.assertEqual(result.status_code, 200)
                self.assertIn("网易音箱", result.text)
                self.assertIn('id="view-music"', result.text)
                self.assertEqual(client.get("/static/app.js").status_code, 200)
                self.assertEqual(client.get("/static/app.css").status_code, 200)
                self.assertEqual(client.get("/static/lucide.js").status_code, 200)
                self.assertIn("frame-ancestors 'none'", result.headers["Content-Security-Policy"])


if __name__ == "__main__":
    unittest.main()
