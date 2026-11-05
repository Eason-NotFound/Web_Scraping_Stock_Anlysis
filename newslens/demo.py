"""Deterministic SYNTHETIC replay. No real prices, historical returns or AI calls."""
import argparse
from datetime import datetime, timedelta, timezone
from .config import load_settings
from .news_analysis import validate_analysis
from .storage import Store
from .worker import process_quotes, process_news_alerts

ANCHOR=datetime(2026,1,5,15,0,tzinfo=timezone.utc)
SYMBOLS=["AAPL","MSFT","NVDA","AMD","JPM","BAC","XOM","CVX","JNJ","UNH"]

def _quotes(step):
    moves=[0,0.4,1.4,0.7,-1.5,-0.6,1.8,2.3]
    t=ANCHOR+timedelta(minutes=step)
    return [dict(symbol=s,price=round(100+i*20+moves[step%len(moves)],2),previous_close=100+i*20,
                 source_time=t.isoformat(),received_at=t.isoformat(),feed="synthetic-fixture",
                 market_open=True,data_mode="demo") for i,s in enumerate(SYMBOLS)]

def seed_demo(store, reset=False):
    store.init(); store.assert_mode("demo")
    if store.get_state("demo_seeded") and not reset: return
    if reset:
        with store.connect() as c:
            for table in ("quotes","bars","news_versions","rules","alerts","state","logs"):
                c.execute(f"DELETE FROM {table}")
        store.assert_mode("demo")
    bars=[]
    days=[]; day=ANCHOR-timedelta(days=1)
    while len(days)<40:
        if day.weekday()<5: days.append(day)
        day-=timedelta(days=1)
    for i,s in enumerate(SYMBOLS):
        for j,d in enumerate(reversed(days)):
            t=d.replace(hour=5,minute=0,second=0)
            bars.append(dict(symbol=s,time=t.isoformat(),close=round(94+i*20+j*0.15,2)))
    store.save_bars(bars)
    fixtures=[
        ("AAPL","Finance","positive","AAPL reports higher quarterly profit.","AAPL reported higher quarterly profit in this synthetic teaching example.","reported higher quarterly profit"),
        ("NVDA","Politics & Policy","negative","New export limits affect NVDA shipments.","New export limits affect NVDA shipments in this synthetic teaching example.","export limits affect NVDA shipments"),
        ("JPM","Macro","unclear","Interest-rate outlook remains uncertain.","JPM discussed an uncertain interest-rate outlook in this synthetic teaching example.","uncertain interest-rate outlook"),
        ("XOM","Politics & Policy","positive","XOM receives approval for a project.","XOM receives approval for a project in this synthetic teaching example.","receives approval for a project"),
    ]
    for i,(symbol,category,sentiment,title,summary,evidence) in enumerate(fixtures):
        stamp=(ANCHOR-timedelta(minutes=10-i)).isoformat()
        n=dict(id=f"synthetic-{i}",headline=title,summary=summary,url="",symbols=[symbol],created_at=stamp,
               updated_at=stamp,received_at=ANCHOR.isoformat(),source="Hand-authored synthetic fixture",data_mode="demo")
        h=store.save_news(n)
        a=validate_analysis(dict(categories=[category],summary=summary,
            impacts=[dict(symbol=symbol,sentiment=sentiment,evidence=evidence,reason="Fixture annotation for software verification only.")]),n)
        a.update(status="success",model="hand-authored-fixture (not AI)",prompt_version="fixture-v1",
                 analyzed_at=ANCHOR.isoformat(),error="")
        store.save_analysis(n["id"],h,a)
    store.upsert_rule(dict(symbol="AAPL",direction="above",threshold=101,enabled=True,cooldown_seconds=1800,require_news=True,categories=["Finance"]))
    store.upsert_rule(dict(symbol="NVDA",direction="below",threshold=139,enabled=True,cooldown_seconds=1800,require_news=True,categories=["Politics & Policy"]))
    process_quotes(store,_quotes(0),ANCHOR)
    process_news_alerts(store,ANCHOR)
    store.put_state("demo_seeded",True); store.put_state("demo_step",0)
    store.put_state("demo_clock",ANCHOR.isoformat()); store.put_state("worker_heartbeat",ANCHOR.isoformat())
    store.log("INFO","Synthetic replay initialized. Annotated fixtures; no network or AI calls.")

def advance_demo(store):
    seed_demo(store)
    step=int(store.get_state("demo_step",0))+1
    now=ANCHOR+timedelta(minutes=step)
    count=process_quotes(store,_quotes(step),now)
    store.put_state("demo_step",step); store.put_state("demo_clock",now.isoformat())
    store.put_state("worker_heartbeat",now.isoformat())
    store.log("INFO",f"Synthetic replay step {step}; {count} new price/composite alert(s)")
    return count

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset",action="store_true"); parser.add_argument("--step",action="store_true")
    args=parser.parse_args(argv); settings=load_settings()
    if settings.mode!="demo":
        print("Refusing to seed a live database. Set NEWSLENS_MODE=demo."); return 2
    store=Store(settings.db_path); seed_demo(store,args.reset)
    if args.step: advance_demo(store)
    print(f"SYNTHETIC replay: step {store.get_state('demo_step')}; {store.counts()}")
    return 0

if __name__=="__main__": raise SystemExit(main())
