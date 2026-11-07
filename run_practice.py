"""Serve a frozen local practice copy. Never calls Alpaca or an AI provider."""
import argparse
from datetime import datetime, timezone
from functools import partial
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
SNAPSHOT = ROOT / "data/practice/snapshot.json"
ASSETS = ROOT / "frontend/public"
CATEGORIES = {"Finance", "Politics & Policy", "Macro", "Other"}
SENTIMENTS = {"positive", "negative", "neutral", "unclear"}
BANNER_CSS = ".practice-banner{background:#fff3d9;border-bottom:2px solid #dfb663;padding:16px 24px;color:#66440b;font:13px/1.7 system-ui,sans-serif}.practice-banner strong{display:block;font-size:15px}.practice-banner p{margin:4px 0 0;max-width:1200px}"


def validate_snapshot(value):
    if (not isinstance(value, dict) or value.get("schema") != 1 or value.get("kind") != "saved-live-snapshot"
            or not isinstance(value.get("captured_at"), str) or not isinstance(value.get("status"), dict)
            or not isinstance(value.get("bars"), dict) or any(not isinstance(value.get(k), list) for k in ("quotes", "news", "alerts"))):
        raise ValueError("Practice snapshot is invalid")
    if value["status"].get("worker_running"):
        raise ValueError("Snapshot must be captured with the real monitor paused")
    return value


def export_snapshot(db_path, destination=SNAPSHOT):
    # TestClient invokes only the read-only ASGI app in this process: no network,
    # provider clients, worker startup, .env loading or AI request is involved.
    from fastapi.testclient import TestClient
    from newslens.api import create_app
    from newslens.config import load_settings
    token = "practice-export-in-process-only"
    app = create_app(db_path, token, load_settings(ROOT / "config.toml").symbols)
    with TestClient(app, headers={"Authorization": "Bearer " + token}) as client:
        def read(path):
            response = client.get(path)
            response.raise_for_status()
            return response.json()
        status = read("/api/status")["data"]
        if not status.get("data_available"):
            raise ValueError("No saved live records are available")
        if status.get("worker_running"):
            raise ValueError("Pause Start_Monitor before refreshing the practice snapshot")
        def all_rows(resource):
            rows = []
            for offset in range(0, 10001, 100):
                page = read(f"/api/{resource}?limit=100&offset={offset}")
                rows.extend(page["data"])
                if len(rows) >= page["meta"]["total"]:
                    return rows
            raise ValueError("Snapshot exceeds the supported record limit")
        quotes = read("/api/quotes")["data"]
        bars = {q["symbol"]: read("/api/bars?symbol=" + q["symbol"] + "&limit=500")["data"] for q in quotes}
        value = validate_snapshot({"schema": 1, "kind": "saved-live-snapshot",
            "captured_at": datetime.now(timezone.utc).isoformat(), "status": status,
            "quotes": quotes, "bars": bars, "news": all_rows("news"), "alerts": all_rows("alerts")})
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        backup = ROOT / "_integration_backups" / ("practice_snapshot_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        backup.mkdir(parents=True)
        shutil.copy2(destination, backup / "snapshot.json")
    temp = destination.with_suffix(".new")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=False)
        stream.write("\n")
    os.replace(temp, destination)
    return value


def practice_response(snapshot, request_path):
    parsed = urlsplit(request_path)
    resource = parsed.path.removeprefix("/api/")
    allowed = {"status": set(), "quotes": set(), "bars": {"symbol", "limit"},
               "news": {"symbol", "category", "sentiment", "limit", "offset"},
               "alerts": {"symbol", "limit", "offset"}}
    if not parsed.path.startswith("/api/") or resource not in allowed:
        raise KeyError("Unknown practice endpoint")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) - allowed[resource] or any(len(v) != 1 for v in query.values()):
        raise ValueError("Invalid query parameters")
    def parameter(key, default=None):
        return query.get(key, [default])[0]
    symbol, category, sentiment = parameter("symbol"), parameter("category"), parameter("sentiment")
    if symbol is not None and not re.fullmatch(r"[A-Z][A-Z.]{0,9}", symbol):
        raise ValueError("Invalid stock")
    if category is not None and category not in CATEGORIES or sentiment is not None and sentiment not in SENTIMENTS:
        raise ValueError("Invalid filter")
    limit = int(parameter("limit", 120 if resource == "bars" else 50 if resource == "alerts" else 20))
    offset = int(parameter("offset", 0))
    if not 1 <= limit <= (500 if resource == "bars" else 100) or not 0 <= offset <= 10000:
        raise ValueError("Invalid pagination")
    meta = {"data_mode": "live", "generated_at": snapshot["captured_at"],
            "practice": True, "snapshot_at": snapshot["captured_at"]}
    # data_mode describes the original saved observations, not a running feed.
    # The persistent bilingual page banner identifies every response as a snapshot.
    if resource == "status":
        data = snapshot["status"]
    elif resource == "quotes":
        data = snapshot["quotes"]
        meta["total"] = len(data)
    elif resource == "bars":
        if not symbol:
            raise ValueError("Choose a stock")
        data = snapshot["bars"].get(symbol, [])[-limit:]
        meta.update(total=len(data), symbol=symbol)
    elif resource == "news":
        data = []
        for row in snapshot["news"]:
            analysis = row.get("analysis") or {}
            if symbol and symbol not in row.get("symbols", []):
                continue
            if category and category not in analysis.get("categories", []):
                continue
            if sentiment and not any(i.get("sentiment") == sentiment and (not symbol or i.get("symbol") == symbol) for i in analysis.get("impacts", [])):
                continue
            data.append(row)
        meta.update(total=len(data), limit=limit, offset=offset)
        data = data[offset:offset + limit]
    else:
        data = [row for row in snapshot["alerts"] if not symbol or row.get("symbol") == symbol]
        meta.update(total=len(data), limit=limit, offset=offset)
        data = data[offset:offset + limit]
    return {"data": data, "meta": meta}


class PracticeHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, snapshot, assets=ASSETS, **kwargs):
        self.snapshot, self.assets = snapshot, Path(assets)
        super().__init__(*args, **kwargs)

    def respond(self, code, content, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            if path.startswith("/api/"):
                content = json.dumps(practice_response(self.snapshot, self.path), ensure_ascii=False).encode()
                return self.respond(200, content, "application/json; charset=utf-8")
            if path == "/practice.css":
                return self.respond(200, BANNER_CSS.encode(), "text/css; charset=utf-8")
            files = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}
            if path not in files:
                raise KeyError("Not available")
            filename = files[path]
            content = (self.assets / filename).read_bytes()
            if filename == "index.html":
                captured = escape(self.snapshot["captured_at"])
                banner = ('<aside class="practice-banner" role="status"><strong>OFFLINE PRACTICE / 离线练习</strong>'
                    '<p>Saved local results / 本地历史快照: ' + captured + '</p>'
                    '<p>No Alpaca or HKBU requests. No new AI analysis or notifications. / 不调用行情或学校 API，不生成新分析或发送通知。</p>'
                    '<p>These are previously collected results, not a live feed. / 以下为已保存结果，并非实时行情。</p></aside>')
                text = content.decode().replace('</head>', '<link rel="stylesheet" href="/practice.css"></head>')
                text = text.replace('</header>', '</header>' + banner, 1)
                text = text.replace('<span data-i18n="readOnly">Public · read only</span>', '<span>Offline practice / 离线练习</span>')
                content = text.encode()
            kind = "text/javascript" if filename.endswith(".js") else mimetypes.guess_type(filename)[0] or "text/plain"
            self.respond(200, content, kind + "; charset=utf-8")
        except KeyError:
            self.respond(404, b'{"error":"Not available in offline practice"}', "application/json")
        except (ValueError, TypeError):
            self.respond(400, b'{"error":"Invalid practice query"}', "application/json")

    do_HEAD = do_GET

    def do_POST(self):
        self.respond(405, b'{"error":"Practice is read only"}', "application/json")

    do_PUT = do_PATCH = do_DELETE = do_POST

    def log_message(self, *_):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-snapshot", action="store_true", help="Read saved database records again; real monitor must be paused")
    parser.add_argument("--check", action="store_true", help="Validate the existing snapshot without starting a server")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Use a port between 1024 and 65535")
    try:
        if args.check:
            snapshot = validate_snapshot(json.loads(SNAPSHOT.read_text()))
        elif args.refresh_snapshot or not SNAPSHOT.exists():
            snapshot = export_snapshot(ROOT / "data/live.sqlite")
        else:
            snapshot = validate_snapshot(json.loads(SNAPSHOT.read_text()))
    except (OSError, ValueError, RuntimeError) as exc:
        print("Practice setup needs attention:", str(exc))
        return 2
    print("OFFLINE PRACTICE | Saved snapshot:", snapshot["captured_at"], flush=True)
    print("No Alpaca/HKBU requests; no monitor, AI analysis or message delivery is started.", flush=True)
    if args.check:
        print("Snapshot OK:", len(snapshot["quotes"]), "stocks;", len(snapshot["news"]), "articles;", len(snapshot["alerts"]), "alerts")
        return 0
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(PracticeHandler, snapshot=snapshot))
    except OSError:
        print("Practice port is already in use. Close the earlier practice window or choose --port.")
        return 2
    print(f"Open http://127.0.0.1:{args.port}/ | Ctrl+C stops practice", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
