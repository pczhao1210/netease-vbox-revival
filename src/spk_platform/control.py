import asyncio
import base64
import datetime
import ipaddress
import json
import logging
import os
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, unquote
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from .auth import AdminAuth, initialize_tokens, require_token, token_file
from .brain import Brain
from .display import display_router, render_html
from .llm import PROBE_TOOL, LLMClient
from .playback import Alarm, Intent, Playback, Track
from .resolver import Collection
from .services import Services
from .settings import Model, Settings
from .state import Conflict, State

ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).with_name("static")
LOGGER = logging.getLogger(__name__)


class SaveRequest(Model):
    expected_revision: int
    config: Settings


class ApplyRequest(Model):
    revision: int = Field(ge=1)


class RestoreRequest(Model):
    schema_version: Literal[1] = 1
    expected_revision: int
    config: Settings
    alarms: list[Alarm] = Field(default_factory=list, max_length=64)


class PlayRequest(Model):
    request_id: str = Field(min_length=1, max_length=160)
    intent: Intent


class PlayCollection(Collection):
    request_id: str = Field(min_length=1, max_length=160)
    enqueue: bool = False


class PlayList(Model):
    request_id: str = Field(min_length=1, max_length=160)
    tracks: list[Track] = Field(min_length=1, max_length=50)
    enqueue: bool = False
    expected_generation: int = Field(ge=0)


def create_control_app(directory=None, keys=None, origin=None, services=None):
    directory = Path(directory or os.getenv("SPK_STATE_DIR", "data/control"))
    keys = Path(keys or os.getenv("SPK_KEY_DIR", "secrets"))
    initialize_tokens(keys)
    state = State(directory, keys / "master.key")
    auth = AdminAuth(state, origin or os.getenv("SPK_PUBLIC_URL", "http://127.0.0.1:8080"))
    services = services or Services(keys)

    async def prepare(identifier, payload, config):
        result = await services.prepare(identifier, payload, config, state)
        if payload.get("track"):
            state.db.execute(
                "INSERT INTO media_catalog VALUES (?,?) ON CONFLICT(media_id) DO UPDATE SET document=excluded.document",
                (result["media_id"], json.dumps(payload["track"], ensure_ascii=False)),
            )
        return result

    player = Playback(state, prepare, services.cancel, prefetch=prepare)
    brain = Brain(state, services, player)
    probes = asyncio.Semaphore(2)
    streams = asyncio.Semaphore(20)
    device_hold = min(4.0, max(0.0, float(os.getenv("SPK_DEVICE_HOLD", "3"))))

    def fire_alarms():
        revision, config = state.config()
        now = datetime.datetime.now(ZoneInfo(config.music.timezone))
        with state.lock:
            rows = state.db.execute("SELECT * FROM alarms").fetchall()
        for row in rows:
            alarm = Alarm.model_validate_json(row["document"])
            day = now.date().isoformat()
            if (
                alarm.enabled
                and (alarm.hour, alarm.minute) == (now.hour, now.minute)
                and row["fired"] != day
            ):
                with state.transaction():
                    state.db.execute("UPDATE alarms SET fired=? WHERE id=?", (day, alarm.id))
                try:
                    player.submit(
                        Intent(track=alarm.track, kind="alarm"),
                        "alarm:%s:%s" % (alarm.id, day),
                        "alarm",
                    )
                except (ValueError, RuntimeError) as error:
                    state.event("alarm.failed", {"id": alarm.id, "error": type(error).__name__})

    async def alarm_loop():
        while True:
            try:
                fire_alarms()
            except Exception:
                LOGGER.exception("Alarm check failed")
            await asyncio.sleep(10)

    @asynccontextmanager
    async def lifespan(app):
        player.recover()
        alarms = asyncio.create_task(alarm_loop())
        try:
            yield
        finally:
            alarms.cancel()
            await asyncio.gather(alarms, return_exceptions=True)
            await brain.cancel()
            await player.close()
            await services.close()
            state.close()

    app = FastAPI(
        title="SPK Control", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )
    app.state.store = state
    app.state.player = player
    app.state.auth = auth

    @app.middleware("http")
    async def security(request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            try:
                length = int(request.headers.get("Content-Length", "-1"))
                limit = 40 * 1048576 if request.url.path == "/api/music/upload" else 131072
                if request.url.path == "/api/probe/asr":
                    limit = 600000
                if length < 0 or length > limit:
                    return JSONResponse(
                        {"detail": "Invalid or oversized Content-Length"}, status_code=413
                    )
                if request.url.path.startswith("/api/"):
                    auth.write(
                        request, authenticated=request.url.path not in ("/api/login", "/api/setup")
                    )
                    auth.rates.check(
                        "write:" + (request.client.host if request.client else ""), 120
                    )
            except (HTTPException, ValueError) as error:
                return JSONResponse(
                    {"detail": getattr(error, "detail", "Invalid request")},
                    status_code=getattr(error, "status_code", 400),
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/display"):
            response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(request, error):
        fields = [".".join(map(str, entry["loc"])) for entry in error.errors()]
        return JSONResponse({"detail": "Invalid settings: " + ", ".join(fields)}, status_code=422)

    @app.exception_handler(ValueError)
    async def bad_value(request, error):
        return JSONResponse(
            {"detail": str(error)}, status_code=409 if isinstance(error, Conflict) else 400
        )

    @app.exception_handler(httpx.HTTPError)
    async def remote_error(request, error):
        if (
            request.url.path.startswith("/api/music/")
            and isinstance(error, httpx.HTTPStatusError)
            and error.response.status_code in (400, 403, 409, 422, 424, 429)
        ):
            try:
                detail = error.response.json().get("detail", "音乐账号请求未完成")
            except ValueError:
                detail = "音乐账号请求未完成"
            if not isinstance(detail, str):
                detail = "音乐账号请求未完成"
            return JSONResponse({"detail": detail[:300]}, status_code=error.response.status_code)
        return JSONResponse(
            {"detail": "Dependent service is unavailable", "category": type(error).__name__},
            status_code=503,
        )

    async def administrator(request: Request):
        return auth.session(request)

    def service_auth(request, name):
        require_token(request, token_file(keys, name))

    def device_auth(request):
        require_token(request, token_file(keys, "device"))
        config = state.config()[1].device
        address = request.client.host if request.client else ""
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            raise HTTPException(403) from None
        if config.ip and str(ip) != config.ip:
            raise HTTPException(403)
        if not any(ip in ipaddress.ip_network(network) for network in config.allowed_cidrs):
            raise HTTPException(403, "Device source address is not allowed")

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return render_html("index.html", theme=state.config()[1].appearance.theme)

    @app.get("/health")
    async def health():
        return {"ok": True, "service": "control", "configured": auth.configured}

    @app.get("/api/session")
    async def session(request: Request):
        auth.rates.check("session:" + (request.client.host if request.client else ""), 60)
        response = JSONResponse({})
        try:
            record = auth.session(request, authenticated=False)
        except HTTPException:
            record = auth.issue(response)
        response.body = json.dumps(
            {
                "configured": auth.configured,
                "authenticated": record["authenticated"],
                "csrf": record["csrf"],
                "local_setup": request.client.host in ("127.0.0.1", "::1", "testclient"),
            }
        ).encode()
        response.headers["Content-Length"] = str(len(response.body))
        return response

    @app.post("/api/setup")
    async def setup(request: Request):
        body = await request.json()
        auth.rates.check("login:" + request.client.host, 10)
        auth.setup(
            request,
            body.get("password", ""),
            body.get("bootstrap", ""),
            token_file(keys, "bootstrap"),
        )
        response = JSONResponse({"ok": True})
        record = auth.issue(response, True)
        response.body = json.dumps({"ok": True, "csrf": record["csrf"]}).encode()
        response.headers["Content-Length"] = str(len(response.body))
        return response

    @app.post("/api/login")
    async def login(request: Request):
        body = await request.json()
        auth.rates.check("login:" + request.client.host, 10)
        auth.login(body.get("password", ""))
        response = JSONResponse({"ok": True})
        record = auth.issue(response, True)
        response.body = json.dumps({"ok": True, "csrf": record["csrf"]}).encode()
        response.headers["Content-Length"] = str(len(response.body))
        return response

    @app.post("/api/logout")
    async def logout(request: Request):
        auth.logout(request)
        response = JSONResponse({"ok": True})
        response.delete_cookie("spk_session")
        return response

    @app.get("/api/config", dependencies=[Depends(administrator)])
    async def configuration():
        revision, config = state.config(applied=False)
        return {
            "saved_revision": revision,
            "applied_revision": int(state.meta("applied")),
            "config": config.model_dump(),
            "secrets": state.secret_metadata(),
            "environment_locked": ["bind ports", "data volumes", "service URLs", "public URL"],
        }

    @app.post("/api/config", dependencies=[Depends(administrator)])
    async def save_configuration(body: SaveRequest):
        return {"revision": state.save(body.config, body.expected_revision)}

    @app.post("/api/config/apply", dependencies=[Depends(administrator)])
    async def apply_configuration(body: ApplyRequest):
        revision = body.revision
        state.config(revision=revision)
        await brain.cancel()
        brain.histories.clear()
        state.apply(revision)
        return {"applied_revision": revision}

    @app.post("/api/secrets/{name}", dependencies=[Depends(administrator)])
    async def update_secret(name: str, request: Request):
        body = await request.json()
        state.put_secret(name, body.get("value", ""), clear=body.get("clear") is True)
        return state.secret_metadata().get(name, {"configured": False})

    @app.get("/api/playback", dependencies=[Depends(administrator)])
    async def playback():
        return player.snapshot()

    @app.post("/api/playback", dependencies=[Depends(administrator)])
    async def submit_playback(body: PlayRequest):
        return {"playback_id": player.submit(body.intent, body.request_id), "status": "preparing"}

    @app.post("/api/playback/list", dependencies=[Depends(administrator)])
    async def submit_list(body: PlayList):
        if body.expected_generation != int(state.meta("generation")):
            raise Conflict("播放状态已变化，请重新提交列表")
        identifiers = [
            player.submit(
                Intent(track=track, enqueue=body.enqueue or index > 0),
                body.request_id + ":" + str(index),
                source="ui",
            )
            for index, track in enumerate(body.tracks)
        ]
        return {"playback_ids": identifiers, "generation": int(state.meta("generation"))}

    @app.post("/api/playback/{action}", dependencies=[Depends(administrator)])
    async def playback_action(action: str, request: Request):
        if action == "stop":
            player.stop()
            await brain.cancel()
        elif action == "next":
            player.next()
        elif action == "replay":
            return {"playback_id": player.replay(), "status": "preparing"}
        elif action == "previous":
            return {"playback_id": player.previous(), "status": "preparing"}
        elif action == "remove":
            player.remove((await request.json())["playback_id"])
        else:
            raise HTTPException(404)
        return {"ok": True, "status": "requested"}

    @app.get("/api/music/library", dependencies=[Depends(administrator)])
    async def library():
        result = await services.request("media", "/internal/library", method="GET")
        for track in result.get("cache", []):
            row = state.db.execute(
                "SELECT document FROM media_catalog WHERE media_id=?", (track["id"],)
            ).fetchone()
            if row:
                metadata = json.loads(row["document"])
                track.update({key: metadata.get(key, "") for key in ("title", "artist")})
        return result

    @app.post("/api/music/search", dependencies=[Depends(administrator)])
    async def search(request: Request):
        query = str((await request.json()).get("query", "")).strip()
        if not 1 <= len(query) <= 200:
            raise ValueError("Search query must contain 1 to 200 characters")
        return await services.request("resolver", "/search", {"query": query}, timeout=45)

    @app.get("/api/music/account", dependencies=[Depends(administrator)])
    async def account():
        return await services.request("resolver", "/account", method="GET")

    @app.post("/api/music/playlist", dependencies=[Depends(administrator)])
    async def playlist(request: Request):
        return await services.request("resolver", "/playlists", await request.json(), timeout=45)

    @app.post("/api/music/account/{action}", dependencies=[Depends(administrator)])
    async def account_action(action: str, request: Request):
        if action not in ("login", "poll", "logout", "credentials", "check"):
            raise HTTPException(404)
        return await services.request("resolver", "/account/" + action, await request.json())

    @app.post("/api/music/capabilities", dependencies=[Depends(administrator)])
    async def music_capabilities():
        return await services.request("resolver", "/capabilities", {})

    @app.post("/api/music/collections", dependencies=[Depends(administrator)])
    async def music_collection(body: Collection):
        return await services.request("resolver", "/collections", body.model_dump(), timeout=65)

    @app.post("/api/music/collections/play", dependencies=[Depends(administrator)])
    async def play_collection(body: PlayCollection):
        if body.kind == "favorites":
            raise HTTPException(400, "请先选择一个收藏歌单")
        epoch = state.meta("cancel_epoch", "0")
        result = await services.request(
            "resolver", "/collections", {"kind": body.kind, "limit": body.limit}, timeout=65
        )
        if epoch != state.meta("cancel_epoch", "0"):
            raise Conflict("取回歌单期间播放被停止或切换，未开始播放")
        tracks = [track for track in result.get("tracks", []) if track.get("playable") is not False]
        identifiers = []
        from .playback import Track

        for index, track in enumerate(tracks):
            intent = Intent(
                track=Track.model_validate(
                    {key: value for key, value in track.items() if key in Track.model_fields}
                ),
                enqueue=body.enqueue or index > 0,
            )
            identifiers.append(
                player.submit(intent, body.request_id + ":" + str(index), source="collection")
            )
        return {"status": "queued" if identifiers else "empty", "playback_ids": identifiers}

    @app.post("/api/music/clear", dependencies=[Depends(administrator)])
    async def clear_cache():
        return await services.request(
            "media", "/internal/clear", {"music": state.config()[1].music.model_dump()}
        )

    @app.post("/api/music/upload", dependencies=[Depends(administrator)])
    async def upload_music(request: Request):
        response = await services.http.post(
            services.urls["media"] + "/internal/upload",
            content=request.stream(),
            headers={"Authorization": "Bearer " + token_file(keys, "media")},
            timeout=150,
        )
        response.raise_for_status()
        result = response.json()
        name = unquote(request.headers.get("X-Filename", "Uploaded audio"))
        result["title"] = Path(name.replace("\\", "/")).stem[:200]
        state.db.execute(
            "INSERT INTO media_catalog VALUES (?,?) ON CONFLICT(media_id) DO UPDATE SET document=excluded.document",
            (result["id"], json.dumps(result, ensure_ascii=False)),
        )
        return result

    @app.get("/api/alarms", dependencies=[Depends(administrator)])
    async def alarms():
        with state.lock:
            return [
                json.loads(row["document"])
                for row in state.db.execute("SELECT document FROM alarms")
            ]

    @app.post("/api/alarms", dependencies=[Depends(administrator)])
    async def set_alarm(alarm: Alarm):
        with state.transaction():
            state.db.execute(
                "INSERT INTO alarms(id,document) VALUES (?,?) "
                "ON CONFLICT(id) DO UPDATE SET document=excluded.document",
                (alarm.id, alarm.model_dump_json()),
            )
        return alarm.model_dump()

    @app.delete("/api/alarms/{identifier}", dependencies=[Depends(administrator)])
    async def delete_alarm(identifier: str):
        state.db.execute("DELETE FROM alarms WHERE id=?", (identifier,))
        return {"ok": True}

    @app.post("/api/ask", dependencies=[Depends(administrator)])
    async def ask(request: Request):
        body = await request.json()
        return await brain.ask(
            str(body.get("text", ""))[:16000],
            secrets.token_hex(16),
            body.get("allow_search") is True,
            body.get("speak") is True,
        )

    @app.post("/api/probe/{stage}", dependencies=[Depends(administrator)])
    async def probe(stage: str, request: Request):
        auth.rates.check("probes", 20)
        body = await request.json()
        revision, config = state.config(revision=int(body.get("revision", state.meta("saved"))))
        if stage not in ("models", "voices", "mcp-discover") and body.get("confirmed") is not True:
            raise HTTPException(403, "Confirm the potentially billable test first")
        started = time.monotonic()
        async with probes:
            try:
                if stage.startswith("mcp-"):
                    server = next(
                        (
                            server
                            for server in config.mcp_servers
                            if server.server_id == body.get("server_id")
                        ),
                        None,
                    )
                    if server is None:
                        raise ValueError("MCP service not found")
                    arguments = {
                        "server": server.model_dump(),
                        "key": brain.secret(config, server.key_ref),
                        "private_hosts": config.approved_private_hosts,
                    }
                    if stage == "mcp-discover":
                        result = await services.request(
                            "mcp", "/internal/discover", arguments, timeout=server.timeout + 2
                        )
                    elif stage == "mcp-search":
                        arguments.update(
                            {
                                "tool": server.search_tool,
                                "arguments": {
                                    server.query_parameter: str(body.get("query", ""))[:500]
                                },
                            }
                        )
                        result = await services.request(
                            "mcp", "/internal/call", arguments, timeout=server.timeout + 2
                        )
                    elif stage == "mcp-loop":
                        from .llm import Tool

                        catalog = await services.request(
                            "mcp", "/internal/discover", arguments, timeout=server.timeout + 2
                        )
                        entry = next(
                            (entry for entry in catalog if entry["name"] == server.search_tool),
                            None,
                        )
                        if not entry or server.approved_tools.get(entry["name"]) != entry["hash"]:
                            raise ValueError("Approve the selected search tool before testing")
                        definition = Tool(
                            entry["qualified_name"], entry["description"], entry["parameters"]
                        )

                        async def search_probe(name, parameters, identifier):
                            return await services.request(
                                "mcp",
                                "/internal/call",
                                {**arguments, "tool": entry["name"], "arguments": parameters},
                                timeout=server.timeout + 2,
                            )

                        client = LLMClient(
                            config.llm,
                            brain.secret(config, config.llm.key_ref),
                            config.approved_private_hosts,
                        )
                        try:
                            result = await client.run(
                                "Use the search tool once to answer this query: "
                                + str(body.get("query", ""))[:500],
                                [definition],
                                search_probe,
                                force_tool=definition.name,
                            )
                            if result["tool_calls"] == 0:
                                raise ValueError("Model did not use the search tool")
                            result.pop("history", None)
                        finally:
                            await client.close()
                    else:
                        raise ValueError("Unknown MCP test stage")
                elif stage == "voices":
                    result = await services.request(
                        "media",
                        "/internal/voices",
                        {
                            "speech": config.speech.model_dump(),
                            "key": brain.secret(config, config.speech.tts_key_ref),
                            "private_hosts": config.approved_private_hosts,
                        },
                    )
                elif stage == "tts":
                    result = await services.prepare(
                        secrets.token_hex(16),
                        {
                            "kind": "tts",
                            "text": str(body.get("text", "语音连接测试。"))[:200],
                            "secret_versions": config.credential_versions,
                        },
                        config,
                        state,
                    )
                    result.pop("url", None)
                    result["audible"] = "not-tested"
                elif stage == "asr":
                    audio = base64.b64decode(body.get("audio", ""), validate=True)
                    if not audio or len(audio) > 400000:
                        raise ValueError("Provide an authorized short WAV sample")
                    result = await services.request(
                        "voice",
                        "/internal/probe",
                        {
                            "speech": config.speech.model_dump(),
                            "key": brain.secret(config, config.speech.asr_key_ref),
                            "audio": body["audio"],
                        },
                        timeout=25,
                    )
                else:
                    client = LLMClient(
                        config.llm,
                        brain.secret(config, config.llm.key_ref),
                        config.approved_private_hosts,
                    )
                    try:
                        if stage == "models":
                            result = await client.models()
                        elif stage == "text":
                            result = await client.run("只回复：连接测试。")
                        elif stage == "tools":

                            async def echo(name, arguments, identifier):
                                return arguments

                            result = await client.run(
                                "Call probe_echo once with value 7, then report its value.",
                                [PROBE_TOOL],
                                echo,
                                force_tool="probe_echo",
                            )
                            if result["tool_calls"] == 0:
                                raise ValueError("Model did not complete the test tool call")
                        elif stage == "stream":
                            result = await client.run("只回复：流式连接测试。", streaming=True)
                        else:
                            raise ValueError("Unknown test stage")
                        if isinstance(result, dict):
                            result.pop("history", None)
                    finally:
                        await client.close()
                output = {"status": "passed", "stage": stage, "result": result}
            except Exception as error:
                output = {"status": "failed", "stage": stage, "category": type(error).__name__}
            output.update(
                {
                    "revision": revision,
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                    "protocol": config.llm.protocol,
                    "model": config.llm.model,
                }
            )
            fingerprint = state.fingerprint(config, [config.llm.key_ref, config.speech.tts_key_ref])
            state.db.execute(
                "INSERT INTO probes VALUES (?,?,?) ON CONFLICT(fingerprint,stage) DO UPDATE SET result=excluded.result",
                (fingerprint, stage, json.dumps(output, ensure_ascii=False)),
            )
            return output

    @app.get("/api/diagnostics", dependencies=[Depends(administrator)])
    async def diagnostics():
        revision, config = state.config()
        fingerprint = state.fingerprint(config, [config.llm.key_ref, config.speech.tts_key_ref])
        usage = shutil.disk_usage(directory)
        return {
            "saved_revision": int(state.meta("saved")),
            "applied_revision": revision,
            "disk_free": usage.free,
            "disk_total": usage.total,
            "voice": json.loads(state.meta("voice_status", "{}")),
            "probes": {
                row["stage"]: json.loads(row["result"])
                for row in state.db.execute(
                    "SELECT stage,result FROM probes WHERE fingerprint=?", (fingerprint,)
                )
            },
            "events": state.events(
                max(
                    0,
                    int(state.db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0])
                    - 20,
                )
            ),
        }

    @app.get("/api/backup", dependencies=[Depends(administrator)])
    async def export():
        return JSONResponse(
            {
                "schema_version": 1,
                "config": state.config()[1].model_dump(),
                "alarms": await alarms(),
            },
            headers={"Content-Disposition": 'attachment; filename="spk-settings.json"'},
        )

    @app.post("/api/restore", dependencies=[Depends(administrator)])
    async def restore(body: RestoreRequest):
        alarms = [alarm.model_dump() for alarm in body.alarms]
        revision = state.save(body.config, body.expected_revision)
        state.set_meta(
            "pending_alarm_restore", json.dumps({"revision": revision, "alarms": alarms})
        )
        return {"revision": revision, "applied": False}

    @app.get("/api/events", dependencies=[Depends(administrator)])
    async def events(request: Request):
        async def generate():
            async with streams:
                cursor = 0
                while not await request.is_disconnected():
                    try:
                        auth.session(request)
                    except HTTPException:
                        yield "event: session-expired\ndata: {}\n\n"
                        return
                    for event in state.events(cursor):
                        cursor = event["id"]
                        yield "id: %s\ndata: %s\n\n" % (
                            cursor,
                            json.dumps(event, ensure_ascii=False),
                        )
                    yield "event: playback\ndata: %s\n\n" % json.dumps(
                        player.snapshot(), ensure_ascii=False
                    )
                    try:
                        await asyncio.wait_for(player.changed.wait(), 5)
                    except TimeoutError:
                        pass
                    player.changed.clear()

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/device/cmd")
    async def device_command(request: Request):
        device_auth(request)
        boot = request.headers.get("X-Device-Boot-ID", "")[:64]
        previous = state.meta("device_boot")
        if boot and previous and boot != previous:
            player.stop()
            state.set_meta("device_state", "unknown")
        if boot:
            state.set_meta("device_boot", boot)
        # Hold an empty poll briefly so stop/play reach the speaker without a sleep gap.
        deadline = time.monotonic() + device_hold
        command = player.poll()
        while command is None and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
            command = player.poll()
        if not command:
            return Response(status_code=204)
        value = "v2\t%s\t%s\t%s\t%s\t%s\n" % (
            command["id"],
            command["generation"],
            max(1, int(command["expires"] - time.time())),
            command["action"],
            command["argument"] or "-",
        )
        return Response(value, media_type="text/plain")

    @app.post("/device/result")
    async def device_result(request: Request):
        device_auth(request)
        values = parse_qs((await request.body()).decode("ascii"), strict_parsing=True)
        accepted = player.report(
            values.get("id", [""])[0],
            int(values.get("generation", ["-1"])[0]),
            values.get("result", ["unknown"])[0],
            values.get("boot_id", [""])[0],
        )
        return {"accepted": accepted}

    @app.get("/internal/voice/config")
    async def voice_config(request: Request):
        service_auth(request, "voice")
        revision, config = state.config()
        return {
            "revision": revision,
            "config": config.model_dump(),
            "asr_key": brain.secret(config, config.speech.asr_key_ref),
        }

    @app.post("/internal/media/fetched")
    async def media_fetched(request: Request):
        service_auth(request, "media")
        body = await request.json()
        identifier = str(body.get("playback_id", ""))
        if identifier != state.meta("current"):
            return {"accepted": False}
        row = state.db.execute("SELECT payload FROM jobs WHERE id=?", (identifier,)).fetchone()
        if row is None or json.loads(row["payload"]).get("media", {}).get("media_id") != body.get(
            "media_id"
        ):
            return {"accepted": False}
        state.set_meta("media_fetched", json.dumps({"playback_id": identifier, "at": time.time()}))
        state.event("media.fetched", {"playback_id": identifier})
        player.changed.set()
        return {"accepted": True}

    @app.post("/internal/voice/interrupt")
    async def voice_interrupt(request: Request):
        service_auth(request, "voice")
        # Until the session ends, a finished reply must not restart music the mic would hear.
        player.listening_until = time.monotonic() + 300
        interrupted = player.interrupt()
        await brain.cancel()
        return {"ok": True, "interrupted_owned_playback": interrupted}

    @app.post("/internal/voice/done")
    async def voice_done(request: Request):
        service_auth(request, "voice")
        player.listening_until = 0.0
        return {"resumed": player.resume()}

    @app.post("/internal/voice/text")
    async def voice_text(request: Request):
        service_auth(request, "voice")
        body = await request.json()
        if body["revision"] != state.config()[0]:
            raise HTTPException(409, "Session configuration changed")
        text = str(body["text"])[:16000]
        if text.strip("。！! ") in ("停止", "停", "别放了", "停止播放"):
            player.stop()
            return {"text": "已提交停止。", "listen": False}
        result = await brain.ask(
            text,
            str(body["request_id"]),
            allow_search=any(word in text for word in ("联网", "网上搜索")),
            speak=True,
        )
        # Listen again only after the spoken reply, so the microphone does not hear it.
        deadline = time.monotonic() + 90
        while player.active_kind() == "tts" and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        result["listen"] = player.active_kind() not in ("music", "alarm")
        return result

    @app.post("/internal/voice/status")
    async def voice_status(request: Request):
        service_auth(request, "voice")
        body = await request.json()
        state.set_meta(
            "voice_status",
            json.dumps({key: body.get(key) for key in ("last_packet", "errors", "wake")}),
        )
        return {"ok": True}

    @app.get("/static/lucide.js")
    async def icons():
        return FileResponse(
            ROOT / "node_modules/lucide/dist/umd/lucide.min.js", media_type="text/javascript"
        )

    app.mount(
        "/fonts",
        StaticFiles(directory=ROOT / "node_modules/@fontsource/noto-sans-sc", check_dir=False),
        name="fonts",
    )
    app.include_router(display_router(state, auth, player))
    app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")
    return app
