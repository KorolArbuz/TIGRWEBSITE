"""Probe the real Caddyfile in front of a real temporary Uvicorn application."""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
# Process launch below uses only fixed executables/arguments and never a shell.
import subprocess  # nosec B404
import sys
import tempfile
import time

from argon2 import PasswordHasher

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--binary", required=True, type=Path, help="Path to a trusted Caddy 2.11.4 binary.")
args = parser.parse_args()
binary = args.binary.resolve()
if not binary.is_file():
    parser.error("Caddy binary unavailable; check NOT RUN.")


def free_port() -> int:
    with socket.socket() as selector:
        selector.bind(("127.0.0.1", 0))
        return int(selector.getsockname()[1])


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def request(port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        client.request("GET", path)
        response = client.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        client.close()


def slow_request(port: int, prefix: bytes, pause: float) -> tuple[bytes, bool]:
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.settimeout(5)
        client.sendall(prefix)
        time.sleep(pause)
        chunks = []
        while True:
            try:
                chunk = client.recv(65536)
            except TimeoutError:
                return b"".join(chunks), False
            except ConnectionResetError:
                return b"".join(chunks), True
            if not chunk:
                return b"".join(chunks), True
            chunks.append(chunk)
            if b"\r\n\r\n" in b"".join(chunks):
                return b"".join(chunks), False


caddy_port, uvicorn_port = free_port(), free_port()
while uvicorn_port == caddy_port:
    uvicorn_port = free_port()
with tempfile.TemporaryDirectory(prefix="tigr-caddy-uvicorn-") as directory_name:
    directory = Path(directory_name)
    environment = os.environ.copy()
    environment.update({
        "APP_ENV": "test",
        "SECRET_KEY": secrets.token_urlsafe(48),
        "ADMIN_PASSWORD_HASH": PasswordHasher(memory_cost=19456, time_cost=2, parallelism=1).hash(
            secrets.token_urlsafe(32)
        ),
        "SESSION_COOKIE_SECURE": "false",
        "ALLOWED_HOSTS": "127.0.0.1,localhost",
        "DATABASE_PATH": str(directory / "shop.sqlite3"),
        "UPLOAD_ROOT": str(directory / "media"),
        "IMPORT_ARCHIVE": str(directory / "imports"),
        "FORM_BODY_IDLE_SECONDS": "0.25",
        "FORM_BODY_TOTAL_SECONDS": "2",
    })
    config = (ROOT / "Caddyfile.example").read_text(encoding="utf-8")
    config = config.replace("{\n", "{\n\tadmin off\n\tauto_https off\n", 1)
    config = config.replace("read_header 10s", "read_header 250ms")
    config = config.replace("{$DOMAIN}", f"http://127.0.0.1:{caddy_port}")
    config = config.replace("shop:8000", f"127.0.0.1:{uvicorn_port}")
    config_path = directory / "Caddyfile"
    config_path.write_text(config, encoding="utf-8")

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    uvicorn = subprocess.Popen(  # nosec B603
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(uvicorn_port),
         "--no-proxy-headers", "--no-access-log"],
        cwd=ROOT, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )
    caddy = subprocess.Popen(  # nosec B603
        [str(binary), "run", "--config", str(config_path), "--adapter", "caddyfile"],
        cwd=directory, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creation_flags,
    )
    try:
        health = None
        for _ in range(60):
            try:
                health = request(caddy_port, "/healthz")
                if health[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.1)
        require(health is not None and health[0] == 200 and b'"ok"' in health[2],
                "Caddy did not reach the temporary Uvicorn application")

        header_response, header_closed = slow_request(
            caddy_port, b"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1", 0.5,
        )
        # Go may close an incomplete-header connection without emitting a body.
        require(b" 408 " in header_response or header_closed, "Caddy read_header timeout was not enforced")

        print(json.dumps({"status": "PASS", "health": 200, "slow_header": "408-or-close",
                          "scope": "temporary loopback Caddy 2.11.4 -> Uvicorn"}))
    finally:
        for process in (caddy, uvicorn):
            process.terminate()
        for process in (caddy, uvicorn):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
