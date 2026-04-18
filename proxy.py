"""Floxy residential proxy configuration.

Each call to `new_proxy_config()` returns a fresh config pinned to a new
floxy session id → new residential exit IP (held for FLOXY_SESSION_LIFETIME
seconds).

Floxy proxy-string format:
    <user>:<pass>_session-<id>_lifetime-<sec>@<host>:<port>

The session id lives in the password, not the username.
"""

from __future__ import annotations

import os
import secrets
import string
from dataclasses import dataclass

_SESSION_ALPHABET = string.ascii_lowercase + string.digits


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def new_session_id() -> str:
    """Match floxy's visible format: 8 lowercase alphanumeric chars."""
    return "".join(secrets.choice(_SESSION_ALPHABET) for _ in range(8))


@dataclass(frozen=True)
class ProxyConfig:
    server: str       # e.g. "http://residential.floxy.io:12321"
    username: str
    password: str
    session_id: str

    def as_playwright(self) -> dict[str, str]:
        return {
            "server": self.server,
            "username": self.username,
            "password": self.password,
        }


def new_proxy_config(session_id: str | None = None) -> ProxyConfig:
    session_id = session_id or new_session_id()
    host = _env("FLOXY_HOST")
    port = _env("FLOXY_PORT")
    user = _env("FLOXY_USER")
    base_pass = _env("FLOXY_PASS")
    lifetime = os.environ.get("FLOXY_SESSION_LIFETIME", "1200")
    password = f"{base_pass}_session-{session_id}_lifetime-{lifetime}"
    return ProxyConfig(
        server=f"http://{host}:{port}",
        username=user,
        password=password,
        session_id=session_id,
    )
