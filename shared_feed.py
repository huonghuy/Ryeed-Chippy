"""Persistent admin credentials, browser ownership, and one shared flight feed."""
import csv
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from http.cookies import SimpleCookie


class FeedError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class Access:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.tokens = {}
        self.failures = {}
        if not self.setting("password"):
            self.set_password(os.environ.get("CHIPPY_INITIAL_PASSWORD", "bpp"))

    def setting(self, key):
        with self.store.lock:
            row = self.store.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def set_setting(self, key, value):
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))

    def set_password(self, password):
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1).hex()
        self.set_setting("password", json.dumps([salt.hex(), digest]))

    def check_password(self, password):
        if not isinstance(password, str) or len(password) > 256:
            return False
        salt, expected = json.loads(self.setting("password"))
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(actual, expected)

    @staticmethod
    def cookies(headers):
        jar = SimpleCookie()
        try:
            jar.load(headers.get("Cookie", ""))
        except Exception:
            return {}
        return {k: v.value for k, v in jar.items()}

    def is_admin(self, token):
        with self.lock:
            now = time.monotonic()
            self.tokens = {k: v for k, v in self.tokens.items() if v > now}
            return token in self.tokens

    def login(self, password, address):
        with self.lock:
            now = time.monotonic()
            self.failures = {k: v for k, v in self.failures.items() if v[1] > now}
            attempts, until = self.failures.get(address, (0, now + 60))
            # Per visitor, so one visitor can't lock out the admin; the total bounds guessing if addresses are spoofed.
            if attempts >= 5 or sum(v[0] for v in self.failures.values()) >= 50:
                raise FeedError("Too many login attempts. Wait one minute and try again.", 429)
            if not self.check_password(password):
                self.failures[address] = (attempts + 1, until)
                raise FeedError("Incorrect admin password.", 401)
            self.failures.pop(address, None)
            token = secrets.token_hex(32)
            self.tokens[token] = now + 12 * 3600
            return token

    def change_password(self, token, current, new):
        with self.lock:
            if not self.is_admin(token):
                raise FeedError("Log into Admin Input first.", 401)
            if not self.check_password(current):
                raise FeedError("Current password is incorrect.", 403)
            if not isinstance(new, str) or not 3 <= len(new) <= 256:
                raise FeedError("Use a password between 3 and 256 characters.")
            self.set_password(new)
            # Other browsers and uploader programs must log in with the new password.
            self.tokens = {token: time.monotonic() + 12 * 3600}

    def visible(self, sid, owner, admin=False):
        with self.store.lock:
            row = self.store.db.execute("SELECT owner,shared FROM sessions WHERE id=?", (sid,)).fetchone()
            return bool(row and (admin or row[1] or row[0] == owner))

    def sessions(self, owner, admin=False):
        return [s for s in self.store.sessions() if self.visible(s["id"], owner, admin)]

    def start(self, owner, shared, demo=False):
        source = "Admin live feed" if shared else "Demo · private" if demo else "User input"
        sid = self.store.create_session(source, owner=owner, shared=shared)
        if shared:
            self.publish(sid)
        return sid

    def publish(self, sid):
        self.set_setting("live_session", sid)

    def append(self, sid, owner, lines, batch, shared, admin):
        if not isinstance(lines, list) or not 1 <= len(lines) <= 200 or any(not isinstance(s, str) or len(s) > 2048 or "\n" in s or "\r" in s for s in lines):
            raise FeedError("Send 1–200 serial lines, each up to 2048 characters.")
        if not isinstance(batch, str) or not 1 <= len(batch) <= 100:
            raise FeedError("A batch ID is required.")
        with self.store.lock:
            row = self.store.db.execute("SELECT owner,shared FROM sessions WHERE id=?", (sid,)).fetchone()
            if not row or row[0] != owner or bool(row[1]) != shared:
                raise FeedError("This feed belongs to a different input connection.", 403)
            if shared and (not admin or self.setting("live_session") != sid):
                raise FeedError("This feed is no longer active. Log in and start publishing again.", 409)
            if self.store.db.execute("SELECT 1 FROM feed_batches WHERE session=? AND batch=?", (sid, batch)).fetchone():
                return {"ok": True, "repeated": True}
            accepted = invalid = 0
            with self.store.db:
                for line in lines:
                    stamp = datetime.now(timezone.utc).isoformat()
                    self.store.db.execute("INSERT INTO raw_lines(session,received_at,line) VALUES (?,?,?)", (sid, stamp, line))
                    try:
                        if self.store.add(sid, line, stamp, commit=False):
                            accepted += 1
                    except (ValueError, csv.Error):
                        invalid += 1
                self.store.db.execute("INSERT INTO feed_batches VALUES (?,?)", (sid, batch))
            return {"ok": True, "accepted": accepted, "invalid": invalid}

    def raw(self, sid, after):
        with self.store.lock:
            # Only the recent monitor window is returned on initial connection.
            if not after:
                rows = self.store.db.execute("SELECT id,received_at,line FROM raw_lines WHERE session=? ORDER BY id DESC LIMIT 300", (sid,)).fetchall()[::-1]
            else:
                rows = self.store.db.execute("SELECT id,received_at,line FROM raw_lines WHERE session=? AND id>? ORDER BY id LIMIT 300", (sid, after)).fetchall()
            return [dict(zip(("id", "received_at", "line"), row)) for row in rows]
