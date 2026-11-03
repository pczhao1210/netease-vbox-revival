import asyncio
import datetime
import hashlib
import json
import secrets
import time
from zoneinfo import ZoneInfo

from pydantic import Field

from .settings import Model
from .state import Conflict

ACTIVE = ("resolving", "buffering", "playing", "stopping")
# Assets.normalize adds a 0.5 s lead-in and a 45 s silent tail; the speaker firmware
# skips to "next" 15 s before the end, so that skip must land in the tail.
TAIL_SECONDS = 45.0
PADDING_SECONDS = 45.5
EARLY_STOP_MARGIN = 15
REWIND_SECONDS = 3
SUSPEND_TTL = 600


class Track(Model):
    provider: str = Field(pattern=r"^(local|cache|ncm)$")
    id: str = Field(min_length=1, max_length=512)
    title: str = Field(default="", max_length=200)
    artist: str = Field(default="", max_length=200)
    original_id: str = Field(default="", max_length=100)


class Intent(Model):
    track: Track | None = None
    text: str = Field(default="", max_length=4000)
    enqueue: bool = False
    kind: str = Field(default="music", pattern=r"^(music|tts|alarm)$")
    start: float = Field(default=0, ge=0, le=21600)


class Alarm(Model):
    id: str = Field(default_factory=lambda: secrets.token_hex(16), pattern=r"^[a-f0-9]{32}$")
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    enabled: bool = True
    track: Track


class Playback:
    def __init__(self, state, prepare, on_cancel=None, prefetch=None):
        self.state = state
        self.prepare = prepare
        self.on_cancel = on_cancel
        self.prefetch = prefetch
        self.background = set()
        self.warming = {}
        self.task = None
        self.end_timer = None
        self.listening_until = 0.0
        self.changed = asyncio.Event()
        self.closed = False

    def recover(self):
        with self.state.transaction():
            self.state.db.execute(
                "UPDATE commands SET status='cancelled' WHERE status IN ('queued','leased')"
            )
            self.state.db.execute(
                "UPDATE jobs SET status='unknown', updated=? WHERE status NOT IN "
                "('completed','cancelled','failed')",
                (time.time(),),
            )
            self.state.set_meta("generation", int(self.state.meta("generation", "0")) + 1)
            self.state.set_meta("device_state", "unknown")
            self.state.set_meta("suspended", "")
            self.state.set_meta("next", "")

    def _generation(self):
        generation = int(self.state.meta("generation", "0")) + 1
        self.state.set_meta("generation", generation)
        self.state.db.execute(
            "UPDATE commands SET status='cancelled' WHERE status IN ('queued','leased')"
        )
        return generation

    def command(self, action, argument, generation, playback_id="", ttl=30):
        if action not in ("play", "stop", "status", "quiet_on", "quiet_off", "quiet_schedule"):
            raise ValueError("Unsupported device action")
        if any(character in argument for character in ("\t", "\r", "\n", '"', "\\")):
            raise ValueError("Unsafe device argument")
        identifier = secrets.token_hex(16)
        now = time.time()
        self.state.db.execute(
            "INSERT INTO commands(id,generation,action,argument,priority,created,expires,playback_id) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                identifier,
                generation,
                action,
                argument,
                0 if action == "stop" else 10,
                now,
                now + ttl,
                playback_id,
            ),
        )
        return identifier

    def submit(self, intent: Intent, request_id, source="ui", keep_queue=None):
        if self.closed:
            raise RuntimeError("Controller is shutting down")
        if intent.kind != "tts" and intent.track is None:
            raise ValueError("A track is required")
        if intent.kind == "tts" and not intent.text.strip():
            raise ValueError("TTS text is empty")
        if keep_queue is None:
            keep_queue = intent.kind == "tts"
        fingerprint = hashlib.sha256(intent.model_dump_json().encode()).hexdigest()
        with self.state.transaction():
            previous = self.state.db.execute(
                "SELECT id,fingerprint FROM jobs WHERE request_id=?", (request_id,)
            ).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise Conflict("Request ID was reused with a different playback intent")
                return previous["id"]
            revision, config = self.state.config()
            if not config.device.advertised_media_url:
                raise ValueError("Configure a speaker-reachable media URL first")
            if intent.kind != "alarm" and self.in_night(config):
                raise ValueError("Playback is blocked by the configured night rule")
            identifier = secrets.token_hex(16)
            now = time.time()
            payload = intent.model_dump()
            payload["revision"] = revision
            payload["secret_versions"] = {
                name: config.credential_versions.get(name, 0)
                for name in (config.llm.key_ref, config.speech.tts_key_ref)
            }
            if not intent.enqueue:
                if keep_queue:
                    self._interrupt_locked()
                else:
                    self._cancel_locked()
            self.state.db.execute(
                "INSERT INTO jobs(id,request_id,fingerprint,generation,source,kind,payload,status,created,updated) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    request_id,
                    fingerprint,
                    0,
                    source,
                    intent.kind,
                    json.dumps(payload, ensure_ascii=False),
                    "queued",
                    now,
                    now,
                ),
            )
            self.state.event("playback.queued", {"playback_id": identifier, "source": source})
            launch = not intent.enqueue or self._active_locked() is None
        if launch:
            self._launch(None if intent.enqueue else identifier)
        else:
            self._prefetch_next()
        self.changed.set()
        return identifier

    def _quiet_argument(self):
        return "night" if self.in_night(self.state.config()[1]) else "day"

    def _interrupt_locked(self):
        current = self.state.meta("current")
        generation = self._generation()
        self.state.db.execute(
            "UPDATE jobs SET status='cancelled',updated=? WHERE id=? AND status IN "
            "('queued','resolving','buffering','playing','unknown','stopping')",
            (time.time(), current),
        )
        self.state.set_meta("current", "")
        self.command("stop", self._quiet_argument(), generation)
        if self.end_timer:
            self.end_timer.cancel()
            self.end_timer = None
        if self.task and not self.task.done():
            self.task.cancel()
        if self.on_cancel and current:
            task = asyncio.create_task(self.on_cancel(current))
            self.background.add(task)
            task.add_done_callback(self.background.discard)
        return generation

    def _cancel_locked(self):
        generation = self._interrupt_locked()
        self.state.db.execute(
            "UPDATE jobs SET status='cancelled',updated=? WHERE status IN "
            "('queued','resolving','buffering','playing','unknown','stopping')",
            (time.time(),),
        )
        self.state.set_meta("cancel_epoch", int(self.state.meta("cancel_epoch", "0")) + 1)
        self.state.set_meta("suspended", "")
        self.state.set_meta("next", "")
        self._drop_warm()
        return generation

    def interrupt(self):
        with self.state.transaction():
            current = self._active_locked()
            if current is None:
                return False
            if self.state.config()[1].speech.resume_seconds > 0:
                self._suspend_locked(current)
            self._interrupt_locked()
            self.state.event("playback.interrupted", {"playback_id": current["id"]})
        self.changed.set()
        return True

    def _suspend_locked(self, job):
        if job["kind"] != "music":
            return
        payload = json.loads(job["payload"])
        start = float(payload.get("start") or 0)
        position = start + (time.time() - job["updated"] if job["status"] == "playing" else 0)
        duration = float((payload.get("media") or {}).get("duration") or 0)
        intent = None
        if not duration or position < start + duration - TAIL_SECONDS - 5:
            resumed = max(0.0, position - REWIND_SECONDS)
            intent = {"track": payload["track"], "start": round(resumed, 1) if resumed >= 5 else 0}
        # intent=None means the song had ended; resuming continues with the queue.
        self.state.set_meta(
            "suspended", json.dumps({"intent": intent, "at": time.time()}, ensure_ascii=False)
        )

    def _suspended_locked(self):
        saved = self.state.meta("suspended")
        if not saved:
            return None
        saved = json.loads(saved)
        if time.time() - saved["at"] > SUSPEND_TTL:
            self.state.set_meta("suspended", "")
            return None
        return saved

    def forget_suspended(self):
        with self.state.transaction():
            self.state.set_meta("suspended", "")

    def resume(self):
        with self.state.transaction():
            if self._suspended_locked() is None or self._active_locked() is not None:
                return False
        launched = self._launch()
        self.changed.set()
        return launched

    def _resume_suspended(self):
        with self.state.transaction():
            if self._active_locked() is not None or self._urgent_locked() is not None:
                return None
            saved = self._suspended_locked()
            if saved is None:
                return None
            self.state.set_meta("suspended", "")
        if saved["intent"] is None:
            return None
        try:
            self.submit(
                Intent.model_validate(saved["intent"]),
                secrets.token_hex(16),
                source="resume",
                keep_queue=True,
            )
        except (ValueError, RuntimeError):
            return None
        return True

    def _resume_if_idle(self):
        if self.listening_until > time.monotonic():
            return False
        return self.resume()

    def stop(self):
        with self.state.transaction():
            self._cancel_locked()
            self.state.event("playback.stopped", {"generation": int(self.state.meta("generation"))})
        self.changed.set()

    def _launch(self, identifier=None):
        if identifier is None:
            resumed = self._resume_suspended()
            if resumed is not None:
                return resumed
        with self.state.transaction():
            if identifier:
                row = self.state.db.execute(
                    "SELECT * FROM jobs WHERE id=? AND status='queued'", (identifier,)
                ).fetchone()
            elif self._active_locked() is None:
                row = self._next_locked()
            else:
                row = None
            if row is None:
                return False
            if self.state.meta("next") == row["id"]:
                self.state.set_meta("next", "")
            generation = self._generation()
            self.state.set_meta("current", row["id"])
            if row["kind"] != "tts":
                self.state.set_meta("last", row["id"])
            self.state.db.execute(
                "UPDATE jobs SET generation=?,status='resolving',updated=? WHERE id=?",
                (generation, time.time(), row["id"]),
            )
            self.command("stop", self._quiet_argument(), generation, row["id"])
        self.task = asyncio.create_task(
            self._resolve(row["id"], generation, json.loads(row["payload"]))
        )
        return True

    def _urgent_locked(self):
        return self.state.db.execute(
            "SELECT * FROM jobs WHERE status='queued' AND kind<>'music' ORDER BY "
            "CASE kind WHEN 'alarm' THEN 0 ELSE 1 END, created LIMIT 1"
        ).fetchone()

    def _next_locked(self):
        row = self._urgent_locked()
        if row is None and self.state.meta("next"):
            row = self.state.db.execute(
                "SELECT * FROM jobs WHERE id=? AND status='queued'", (self.state.meta("next"),)
            ).fetchone()
        if row is None:
            # The choice is remembered so a shuffled prefetch is the track that plays next.
            ordering = "RANDOM()" if self.state.config()[1].music.shuffle else "created"
            row = self.state.db.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY " + ordering + " LIMIT 1"
            ).fetchone()
            self.state.set_meta("next", row["id"] if row else "")
        return row

    def _prefetch_next(self):
        if self.prefetch is None:
            return
        with self.state.transaction():
            current = self._active_locked()
            # Warm only while something plays, so the current track keeps the CPU first.
            if (
                current is None
                or current["status"] != "playing"
                or not self.state.config()[1].music.cache_enabled
            ):
                return
            row = self._next_locked()
        if row is None or row["kind"] != "music" or row["id"] in self.warming:
            return
        task = asyncio.create_task(self._warm(row["id"], json.loads(row["payload"])))
        self.warming[row["id"]] = task
        task.add_done_callback(
            lambda done, key=row["id"]: self.warming.get(key) is done and self.warming.pop(key)
        )

    async def _warm(self, identifier, payload):
        try:
            config = self.state.config(revision=payload["revision"])[1]
            async with asyncio.timeout(260):
                await self.prefetch(identifier, payload, config)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def _drop_warm(self, identifiers=None):
        for key in [key for key in self.warming if identifiers is None or key in identifiers]:
            self.warming.pop(key).cancel()
            if self.on_cancel:
                task = asyncio.create_task(self.on_cancel(key))
                self.background.add(task)
                task.add_done_callback(self.background.discard)

    def owns(self, identifier, generation):
        return (
            self.state.meta("current") == identifier
            and int(self.state.meta("generation")) == generation
        )

    def _active_locked(self):
        row = self.state.db.execute(
            "SELECT id,kind,status,payload,updated FROM jobs WHERE id=?",
            (self.state.meta("current"),),
        ).fetchone()
        return row if row and row["status"] in ACTIVE else None

    def active_kind(self):
        with self.state.lock:
            row = self._active_locked()
        return row["kind"] if row else None

    async def _resolve(self, identifier, generation, payload):
        try:
            revision, config = self.state.config(revision=payload["revision"])
            async with asyncio.timeout(260):
                media = await self.prepare(identifier, payload, config)
            if not self.owns(identifier, generation):
                return
            with self.state.transaction():
                payload["media"] = media
                self.state.db.execute(
                    "UPDATE jobs SET payload=?,status='buffering',updated=? WHERE id=? AND generation=?",
                    (json.dumps(payload, ensure_ascii=False), time.time(), identifier, generation),
                )
                schedule = (
                    "%s,%s" % (config.music.night_start, config.music.night_end)
                    if config.music.night_enabled
                    else "off"
                )
                self.command("quiet_schedule", schedule, generation, identifier)
                if payload["kind"] == "alarm":
                    self.command("quiet_off", "", generation, identifier)
                self.command(
                    "play", media["url"], generation, identifier, config.device.command_ttl
                )
                self.state.event("playback.buffering", {"playback_id": identifier})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failed = False
            with self.state.transaction():
                if self.owns(identifier, generation):
                    self.state.db.execute(
                        "UPDATE jobs SET status='failed',error=?,updated=? WHERE id=?",
                        (type(error).__name__, time.time(), identifier),
                    )
                    self.state.event(
                        "playback.failed",
                        {"playback_id": identifier, "error": type(error).__name__},
                    )
                    self.state.set_meta("current", "")
                    failed = True
            if failed:
                self._proceed(payload["kind"], True)
        finally:
            self.changed.set()

    def poll(self, lease_seconds=5):
        with self.state.transaction():
            now = time.time()
            self.state.db.execute(
                "UPDATE commands SET status='expired' WHERE expires<? AND status IN ('queued','leased')",
                (now,),
            )
            row = self.state.db.execute(
                "SELECT * FROM commands WHERE status IN ('queued','leased') AND generation=? "
                "ORDER BY priority,created LIMIT 1",
                (int(self.state.meta("generation")),),
            ).fetchone()
            if not row or row["lease"] > now:
                return None
            self.state.db.execute(
                "UPDATE commands SET status='leased',lease=? WHERE id=?",
                (now + lease_seconds, row["id"]),
            )
            return dict(row)

    def report(self, command_id, generation, result, boot_id=""):
        if result not in ("accepted", "playing", "stopped", "unknown", "failed", "completed"):
            raise ValueError("Invalid device result")
        advance = False
        repeat = None
        started = ended = None
        with self.state.transaction():
            row = self.state.db.execute(
                "SELECT * FROM commands WHERE id=?", (command_id,)
            ).fetchone()
            if (
                not row
                or row["generation"] != generation
                or generation != int(self.state.meta("generation"))
                or row["status"] in ("expired", "cancelled")
                or (row["expires"] < time.time() and row["status"] != "done")
            ):
                return False
            if boot_id and self.state.meta("device_boot") not in ("", boot_id):
                self.state.set_meta("device_state", "unknown")
            if boot_id:
                self.state.set_meta("device_boot", boot_id)
            self.state.db.execute(
                "UPDATE commands SET status='done',result=? WHERE id=?", (result, command_id)
            )
            self.state.set_meta("device_at", time.time())
            self.state.set_meta("device_state", result)
            if row["action"] == "play" and self.owns(row["playback_id"], generation):
                job = self.state.db.execute(
                    "SELECT kind,payload,status,updated FROM jobs WHERE id=?",
                    (row["playback_id"],),
                ).fetchone()
                now = time.time()
                if result == "playing" and job["status"] in ("resolving", "buffering"):
                    self.state.db.execute(
                        "UPDATE jobs SET status='playing',updated=? WHERE id=?",
                        (now, row["playback_id"]),
                    )
                    started = json.loads(job["payload"])
                elif result in ("failed", "completed", "stopped") and job["status"] in ACTIVE:
                    payload = json.loads(job["payload"])
                    if result == "failed":
                        status = "failed"
                    elif self._completed_naturally(job, payload, now):
                        status = "completed"
                    else:
                        status = "cancelled"
                    repeat = self._end_locked(row["playback_id"], generation, job, status)
                    ended = job["kind"]
                    # A user pause, phone cast or native stop must not start the next track.
                    advance = status != "cancelled"
                self.state.event(
                    "device.result", {"playback_id": row["playback_id"], "result": result}
                )
        if started is not None:
            self._schedule_end(row["playback_id"], generation, started)
            self._prefetch_next()
        elif ended:
            self._proceed(ended, advance, repeat)
        self.changed.set()
        return True

    def _end_locked(self, identifier, generation, job, status):
        error = {"cancelled": "DeviceStoppedEarly", "failed": "DeviceFailed"}.get(status, "")
        self.state.db.execute(
            "UPDATE jobs SET status=?,error=?,updated=? WHERE id=?",
            (status, error, time.time(), identifier),
        )
        if job["kind"] == "alarm":
            self.command("quiet_on", self._quiet_argument(), generation)
        self.state.set_meta("current", "")
        if self.end_timer:
            self.end_timer.cancel()
            self.end_timer = None
        if status == "completed" and job["kind"] == "music":
            if self.state.config()[1].music.repeat != "off":
                return json.loads(job["payload"])
        return None

    def _proceed(self, kind, advance, repeat=None):
        if repeat:
            intent = Intent.model_validate(
                {key: value for key, value in repeat.items() if key in Intent.model_fields}
            )
            intent.enqueue = self.state.config()[1].music.repeat == "all"
            intent.start = 0
            try:
                self.submit(intent, secrets.token_hex(16), source="queue", keep_queue=True)
                return True
            except (ValueError, RuntimeError):
                pass
        if not advance:
            return False
        if kind == "tts":
            # A spoken reply only continues music that the wake word interrupted.
            return self._resume_if_idle()
        return self._launch()

    def _schedule_end(self, identifier, generation, payload):
        if self.end_timer:
            self.end_timer.cancel()
            self.end_timer = None
        media = payload.get("media") or {}
        duration = float(media.get("duration") or 0)
        if media.get("streaming") or duration <= TAIL_SECONDS:
            return
        delay = duration - TAIL_SECONDS + self.state.config()[1].music.advance_margin
        self.end_timer = asyncio.get_running_loop().call_later(
            delay, self._timed_end, identifier, generation
        )

    def _timed_end(self, identifier, generation):
        # The audible part has ended; switch inside the silent tail, before the
        # firmware's pre-end skip makes the native controller pause the player.
        self.end_timer = None
        if self.closed:
            return
        with self.state.transaction():
            if not self.owns(identifier, generation):
                return
            job = self.state.db.execute(
                "SELECT kind,payload,status FROM jobs WHERE id=?", (identifier,)
            ).fetchone()
            if job is None or job["status"] != "playing":
                return
            repeat = self._end_locked(identifier, generation, job, "completed")
            self.state.event("playback.completed", {"playback_id": identifier})
        if not self._proceed(job["kind"], True, repeat):
            with self.state.transaction():
                self.command("stop", self._quiet_argument(), int(self.state.meta("generation")))
        self.changed.set()

    @staticmethod
    def _completed_naturally(job, payload, now):
        media = payload.get("media") or {}
        if job["status"] != "playing" or media.get("streaming"):
            return True
        expected = float(media.get("duration") or 0) - PADDING_SECONDS - EARLY_STOP_MARGIN
        return now - job["updated"] >= expected

    @staticmethod
    def in_night(config):
        policy = config.music
        if not policy.night_enabled:
            return False
        hour = datetime.datetime.now(ZoneInfo(policy.timezone)).hour
        if policy.night_start < policy.night_end:
            return policy.night_start <= hour < policy.night_end
        return hour >= policy.night_start or hour < policy.night_end

    def next(self):
        with self.state.transaction():
            self.state.set_meta("suspended", "")
            self._interrupt_locked()
        self._launch()
        self.changed.set()

    def replay(self):
        with self.state.lock:
            row = self.state.db.execute(
                "SELECT payload FROM jobs WHERE id=?", (self.state.meta("last"),)
            ).fetchone()
        if not row:
            raise ValueError("No previous track")
        payload = json.loads(row["payload"])
        intent = Intent.model_validate(
            {key: value for key, value in payload.items() if key in Intent.model_fields}
        )
        intent.enqueue = False
        intent.start = 0
        self.forget_suspended()
        return self.submit(intent, secrets.token_hex(16), keep_queue=True)

    def previous(self):
        with self.state.lock:
            row = self.state.db.execute(
                "SELECT payload FROM jobs WHERE id<>? AND kind='music' AND generation>0 "
                "AND status IN ('completed','cancelled') ORDER BY updated DESC LIMIT 1",
                (self.state.meta("current"),),
            ).fetchone()
        if row is None:
            raise ValueError("No previous track")
        payload = json.loads(row["payload"])
        intent = Intent.model_validate(
            {key: value for key, value in payload.items() if key in Intent.model_fields}
        )
        intent.enqueue = False
        intent.start = 0
        self.forget_suspended()
        return self.submit(intent, secrets.token_hex(16), keep_queue=True)

    def remove(self, identifier):
        with self.state.transaction():
            self.state.db.execute(
                "UPDATE jobs SET status='cancelled',updated=? WHERE id=? AND status='queued'",
                (time.time(), identifier),
            )
        self._drop_warm({identifier})
        self.changed.set()

    def snapshot(self):
        with self.state.lock:
            current = self.state.db.execute(
                "SELECT * FROM jobs WHERE id=?", (self.state.meta("current"),)
            ).fetchone()
            queue = self.state.db.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 100"
            ).fetchall()
            last_seen = float(self.state.meta("device_at", "0"))
            revision, config = self.state.config()
            device_state = self.state.meta("device_state", "unknown")
            if time.time() - last_seen > config.device.state_ttl:
                device_state = "unknown"

            def public(row):
                if not row:
                    return None
                return {
                    "playback_id": row["id"],
                    "status": row["status"],
                    "source": row["source"],
                    "kind": row["kind"],
                    "intent": json.loads(row["payload"]).get("track"),
                    "error": row["error"],
                    "updated": row["updated"],
                }

            return {
                "current": public(current),
                "queue": [public(row) for row in queue],
                "device_state": device_state,
                "device_seen": last_seen,
                "media_fetched": json.loads(self.state.meta("media_fetched", "{}")),
                "generation": int(self.state.meta("generation")),
                "audible": "unknown",
                "capabilities": {"pause": False, "seek": False, "volume": False},
            }

    async def close(self):
        self.closed = True
        if self.end_timer:
            self.end_timer.cancel()
        pending = list(self.warming.values())
        if self.task and not self.task.done():
            pending.append(self.task)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if self.background:
            await asyncio.gather(*self.background, return_exceptions=True)
