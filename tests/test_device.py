import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class DeviceProtocolTests(unittest.TestCase):
    def test_lock_recovers_without_blocking_on_reused_or_missing_pid(self):
        script = Path(__file__).resolve().parents[1] / "device/spk_agent_v2.sh"
        program = r"""
. "$1"
AGENT_PATH=$1 RUN=$2
acquire_lock || exit 1
[ "$(readlink "$LOCK")" = "$$" ] || exit 2
acquire_lock && exit 3
release_lock
[ ! -L "$LOCK" ] || exit 4
"""
        for owner in (None, "2147483647", str(os.getpid()), "invalid"):
            with self.subTest(owner=owner), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "lock").mkdir()
                if owner is not None:
                    (root / "agent.pid").symlink_to(owner)
                result = subprocess.run(
                    ["/bin/sh", "-c", program, "sh", str(script), directory],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    env={"PATH": os.environ.get("PATH", ""), "SPK_DIR": directory},
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_quiet_schedule_replacement_preserves_the_old_file_on_failure(self):
        script = Path(__file__).resolve().parents[1] / "device/spk_agent_v2.sh"
        program = r"""
. "$1"
DIR=$2 ARGUMENT=22,8
mv() { [ "$(cat "$DIR/quiet.conf")" = '21 7' ] || exit 5; return 1; }
update_quiet_schedule && exit 1
[ "$(cat "$DIR/quiet.conf")" = '21 7' ] || exit 2
[ ! -f "$DIR/.quiet.conf.$$" ] || exit 3
unset -f mv
update_quiet_schedule || exit 4
mv() { exit 6; }
update_quiet_schedule
"""
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "quiet.conf"
            config.write_text("21 7\n")
            result = subprocess.run(
                ["/bin/sh", "-c", program, "sh", str(script), directory],
                capture_output=True,
                text=True,
                timeout=2,
                env={"PATH": os.environ.get("PATH", ""), "SPK_DIR": directory},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config.read_text(), "22 8\n")

    def test_player_id_uses_existing_controller_evidence(self):
        script = Path(__file__).resolve().parents[1] / "device/spk_agent_v2.sh"
        for text, expected in [
            ("Player(1) not the curPlayer(2)", "2"),
            ('cmdId:0x601 {"playerId":1}', "1"),
            ("missing", "1"),
            ("curPlayer(99)", "1"),
        ]:
            with self.subTest(text=text):
                result = subprocess.run(
                    ["/bin/sh", str(script), "--player-id", text],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=True,
                )
                self.assertEqual(result.stdout.strip(), expected)

    def test_device_accepts_only_typed_commands(self):
        script = Path(__file__).resolve().parents[1] / "device/spk_agent_v2.sh"
        prefix = "v2\t" + "a" * 32 + "\t12\t30\t"
        valid = [
            "play\thttp://192.168.1.5:8899/media/a.mp3?expires=123&ticket=abc",
            "stop\tnight",
            "quiet_schedule\t21,7",
        ]
        invalid = [
            "shell\treboot",
            "reboot\t-",
            "flash\t/dev/mtd0",
            "dsp\tfirmware.bin",
            "volume\t100",
            "seek\t0",
            'play\thttp://host/";reboot',
            "play\tfile:///etc/passwd",
            "quiet_schedule\t25,7",
            "stop\tnight\textra",
            "play\thttp://host/x\nreboot",
        ]
        for value in valid + invalid:
            with self.subTest(value=value):
                result = subprocess.run(
                    ["/bin/sh", str(script), "--validate", prefix + value],
                    capture_output=True,
                    timeout=2,
                )
                self.assertEqual(result.returncode == 0, value in valid)

    def test_lost_player_reports_are_retried(self):
        script = Path(__file__).resolve().parents[1] / "device/spk_agent_v2.sh"
        program = r"""
. "$1"
player_line() { printf '%s\n' "$LINE_VALUE"; }
report() { attempts=$((attempts + 1)); printf '%s ' "$1"; [ "$attempts" -gt 1 ]; }
PLAY_ID=current PLAY_GENERATION=1 LAST_LINE= SEEN_PLAY=0 attempts=0
LINE_VALUE='playerCallbackHandler(755) playerId:1 st:0x3'
observe; observe; observe
attempts=0
LINE_VALUE='playerCallbackHandler(755) playerId:1 st:0x0'
observe; observe; observe
printf '[%s] ' "$PLAY_ID"
PLAY_ID=next SEEN_PLAY=1 attempts=1
LINE_VALUE='playerCallbackHandler(755) playerId:1 st:0x4'
observe
printf '[%s]\n' "$PLAY_ID"
"""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                ["/bin/sh", "-c", program, "sh", str(script)],
                capture_output=True,
                text=True,
                timeout=2,
                env={"PATH": os.environ.get("PATH", ""), "SPK_DIR": directory},
            )
        self.assertEqual(result.stdout.strip(), "playing playing completed completed [] stopped []")


if __name__ == "__main__":
    unittest.main()
