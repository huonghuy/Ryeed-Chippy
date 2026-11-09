"""Same-origin HTTP API for private inputs and authenticated shared publishing."""
import csv
import io
import json
import mimetypes
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import secrets

from shared_feed import Access, FeedError
from mission import MapConfig, Airspace


def handler_for(store, receiver, root, columns, public_url=None):
    access = Access(store)
    maps = MapConfig(store)
    airspace = Airspace()
    configured = urlparse(public_url) if public_url else None
    if configured and (configured.scheme not in ("http", "https") or not configured.netloc or configured.path not in ("", "/")):
        raise ValueError("--public-url must be an http(s) origin without a path")

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(20)

        def log_message(self, *_):
            pass

        def context(self):
            cookies = access.cookies(self.headers)
            self.owner = cookies.get("chippy_browser", "")
            if len(self.owner) != 64 or any(c not in "0123456789abcdef" for c in self.owner):
                self.owner = secrets.token_hex(32)
            self.token = cookies.get("chippy_admin", "")
            self.admin = access.is_admin(self.token)

        def cookie(self, name, value, age, same_site="Strict"):
            secure = "; Secure" if configured and configured.scheme == "https" else ""
            return f"{name}={value}; Path=/; HttpOnly; SameSite={same_site}; Max-Age={age}{secure}"

        def reply(self, data, content_type="application/json", status=200, extra=None):
            body = json.dumps(data, allow_nan=False).encode() if content_type == "application/json" else data
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("X-Frame-Options", "DENY")
            # Lax: browsers withhold Strict cookies on links from other sites, and a reissued ID would orphan this browser's live feed.
            self.send_header("Set-Cookie", self.cookie("chippy_browser", self.owner, 365 * 86400, "Lax"))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def validate_origin(self, post=False):
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if configured:
                hosts.add(configured.netloc)
            if self.headers.get("Host") not in hosts:
                raise FeedError("Host is not configured. Set --public-url for the website address.", 403)
            if post:
                origin = self.headers.get("Origin")
                expected = {"http://" + h for h in hosts if h.startswith(("127.0.0.1:", "localhost:"))}
                if configured:
                    expected.add(f"{configured.scheme}://{configured.netloc}")
                if origin and origin not in expected:
                    raise FeedError("Origin not allowed", 403)
                if self.headers.get("Sec-Fetch-Site") == "cross-site":
                    raise FeedError("Cross-site request rejected", 403)
                if self.headers.get_content_type() != "application/json":
                    raise FeedError("Use application/json", 415)

        def require_admin(self):
            if not self.admin:
                raise FeedError("Log into Admin Input first.", 401)

        def require_session(self, sid):
            if not access.visible(sid, self.owner, self.admin):
                raise FeedError("Session not found", 404)

        def do_GET(self):
            self.context()
            try:
                self.validate_origin()
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                sid = query.get("session", [""])[0]
                if parsed.path == "/api/state":
                    live = access.setting("live_session")
                    self.reply({"admin": self.admin, "live_session": live, "sessions": access.sessions(self.owner, self.admin)})
                elif parsed.path == "/api/map-config":
                    self.reply(maps.get("shared"))
                elif parsed.path == "/api/airspace":
                    bounds = query.get("bounds", [""])[0].split(",")
                    kinds = query.get("layers", ["controlled,sua,tfr"])[0].split(",")
                    self.reply(airspace.query(bounds, kinds))
                elif parsed.path in ("/api/packets", "/api/raw", "/api/export"):
                    self.require_session(sid)
                    after = max(0, int(query.get("after", [0])[0]))
                    if parsed.path == "/api/packets":
                        self.reply(store.packets(sid, after))
                    elif parsed.path == "/api/raw":
                        self.reply(access.raw(sid, after))
                    else:
                        output = io.StringIO(newline="")
                        fields = ["received_at", *columns, "ascent_mps", "ground_speed_mps", "boot", "duplicate", "time_kind"]
                        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
                        writer.writeheader()
                        after = 0
                        while batch := store.packets(sid, after):
                            writer.writerows(batch)
                            after = batch[-1]["id"]
                        self.reply(output.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", extra={"Content-Disposition": 'attachment; filename="chippy-received.csv"'})
                else:
                    files = {"/": "index.html", "/app.js": "app.js", "/live.js": "live.js", "/globe.js": "globe.js", "/mission.js": "mission.js", "/geo.js": "geo.js", "/local_files.js": "local_files.js", "/style.css": "style.css", "/vendor/leaflet.js": "vendor/leaflet.js", "/vendor/leaflet.css": "vendor/leaflet.css"}
                    name = files.get(parsed.path)
                    if not name:
                        raise FeedError("Not found", 404)
                    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                    self.reply((root / "static" / name).read_bytes(), mime)
            except (FeedError, ValueError, TypeError) as exc:
                self.reply({"error": str(exc)}, status=getattr(exc, "status", 400))

        def do_POST(self):
            self.context()
            try:
                self.validate_origin(post=True)
                # Anyone may watch; only a logged-in admin may send data to the host.
                if self.path not in ("/api/login", "/api/logout"):
                    self.require_admin()
                length = int(self.headers.get("Content-Length", "0"))
                maximum = 12 * 1024 * 1024 if self.path in ("/api/import", "/api/map-config") else 512 * 1024
                if not 0 <= length <= maximum:
                    raise FeedError("Request too large", 413)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise FeedError("Expected a JSON object")
                if self.path == "/api/map-config":
                    self.reply(maps.update("shared", body))
                elif self.path == "/api/login":
                    # ponytail: CF-Connecting-IP is only trustworthy behind Cloudflare; Access.login's total cap bounds spoofing elsewhere.
                    token = access.login(body.get("password"), self.headers.get("CF-Connecting-IP") or self.client_address[0])
                    self.reply({"ok": True}, extra={"Set-Cookie": self.cookie("chippy_admin", token, 12 * 3600)})
                elif self.path == "/api/logout":
                    with access.lock:
                        access.tokens.pop(self.token, None)
                    self.reply({"ok": True}, extra={"Set-Cookie": self.cookie("chippy_admin", "", 0)})
                elif self.path == "/api/password":
                    access.change_password(self.token, body.get("current"), body.get("password"))
                    self.reply({"ok": True})
                elif self.path in ("/api/input/start", "/api/input/append"):
                    shared = body.get("shared", False)
                    if not isinstance(shared, bool):
                        raise FeedError("shared must be a boolean")
                    if self.path.endswith("/start"):
                        self.reply({"session": access.start(self.owner, shared, demo=body.get("demo") is True and not shared)})
                    else:
                        self.reply(access.append(body.get("session"), self.owner, body.get("lines"), body.get("batch"), shared, self.admin))
                elif self.path == "/api/import":
                    shared = body.get("shared", False)
                    if not isinstance(shared, bool):
                        raise FeedError("shared must be a boolean")
                    if not isinstance(body.get("csv"), str) or len(body["csv"].encode()) > 8 * 1024 * 1024:
                        raise FeedError("Choose a UTF-8 CSV up to 8 MB.")
                    sid, count = store.import_csv(body["csv"], body.get("name", "flight.csv"), owner=self.owner, shared=shared)
                    if shared:
                        access.publish(sid)
                    self.reply({"session": sid, "count": count})
                else:
                    raise FeedError("Not found", 404)
            except (FeedError, ValueError, TypeError, csv.Error) as exc:
                self.reply({"error": str(exc)}, status=getattr(exc, "status", 400))

    return Handler
