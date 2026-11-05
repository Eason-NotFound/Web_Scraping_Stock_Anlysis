"""Independent hand-calculated examples and adversarial time/evidence checks."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import tempfile
import unittest

from newslens.signals import evaluate_rule, indicators, news_alert, parse_time
from newslens.evaluation import classification_metrics, observed_return, export_label_template, evaluate_labels

NOW = datetime(2026, 11, 4, 15, 1, tzinfo=timezone.utc)


def quote(price, minute=0, **changes):
    result = {"symbol": "AAPL", "price": price, "previous_close": 98,
              "source_time": (NOW + timedelta(minutes=minute)).isoformat(),
              "received_at": (NOW + timedelta(minutes=minute)).isoformat(),
              "feed": "iex", "market_open": True, "data_mode": "live"}
    result.update(changes)
    return result


def article():
    time = (NOW - timedelta(hours=1)).isoformat()
    return {"id": "n1", "headline": "Apple reports higher profit", "summary": "Apple profit increased 10%.",
            "symbols": ["AAPL"], "created_at": time, "updated_at": time, "received_at": time,
            "data_mode": "live", "hash": "version1", "analysis": {
                "status": "success", "categories": ["Finance"],
                "analyzed_at": time, "summary": "Profit increased.",
                "impacts": [{"symbol": "AAPL", "sentiment": "positive", "evidence": "Apple profit increased 10%.",
                             "reason": "Higher profit"}]}}


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.rule = {"id": 7, "symbol": "AAPL", "direction": "above", "threshold": 100,
                     "enabled": True, "require_news": False, "categories": ["Finance"]}

    def test_crossing_above_at_equality(self):
        alert = evaluate_rule(self.rule, quote(99, -1), quote(100), [], NOW)
        self.assertEqual(alert["kind"], "price")
        self.assertEqual(alert["price"], 100)
        self.assertIn("$99.00 → $100.00", alert["reason"])

    def test_crossing_below(self):
        self.rule["direction"] = "below"
        self.assertIsNotNone(evaluate_rule(self.rule, quote(101, -1), quote(99), [], NOW))

    def test_initial_observation_and_remaining_above_do_not_trigger(self):
        self.assertIsNone(evaluate_rule(self.rule, None, quote(101), [], NOW))
        self.assertIsNone(evaluate_rule(self.rule, quote(100, -1), quote(101), [], NOW))
        self.assertIsNone(evaluate_rule(self.rule, quote(101, -1), quote(102), [], NOW))

    def test_disabled_and_nonfinite_threshold_do_not_trigger(self):
        for field, value in [("enabled", False), ("threshold", float("nan")), ("threshold", -1),
                             ("threshold", True), ("direction", "buy")]:
            rule = {**self.rule, field: value}
            self.assertIsNone(evaluate_rule(rule, quote(99, -1), quote(101), [], NOW))
        self.assertIsNone(evaluate_rule(self.rule, quote(True, -1), quote(101), [], NOW))
        self.assertIsNone(evaluate_rule(self.rule, quote(99, -1), quote(True), [], NOW))

    def test_market_closed_and_stale_data_block(self):
        self.assertIsNone(evaluate_rule(self.rule, quote(99, -1), quote(101, market_open=False), [], NOW))
        self.assertIsNone(evaluate_rule(self.rule, quote(99, -10), quote(101), [], NOW))
        self.assertIsNone(evaluate_rule(self.rule, quote(99, -11), quote(101, -10), [], NOW))

    def test_future_naive_and_out_of_order_quotes_block(self):
        for old, current in [(quote(99, -1), quote(101, 1)),
                             (quote(99, -1), quote(101, -2)),
                             (quote(99, -1), quote(101, received_at=(NOW + timedelta(seconds=1)).isoformat())),
                             (quote(99, -1), quote(101, source_time="2026-11-04T15:01:00"))]:
            self.assertIsNone(evaluate_rule(self.rule, old, current, [], NOW))

    def test_feed_symbol_and_mode_mismatch_block(self):
        for field, value in [("feed", "sip"), ("symbol", "MSFT"), ("data_mode", "demo")]:
            self.assertIsNone(evaluate_rule(self.rule, quote(99, -1), quote(101, **{field: value}), [], NOW))

    def test_overnight_baseline_does_not_trigger(self):
        old = quote(99, source_time=(NOW - timedelta(days=1)).isoformat(),
                    received_at=(NOW - timedelta(days=1)).isoformat())
        self.assertIsNone(evaluate_rule(self.rule, old, quote(101), [], NOW, max_age_seconds=100000))

    def test_composite_requires_supported_directional_news(self):
        self.rule["require_news"] = True
        news = article()
        alert = evaluate_rule(self.rule, quote(99, -1), quote(101), [news], NOW)
        self.assertEqual(alert["kind"], "composite")
        self.assertEqual(alert["news_id"], "n1")
        self.assertIsNone(evaluate_rule(self.rule, quote(99, -1), quote(101), [], NOW))
        for sentiment in ("negative", "neutral", "unclear"):
            news["analysis"]["impacts"][0]["sentiment"] = sentiment
            self.assertIsNone(evaluate_rule(self.rule, quote(99, -1), quote(101), [news], NOW))

    def test_unavailable_or_future_news_never_leaks_into_signal(self):
        self.rule["require_news"] = True
        for field, value in [("created_at", (NOW + timedelta(seconds=1)).isoformat()),
                             ("received_at", (NOW + timedelta(seconds=1)).isoformat()),
                             ("updated_at", (NOW + timedelta(seconds=1)).isoformat()),
                             ("created_at", (NOW - timedelta(days=2)).isoformat()),
                             ("data_mode", "demo")]:
            news = article()
            news[field] = value
            self.assertIsNone(evaluate_rule(self.rule, quote(99, -1), quote(101), [news], NOW))

    def test_invalid_evidence_or_added_symbol_cannot_trigger(self):
        self.rule["require_news"] = True
        news = article()
        for key, value in [("evidence", "invented claim"), ("symbol", "MSFT")]:
            edited = deepcopy(news)
            edited["analysis"]["impacts"][0][key] = value
            self.assertIsNone(evaluate_rule(self.rule, quote(99, -1), quote(101), [edited], NOW))

    def test_news_filters_and_duplicate_impact(self):
        news = article()
        analysis = news["analysis"]
        self.assertEqual(len(news_alert(news, analysis, NOW, ["Finance"])), 1)
        self.assertEqual(news_alert(news, analysis, NOW, ["Politics & Policy"]), [])
        analysis["impacts"].append(dict(analysis["impacts"][0]))
        self.assertEqual(len(news_alert(news, analysis, NOW)), 1)
        analysis["impacts"][0]["sentiment"] = "unclear"
        analysis["impacts"][1]["sentiment"] = "unclear"
        self.assertEqual(news_alert(news, analysis, NOW), [])

    def test_malformed_nested_categories_symbols_and_sentiments_fail_closed(self):
        news = article()
        for bad_labels in ([["Finance"]], [{"name": "Finance"}], [None], "Finance"):
            analysis = {**news["analysis"], "categories": bad_labels}
            self.assertEqual(news_alert(news, analysis, NOW), [])
        for bad_symbols in ({"AAPL": True}, [["AAPL"]], [None]):
            self.assertEqual(news_alert({**news, "symbols": bad_symbols}, news["analysis"], NOW), [])
        for bad_sentiment in (["positive"], {"positive": 1}):
            analysis = deepcopy(news["analysis"])
            analysis["impacts"][0]["sentiment"] = bad_sentiment
            self.assertEqual(news_alert(news, analysis, NOW), [])
        self.assertEqual(news_alert(news, news["analysis"], NOW, [["Finance"]]), [])


class IndicatorTests(unittest.TestCase):
    def test_hand_calculated_ma_and_five_day_return(self):
        start = datetime(2026, 10, 1, 14, tzinfo=timezone.utc)
        bars = [{"time": (start + timedelta(days=i)).isoformat(), "close": i + 1} for i in range(20)]
        result = indicators(list(reversed(bars)))
        self.assertEqual(result["ma20"], 10.5)
        self.assertAlmostEqual(result["return_5d"], 20 / 15 - 1)
        self.assertEqual(result["count"], 20)

    def test_insufficient_history_stays_none(self):
        result = indicators([{"time": "2026-10-01T14:00:00Z", "close": 10}])
        self.assertIsNone(result["ma20"])
        self.assertIsNone(result["return_5d"])

    def test_future_today_invalid_and_duplicate_bars_excluded(self):
        bars = [{"time": "2026-11-03T14:00:00Z", "close": 10}] * 2
        bars += [{"time": "2026-11-04T14:00:00Z", "close": 11},
                 {"time": "2026-11-05T14:00:00Z", "close": 12},
                 {"time": "2026-11-02T14:00:00Z", "close": float("inf")}]
        self.assertEqual(indicators(bars, now=NOW)["count"], 1)


class EvaluationTests(unittest.TestCase):
    def test_precision_recall_are_hand_computed(self):
        result = classification_metrics(["positive", "positive", "negative"], ["positive", "negative", "negative"])
        self.assertEqual(result["samples"], 3)
        self.assertEqual(result["by_label"]["positive"]["precision"], 1)
        self.assertEqual(result["by_label"]["positive"]["recall"], .5)
        self.assertEqual(result["by_label"]["negative"]["precision"], .5)
        self.assertIsNone(classification_metrics([], [])["exact_match"])

    def test_outcome_uses_matching_feed_and_bounded_future_quote(self):
        alert = {"data_mode": "live", "symbol": "AAPL", "price": 100,
                 "created_at": NOW.isoformat(), "feed": "iex"}
        quotes = [quote(1000, 60, feed="sip"), quote(105, 62), quote(99, 59)]
        result = observed_return(alert, quotes)
        self.assertEqual(result["status"], "observed")
        self.assertAlmostEqual(result["return"], .05)
        self.assertEqual(observed_return(alert, [quote(105, 80)])["status"], "pending")
        self.assertEqual(observed_return({**alert, "data_mode": "demo"}, quotes)["status"], "demo")
        self.assertEqual(observed_return({**alert, "feed": None}, quotes)["status"], "unavailable")

    def test_export_labels_are_blank_and_version_matched(self):
        import csv
        news = article()
        with tempfile.TemporaryDirectory() as tmp:
            path = export_label_template([news], f"{tmp}/labels.csv")
            with open(path, encoding="utf-8-sig") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["human_categories"], "")
            rows[0].update(human_categories="Finance", human_symbol="AAPL", human_sentiment="positive")
            with open(path, "w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            report = evaluate_labels([news], path)
            self.assertEqual(report["categories"]["exact_match"], 1)
            self.assertEqual(report["sentiment"]["exact_match"], 1)
            self.assertEqual(evaluate_labels([{**news, "hash": "changed"}], path)["unmatched_versions"], 1)


if __name__ == "__main__":
    unittest.main()
