"""Password hashing, sessions and the auth middleware.

Single-admin model: one password, stored as an scrypt hash in a file that
only root and the tarn user can read. Sessions live in memory, so a restart
logs everyone out, which is fine for a homelab box.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import urlsplit

from aiohttp import web

COOKIE = "tarn_session"
# scrypt cost parameters: ~16 MiB of memory per hash, well under a second.
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.strip().split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def read_password_hash(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip()
        return value or None
    except FileNotFoundError:
        return None


def write_password_hash(path: str, password: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(hash_password(password) + "\n")
    # The service runs as tarn:libvirt, so the file must stay readable by
    # that group even when root rewrites it with `doas tarn passwd`.
    if os.geteuid() == 0:
        try:
            st = os.stat(path)
            os.chown(tmp, st.st_uid, st.st_gid)
        except FileNotFoundError:
            try:
                import grp
                os.chown(tmp, 0, grp.getgrnam("libvirt").gr_gid)
            except KeyError:
                pass
    os.replace(tmp, path)


class Sessions:
    def __init__(self, ttl_hours: float):
        self.ttl = ttl_hours * 3600
        self._sessions: dict[str, float] = {}
        self._failures: dict[str, list[float]] = {}

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = time.time() + self.ttl
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        expires = self._sessions.get(token)
        if expires is None:
            return False
        if expires < time.time():
            self._sessions.pop(token, None)
            return False
        return True

    def drop(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

    # Simple brute-force brake: 5 failures per IP within 5 minutes locks
    # that IP out until the oldest failure ages out.
    def locked_out(self, ip: str) -> bool:
        now = time.time()
        recent = [t for t in self._failures.get(ip, []) if now - t < 300]
        self._failures[ip] = recent
        return len(recent) >= 5

    def record_failure(self, ip: str) -> None:
        self._failures.setdefault(ip, []).append(time.time())


PUBLIC_PREFIXES = ("/static/", "/api/login", "/favicon")
PUBLIC_EXACT = {"/", "/console"}


def _same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return True  # non-browser clients and same-origin GETs
    return urlsplit(origin).netloc == request.host


@web.middleware
async def auth_middleware(request: web.Request, handler):
    path = request.path
    if path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES):
        return await handler(request)

    sessions: Sessions = request.app["sessions"]
    if not sessions.valid(request.cookies.get(COOKIE)):
        raise web.HTTPUnauthorized(
            text='{"error": "not logged in"}', content_type="application/json"
        )

    # CSRF defence on top of SameSite=Strict: state-changing requests must
    # carry a header a cross-site form cannot set, and every request with an
    # Origin header (including WebSocket handshakes) must be same-origin.
    if not _same_origin(request):
        raise web.HTTPForbidden(text='{"error": "cross-origin request"}', content_type="application/json")
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("X-Tarn") != "1":
        raise web.HTTPForbidden(text='{"error": "missing X-Tarn header"}', content_type="application/json")
    return await handler(request)
