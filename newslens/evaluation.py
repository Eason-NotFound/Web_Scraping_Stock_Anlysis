"""Evaluate human labels and observe alert outcomes without fabricated backtests."""
import argparse
import csv
from datetime import timedelta
import json
import math
from pathlib import Path

from .signals import parse_time


def classification_metrics(expected, predicted):
    """Per-class precision/recall with None for undefined metrics.

    Each item is one label or a collection of labels (for multilabel categories).
    Human labels should be frozen before running this evaluation.
    """
    if len(expected) != len(predicted):
        raise ValueError("Expected and predicted sample counts must agree")
    def as_set(value):
        if value is None or value == "":
            return set()
        return {value} if isinstance(value, str) else set(value)
    actual = [as_set(x) for x in expected]
    guessed = [as_set(x) for x in predicted]
    labels = sorted(set().union(*actual, *guessed)) if actual or guessed else []
    report = {}
    for label in labels:
        tp = sum(label in a and label in p for a, p in zip(actual, guessed))
        fp = sum(label not in a and label in p for a, p in zip(actual, guessed))
        fn = sum(label in a and label not in p for a, p in zip(actual, guessed))
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        f1 = (2 * precision * recall / (precision + recall)
              if precision is not None and recall is not None and precision + recall
              else (0.0 if precision is not None and recall is not None else None))
        report[label] = {"precision": precision, "recall": recall, "f1": f1,
                         "support": tp + fn, "tp": tp, "fp": fp, "fn": fn}
    return {"samples": len(actual), "exact_match":
            sum(a == p for a, p in zip(actual, guessed)) / len(actual) if actual else None,
            "by_label": report}


def observed_return(alert, quotes, horizon_minutes=60, max_lateness_minutes=10):
    """Observe one real alert's later price, not a simulated trade profit.

    Sample the first quote at/after the horizon, with bounded lateness. Require
    the same symbol/feed/mode and session. News-only alerts lack an entry price
    and remain unavailable. Demo output is explicitly not strategy evidence.
    """
    if alert.get("data_mode") != "live":
        return {"status": "demo", "return": None,
                "note": "Synthetic demonstration; not strategy-performance evidence"}
    try:
        price = float(alert["price"])
        if not math.isfinite(price) or price <= 0:
            raise ValueError
        start = parse_time(alert["created_at"])
    except (ValueError, TypeError, KeyError):
        return {"status": "unavailable", "return": None, "note": "No valid alert-time price"}
    if not alert.get("feed"):
        return {"status": "unavailable", "return": None, "note": "Alert has no recorded market-data feed"}
    target = start + timedelta(minutes=horizon_minutes)
    latest = target + timedelta(minutes=max_lateness_minutes)
    from .signals import NY
    candidates = []
    for quote in quotes:
        if quote.get("symbol") != alert.get("symbol") or quote.get("data_mode") != "live":
            continue
        if alert.get("feed") and quote.get("feed") != alert["feed"]:
            continue
        try:
            timestamp = parse_time(quote["source_time"])
            value = float(quote["price"])
        except (ValueError, TypeError, KeyError):
            continue
        if (target <= timestamp <= latest and math.isfinite(value) and value > 0
                and timestamp.astimezone(NY).date() == start.astimezone(NY).date()):
            candidates.append((timestamp, value))
    if not candidates:
        return {"status": "pending", "return": None,
                "note": "No same-session quote within the observation window"}
    timestamp, value = min(candidates)
    return {"status": "observed", "return": value / price - 1,
            "observed_at": timestamp.isoformat(), "observed_price": value,
            "note": "Price observation only; excludes fees/slippage and is not causal evidence"}


def export_label_template(news_rows, path):
    """Export original text and blank human labels, with no model-label leakage."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fields = ["news_id", "version_hash", "headline", "summary", "source_symbols",
              "human_categories", "human_symbol", "human_sentiment", "notes"]
    with target.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for news in news_rows:
            writer.writerow({"news_id": news["id"], "version_hash": news.get("hash", ""),
                             "headline": news.get("headline", ""), "summary": news.get("summary", ""),
                             "source_symbols": "|".join(news.get("symbols", []))})
    return str(target)


def evaluate_labels(news_rows, path):
    indexed = {(str(n["id"]), n.get("hash", "")): n for n in news_rows}
    expected_categories, predicted_categories = [], []
    expected_sentiments, predicted_sentiments = [], []
    unmatched = 0
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            news = indexed.get((row["news_id"], row.get("version_hash", "")))
            if news is None:
                unmatched += 1
                continue
            analysis = news.get("analysis") or {}
            if row.get("human_categories", "").strip():
                expected_categories.append([x.strip() for x in row["human_categories"].split("|") if x.strip()])
                predicted_categories.append(analysis.get("categories", []) if analysis.get("status") == "success" else [])
            if row.get("human_symbol", "").strip() and row.get("human_sentiment", "").strip():
                symbol = row["human_symbol"].strip()
                predicted = next((p.get("sentiment") for p in analysis.get("impacts", [])
                                  if p.get("symbol") == symbol), None)
                expected_sentiments.append(row["human_sentiment"].strip())
                predicted_sentiments.append(predicted if analysis.get("status") == "success" else None)
    return {"categories": classification_metrics(expected_categories, predicted_categories),
            "sentiment": classification_metrics(expected_sentiments, predicted_sentiments),
            "unmatched_versions": unmatched,
            "note": "Human labels required; synthetic demonstration is not real-world model accuracy"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export-labels", metavar="CSV", help="Export blank human-label template")
    parser.add_argument("--labels", metavar="CSV", help="Evaluate completed frozen human-label CSV")
    args = parser.parse_args()
    from .config import load_settings
    from .storage import Store
    settings = load_settings()
    store = Store(settings.db_path)
    store.init()
    news = store.news_rows(limit=10000)
    if args.export_labels:
        print(export_label_template(news, args.export_labels))
    elif args.labels:
        print(json.dumps(evaluate_labels(news, args.labels), indent=2))
    else:
        alerts = store.alerts(limit=1000)
        outcomes = [{"event_id": a["event_id"], **observed_return(a, store.quote_history(a["symbol"], limit=10000))}
                    for a in alerts]
        print(json.dumps({"mode": settings.mode, "counts": store.counts(),
                          "outcomes": outcomes, "model_accuracy": "Not measured without frozen human labels"}, indent=2))


if __name__ == "__main__":
    main()
