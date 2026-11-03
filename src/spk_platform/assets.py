import asyncio
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import time
import weakref
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urljoin

from .network import remote_client


def audio_info(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            "mp3,wav,flac,ogg,mov,aac",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        timeout=15,
        check=True,
    )
    information = json.loads(result.stdout)
    duration = float(information.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or not 0 < duration <= 6 * 3600:
        raise ValueError("Invalid or overlong audio")
    if not any(stream.get("codec_type") == "audio" for stream in information.get("streams", [])):
        raise ValueError("No decodable audio stream")
    return {"duration": duration}


async def process(*arguments, timeout=120):
    child = await asyncio.create_subprocess_exec(
        *map(str, arguments), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        await asyncio.wait_for(child.wait(), timeout=timeout)
        if child.returncode:
            raise ValueError("Audio conversion failed")
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


class Assets:
    def __init__(self, root, local_root):
        self.root = Path(root)
        self.local_root = Path(local_root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.locks = weakref.WeakValueDictionary()
        self.slots = asyncio.Semaphore(2)
        self.protected = set()
        self.pending = Counter()
        self.pins_file = self.root / "pins.json"
        self.pins = set()
        if self.pins_file.is_file() and not self.pins_file.is_symlink():
            try:
                stored = json.loads(self.pins_file.read_text())
                self.pins = {value for value in stored if isinstance(value, str)}
            except (OSError, TypeError, ValueError):
                pass

    def pin(self, identifier):
        self.path(identifier)
        self.pins.add(identifier)
        temporary = self.root / "pins.tmp"
        temporary.write_text(json.dumps(sorted(self.pins)))
        os.replace(temporary, self.pins_file)

    @asynccontextmanager
    async def hold(self, identifier):
        self.pending[identifier] += 1
        try:
            yield
        finally:
            self.pending[identifier] -= 1
            if self.pending[identifier] <= 0:
                del self.pending[identifier]

    def path(self, identifier):
        if len(identifier) != 64 or any(
            character not in "0123456789abcdef" for character in identifier
        ):
            raise ValueError("Invalid media identifier")
        path = self.root / (identifier + ".mp3")
        if path.is_symlink():
            raise ValueError("Media symlinks are not allowed")
        return path

    @asynccontextmanager
    async def temporary(self):
        descriptor, name = tempfile.mkstemp(prefix="download-", suffix=".part", dir=self.root)
        os.close(descriptor)
        path = Path(name)
        try:
            yield path
        finally:
            path.unlink(missing_ok=True)

    def limits(self, settings, size=0):
        if size > settings.one_file_mb * 1048576:
            raise ValueError("Media exceeds single-file limit")
        if shutil.disk_usage(self.root).free < settings.disk_floor_mb * 1048576:
            raise ValueError("Insufficient free disk space")

    async def download(self, client, url, target, settings):
        for _redirect in range(6):
            async with client.stream("GET", url) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Redirect has no destination")
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                if "text/" in response.headers.get("content-type", "").lower():
                    raise ValueError("Provider returned a text error instead of audio")
                length = response.headers.get("content-length")
                if length:
                    self.limits(settings, int(length))
                received = 0
                with target.open("wb") as output:
                    async for chunk in response.aiter_bytes(65536):
                        received += len(chunk)
                        self.limits(settings, received)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if length and received != int(length):
                    raise ValueError("Truncated audio download")
                if not received:
                    raise ValueError("Empty audio download")
                return
        raise ValueError("Too many media redirects")

    async def normalize(self, source, destination, gain_db=0, size_limit=40 * 1048576):
        original = await asyncio.to_thread(audio_info, source)
        await process(
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-max_alloc",
            "67108864",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            "mp3,wav,flac,ogg,mov,aac",
            "-threads",
            "2",
            "-i",
            source,
            "-vn",
            "-af",
            "volume=%sdB,adelay=500,apad=pad_dur=45" % gain_db,
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
            "-fs",
            size_limit,
            "-write_xing",
            "0",
            "-f",
            "mp3",
            destination,
        )
        converted = await asyncio.to_thread(audio_info, destination)
        if converted["duration"] < original["duration"] + 44:
            raise ValueError("Audio conversion was truncated before the protection tail")

    async def excerpt(self, source, start, destination):
        await process(
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-protocol_whitelist",
            "file,pipe",
            "-format_whitelist",
            "mp3",
            "-ss",
            "%.3f" % start,
            "-i",
            source,
            "-map",
            "0:a:0",
            "-c:a",
            "copy",
            "-write_xing",
            "0",
            "-f",
            "mp3",
            destination,
            timeout=60,
        )
        if (await asyncio.to_thread(audio_info, destination))["duration"] < 44:
            raise ValueError("Resume point is past the end of the track")

    async def prepare(self, key, settings, url=None, local_id=None, private_hosts=(), client=None):
        identifier = hashlib.sha256(key.encode()).hexdigest()
        lock = self.locks.setdefault(identifier, asyncio.Lock())
        async with self.hold(identifier), lock, self.slots:
            target = self.path(identifier)
            if target.is_file():
                return {"media_id": identifier, **await asyncio.to_thread(audio_info, target)}
            self.limits(settings)
            async with self.temporary() as source, self.temporary() as converted:
                if local_id:
                    entry = next((entry for entry in self.local() if entry["id"] == local_id), None)
                    if entry is None:
                        raise ValueError("Local audio not found")
                    descriptor = os.open(
                        self.local_root / entry["name"], os.O_RDONLY | os.O_NOFOLLOW
                    )
                    with os.fdopen(descriptor, "rb") as original, source.open("wb") as output:
                        while chunk := original.read(65536):
                            self.limits(settings, output.tell() + len(chunk))
                            output.write(chunk)
                elif url:
                    if client is not None:
                        await self.download(client, url, source, settings)
                    else:
                        async with remote_client(
                            private_hosts, timeout=60, response_limit=settings.one_file_mb * 1048576
                        ) as upstream:
                            async with asyncio.timeout(240):
                                await self.download(upstream, url, source, settings)
                else:
                    raise ValueError("No media source")
                await self.normalize(source, converted, size_limit=settings.one_file_mb * 1048576)
                self.limits(settings, converted.stat().st_size)
                os.replace(converted, target)
            self.trim(settings, {identifier})
            return {"media_id": identifier, **await asyncio.to_thread(audio_info, target)}

    def local(self):
        if not self.local_root.is_dir():
            return []
        output = []
        for path in sorted(self.local_root.iterdir()):
            if len(output) >= 1000:
                break
            if (
                path.is_file()
                and not path.is_symlink()
                and path.suffix.lower() in (".mp3", ".wav", ".m4a", ".flac", ".ogg")
            ):
                status = path.stat()
                output.append(
                    {
                        "id": hashlib.sha256(path.name.encode()).hexdigest(),
                        "name": path.name,
                        "title": path.stem,
                        "size": status.st_size,
                        "mtime": status.st_mtime_ns,
                        "provider": "local",
                    }
                )
        return output

    def cached(self):
        return [
            {
                "media_id": path.stem,
                "title": path.stem[:12],
                "size": path.stat().st_size,
                "provider": "cache",
                "id": path.stem,
            }
            for path in self.root.glob("*.mp3")
            if not path.is_symlink()
        ]

    def trim(self, settings, protect=(), clear=False):
        # Uploaded music is pinned: it is neither evicted nor counted against the cache.
        protected = self.protected | set(self.pending) | set(protect)
        paths = sorted(
            (
                path
                for path in self.root.glob("*.mp3")
                if not path.is_symlink() and path.stem not in self.pins
            ),
            key=lambda path: path.stat().st_mtime,
        )
        total = sum(path.stat().st_size for path in paths)
        for path in paths:
            if path.stem in protected:
                continue
            if clear or total > settings.cache_mb * 1048576:
                total -= path.stat().st_size
                path.unlink(missing_ok=True)
        for path in self.root.glob("*.part"):
            if path.stat().st_mtime < time.time() - 3600:
                path.unlink(missing_ok=True)
        return total
