"""Explicitly opt-in Telegram outbox. Ambiguous sends are never blindly retried."""
import json
import os
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

def format_message(alert):
    price=alert.get("price")
    return (f"NewsLens | {alert['symbol']} | {alert['kind']}\n"
            f"Price: {'N/A' if price is None else f'USD {price:.2f}'}\n"
            f"Time: {alert['created_at']}\n{alert['reason']}\n"
            f"Event: {alert['event_id'][:16]}\nObservation alert, not an order.")[:4000]

def deliver_pending(store, opener=None):
    if os.getenv("NEWSLENS_ENABLE_TELEGRAM")!="1":
        return 0
    token=os.getenv("TELEGRAM_BOT_TOKEN",""); chat=os.getenv("TELEGRAM_CHAT_ID","")
    if not token or not chat:
        store.log("WARNING","Telegram enabled but token/chat ID missing; outbox retained")
        return 0
    sent=0
    for alert in store.pending_alerts()[:10]:
        if alert.get("data_mode")!="live":
            store.set_delivery(alert["event_id"],"skipped_demo")
            continue
        # A crash after this durable claim cannot cause an automatic resend.
        # 'sending' remains visible for manual reconciliation after a restart.
        store.set_delivery(alert["event_id"],"sending")
        request=Request(f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id":chat,"text":format_message(alert)}).encode(),
            headers={"Content-Type":"application/json"})
        try:
            with (opener or urlopen)(request,timeout=12) as response:
                result=json.loads(response.read(100_000))
            if result.get("ok") is True:
                store.set_delivery(alert["event_id"],"sent"); sent+=1
            else:
                store.set_delivery(alert["event_id"],"failed","Telegram rejected the request; inspect configuration")
        except HTTPError as exc:
            status="failed" if exc.code in {400,401,403,404,429} else "unknown"
            store.set_delivery(alert["event_id"],status,f"Telegram HTTP {exc.code}; no automatic resend")
        except (URLError,TimeoutError,OSError,ValueError):
            store.set_delivery(alert["event_id"],"unknown","Send result uncertain; reconcile before retry")
    return sent
