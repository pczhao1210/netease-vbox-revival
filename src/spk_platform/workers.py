import asyncio
import base64
import hashlib
import io
import os
import re
import secrets
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import Field

from .assets import Assets, audio_info
from .auth import require_token, token_file
from .mcp_runtime import MCPGateway
from .services import check_ticket
from .settings import MCPServer, Model, Music, Speech
from .speech import synthesize, voices
from .streaming import TTSStream


def worker_app(name, keys, lifespan=None):
    app = FastAPI(
        title="spk-" + name, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )

    async def authorized(request: Request):
        require_token(request, token_file(keys, name))

    @app.exception_handler(RequestValidationError)
    async def invalid(request, error):
        return JSONResponse({"detail": "Invalid service request"}, status_code=422)

    @app.exception_handler(ValueError)
    async def failure(request, error):
        return JSONResponse({"detail": str(error)}, status_code=400)

    @app.get("/health")
    async def health():
        task = getattr(app.state, "background_task", None)
        if task is not None and task.done():
            return JSONResponse(
                {"ok": False, "service": name, "reason": "input service stopped"}, status_code=503
            )
        return {"ok": True, "service": name}

    return app, authorized


class MediaRequest(Model):
    playback_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    kind: str = "music"
    music: Music = Field(default_factory=Music)
    speech: Speech = Field(default_factory=Speech)
    private_hosts: list[str] = Field(default_factory=list)
    cache_key: str = Field(default="", max_length=1024)
    media_id: str = ""
    local_id: str = ""
    url: str = Field(default="", max_length=4096)
    text: str = Field(default="", max_length=4000)
    key: str = Field(default="", max_length=16384)
    start: float = Field(default=0, ge=0, le=21600)


def create_media_app(keys, root, local, notify=None):
    assets = Assets(root, local)
    temporary_assets = Assets(Path(root) / "tts", local)
    tasks = {}
    stream_jobs = {}
    leases = {}
    cancelled = set()

    @asynccontextmanager
    async def lifespan(app):
        async def sweep():
            while True:
                now = time.time()
                for identifier, lease in list(leases.items()):
                    media_id, expires, owner, ephemeral = lease
                    if expires < now:
                        leases.pop(identifier, None)
                        if any(
                            other[0] == media_id and other[2] is owner for other in leases.values()
                        ):
                            continue
                        owner.protected.discard(media_id)
                        if ephemeral:
                            owner.path(media_id).unlink(missing_ok=True)
                for path in temporary_assets.root.glob("*.mp3"):
                    if (
                        path.stat().st_mtime < now - 600
                        and path.stem not in temporary_assets.protected
                    ):
                        path.unlink(missing_ok=True)
                for identifier, stream in list(stream_jobs.items()):
                    if stream.done and stream.created < now - 300:
                        stream_jobs.pop(identifier, None)
                await asyncio.sleep(30)

        sweeper = asyncio.create_task(sweep())
        try:
            yield
        finally:
            sweeper.cancel()
            for task in list(tasks.values()):
                task.cancel()
            await asyncio.gather(sweeper, *tasks.values(), return_exceptions=True)
            await asyncio.gather(
                *(stream.cancel() for stream in stream_jobs.values()), return_exceptions=True
            )

    app, authorized = worker_app("media", keys, lifespan)
    app.state.assets = assets

    async def prepare(body):
        owner = assets
        if body.kind == "tts":
            owner = temporary_assets
            identifier = hashlib.sha256(body.playback_id.encode()).hexdigest()
            if body.speech.streaming:
                stream = TTSStream(
                    body.text, body.speech, body.key, body.private_hosts, owner.slots
                )
                stream_jobs[identifier] = stream
                await stream.start()
                if body.playback_id in cancelled:
                    await stream.cancel()
                    raise HTTPException(409, "Playback was cancelled")
                return {"media_id": identifier, "duration": 600, "streaming": True}
            async with owner.slots, owner.temporary() as source, owner.temporary() as converted:
                await synthesize(body.text, body.speech, body.key, source, body.private_hosts)
                await owner.normalize(source, converted, body.speech.gain_db)
                os.replace(converted, owner.path(identifier))
            result = {
                "media_id": identifier,
                **await asyncio.to_thread(audio_info, owner.path(identifier)),
            }
        elif body.media_id:
            path = owner.path(body.media_id)
            async with owner.hold(body.media_id):
                result = {"media_id": body.media_id, **await asyncio.to_thread(audio_info, path)}
        else:
            cache_key = body.cache_key if body.music.cache_enabled else secrets.token_hex(32)
            if body.local_id and body.music.cache_enabled:
                entry = next(
                    (entry for entry in owner.local() if entry["id"] == body.local_id), None
                )
                if entry:
                    # A replaced file with the same name must not reuse the old conversion.
                    cache_key += ":%s:%s" % (entry["size"], entry["mtime"])
            result = await owner.prepare(
                cache_key,
                body.music,
                url=body.url or None,
                local_id=body.local_id or None,
                private_hosts=body.private_hosts,
            )
        if body.playback_id in cancelled:
            raise HTTPException(409, "Playback was cancelled")
        ephemeral = body.kind == "tts" or not body.music.cache_enabled
        if body.start > 0 and body.kind != "tts":
            # Resume after a wake word: serve the remainder of the converted track.
            source = owner.path(result["media_id"])
            identifier = hashlib.sha256(body.playback_id.encode()).hexdigest()
            async with owner.hold(result["media_id"]), temporary_assets.temporary() as part:
                await temporary_assets.excerpt(source, body.start, part)
                os.replace(part, temporary_assets.path(identifier))
            if ephemeral and owner is assets:
                source.unlink(missing_ok=True)
            owner, ephemeral = temporary_assets, True
            result = {
                "media_id": identifier,
                **await asyncio.to_thread(audio_info, owner.path(identifier)),
            }
        owner.protected.add(result["media_id"])
        leases[body.playback_id] = (
            result["media_id"],
            time.time() + result["duration"] + 300,
            owner,
            ephemeral,
        )
        return result

    @app.post("/internal/prepare", dependencies=[Depends(authorized)])
    async def prepare_media(body: MediaRequest):
        if body.playback_id in cancelled:
            raise HTTPException(409, "Playback was cancelled")
        if body.playback_id in tasks:
            return await asyncio.shield(tasks[body.playback_id])
        task = asyncio.create_task(prepare(body))
        tasks[body.playback_id] = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            raise HTTPException(409, "Media generation cancelled") from None
        finally:
            tasks.pop(body.playback_id, None)

    @app.post("/internal/cancel", dependencies=[Depends(authorized)])
    async def cancel(body: dict):
        identifier = body.get("playback_id", "")
        cancelled.add(identifier)
        if len(cancelled) > 1024:
            cancelled.pop()
        task = tasks.get(identifier)
        stream = stream_jobs.get(hashlib.sha256(identifier.encode()).hexdigest())
        if stream:
            await stream.cancel()
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return {"ok": True}

    @app.get("/internal/library", dependencies=[Depends(authorized)])
    async def library():
        return {"local": assets.local(), "cache": assets.cached()}

    @app.post("/internal/clear", dependencies=[Depends(authorized)])
    async def clear(body: dict):
        return {"bytes": assets.trim(Music.model_validate(body.get("music", {})), clear=True)}

    @app.post("/internal/voices", dependencies=[Depends(authorized)])
    async def voice_list(body: dict):
        return await voices(
            Speech.model_validate(body["speech"]), body["key"], body.get("private_hosts", [])
        )

    @app.post("/internal/upload", dependencies=[Depends(authorized)])
    async def upload(request: Request):
        settings = Music()
        async with assets.slots, assets.temporary() as source, assets.temporary() as converted:
            received = 0
            with source.open("wb") as output:
                async for chunk in request.stream():
                    received += len(chunk)
                    assets.limits(settings, received)
                    output.write(chunk)
            await assets.normalize(source, converted)
            identifier = hashlib.sha256(converted.read_bytes()).hexdigest()
            os.replace(converted, assets.path(identifier))
            assets.pin(identifier)
        return {"provider": "cache", "id": identifier, "title": "Uploaded audio"}

    @app.api_route("/media/{filename}", methods=["GET", "HEAD"])
    async def media(
        filename: str, request: Request, playback_id: str = "", expires: int = 0, ticket: str = ""
    ):
        if not filename.endswith(".mp3"):
            raise HTTPException(404)
        identifier = filename[:-4]
        if not check_ticket(
            token_file(keys, "media-ticket"), identifier, playback_id, expires, ticket
        ):
            raise HTTPException(403, "Invalid media ticket")
        stream = stream_jobs.get(identifier)
        if stream:
            if stream.cancelled:
                raise HTTPException(410, "Stream was cancelled")
            if request.method == "HEAD":
                return Response(headers={"Content-Type": "audio/mpeg", "Connection": "close"})
            return StreamingResponse(
                stream.content(), media_type="audio/mpeg", headers={"Connection": "close"}
            )
        owner = assets if assets.path(identifier).is_file() else temporary_assets
        path = owner.path(identifier)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            raise HTTPException(404) from None
        # A file object closes the descriptor even if the response body never starts.
        handle = os.fdopen(descriptor, "rb", buffering=0)
        size = os.fstat(handle.fileno()).st_size
        start, end = 0, size - 1
        status = 200
        headers = {"Accept-Ranges": "bytes", "Content-Type": "audio/mpeg"}
        value = request.headers.get("Range", "")
        if value:
            match = re.fullmatch(r"bytes=(\d{0,16})-(\d{0,16})", value)
            if match and (match[1] or match[2]):
                if match[1]:
                    start = int(match[1])
                    end = min(int(match[2]) if match[2] else end, end)
                else:
                    start = max(0, size - int(match[2]))
                if start > end or start >= size:
                    handle.close()
                    return Response(
                        status_code=416,
                        headers={"Content-Range": "bytes */%s" % size, "Content-Length": "0"},
                    )
                status = 206
                headers["Content-Range"] = "bytes %s-%s/%s" % (start, end, size)
        headers["Content-Length"] = str(max(0, end - start + 1))
        if request.method == "HEAD":
            handle.close()
            return Response(status_code=status, headers=headers)
        if notify:
            await notify({"playback_id": playback_id, "media_id": identifier})

        async def content():
            with handle:
                offset = start
                while offset <= end:
                    chunk = await asyncio.to_thread(
                        os.pread, handle.fileno(), min(65536, end - offset + 1), offset
                    )
                    if not chunk:
                        break
                    offset += len(chunk)
                    yield chunk

        return StreamingResponse(content(), status_code=status, headers=headers)

    return app


class MCPRequest(Model):
    server: MCPServer
    key: str = Field(default="", max_length=16384)
    private_hosts: list[str] = Field(default_factory=list)
    tool: str = ""
    arguments: dict = Field(default_factory=dict)
    sensitive: bool = True


def create_mcp_app(keys):
    app, authorized = worker_app("mcp", keys)
    gateway = MCPGateway()
    active = set()

    @app.post("/internal/discover", dependencies=[Depends(authorized)])
    async def discover(body: MCPRequest):
        return await gateway.discover(body.server, body.key, body.private_hosts)

    @app.post("/internal/call", dependencies=[Depends(authorized)])
    async def call(body: MCPRequest):
        task = asyncio.current_task()
        active.add(task)
        try:
            return await gateway.call(
                body.server,
                body.tool,
                body.arguments,
                body.key,
                body.private_hosts,
                sensitive=body.sensitive,
            )
        finally:
            active.discard(task)

    @app.post("/internal/revoke", dependencies=[Depends(authorized)])
    async def revoke():
        for task in list(active):
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        gateway.cache.clear()
        return {"ok": True}

    return app


def create_voice_app(keys, control_url):
    import httpx

    from .speech import AzureASR
    from .voice import VoiceReceiver

    receiver = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal receiver
        async with httpx.AsyncClient(
            base_url=control_url,
            timeout=120,
            trust_env=False,
            headers={"Authorization": "Bearer " + token_file(keys, "voice")},
        ) as client:

            async def api(method, path, body=None):
                response = await client.request(method, path, json=body)
                response.raise_for_status()
                return response.json()

            receiver = VoiceReceiver(api)
            task = asyncio.create_task(receiver.run())
            app.state.background_task = task
            try:
                yield
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    app, authorized = worker_app("voice", keys, lifespan)

    @app.post("/internal/probe", dependencies=[Depends(authorized)])
    async def probe(body: dict):
        raw = base64.b64decode(body.get("audio", ""), validate=True)
        if not raw or len(raw) > 400000:
            raise ValueError("Provide a short authorized WAV sample")
        with wave.open(io.BytesIO(raw)) as sample:
            if (
                sample.getframerate() != 16000
                or sample.getnchannels() != 1
                or sample.getsampwidth() != 2
                or sample.getnframes() > 160000
            ):
                raise ValueError("ASR test requires <=10s of 16kHz mono PCM16 WAV")
            pcm = sample.readframes(sample.getnframes())
        loop = asyncio.get_running_loop()
        final = loop.create_future()

        def emit(kind, text, identifier):
            def finish():
                if not final.done() and kind in ("final", "cancelled", "no_match"):
                    final.set_result({"event": kind, "text": text, "result_id": identifier})

            loop.call_soon_threadsafe(finish)

        recognizer = AzureASR(Speech.model_validate(body["speech"]), body["key"], emit)
        try:
            await recognizer.start()
            recognizer.write(pcm)
            recognizer.end_input()
            result = await asyncio.wait_for(final, 15)
            if result["event"] != "final" or not result["text"]:
                raise ValueError("ASR did not return recognized speech")
            return result
        finally:
            await recognizer.close()

    return app
