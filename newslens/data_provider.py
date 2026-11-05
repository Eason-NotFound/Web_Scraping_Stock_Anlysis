"""Bounded REST access to Alpaca; no network access occurs at import time.

The public methods return plain dictionaries. A transport can be injected for
offline tests: ``transport(url, headers, body=None, timeout=15) -> JSON value``.
Provider errors intentionally exclude URLs, response bodies and credentials.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
import json
import math
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

UTC = timezone.utc
NY = ZoneInfo("America/New_York")


class ProviderError(RuntimeError):
    """Sanitized data-provider failure; safe to show in an app log."""


def utc_now():
    return datetime.now(UTC)


def iso(value):
    """Normalize a timezone-aware timestamp, never assuming a local timezone."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("A timezone-aware timestamp is required")
    return value.astimezone(UTC).isoformat()


def positive(value):
    if isinstance(value, bool):
        raise ValueError("Boolean is not a price")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("Price must be finite and positive")
    return number


def json_transport(url, headers, body=None, timeout=15):
    request = Request(url, headers=headers, data=body)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ProviderError("Response exceeded the allowed size")
        return json.loads(raw)
    except HTTPError as exc:
        raise ProviderError(f"API returned HTTP {exc.code}") from None
    except (URLError, TimeoutError, OSError):
        raise ProviderError("API connection failed or timed out") from None
    except (ValueError, UnicodeError):
        raise ProviderError("API returned invalid JSON") from None


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(value):
    parser = _PlainText()
    parser.feed(str(value or ""))
    return re.sub(r"\s+", " ", unescape(" ".join(parser.parts))).strip()


class AlpacaProvider:
    DATA_URL = "https://data.alpaca.markets"
    CLOCK_URL = "https://paper-api.alpaca.markets"
    MAX_PAGES = 10

    def __init__(self, api_key, secret, feed="iex", transport=None, now=None):
        if feed not in {"iex", "sip"}:
            raise ValueError("Use an explicit iex or sip feed")
        self.api_key = api_key
        self.secret = secret
        self.feed = feed
        self.transport = transport or json_transport
        self.now = now or utc_now

    def _get(self, base, path, parameters=None):
        if not self.api_key or not self.secret:
            raise ProviderError("Alpaca credentials are not configured")
        url = base + path
        if parameters:
            url += "?" + urlencode({k: v for k, v in parameters.items() if v is not None})
        headers = {"APCA-API-KEY-ID": self.api_key,
                   "APCA-API-SECRET-KEY": self.secret, "Accept": "application/json"}
        try:
            return self.transport(url, headers, timeout=15)
        except ProviderError:
            raise
        except Exception:
            # A custom transport must not accidentally expose its URL or secrets.
            raise ProviderError("API request failed") from None

    def _clock(self):
        clock = self._get(self.CLOCK_URL, "/v2/clock")
        if not isinstance(clock, dict) or not isinstance(clock.get("is_open"), bool):
            raise ProviderError("Market clock response is invalid")
        return clock["is_open"]

    def snapshots(self, symbols):
        symbols = list(dict.fromkeys(symbols))
        if not symbols:
            return []
        # A trustworthy clock handles holidays, half days and daylight saving.
        # Fail closed instead of guessing whether the market is open.
        try:
            market_open = self._clock()
        except ProviderError:
            market_open = False
            clock_status = "unavailable"
        else:
            clock_status = "verified"
        payload = self._get(self.DATA_URL, "/v2/stocks/snapshots",
                            {"symbols": ",".join(symbols), "feed": self.feed})
        if not isinstance(payload, dict):
            raise ProviderError("Snapshot response is invalid")
        received = iso(self.now())
        quotes = []
        for symbol in symbols:
            row = payload.get(symbol)
            if not isinstance(row, dict):
                continue
            trade, previous = row.get("latestTrade") or {}, row.get("prevDailyBar") or {}
            try:
                quote = {"symbol": symbol, "price": positive(trade.get("p")),
                         "previous_close": positive(previous.get("c")),
                         "source_time": iso(trade.get("t")), "received_at": received,
                         "feed": self.feed, "market_open": market_open,
                         "clock_status": clock_status, "data_mode": "live"}
            except (ValueError, TypeError, OverflowError):
                continue
            quotes.append(quote)
        return quotes

    def news(self, symbols, since):
        allowed = set(symbols)
        since = iso(since)
        params = {"symbols": ",".join(symbols), "start": since,
                  "sort": "asc", "limit": 50, "include_content": "false"}
        rows, seen_tokens = {}, set()
        for _ in range(self.MAX_PAGES):
            result = self._get(self.DATA_URL, "/v1beta1/news", params)
            if not isinstance(result, dict) or not isinstance(result.get("news"), list):
                raise ProviderError("News response is invalid")
            # A later page can contain revisions published while earlier pages
            # were downloading. Availability is the response time of THIS page.
            received = iso(self.now())
            received_time = datetime.fromisoformat(received)
            for item in result["news"]:
                try:
                    headline = plain_text(item.get("headline"))
                    if not headline or item.get("id") is None:
                        continue
                    created = iso(item["created_at"])
                    updated = iso(item.get("updated_at") or item["created_at"])
                    created_time, updated_time = datetime.fromisoformat(created), datetime.fromisoformat(updated)
                    if created_time > received_time or updated_time > received_time or updated_time < created_time:
                        # Do not persist a future version with an earlier immutable
                        # first-seen time. A later poll can accept it once available.
                        continue
                    url = str(item.get("url") or "")
                    if urlparse(url).scheme not in {"http", "https"}:
                        url = ""
                    row = {"id": str(item["id"]), "headline": headline,
                           "summary": plain_text(item.get("summary")), "url": url,
                           "symbols": sorted(set(item.get("symbols") or []) & allowed),
                           "created_at": created, "updated_at": updated,
                           "received_at": received, "source": str(item.get("source") or "Alpaca news"),
                           "data_mode": "live"}
                    # Overlapping pagination can repeat an ID; retain latest update.
                    old = rows.get(row["id"])
                    if old is None or row["updated_at"] >= old["updated_at"]:
                        rows[row["id"]] = row
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
            token = result.get("next_page_token")
            if not token:
                return list(rows.values())
            if token in seen_tokens:
                raise ProviderError("News pagination did not advance")
            seen_tokens.add(token)
            params["page_token"] = token
        raise ProviderError("News pagination limit exceeded (10 pages); narrow the time range")

    def daily_bars(self, symbols):
        now = self.now()
        # Request only sessions before today's New York calendar date. This is
        # deliberately conservative even after today's close: no partial day leaks.
        today_ny = now.astimezone(NY).date()
        cutoff = datetime.combine(today_ny, datetime.min.time(), NY).astimezone(UTC)
        params = {"symbols": ",".join(symbols), "timeframe": "1Day",
                  "start": iso(cutoff - timedelta(days=100)),
                  "end": iso(cutoff - timedelta(microseconds=1)), "limit": 10000,
                  "adjustment": "split", "feed": self.feed, "sort": "asc"}
        rows, seen_tokens = {}, set()
        allowed = set(symbols)
        for _ in range(self.MAX_PAGES):
            result = self._get(self.DATA_URL, "/v2/stocks/bars", params)
            if not isinstance(result, dict) or not isinstance(result.get("bars"), dict):
                raise ProviderError("Historical bar response is invalid")
            for symbol, bars in result["bars"].items():
                if symbol not in allowed or not isinstance(bars, list):
                    continue
                for bar in bars:
                    try:
                        timestamp = iso(bar["t"])
                        if datetime.fromisoformat(timestamp) >= cutoff:
                            continue
                        row = {"symbol": symbol, "time": timestamp, "close": positive(bar["c"])}
                        rows[(symbol, timestamp)] = row
                    except (TypeError, ValueError, KeyError):
                        continue
            token = result.get("next_page_token")
            if not token:
                return sorted(rows.values(), key=lambda x: (x["symbol"], x["time"]))
            if token in seen_tokens:
                raise ProviderError("Historical pagination did not advance")
            seen_tokens.add(token)
            params["page_token"] = token
        raise ProviderError("Historical pagination limit exceeded (10 pages)")
