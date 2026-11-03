import hashlib
import hmac
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException

from .state import private_file


class RateLimiter:
    def __init__(self):
        self.buckets = {}

    def check(self, key, limit=20, window=60):
        now = time.monotonic()
        self.buckets = {
            name: values for name, values in self.buckets.items() if values[-1] > now - window
        }
        hits = [value for value in self.buckets.get(key, []) if value > now - window]
        if len(hits) >= limit or len(self.buckets) > 1024:
            raise HTTPException(429, "Too many requests")
        hits.append(now)
        self.buckets[key] = hits


def password_hash(password, salt=None):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError("Password must contain 12 to 256 characters")
    salt = salt or secrets.token_hex(16)
    hashed = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ":" + hashed


def set_admin_password(state, password):
    hashed = password_hash(password)
    with state.transaction():
        state.set_meta("admin_password", hashed)
        state.set_meta("admin_password_version", secrets.token_hex(16))
        state.event("admin.password_changed", {})


def token_file(directory, name):
    path = token_path(directory, name)
    if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise RuntimeError("Missing/private service token required: " + name)
    token = path.read_text().strip()
    if len(token) < 32:
        raise RuntimeError("Service token is too short: " + name)
    return token


def token_path(directory, name):
    directory = Path(directory)
    group = "media" if name == "media-ticket" else name
    nested = directory / group
    return (nested if nested.is_dir() else directory) / (name + ".token")


def initialize_tokens(directory):
    for name in ("media", "voice", "resolver", "mcp", "device", "media-ticket", "bootstrap"):
        path = token_path(directory, name)
        if not path.exists():
            private_file(path, secrets.token_urlsafe(48).encode())


def require_token(request, expected):
    supplied = request.headers.get("Authorization", "")
    if not hmac.compare_digest(supplied.encode(), ("Bearer " + expected).encode()):
        raise HTTPException(403, "Not authorized")


class AdminAuth:
    def __init__(self, state, origin):
        self.state = state
        self.origin = origin.rstrip("/")
        self.secure = self.origin.startswith("https://")
        self.sessions = {}
        self.rates = RateLimiter()

    @property
    def configured(self):
        return bool(self.state.meta("admin_password"))

    def session(self, request, authenticated=True):
        identifier = request.cookies.get("spk_session", "")
        record = self.sessions.get(hashlib.sha256(identifier.encode()).hexdigest())
        if (
            not record
            or record["expires"] < time.time()
            or record.get("credential_version", "") != self.state.meta("admin_password_version")
            or (authenticated and not record["authenticated"])
        ):
            raise HTTPException(401, "Login required")
        return record

    def issue(self, response, authenticated=False):
        now = time.time()
        self.sessions = {
            key: value for key, value in self.sessions.items() if value["expires"] > now
        }
        if len(self.sessions) >= 256:
            # Anonymous login pages must not evict signed-in administrators.
            victim = next(
                (key for key, value in self.sessions.items() if not value["authenticated"]),
                next(iter(self.sessions)),
            )
            self.sessions.pop(victim)
        token = secrets.token_urlsafe(32)
        record = {
            "csrf": secrets.token_urlsafe(32),
            "expires": now + 28800,
            "authenticated": authenticated,
            "credential_version": self.state.meta("admin_password_version"),
        }
        self.sessions[hashlib.sha256(token.encode()).hexdigest()] = record
        response.set_cookie(
            "spk_session",
            token,
            httponly=True,
            secure=self.secure,
            samesite="strict",
            max_age=28800,
            path="/",
        )
        return record

    def write(self, request, authenticated=True):
        session = self.session(request, authenticated)
        if request.headers.get("Origin", "") != self.origin:
            raise HTTPException(403, "Invalid request origin")
        if not hmac.compare_digest(
            request.headers.get("X-CSRF-Token", "").encode(), session["csrf"].encode()
        ):
            raise HTTPException(403, "Invalid CSRF token")
        return session

    def setup(self, request, password, bootstrap="", expected=""):
        if self.configured:
            raise HTTPException(409, "Administrator already configured")
        address = request.client.host if request.client else ""
        local = address in ("127.0.0.1", "::1", "testclient")
        loopback_origin = urlsplit(self.origin).hostname in ("127.0.0.1", "::1", "localhost")
        if not local and (
            not (self.secure or loopback_origin)
            or not expected
            or not isinstance(bootstrap, str)
            or not hmac.compare_digest(bootstrap.encode(), expected.encode())
        ):
            raise HTTPException(403, "Remote setup requires HTTPS and the local bootstrap token")
        set_admin_password(self.state, password)

    def login(self, password):
        stored = self.state.meta("admin_password")
        if not stored:
            raise HTTPException(409, "Configure an administrator first")
        try:
            computed = password_hash(password, stored.split(":")[0])
        except ValueError:
            computed = ""
        if not hmac.compare_digest(stored, computed):
            raise HTTPException(403, "Invalid credentials")

    def logout(self, request):
        key = hashlib.sha256(request.cookies.get("spk_session", "").encode()).hexdigest()
        self.sessions.pop(key, None)
