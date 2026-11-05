"""NewsLens bilingual UI. Reads saved data; never starts background workers."""
from __future__ import annotations

import csv
from dataclasses import replace
from datetime import datetime, timezone
from functools import partial
import hmac
import io
import os
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from newslens.config import load_settings
from newslens.i18n import translate, localize_log
from newslens.storage import Store

CATEGORIES = ["Finance", "Politics & Policy", "Macro", "Other"]
SECRET_KEYS = ["ALPACA_API_KEY", "ALPACA_SECRET_KEY", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
               "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NEWSLENS_ADMIN_TOKEN"]
COLUMN_LABELS = {"source_time": "Price timestamp (UTC)", "price": "Price (USD)", "feed": "Feed",
                 "created_at": "Created at (UTC)", "symbol": "Stock", "kind": "Event type",
                 "reason": "Reason / original record", "delivery_status": "Delivery status",
                 "data_mode": "Data mode", "time": "Time (UTC)", "level": "Level", "message": "Message"}


def language():
    return "zh" if st.session_state.get("ui_language", "English") == "中文" else "en"


def t(text, **values):
    return translate(text, language(), **values)


def refresh_translated_selections():
    """Resend labels to browser widgets while retaining canonical selections."""
    prefixes = ("stock_", "news_stock_", "news_category_", "news_sentiment_",
                "subscribed_", "rule_stock_", "rule_direction_", "rule_categories_")
    for key in list(st.session_state):
        if key in ("data_mode", "workspace") or key.startswith(prefixes):
            st.session_state[key] = st.session_state[key]


def localized_frame(frame):
    """Localize headings and enums on a copy; raw audit exports stay unchanged."""
    frame = frame.copy()
    for column in ("kind", "data_mode", "level"):
        if column in frame:
            frame[column] = frame[column].map(lambda value: t(str(value)))
    if "delivery_status" in frame:
        labels = {"pending": "Pending delivery", "unknown": "Delivery outcome unknown", "sent": "Sent",
                  "failed": "Failed", "sending": "Sending", "skipped_demo": "Skipped in demo"}
        frame["delivery_status"] = frame["delivery_status"].map(lambda value: t(labels.get(value, str(value))))
    if "reason" in frame:
        frame["reason"] = frame["reason"].map(lambda value: localize_log(str(value), language()))
    return frame.rename(columns=lambda key: t(COLUMN_LABELS.get(key, key)))


def utc_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc) if dt.tzinfo else None
    except (ValueError, TypeError):
        return None


def display_time(value):
    dt = utc_time(value)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC") if dt else t("Not available")


def credential_environment():
    try:
        for key in SECRET_KEYS:
            if key in st.secrets and not os.getenv(key):
                os.environ[key] = str(st.secrets[key])
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        pass


def selected_settings(mode):
    base = load_settings()
    db_path = base.db_path
    if mode != base.mode:
        db_path = str(Path(base.db_path).parent / f"{mode}.sqlite")
    return replace(base, mode=mode, db_path=db_path)


def as_csv(rows):
    if not rows:
        return ""
    keys = list(dict.fromkeys(k for row in rows for k in row))
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=keys, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()


def analysis_status(row):
    analysis = row.get("analysis")
    return analysis.get("status", "pending") if isinstance(analysis, dict) else "pending"


def render_market(store, settings):
    quotes = store.latest_quotes()
    news = store.news_rows(limit=300)
    alerts = store.alerts(limit=300)
    a, b, c, d = st.columns(4)
    a.metric(t("Stocks tracked"), len(quotes))
    b.metric(t("News in view"), len(news))
    c.metric(t("Completed analyses"), sum(analysis_status(n) == "success" for n in news))
    d.metric(t("Recorded alerts"), len(alerts))
    if settings.mode == "demo":
        st.caption(t("Demo analyses are hand-authored fixture annotations, not AI predictions."))
    else:
        st.caption(t("Completed analyses passed format and evidence checks; semantic accuracy still requires human review."))
    st.subheader(t("Your watchlist"))
    if not quotes:
        st.info(t("No live quotes yet. Start the separate worker with your data credentials; this page only reads saved results."))
        st.code("./Start_Monitor.command", language="bash")
        return
    display = []
    now = datetime.now(timezone.utc)
    for q in quotes:
        p, close = q.get("price"), q.get("previous_close")
        change = ((p / close) - 1) * 100 if p is not None and close else None
        timestamp = utc_time(q.get("source_time"))
        age = (now - timestamp).total_seconds() if timestamp else None
        if settings.mode == "demo":
            state = "Synthetic fixture"
        elif not q.get("market_open"):
            state = "Market closed / clock unavailable"
        elif age is None or age < -30 or age > settings.max_age_seconds:
            state = "Stale / unavailable"
        else:
            state = "Fresh"
        display.append({"Stock": q["symbol"], "Price (USD)": p, "Change (%)": change,
                        "Status": t(state), "Feed": q.get("feed", t("unknown")),
                        "Price timestamp (UTC)": q.get("source_time")})
    st.dataframe(localized_frame(pd.DataFrame(display)), hide_index=True, width="stretch",
                 column_config={t("Price (USD)"): st.column_config.NumberColumn(format="$%.2f"),
                                t("Change (%)"): st.column_config.NumberColumn(format="%.2f%%")})
    symbols = [r["Stock"] for r in display]
    symbol = st.selectbox(t("Explore a stock"), symbols, key=f"stock_{settings.mode}")
    quote = next(q for q in quotes if q["symbol"] == symbol)
    history = store.bars(symbol)
    intraday = store.quote_history(symbol, limit=500)
    left, right = st.columns([3, 1])
    with left:
        st.subheader(t("{symbol} · completed daily closes", symbol=symbol))
        if history:
            bars = pd.DataFrame(history)
            bars["time"] = pd.to_datetime(bars["time"], utc=True)
            bars = bars.sort_values("time").drop_duplicates("time")
            bars["MA20"] = bars["close"].rolling(20, min_periods=20).mean()
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=bars["time"], y=bars["close"], name=t("Close"), line={"color": "#2257d5", "width": 2.5}))
            fig.add_trace(go.Scatter(x=bars["time"], y=bars["MA20"], name=t("MA20"), line={"color": "#d98d20", "width": 1.5}))
            fig.update_layout(height=340, margin={"l": 10, "r": 10, "t": 15, "b": 10},
                              legend={"orientation": "h"}, xaxis_title=None, yaxis_title=t("USD"), template="plotly_white")
            st.plotly_chart(fig, width="stretch")
        else:
            st.info(t("Completed daily bars have not been collected yet."))
    with right:
        st.subheader(t("Data receipt"))
        st.write("**" + t("Price time") + "**")
        st.caption(display_time(quote.get("source_time")))
        st.write("**" + t("Received time") + "**")
        st.caption(display_time(quote.get("received_at")))
        st.write("**" + t("Source") + "**")
        st.caption(str(quote.get("feed", t("unknown"))))
        st.caption(t("MA20 describes past prices. It is not a forecast or an estimated probability."))
    if len(intraday) > 1:
        with st.expander(t("Saved monitoring observations")):
            observed = pd.DataFrame(intraday)
            st.dataframe(localized_frame(observed[[k for k in ["source_time", "price", "feed"] if k in observed]]), hide_index=True, width="stretch")


def render_news(store, settings):
    st.subheader(t("News intelligence"))
    st.caption(t("Inspect the source, category and evidence before acting on an interpretation."))
    st.caption(t("Article text, model explanations and source evidence are preserved in their original language."))
    rows = store.news_rows(limit=300)
    c1, c2, c3 = st.columns(3)
    symbol = c1.selectbox(t("Stock"), ["All stocks"] + settings.symbols, format_func=partial(translate, language=language()), key=f"news_stock_{settings.mode}")
    category = c2.selectbox(t("News category"), ["All categories"] + CATEGORIES, format_func=partial(translate, language=language()), key=f"news_category_{settings.mode}")
    sentiment = c3.selectbox(t("Sentiment"), ["All sentiments", "positive", "negative", "neutral", "unclear"], format_func=partial(translate, language=language()), key=f"news_sentiment_{settings.mode}")
    count = 0
    for row in rows:
        analysis = row.get("analysis") or {}
        categories = analysis.get("categories", [])
        impacts = analysis.get("impacts", [])
        if symbol != "All stocks" and symbol not in row.get("symbols", []):
            continue
        if category != "All categories" and category not in categories:
            continue
        selected_impacts = [i for i in impacts if symbol == "All stocks" or i.get("symbol") == symbol]
        if sentiment != "All sentiments" and not any(i.get("sentiment") == sentiment for i in selected_impacts):
            continue
        count += 1
        with st.container(border=True):
            st.markdown(f"**{row.get('headline', t('Untitled'))}**")
            st.caption(f"{row.get('source', t('Unknown source'))} · {display_time(row.get('created_at'))} · {', '.join(row.get('symbols', [])) or t('No verified stock association')}")
            if categories:
                st.caption(" / ".join(t(category) for category in categories))
            status = analysis_status(row)
            if status != "success":
                st.info(t("Analysis: {status}. {error}", status=t(status), error=localize_log(analysis.get("error", ""), language())))
                st.write(row.get("summary", ""))
            else:
                st.write(analysis.get("summary") or row.get("summary", ""))
                for impact in selected_impacts:
                    st.write(f"**{impact.get('symbol', '')} · {t(impact.get('sentiment', 'unclear')).upper()}** — {impact.get('reason', '')}")
                    if impact.get("evidence"):
                        st.caption(t("Source evidence"))
                        st.text(impact["evidence"])
                if not selected_impacts:
                    st.caption(t("No evidence-backed individual-stock impact available."))
            url = str(row.get("url", ""))
            if url.startswith(("https://", "http://")):
                st.link_button(t("Read original source"), url)
            with st.expander(t("Audit trail")):
                st.caption(t("Model: {model} · Prompt: {prompt}", model=analysis.get("model", t("not run")), prompt=analysis.get("prompt_version", t("not run"))))
                st.caption(t("Received: {received} · Analysed: {analysed}", received=display_time(row.get("received_at")), analysed=display_time(analysis.get("analyzed_at"))))
                st.caption(t("Article ID: {id} · Version: {version}", id=row.get("id"), version=str(row.get("hash", ""))[:16]))
                st.text(row.get("summary", ""))
    if not count:
        st.info(t("No saved articles match these filters."))


def render_alerts(store, settings, can_edit):
    st.subheader(t("Alerts & validation"))
    st.caption(t("Price crossings are reproducible observations. News sentiment is not an upward-price probability."))
    with st.expander(t("News alert subscription")):
        with st.form(f"news_subscription_{settings.mode}"):
            subscribed = st.multiselect(t("Notify me about"), CATEGORIES, default=None if f"subscribed_{settings.mode}" in st.session_state else store.get_state("news_categories", []),
                                        format_func=partial(translate, language=language()), key=f"subscribed_{settings.mode}", help=t("An empty selection subscribes to every category."))
            if st.form_submit_button(t("Save news subscription"), key=f"save_subscription_{settings.mode}", disabled=not can_edit) and can_edit:
                store.put_state("news_categories", subscribed)
                st.success(t("News subscription saved for the background worker."))
    rules = store.rules()
    with st.expander(t("Create a price-crossing rule"), expanded=not rules):
        if not can_edit:
            st.info(t("Live settings are read-only. Use the administrator token in the sidebar to change rules."))
        with st.form(f"create_rule_{settings.mode}", clear_on_submit=False):
            a, b, c = st.columns(3)
            symbol = a.selectbox(t("Rule stock"), settings.symbols, key=f"rule_stock_{settings.mode}")
            direction = b.selectbox(t("Crossing direction"), ["above", "below"], format_func=partial(translate, language=language()), key=f"rule_direction_{settings.mode}")
            threshold = c.number_input(t("Threshold (USD)"), min_value=0.01, value=200.0, step=1.0, key=f"rule_threshold_{settings.mode}")
            require_news = st.checkbox(t("Require recent, evidence-backed news with matching sentiment"), key=f"rule_require_news_{settings.mode}")
            categories = st.multiselect(t("Matching news categories (composite rule)"), CATEGORIES, default=None if f"rule_categories_{settings.mode}" in st.session_state else ["Finance", "Politics & Policy"], format_func=partial(translate, language=language()), key=f"rule_categories_{settings.mode}")
            cooldown = st.number_input(t("Cooldown (minutes)"), min_value=1, max_value=1440, value=30, key=f"rule_cooldown_{settings.mode}")
            submitted = st.form_submit_button(t("Save rule"), key=f"save_rule_{settings.mode}", disabled=not can_edit)
            if submitted and can_edit:
                store.upsert_rule({"symbol": symbol, "direction": direction, "threshold": float(threshold),
                                   "require_news": require_news, "categories": categories,
                                   "cooldown_seconds": int(cooldown) * 60, "enabled": True})
                st.success(t("Rule saved. The first valid observation seeds its baseline; it does not create a crossing alert."))
                st.rerun()
    if rules:
        st.write("**" + t("Saved rules") + "**")
        for rule in rules:
            cols = st.columns([5, 1])
            label = t("{symbol} crosses {direction} ${threshold}", symbol=rule["symbol"], direction=t(rule["direction"]), threshold=f"{float(rule['threshold']):,.2f}")
            if rule.get("require_news"):
                label += t(" · matching news required")
            cols[0].write(label)
            cols[0].caption(t("{state} · cooldown {minutes} min", state=t("Enabled" if rule.get("enabled") else "Disabled"), minutes=int(rule.get("cooldown_seconds", 0)) // 60))
            if rule.get("enabled") and cols[1].button(t("Disable"), key=f"disable_{settings.mode}_{rule['id']}", disabled=not can_edit):
                store.disable_rule(rule["id"])
                st.rerun()
    alerts = store.alerts(limit=500)
    st.write("**" + t("Recorded events") + "**")
    if alerts:
        frame = pd.DataFrame(alerts)
        columns = [c for c in ["created_at", "symbol", "kind", "price", "reason", "delivery_status", "data_mode"] if c in frame]
        st.dataframe(localized_frame(frame[columns]), hide_index=True, width="stretch")
        st.download_button(t("Download alert audit CSV"), as_csv(alerts), file_name=f"newslens_{settings.mode}_alerts.csv", mime="text/csv")
    else:
        st.info(t("No alerts recorded. A new rule waits for a valid baseline and a subsequent crossing."))
    with st.expander(t("Validation & operating notes"), expanded=False):
        st.markdown(t("""- **Data accuracy:** check source timestamps, feed, market state and stale-data handling.
- **AI quality:** label real articles independently, then evaluate category, stock association and sentiment on held-out examples.
- **Rule correctness:** replay threshold crossings, stale gaps, restarts and duplicate events.
- **Strategy outcomes:** collect later observations before measuring returns; missing future observations stay pending.
- **Demo evidence:** synthetic fixtures test application flow only; they do not measure market prediction accuracy."""))
        st.code("python -m unittest discover -s tests -v\npython -m newslens.evaluation", language="bash")
    st.write("**" + t("Replay status" if settings.mode == "demo" else "Worker status") + "**")
    heartbeat = store.get_state("worker_heartbeat", None)
    if isinstance(heartbeat, dict):
        st.json(heartbeat)
    else:
        label = t("Replay clock / fixture heartbeat" if settings.mode == "demo" else "Last worker heartbeat")
        st.caption(f"{label}: {display_time(heartbeat)}")
    st.caption(t("The webpage does not launch a worker. Live collection and Telegram delivery require the separate worker process."))
    logs = store.logs(limit=30)
    if logs:
        with st.expander(t("Recent runtime log")):
            frame = pd.DataFrame(logs)
            frame["message"] = frame["message"].map(lambda message: localize_log(message, language()))
            st.dataframe(localized_frame(frame), hide_index=True, width="stretch")


def render_worker_status(store):
    now = datetime.now(timezone.utc)
    lease = store.get_state("worker_lease", {})
    last_seen = utc_time(store.get_state("worker_last_seen"))
    leased = isinstance(lease, dict) and lease.get("expires", 0) > now.timestamp()
    age = (now - last_seen).total_seconds() if last_seen else None
    if not leased:
        st.warning(t("Background monitoring is stopped · Showing saved data. Start Start_Monitor.command in another terminal."))
    elif age is not None and 0 <= age <= 90:
        st.success(t("Background monitoring is running · Fetching, analysing or waiting for the next cycle. This page refreshes every 30s."))
    else:
        st.warning(t("The worker lease is still valid, but no recent process heartbeat was received. Check the monitor terminal; an API request may still be pending."))
    st.caption(t("Last successful quote fetch: {quotes} · Last successful news fetch: {news} · Last completed cycle: {cycle}",
                 quotes=display_time(store.get_state("quotes_last_success")), news=display_time(store.get_state("news_last_success")),
                 cycle=display_time(store.get_state("worker_heartbeat"))))
    rows = store.news_rows(500)
    statuses = [(row.get("analysis") or {}).get("status", "pending") for row in rows]
    st.caption(t("Recent {total} articles · Pending {pending} · Successful {success} · Insufficient content {insufficient} · Errors {errors} (subject to retry limits)",
                 total=len(rows), pending=statuses.count("pending"), success=statuses.count("success"), insufficient=statuses.count("insufficient"), errors=statuses.count("error")))


def main():
    st.set_page_config(page_title=t("NewsLens · Market intelligence"), page_icon="◉", layout="wide")
    credential_environment()
    st.markdown("<style>.block-container{padding-top:2rem;padding-bottom:3rem} [data-testid='stMetric']{background:#f4f7fc;padding:14px;border-radius:8px}</style>", unsafe_allow_html=True)
    with st.sidebar:
        st.title("◉ NewsLens")
        st.selectbox("Language / 语言", ["English", "中文"], key="ui_language", on_change=refresh_translated_selections)
        st.caption(t("Evidence before action"))
        current = os.getenv("NEWSLENS_MODE", "demo")
        label = st.selectbox(t("Data mode"), ["Synthetic replay", "Live monitoring"], index=1 if current == "live" and "data_mode" not in st.session_state else 0, format_func=partial(translate, language=language()), key="data_mode")
        mode = "demo" if label == "Synthetic replay" else "live"
        page = st.radio(t("Workspace"), ["Market Monitor", "News Intelligence", "Alerts & Validation"], format_func=partial(translate, language=language()), key="workspace", label_visibility="collapsed")
        st.divider()
        st.caption(t("Single-market monitoring · USD · UTC"))
        can_edit = mode == "demo"
        if mode == "live":
            expected = os.getenv("NEWSLENS_ADMIN_TOKEN", "")
            supplied = st.text_input(t("Administrator token"), type="password", key="admin_token", help=t("Only needed to change live rules. Set NEWSLENS_ADMIN_TOKEN on the server."))
            can_edit = bool(expected and supplied and hmac.compare_digest(expected, supplied))
            st.caption(t("Live rule editing unlocked" if can_edit else "Live rules are read-only"))
    try:
        settings = selected_settings(mode)
        store = Store(settings.db_path)
        store.init()
    except (ValueError, OSError):
        st.error(t("Configuration could not be loaded. Check config.toml and the database path."))
        st.stop()
    if mode == "demo":
        from newslens.demo import seed_demo, advance_demo
        seed_demo(store, reset=False)
        st.caption(t("Replay clock: {clock} · Step {step}", clock=display_time(store.get_state("demo_clock")), step=store.get_state("demo_step", 0)))
        st.warning(t("SYNTHETIC REPLAY · Hand-authored prices and annotated news fixtures. These are not live prices, real historical data or AI outputs."), icon="⚗️")
        with st.sidebar:
            if st.button(t("Advance replay →"), key="advance_replay", width="stretch"):
                advance_demo(store)
                st.rerun()
            st.caption(t("Each click advances a deterministic test scenario."))
    else:
        st.info(t("LIVE MONITORING · Feed: {feed}. Polling targets when the worker is running: quotes {quotes}s; news {news}s. Source timestamps determine freshness.", feed=settings.feed.upper(), quotes=settings.quote_interval, news=settings.news_interval))
    st.title(t(page))
    st.caption(t("Automated tracking. Traceable news analysis. Explainable event alerts."))

    @st.fragment(run_every="30s")
    def workspace():
        if mode == "live":
            render_worker_status(store)
        if page == "Market Monitor":
            render_market(store, settings)
        elif page == "News Intelligence":
            render_news(store, settings)
        else:
            render_alerts(store, settings, can_edit)
    workspace()


if __name__ == "__main__":
    main()
