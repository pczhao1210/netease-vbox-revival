import ipaddress
import os
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def endpoint(value: str, allow_http: bool = False) -> str:
    if not value:
        return ""
    parsed = urlsplit(value)
    if (
        parsed.scheme not in (("https", "http") if allow_http else ("https",))
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.query
    ):
        raise ValueError(
            "Endpoint must be an absolute HTTPS URL without credentials/query/fragment"
        )
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("Invalid endpoint port")
    return value.rstrip("/")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Device(Model):
    ip: str = ""
    allowed_cidrs: list[str] = Field(default_factory=list)
    advertised_media_url: str = ""
    command_ttl: int = Field(default=30, ge=3, le=120)
    state_ttl: int = Field(default=15, ge=3, le=120)

    @field_validator("ip")
    @classmethod
    def valid_ip(cls, value):
        if value:
            ipaddress.ip_address(value)
        return value

    @field_validator("allowed_cidrs")
    @classmethod
    def valid_cidrs(cls, values):
        return [str(ipaddress.ip_network(value, strict=True)) for value in values]

    @field_validator("advertised_media_url")
    @classmethod
    def valid_media_url(cls, value):
        value = endpoint(value, allow_http=True)
        if value:
            host = urlsplit(value).hostname
            if host in ("localhost", "0.0.0.0", "::"):
                raise ValueError("Speaker media URL must be reachable on the LAN")
            try:
                if ipaddress.ip_address(host).is_loopback:
                    raise ValueError("Speaker cannot reach a loopback media URL")
            except ValueError as error:
                if "loopback" in str(error):
                    raise
        return value


class LLM(Model):
    endpoint: str = ""
    protocol: Literal["chat", "responses"] = "chat"
    auth: Literal["bearer", "azure-api-key"] = "bearer"
    model: str = Field(default="", max_length=200)
    key_ref: str = "llm"
    timeout: float = Field(default=30, ge=1, le=120)
    max_rounds: int = Field(default=6, ge=1, le=12)
    max_tokens: int = Field(default=1024, ge=64, le=8192)
    chat_token_parameter: Literal["max_completion_tokens", "max_tokens"] = "max_completion_tokens"
    mcp_servers: list[str] = Field(default_factory=list)

    @field_validator("endpoint")
    @classmethod
    def valid_endpoint(cls, value):
        return endpoint(value, allow_http=True)


class Speech(Model):
    region: str = ""
    asr_endpoint: str = ""
    tts_endpoint: str = ""
    asr_key_ref: str = "azure_asr"
    tts_key_ref: str = "azure_tts"
    language: str = "zh-CN"
    voice: str = "zh-CN-XiaoxiaoNeural"
    rate: float = Field(default=1, ge=0.5, le=1.5)
    gain_db: float = Field(default=-6, ge=-24, le=0)
    streaming: bool = False
    session_seconds: int = Field(default=20, ge=3, le=60)
    resume_seconds: float = Field(default=8, ge=0, le=30)
    vad_threshold: float = Field(default=0.015, ge=0.001, le=0.5)
    mic_gain: float = Field(default=6, ge=1, le=20)
    vad_mode: int = Field(default=2, ge=0, le=3)

    @field_validator("asr_endpoint", "tts_endpoint")
    @classmethod
    def valid_endpoint(cls, value):
        return endpoint(value)

    @field_validator("region")
    @classmethod
    def valid_region(cls, value):
        if value and not re.fullmatch(r"[a-z0-9-]{2,40}", value):
            raise ValueError("Invalid Speech region")
        return value

    @field_validator("language", "voice")
    @classmethod
    def valid_name(cls, value):
        if not re.fullmatch(r"[A-Za-z0-9-]{2,100}", value):
            raise ValueError("Invalid language or voice")
        return value


class Music(Model):
    cache_mb: int = Field(default=300, ge=10, le=100000)
    one_file_mb: int = Field(default=40, ge=1, le=500)
    disk_floor_mb: int = Field(default=2048, ge=64, le=100000)
    cache_enabled: bool = True
    repeat: Literal["off", "one", "all"] = "off"
    shuffle: bool = False
    advance_margin: float = Field(default=1.5, ge=0, le=20)
    night_start: int = Field(default=21, ge=0, le=23)
    night_end: int = Field(default=7, ge=0, le=23)
    night_enabled: bool = True
    timezone: str = "Asia/Shanghai"

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(value)
        except Exception:
            raise ValueError("Unknown timezone") from None
        return value

    @model_validator(mode="after")
    def valid_night(self):
        if self.night_enabled and self.night_start == self.night_end:
            raise ValueError("Night start and end must differ")
        return self


class MCPServer(Model):
    server_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,23}$")
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = False
    transport: Literal["streamable-http", "stdio"] = "streamable-http"
    endpoint: str = ""
    template: Literal["brave-search"] = "brave-search"
    auth: Literal["none", "bearer", "api-key", "oauth"] = "none"
    header_name: str = Field(default="X-API-Key", pattern=r"^[A-Za-z][A-Za-z0-9-]{0,63}$")
    key_ref: str = ""
    approved_tools: dict[str, str] = Field(default_factory=dict)
    search_tool: str = ""
    query_parameter: str = Field(default="query", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
    timeout: float = Field(default=10, ge=1, le=60)
    max_results: int = Field(default=5, ge=1, le=10)
    max_bytes: int = Field(default=32768, ge=1024, le=65536)
    cache_ttl: int = Field(default=0, ge=0, le=3600)

    @field_validator("endpoint")
    @classmethod
    def valid_endpoint(cls, value):
        return endpoint(value, allow_http=True)

    @model_validator(mode="after")
    def valid_transport(self):
        if self.enabled and self.auth == "oauth":
            raise ValueError("OAuth is not supported; keep this server disabled")
        if self.enabled and self.transport == "streamable-http" and not self.endpoint:
            raise ValueError("MCP endpoint is required")
        if self.header_name.lower() in ("host", "cookie", "origin", "connection", "content-length"):
            raise ValueError("This authentication header is not allowed")
        return self


class Appearance(Model):
    theme: Literal["light", "dark", "eink"] = "light"
    eink_refresh_seconds: int = Field(default=60, ge=15, le=300)
    eink_layout: Literal["auto", "portrait", "landscape"] = "auto"


class Settings(Model):
    schema_version: Literal[1] = 1
    credential_versions: dict[str, int] = Field(default_factory=dict)
    device: Device = Field(default_factory=Device)
    llm: LLM = Field(default_factory=LLM)
    speech: Speech = Field(default_factory=Speech)
    music: Music = Field(default_factory=Music)
    appearance: Appearance = Field(default_factory=Appearance)
    mcp_servers: list[MCPServer] = Field(default_factory=list, max_length=8)
    search_mode: Literal["off", "auto", "explicit"] = "off"
    approved_private_hosts: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def unique_servers(self):
        identifiers = [server.server_id for server in self.mcp_servers]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Duplicate MCP server ID")
        for host in self.approved_private_hosts:
            if not re.fullmatch(r"[a-zA-Z0-9.:-]{1,253}", host):
                raise ValueError("Private host allowlist contains an invalid hostname")
        return self

    @classmethod
    def from_legacy(cls):
        config = cls()
        config.llm.endpoint = os.getenv("SPK_LLM_BASE") or os.getenv("DS_BASE", "")
        config.llm.model = os.getenv("SPK_LLM_MODEL") or os.getenv("DS_MODEL", "")
        config.device.ip = os.getenv("SPK_DEVICE_IP", "")
        return config
