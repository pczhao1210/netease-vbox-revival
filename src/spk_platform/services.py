import hashlib
import hmac
import os
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .auth import token_file


class Services:
    def __init__(self, key_directory, urls=None, transport=None):
        self.keys = Path(key_directory)
        self.urls = urls or {
            "media": os.getenv("SPK_MEDIA_INTERNAL_URL", "http://media:8899"),
            "resolver": os.getenv("SPK_RESOLVER_URL", "http://music-resolver:8082"),
            "mcp": os.getenv("SPK_MCP_URL", "http://mcp-worker:8083"),
            "voice": os.getenv("SPK_VOICE_URL", "http://voice:8084"),
        }
        self.http = httpx.AsyncClient(timeout=270, trust_env=False, transport=transport)

    async def request(self, service, path, body=None, method="POST", timeout=30):
        url = self.urls[service].rstrip("/") + path
        response = await self.http.request(
            method,
            url,
            json=body,
            headers={"Authorization": "Bearer " + token_file(self.keys, service)},
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    async def prepare(self, identifier, payload, config, state):
        track = payload.get("track")
        body = {
            "playback_id": identifier,
            "kind": payload["kind"],
            "music": config.music.model_dump(),
            "private_hosts": config.approved_private_hosts,
            "key": "",
            "speech": config.speech.model_dump(),
            "start": payload.get("start", 0),
        }
        media = None
        if payload["kind"] == "tts":
            body["text"] = payload["text"]
            body["key"] = state.secret(
                config.speech.tts_key_ref,
                payload.get("secret_versions", {}).get(config.speech.tts_key_ref),
            )
        elif track["provider"] == "local":
            body["local_id"] = track["id"]
            body["cache_key"] = "local:" + track["id"]
        elif track["provider"] == "cache":
            body["media_id"] = track["id"]
        else:
            body["cache_key"] = "ncm:" + track["id"] + ":mp3-128k"
            if config.music.cache_enabled:
                # A cached or prefetched track needs no new provider URL.
                try:
                    media = await self.request("media", "/internal/prepare", body, timeout=260)
                except httpx.HTTPStatusError as error:
                    if error.response.status_code != 400:
                        raise
            if media is None:
                resolved = await self.request(
                    "resolver",
                    "/resolve",
                    {"id": track["id"], "original_id": track.get("original_id", "")},
                    timeout=90,
                )
                body["url"] = resolved["url"]
        if media is None:
            media = await self.request("media", "/internal/prepare", body, timeout=260)
        expires = int(time.time() + min(86400, media["duration"] + 300))
        ticket = media_ticket(
            token_file(self.keys, "media-ticket"), media["media_id"], identifier, expires
        )
        media["url"] = (
            config.device.advertised_media_url
            + "/media/"
            + media["media_id"]
            + ".mp3?"
            + urlencode({"playback_id": identifier, "expires": expires, "ticket": ticket})
        )
        return media

    async def cancel(self, identifier):
        if not identifier:
            return
        try:
            await self.request("media", "/internal/cancel", {"playback_id": identifier}, timeout=2)
        except (httpx.HTTPError, OSError):
            pass

    async def close(self):
        await self.http.aclose()


def media_ticket(secret, media_id, playback_id, expires):
    value = "%s:%s:%s" % (media_id, playback_id, expires)
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def check_ticket(secret, media_id, playback_id, expires, supplied):
    if expires < time.time() or expires > time.time() + 86400:
        return False
    expected = media_ticket(secret, media_id, playback_id, expires)
    return hmac.compare_digest(expected.encode(), supplied.encode("utf-8", "replace"))
