import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spk_platform.playback import Intent, Playback, Track
from spk_platform.state import Conflict, State


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.state = State(self.root / "data", self.root / "keys" / "master.key")
        revision, config = self.state.config()
        config.device.advertised_media_url = "http://192.168.1.5:8899"
        config.music.night_enabled = False
        self.state.apply(self.state.save(config, revision))
        self.started = asyncio.Event()
        self.release = asyncio.Event()

        async def prepare(identifier, payload, config):
            self.started.set()
            await self.release.wait()
            return {"url": config.device.advertised_media_url + "/media/" + identifier + ".mp3"}

        self.player = Playback(self.state, prepare)
        self.player.recover()

    async def asyncTearDown(self):
        await self.player.close()
        self.state.close()
        self.directory.cleanup()

    def intent(self, identifier="one", enqueue=False):
        return Intent(
            track=Track(provider="local", id=identifier, title=identifier), enqueue=enqueue
        )

    async def settle(self):
        while self.player.task and not self.player.task.done():
            await asyncio.gather(self.player.task, return_exceptions=True)

    def deliver(self):
        play = None
        while (command := self.player.poll()) is not None:
            self.player.report(command["id"], command["generation"], "accepted")
            if command["action"] == "play":
                play = command
        return play

    def current_track(self):
        current = self.player.snapshot()["current"]
        return current and current["intent"] and current["intent"]["id"]

    def queued_tracks(self):
        return [item["intent"]["id"] for item in self.player.snapshot()["queue"]]

    async def test_failed_preparation_continues_with_next_track(self):
        async def prepare(identifier, payload, config):
            if payload["track"]["id"] == "broken":
                raise RuntimeError("resolver failed")
            return {"url": "http://192.168.1.5:8899/media/x.mp3", "duration": 60}

        self.player.prepare = prepare
        broken = self.player.submit(self.intent("broken"), "broken")
        self.player.submit(self.intent("good", enqueue=True), "good")
        await self.settle()
        self.assertEqual(self.current_track(), "good")
        status = self.state.db.execute("SELECT status FROM jobs WHERE id=?", (broken,))
        self.assertEqual(status.fetchone()["status"], "failed")

    async def test_device_failure_advances_and_failed_alarm_restores_quiet(self):
        self.release.set()
        self.player.submit(Intent(track=Track(provider="local", id="alarm"), kind="alarm"), "a")
        await self.settle()
        play = self.deliver()
        self.assertTrue(self.player.report(play["id"], play["generation"], "failed"))
        self.assertIsNone(self.player.snapshot()["current"])
        self.assertEqual(self.player.poll()["action"], "quiet_on")
        self.player.submit(self.intent("first"), "first")
        self.player.submit(self.intent("second", enqueue=True), "second")
        await self.settle()
        play = self.deliver()
        self.player.report(play["id"], play["generation"], "failed")
        await self.settle()
        self.assertEqual(self.current_track(), "second")

    async def test_speech_and_wake_interrupt_keep_the_queue(self):
        self.release.set()
        self.player.submit(self.intent("first"), "first")
        self.player.submit(self.intent("second", enqueue=True), "second")
        await self.settle()
        self.player.submit(Intent(kind="tts", text="answer"), "answer")
        await self.settle()
        self.assertEqual(self.player.snapshot()["current"]["kind"], "tts")
        self.assertEqual(self.queued_tracks(), ["second"])
        self.assertTrue(self.player.interrupt())
        self.assertFalse(self.player.interrupt())
        self.assertIsNone(self.player.snapshot()["current"])
        self.assertEqual(self.queued_tracks(), ["second"])
        self.player.next()
        await self.settle()
        self.assertEqual(self.current_track(), "second")

    async def test_repeat_one_keeps_queue_and_early_device_stop_does_not_advance(self):
        async def prepare(identifier, payload, config):
            return {"url": "http://192.168.1.5:8899/media/x.mp3", "duration": 300}

        self.player.prepare = prepare
        revision, config = self.state.config()
        config.music.repeat = "one"
        self.state.apply(self.state.save(config, revision))
        self.player.submit(self.intent("first"), "first")
        self.player.submit(self.intent("second", enqueue=True), "second")
        await self.settle()
        play = self.deliver()
        self.player.report(play["id"], play["generation"], "playing")
        self.state.db.execute("UPDATE jobs SET updated=updated-300 WHERE status='playing'")
        self.player.report(play["id"], play["generation"], "completed")
        await self.settle()
        self.assertEqual(self.current_track(), "first")
        self.assertEqual(self.queued_tracks(), ["second"])
        play = self.deliver()
        self.player.report(play["id"], play["generation"], "playing")
        self.player.report(play["id"], play["generation"], "completed")
        await self.settle()
        self.assertIsNone(self.player.snapshot()["current"])
        self.assertEqual(self.queued_tracks(), ["second"])

    async def test_next_cancels_remote_preparation(self):
        cancelled = []

        async def cancel(identifier):
            cancelled.append(identifier)

        self.player.on_cancel = cancel
        first = self.player.submit(self.intent("first"), "first")
        await self.started.wait()
        self.player.next()
        await asyncio.gather(*self.player.background)
        self.assertEqual(cancelled, [first])

    async def test_replay_restarts_and_previous_ignores_removed_items(self):
        self.release.set()
        self.player.submit(self.intent("first"), "first")
        removed = self.player.submit(self.intent("removed", enqueue=True), "removed")
        self.player.remove(removed)
        with self.assertRaises(ValueError):
            self.player.previous()
        self.player.submit(self.intent("second", enqueue=True), "second")
        self.player.submit(self.intent("third", enqueue=True), "third")
        self.player.next()
        await self.settle()
        self.player.replay()
        await self.settle()
        self.assertEqual(self.current_track(), "second")
        self.assertEqual(self.queued_tracks(), ["third"])
        self.player.previous()
        await self.settle()
        self.assertEqual(self.current_track(), "second")
        self.assertEqual(self.queued_tracks(), ["third"])

    async def test_enqueue_after_restart_starts_playback(self):
        self.release.set()
        self.player.submit(self.intent("first"), "first")
        await self.settle()
        self.player.recover()
        self.player.submit(self.intent("second", enqueue=True), "second")
        await self.settle()
        self.assertEqual(self.current_track(), "second")

    def use_media(self, duration):
        async def prepare(identifier, payload, config):
            # A resume excerpt is shorter by its start offset, like the media worker's.
            remaining = duration - payload.get("start", 0)
            return {"url": "http://192.168.1.5:8899/media/x.mp3", "duration": remaining}

        self.player.prepare = prepare

    def update_config(self, section, **values):
        revision, config = self.state.config()
        for key, value in values.items():
            setattr(getattr(config, section), key, value)
        self.state.apply(self.state.save(config, revision))

    def start_playing(self, seconds_ago=0):
        play = self.deliver()
        self.player.report(play["id"], play["generation"], "playing")
        self.state.db.execute(
            "UPDATE jobs SET updated=updated-? WHERE status='playing'", (seconds_ago,)
        )
        return play

    def current_start(self):
        row = self.state.db.execute(
            "SELECT payload FROM jobs WHERE id=?", (self.state.meta("current"),)
        ).fetchone()
        return json.loads(row["payload"])["start"]

    async def test_next_track_starts_inside_the_silent_tail_and_is_prefetched(self):
        self.use_media(100)
        warmed = []

        async def prefetch(identifier, payload, config):
            warmed.append(payload["track"]["id"])

        self.player.prefetch = prefetch
        first = self.player.submit(self.intent("first"), "first")
        self.player.submit(self.intent("second", enqueue=True), "second")
        await self.settle()
        play = self.start_playing()
        delay = self.player.end_timer.when() - asyncio.get_running_loop().time()
        self.assertAlmostEqual(delay, 100 - 45 + 1.5, delta=1)
        await asyncio.sleep(0)
        self.assertEqual(warmed, ["second"])
        self.player._timed_end(first, play["generation"])
        await self.settle()
        self.assertEqual(self.current_track(), "second")
        play = self.start_playing()
        self.player._timed_end(self.state.meta("current"), play["generation"])
        self.assertIsNone(self.player.snapshot()["current"])
        self.assertEqual(self.player.poll()["action"], "stop")

    async def test_device_pause_before_the_end_does_not_advance(self):
        self.use_media(300)
        self.player.submit(self.intent("first"), "first")
        self.player.submit(self.intent("second", enqueue=True), "second")
        await self.settle()
        play = self.start_playing(seconds_ago=30)
        self.player.report(play["id"], play["generation"], "stopped")
        await self.settle()
        self.assertIsNone(self.player.snapshot()["current"])
        self.assertIsNone(self.player.end_timer)
        self.assertEqual(self.queued_tracks(), ["second"])

    async def test_wake_resumes_the_interrupted_song_near_its_position(self):
        self.use_media(300)
        self.player.submit(self.intent("first"), "first")
        self.player.submit(self.intent("second", enqueue=True), "second")
        await self.settle()
        self.start_playing(seconds_ago=60)
        self.assertTrue(self.player.interrupt())
        self.assertIsNone(self.player.snapshot()["current"])
        self.assertTrue(self.player.resume())
        await self.settle()
        self.assertEqual(self.current_track(), "first")
        self.assertAlmostEqual(self.current_start(), 57, delta=1)
        self.assertEqual(self.queued_tracks(), ["second"])
        self.start_playing(seconds_ago=20)
        self.player.interrupt()
        self.player.resume()
        await self.settle()
        self.assertAlmostEqual(self.current_start(), 74, delta=1)
        self.start_playing(seconds_ago=180)
        self.player.interrupt()
        self.player.resume()
        await self.settle()
        self.assertEqual(self.current_track(), "second")

    async def test_reply_waits_for_the_session_and_navigation_discards_resume(self):
        self.use_media(300)
        self.player.submit(self.intent("first"), "first")
        self.player.submit(self.intent("second", enqueue=True), "second")
        await self.settle()
        self.start_playing(seconds_ago=60)
        self.player.listening_until = time.monotonic() + 60
        self.player.interrupt()
        reply = self.player.submit(Intent(kind="tts", text="answer"), "answer")
        await self.settle()
        play = self.start_playing()
        self.player._timed_end(reply, play["generation"])
        self.assertIsNone(self.player.snapshot()["current"])
        self.player.listening_until = 0
        self.assertTrue(self.player.resume())
        await self.settle()
        self.assertEqual(self.current_track(), "first")
        self.player.interrupt()
        self.player.next()
        await self.settle()
        self.assertEqual(self.current_track(), "second")
        self.assertFalse(self.player.resume())

    async def test_disabled_resume_keeps_wake_as_a_stop(self):
        self.use_media(300)
        self.update_config("speech", resume_seconds=0)
        self.player.submit(self.intent("first"), "first")
        await self.settle()
        self.start_playing(seconds_ago=60)
        self.assertTrue(self.player.interrupt())
        self.assertFalse(self.player.resume())
        self.assertIsNone(self.player.snapshot()["current"])

    async def test_cancelled_resolver_never_plays(self):
        self.player.submit(self.intent(), "request-one")
        await self.started.wait()
        old_task = self.player.task
        self.player.stop()
        self.release.set()
        await asyncio.gather(old_task, return_exceptions=True)
        commands = self.state.db.execute("SELECT * FROM commands WHERE status='queued'").fetchall()
        self.assertEqual([row["action"] for row in commands], ["stop"])

    async def test_fifo_ack_and_result_association(self):
        self.release.set()
        self.player.submit(self.intent(), "request-one")
        await self.player.task
        stop = self.player.poll()
        self.assertEqual(stop["action"], "stop")
        self.assertIsNone(self.player.poll())
        self.assertTrue(self.player.report(stop["id"], stop["generation"], "accepted"))
        schedule = self.player.poll()
        self.assertEqual(schedule["action"], "quiet_schedule")
        self.player.report(schedule["id"], schedule["generation"], "accepted")
        play = self.player.poll()
        self.assertEqual(play["action"], "play")
        self.assertFalse(self.player.report(play["id"], play["generation"] - 1, "playing"))
        self.assertTrue(self.player.report(play["id"], play["generation"], "playing"))
        self.assertEqual(self.player.snapshot()["current"]["status"], "playing")
        self.player.stop()
        self.assertFalse(self.player.report(play["id"], play["generation"], "playing"))

    async def test_request_id_is_idempotent(self):
        first = self.player.submit(self.intent(), "same-request")
        self.assertEqual(first, self.player.submit(self.intent(), "same-request"))
        with self.assertRaises(Conflict):
            self.player.submit(self.intent("different"), "same-request")

    async def test_queue_and_restart_do_not_replay_stale_jobs(self):
        self.release.set()
        self.player.submit(self.intent(), "one")
        await self.player.task
        self.player.submit(self.intent("two", enqueue=True), "two")
        self.assertEqual(len(self.player.snapshot()["queue"]), 1)
        self.player.next()
        await self.player.task
        self.assertEqual(self.player.snapshot()["current"]["intent"]["id"], "two")
        self.player.recover()
        self.assertIsNone(self.player.poll())
        self.assertEqual(self.player.snapshot()["current"]["status"], "unknown")

    async def test_expired_command_is_not_delivered(self):
        self.release.set()
        self.player.submit(self.intent(), "one")
        await self.player.task
        self.state.db.execute("UPDATE commands SET expires=0")
        self.assertIsNone(self.player.poll())

    async def test_confirmed_long_track_can_complete_after_command_ttl(self):
        self.release.set()
        self.player.submit(self.intent(), "long-track")
        await self.player.task
        stop = self.player.poll()
        self.player.report(stop["id"], stop["generation"], "accepted")
        schedule = self.player.poll()
        self.player.report(schedule["id"], schedule["generation"], "accepted")
        play = self.player.poll()
        self.player.report(play["id"], play["generation"], "playing")
        self.player.submit(self.intent("next", enqueue=True), "next-track")
        self.state.db.execute("UPDATE commands SET expires=0 WHERE id=?", (play["id"],))
        self.assertTrue(self.player.report(play["id"], play["generation"], "completed"))
        await self.player.task
        self.assertEqual(self.player.snapshot()["current"]["intent"]["id"], "next")


if __name__ == "__main__":
    unittest.main()
