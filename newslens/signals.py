"""Pure, auditable alert rules. Sentiment is never an upward-price probability."""
from datetime import datetime, timezone
import math
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
VALID_CATEGORIES = {"Finance", "Politics & Policy", "Macro", "Other"}


def parse_time(value):
    """Accept timezone-aware ISO timestamps; reject ambiguous local timestamps."""
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("Timestamp must be an ISO string or datetime")
    if result.tzinfo is None:
        raise ValueError("Timestamp must include its timezone")
    return result.astimezone(timezone.utc)


def _positive(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and number > 0 else None
    except (TypeError, ValueError):
        return None


def _eligible_news(news, analysis, now, categories, max_age_hours=24):
    """Reject future/unavailable versions, missing evidence and invented symbols."""
    if not isinstance(analysis, dict) or analysis.get("status") != "success":
        return []
    try:
        now = parse_time(now)
        published = parse_time(news["created_at"])
        received = parse_time(news["received_at"])
        updated = parse_time(news.get("updated_at", news["created_at"]))
        if not 0 <= (now - published).total_seconds() < max_age_hours * 3600:
            return []
        if received > now or updated > now or received < published or received < updated:
            return []
        if analysis.get("analyzed_at") and parse_time(analysis["analyzed_at"]) > now:
            return []
    except (ValueError, TypeError, KeyError, OverflowError):
        return []
    labels = analysis.get("categories", [])
    if (not isinstance(labels, list) or not labels
            or any(not isinstance(x, str) for x in labels)
            or not set(labels) <= VALID_CATEGORIES):
        return []
    if categories:
        if (not isinstance(categories, (list, tuple, set))
                or any(not isinstance(x, str) for x in categories)
                or not set(labels).intersection(categories)):
            return []
    text = str(news.get("headline", "")) + "\n" + str(news.get("summary", ""))
    linked = news.get("symbols", [])
    if not isinstance(linked, list) or any(not isinstance(x, str) for x in linked):
        return []
    valid = []
    impacts = analysis.get("impacts", [])
    if not isinstance(impacts, list):
        return []
    for impact in impacts:
        if not isinstance(impact, dict):
            continue
        evidence = impact.get("evidence")
        if (isinstance(impact.get("symbol"), str) and impact["symbol"] in linked
                and isinstance(impact.get("sentiment"), str)
                and impact["sentiment"] in {"positive", "negative", "neutral"}
                and isinstance(evidence, str) and evidence.strip() and evidence in text):
            valid.append(impact)
    return valid


def news_alert(news, analysis, now, categories=None, max_age_hours=24):
    """One candidate per directly linked stock; worker handles subscriptions/dedup."""
    impacts = _eligible_news(news, analysis, now, categories, max_age_hours)
    candidates = []
    seen = set()
    for impact in impacts:
        symbol = impact["symbol"]
        if symbol in seen:
            continue
        seen.add(symbol)
        candidates.append({
            "kind": "news", "rule_id": None, "symbol": symbol,
            "created_at": parse_time(now).isoformat(), "price": None,
            "reason": f"New related news ({', '.join(analysis['categories'])}); "
                      f"{impact['sentiment']} sentiment. Evidence: {impact['evidence']}",
            "news_id": str(news["id"]), "data_mode": news.get("data_mode", "live"),
        })
    return candidates


def evaluate_rule(rule, previous_quote, quote, news_rows, now, max_age_seconds=180):
    """Trigger only on a fresh, same-session threshold crossing.

    An initial observation is a baseline, never a crossing. News must have been
    received before the decision, so historical replay cannot see future news.
    The worker remains responsible for cooldowns and durable event identities.
    """
    if not rule.get("enabled", True) or not previous_quote:
        return None
    threshold = _positive(rule.get("threshold"))
    old = _positive(previous_quote.get("price"))
    price = _positive(quote.get("price"))
    if threshold is None or old is None or price is None:
        return None
    if rule.get("direction") not in {"above", "below"}:
        return None
    for key in ("symbol", "feed", "data_mode"):
        if key not in quote or quote.get(key) != previous_quote.get(key):
            return None
    if quote["symbol"] != rule.get("symbol"):
        return None
    if quote.get("market_open") is not True or previous_quote.get("market_open") is not True:
        return None
    try:
        now = parse_time(now)
        old_time = parse_time(previous_quote["source_time"])
        source_time = parse_time(quote["source_time"])
        if source_time <= old_time:
            return None
        if old_time.astimezone(NY).date() != source_time.astimezone(NY).date():
            return None
        for q, timestamp in ((quote, source_time), (previous_quote, old_time)):
            received = parse_time(q["received_at"])
            if not 0 <= (now - timestamp).total_seconds() <= max_age_seconds:
                return None
            if timestamp > received or received > now:
                return None
        if (source_time - old_time).total_seconds() > max_age_seconds:
            return None
    except (TypeError, ValueError, KeyError, OverflowError):
        return None
    above = rule["direction"] == "above"
    crossed = old < threshold <= price if above else old > threshold >= price
    if not crossed:
        return None
    selected_news = None
    impact = None
    if rule.get("require_news", False):
        for item in news_rows:
            if item.get("data_mode") != quote["data_mode"]:
                continue
            candidates = _eligible_news(item, item.get("analysis"), now, rule.get("categories"))
            wanted = "positive" if above else "negative"
            matched = next((x for x in candidates if x["symbol"] == quote["symbol"]
                            and x["sentiment"] == wanted), None)
            if matched:
                selected_news, impact = item, matched
                break
        if selected_news is None:
            return None
    reason = (f"Price crossed {'above' if above else 'below'} ${threshold:.2f}: "
              f"${old:.2f} → ${price:.2f}; feed={quote['feed']}.")
    if impact:
        reason += f" Related {impact['sentiment']} news. Evidence: {impact['evidence']}"
    return {
        "kind": "composite" if selected_news else "price",
        "rule_id": rule.get("id"), "symbol": quote["symbol"],
        "created_at": now.isoformat(), "price": price, "reason": reason,
        "news_id": str(selected_news["id"]) if selected_news else None,
        "data_mode": quote["data_mode"],
    }


def indicators(bars, now=None):
    """MA20 and five-trading-session close return (fraction, not percent).

    Callers supply completed daily bars. If now is supplied, additionally reject
    today's/future daily bars by New York session date. Missing history remains
    None; it is never padded with invented prices.
    """
    completed = {}
    today = parse_time(now).astimezone(NY).date() if now is not None else None
    for bar in bars:
        price = _positive(bar.get("close"))
        if price is None:
            continue
        try:
            timestamp = parse_time(bar["time"])
        except (ValueError, KeyError, TypeError):
            continue
        day = timestamp.astimezone(NY).date()
        if today is not None and day >= today:
            continue
        completed[day] = (timestamp, price)
    prices = [completed[d][1] for d in sorted(completed)]
    return {"ma20": sum(prices[-20:]) / 20 if len(prices) >= 20 else None,
            "return_5d": prices[-1] / prices[-6] - 1 if len(prices) >= 6 else None,
            "count": len(prices)}
