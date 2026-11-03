import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .settings import Settings


def private_file(path: Path, content: bytes):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


class Conflict(ValueError):
    pass


class State:
    def __init__(self, directory: Path, key_file: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key_file = Path(key_file)
        database = self.directory / "control.sqlite3"
        if not key_file.exists():
            if database.exists():
                raise RuntimeError("Master key missing; restore it before opening the database")
            private_file(key_file, AESGCM.generate_key(bit_length=256))
        if key_file.is_symlink() or key_file.stat().st_mode & 0o077:
            raise RuntimeError("Master key must be a private regular file (0600)")
        self.cipher = AESGCM(key_file.read_bytes())
        self.lock = threading.RLock()
        self.db = sqlite3.connect(database, isolation_level=None, check_same_thread=False)
        os.chmod(database, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS config(
                revision INTEGER PRIMARY KEY AUTOINCREMENT, document TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS secrets(
                name TEXT NOT NULL, version INTEGER NOT NULL, ciphertext BLOB NOT NULL,
                PRIMARY KEY(name, version));
            CREATE TABLE IF NOT EXISTS commands(
                id TEXT PRIMARY KEY, generation INTEGER NOT NULL, action TEXT NOT NULL,
                argument TEXT NOT NULL, priority INTEGER NOT NULL, created REAL NOT NULL,
                expires REAL NOT NULL, lease REAL NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'queued', result TEXT NOT NULL DEFAULT '',
                playback_id TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
                generation INTEGER NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
                payload TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL,
                updated REAL NOT NULL, error TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
                payload TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS alarms(
                id TEXT PRIMARY KEY, document TEXT NOT NULL, fired TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS media_catalog(
                media_id TEXT PRIMARY KEY, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS probes(
                fingerprint TEXT NOT NULL, stage TEXT NOT NULL, result TEXT NOT NULL,
                PRIMARY KEY(fingerprint, stage));
        """)
        with self.transaction():
            if self.db.execute("SELECT 1 FROM config LIMIT 1").fetchone() is None:
                revision = self.db.execute(
                    "INSERT INTO config(document, created) VALUES (?, ?)",
                    (Settings().model_dump_json(), time.time()),
                ).lastrowid
                self.set_meta("saved", revision)
                self.set_meta("applied", revision)
                self.set_meta("generation", 0)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.rollback()
                raise
            else:
                self.db.commit()

    def set_meta(self, key, value):
        self.db.execute(
            "INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def meta(self, key, default=""):
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def config(self, applied=True, revision=None):
        with self.lock:
            if revision is None:
                revision = int(self.meta("applied" if applied else "saved"))
            row = self.db.execute(
                "SELECT document FROM config WHERE revision=?", (revision,)
            ).fetchone()
            if row is None:
                raise ValueError("Unknown configuration revision")
            return revision, Settings.model_validate_json(row["document"])

    def save(self, config: Settings, expected: int):
        with self.transaction():
            if int(self.meta("saved")) != expected:
                raise Conflict("Configuration changed; refresh before saving")
            config = config.model_copy(deep=True)
            config.credential_versions = {
                row["name"]: self.secret_version(row["name"])
                for row in self.db.execute("SELECT DISTINCT name FROM secrets")
            }
            revision = self.db.execute(
                "INSERT INTO config(document, created) VALUES (?, ?)",
                (config.model_dump_json(), time.time()),
            ).lastrowid
            self.set_meta("saved", revision)
        return revision

    def apply(self, revision: int):
        self.config(revision=revision)
        with self.transaction():
            pending = json.loads(self.meta("pending_alarm_restore", "{}"))
            if pending.get("revision") == revision:
                self.db.execute("DELETE FROM alarms")
                for alarm in pending["alarms"]:
                    self.db.execute(
                        "INSERT INTO alarms(id,document) VALUES (?,?)",
                        (alarm["id"], json.dumps(alarm, ensure_ascii=False)),
                    )
                self.set_meta("pending_alarm_restore", "{}")
            self.set_meta("applied", revision)
            self.event("config.applied", {"revision": revision})

    def put_secret(self, name: str, value: str, clear=False):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
            raise ValueError("Invalid secret reference")
        if not clear and (not value or value.strip("*") == "" or len(value) > 16384):
            raise ValueError("Supply a real secret, or explicitly clear it")
        with self.transaction():
            version = self.secret_version(name) + 1
            nonce = secrets.token_bytes(12)
            aad = ("%s:%d" % (name, version)).encode()
            encrypted = nonce + self.cipher.encrypt(nonce, ("" if clear else value).encode(), aad)
            self.db.execute("INSERT INTO secrets VALUES (?, ?, ?)", (name, version, encrypted))
            self.db.execute("DELETE FROM probes")
        return version

    def secret_version(self, name):
        with self.lock:
            row = self.db.execute(
                "SELECT MAX(version) AS version FROM secrets WHERE name=?", (name,)
            ).fetchone()
            return row["version"] or 0

    def secret(self, name, version=None):
        with self.lock:
            version = self.secret_version(name) if version is None else version
            row = self.db.execute(
                "SELECT ciphertext FROM secrets WHERE name=? AND version=?", (name, version)
            ).fetchone()
            if row is None:
                return ""
            encrypted = row["ciphertext"]
            return self.cipher.decrypt(
                encrypted[:12], encrypted[12:], ("%s:%d" % (name, version)).encode()
            ).decode()

    def secret_metadata(self):
        with self.lock:
            names = [row["name"] for row in self.db.execute("SELECT DISTINCT name FROM secrets")]
            return {
                name: {"configured": bool(self.secret(name)), "version": self.secret_version(name)}
                for name in names
            }

    def event(self, kind, payload):
        self.db.execute(
            "INSERT INTO events(type, payload, created) VALUES (?, ?, ?)",
            (kind, json.dumps(payload, ensure_ascii=False), time.time()),
        )
        self.db.execute("DELETE FROM events WHERE id < (SELECT MAX(id)-1000 FROM events)")

    def events(self, after=0):
        with self.lock:
            return [
                dict(row)
                for row in self.db.execute(
                    "SELECT * FROM events WHERE id>? ORDER BY id LIMIT 100", (after,)
                )
            ]

    def fingerprint(self, config, refs=()):
        # Only settings that can change a provider probe result invalidate it.
        value = config.model_dump_json(
            include={
                "credential_versions",
                "llm",
                "speech",
                "mcp_servers",
                "approved_private_hosts",
            }
        )
        return hashlib.sha256(value.encode()).hexdigest()

    def backup(self):
        destination = self.directory / ("backup-%s.sqlite3" % secrets.token_hex(8))
        with self.lock, sqlite3.connect(destination) as backup:
            self.db.backup(backup)
        os.chmod(destination, 0o600)
        return destination

    def close(self):
        with self.lock:
            self.db.close()
