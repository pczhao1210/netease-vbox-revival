import asyncio
import time

from .speech import tts_chunks


class TTSStream:
    def __init__(self, text, config, key, private_hosts=(), slots=None, source=None):
        self.text = text
        self.config = config
        self.key = key
        self.private_hosts = private_hosts
        self.slots = slots or asyncio.Semaphore(2)
        self.source = source
        self.buffer = bytearray()
        self.condition = asyncio.Condition()
        self.ready = asyncio.Event()
        self.done = False
        self.cancelled = False
        self.error = ""
        self.created = time.time()
        self.task = None

    async def start(self):
        if self.cancelled:
            raise ValueError("TTS stream was cancelled")
        self.task = asyncio.create_task(self.generate())
        try:
            await asyncio.wait_for(self.ready.wait(), 10)
        except BaseException:
            await self.cancel()
            raise
        if self.error or self.cancelled or not self.buffer:
            raise ValueError("TTS stream did not produce valid audio")

    async def generate(self):
        child = None
        feeder = None
        try:
            async with self.slots, asyncio.timeout(90):
                child = await asyncio.create_subprocess_exec(
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-max_alloc",
                    "67108864",
                    "-protocol_whitelist",
                    "pipe",
                    "-format_whitelist",
                    "mp3",
                    "-threads",
                    "2",
                    "-i",
                    "pipe:0",
                    "-vn",
                    "-af",
                    "volume=%sdB,adelay=500,apad=pad_dur=45" % self.config.gain_db,
                    "-ar",
                    "48000",
                    "-ac",
                    "1",
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    "128k",
                    "-threads",
                    "2",
                    "-write_xing",
                    "0",
                    "-flush_packets",
                    "1",
                    "-f",
                    "mp3",
                    "pipe:1",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )

                async def feed():
                    chunks = (
                        self.source()
                        if self.source
                        else tts_chunks(self.text, self.config, self.key, self.private_hosts)
                    )
                    try:
                        async for chunk in chunks:
                            if self.cancelled:
                                raise asyncio.CancelledError()
                            child.stdin.write(chunk)
                            await child.stdin.drain()
                    finally:
                        child.stdin.close()

                feeder = asyncio.create_task(feed())
                while chunk := await child.stdout.read(16384):
                    if self.cancelled:
                        raise asyncio.CancelledError()
                    async with self.condition:
                        if len(self.buffer) + len(chunk) > 16 * 1048576:
                            raise ValueError("TTS stream exceeded its buffer limit")
                        self.buffer.extend(chunk)
                        self.ready.set()
                        self.condition.notify_all()
                await feeder
                await child.wait()
                if child.returncode:
                    raise ValueError("Stream conversion failed")
        except asyncio.CancelledError:
            self.cancelled = True
        except Exception as error:
            self.error = type(error).__name__
        finally:
            if feeder:
                feeder.cancel()
                await asyncio.gather(feeder, return_exceptions=True)
            if child and child.returncode is None:
                child.kill()
                await child.wait()
            async with self.condition:
                self.done = True
                self.ready.set()
                self.condition.notify_all()

    async def content(self):
        offset = 0
        while True:
            async with self.condition:
                await self.condition.wait_for(
                    lambda start=offset: self.done or self.cancelled or len(self.buffer) > start
                )
                if self.cancelled:
                    return
                chunk = bytes(self.buffer[offset : offset + 65536])
                finished = self.done and offset + len(chunk) == len(self.buffer)
            if chunk:
                offset += len(chunk)
                yield chunk
            if finished:
                return

    async def cancel(self):
        self.cancelled = True
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        async with self.condition:
            self.condition.notify_all()
