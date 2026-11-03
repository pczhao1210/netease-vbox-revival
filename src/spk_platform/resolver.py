import asyncio
import base64
import io
import json
import os
import re
import signal
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from pydantic import Field

from .settings import Model
from .workers import worker_app


class Search(Model):
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=12, ge=1, le=50)


class Collection(Model):
    kind: Literal["daily", "liked", "favorites"]
    limit: int = Field(default=30, ge=1, le=50)


COLLECTION_COMMANDS = {
    "daily": ("recommend songs", "recommend daily"),
    "liked": ("playlist liked", "playlist likelist"),
    "favorites": ("playlist collected",),
}


def collection_command(kind, directory):
    for candidate in COLLECTION_COMMANDS[kind]:
        if re.search(r"(?m)^\s*" + re.escape(candidate) + r"(?:\s{2,}|\s*$)", directory):
            return candidate.split()
        parent, child = candidate.split()
        active = ""
        for line in directory.splitlines():
            root = re.match(r"^([a-z][a-z0-9-]*)\b", line)
            if root:
                active = root.group(1)
            if active == parent and re.match(r"^\s+" + re.escape(child) + r"(?:\s{2,}|\s*$)", line):
                return [parent, child]
    return None


def song_rows(value):
    if isinstance(value, dict):
        if value.get("code") not in (None, 0, 200) or value.get("success") is False:
            raise ValueError("Music provider rejected the collection request")
        value = value.get("data", value)
        for name in ("records", "songs", "dailySongs", "tracks", "items"):
            if isinstance(value, dict) and isinstance(value.get(name), list):
                value = value[name]
                break
    if not isinstance(value, list):
        raise ValueError("Music provider returned an unsupported song collection shape")
    return [row for row in value if isinstance(row, dict)]


def public_song(song):
    original = song.get("originalId", song.get("orig"))
    identifier = song.get("id")
    if not identifier or original is None:
        return None
    artists = song.get("artists") or []
    artist = song.get("artist") or "/".join(
        str(item.get("name", "")) for item in artists[:3] if isinstance(item, dict)
    )
    return {
        "provider": "ncm",
        "id": str(identifier),
        "original_id": str(original),
        "title": str(song.get("name", song.get("title", "")))[:200],
        "artist": str(artist)[:200],
        "playable": song.get("visible") is not False
        and song.get("playFlag", song.get("play", True)) is not False,
    }


def cli_json(text):
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"(?m)^\s*[\[{]", text):
        try:
            value, end = decoder.raw_decode(text[match.start() :].lstrip())
            return value
        except ValueError:
            continue
    return None


def login_url(text):
    value = cli_json(text)
    candidates = re.findall(r"https://[^\s\x1b<>\"']+", text)
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str) and item.startswith("https://"):
            candidates.insert(0, item)
    for candidate in candidates:
        candidate = candidate.rstrip("，。)")
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        host = parsed.hostname or ""
        if (
            (host == "music.163.com" or host.endswith(".music.163.com"))
            and not parsed.username
            and not parsed.password
        ):
            if len(candidate) <= 4096:
                return candidate
    return ""


def login_status(code, text):
    if re.search(r"API.?[Kk]ey.*未设置|PRIVATE_KEY.*未设置|未配置.*凭据", text):
        return "unconfigured"
    if re.search(
        r"未登录|尚未登录|请先登录|未授权|登录.*(?:过期|失效)|not logged|unauthorized", text, re.I
    ):
        return "unauthorized"
    value = cli_json(text)
    data = value.get("data", value) if isinstance(value, dict) else {}
    if code == 0 and isinstance(data, dict):
        flags = [data.get(name) for name in ("loggedIn", "isLoggedIn", "authenticated", "isLogin")]
        if any(flag is False for flag in flags):
            return "unauthorized"
        if any(flag is True for flag in flags):
            return "authorized"
    if code == 0 and re.search(r"已登录|登录状态.*(?:正常|有效)|登录有效|logged in", text, re.I):
        return "authorized"
    return "unknown" if code == 0 else "failed"


def qr_image(url):
    import qrcode

    image = qrcode.make(url, box_size=6, border=4)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


def create_resolver_app(keys, runtime=None, cli=None, runner=None):
    app, authorized = worker_app("resolver", keys)
    root = Path(runtime or os.getenv("SPK_RESOLVER_DATA", "/tmp/resolver"))
    root.mkdir(parents=True, exist_ok=True)
    (root / "log").mkdir(exist_ok=True)
    cli = Path(cli or os.getenv("SPK_NCM_CLI", "/app/node_modules/@music163/ncm-cli/dist/index.js"))
    node = os.getenv("SPK_NODE", "node")
    lock = asyncio.Lock()
    catalog = {}
    catalog_file = root / "catalog.json"
    if catalog_file.is_file() and not catalog_file.is_symlink():
        try:
            stored = json.loads(catalog_file.read_text())
            if isinstance(stored, dict) and len(stored) <= 1000:
                catalog.update(stored)
        except (OSError, ValueError):
            pass
    account_file = root / "account-state.json"
    login = {"configured": False, "state": "unknown", "url": "", "expires_at": 0, "checked_at": 0}
    if account_file.is_file() and not account_file.is_symlink():
        try:
            login["configured"] = json.loads(account_file.read_text()).get("configured") is True
        except (OSError, ValueError, AttributeError):
            pass

    def save_catalog():
        while len(catalog) > 1000:
            catalog.pop(next(iter(catalog)))
        temporary = root / "catalog.tmp"
        temporary.write_text(json.dumps(catalog, ensure_ascii=False))
        os.replace(temporary, catalog_file)

    def environment():
        return {
            key: value
            for key, value in os.environ.items()
            if key in ("HOME", "PATH", "LANG", "XDG_CONFIG_HOME")
        }

    async def command(arguments, timeout=30, strict=True):
        if not cli.is_file():
            raise HTTPException(503, "Pinned ncm-cli is not installed")
        if runner is not None:
            code, text = await runner(arguments, timeout)
            if strict and code:
                raise HTTPException(503, "Music provider rejected the request")
            return text if strict else (code, text)
        process = await asyncio.create_subprocess_exec(
            node,
            str(cli),
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment(),
            start_new_session=True,
        )
        try:
            output, error = await asyncio.wait_for(process.communicate(), timeout)
            if len(output) + len(error) > 2 * 1048576:
                raise HTTPException(503, "Music CLI response exceeds its limit")
            if strict and process.returncode:
                raise HTTPException(
                    503,
                    "Music CLI is not configured, not logged in, or the provider rejected the request",
                )
            text = (output + error).decode("utf-8", "replace")
            return text if strict else (process.returncode, text)
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
                await process.wait()

    def legacy():
        import spk_alarm

        spk_alarm.HERE = str(root)
        spk_alarm.NCM_DIR = str(cli.parents[4])
        spk_alarm.NCM_CLI = str(cli)
        spk_alarm.NODE = node
        spk_alarm.NCM_BIN = os.getenv("SPK_MPV_BIN", "/app/bin")
        spk_alarm.URL_JSON = str(root / "log/ncm_url.json")
        spk_alarm.URL_LOCK_F = str(root / "log/ncm_url.lock")
        spk_alarm.LOGF = str(root / "log/resolver.log")
        return spk_alarm

    def public_account(include_qr=False):
        if login["state"] == "pending" and login["expires_at"] <= time.time():
            login.update(state="expired", url="")
        result = {key: login[key] for key in ("configured", "state", "expires_at", "checked_at")}
        result.update(
            version="0.1.7", available=cli.is_file(), login_running=login["state"] == "pending"
        )
        if include_qr and login["state"] == "pending" and login["url"]:
            result.update(qr_url=login["url"], qr_image=qr_image(login["url"]))
        return result

    async def check_account():
        code, text = await command(["login", "--check"], timeout=20, strict=False)
        status = login_status(code, text)
        if (
            status == "unauthorized"
            and login["state"] == "pending"
            and login["expires_at"] > time.time()
        ):
            status = "pending"
        login["state"] = status
        login["checked_at"] = time.time()
        if status == "authorized":
            login.update(configured=True, url="", expires_at=0)
        elif status == "unconfigured":
            login.update(configured=False, url="", expires_at=0)
        return public_account(include_qr=True)

    async def command_directory():
        return await command(["commands"], timeout=20)

    async def load_collection(body):
        status = await check_account()
        if status["state"] != "authorized":
            raise HTTPException(409, "请先完成网易云扫码授权")
        directory = await command_directory()
        selected = collection_command(body.kind, directory)
        if selected is None:
            raise HTTPException(
                424, "当前账号的 CLI 命令目录未提供这项能力，请刷新目录或检查开放平台权限"
            )
        intent = {
            "daily": "查看每日推荐歌曲",
            "liked": "查看红心歌曲",
            "favorites": "查看收藏歌单",
        }[body.kind]
        text = await command(selected + ["--userInput", intent], timeout=40)
        value = cli_json(text)
        if body.kind == "favorites":
            data = value.get("data", value) if isinstance(value, dict) else value
            if isinstance(data, dict):
                data = data.get("records", data.get("playlists"))
            if not isinstance(data, list):
                raise HTTPException(503, "收藏歌单返回格式不受支持")
            result = []
            for entry in data[: body.limit]:
                if not isinstance(entry, dict):
                    continue
                identifier = str(entry.get("originalId", entry.get("id", "")))
                if not identifier.isdecimal():
                    continue
                result.append(
                    {
                        "id": identifier,
                        "title": str(entry.get("name", ""))[:200],
                        "track_count": entry.get("trackCount", 0),
                        "kind": "playlist",
                    }
                )
            return {"kind": body.kind, "playlists": result}
        tracks = []
        for entry in song_rows(value)[: body.limit]:
            track = public_song(entry)
            if track:
                catalog[track["id"]] = {
                    "id": track["id"],
                    "orig": track["original_id"],
                    "name": track["title"],
                    "artist": track["artist"],
                    "play": track["playable"],
                }
                tracks.append(track)
        save_catalog()
        return {"kind": body.kind, "tracks": tracks}

    @app.get("/account", dependencies=[Depends(authorized)])
    async def account():
        return public_account()

    @app.post("/account/check", dependencies=[Depends(authorized)])
    async def check():
        async with lock:
            return await check_account()

    @app.post("/account/credentials", dependencies=[Depends(authorized)])
    async def credentials(request: Request):
        body = await request.json()
        app_id = str(body.get("app_id", ""))
        private_key = str(body.get("private_key", ""))
        if not app_id or len(app_id) > 200 or not 80 <= len(private_key) <= 8192:
            raise ValueError("Provide a valid music application ID and private key")
        async with lock:
            await command(["config", "set", "appId", app_id])
            await command(["config", "set", "privateKey", "--", private_key])
            await command(["config", "set", "player", "mpv"])
        login.update(configured=True, state="unknown", url="", expires_at=0, checked_at=0)
        account_file.write_text(json.dumps({"configured": True}))
        os.chmod(account_file, 0o600)
        return public_account()

    @app.post("/account/login", dependencies=[Depends(authorized)])
    async def start_login():
        async with lock:
            if login["state"] == "pending" and login["expires_at"] > time.time():
                return public_account(include_qr=True)
            code, text = await command(["login", "--background"], timeout=20, strict=False)
            url = login_url(text)
            if code == 0 and url:
                login.update(state="pending", url=url, expires_at=time.time() + 180)
                return public_account(include_qr=True)
            status = login_status(code, text)
            login.update(state="unconfigured" if status == "unconfigured" else "failed", url="")
            return public_account()

    @app.post("/account/poll", dependencies=[Depends(authorized)])
    async def poll_login():
        async with lock:
            if login["state"] == "pending" and login["expires_at"] <= time.time():
                login.update(state="expired", url="")
                return public_account()
            return await check_account()

    @app.post("/account/logout", dependencies=[Depends(authorized)])
    async def logout():
        async with lock:
            await command(["logout"])
        login.update(state="unauthorized", url="", expires_at=0, checked_at=time.time())
        return public_account()

    @app.post("/capabilities", dependencies=[Depends(authorized)])
    async def capabilities():
        async with lock:
            account = await check_account()
            if account["state"] != "authorized":
                raise HTTPException(409, "请先完成网易云扫码授权")
            directory = await command_directory()
            return {
                kind: {"available": collection_command(kind, directory) is not None}
                for kind in COLLECTION_COMMANDS
            }

    @app.post("/collections", dependencies=[Depends(authorized)])
    async def collections(body: Collection):
        async with lock:
            return await load_collection(body)

    @app.post("/search", dependencies=[Depends(authorized)])
    async def search(body: Search):
        if not cli.is_file():
            raise HTTPException(503, "Music resolver is not installed")
        async with lock:
            songs = await asyncio.to_thread(legacy().search_songs, body.query, body.limit)
        result = []
        for song in songs:
            catalog[str(song["id"])] = song
            result.append(
                {
                    "provider": "ncm",
                    "id": str(song["id"]),
                    "original_id": str(song["orig"]),
                    "title": song["name"],
                    "artist": song.get("artist", ""),
                    "playable": song.get("play", True),
                }
            )
        while len(catalog) > 1000:
            catalog.pop(next(iter(catalog)))
        save_catalog()
        return result

    @app.post("/resolve", dependencies=[Depends(authorized)])
    async def resolve(request: Request):
        body = await request.json()
        identifier = str(body.get("id", ""))
        song = catalog.get(identifier)
        original = str(body.get("original_id", ""))
        if (
            song is None
            and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identifier)
            and original.isdecimal()
            and len(original) <= 32
        ):
            # Saved alarms and queues outlive the bounded search catalog.
            song = {"id": identifier, "orig": original, "play": True}
        if song is None:
            raise ValueError("Search for this track again before resolving it")
        if not song.get("play", True):
            raise HTTPException(403, "Account has no known playback rights for this track")
        async with lock:
            url, reason, denied = await asyncio.to_thread(legacy().fetch_url, song, 30, 20)
        if not url:
            raise HTTPException(
                403 if denied else 503,
                "Playback rights unavailable" if denied else "Music URL resolution failed",
            )
        return {"url": url, "id": identifier}

    @app.post("/playlists", dependencies=[Depends(authorized)])
    async def playlists(request: Request):
        playlist = str((await request.json()).get("id", ""))
        if not playlist.isdecimal() or len(playlist) > 32:
            raise ValueError("Invalid playlist ID")
        async with lock:
            module = legacy()
            module.PLAYLIST = playlist
            module.TRACKS_F = str(root / "log" / ("tracks-%s.json" % playlist))
            songs = await asyncio.to_thread(module.tracks, True)
        for song in songs:
            catalog[str(song["id"])] = song
        save_catalog()
        return [
            {
                "provider": "ncm",
                "id": str(song["id"]),
                "original_id": str(song["orig"]),
                "title": song["name"],
                "playable": song.get("play", True),
            }
            for song in songs
        ]

    return app
