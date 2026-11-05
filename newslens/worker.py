"""Independent monitor. Run --once for diagnostics, otherwise bounded polling."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import os
import signal
import time
import uuid

from .config import load_settings
from .data_provider import AlpacaProvider, ProviderError
from .news_analysis import Analyzer
from .notifications import deliver_pending
from .signals import evaluate_rule, news_alert, parse_time
from .storage import Store, utcnow

class ConsoleStore(Store):
    """Mirror safe runtime logs to the user's terminal as each step happens."""
    def log(self, level, message):
        text = str(message)
        secret_names = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "LLM_API_KEY",
                        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NEWSLENS_ADMIN_TOKEN")
        secrets = sorted({os.getenv(k) for k in secret_names if os.getenv(k)}, key=len, reverse=True)
        for secret in secrets:
            text = text.replace(secret, "[REDACTED]")
        text = text[:1000]
        super().log(level, text)
        stamp = datetime.now().astimezone().strftime("%H:%M:%S %Z")
        print(f"[{stamp}] {level} {text}", flush=True)


def event_key(*values):
    return hashlib.sha256("|".join(str(v) for v in values).encode()).hexdigest()

def process_quotes(store, quotes, now, max_age_seconds=180):
    """Persist baselines across polls and restarts; first observation is not a trade."""
    news=store.news_rows(300)
    rules=[r for r in store.rules() if r.get("enabled",True)]
    emitted=0
    for quote in quotes:
        try:
            source=parse_time(quote["source_time"])
            received=parse_time(quote["received_at"])
            decision=parse_time(now)
            if source>received or received>decision:
                store.log("WARNING","Future quote rejected before persistence")
                continue
        except (ValueError,TypeError,KeyError):
            store.log("WARNING","Ambiguous quote timestamp rejected")
            continue
        store.save_quote(quote)
        for rule in rules:
            if rule["symbol"]!=quote["symbol"]:
                continue
            key=f"baseline:{rule['id']}"
            previous=store.get_state(key)
            if previous and parse_time(quote["source_time"])<parse_time(previous["source_time"]):
                continue
            candidate=evaluate_rule(rule,previous,quote,news,now,max_age_seconds)
            if candidate:
                last=store.get_state(f"cooldown:{rule['id']}")
                elapsed=(parse_time(now)-parse_time(last)).total_seconds() if last else float("inf")
                if elapsed>=rule.get("cooldown_seconds",1800):
                    candidate.update(event_id=event_key("price",rule["id"],quote["source_time"],quote["feed"],quote["data_mode"]),feed=quote["feed"])
                    if store.create_alert(candidate):
                        emitted+=1
                    store.put_state(f"cooldown:{rule['id']}",parse_time(now).isoformat())
            store.put_state(key,quote)
    return emitted

def process_news_alerts(store, now, categories=None, symbols=None):
    emitted=0
    for news in store.news_rows(300):
        for alert in news_alert(news,news.get("analysis"),now,categories):
            if symbols is not None and alert["symbol"] not in symbols:
                continue
            # Stable article ID across revisions plus normalized headline handles
            # repeated polls and common syndications without repeated notifications.
            identity=" ".join(news["headline"].lower().split())
            date=news["created_at"][:10]
            alert["event_id"]=event_key("news",alert["symbol"],identity,date,alert["data_mode"])
            alert["feed"]=None
            if store.create_alert(alert): emitted+=1
    return emitted

def run_cycle(settings, store, provider, analyzer, *, prices=True, news=True):
    store.assert_mode("live")
    now=datetime.now(timezone.utc)
    if prices:
        try:
            store.log("INFO", f"Fetching quotes / 抓取行情: {len(settings.symbols)} symbols")
            quotes=provider.snapshots(settings.symbols)
            count=process_quotes(store,quotes,datetime.now(timezone.utc),settings.max_age_seconds)
            store.put_state("quotes_last_success",utcnow())
            store.log("INFO",f"Quotes: {len(quotes)}/{len(settings.symbols)}; alerts: {count}")
            if any(q.get("clock_status")=="unavailable" for q in quotes):
                store.log("WARNING","Market clock unavailable; price alerts suspended")
        except (ProviderError,ValueError,KeyError):
            store.log("ERROR","Quote fetch/validation failed; retained last saved data")
    if news:
        try:
            # A rolling overlap catches late deliveries; id/hash dedup makes replay safe.
            store.log("INFO", "Fetching news / 抓取新闻: rolling last 24 hours")
            items=provider.news(settings.symbols,now-timedelta(hours=24))
            count=0
            for item in items:
                store.save_news(item)
                count+=1
            store.put_state("news_last_success",utcnow())
            store.log("INFO", f"News fetched / 新闻入库: {count} received (existing versions deduplicated)")
        except (ProviderError,ValueError,KeyError):
            store.log("ERROR","News fetch/validation failed; retry on next scheduled poll")
    # Bound model calls per cycle, leaving price polling responsive.
    analyzed_count=0
    for item in store.pending_news(limit=300):
        retry_key=f"ai_retry:{item['id']}:{item['hash']}"
        retry=store.get_state(retry_key,{})
        if retry.get("attempts",0)>=3 or retry.get("after",0)>time.time():
            continue
        if analyzed_count>=2:
            break
        store.log("INFO", f"Analyzing news / 正在分析: article {item['id']} ({analyzed_count+1}/2)")
        analysis=analyzer.analyze(item)
        analyzed_count+=1
        store.save_analysis(item["id"],item["hash"],analysis)
        store.log("INFO", f"Analysis saved / 分析已保存: article {item['id']}; status={analysis['status']}")
        if analysis["status"]=="error":
            attempts=retry.get("attempts",0)+1
            store.put_state(retry_key,{"attempts":attempts,"after":time.time()+min(60*2**attempts,900)})
            store.log("WARNING","News analysis failed validation/configuration; bounded retry scheduled")
    process_news_alerts(store,datetime.now(timezone.utc),store.get_state("news_categories",[]),settings.symbols)
    deliver_pending(store)
    store.put_state("worker_heartbeat",utcnow())
    store.log("INFO", f"Cycle complete / 本轮完成: {analyzed_count} articles processed; dashboard refreshes every 30s")

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once",action="store_true",help="One diagnostic cycle; never starts a web app")
    parser.add_argument("--console",action="store_true",help="Show live progress in this terminal")
    args=parser.parse_args(argv)
    settings=load_settings()
    if settings.mode!="live":
        print("Demo is stepped explicitly: python -m newslens.demo --reset / --step")
        return 0
    if not os.getenv("ALPACA_API_KEY") or not os.getenv("ALPACA_SECRET_KEY"):
        print("Live worker needs ALPACA_API_KEY and ALPACA_SECRET_KEY; no network request made.")
        return 2
    store=(ConsoleStore if args.console else Store)(settings.db_path)
    store.init(); store.assert_mode("live")
    owner=uuid.uuid4().hex
    acquired=False
    old_handlers={}
    def stop(*_):
        raise KeyboardInterrupt
    next_quotes=next_news=next_bars=0.0
    try:
        for name in ("SIGTERM", "SIGINT", "SIGHUP"):
            sig=getattr(signal,name,None)
            if sig is not None:
                old_handlers[sig]=signal.signal(sig,stop)
        acquired=store.acquire_lease(owner,time.time(),ttl=1800)
        if not acquired:
            print("Another worker holds the database lease. 已有后台监控，请查看原终端。",flush=True)
            return 2
        store.put_state("worker_last_seen",utcnow())
        store.log("INFO", f"Monitor started / 后台已启动: quotes {settings.quote_interval}s; news {settings.news_interval}s; Ctrl+C stops")
        # Constructors belong inside the lease cleanup boundary too.
        provider=AlpacaProvider(os.getenv("ALPACA_API_KEY"),os.getenv("ALPACA_SECRET_KEY"),settings.feed)
        analyzer=Analyzer(os.getenv("LLM_API_KEY"),os.getenv("LLM_BASE_URL"),os.getenv("LLM_MODEL"))
        for rule in store.rules(): store.put_state(f"baseline:{rule['id']}",None)
        while True:
            tick=time.monotonic()
            if not store.acquire_lease(owner,time.time(),ttl=1800):
                store.log("ERROR", "Worker lease lost; stopping")
                break
            store.put_state("worker_last_seen",utcnow())
            if tick>=next_bars:
                try:
                    store.log("INFO", "Fetching historical bars / 抓取历史日线")
                    bars=provider.daily_bars(settings.symbols)
                    store.save_bars(bars)
                    store.put_state("bars_last_success",utcnow())
                    store.log("INFO", f"Historical bars saved / 日线已保存: {len(bars)}")
                    next_bars=tick+3600
                except (ProviderError,ValueError,KeyError):
                    store.log("ERROR","Historical bars unavailable; retry after 5 minutes")
                    next_bars=tick+300
            prices=tick>=next_quotes; news=tick>=next_news
            if prices or news:
                try:
                    run_cycle(settings,store,provider,analyzer,prices=prices,news=news)
                except Exception as exc:
                    store.log("ERROR",f"Cycle failed ({type(exc).__name__}); retry safely")
                if prices: next_quotes=tick+settings.quote_interval
                if news: next_news=tick+settings.news_interval
                if not args.once:
                    delay=max(0,int(min(next_quotes,next_news)-time.monotonic()))
                    store.log("INFO",f"Waiting / 等待下一轮: about {delay}s; Ctrl+C stops")
            if args.once: break
            time.sleep(1)
    except KeyboardInterrupt:
        store.log("INFO", "Stop requested / 收到停止指令")
    except Exception as exc:
        store.log("ERROR", f"Worker stopped ({type(exc).__name__}); check configuration")
        return 2
    finally:
        # A second interrupt must not prevent lease release; restore callers' handlers.
        for sig in old_handlers: signal.signal(sig,signal.SIG_IGN)
        try:
            if acquired:
                store.release_lease(owner)
                store.log("INFO", "Monitor stopped / 后台已停止; saved data retained")
        finally:
            for sig,handler in old_handlers.items(): signal.signal(sig,handler)
    return 0

if __name__=="__main__": raise SystemExit(main())
