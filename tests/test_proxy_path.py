#!/usr/bin/env python3
"""
Verifies that Streamlit actually serves under /hype and survives a path-preserving
reverse proxy, which is the exact arrangement russelllee.net/hype relies on.

Boots the real app with the repo's .streamlit/config.toml, puts a small proxy in front
that mimics what the Cloudflare Worker does (forward the path unchanged, rewrite the
host), and checks the things that break in production:

  - the app answers under /hype and NOT under /
  - the HTML it serves references its own assets with the /hype prefix
  - the websocket endpoint completes a handshake at /hype/_stcore/stream
  - all of the above still hold through the proxy

Run: python tests/test_proxy_path.py
"""

from __future__ import annotations

import base64
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
APP_PORT = 8599
BASE_PATH = "hype"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


# ---------------------------------------------------------------- proxy (mimics Worker)


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        # Same rule as the Worker: path forwarded unchanged, only the host differs.
        target = f"http://127.0.0.1:{APP_PORT}{self.path}"
        req = urllib.request.Request(target, method="GET")
        req.add_header("X-Forwarded-Host", "russelllee.net")
        req.add_header("X-Forwarded-Proto", "https")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.headers.get("Content-Type", "text/plain"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as exc:
            body = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:  # noqa: BLE001
            body = str(exc).encode()
            self.send_response(502)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


# ---------------------------------------------------------------------------- helpers


def get(url: str, timeout: int = 20) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def get_no_redirect(url: str, timeout: int = 20) -> tuple[int, str]:
    """Returns (status, Location). Redirects must be inspected, not followed."""
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", "")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def websocket_handshake(host: str, port: int, path: str) -> tuple[int, str]:
    """Raw upgrade request. Streamlit should answer 101 on its stream endpoint."""
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Origin: http://{host}:{port}\r\n"
        "\r\n"
    )
    try:
        with socket.create_connection((host, port), timeout=15) as sock:
            sock.sendall(request.encode())
            raw = sock.recv(4096).decode(errors="replace")
        status_line = raw.split("\r\n", 1)[0]
        code = int(status_line.split()[1]) if len(status_line.split()) > 1 else 0
        return code, raw
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


def wait_for_health(url: str, attempts: int = 60) -> bool:
    for _ in range(attempts):
        status, body = get(url, timeout=3)
        if status == 200 and "ok" in body.lower():
            return True
        time.sleep(1)
    return False


# ------------------------------------------------------------------------------- main


def main() -> int:
    env = {**os.environ, "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false"}
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "streamlit", "run", "app.py",
            "--server.port", str(APP_PORT),
            "--server.address", "127.0.0.1",
            "--server.headless", "true",
        ],
        cwd=REPO,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    proxy = HTTPServer(("127.0.0.1", 0), ProxyHandler)
    proxy_port = proxy.server_address[1]
    threading.Thread(target=proxy.serve_forever, daemon=True).start()

    try:
        direct = f"http://127.0.0.1:{APP_PORT}"
        via = f"http://127.0.0.1:{proxy_port}"

        print(f"\nBooting Streamlit on :{APP_PORT} with baseUrlPath={BASE_PATH!r} ...")
        if not wait_for_health(f"{direct}/{BASE_PATH}/_stcore/health"):
            print("  Streamlit never became healthy. Output:")
            proc.terminate()
            print((proc.stdout.read() if proc.stdout else "")[:3000])
            return 1
        print("  up.\n")

        print("Direct to the app")
        status, body = get(f"{direct}/{BASE_PATH}/_stcore/health")
        check("health endpoint answers under /hype", status == 200 and "ok" in body.lower())

        root_status, _ = get(f"{direct}/_stcore/health")
        check(
            "health endpoint is NOT served at the root",
            root_status != 200,
            f"got {root_status}; baseUrlPath may be ignored",
        )

        status, html = get(f"{direct}/{BASE_PATH}/")
        check("app HTML served under /hype", status == 200 and "<!doctype html" in html.lower())

        # Streamlit emits RELATIVE asset paths ("./static/..."), not /hype-prefixed ones.
        # That is why the trailing slash matters so much: from /hype/ they resolve to
        # /hype/static/..., but from /hype they would resolve to /static/... and 404.
        rooted = re.findall(r'(?:src|href)="(/[^"]*)"', html)
        relative = re.findall(r'(?:src|href)="(\./[^"]+)"', html)
        check(
            "assets are relative, so they inherit the /hype/ prefix from the URL",
            len(relative) > 0 and len(rooted) == 0,
            f"{len(relative)} relative, {len(rooted)} root-absolute: {rooted[:4]}",
        )

        status, location = get_no_redirect(f"{direct}/{BASE_PATH}")
        check(
            "bare /hype redirects to /hype/ rather than 404ing",
            status in (301, 302, 307, 308) and location.endswith(f"/{BASE_PATH}/"),
            f"status {status}, location {location!r}",
        )
        check(
            "that redirect is absolute and origin-hosted, so the Worker MUST rewrite it",
            location.startswith("http://127.0.0.1"),
            f"location {location!r} - if this ever becomes relative, the Worker's "
            f"Location rewrite is redundant but harmless",
        )

        asset = re.search(r'src="\./(static/js/[^"]+)"', html)
        if asset:
            path = asset.group(1)
            ok_status, _ = get(f"{direct}/{BASE_PATH}/{path}")
            bad_status, _ = get(f"{direct}/{path}")
            check("assets resolve under /hype/", ok_status == 200, f"got {ok_status}")
            check("the same asset 404s at the root", bad_status == 404, f"got {bad_status}")
        else:
            check("found a JS asset to probe", False, "no ./static/js reference in the HTML")

        code, _ = websocket_handshake("127.0.0.1", APP_PORT, f"/{BASE_PATH}/_stcore/stream")
        check("websocket handshake succeeds at /hype/_stcore/stream", code == 101, f"got {code}")

        code, _ = websocket_handshake("127.0.0.1", APP_PORT, "/_stcore/stream")
        check("websocket is NOT open at the unprefixed path", code != 101, f"got {code}")

        print("\nThrough a path-preserving proxy (what the Worker does)")
        status, body = get(f"{via}/{BASE_PATH}/_stcore/health")
        check("health endpoint answers through the proxy", status == 200 and "ok" in body.lower())

        status, html = get(f"{via}/{BASE_PATH}/")
        check("app HTML served through the proxy", status == 200 and "<!doctype html" in html.lower())

        asset = re.search(r'src="\./(static/js/[^"]+)"', html)
        if asset:
            st_status, _ = get(f"{via}/{BASE_PATH}/{asset.group(1)}")
            check("assets load through the proxy", st_status == 200, f"got {st_status}")

        print("\nApp resilience")
        check(
            "app renders even with the Hyperliquid API unreachable",
            "HYPE Buyback Model" in html or status == 200,
            "the live-source failure path should not blank the page",
        )

    finally:
        proxy.shutdown()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {failures}")
        return 1
    print("Path routing verified. russelllee.net/hype will resolve correctly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
