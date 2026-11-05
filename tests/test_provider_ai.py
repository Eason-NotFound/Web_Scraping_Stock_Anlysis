"""Offline contract tests: no credential or external request is needed."""
from datetime import datetime, timezone
import copy
import json
import unittest
from urllib.parse import parse_qs, urlparse

from newslens.data_provider import AlpacaProvider, ProviderError
from newslens.news_analysis import Analyzer, content_hash, source_text, validate_analysis

NOW = datetime(2026, 11, 4, 16, tzinfo=timezone.utc)


def article(**changes):
    row = {"id": "123", "headline": "Apple reports higher earnings",
           "summary": "Apple reported higher earnings, while its outlook remained uncertain.",
           "symbols": ["AAPL"], "created_at": "2026-11-04T15:00:00Z",
           "updated_at": "2026-11-04T15:00:00Z", "url": "https://example.com/news"}
    row.update(changes)
    return row


def output(**changes):
    row = {"categories": ["Finance"], "summary": "Apple reported higher earnings.",
           "impacts": [{"symbol": "AAPL", "sentiment": "positive",
                        "evidence": "Apple reported higher earnings", "reason": "Earnings increased."}]}
    row.update(changes)
    return row


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, body=None, timeout=15):
        self.calls.append((url, headers, body, timeout))
        if not self.responses:
            raise AssertionError("Unexpected extra request")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ProviderTests(unittest.TestCase):
    def provider(self, transport):
        return AlpacaProvider("not-real", "not-real-secret", transport=transport, now=lambda: NOW)

    def test_quote_uses_trade_time_and_explicit_feed(self):
        transport = FakeTransport([{"is_open": True}, {
            "AAPL": {"latestTrade": {"p": 100, "t": "2026-11-04T15:58:00Z"},
                     "prevDailyBar": {"c": 99}},
            "MSFT": {"latestTrade": {"p": float("nan"), "t": "2026-11-04T15:58:00Z"},
                     "prevDailyBar": {"c": 100}}}])
        quotes = self.provider(transport).snapshots(["AAPL", "MSFT"])
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0]["source_time"], "2026-11-04T15:58:00+00:00")
        self.assertNotEqual(quotes[0]["source_time"], quotes[0]["received_at"])
        self.assertTrue(quotes[0]["market_open"])
        self.assertEqual(parse_qs(urlparse(transport.calls[1][0]).query)["feed"], ["iex"])

    def test_clock_failure_fails_closed_without_losing_quote(self):
        transport = FakeTransport([ProviderError("API returned HTTP 403"), {
            "AAPL": {"latestTrade": {"p": 100, "t": "2026-11-04T15:58:00Z"},
                     "prevDailyBar": {"c": 99}}}])
        quote = self.provider(transport).snapshots(["AAPL"])[0]
        self.assertFalse(quote["market_open"])
        self.assertEqual(quote["clock_status"], "unavailable")

    def test_news_follows_pagination_and_filters_symbols(self):
        transport = FakeTransport([
            {"news": [article(symbols=["AAPL", "UNSUPPORTED"])], "next_page_token": "page2"},
            {"news": [article(id=124, headline="<b>Policy</b> update", url="javascript:bad")],
             "next_page_token": None}])
        rows = self.provider(transport).news(["AAPL"], "2026-11-04T00:00:00Z")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["symbols"], ["AAPL"])
        self.assertEqual(rows[1]["headline"], "Policy update")
        self.assertEqual(rows[1]["url"], "")
        self.assertEqual(parse_qs(urlparse(transport.calls[1][0]).query)["page_token"], ["page2"])

    def test_news_availability_time_is_recorded_after_each_page(self):
        transport = FakeTransport([
            {"news": [article()], "next_page_token": "page2"},
            {"news": [article(id=124, created_at="2026-11-04T16:00:05Z",
                              updated_at="2026-11-04T16:00:10Z")]}])
        times = iter([NOW, datetime(2026, 11, 4, 16, 0, 11, tzinfo=timezone.utc)])
        provider = AlpacaProvider("key", "secret", transport=transport, now=lambda: next(times))
        rows = provider.news(["AAPL"], "2026-11-04T00:00:00Z")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["received_at"], "2026-11-04T16:00:00+00:00")
        self.assertEqual(rows[1]["received_at"], "2026-11-04T16:00:11+00:00")
        self.assertGreaterEqual(rows[1]["received_at"], rows[1]["updated_at"])

    def test_future_or_backdated_news_version_is_not_persistable(self):
        transport = FakeTransport([{"news": [
            article(id=1, updated_at="2026-11-04T16:00:01Z"),
            article(id=2, created_at="2026-11-04T16:00:01Z", updated_at="2026-11-04T16:00:02Z"),
            article(id=3, created_at="2026-11-04T15:00:00Z", updated_at="2026-11-04T14:00:00Z"),
            article(id=4)]}])
        rows = self.provider(transport).news(["AAPL"], "2026-11-04T00:00:00Z")
        self.assertEqual([row["id"] for row in rows], ["4"])

    def test_repeated_pagination_is_error(self):
        transport = FakeTransport([{"news": [], "next_page_token": "again"},
                                   {"news": [], "next_page_token": "again"}])
        with self.assertRaisesRegex(ProviderError, "pagination"):
            self.provider(transport).news(["AAPL"], "2026-11-04T00:00:00Z")

    def test_news_page_budget_stops_after_ten_requests(self):
        transport = FakeTransport([{"news": [], "next_page_token": f"page-{i}"}
                                   for i in range(12)])
        with self.assertRaisesRegex(ProviderError, "10 pages"):
            self.provider(transport).news(["AAPL"], "2026-11-04T00:00:00Z")
        self.assertEqual(len(transport.calls), 10)
        self.assertTrue(all(call[3] == 15 for call in transport.calls))

    def test_historical_page_budget_stops_after_ten_requests(self):
        transport = FakeTransport([{"bars": {}, "next_page_token": f"page-{i}"}
                                   for i in range(12)])
        with self.assertRaisesRegex(ProviderError, "10 pages"):
            self.provider(transport).daily_bars(["AAPL"])
        self.assertEqual(len(transport.calls), 10)

    def test_news_revisions_keep_most_recent(self):
        transport = FakeTransport([{"news": [article(updated_at="2026-11-04T15:30:00Z"),
                                                        article(updated_at="2026-11-04T15:00:00Z")]}])
        rows = self.provider(transport).news(["AAPL"], "2026-11-04T00:00:00Z")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["updated_at"], "2026-11-04T15:30:00+00:00")

    def test_daily_pagination_excludes_today_and_sets_split(self):
        transport = FakeTransport([
            {"bars": {"AAPL": [{"t": "2026-11-03T05:00:00Z", "c": 99},
                                  {"t": "2026-11-04T05:00:00Z", "c": 101}]},
             "next_page_token": "p2"},
            {"bars": {"MSFT": [{"t": "2026-11-03T05:00:00Z", "c": 150}]}}])
        bars = self.provider(transport).daily_bars(["AAPL", "MSFT"])
        self.assertEqual([(x["symbol"], x["close"]) for x in bars], [("AAPL", 99), ("MSFT", 150)])
        params = parse_qs(urlparse(transport.calls[0][0]).query)
        self.assertEqual(params["adjustment"], ["split"])
        self.assertEqual(params["feed"], ["iex"])
        self.assertEqual(parse_qs(urlparse(transport.calls[1][0]).query)["page_token"], ["p2"])

    def test_no_credentials_and_safe_custom_transport_error(self):
        transport = FakeTransport([])
        with self.assertRaisesRegex(ProviderError, "credentials"):
            AlpacaProvider("", "", transport=transport).snapshots(["AAPL"])
        self.assertEqual(transport.calls, [])
        with self.assertRaises(ProviderError) as caught:
            self.provider(FakeTransport([RuntimeError("not-real-secret")])).daily_bars(["AAPL"])
        self.assertNotIn("not-real-secret", str(caught.exception))


class AnalysisTests(unittest.TestCase):
    def test_evidence_supported_output(self):
        checked = validate_analysis(output(), article())
        self.assertEqual(checked["impacts"][0]["sentiment"], "positive")
        self.assertEqual(source_text(article()).splitlines()[0], article()["headline"])

    def test_unsupported_symbol_or_evidence_rejected(self):
        for field, value in [("symbol", "MSFT"), ("evidence", "An invented quotation")]:
            bad = output()
            bad["impacts"][0][field] = value
            with self.assertRaises(ValueError):
                validate_analysis(bad, article())

    def test_invalid_categories_sentiment_and_extra_fields_rejected(self):
        bads = [output(categories=["BUY"]), output(categories=["Finance", "Finance"]),
                {**output(), "confidence": 0.99}]
        bad = output()
        bad["impacts"][0]["sentiment"] = "bullish"
        bads.append(bad)
        for bad in bads:
            with self.assertRaises(ValueError):
                validate_analysis(bad, article())

    def test_empty_association_and_multilabel_are_valid(self):
        checked = validate_analysis(output(categories=["Politics & Policy", "Macro"], impacts=[]),
                                    article(symbols=[]))
        self.assertEqual(checked["impacts"], [])

    def test_hash_changes_for_revision_not_symbol_order(self):
        self.assertNotEqual(content_hash(article()), content_hash(article(summary="New summary")))
        self.assertNotEqual(content_hash(article()), content_hash(article(updated_at="2026-11-04T16:00:00Z")))
        self.assertEqual(content_hash(article(symbols=["AAPL", "MSFT"])),
                         content_hash(article(symbols=["MSFT", "AAPL"])))

    def test_missing_key_and_summary_do_not_fake_analysis(self):
        transport = FakeTransport([])
        analyzer = Analyzer("", "https://example.com/v1", "example", transport=transport)
        self.assertEqual(analyzer.analyze(article())["status"], "error")
        self.assertEqual(analyzer.analyze(article(summary=""))["status"], "insufficient")
        self.assertEqual(transport.calls, [])

    def test_model_strict_json_prompt_boundary_and_no_secret_errors(self):
        response = {"choices": [{"message": {"content": json.dumps(output())}}]}
        transport = FakeTransport([response])
        analyzer = Analyzer("private-token", "https://example.com/v1", "test-model", transport)
        original = article()
        result = analyzer.analyze(original)
        self.assertEqual(result["status"], "success")
        self.assertEqual(transport.calls[0][3], 20)
        sent = json.loads(transport.calls[0][2])
        self.assertIn("untrusted", sent["messages"][0]["content"])
        self.assertEqual(json.loads(sent["messages"][1]["content"])["headline"], original["headline"])
        self.assertNotIn("status", original)
        analyzer.transport = FakeTransport([RuntimeError("private-token")])
        error = analyzer.analyze(original)
        self.assertEqual(error["status"], "error")
        self.assertNotIn("private-token", error["error"])

    def test_markdown_and_invalid_https_rejected(self):
        transport = FakeTransport([{"choices": [{"message": {"content": "```json\n{}\n```"}}]}])
        analyzer = Analyzer("key", "https://example.com/v1", "test", transport)
        self.assertEqual(analyzer.analyze(article())["status"], "error")
        analyzer = Analyzer("key", "http://example.com/v1", "test", FakeTransport([]))
        self.assertEqual(analyzer.analyze(article())["status"], "error")


if __name__ == "__main__":
    unittest.main()
