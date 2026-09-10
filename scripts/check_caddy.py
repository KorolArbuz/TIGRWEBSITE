"""Exercise the real Caddyfile against temporary loopback listeners, without TLS or production traffic."""
from __future__ import annotations
import argparse
import http.client
import http.server
import json
from pathlib import Path
import socket
# Starts only the explicitly selected local binary, with no shell; output is discarded.
import subprocess  # nosec B404
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--binary", required=True, type=Path, help="Path to a trusted Caddy 2.11.4 binary.")
args = parser.parse_args()
binary = args.binary.resolve()
if not binary.is_file():
    parser.error("Caddy binary unavailable; check NOT RUN.")

def require(condition, message):
    if not condition:
        raise RuntimeError(message)

class Upstream(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):
        if self.headers.get("Transfer-Encoding") == "chunked":
            while True:
                raw = self.rfile.readline()
                if not raw:
                    break
                size = int(raw.strip(), 16)
                if not size:
                    break
                self.rfile.read(size + 2)
        else:
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
        try:
            self.do_GET()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

with socket.socket() as selector:
    selector.bind(("127.0.0.1", 0))
    port = selector.getsockname()[1]
upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
threading.Thread(target=upstream.serve_forever, daemon=True).start()
config = (ROOT / "Caddyfile.example").read_text(encoding="utf-8")
config = config.replace("{\n", "{\n\tadmin off\n\tauto_https off\n", 1)
config = config.replace("{$DOMAIN}", f"http://127.0.0.1:{port}")
config = config.replace("shop:8000", f"127.0.0.1:{upstream.server_port}")

def request(path, size=None, chunked=False):
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
    try:
        body = b"x" * size if size else None
        if chunked:
            body = iter([body[:100], body[100:]])
        client.request("POST" if size else "GET", path, body=body, encode_chunked=chunked)
        response = client.getresponse()
        result = {"path": path, "body_bytes": size, "chunked": chunked, "status": response.status,
                  "headers": dict(response.getheaders())}
        response.read()
        return result
    finally:
        client.close()

with tempfile.TemporaryDirectory(prefix="tigr-caddy-probe-") as directory:
    path = Path(directory) / "Caddyfile"
    path.write_text(config, encoding="utf-8")
    # Explicit operator-supplied Caddy executable, fixed run arguments, local synthetic config.
    process = subprocess.Popen([str(binary), "run", "--config", str(path), "--adapter", "caddyfile"],  # nosec B603
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), cwd=directory)
    try:
        for _ in range(40):
            try:
                result = request("/healthz")
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Temporary Caddy listener did not start.")
        require(result["status"] == 200, "Synthetic upstream did not become healthy.")
        results = []
        for uri, size in (("/admin/login", 16385), ("/checkout", 65537), ("/ordinary", 65537)):
            for chunked in (False, True):
                result = request(uri, size, chunked)
                require(result["status"] == 413, "Caddy did not enforce the body budget.")
                results.append(result)
        upstream.shutdown()
        upstream.server_close()
        for uri in ("/admin", "/order/probe"):
            result = request(uri)
            require(result["status"] == 502, "Caddy did not report the unavailable synthetic upstream.")
            results.append(result)
        for result in results:
            headers = {key.lower(): value for key, value in result["headers"].items()}
            require(headers.get("cache-control") == "no-store", "Caddy error lacks no-store.")
            require(headers.get("referrer-policy") == "no-referrer", "Caddy error lacks strict referrer policy.")
            require(headers.get("x-content-type-options") == "nosniff", "Caddy error lacks nosniff.")
            require(headers.get("x-frame-options") == "DENY", "Caddy error lacks frame denial.")
            require("frame-ancestors 'none'" in headers.get("content-security-policy", ""), "Caddy error lacks CSP.")
            require("noindex" in headers.get("x-robots-tag", ""), "Caddy error lacks noindex.")
            require("server" not in headers, "Caddy error exposes a server header.")
        print(json.dumps({"checks": len(results), "status": "PASS", "caddy_config": "Caddyfile.example",
                          "scope": "temporary HTTP loopback listeners; production TLS not exercised"}))
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        upstream.server_close()
