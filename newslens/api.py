"""Authenticated, read-only public projection of the live NewsLens database."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hmac
import json
import os
from pathlib import Path
import sqlite3
from typing import Annotated, Literal

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

ROOT = Path(__file__).resolve().parents[1]
SYMBOL = r"^[A-Z][A-Z.]{0,9}$"
LATEST = """WITH ranked AS (
 SELECT id,hash,payload,analysis,updated,
 ROW_NUMBER() OVER (PARTITION BY id ORDER BY updated DESC,rowid DESC) AS position
 FROM news_versions
), latest AS (SELECT * FROM ranked WHERE position=1) """


class DataUnavailable(Exception):
    pass


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def error_response(code, message, status):
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status,
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def envelope(data, **metadata):
    return {"data": data, "meta": {"generated_at": timestamp(), "data_mode": "live", **metadata}}


def select_fields(payload, names):
    return {key: payload[key] for key in names if key in payload}


def public_analysis(raw):
    if raw is None:
        return None
    result = select_fields(raw, ("status", "categories", "summary", "model", "prompt_version", "analyzed_at"))
    result["impacts"] = [select_fields(impact, ("symbol", "sentiment", "evidence", "reason"))
                         for impact in raw.get("impacts", []) if isinstance(impact, dict)]
    # Stored exception/configuration details are never exposed by the public API.
    result["error"] = ("Analysis could not be completed" if raw.get("status") == "error"
                       else "Insufficient source content" if raw.get("status") == "insufficient" else "")
    return result


def public_news(row):
    result = select_fields(json.loads(row["payload"]),
                           ("id", "headline", "summary", "symbols", "created_at", "updated_at", "received_at", "source", "url", "data_mode"))
    result.update(id=row["id"], hash=row["hash"], analysis=public_analysis(json.loads(row["analysis"]) if row["analysis"] else None))
    return result


@contextmanager
def live_database(path):
    if not path.is_file():
        raise DataUnavailable
    connection = None
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        marker = connection.execute("SELECT payload FROM state WHERE key='data_mode'").fetchone()
        if not marker or json.loads(marker[0]) != "live":
            raise DataUnavailable
        yield connection
    except (sqlite3.Error, ValueError, TypeError, KeyError) as exc:
        raise DataUnavailable from exc
    finally:
        if connection is not None:
            connection.close()


def state(connection, key, default=None):
    row = connection.execute("SELECT payload FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def worker_running(lease, seen):
    try:
        dt = datetime.fromisoformat(seen.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return False
        now = datetime.now(timezone.utc)
        return lease.get("expires", 0) > now.timestamp() and 0 <= (now - dt).total_seconds() <= 90
    except (AttributeError, ValueError, TypeError):
        return False


def create_app(db_path=None, api_token=None, symbols=None):
    path = Path(db_path) if db_path is not None else ROOT / "data" / "live.sqlite"
    token = os.getenv("NEWSLENS_API_TOKEN", "") if api_token is None else api_token
    configured_symbols = list(symbols or ["AAPL", "MSFT", "NVDA", "AMD", "JPM", "BAC", "XOM", "CVX", "JNJ", "UNH"])
    app = FastAPI(title="NewsLens read-only API", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if not token:
            return error_response("api_not_configured", "API authentication is not configured", 503)
        provided = request.headers.get("authorization", "")
        if not provided.startswith("Bearer ") or len(provided) > 4096 or not hmac.compare_digest(provided[7:].encode(), str(token).encode()):
            return error_response("unauthorized", "Authentication required", 401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_query(request, exc):
        return error_response("invalid_request", "Invalid query parameters", 422)

    @app.exception_handler(DataUnavailable)
    async def missing_data(request, exc):
        return error_response("data_unavailable", "Live data is not available", 503)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error_response("not_found" if exc.status_code == 404 else "method_not_allowed", "Endpoint or method is not available", exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return error_response("server_error", "Unable to read monitoring data", 500)

    @app.get("/api/status")
    def status():
        result = {"data_available": False, "worker_running": False, "worker_last_seen": None,
                  "worker_heartbeat": None, "quotes_last_success": None, "news_last_success": None,
                  "bars_last_success": None, "counts": {}, "news_total": 0,
                  "analysis_counts": {key: 0 for key in ("pending", "success", "insufficient", "error")},
                  "symbols": configured_symbols}
        try:
            with live_database(path) as connection:
                for key in ("worker_last_seen", "worker_heartbeat", "quotes_last_success", "news_last_success", "bars_last_success"):
                    result[key] = state(connection, key)
                result["worker_running"] = worker_running(state(connection, "worker_lease", {}), result["worker_last_seen"])
                result["counts"] = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                                    for table in ("quotes", "bars", "news_versions", "rules", "alerts")}
                for row in connection.execute(LATEST + "SELECT COALESCE(json_extract(analysis,'$.status'),'pending') AS status, COUNT(*) AS total FROM latest GROUP BY status"):
                    result["analysis_counts"][row["status"]] = row["total"]
                    result["news_total"] += row["total"]
                result["data_available"] = True
        except DataUnavailable:
            pass
        return envelope(result)

    @app.get("/api/quotes")
    def quotes():
        with live_database(path) as connection:
            rows = connection.execute("SELECT payload FROM quotes q WHERE time=(SELECT MAX(time) FROM quotes WHERE symbol=q.symbol) ORDER BY symbol")
            result = [select_fields(json.loads(row[0]), ("symbol", "price", "previous_close", "source_time", "received_at", "market_open", "clock_status", "feed", "data_mode")) for row in rows]
        return envelope(result, total=len(result))

    @app.get("/api/bars")
    def bars(symbol: Annotated[str, Query(pattern=SYMBOL)], limit: Annotated[int, Query(ge=1, le=500)] = 120):
        with live_database(path) as connection:
            rows = list(connection.execute("SELECT payload FROM bars WHERE symbol=? ORDER BY time DESC LIMIT ?", (symbol, limit)))
            result = [select_fields(json.loads(row[0]), ("symbol", "time", "close", "feed", "data_mode")) for row in reversed(rows)]
        return envelope(result, total=len(result), symbol=symbol)

    @app.get("/api/news")
    def news(symbol: Annotated[str | None, Query(pattern=SYMBOL)] = None,
             category: Literal["Finance", "Politics & Policy", "Macro", "Other"] | None = None,
             sentiment: Literal["positive", "negative", "neutral", "unclear"] | None = None,
             limit: Annotated[int, Query(ge=1, le=100)] = 20, offset: Annotated[int, Query(ge=0, le=10000)] = 0):
        filters, parameters = [], []
        if symbol:
            filters.append("EXISTS (SELECT 1 FROM json_each(latest.payload,'$.symbols') WHERE value=?)")
            parameters.append(symbol)
        if category:
            filters.append("EXISTS (SELECT 1 FROM json_each(latest.analysis,'$.categories') WHERE value=?)")
            parameters.append(category)
        if sentiment:
            filters.append("EXISTS (SELECT 1 FROM json_each(latest.analysis,'$.impacts') WHERE json_extract(value,'$.sentiment')=?" + (" AND json_extract(value,'$.symbol')=?" if symbol else "") + ")")
            parameters.append(sentiment)
            if symbol: parameters.append(symbol)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with live_database(path) as connection:
            total = connection.execute(LATEST + "SELECT COUNT(*) FROM latest" + where, parameters).fetchone()[0]
            rows = connection.execute(LATEST + "SELECT * FROM latest" + where + " ORDER BY updated DESC,id DESC LIMIT ? OFFSET ?", [*parameters, limit, offset])
            result = [public_news(row) for row in rows]
        return envelope(result, total=total, limit=limit, offset=offset)

    @app.get("/api/alerts")
    def alerts(symbol: Annotated[str | None, Query(pattern=SYMBOL)] = None,
               limit: Annotated[int, Query(ge=1, le=100)] = 50, offset: Annotated[int, Query(ge=0, le=10000)] = 0):
        where = " WHERE json_extract(payload,'$.symbol')=?" if symbol else ""
        parameters = [symbol] if symbol else []
        with live_database(path) as connection:
            total = connection.execute("SELECT COUNT(*) FROM alerts" + where, parameters).fetchone()[0]
            rows = connection.execute("SELECT payload,delivery_status FROM alerts" + where + " ORDER BY created DESC,event_id DESC LIMIT ? OFFSET ?", [*parameters, limit, offset])
            result = []
            for row in rows:
                payload = select_fields(json.loads(row["payload"]), ("event_id", "created_at", "symbol", "kind", "price", "reason", "data_mode", "feed"))
                payload["delivery_status"] = row["delivery_status"]
                result.append(payload)
        return envelope(result, total=total, limit=limit, offset=offset)

    return app


app = create_app()
