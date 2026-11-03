import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spk_platform.settings import Settings
from spk_platform.state import Conflict, State


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = State(self.root / "data", self.root / "keys" / "master.key")

    def tearDown(self):
        self.state.close()
        self.temp.cleanup()

    def test_revision_and_rollback(self):
        original, config = self.state.config()
        config.llm.model = "new-model"
        saved = self.state.save(config, original)
        self.assertEqual(self.state.config()[0], original)
        self.state.apply(saved)
        self.assertEqual(self.state.config()[1].llm.model, "new-model")
        self.state.apply(original)
        self.assertEqual(self.state.config()[0], original)
        with self.assertRaises(Conflict):
            self.state.save(config, original)

    def test_encrypted_secret_and_rotation(self):
        self.state.put_secret("llm", "test-secret-one")
        self.state.put_secret("llm", "test-secret-two")
        self.assertEqual(self.state.secret("llm"), "test-secret-two")
        self.assertEqual(self.state.secret("llm", 1), "test-secret-one")
        self.assertNotIn("test-secret", json.dumps(self.state.secret_metadata()))
        row = self.state.db.execute("SELECT ciphertext FROM secrets LIMIT 1").fetchone()
        self.assertNotIn(b"test-secret", row["ciphertext"])
        with self.assertRaises(ValueError):
            self.state.put_secret("llm", "********")
        self.state.put_secret("llm", "", clear=True)
        self.assertFalse(self.state.secret_metadata()["llm"]["configured"])

    def test_restart_and_missing_master_key(self):
        self.state.put_secret("llm", "kept")
        self.state.close()
        self.state = State(self.root / "data", self.root / "keys" / "master.key")
        self.assertEqual(self.state.secret("llm"), "kept")
        with self.assertRaises(RuntimeError):
            State(self.root / "data", self.root / "missing.key")

    def test_applied_snapshot_keeps_credential_version(self):
        self.state.put_secret("llm", "old-secret")
        revision, config = self.state.config(applied=False)
        revision = self.state.save(config, revision)
        self.state.apply(revision)
        self.state.put_secret("llm", "new-secret")
        active = self.state.config()[1]
        self.assertEqual(self.state.secret("llm", active.credential_versions["llm"]), "old-secret")
        self.assertEqual(self.state.secret("llm", 0), "")

    def test_restored_alarms_apply_with_the_matching_revision(self):
        revision, config = self.state.config(applied=False)
        restored = self.state.save(config, revision)
        alarm = {
            "id": "a" * 32,
            "hour": 8,
            "minute": 0,
            "enabled": True,
            "track": {"provider": "local", "id": "track"},
        }
        self.state.set_meta(
            "pending_alarm_restore", json.dumps({"revision": restored, "alarms": [alarm]})
        )
        self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM alarms").fetchone()[0], 0)
        self.state.apply(restored)
        self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM alarms").fetchone()[0], 1)

    def test_validation(self):
        for change in (
            {"device": {"allowed_cidrs": ["not-cidr"]}},
            {"device": {"advertised_media_url": "http://127.0.0.1:8899"}},
            {"llm": {"endpoint": "https://user:pass@example.com"}},
            {"unexpected": "field"},
            {"music": {"timezone": "No/Such_Zone"}},
            {"music": {"night_start": 3, "night_end": 3}},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                Settings.model_validate(change)

    def test_zero_revision_cannot_replace_the_applied_pointer(self):
        with self.assertRaises(ValueError):
            self.state.config(revision=0)
        self.assertGreater(self.state.config()[0], 0)

    def test_probe_results_survive_apply_and_unrelated_changes(self):
        revision, config = self.state.config(applied=False)
        fingerprint = self.state.fingerprint(config)
        self.state.db.execute("INSERT INTO probes VALUES (?, 'tools', '{}')", (fingerprint,))
        config.appearance.theme = "eink"
        config.music.cache_mb = 900
        self.state.apply(self.state.save(config, revision))
        self.assertEqual(self.state.fingerprint(self.state.config()[1]), fingerprint)
        self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM probes").fetchone()[0], 1)
        config.llm.model = "other-model"
        self.assertNotEqual(self.state.fingerprint(config), fingerprint)

    def test_theme_is_defaulted_persisted_and_rolled_back(self):
        original, config = self.state.config()
        self.assertEqual(config.appearance.theme, "light")
        for theme in ("dark", "eink"):
            saved, draft = self.state.config(applied=False)
            draft.appearance.theme = theme
            draft.appearance.eink_refresh_seconds = 90
            draft.appearance.eink_layout = "landscape"
            revision = self.state.save(draft, saved)
            self.state.apply(revision)
            self.assertEqual(self.state.config()[1].appearance.theme, theme)
            self.assertEqual(self.state.config()[1].appearance.eink_layout, "landscape")
        self.state.apply(original)
        self.assertEqual(self.state.config()[1].appearance.theme, "light")
        self.assertEqual(Settings.model_validate({}).appearance.theme, "light")
        with self.assertRaises(ValueError):
            Settings.model_validate({"appearance": {"theme": "unknown"}})


if __name__ == "__main__":
    unittest.main()
