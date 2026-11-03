import getpass
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spk_platform.auth import AdminAuth, set_admin_password, token_file
from spk_platform.cli import main, preflight
from spk_platform.state import State


class CLITests(unittest.TestCase):
    def test_admin_reset_refuses_echo_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = State(root / "state", root / "keys/master.key")
            set_admin_password(state, "original-password")
            original = state.meta("admin_password")
            arguments = [
                "reset-admin-password",
                "--state-dir",
                str(root / "state"),
                "--key-dir",
                str(root / "keys"),
            ]
            with (
                patch("spk_platform.cli.sys.stdin.isatty", return_value=True),
                patch("spk_platform.cli.getpass.getpass", side_effect=getpass.GetPassWarning),
            ):
                self.assertEqual(main(arguments), 2)
            self.assertEqual(state.meta("admin_password"), original)
            state.close()

    def test_interactive_admin_reset_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = [
                "reset-admin-password",
                "--state-dir",
                str(root / "state"),
                "--key-dir",
                str(root / "keys"),
            ]
            state = State(root / "state", root / "keys/master.key")
            set_admin_password(state, "original-password")
            state.put_secret("llm", "unchanged-secret")
            original = state.meta("admin_password")
            with patch("spk_platform.cli.sys.stdin.isatty", return_value=True):
                with patch(
                    "spk_platform.cli.getpass.getpass",
                    side_effect=["new-password-long", "mismatch"],
                ):
                    self.assertEqual(main(arguments), 2)
                self.assertEqual(state.meta("admin_password"), original)
                with patch(
                    "spk_platform.cli.getpass.getpass",
                    side_effect=["new-password-long", "new-password-long"],
                ):
                    self.assertEqual(main(arguments), 0)
            AdminAuth(state, "http://localhost").login("new-password-long")
            self.assertEqual(state.secret("llm"), "unchanged-secret")
            state.close()

    def test_admin_reset_rejects_noninteractive_input_and_missing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments = [
                "reset-admin-password",
                "--state-dir",
                str(root / "state"),
                "--key-dir",
                str(root / "keys"),
            ]
            with self.assertRaises(SystemExit):
                main(arguments)
            self.assertFalse((root / "state").exists())
            State(root / "state", root / "keys/master.key").close()
            with (
                patch("spk_platform.cli.sys.stdin.isatty", return_value=False),
                self.assertRaises(SystemExit),
            ):
                main(arguments)

    def test_init_creates_private_state_without_cloud_or_device(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(
                main(["init", "--state-dir", str(root / "state"), "--key-dir", str(root / "keys")]),
                0,
            )
            self.assertTrue((root / "state/control.sqlite3").is_file())
            self.assertGreater(len(token_file(root / "keys", "device")), 32)
            self.assertEqual((root / "keys/master.key").stat().st_mode & 0o077, 0)

    def test_preflight_has_no_side_effects(self):
        result = preflight()
        self.assertFalse(result["cloud_calls"])
        self.assertFalse(result["device_access"])


if __name__ == "__main__":
    unittest.main()
