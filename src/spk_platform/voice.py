import asyncio
import ipaddress
import json
import os
import struct
import time
from collections import deque
from pathlib import Path

from .speech import AzureASR, RecognitionEpoch

HEADER = struct.Struct("<8I")
MAGIC = 0x5041544D


def pcm16(samples):
    import numpy as np

    samples = np.nan_to_num(samples, nan=0.0, posinf=0.0, neginf=0.0)
    return np.clip(samples * 32768, -32768, 32767).astype("<i2").tobytes()


class Decoder:
    def __init__(self, gain=6):
        self.gain = gain
        self.resampler = None
        self.rate = None

    def decode(self, packet):
        import numpy as np

        if len(packet) <= HEADER.size:
            raise ValueError("Truncated microphone packet")
        magic, version, sequence, frames, rate, channels, format_id, flags = HEADER.unpack_from(
            packet
        )
        if (
            magic != MAGIC
            or version != 1
            or rate not in (8000, 16000, 24000, 32000, 44100, 48000, 96000)
            or not 1 <= channels <= 4
        ):
            raise ValueError("Unsupported microphone format")
        payload = packet[HEADER.size :]
        if format_id == 2:
            width = 2
            data = np.frombuffer(payload, "<i2").astype("float32") / 32768
        elif format_id in (6, 10):
            width = 4
            data = (np.frombuffer(payload, "<i4") >> 16).astype("float32") / 32768
        elif format_id == 14:
            width = 4
            data = np.frombuffer(payload, "<f4").copy()
        else:
            raise ValueError("Unsupported microphone sample type")
        if len(payload) != frames * channels * width or frames > 8192:
            raise ValueError("Microphone frame count mismatch")
        data = data.reshape(-1, channels)[:, 0]
        if rate != 16000:
            import soxr

            if self.rate != rate:
                self.resampler = soxr.ResampleStream(rate, 16000, 1, dtype="float32")
                self.rate = rate
            data = self.resampler.resample_chunk(data)
        return pcm16(np.clip(data * self.gain, -1, 1))


class KeywordWake:
    def __init__(self, root):
        import sherpa_onnx

        root = Path(root)
        files = json.loads((root / "model.json").read_text())
        paths = {}
        for name in ("tokens", "encoder", "decoder", "joiner", "keywords_file"):
            path = root / files[name]
            if (
                not path.resolve().is_relative_to(root.resolve())
                or path.is_symlink()
                or not path.is_file()
            ):
                raise ValueError("KWS assets must be regular files inside the model directory")
            paths[name] = str(path)
        self.spotter = sherpa_onnx.KeywordSpotter(
            **paths, num_threads=2, sample_rate=16000, feature_dim=80, provider="cpu"
        )
        self.stream = self.spotter.create_stream()

    def accept(self, pcm):
        import numpy as np

        self.stream.accept_waveform(16000, np.frombuffer(pcm, "<i2").astype("float32") / 32768)
        while self.spotter.is_ready(self.stream):
            self.spotter.decode_stream(self.stream)
        keyword = self.spotter.get_result(self.stream)
        if keyword:
            self.spotter.reset_stream(self.stream)
            return True
        return False


class VoiceReceiver:
    def __init__(self, api):
        import webrtcvad

        self.api = api
        self.decoder = Decoder()
        self.vad = webrtcvad.Vad(2)
        self.epoch = RecognitionEpoch()
        self.pre = deque(maxlen=20)
        self.recognizer = None
        self.session = None
        self.queue = asyncio.Queue(maxsize=200)
        self.last_packet = 0
        self.errors = 0
        self.kws = None
        self.status = "native-wake"
        self.task = None
        self.wake_lock = asyncio.Lock()
        self.wakes = set()
        self.input_closed = False
        self.last_speech = 0
        self.speech_started = False
        root = Path(os.getenv("SPK_KWS_DIR", "/models/kws"))
        if (root / "model.json").is_file():
            self.kws = KeywordWake(root)
            self.status = "local-kws+native-wake"

    def allowed(self, address):
        if self.session is None:
            return False
        config = self.session["config"]["device"]
        ip = ipaddress.ip_address(address)
        if config["ip"] and str(ip) != config["ip"]:
            return False
        return any(ip in ipaddress.ip_network(network) for network in config["allowed_cidrs"])

    async def refresh(self):
        self.session = await self.api("GET", "/internal/voice/config")
        if self.recognizer is None:
            speech = self.session["config"]["speech"]
            self.decoder.gain = speech["mic_gain"]
            self.vad.set_mode(speech["vad_mode"])

    def receive(self, data, address):
        if not self.allowed(address):
            return
        self.last_packet = time.time()
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(data)

    async def wake(self):
        # Wakes can arrive over TCP, UDP and local KWS at once; keep one listener.
        async with self.wake_lock:
            if self.task and not self.task.done():
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            try:
                await self.api("POST", "/internal/voice/interrupt", {})
                await self.refresh()
            except Exception:
                self.errors += 1
                return
            self.task = asyncio.create_task(self.listen())

    def schedule_wake(self):
        task = asyncio.create_task(self.wake())
        self.wakes.add(task)
        task.add_done_callback(self.wakes.discard)

    async def heartbeat(self):
        try:
            await self.refresh()
            await self.api(
                "POST",
                "/internal/voice/status",
                {"last_packet": self.last_packet, "errors": self.errors, "wake": self.status},
            )
        except Exception:
            self.errors += 1

    async def listen(self):
        from .settings import Settings

        first_turn = True
        try:
            while True:
                snapshot = self.session
                config = Settings.model_validate(snapshot["config"])
                async with asyncio.timeout(config.speech.session_seconds):
                    recognized = await self.recognize_turn(
                        snapshot, config, first_turn, config.speech.resume_seconds
                    )
                if recognized is None:
                    break
                reply = await self.api("POST", "/internal/voice/text", recognized)
                self.pre.clear()
                first_turn = False
                if not reply.get("listen", True):
                    break
        except Exception:
            self.errors += 1
        try:
            await self.api("POST", "/internal/voice/done", {})
        except Exception:
            self.errors += 1

    async def recognize_turn(self, snapshot, config, include_pre=True, quiet=0):
        loop = asyncio.get_running_loop()
        generation = self.epoch.begin()
        events = asyncio.Queue(maxsize=100)

        def emit(kind, text, identifier):
            def put():
                if not events.full():
                    events.put_nowait((kind, text, identifier))

            loop.call_soon_threadsafe(put)

        recognizer = AzureASR(config.speech, snapshot["asr_key"], emit)
        self.recognizer = recognizer
        self.input_closed = False
        self.speech_started = False
        self.vad_threshold = config.speech.vad_threshold
        try:
            await recognizer.start()
            if include_pre:
                for pcm in self.pre:
                    recognizer.write(pcm)
            deadline = loop.time() + quiet if quiet else None
            heard = False
            while True:
                timeout = None
                if deadline is not None and not heard:
                    timeout = deadline - loop.time()
                    if timeout <= 0:
                        return None
                try:
                    kind, text, identifier = await asyncio.wait_for(events.get(), timeout)
                except TimeoutError:
                    continue
                heard = True
                if kind == "final" and text.strip() and self.epoch.accept(generation, identifier):
                    return {
                        "text": text,
                        "request_id": "%s:%s" % (generation, identifier),
                        "revision": snapshot["revision"],
                    }
                if kind in ("cancelled", "no_match"):
                    return None
        finally:
            self.epoch.cancel()
            self.recognizer = None
            await recognizer.close()

    async def run(self):
        self.task = None
        delay = 1
        while True:
            try:
                await self.refresh()
                break
            except Exception:
                self.errors += 1
                await asyncio.sleep(delay)
                delay = min(30, delay * 2)
        loop = asyncio.get_running_loop()
        receiver = self

        class Microphone(asyncio.DatagramProtocol):
            def datagram_received(self, data, address):
                receiver.receive(data, address[0])

        class Wake(asyncio.DatagramProtocol):
            def datagram_received(self, data, address):
                if receiver.allowed(address[0]) and data.strip() == b"WAKE":
                    receiver.schedule_wake()

        async def tcp_wake(reader, writer):
            address = writer.get_extra_info("peername")
            try:
                if address and self.allowed(address[0]):
                    data = await asyncio.wait_for(reader.read(64), 1)
                    if data.strip().startswith(b"WAKE"):
                        await self.wake()
            finally:
                writer.close()
                await writer.wait_closed()

        microphone, protocol = await loop.create_datagram_endpoint(
            Microphone, local_addr=("0.0.0.0", 9998)
        )
        wake, protocol = await loop.create_datagram_endpoint(Wake, local_addr=("0.0.0.0", 9997))
        server = await asyncio.start_server(tcp_wake, "0.0.0.0", 9997)
        pending = bytearray()
        refreshed = time.monotonic()
        try:
            while True:
                if time.monotonic() - refreshed > 5:
                    await self.heartbeat()
                    refreshed = time.monotonic()
                try:
                    data = await asyncio.wait_for(self.queue.get(), 5)
                except TimeoutError:
                    await self.heartbeat()
                    refreshed = time.monotonic()
                    continue
                try:
                    pcm = self.decoder.decode(data)
                except (ValueError, struct.error):
                    self.errors += 1
                    continue
                self.pre.append(pcm)
                if self.recognizer and not self.input_closed:
                    self.recognizer.write(pcm)
                elif self.kws and self.kws.accept(pcm):
                    await self.wake()
                pending.extend(pcm)
                while len(pending) >= 960:
                    frame = bytes(pending[:960])
                    del pending[:960]
                    import numpy as np

                    samples = np.frombuffer(frame, "<i2").astype("float32") / 32768
                    rms = float(np.sqrt(np.mean(samples * samples)))
                    if self.vad.is_speech(frame, 16000) and rms >= getattr(
                        self, "vad_threshold", 0.015
                    ):
                        self.speech_started = True
                        self.last_speech = time.monotonic()
                    elif (
                        self.recognizer
                        and self.speech_started
                        and not self.input_closed
                        and time.monotonic() - self.last_speech > 0.8
                    ):
                        self.input_closed = True
                        self.recognizer.end_input()
        finally:
            microphone.close()
            wake.close()
            server.close()
            await server.wait_closed()
            for task in (self.task, *self.wakes):
                if task:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (self.task, *self.wakes) if task), return_exceptions=True
            )
