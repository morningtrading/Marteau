"""`jevloop serve-hyperliquid`: a tiny static file server for Marteau's
Hyperliquid dashboard.

Same shape as serve.py, deliberately kept separate: this one serves
hyperliquid-log.jsonl / hyperliquid-latest.json (loop_hyperliquid.py's own
files, never loop.py's) at hyperliquid.html, on its own default port so it
never collides with `jev-loop serve`'s Alpaca dashboard on 8765. Read-only:
loop_hyperliquid.py has no standby/control channel to write to yet, so
there is no POST /control here.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import socketserver
import time
from pathlib import Path

LOG_DIR = Path(os.environ.get("JEV_LOOP_HOME", str(Path.home() / ".jev-loop")))
LOG_FILE = LOG_DIR / "hyperliquid-log.jsonl"
LATEST_FILE = LOG_DIR / "hyperliquid-latest.json"
SKILL_DIR = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = SKILL_DIR / "dashboard"


_last_good: dict[str, bytes] = {}


def read_feed(path: Path, attempts: int = 5, wait_s: float = 0.02) -> bytes | None:
    """Same read-then-retry-then-last-good-copy discipline as serve.py's
    read_feed, so a dashboard poll never sees a half-written JSON file."""
    for _ in range(attempts):
        try:
            data = path.read_bytes()
            if path.suffix == ".json":
                json.loads(data)
            _last_good[path.name] = data
            return data
        except FileNotFoundError:
            break
        except (PermissionError, ValueError):
            time.sleep(wait_s)
    return _last_good.get(path.name)


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path not in ("/latest.json", "/log.jsonl"):
            return super().do_GET()
        real = LATEST_FILE if path == "/latest.json" else LOG_FILE
        data = read_feed(real)
        if data is None:
            data = b'{"ticks": [], "stats": {}}' if path == "/latest.json" else b""
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/json" if path == "/latest.json" else "text/plain",
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def translate_path(self, path: str) -> str:
        path = path.split("?", 1)[0].split("#", 1)[0]
        if path in ("/latest.json", "/log.jsonl"):
            return str(LATEST_FILE if path == "/latest.json" else LOG_FILE)
        if path == "/":
            path = "/hyperliquid.html"
        candidate = DASHBOARD_DIR / path.lstrip("/")
        return str(candidate)

    def log_message(self, format, *args):  # noqa: A002
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jev-loop serve-hyperliquid")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args(argv)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    if not LATEST_FILE.exists():
        LATEST_FILE.write_text('{"ticks": [], "stats": {}}')

    with socketserver.TCPServer(("127.0.0.1", args.port), Handler) as httpd:
        print(f"dashboard: http://127.0.0.1:{args.port}/hyperliquid.html")
        print(f"raw feed:  http://127.0.0.1:{args.port}/latest.json")
        print("Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
